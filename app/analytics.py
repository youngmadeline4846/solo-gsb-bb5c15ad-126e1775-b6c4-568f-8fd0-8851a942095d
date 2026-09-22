"""书写练习质量趋势统计：通过率、分位数与 UTC 日期分桶。

统计输入为 db.Database.snapshot_submissions() 在单个加锁读取中返回的
提交快照（含 ValidateResponse 负载），本模块只做纯内存聚合，不触碰数据库。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence


def to_naive_utc(dt: datetime) -> datetime:
    """带时区的时间换算为 naive UTC；naive 时间视为 UTC 原样返回。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_created_at(value: str) -> datetime:
    """解析落库时间字符串，返回 naive UTC（落库格式统一为秒级 ISO-8601）。

    不带时区偏移的历史值按 UTC 处理，保证日期分桶始终基于 UTC。
    """
    return to_naive_utc(datetime.fromisoformat(value))


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """分位数：显式线性插值（与 numpy 默认 linear / NIST R-7 同一约定）。

    对升序序列 x_0 ≤ ... ≤ x_{n-1}，位置 h = q·(n−1)，取

        x_floor(h) + (h − floor(h)) · (x_ceil(h) − x_floor(h))

    因此 q=0/1 恰为最小/最大值，n=1 时退化为该值。调用前需保证序列非空且升序。
    """
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = int(pos)  # pos ∈ [0, n-1]，int() 即向零取整 = floor
    hi = lo + 1 if pos > lo else lo
    frac = pos - lo
    return float(sorted_values[lo]) + frac * (
        float(sorted_values[hi]) - float(sorted_values[lo])
    )


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """对一组提交计算共享指标：数量、通过数、通过率、均分、P50、P90。"""
    n = len(records)
    if n == 0:
        return {
            "submission_count": 0,
            "passed_count": 0,
            "pass_rate": 0.0,
            "average_score": 0.0,
            "p50_score": 0.0,
            "p90_score": 0.0,
        }
    scores = sorted(float(r["response"]["score"]) for r in records)
    passed_count = sum(1 for r in records if r["response"].get("passed"))
    return {
        "submission_count": n,
        "passed_count": passed_count,
        "pass_rate": round(passed_count / n, 4),
        "average_score": round(sum(scores) / n, 2),
        "p50_score": round(percentile(scores, 0.5), 2),
        "p90_score": round(percentile(scores, 0.9), 2),
    }


def build_practice_analytics(
    records: list[dict[str, Any]],
    *,
    character: str,
    start: datetime,
    end: datetime,
    passed: Optional[bool],
    generated_at: str,
) -> dict[str, Any]:
    """基于一致性快照构建练习质量趋势响应字典。

    - 总体指标见 _summarize；
    - first_failed_strokes：未通过提交的首个失败笔画下标分布，按下标升序；
    - daily_trend：按 UTC 日期分桶（仅含有提交的日期），日期升序，
      每桶复用同一套汇总指标。
    """
    summary = _summarize(records)

    fail_counts: dict[int, int] = {}
    for r in records:
        idx = r["response"].get("first_failed_stroke")
        if idx is not None:
            fail_counts[idx] = fail_counts.get(idx, 0) + 1
    first_failed_strokes = [
        {"stroke_index": idx, "count": fail_counts[idx]}
        for idx in sorted(fail_counts)
    ]

    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        day = parse_created_at(r["created_at"]).date().isoformat()
        by_day.setdefault(day, []).append(r)
    daily_trend = []
    for day in sorted(by_day):
        bucket = _summarize(by_day[day])
        daily_trend.append({"date": day, **bucket})

    return {
        "filter": {
            "character": character,
            "start": start,
            "end": end,
            "passed": passed,
        },
        "generated_at": generated_at,
        **summary,
        "first_failed_strokes": first_failed_strokes,
        "daily_trend": daily_trend,
    }
