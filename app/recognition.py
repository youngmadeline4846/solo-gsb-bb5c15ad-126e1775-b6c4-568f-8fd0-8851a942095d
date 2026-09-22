"""候选字识别引擎：未标注笔画 → 已注册参考字的 top-k 匹配。

与 validation 的“按目标字符逐笔核对笔顺”不同，识别场景下：

- 输入不携带目标字符，需在全部已注册参考字中择优；
- 不假设笔顺，用户笔与参考笔之间通过代价矩阵做全局一对一最优分配
  （匈牙利算法），允许书写次序与参考不同；
- 笔数可能不等：用虚拟槽把缺笔（参考笔无对应用户笔）与多笔
  （用户笔无对应参考笔）纳入同一个分配问题，并按固定代价显式扣分。

单笔代价复用 validation._compare_stroke 的四项指标（起点/终点/方向/
形状，含分叉笔画的额外容差），取 1 - 单笔得分/100，落在 [0,1]。
"""
from __future__ import annotations

from typing import Optional

from . import geometry as geo
from .config import settings
from .validation import Tolerances, compare_stroke


# ------------------------------------------------------------------ 分配

def _hungarian(cost: list[list[float]]) -> tuple[list[int], float]:
    """方阵最小权完美匹配（经典 O(n^3) 匈牙利算法）。

    1 下标实现，行（用户笔）与列（参考笔）均按固定下标顺序增广；
    等代价时先出现的下标优先，保证相同输入下结果确定。
    返回 (row -> col 的匹配列, 总代价)。
    """
    n = len(cost)
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)   # p[j]：与列 j 匹配的行
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        # 沿增广路径翻转匹配
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    assignment = [-1] * n
    total = 0.0
    for j in range(1, n + 1):
        i = p[j]
        if i != 0:
            assignment[i - 1] = j - 1
            total += cost[i - 1][j - 1]
    return assignment, total


# ------------------------------------------------------------------ 单字匹配

def _pair_cost_metrics(
    ref: geo.Polyline, usr: geo.Polyline, tol: Tolerances, resample_n: int
) -> tuple[float, dict]:
    """单笔用户笔 vs 单笔参考笔：返回 (cost∈[0,1], metrics)。"""
    _ok, _reasons, metrics, score = compare_stroke(ref, usr, tol, resample_n)
    return 1.0 - score / 100.0, metrics


def recognize_against(
    character: str,
    reference_strokes: list[list[tuple[float, float]]],
    user_strokes: list[list[tuple[float, float]]],
    *,
    tol: Optional[Tolerances] = None,
    canvas_size: Optional[tuple[float, float]] = None,
    resample_n: Optional[int] = None,
    missing_penalty: Optional[float] = None,
    extra_penalty: Optional[float] = None,
) -> dict:
    """把一份用户轨迹与单个参考字比对，返回候选结果字典。

    代价矩阵边长 max(用户笔数, 参考笔数)：真实笔×真实笔为单笔匹配代价；
    虚拟列（仅当用户笔更多）每条边取多笔惩罚，虚拟行（仅当参考笔更多）
    每条边或缺笔惩罚。真实列下标先于虚拟列、真实行先于虚拟行，配合
    匈牙利算法的固定增广顺序，等代价时真实配对优先且结果确定。
    """
    tol = tol or Tolerances()
    n = resample_n or settings.resample_points
    miss_pen = (
        settings.recognize_missing_penalty
        if missing_penalty is None
        else missing_penalty
    )
    extra_pen = (
        settings.recognize_extra_penalty if extra_penalty is None else extra_penalty
    )

    origin, scale = geo.make_normalization(reference_strokes, canvas_size)
    ref_norm = geo.apply_normalization(reference_strokes, origin, scale)
    usr_norm = geo.apply_normalization(user_strokes, origin, scale)

    n_usr = len(usr_norm)
    n_ref = len(ref_norm)
    size = max(n_usr, n_ref, 1)

    pair_cache: dict[tuple[int, int], tuple[float, dict]] = {}

    def pair(i: int, j: int) -> tuple[float, dict]:
        key = (i, j)
        if key not in pair_cache:
            pair_cache[key] = _pair_cost_metrics(
                ref_norm[j], usr_norm[i], tol, n
            )
        return pair_cache[key]

    # 行 0..n_usr-1 为用户笔（其后为缺笔虚拟行），
    # 列 0..n_ref-1 为参考笔（其后为多笔虚拟列）。
    matrix = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if i < n_usr and j < n_ref:
                matrix[i][j] = pair(i, j)[0]
            elif i < n_usr:
                matrix[i][j] = extra_pen
            else:
                matrix[i][j] = miss_pen

    assignment, _total_cost = _hungarian(matrix)

    correspondences: list[dict] = []
    matched_ref: set[int] = set()
    matched_cost_total = 0.0
    for i in range(n_usr):
        j = assignment[i]
        if j < n_ref:
            pair_cost, metrics = pair(i, j)
            matched_cost_total += pair_cost
            matched_ref.add(j)
            correspondences.append(
                {
                    "user_stroke_index": i,
                    "reference_stroke_index": j,
                    "cost": round(pair_cost, 4),
                    "metrics": metrics,
                }
            )
        else:
            correspondences.append(
                {
                    "user_stroke_index": i,
                    "reference_stroke_index": None,
                    "cost": round(extra_pen, 4),
                    "metrics": None,
                }
            )
    # 报告按参考笔次序排列（多笔项置尾，再按用户笔下标）
    correspondences.sort(
        key=lambda c: (
            c["reference_stroke_index"] is None,
            c["reference_stroke_index"] if c["reference_stroke_index"] is not None else 0,
            c["user_stroke_index"],
        )
    )

    missing_indices = sorted(j for j in range(n_ref) if j not in matched_ref)
    extra_indices = sorted(
        c["user_stroke_index"]
        for c in correspondences
        if c["reference_stroke_index"] is None
    )

    missing_penalty_total = round(miss_pen * len(missing_indices), 4)
    extra_penalty_total = round(extra_pen * len(extra_indices), 4)
    matched_cost_total = round(matched_cost_total, 4)
    total_cost = round(
        matched_cost_total + missing_penalty_total + extra_penalty_total, 4
    )

    # 置信度：总代价（单笔代价 + 缺/多笔惩罚）相对笔画数归一
    denom = max(n_usr, n_ref, 1)
    score = max(0.0, 1.0 - total_cost / denom) * 100.0

    return {
        "character": character,
        "score": round(score, 2),
        "reference_stroke_count": n_ref,
        "user_stroke_count": n_usr,
        "matched_stroke_count": len(matched_ref),
        "correspondences": correspondences,
        "penalties": {
            "missing_count": len(missing_indices),
            "extra_count": len(extra_indices),
            "missing_stroke_indices": missing_indices,
            "extra_stroke_indices": extra_indices,
            "missing_penalty_per_stroke": round(miss_pen, 4),
            "extra_penalty_per_stroke": round(extra_pen, 4),
            "matched_cost_total": matched_cost_total,
            "missing_penalty_total": missing_penalty_total,
            "extra_penalty_total": extra_penalty_total,
            "total_cost": total_cost,
        },
    }
