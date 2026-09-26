"""轮次计时专项测试：持久化时钟、幂等、版本冲突、截止时刻约束。"""
import time
from datetime import datetime, timezone


def create_tournament(client, name="计时测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def gen_round(client, tid):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201
    return r.json()


def clock_url(tid, round_no):
    return f"/tournaments/{tid}/rounds/{round_no}/clock"


def get_clock(client, tid, round_no=1):
    r = client.get(clock_url(tid, round_no))
    assert r.status_code == 200
    return r.json()


def act(client, tid, round_no=1, **payload):
    return client.post(f"{clock_url(tid, round_no)}/actions", json=payload)


def parse_z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def remaining_by_match(view):
    return {m["match_id"]: m["remaining_seconds"] for m in view["matches"]}


def event_types(client, tid):
    r = client.get(f"/tournaments/{tid}/events", params={"limit": 200})
    assert r.status_code == 200
    return [e["type"] for e in r.json()["items"]]


def test_clock_view_before_start(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁", "戊"])  # 5 人，1 轮空
    rnd = gen_round(client, tid)

    view = get_clock(client, tid, rnd["round_no"])
    assert view["version"] == 0
    assert view["status"] == "not_started"
    assert view["duration_seconds"] is None
    assert view["started_at"] is None
    assert view["paused_at"] is None
    assert view["accumulated_pause_seconds"] == 0.0
    # 仅列出非轮空对局，且尚无截止时刻
    non_bye_ids = {m["id"] for m in rnd["matches"] if not m["is_bye"]}
    assert {m["match_id"] for m in view["matches"]} == non_bye_ids
    assert all(m["deadline"] is None for m in view["matches"])
    assert all(m["remaining_seconds"] is None for m in view["matches"])
    # server_time 为带 Z 的 ISO 8601
    assert view["server_time"].endswith("Z")
    parse_z(view["server_time"])

    # 轮次/赛事不存在
    assert client.get(clock_url(tid, 99)).status_code == 404
    assert client.get(clock_url(999, 1)).status_code == 404


def test_clock_start_pause_resume_extend_flow(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = gen_round(client, tid)
    m1, m2 = (m["id"] for m in rnd["matches"])

    # start：设置总时长，版本 0 -> 1
    r = act(
        client,
        tid,
        request_id="cmd-1",
        expected_version=0,
        action="start",
        duration_seconds=1800,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request_id"] == "cmd-1"
    assert body["action"] == "start"
    assert body["version"] == 1
    assert body["status"] == "running"
    assert body["duration_seconds"] == 1800
    started_at = parse_z(body["started_at"])
    assert abs(
        (started_at - datetime.now(timezone.utc)).total_seconds()
    ) < 10
    for mv in body["matches"]:
        assert mv["extra_seconds"] == 0
        deadline = parse_z(mv["deadline"])
        assert abs((deadline - started_at).total_seconds() - 1800) < 1
        assert 1790 < mv["remaining_seconds"] <= 1800

    # pause：冻结所有对局，版本 1 -> 2
    r = act(client, tid, request_id="cmd-2", expected_version=1, action="pause")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    assert body["status"] == "paused"
    assert body["paused_at"] is not None
    frozen = remaining_by_match(body)

    time.sleep(0.3)
    later = remaining_by_match(get_clock(client, tid))
    # 暂停期间剩余时间冻结（不随墙钟流逝）
    for mid in frozen:
        assert abs(later[mid] - frozen[mid]) < 0.05

    # resume：延续计时，累计暂停时长落库，版本 2 -> 3
    r = act(client, tid, request_id="cmd-3", expected_version=2, action="resume")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 3
    assert body["status"] == "running"
    assert body["paused_at"] is None
    assert body["accumulated_pause_seconds"] >= 0.2
    resumed = remaining_by_match(body)
    for mid in frozen:
        # 恢复时刻的剩余时间与冻结值衔接（允许请求耗时误差）
        assert abs(resumed[mid] - frozen[mid]) < 5

    # 恢复后剩余时间继续流逝
    time.sleep(0.3)
    later = remaining_by_match(get_clock(client, tid))
    for mid in resumed:
        assert 0.2 < resumed[mid] - later[mid] < 5

    # extend：只给指定对局加时，版本 3 -> 4
    r = act(
        client,
        tid,
        request_id="cmd-4",
        expected_version=3,
        action="extend",
        match_id=m1,
        extra_seconds=300,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 4
    after = remaining_by_match(body)
    assert 299 < after[m1] - later[m1] < 301 + 5
    assert abs(after[m2] - later[m2]) < 5
    extras = {m["match_id"]: m["extra_seconds"] for m in body["matches"]}
    assert extras == {m1: 300, m2: 0}

    # 同一对局可多次加时，累计生效
    r = act(
        client,
        tid,
        request_id="cmd-5",
        expected_version=4,
        action="extend",
        match_id=m1,
        extra_seconds=60,
    )
    assert r.status_code == 200
    extras = {m["match_id"]: m["extra_seconds"] for m in r.json()["matches"]}
    assert extras[m1] == 360

    # 计时事件与命令同事务落库
    types = event_types(client, tid)
    for t in (
        "clock.started",
        "clock.paused",
        "clock.resumed",
        "clock.extended",
    ):
        assert t in types


def test_clock_idempotent_replay(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    gen_round(client, tid)

    payload = {
        "request_id": "req-A",
        "expected_version": 0,
        "action": "start",
        "duration_seconds": 900,
    }
    r1 = act(client, tid, **payload)
    assert r1.status_code == 200
    events_after_first = event_types(client, tid)

    # 重复 request_id（载荷一致）：返回首次的同一结果，版本不变、
    # 不产生新事件
    r2 = act(client, tid, **payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert get_clock(client, tid)["version"] == 1
    assert event_types(client, tid) == events_after_first

    # 同一 request_id 但载荷不同：409 且不落库
    r3 = act(
        client,
        tid,
        request_id="req-A",
        expected_version=1,
        action="pause",
    )
    assert r3.status_code == 409
    assert get_clock(client, tid)["version"] == 1
    assert get_clock(client, tid)["status"] == "running"
    assert event_types(client, tid) == events_after_first


def test_clock_version_conflict_and_illegal_states(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    gen_round(client, tid)

    # 版本冲突：409 且不落库
    r = act(
        client,
        tid,
        request_id="v-1",
        expected_version=3,
        action="start",
        duration_seconds=600,
    )
    assert r.status_code == 409
    assert get_clock(client, tid)["version"] == 0

    # 未开始时的非法命令
    for action in ("pause", "resume", "extend"):
        r = act(
            client,
            tid,
            request_id=f"v-illegal-{action}",
            expected_version=0,
            action=action,
            match_id=1,
            extra_seconds=10,
        )
        assert r.status_code == 409, action
    assert get_clock(client, tid)["version"] == 0

    # start 缺少 duration_seconds：422
    r = act(
        client, tid, request_id="v-2", expected_version=0, action="start"
    )
    assert r.status_code == 422

    # 非法 action：422（请求校验失败）
    r = act(
        client,
        tid,
        request_id="v-3",
        expected_version=0,
        action="reset",
    )
    assert r.status_code == 422

    r = act(
        client,
        tid,
        request_id="v-4",
        expected_version=0,
        action="start",
        duration_seconds=600,
    )
    assert r.status_code == 200

    # 重复 start：409
    r = act(
        client,
        tid,
        request_id="v-5",
        expected_version=1,
        action="start",
        duration_seconds=600,
    )
    assert r.status_code == 409

    # running 时 resume：409
    r = act(client, tid, request_id="v-6", expected_version=1, action="resume")
    assert r.status_code == 409

    # 过期版本（当前已为 1）：409
    r = act(client, tid, request_id="v-7", expected_version=0, action="pause")
    assert r.status_code == 409

    # pause 后再 pause：409
    assert (
        act(client, tid, request_id="v-8", expected_version=1, action="pause").status_code
        == 200
    )
    r = act(client, tid, request_id="v-9", expected_version=2, action="pause")
    assert r.status_code == 409

    # 失败的命令均未落库：版本与事件流不变
    view = get_clock(client, tid)
    assert view["version"] == 2
    assert view["status"] == "paused"
    assert event_types(client, tid).count("clock.paused") == 1


def test_clock_extend_validation(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁", "戊"])  # 1 轮空
    rnd = gen_round(client, tid)
    bye = next(m for m in rnd["matches"] if m["is_bye"])
    real = next(m for m in rnd["matches"] if not m["is_bye"])

    assert (
        act(
            client,
            tid,
            request_id="e-0",
            expected_version=0,
            action="start",
            duration_seconds=600,
        ).status_code
        == 200
    )

    # 轮空对局不能加时
    r = act(
        client,
        tid,
        request_id="e-1",
        expected_version=1,
        action="extend",
        match_id=bye["id"],
        extra_seconds=60,
    )
    assert r.status_code == 409

    # 不存在的对局 / 其他轮次的对局
    r = act(
        client,
        tid,
        request_id="e-2",
        expected_version=1,
        action="extend",
        match_id=99999,
        extra_seconds=60,
    )
    assert r.status_code == 404

    # 缺少参数：422
    r = act(
        client,
        tid,
        request_id="e-3",
        expected_version=1,
        action="extend",
        match_id=real["id"],
    )
    assert r.status_code == 422
    r = act(
        client,
        tid,
        request_id="e-4",
        expected_version=1,
        action="extend",
        extra_seconds=60,
    )
    assert r.status_code == 422

    # 已提交赛果的对局不能再加时（已提交赛果不受后续命令影响）
    r = client.put(
        f"/matches/{real['id']}/result",
        json={"score_white": 1.0, "score_black": 0.0},
    )
    assert r.status_code == 200
    r = act(
        client,
        tid,
        request_id="e-5",
        expected_version=1,
        action="extend",
        match_id=real["id"],
        extra_seconds=60,
    )
    assert r.status_code == 409

    # 全部校验失败均未改变版本
    assert get_clock(client, tid)["version"] == 1


def test_result_deadline_enforced(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = gen_round(client, tid)
    m1, m2 = (m["id"] for m in rnd["matches"])

    assert (
        act(
            client,
            tid,
            request_id="d-1",
            expected_version=0,
            action="start",
            duration_seconds=1,
        ).status_code
        == 200
    )
    time.sleep(1.2)

    # 超过截止时刻提交：拒绝
    r = client.put(
        f"/matches/{m1}/result", json={"score_white": 1.0, "score_black": 0.0}
    )
    assert r.status_code == 409

    # 加时后可以继续提交（只影响指定对局）
    r = act(
        client,
        tid,
        request_id="d-2",
        expected_version=1,
        action="extend",
        match_id=m1,
        extra_seconds=600,
    )
    assert r.status_code == 200
    r = client.put(
        f"/matches/{m1}/result", json={"score_white": 1.0, "score_black": 0.0}
    )
    assert r.status_code == 200, r.text

    # 另一场未加时，仍然超时拒绝
    r = client.put(
        f"/matches/{m2}/result", json={"score_white": 0.5, "score_black": 0.5}
    )
    assert r.status_code == 409
    r = act(
        client,
        tid,
        request_id="d-3",
        expected_version=2,
        action="extend",
        match_id=m2,
        extra_seconds=600,
    )
    assert r.status_code == 200
    r = client.put(
        f"/matches/{m2}/result", json={"score_white": 0.5, "score_black": 0.5}
    )
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "closed"

    # 已结束轮次拒绝一切后续计时命令（已提交赛果与已结束轮次不受影响）
    for i, action in enumerate(("pause", "resume", "extend", "start")):
        r = act(
            client,
            tid,
            request_id=f"d-closed-{i}",
            expected_version=3,
            action=action,
            duration_seconds=600,
            match_id=m1,
            extra_seconds=10,
        )
        assert r.status_code == 409, action
    # 时钟读数仍可查，版本未被后续命令改变
    view = get_clock(client, tid)
    assert view["version"] == 3
    # 已提交的赛果保持原样
    r = client.get(f"/tournaments/{tid}/rounds/1")
    scores = {m["id"]: (m["score_white"], m["score_black"]) for m in r.json()["matches"]}
    assert scores[m1] == (1.0, 0.0)
    assert scores[m2] == (0.5, 0.5)


def test_pause_defers_deadline(client):
    """全局暂停冻结截止判定：暂停期间不算超时。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = gen_round(client, tid)
    m1 = rnd["matches"][0]["id"]

    assert (
        act(
            client,
            tid,
            request_id="p-1",
            expected_version=0,
            action="start",
            duration_seconds=2,
        ).status_code
        == 200
    )
    assert (
        act(client, tid, request_id="p-2", expected_version=1, action="pause").status_code
        == 200
    )
    # 暂停 1.5s（总时长仅 2s，若不冻结提交时必超时）
    time.sleep(1.5)
    assert (
        act(client, tid, request_id="p-3", expected_version=2, action="resume").status_code
        == 200
    )
    r = client.put(
        f"/matches/{m1}/result", json={"score_white": 1.0, "score_black": 0.0}
    )
    assert r.status_code == 200, r.text


def test_clock_restart_keeps_continuity(client):
    """状态全部落库：新会话（模拟容器重启）读数连续，无逐秒写库。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    gen_round(client, tid)
    assert (
        act(
            client,
            tid,
            request_id="r-1",
            expected_version=0,
            action="start",
            duration_seconds=600,
        ).status_code
        == 200
    )

    # 用全新的数据库会话读取（等价于进程重启后首次访问）
    from app.database import SessionLocal
    from app.services.clock import get_clock_view

    with SessionLocal() as db:
        view = get_clock_view(db, tid, 1)
    assert view.version == 1
    assert view.status == "running"
    for mv in view.matches:
        assert 550 < mv.remaining_seconds <= 600

    # 暂停状态同样持久：新会话中剩余时间保持冻结
    assert (
        act(client, tid, request_id="r-2", expected_version=1, action="pause").status_code
        == 200
    )
    time.sleep(0.2)
    with SessionLocal() as db:
        view2 = get_clock_view(db, tid, 1)
    assert view2.status == "paused"
    frozen = {m.match_id: m.remaining_seconds for m in view2.matches}
    time.sleep(0.2)
    with SessionLocal() as db:
        view3 = get_clock_view(db, tid, 1)
    for m in view3.matches:
        assert abs(m.remaining_seconds - frozen[m.match_id]) < 0.05


def test_clock_action_unknown_round_or_tournament(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    gen_round(client, tid)

    payload = {
        "request_id": "x-1",
        "expected_version": 0,
        "action": "start",
        "duration_seconds": 60,
    }
    assert act(client, tid, round_no=99, **payload).status_code == 404
    assert act(client, 999, **payload).status_code == 404
