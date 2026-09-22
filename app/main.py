"""FastAPI 应用入口与路由。"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status

from . import __version__, analytics, db as db_module
from .config import settings
from .models import (
    ConfusionAuditResponse,
    ConfusionPair,
    PracticeAnalyticsResponse,
    RecognitionCandidate,
    RecognizeRequest,
    RecognizeResponse,
    ReferenceResponse,
    RegisterReferenceRequest,
    SubmissionRecord,
    Tolerances,
    ValidateRequest,
    ValidateResponse,
)
from .recognition import compare_reference_pair, recognize_against
from .validation import Tolerances as EngineTolerances
from .validation import validate


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_module.init_db(settings.database_path)
    yield
    db_module.get_db().close()


app = FastAPI(
    title="汉字笔顺轨迹校验 API",
    description=(
        "接收汉字的参考笔画序列与用户书写轨迹，按笔画方向、起止点和书写顺序"
        "做归一化匹配，支持可配置的分叉/转折笔画容差，返回首个不合格笔画、"
        "偏差原因与整体得分。同一 submission_id 重复提交幂等。"
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_db() -> db_module.Database:
    return db_module.get_db()


# ------------------------------------------------------------------ 基础

@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/v1/config", tags=["meta"])
def get_config():
    """查看服务端默认容差（请求体 tolerances 可逐字段覆盖）。"""
    return {
        "start_end_tolerance": settings.start_end_tolerance,
        "direction_tolerance_deg": settings.direction_tolerance_deg,
        "shape_tolerance": settings.shape_tolerance,
        "fork_extra_direction_deg": settings.fork_extra_direction_deg,
        "fork_extra_shape": settings.fork_extra_shape,
        "corner_angle_deg": settings.corner_angle_deg,
        "resample_points": settings.resample_points,
        "recognize_missing_penalty": settings.recognize_missing_penalty,
        "recognize_extra_penalty": settings.recognize_extra_penalty,
    }


# ------------------------------------------------------------------ 参考字

@app.put(
    "/v1/references/{character}",
    response_model=ReferenceResponse,
    tags=["references"],
)
def register_reference(
    character: str,
    body: RegisterReferenceRequest,
    db: db_module.Database = Depends(get_db),
):
    """注册或更新某汉字的参考笔画序列（按字符幂等覆盖）。"""
    if character != body.character:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="路径字符与请求体 character 不一致",
        )
    record = db.upsert_reference(
        body.character,
        [[[float(p[0]), float(p[1])] for p in s] for s in body.strokes],
        body.source,
    )
    return record


@app.get(
    "/v1/references/confusions",
    response_model=ConfusionAuditResponse,
    tags=["references"],
)
def reference_confusions(
    threshold: float = Query(
        default=0.0, ge=0.0, le=100.0,
        description="相似度阈值（0-100），仅返回双向相似度达到该值的字符对",
    ),
    limit: int = Query(
        default=100, ge=1, le=1000,
        description="返回字符对数上限，在排序完成后截断",
    ),
    db: db_module.Database = Depends(get_db),
):
    """参考字混淆审计：对库中不同字符的参考轨迹做两两无序匹配。

    每对字符分别计算 A→B 与 B→A 两个方向的置信度（复用识别引擎的
    全局一对一分配与缺笔/多笔惩罚），字符对相似度取两者的较低值，
    避免少笔字（如“一”对“十”）在单向上被高估。仅保留相似度达到
    threshold 的字符对，按相似度降序、同分按字符对字典序稳定排列后
    用 limit 截断。空库或只有一个参考字时返回空列表。
    """
    references = sorted(db.list_references(), key=lambda r: r["character"])
    total_comparisons = len(references) * (len(references) - 1) // 2

    pairs: list[ConfusionPair] = []
    for i in range(len(references)):
        for j in range(i + 1, len(references)):
            ra, rb = references[i], references[j]
            result = compare_reference_pair(
                ra["character"],
                [[tuple(p) for p in s] for s in ra["strokes"]],
                rb["character"],
                [[tuple(p) for p in s] for s in rb["strokes"]],
            )
            if result["score"] >= threshold:
                pairs.append(ConfusionPair(**result))

    # 相似度降序；同分按 (character_a, character_b) 字典序，保证稳定
    pairs.sort(key=lambda p: (-p.score, p.character_a, p.character_b))
    total_hits = len(pairs)
    pairs = pairs[:limit]

    return ConfusionAuditResponse(
        threshold=threshold,
        limit=limit,
        total_references=len(references),
        total_comparisons=total_comparisons,
        total_hits=total_hits,
        pairs=pairs,
    )


@app.get(
    "/v1/references/{character}",
    response_model=ReferenceResponse,
    tags=["references"],
)
def get_reference(character: str, db: db_module.Database = Depends(get_db)):
    record = db.get_reference(character)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="参考字未注册")
    return record


# ------------------------------------------------------------------ 识别

@app.post(
    "/v1/recognize",
    response_model=RecognizeResponse,
    tags=["recognition"],
)
def recognize(
    req: RecognizeRequest, db: db_module.Database = Depends(get_db)
):
    """候选字识别：对未标注字符的笔画轨迹返回按置信度排序的参考字候选。

    与每个已注册参考字在归一化后构建用户笔×参考笔代价矩阵，用全局
    一对一最优分配容忍书写次序差异，缺笔/多笔按固定代价显式计入总分。
    无注册参考字时返回空候选列表。
    """
    references = db.list_references()
    canvas = tuple(req.canvas_size) if req.canvas_size else None

    candidates = [
        recognize_against(
            record["character"],
            reference_strokes=[[tuple(p) for p in s] for s in record["strokes"]],
            user_strokes=[[tuple(p) for p in s] for s in req.strokes],
            canvas_size=canvas,
        )
        for record in references
    ]
    # 置信度降序；相同分数按字符升序，保证同分稳定排序
    candidates.sort(key=lambda c: (-c["score"], c["character"]))
    top = candidates[: req.top_k]

    return RecognizeResponse(
        top_k=req.top_k,
        total_references=len(references),
        candidates=[RecognitionCandidate(**c) for c in top],
    )


# ------------------------------------------------------------------ 校验

def _request_hash(req: ValidateRequest) -> str:
    """请求负载的规范化哈希（含全部影响结果的字段）。"""
    canonical = json.dumps(
        req.model_dump(mode="json", exclude={"submission_id"}),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.post(
    "/v1/validate",
    response_model=ValidateResponse,
    responses={409: {"description": "submission_id 已被不同负载占用"}},
    tags=["validation"],
)
def validate_submission(
    req: ValidateRequest, db: db_module.Database = Depends(get_db)
):
    """校验一次书写提交。

    幂等语义：相同 submission_id + 相同负载 → 返回首个结果并标记
    idempotent_replay=true；相同 submission_id + 不同负载 → 409。
    """
    req_hash = _request_hash(req)

    existing = db.get_submission(req.submission_id)
    if existing is not None:
        if existing["request_hash"] != req_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="submission_id 已被不同负载的提交占用",
            )
        replay = dict(existing["response"])
        replay["idempotent_replay"] = True
        return replay

    # 解析参考笔画：内联优先，其次按 reference_id 查库
    if req.reference_strokes is not None:
        reference = req.reference_strokes
    else:
        record = db.get_reference(req.reference_id)
        if record is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail=f"参考字 '{req.reference_id}' 未注册",
            )
        reference = record["strokes"]

    tol = EngineTolerances.from_request(req.tolerances)
    canvas = tuple(req.canvas_size) if req.canvas_size else None
    result = validate(
        reference_strokes=[[tuple(p) for p in s] for s in reference],
        user_strokes=[[tuple(p) for p in s] for s in req.strokes],
        tol=tol,
        canvas_size=canvas,
    )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    response = ValidateResponse(
        submission_id=req.submission_id,
        character=req.character,
        passed=result.passed,
        score=result.score,
        expected_stroke_count=len(reference),
        actual_stroke_count=len(req.strokes),
        first_failed_stroke=result.first_failed_stroke,
        deviation=result.deviation,
        strokes=[
            {
                "index": v.index,
                "passed": v.passed,
                "score": v.score,
                "reasons": v.reasons,
                "metrics": v.metrics,
            }
            for v in result.strokes
        ],
        idempotent_replay=False,
        created_at=now,
    )

    saved, inserted = db.save_submission(
        req.submission_id, req_hash, response.model_dump(mode="json")
    )
    if not inserted:
        # 并发下同键提交：以首个落库结果为准
        if saved["request_hash"] != req_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="submission_id 已被不同负载的提交占用",
            )
        replay = dict(saved["response"])
        replay["idempotent_replay"] = True
        return replay
    return response


@app.get(
    "/v1/submissions/{submission_id}",
    response_model=SubmissionRecord,
    tags=["validation"],
)
def get_submission(submission_id: str, db: db_module.Database = Depends(get_db)):
    """按幂等键查询历史提交结果。"""
    record = db.get_submission(submission_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="提交不存在")
    return {
        "submission_id": record["submission_id"],
        "request_hash": record["request_hash"],
        "result": record["response"],
        "created_at": record["created_at"],
    }


# ------------------------------------------------------------------ 分析

@app.get(
    "/v1/analytics/practice",
    response_model=PracticeAnalyticsResponse,
    tags=["analytics"],
)
def practice_analytics(
    character: str = Query(min_length=1, max_length=1, description="目标汉字"),
    start: datetime = Query(description="时间闭区间起点（ISO-8601，按 UTC 归一）"),
    end: datetime = Query(description="时间闭区间终点（ISO-8601，按 UTC 归一）"),
    passed: Optional[bool] = Query(
        default=None, description="可选：只统计通过（true）或未通过（false）的提交"
    ),
    db: db_module.Database = Depends(get_db),
):
    """书写练习质量趋势：对筛选后的校验提交做一致性快照统计。

    返回提交数、通过率、平均分、P50/P90 分数（显式线性插值）、首个失败
    笔画分布与按 UTC 日期分桶的趋势；无匹配提交时数值为 0、数组为空。
    """
    # 统一归一到 UTC（naive 输入视为 UTC），回显时带显式时区
    start_utc = analytics.to_naive_utc(start).replace(tzinfo=timezone.utc)
    end_utc = analytics.to_naive_utc(end).replace(tzinfo=timezone.utc)
    if start_utc > end_utc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start 不能晚于 end",
        )

    records = db.snapshot_submissions(character, start_utc, end_utc, passed)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return analytics.build_practice_analytics(
        records,
        character=character,
        start=start_utc,
        end=end_utc,
        passed=passed,
        generated_at=generated_at,
    )
