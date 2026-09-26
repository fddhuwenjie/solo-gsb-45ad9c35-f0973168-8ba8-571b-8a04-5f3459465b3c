"""轮次计时：UTC 锚点 + 累计暂停的持久化时钟。

设计要点：
- 每轮至多一个 RoundClock 行（start 时创建）：记录总时长
  duration_seconds、启动锚点 started_at、已结算的累计暂停时长
  accumulated_pause_seconds 与进行中的暂停起点 paused_at；
  各场加时累计在 extensions（str(match_id) -> 秒）；
- 剩余时间完全由「锚点 + 累计暂停 + 总时长 + 加时」在读取时推算，
  不运行逐秒写库的后台任务，容器重启后读数自然连续；
- 全局暂停冻结所有对局（暂停期间剩余时间不变），恢复后延续；
- 每个成功命令把 version +1，并与对应 clock.* 事件在同一事务
  提交；request_id 幂等：同请求重放返回首次结果，同号不同请求
  返回 409；非法状态或版本冲突返回 409 且不落库；
- 已提交赛果的对局不能再加时，已结束轮次拒绝一切计时命令。

注意：apply_clock_action 只在调用方事务内写入并 flush，不提交；
时钟行、幂等日志与 clock.* 事件由路由层在同一事务中统一提交。
"""
import hashlib
import json
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    CLOCK_EXTENDED,
    CLOCK_PAUSED,
    CLOCK_RESUMED,
    CLOCK_STARTED,
    append_event,
)
from ..models import ClockAction, Event, Match, Round, RoundClock
from ..schemas import (
    ClockActionRequest,
    ClockActionResponse,
    ClockMatchView,
    ClockView,
)


def _get_round_or_404(db: Session, tournament_id: int, round_no: int) -> Round:
    round_obj = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if round_obj is None:
        raise HTTPException(status_code=404, detail="该轮次不存在")
    return round_obj


def _timed_matches(db: Session, round_id: int) -> list[Match]:
    """本轮参与计时的对局（非轮空，按 id 升序）。"""
    return (
        db.query(Match)
        .filter(
            Match.round_id == round_id,
            Match.player_black_id.is_not(None),
        )
        .order_by(Match.id)
        .all()
    )


def _effective_pause_seconds(clock: RoundClock, now: datetime) -> float:
    """截至 now 的累计暂停时长（含进行中暂停已冻结的部分）。"""
    total = clock.accumulated_pause_seconds
    if clock.status == "paused" and clock.paused_at is not None:
        total += (now - clock.paused_at).total_seconds()
    return total


def match_deadline(
    clock: RoundClock, match_id: int, now: datetime
) -> datetime | None:
    """该场对局在 now 时刻的有效截止时刻；计时未开始返回 None。

    暂停中按「此刻恢复」投影：截止时刻随暂停继续而后移，
    剩余时间（截止时刻 - now）保持冻结。
    """
    if clock.started_at is None:
        return None
    extra = int(clock.extensions.get(str(match_id), 0))
    return clock.started_at + timedelta(
        seconds=clock.duration_seconds + extra + _effective_pause_seconds(clock, now)
    )


def build_clock_view(
    round_obj: Round,
    clock: RoundClock | None,
    matches: list[Match],
    now: datetime,
) -> ClockView:
    """由持久化锚点推算当前时钟视图（纯计算，不写库）。"""
    if clock is None:
        return ClockView(
            round_id=round_obj.id,
            round_no=round_obj.round_no,
            version=0,
            status="not_started",
            server_time=now,
            matches=[ClockMatchView(match_id=m.id) for m in matches],
        )
    match_views = []
    for m in matches:
        deadline = match_deadline(clock, m.id, now)
        match_views.append(
            ClockMatchView(
                match_id=m.id,
                extra_seconds=int(clock.extensions.get(str(m.id), 0)),
                deadline=deadline,
                remaining_seconds=(
                    round((deadline - now).total_seconds(), 3)
                    if deadline is not None
                    else None
                ),
            )
        )
    return ClockView(
        round_id=round_obj.id,
        round_no=round_obj.round_no,
        version=clock.version,
        status=clock.status,
        duration_seconds=clock.duration_seconds,
        started_at=clock.started_at,
        paused_at=clock.paused_at if clock.status == "paused" else None,
        accumulated_pause_seconds=round(clock.accumulated_pause_seconds, 3),
        server_time=now,
        matches=match_views,
    )


def get_clock_view(db: Session, tournament_id: int, round_no: int) -> ClockView:
    round_obj = _get_round_or_404(db, tournament_id, round_no)
    clock = db.get(RoundClock, round_obj.id)
    matches = _timed_matches(db, round_obj.id)
    return build_clock_view(round_obj, clock, matches, datetime.utcnow())


