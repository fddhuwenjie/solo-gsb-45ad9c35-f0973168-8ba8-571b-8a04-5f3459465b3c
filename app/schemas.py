from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_serializer


def _normalize_tags(tags: list[str]) -> list[str]:
    """标签规范化：去空白、去重、排序；空标签视为非法。"""
    cleaned = sorted({t.strip() for t in tags})
    if any(not t for t in cleaned):
        raise ValueError("标签去空白后不能为空")
    if len(cleaned) > 32:
        raise ValueError("标签数量不能超过 32 个")
    if any(len(t) > 64 for t in cleaned):
        raise ValueError("单个标签长度不能超过 64 个字符")
    return cleaned


# 能力/必需标签列表：自动去空白、去重、排序
Tags = Annotated[list[str], AfterValidator(_normalize_tags)]


# ---------- Tournament ----------

class TournamentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="赛事名称")


class TournamentResponse(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


# ---------- Participant ----------

class ParticipantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="参赛者姓名")
    required_tags: Tags = Field(
        default_factory=list, description="参赛者必需标签（工位须覆盖）"
    )


class ParticipantTagsUpdate(BaseModel):
    required_tags: Tags = Field(description="参赛者必需标签（整体替换）")


class ParticipantsBulkCreate(BaseModel):
    names: list[str] = Field(
        min_length=1,
        description="参赛者姓名列表，编号按此顺序从 1 开始分配",
    )


class ParticipantResponse(BaseModel):
    id: int
    number: int
    name: str
    required_tags: list[str] = []

    model_config = {"from_attributes": True}


# ---------- Match / Round ----------

class MatchResponse(BaseModel):
    id: int
    player_white: ParticipantResponse | None = None
    player_black: ParticipantResponse | None = None
    # 0 / 0.5 / 1；轮空方 1，另一方为 None
    score_white: float | None = None
    score_black: float | None = None
    is_bye: bool = False


class RoundResponse(BaseModel):
    id: int
    round_no: int
    status: str
    matches: list[MatchResponse] = []


class ResultUpdate(BaseModel):
    # 0 / 1 / 2 个半分，即允许 0、0.5、1
    score_white: float = Field(ge=0, le=1, description="小白方得分：0 / 0.5 / 1")
    score_black: float = Field(ge=0, le=1, description="小黑方得分：0 / 0.5 / 1")


# ---------- RoundClock ----------

# 计时相关时长上限：7 天（秒）
CLOCK_MAX_SECONDS = 7 * 24 * 3600


def _serialize_utc_z(v: datetime | None) -> str | None:
    """naive UTC 时间序列化为带 Z 后缀的 ISO 8601；None 原样返回。"""
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ClockActionRequest(BaseModel):
    """计时命令：start / pause / resume / extend。

    - start：开始本轮计时，需带 duration_seconds（各场共同的总时长）；
    - pause / resume：全局暂停 / 恢复（冻结或延续所有对局）；
    - extend：给指定非轮空对局加时，需带 match_id 与 extra_seconds。
    """

    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="客户端请求标识（轮次内幂等）：重复提交返回首次结果",
    )
    expected_version: int = Field(
        ge=0,
        description="期望的当前计时版本（未开始为 0）；不一致返回 409",
    )
    action: Literal["start", "pause", "resume", "extend"] = Field(
        description="计时命令"
    )
    duration_seconds: int | None = Field(
        default=None,
        gt=0,
        le=CLOCK_MAX_SECONDS,
        description="总时长（秒），仅 start 需要",
    )
    match_id: int | None = Field(
        default=None, ge=1, description="加时对局 id，仅 extend 需要"
    )
    extra_seconds: int | None = Field(
        default=None,
        gt=0,
        le=CLOCK_MAX_SECONDS,
        description="追加时长（秒），仅 extend 需要",
    )


class ClockMatchView(BaseModel):
    match_id: int
    # 该场累计加时（秒）
    extra_seconds: int = 0
    # 该场截止时刻（UTC，ISO 8601 Z 结尾）；计时未开始为 None
    deadline: datetime | None = None
    # 距截止的剩余秒数（暂停中为冻结值；为负表示已超时）
    remaining_seconds: float | None = None

    @field_serializer("deadline")
    def serialize_deadline(self, v: datetime | None) -> str | None:
        return _serialize_utc_z(v)


