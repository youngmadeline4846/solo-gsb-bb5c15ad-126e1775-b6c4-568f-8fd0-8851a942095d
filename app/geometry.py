"""几何工具：归一化、重采样、方向角、拐点检测。

坐标约定：输入为画布坐标（x 向右、y 向下）。归一化把参考字的外接框
（扩成正方形并加少量边距）仿射映射到 0..1 区间，用户轨迹套用同一变换，
从而保留用户书写在位置/尺寸上的绝对偏差。
"""
from __future__ import annotations

import math
from typing import Sequence

Point2 = tuple[float, float]
Polyline = list[Point2]

_PADDING = 0.05  # 外接框四周各留 5% 边距


# ---------------------------------------------------------------- 基础运算

def _dist(a: Point2, b: Point2) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(points: Sequence[Point2]) -> float:
    return sum(_dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def resample(points: Sequence[Point2], n: int) -> Polyline:
    """把折线按弧长等距重采样为 n 个点（含首尾）。"""
    if len(points) < 2:
        raise ValueError("折线至少需要 2 个点")
    if n < 2:
        raise ValueError("重采样点数至少为 2")
    total = polyline_length(points)
    if total == 0.0:
        return [tuple(points[0])] * n  # 退化为一个点
    step = total / (n - 1)
    out: Polyline = [tuple(points[0])]
    prev = tuple(points[0])
    acc = 0.0
    i = 1
    while i < len(points):
        cur = tuple(points[i])
        seg = _dist(prev, cur)
        if acc + seg >= step and seg > 0.0:
            t = (step - acc) / seg
            nx = prev[0] + t * (cur[0] - prev[0])
            ny = prev[1] + t * (cur[1] - prev[1])
            out.append((nx, ny))
            prev = (nx, ny)
            acc = 0.0
            # 不推进 i：当前线段剩余部分继续参与采样
        else:
            acc += seg
            prev = cur
            i += 1
    while len(out) < n:
        out.append(tuple(points[-1]))
    return out[:n]


# ---------------------------------------------------------------- 归一化

def bounding_box(strokes: Sequence[Sequence[Point2]]) -> tuple[float, float, float, float]:
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    return min(xs), min(ys), max(xs), max(ys)


def make_normalization(
    reference: Sequence[Sequence[Point2]],
    canvas_size: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], float]:
    """返回 (origin, scale)：normalized = (p - origin) / scale。

    - 提供 canvas_size 时：以画布中心为基准，scale 取宽高中较大者；
    - 否则以参考字外接框（扩成正方形 + 边距）为基准。
    """
    if canvas_size is not None:
        w, h = canvas_size
        scale = max(w, h)
        if scale <= 0:
            raise ValueError("canvas_size 必须为正数")
        return (0.0, 0.0), scale

    min_x, min_y, max_x, max_y = bounding_box(reference)
    w = max_x - min_x
    h = max_y - min_y
    side = max(w, h)
    if side == 0.0:
        side = 1.0
    # 扩成正方形（短边居中）
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    half = side / 2.0
    pad = side * _PADDING
    origin = (cx - half - pad, cy - half - pad)
    scale = side + 2.0 * pad
    return origin, scale


def apply_normalization(
    strokes: Sequence[Sequence[Point2]],
    origin: Point2,
    scale: float,
) -> list[Polyline]:
    ox, oy = origin
    return [
        [((p[0] - ox) / scale, (p[1] - oy) / scale) for p in stroke]
        for stroke in strokes
    ]


# ---------------------------------------------------------------- 方向与拐点

def displacement_angle(points: Sequence[Point2]) -> float | None:
    """起点→终点位移向量的方向角（度，0..360）。位移过短返回 None。"""
    dx = points[-1][0] - points[0][0]
    dy = points[-1][1] - points[0][1]
    if math.hypot(dx, dy) < 1e-9:
        return None
    return math.degrees(math.atan2(dy, dx)) % 360.0


def angle_diff(a: float, b: float) -> float:
    """两个方向角的最小夹角（0..180）。"""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def detect_corners(
    points: Sequence[Point2], angle_threshold_deg: float, window: int = 2
) -> list[int]:
    """检测折线上的拐点（分叉点）下标。

    比较 points[i-window]→points[i] 与 points[i]→points[i+window] 两个
    方向的转角，超过阈值即视为拐点，对应汉字中折、钩类复合笔画的转折处。
    使用窗口是因为重采样会把一个 90° 拐角摊成相邻两个 ~45° 转角，
    单点比较会漏检。
    """
    corners: list[int] = []
    n = len(points)
    for i in range(window, n - window):
        a, b, c = points[i - window], points[i], points[i + window]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        if math.hypot(*v1) < 1e-12 or math.hypot(*v2) < 1e-12:
            continue
        ang1 = math.degrees(math.atan2(v1[1], v1[0]))
        ang2 = math.degrees(math.atan2(v2[1], v2[0]))
        if angle_diff(ang1, ang2) >= angle_threshold_deg:
            corners.append(i)
    # 相邻拐点合并（保留转角最大处），避免一个拐角报出多个点
    merged: list[int] = []
    for idx in corners:
        if merged and idx - merged[-1] <= window:
            continue
        merged.append(idx)
    return merged


def segment_angles(
    points: Sequence[Point2], corners: Sequence[int]
) -> list[float | None]:
    """按拐点把折线切成分段，返回每段的方向角（退化段为 None）。"""
    cuts = [0, *corners, len(points) - 1]
    angles: list[float | None] = []
    for k in range(len(cuts) - 1):
        seg = points[cuts[k]: cuts[k + 1] + 1]
        angles.append(displacement_angle(seg) if len(seg) >= 2 else None)
    return angles


def mean_pointwise_deviation(a: Sequence[Point2], b: Sequence[Point2]) -> float:
    """两条等长重采样折线的平均点对点距离。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(_dist(a[i], b[i]) for i in range(n)) / n
