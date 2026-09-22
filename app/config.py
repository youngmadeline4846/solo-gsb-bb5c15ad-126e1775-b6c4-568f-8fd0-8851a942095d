"""应用配置：环境变量覆盖默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """服务端默认容差与运行参数。

    请求体中的 tolerances 字段可逐次覆盖这些默认值。
    """

    database_path: str = field(
        default_factory=lambda: os.getenv("DATABASE_PATH", "/data/app.db")
    )
    # 归一化后（0..1 坐标系）起止点允许偏差
    start_end_tolerance: float = field(
        default_factory=lambda: _float_env("TOL_START_END", 0.15)
    )
    # 笔画整体方向允许偏差（度）
    direction_tolerance_deg: float = field(
        default_factory=lambda: _float_env("TOL_DIRECTION_DEG", 35.0)
    )
    # 重采样后形状平均偏差
    shape_tolerance: float = field(
        default_factory=lambda: _float_env("TOL_SHAPE", 0.18)
    )
    # 分叉/转折笔画：在拐角相邻分段上额外放宽的方向角（度）
    fork_extra_direction_deg: float = field(
        default_factory=lambda: _float_env("TOL_FORK_EXTRA_DEG", 20.0)
    )
    # 分叉/转折笔画：额外放宽的形状偏差
    fork_extra_shape: float = field(
        default_factory=lambda: _float_env("TOL_FORK_EXTRA_SHAPE", 0.08)
    )
    # 判定拐点的最小转角（度）
    corner_angle_deg: float = field(
        default_factory=lambda: _float_env("CORNER_ANGLE_DEG", 50.0)
    )
    # 每笔重采样点数
    resample_points: int = field(
        default_factory=lambda: int(_float_env("RESAMPLE_POINTS", 32))
    )
    # 候选字识别：漏写一笔（参考笔无人对应）的显式惩罚。
    # 单笔匹配代价 ∈ [0,1]，1.0 等价于“这一笔完全写错”。
    recognize_missing_penalty: float = field(
        default_factory=lambda: _float_env("RECOG_MISSING_PENALTY", 1.0)
    )
    # 候选字识别：多写一笔（用户笔无参考对应）的显式惩罚
    recognize_extra_penalty: float = field(
        default_factory=lambda: _float_env("RECOG_EXTRA_PENALTY", 1.0)
    )


settings = Settings()
