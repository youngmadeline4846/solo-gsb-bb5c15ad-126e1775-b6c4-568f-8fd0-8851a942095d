"""笔顺匹配引擎：方向、起止点、形状的归一化比对。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import geometry as geo
from .config import settings


@dataclass(frozen=True)
class Tolerances:
    """单次校验生效的容差（请求覆盖 + 服务端默认）。"""

    start_end: float = settings.start_end_tolerance
    direction_deg: float = settings.direction_tolerance_deg
    shape: float = settings.shape_tolerance
    fork_extra_direction_deg: float = settings.fork_extra_direction_deg
    fork_extra_shape: float = settings.fork_extra_shape
    corner_angle_deg: float = settings.corner_angle_deg

    @classmethod
    def from_request(cls, req_tol) -> "Tolerances":
        """用请求中的 Tolerances 模型逐字段覆盖默认值。"""
        base = cls()
        if req_tol is None:
            return base
        overrides = {
            k: v for k, v in req_tol.model_dump(exclude_none=True).items()
        }
        return cls(**{**base.__dict__, **overrides})


@dataclass
class StrokeVerdict:
    index: int
    passed: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    metrics: dict | None = None


@dataclass
class MatchResult:
    passed: bool
    score: float
    first_failed_stroke: int | None
    deviation: dict | None
    strokes: list[StrokeVerdict]


# ---------------------------------------------------------------- 单笔比对

def _compare_stroke(
    ref: geo.Polyline,
    usr: geo.Polyline,
    tol: Tolerances,
    resample_n: int,
) -> tuple[bool, list[str], dict, float]:
    """比对单条参考笔画与用户笔画（均已归一化）。

    返回 (passed, reasons, metrics, score)。
    """
    ref_rs = geo.resample(ref, resample_n)
    usr_rs = geo.resample(usr, resample_n)

    start_dev = geo._dist(usr_rs[0], ref_rs[0])
    end_dev = geo._dist(usr_rs[-1], ref_rs[-1])
    shape_dev = geo.mean_pointwise_deviation(ref_rs, usr_rs)

    corners = geo.detect_corners(ref_rs, tol.corner_angle_deg)
    is_forked = len(corners) > 0

    # 分叉/转折笔画获得可配置的额外容差
    dir_tol = tol.direction_deg + (tol.fork_extra_direction_deg if is_forked else 0.0)
    shape_tol = tol.shape + (tol.fork_extra_shape if is_forked else 0.0)

    # 方向偏差：分叉笔画按拐点分段，取各分段方向偏差的最大值；
    # 用户笔画按参考笔画拐点的弧长比例切分。
    dir_dev = 0.0
    if is_forked:
        ref_angles = geo.segment_angles(ref_rs, corners)
        # 参考拐点在弧长上的比例 → 映射到用户重采样折线
        frac = [c / (resample_n - 1) for c in corners]
        usr_corners = [round(f * (resample_n - 1)) for f in frac]
        usr_angles = geo.segment_angles(usr_rs, usr_corners)
        for ra, ua in zip(ref_angles, usr_angles):
            if ra is None or ua is None:
                continue
            dir_dev = max(dir_dev, geo.angle_diff(ra, ua))
    else:
        ra = geo.displacement_angle(ref_rs)
        ua = geo.displacement_angle(usr_rs)
        if ra is not None and ua is not None:
            dir_dev = geo.angle_diff(ra, ua)

    metrics = {
        "start_deviation": round(start_dev, 4),
        "end_deviation": round(end_dev, 4),
        "direction_deviation_deg": round(dir_dev, 2),
        "shape_deviation": round(shape_dev, 4),
        "is_forked": is_forked,
    }

    reasons: list[str] = []
    if start_dev > tol.start_end:
        reasons.append("START_POINT_DEVIATION")
    if end_dev > tol.start_end:
        reasons.append("END_POINT_DEVIATION")
    if dir_dev > dir_tol:
        reasons.append("DIRECTION_DEVIATION")
    if shape_dev > shape_tol:
        reasons.append("SHAPE_DEVIATION")

    # 得分：各指标在 2 倍容差处线性衰减到 0
    def _metric_score(value: float, limit: float) -> float:
        limit = max(limit, 1e-6)
        return max(0.0, 1.0 - value / (2.0 * limit))

    score = 100.0 * (
        0.20 * _metric_score(start_dev, tol.start_end)
        + 0.20 * _metric_score(end_dev, tol.start_end)
        + 0.30 * _metric_score(dir_dev, dir_tol)
        + 0.30 * _metric_score(shape_dev, shape_tol)
    )
    return (not reasons), reasons, metrics, round(score, 1)


def _matches_reference(
    usr: geo.Polyline, ref: geo.Polyline, tol: Tolerances, resample_n: int
) -> bool:
    """判断用户笔画是否落在某条参考笔画的容差内（用于错序检测）。"""
    ok, _, _, _ = _compare_stroke(ref, usr, tol, resample_n)
    return ok


def compare_stroke(
    reference: geo.Polyline, user: geo.Polyline, tol: Tolerances, resample_n: int
) -> tuple[bool, list[str], dict, float]:
    """单笔比对的公共入口（归一化坐标输入），供识别引擎构建代价矩阵复用。

    与内部 _compare_stroke 相同：返回 (passed, reasons, metrics, score)，
    参数顺序与 validate() 中 (reference, user) 保持一致。
    """
    return _compare_stroke(reference, user, tol, resample_n)


# ---------------------------------------------------------------- 整体匹配

_REASON_MESSAGES = {
    "STROKE_COUNT_MISMATCH": "笔画数量不符：期望 {expected} 笔，实际 {actual} 笔",
    "WRONG_ORDER": "笔顺错误：第 {index} 笔与参考第 {matched} 笔形状相符，但书写顺序不对",
    "START_POINT_DEVIATION": "第 {index} 笔起点偏差过大（{value:.3f}，上限 {limit:.3f}）",
    "END_POINT_DEVIATION": "第 {index} 笔终点偏差过大（{value:.3f}，上限 {limit:.3f}）",
    "DIRECTION_DEVIATION": "第 {index} 笔方向偏差过大（{value:.1f}°，上限 {limit:.1f}°）",
    "SHAPE_DEVIATION": "第 {index} 笔形状偏差过大（{value:.3f}，上限 {limit:.3f}）",
}


def validate(
    reference_strokes: list[list[tuple[float, float]]],
    user_strokes: list[list[tuple[float, float]]],
    tol: Tolerances,
    canvas_size: tuple[float, float] | None = None,
    resample_n: int | None = None,
) -> MatchResult:
    """主入口：归一化后逐笔比对，返回首个不合格笔画与整体得分。"""
    n = resample_n or settings.resample_points

    origin, scale = geo.make_normalization(reference_strokes, canvas_size)
    ref_norm = geo.apply_normalization(reference_strokes, origin, scale)
    usr_norm = geo.apply_normalization(user_strokes, origin, scale)

    expected = len(ref_norm)
    actual = len(usr_norm)
    compared = min(expected, actual)

    verdicts: list[StrokeVerdict] = []
    first_failed: int | None = None
    deviation: dict | None = None

    for i in range(compared):
        ok, reasons, metrics, score = _compare_stroke(
            ref_norm[i], usr_norm[i], tol, n
        )
        # 错序检测：该笔不合格，但与另一笔参考形状相符 → 判为笔顺错误
        matched_j: int | None = None
        if not ok:
            for j in range(expected):
                if j == i:
                    continue
                if _matches_reference(usr_norm[i], ref_norm[j], tol, n):
                    matched_j = j
                    reasons = ["WRONG_ORDER"]
                    break
        verdicts.append(
            StrokeVerdict(index=i, passed=ok, reasons=reasons, metrics=metrics, score=score)
        )
        if not ok and first_failed is None:
            first_failed = i
            if matched_j is not None:
                deviation = {
                    "code": "WRONG_ORDER",
                    "stroke_index": i,
                    "message": _REASON_MESSAGES["WRONG_ORDER"].format(
                        index=i + 1, matched=matched_j + 1
                    ),
                    "metrics": metrics,
                }
            else:
                primary = reasons[0]
                value, limit = _reason_value_limit(primary, metrics, tol)
                deviation = {
                    "code": primary,
                    "stroke_index": i,
                    "message": _REASON_MESSAGES[primary].format(
                        index=i + 1, value=value, limit=limit
                    ),
                    "metrics": metrics,
                }

    # 笔画数量不符：所有可比笔画合格时也需报告
    count_ok = expected == actual
    if not count_ok and deviation is None:
        first_failed = compared if actual < expected else expected
        deviation = {
            "code": "STROKE_COUNT_MISMATCH",
            "stroke_index": first_failed,
            "message": _REASON_MESSAGES["STROKE_COUNT_MISMATCH"].format(
                expected=expected, actual=actual
            ),
            "metrics": None,
        }

    # 整体得分：单笔均分 × 覆盖率（缺笔/多笔都扣分）
    if verdicts:
        mean_score = sum(v.score for v in verdicts) / len(verdicts)
    else:
        mean_score = 0.0
    coverage = compared / max(expected, actual) if max(expected, actual) else 0.0
    overall = round(mean_score * coverage, 1)

    return MatchResult(
        passed=(first_failed is None and count_ok),
        score=overall,
        first_failed_stroke=first_failed,
        deviation=deviation,
        strokes=verdicts,
    )


def _reason_value_limit(
    reason: str, metrics: dict, tol: Tolerances
) -> tuple[float, float]:
    is_forked = metrics["is_forked"]
    if reason == "START_POINT_DEVIATION":
        return metrics["start_deviation"], tol.start_end
    if reason == "END_POINT_DEVIATION":
        return metrics["end_deviation"], tol.start_end
    if reason == "DIRECTION_DEVIATION":
        limit = tol.direction_deg + (tol.fork_extra_direction_deg if is_forked else 0.0)
        return metrics["direction_deviation_deg"], limit
    # SHAPE_DEVIATION
    limit = tol.shape + (tol.fork_extra_shape if is_forked else 0.0)
    return metrics["shape_deviation"], limit
