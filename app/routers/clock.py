"""轮次计时接口。

- POST /tournaments/{id}/rounds/{round_no}/clock/actions：计时命令
  （start / pause / resume / extend）。request_id 幂等（重复提交
  返回首次结果）、expected_version 乐观并发；命令、幂等日志与
  clock.* 事件在同一事务提交，非法状态或版本冲突返回 409 且不落库；
- GET /tournaments/{id}/rounds/{round_no}/clock：版本、状态及各场
  非轮空对局的截止时刻与剩余秒数（由服务端 UTC 锚点与累计暂停
  时长实时推算，无逐秒写库任务，重启后读数连续）。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..schemas import ClockActionRequest, ClockActionResponse, ClockView
from ..services.clock import apply_clock_action, get_clock_view
from .tournaments import get_tournament_or_404

router = APIRouter()


@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/clock/actions",
    response_model=ClockActionResponse,
)
def clock_action(
    tournament_id: int,
    round_no: int,
    payload: ClockActionRequest,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：幂等检查、版本校验、状态迁移、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        response, event = apply_clock_action(
            db, tournament_id, round_no, payload
        )
        # 重放的请求不落库、不发事件，直接返回首次的响应快照
        if event is not None:
            commit_and_publish(db, [event])
        return response


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/clock",
    response_model=ClockView,
)
def get_clock(
    tournament_id: int,
    round_no: int,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    return get_clock_view(db, tournament_id, round_no)
