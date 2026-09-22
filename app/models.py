"""请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 平面坐标点 [x, y]，坐标系以画布左上角为原点、y 轴向下
Point = Annotated[list[float], Field(min_length=2, max_length=2)]
# 单笔画：至少 2 个采样点的折线
Stroke = Annotated[list[Point], Field(min_length=2)]


class Tolerances(BaseModel):
    """匹配容差。所有字段缺省时回落到服务端默认值（见 GET /v1/config）。

    距离类阈值基于归一化坐标（参考字外接正方形映射到 0..1）。
    """

    start_end: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="起止点允许偏差（归一化距离）",
    )
    direction_deg: Optional[float] = Field(
        default=None, ge=0.0, le=180.0,
        description="笔画方向允许偏差（度）",
    )
    shape: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="形状平均偏差上限",
    )
    fork_extra_direction_deg: Optional[float] = Field(
        default=None, ge=0.0, le=180.0,
        description="分叉/转折笔画在拐角分段上额外放宽的方向角（度）",
    )
    fork_extra_shape: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="分叉/转折笔画额外放宽的形状偏差",
    )
    corner_angle_deg: Optional[float] = Field(
        default=None, ge=10.0, le=170.0,
        description="判定分叉拐点的最小转角（度）",
    )


class ValidateRequest(BaseModel):
    """笔顺校验请求。同一 submission_id 重复提交返回首个结果（幂等）。"""

    submission_id: str = Field(
        min_length=1, max_length=128,
        description="客户端生成的幂等键；相同键+相同负载返回首个结果",
    )
    character: str = Field(min_length=1, max_length=1, description="目标汉字")
    reference_strokes: Optional[list[Stroke]] = Field(
        default=None, description="内联参考笔画序列（与 reference_id 二选一）",
    )
    reference_id: Optional[str] = Field(
        default=None, description="已注册的参考字 ID（即字符本身）",
    )
    strokes: list[Stroke] = Field(description="用户书写轨迹，按书写顺序")
    canvas_size: Optional[Point] = Field(
        default=None,
        description="画布宽高 [w, h]；缺省时以参考字外接框归一化",
    )
    tolerances: Tolerances = Field(default_factory=Tolerances)

    @model_validator(mode="after")
    def _check_reference(self) -> "ValidateRequest":
        if self.reference_strokes is None and self.reference_id is None:
            raise ValueError("必须提供 reference_strokes 或 reference_id 之一")
        if self.reference_strokes is not None and self.reference_id is not None:
            raise ValueError("reference_strokes 与 reference_id 只能提供一个")
        if self.reference_strokes is not None and not self.reference_strokes:
            raise ValueError("reference_strokes 不能为空列表")
        return self


class RegisterReferenceRequest(BaseModel):
    """注册/更新某汉字的参考笔画序列。"""

    character: str = Field(min_length=1, max_length=1)
    strokes: list[Stroke] = Field(min_length=1)
    source: Optional[str] = Field(default=None, description="来源标注，如字体/字帖")


class StrokeMetrics(BaseModel):
    start_deviation: float
    end_deviation: float
    direction_deviation_deg: float
    shape_deviation: float
    is_forked: bool = Field(description="参考笔画是否含分叉/转折拐点")


class StrokeReport(BaseModel):
    index: int
    passed: bool
    score: float = Field(ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)
    metrics: Optional[StrokeMetrics] = None


DeviationCode = Literal[
    "STROKE_COUNT_MISMATCH",
    "WRONG_ORDER",
    "START_POINT_DEVIATION",
    "END_POINT_DEVIATION",
    "DIRECTION_DEVIATION",
    "SHAPE_DEVIATION",
]


class Deviation(BaseModel):
    code: DeviationCode
    stroke_index: Optional[int] = None
    message: str
    metrics: Optional[StrokeMetrics] = None


class ValidateResponse(BaseModel):
    submission_id: str
    character: str
    passed: bool
    score: float = Field(ge=0.0, le=100.0, description="整体得分 0-100")
    expected_stroke_count: int
    actual_stroke_count: int
    first_failed_stroke: Optional[int] = Field(
        default=None, description="首个不合格笔画的下标（0 起），全部合格为 null"
    )
    deviation: Optional[Deviation] = Field(
        default=None, description="首个不合格笔画的偏差原因"
    )
    strokes: list[StrokeReport]
    idempotent_replay: bool = Field(
        description="true 表示本次为幂等重放，返回的是首个提交的结果"
    )
    created_at: str


class ReferenceResponse(BaseModel):
    character: str
    stroke_count: int
    strokes: list[Stroke]
    source: Optional[str] = None
    updated_at: str


# ------------------------------------------------------------ 候选字识别

class RecognizeRequest(BaseModel):
    """候选字识别请求：在全部已注册参考字中为未标注轨迹寻找候选。"""

    strokes: list[Stroke] = Field(
        min_length=1, description="用户书写轨迹（不要求按参考笔顺）",
    )
    top_k: int = Field(
        ge=1, le=100, description="返回候选数量上限，按置信度降序",
    )
    canvas_size: Optional[Point] = Field(
        default=None,
        description="画布宽高 [w, h]；缺省时以各参考字外接框分别归一化",
    )


class StrokeCorrespondence(BaseModel):
    user_stroke_index: int = Field(description="用户笔画下标（0 起，按书写次序）")
    reference_stroke_index: Optional[int] = Field(
        description="分配到的参考笔画下标（0 起）；null 表示多笔（无对应参考笔）",
    )
    cost: float = Field(ge=0.0, le=1.0, description="该配对的归一化代价 0-1")
    metrics: Optional[StrokeMetrics] = None


class CandidatePenalties(BaseModel):
    missing_count: int = Field(description="缺笔数：参考笔中无对应用户笔的数量")
    extra_count: int = Field(description="多笔数：用户笔中无对应参考笔的数量")
    missing_stroke_indices: list[int] = Field(description="未匹配的参考笔画下标")
    extra_stroke_indices: list[int] = Field(description="未匹配的用户笔画下标")
    missing_penalty_per_stroke: float
    extra_penalty_per_stroke: float
    matched_cost_total: float = Field(description="已配对笔画的代价之和")
    missing_penalty_total: float = Field(description="缺笔惩罚合计")
    extra_penalty_total: float = Field(description="多笔惩罚合计")
    total_cost: float = Field(
        description="总代价 = 已配对代价 + 缺笔惩罚 + 多笔惩罚",
    )


class RecognitionCandidate(BaseModel):
    character: str
    score: float = Field(
        ge=0.0, le=100.0, description="置信度得分 0-100，越高越相似",
    )
    reference_stroke_count: int
    user_stroke_count: int
    matched_stroke_count: int
    correspondences: list[StrokeCorrespondence] = Field(
        description="最优一对一分配下的笔画对应关系",
    )
    penalties: CandidatePenalties


class RecognizeResponse(BaseModel):
    top_k: int
    total_references: int = Field(description="参与比对的已注册参考字数量")
    candidates: list[RecognitionCandidate] = Field(
        description="按置信度降序的候选；同分按字符稳定排序",
    )


class SubmissionRecord(BaseModel):
    submission_id: str
    request_hash: str
    result: ValidateResponse
    created_at: str


# ------------------------------------------------------------ 练习质量趋势

class PracticeAnalyticsFilter(BaseModel):
    """本次统计实际生效的筛选条件（回显）。"""

    character: str = Field(description="目标汉字")
    start: datetime = Field(description="时间闭区间起点（UTC）")
    end: datetime = Field(description="时间闭区间终点（UTC）")
    passed: Optional[bool] = Field(
        default=None, description="通过状态筛选；null 表示未筛选"
    )


class FirstFailedStrokeCount(BaseModel):
    stroke_index: int = Field(description="首个失败笔画的下标（0 起）")
    count: int = Field(ge=0, description="该下标作为首个失败笔画的提交数")


class DailyTrendBucket(BaseModel):
    """单个 UTC 日期桶的汇总指标（仅包含有提交的日期）。"""

    date: str = Field(description="UTC 日期，YYYY-MM-DD")
    submission_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    average_score: float = Field(ge=0.0, le=100.0)
    p50_score: float = Field(ge=0.0, le=100.0)
    p90_score: float = Field(ge=0.0, le=100.0)


class PracticeAnalyticsResponse(BaseModel):
    """书写练习质量趋势：基于单次一致性快照的汇总结果。

    分数分位数（p50_score/p90_score）采用显式线性插值：
    升序序列 x_0..x_{n-1}，位置 h = q·(n−1)，取
    x_floor(h) + (h − floor(h))·(x_ceil(h) − x_floor(h))。
    无匹配提交时各数值为 0，first_failed_strokes 与 daily_trend 为空数组。
    """

    filter: PracticeAnalyticsFilter
    generated_at: str = Field(description="响应生成时间（UTC，ISO-8601）")
    submission_count: int = Field(ge=0, description="匹配筛选条件的提交总数")
    passed_count: int = Field(ge=0, description="其中通过的提交数")
    pass_rate: float = Field(ge=0.0, le=1.0, description="通过率 passed/total")
    average_score: float = Field(ge=0.0, le=100.0, description="平均分")
    p50_score: float = Field(ge=0.0, le=100.0, description="分数 P50（线性插值）")
    p90_score: float = Field(ge=0.0, le=100.0, description="分数 P90（线性插值）")
    first_failed_strokes: list[FirstFailedStrokeCount] = Field(
        description="未通过提交的首个失败笔画下标分布，按下标升序",
    )
    daily_trend: list[DailyTrendBucket] = Field(
        description="按 UTC 日期分桶的趋势，日期升序，仅含有提交的日期",
    )