class ClockView(BaseModel):
    round_id: int
    round_no: int
    # 计时版本：每次成功命令 +1；未开始为 0
    version: int
    # not_started / running / paused
    status: str
    # 总时长（秒）；未开始为 None
    duration_seconds: int | None = None
    # 计时启动的 UTC 锚点；未开始为 None
    started_at: datetime | None = None
    # 暂停中的 UTC 起点；非暂停为 None
    paused_at: datetime | None = None
    # 已结算的累计暂停时长（秒）
    accumulated_pause_seconds: float = 0.0
    # 服务端当前 UTC 时间，供客户端校时
    server_time: datetime
    # 各场非轮空对局的截止时刻与剩余秒数
    matches: list[ClockMatchView] = []

    @field_serializer("started_at", "paused_at", "server_time")
    def serialize_utc_fields(self, v: datetime | None) -> str | None:
        return _serialize_utc_z(v)


class ClockActionResponse(ClockView):
    """计时命令应用后的时钟快照；重复 request_id 返回首次的同一快照。"""

    request_id: str
    action: str


# ---------- Standing ----------

class StandingResponse(BaseModel):
    rank: int
    participant_id: int
    number: int
    name: str
    points: float
    buchholz: float
    played: int
    byes: int


# ---------- Event ----------

class EventResponse(BaseModel):
    tournament_id: int
    # 赛事内单调递增序号（每赛事独立，从 1 开始）
    seq: int
    type: str
    # 相关资源标识（如 tournament_id / participant_id / round_id 等）
    payload: dict
    # UTC 发生时间，ISO 8601（Z 结尾）
    occurred_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("occurred_at")
    def serialize_occurred_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EventPageResponse(BaseModel):
    items: list[EventResponse]
    # 本页最后一条事件序号；本页为空时等于请求的 after
    last_seq: int
    # 之后是否还有更多事件
    has_more: bool


# ---------- Station ----------

class StationCreate(BaseModel):
    station_no: int = Field(ge=1, description="工位编号（赛事内唯一）")
    available: bool = Field(default=True, description="是否可用")
    tags: Tags = Field(default_factory=list, description="工位能力标签")


class StationsBulkCreate(BaseModel):
    stations: list[StationCreate] = Field(
        min_length=1, description="工位列表，全部成功才落库"
    )


class StationUpdate(BaseModel):
    available: bool = Field(description="是否可用")
    tags: Tags = Field(description="工位能力标签（整体替换）")


class StationResponse(BaseModel):
    id: int
    station_no: int
    available: bool
    tags: list[str]

    model_config = {"from_attributes": True}


# ---------- StationPlan ----------

class StationPin(BaseModel):
    """固定工位：指定某场非轮空对局必须使用的工位。"""

    match_id: int = Field(description="本轮非轮空对局 id")
    station_no: int = Field(description="工位编号")


class StationPlanCreate(BaseModel):
    expected_version: int = Field(
        ge=0,
        description="期望的当前最新版本号（尚无方案时为 0）；不一致返回 409",
    )
    pins: list[StationPin] = Field(
        default_factory=list, description="固定工位（必须全部满足）"
    )


class StationAssignmentResponse(BaseModel):
    match_id: int
    station_no: int


class UnassignedMatchResponse(BaseModel):
    match_id: int
    # 双方选手必需标签的并集
    required_tags: list[str]
    # 所有可用工位都不提供的标签
    missing_tags: list[str]
    # 能覆盖全部必需标签、但本方案中已被占用的工位编号
    conflicting_stations: list[int]
    # 人类可读的原因说明
    reason: str


class StationPlanResponse(BaseModel):
    id: int
    round_id: int
    round_no: int
    version: int
    assignments: list[StationAssignmentResponse]
    unassigned: list[UnassignedMatchResponse]
    # UTC 发布时间，ISO 8601（Z 结尾）
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