def _fingerprint(payload: ClockActionRequest) -> str:
    """请求载荷指纹：同一 request_id 仅当载荷完全一致时视为重放。"""
    canonical = json.dumps(
        {
            "action": payload.action,
            "expected_version": payload.expected_version,
            "duration_seconds": payload.duration_seconds,
            "match_id": payload.match_id,
            "extra_seconds": payload.extra_seconds,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_clock(clock: RoundClock | None) -> RoundClock:
    if clock is None:
        raise HTTPException(status_code=409, detail="该轮计时尚未开始")
    return clock


def apply_clock_action(
    db: Session,
    tournament_id: int,
    round_no: int,
    payload: ClockActionRequest,
) -> tuple[ClockActionResponse, Event | None]:
    """应用一条计时命令，返回（响应快照, 待提交事件）。

    重复 request_id（载荷一致）直接返回首次的响应快照，事件为
    None（不重复执行、不重复发事件）；版本冲突、非法状态等抛
    409/404/422，且不落任何数据。调用方负责在同事务提交。
    """
    round_obj = _get_round_or_404(db, tournament_id, round_no)
    fingerprint = _fingerprint(payload)

    # 幂等：request_id 已应用过——载荷一致返回原结果，否则 409
    existing = (
        db.query(ClockAction)
        .filter(
            ClockAction.round_id == round_obj.id,
            ClockAction.request_id == payload.request_id,
        )
        .first()
    )
    if existing is not None:
        if existing.fingerprint != fingerprint:
            raise HTTPException(
                status_code=409, detail="request_id 已被不同的计时请求使用"
            )
        return ClockActionResponse(**existing.response), None

    clock = db.get(RoundClock, round_obj.id)
    current_version = clock.version if clock is not None else 0
    if payload.expected_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=f"计时版本冲突：期望 {payload.expected_version}，"
            f"当前为 {current_version}",
        )
    if round_obj.status != "open":
        raise HTTPException(
            status_code=409, detail="该轮次已结束，不能执行计时命令"
        )

    now = datetime.utcnow()
    event_payload: dict = {"round_id": round_obj.id, "round_no": round_obj.round_no}

    if payload.action == "start":
        if clock is not None:
            raise HTTPException(status_code=409, detail="该轮计时已开始")
        if payload.duration_seconds is None:
            raise HTTPException(
                status_code=422, detail="start 命令需要 duration_seconds"
            )
        clock = RoundClock(
            round_id=round_obj.id,
            version=0,
            status="running",
            duration_seconds=payload.duration_seconds,
            started_at=now,
            paused_at=None,
            accumulated_pause_seconds=0.0,
            extensions={},
        )
        db.add(clock)
        event_type = CLOCK_STARTED
        event_payload["duration_seconds"] = payload.duration_seconds
    elif payload.action == "pause":
        clock = _require_clock(clock)
        if clock.status != "running":
            raise HTTPException(
                status_code=409, detail="仅进行中的计时可以暂停"
            )
        clock.status = "paused"
        clock.paused_at = now
        event_type = CLOCK_PAUSED
    elif payload.action == "resume":
        clock = _require_clock(clock)
        if clock.status != "paused":
            raise HTTPException(
                status_code=409, detail="仅已暂停的计时可以恢复"
            )
        clock.accumulated_pause_seconds += (
            now - clock.paused_at
        ).total_seconds()
        clock.paused_at = None
        clock.status = "running"
        event_type = CLOCK_RESUMED
        event_payload["accumulated_pause_seconds"] = round(
            clock.accumulated_pause_seconds, 3
        )
    else:  # extend
        clock = _require_clock(clock)
        if payload.match_id is None or payload.extra_seconds is None:
            raise HTTPException(
                status_code=422, detail="extend 命令需要 match_id 与 extra_seconds"
            )
        match = db.get(Match, payload.match_id)
        if match is None or match.round_id != round_obj.id:
            raise HTTPException(status_code=404, detail="对局不存在")
        if match.player_black_id is None:
            raise HTTPException(
                status_code=409, detail="轮空对局不参与计时，不能加时"
            )
        if match.score_white is not None:
            raise HTTPException(
                status_code=409, detail="该场赛果已提交，不能再加时"
            )
        extensions = dict(clock.extensions)
        key = str(match.id)
        extensions[key] = int(extensions.get(key, 0)) + payload.extra_seconds
        clock.extensions = extensions
        event_type = CLOCK_EXTENDED
        event_payload["match_id"] = match.id
        event_payload["extra_seconds"] = payload.extra_seconds
        event_payload["total_extra_seconds"] = extensions[key]

    clock.version = current_version + 1
    db.flush()  # 取得时钟行状态，供视图与事件使用

    event_payload["version"] = clock.version
    event = append_event(db, tournament_id, event_type, event_payload)

    view = build_clock_view(
        round_obj, clock, _timed_matches(db, round_obj.id), now
    )
    response = ClockActionResponse(
        **view.model_dump(),
        request_id=payload.request_id,
        action=payload.action,
    )
    # 幂等日志与业务变更同事务落库：重放时原样返回该快照
    db.add(
        ClockAction(
            round_id=round_obj.id,
            request_id=payload.request_id,
            fingerprint=fingerprint,
            action=payload.action,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event
