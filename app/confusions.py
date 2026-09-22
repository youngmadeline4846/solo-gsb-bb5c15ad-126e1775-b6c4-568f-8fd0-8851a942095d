"""参考字混淆审计：对库内不同字符的参考轨迹做两两无序匹配。

复用候选字识别引擎（recognition.recognize_against）：每对字符各跑一次
以自己为归一化基准的全局一对一分配。相似度对 A→B 与 B→A 两个方向都
计算并取较低值，避免缺笔字在单向匹配下被高估（短字当“用户轨迹”时，
长字多出的笔只按笔数均摊扣分，单方向分数可能虚高）。
"""
from __future__ import annotations

from typing import Optional

from .recognition import recognize_against
from .validation import Tolerances


def compare_characters(
    char_a: str,
    strokes_a: list[list[tuple[float, float]]],
    char_b: str,
    strokes_b: list[list[tuple[float, float]]],
    *,
    tol: Optional[Tolerances] = None,
) -> dict:
    """比对一对参考字符，返回含双向分数与双向缺/多笔惩罚的明细。

    A→B：以 A 为归一化基准，A 作参考、B 作用户轨迹；此时 B 多出的笔
    计为多笔、A 中未被对上的笔计为缺笔。B→A 同理、方向相反。
    聚合相似度取两个方向分数的较低值。
    """
    ab = recognize_against(
        char_b,
        reference_strokes=strokes_a,
        user_strokes=strokes_b,
        tol=tol,
    )
    ba = recognize_against(
        char_a,
        reference_strokes=strokes_b,
        user_strokes=strokes_a,
        tol=tol,
    )

    def _direction(result: dict, from_char: str, to_char: str) -> dict:
        penalties = result["penalties"]
        return {
            "direction": f"{from_char}→{to_char}",
            "score": result["score"],
            "stroke_count": result["user_stroke_count"],
            "matched_stroke_count": result["matched_stroke_count"],
            "missing_count": penalties["missing_count"],
            "extra_count": penalties["extra_count"],
            "missing_stroke_indices": penalties["missing_stroke_indices"],
            "extra_stroke_indices": penalties["extra_stroke_indices"],
            "matched_cost_total": penalties["matched_cost_total"],
            "missing_penalty_total": penalties["missing_penalty_total"],
            "extra_penalty_total": penalties["extra_penalty_total"],
            "total_cost": penalties["total_cost"],
        }

    forward = _direction(ab, char_a, char_b)
    backward = _direction(ba, char_b, char_a)
    return {
        "characters": [char_a, char_b],
        "score": round(min(ab["score"], ba["score"]), 2),
        "forward": forward,
        "backward": backward,
    }


def audit_confusions(
    references: list[dict],
    *,
    threshold: float,
    limit: int,
    tol: Optional[Tolerances] = None,
) -> dict:
    """对全部参考字两两无序匹配，返回达到阈值的混淆对。

    references 为 db.list_references() 风格记录（含 character/strokes）。
    字符对按字典序规范化（小者在前），结果按聚合相似度降序、字符对
    字典序升序稳定排列，limit 在排序之后截断。
    """
    records = sorted(references, key=lambda r: r["character"])
    total_references = len(records)
    total_comparisons = total_references * (total_references - 1) // 2

    pairs: list[dict] = []
    for i in range(total_references):
        for j in range(i + 1, total_references):
            ra, rb = records[i], records[j]
            pair = compare_characters(
                ra["character"],
                [[tuple(p) for p in s] for s in ra["strokes"]],
                rb["character"],
                [[tuple(p) for p in s] for s in rb["strokes"]],
                tol=tol,
            )
            if pair["score"] >= threshold:
                pairs.append(pair)

    pairs.sort(key=lambda p: (-p["score"], p["characters"][0], p["characters"][1]))
    hits = len(pairs)
    return {
        "threshold": threshold,
        "limit": limit,
        "total_references": total_references,
        "total_comparisons": total_comparisons,
        "total_hits": hits,
        "pairs": pairs[:limit],
    }
