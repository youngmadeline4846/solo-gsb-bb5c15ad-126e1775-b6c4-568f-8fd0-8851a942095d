"""SQLite 持久化层：参考字与幂等提交记录。

使用单连接 + 锁（WAL 模式），FastAPI 同步端点在线程池中调用时保证线程安全。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_glyphs (
    character   TEXT PRIMARY KEY,
    strokes     TEXT NOT NULL,          -- JSON: [[[x,y],...],...]
    source      TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,        -- 请求负载的 SHA-256
    response      TEXT NOT NULL,        -- JSON: ValidateResponse
    created_at    TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_naive_utc(dt: datetime) -> datetime:
    """带时区的时间换算为 naive UTC；naive 时间直接返回（落库时间即 UTC）。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class Database:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 参考字

    def upsert_reference(
        self, character: str, strokes: list, source: Optional[str]
    ) -> dict:
        now = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO reference_glyphs (character, strokes, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(character) DO UPDATE SET
                    strokes = excluded.strokes,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (character, json.dumps(strokes), source, now),
            )
        return {
            "character": character,
            "stroke_count": len(strokes),
            "strokes": strokes,
            "source": source,
            "updated_at": now,
        }

    def get_reference(self, character: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reference_glyphs WHERE character = ?",
                (character,),
            ).fetchone()
        if row is None:
            return None
        strokes = json.loads(row["strokes"])
        return {
            "character": row["character"],
            "stroke_count": len(strokes),
            "strokes": strokes,
            "source": row["source"],
            "updated_at": row["updated_at"],
        }

    def list_references(self) -> list[dict]:
        """列出全部已注册参考字，按字符排序（保证识别结果稳定）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reference_glyphs ORDER BY character ASC"
            ).fetchall()
        records = []
        for row in rows:
            strokes = json.loads(row["strokes"])
            records.append(
                {
                    "character": row["character"],
                    "stroke_count": len(strokes),
                    "strokes": strokes,
                    "source": row["source"],
                    "updated_at": row["updated_at"],
                }
            )
        return records

    # ------------------------------------------------------------ 幂等提交

    def get_submission(self, submission_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "submission_id": row["submission_id"],
            "request_hash": row["request_hash"],
            "response": json.loads(row["response"]),
            "created_at": row["created_at"],
        }

    def snapshot_submissions(
        self,
        character: str,
        start: datetime,
        end: datetime,
        passed: Optional[bool],
    ) -> list[dict]:
        """在单个加锁读取（一致性快照）中筛选已保存的校验提交。

        - character 精确匹配响应负载中的目标字；
        - created_at 落在 [start, end] 闭区间（统一按 UTC 比较）；
        - passed 为 None 时不筛通过状态。

        时间先以秒级 ISO 字符串做宽松粗筛（粗筛区间外扩一秒并放开秒以下
        精度），精确闭区间判定在解析后于内存完成，避免不同精度/时区写法
        带来的字符串比较边界问题。结果按 created_at、submission_id 升序。
        """
        lo = _as_naive_utc(start).replace(microsecond=0).isoformat()
        hi_dt = _as_naive_utc(end).replace(microsecond=0)
        hi = (hi_dt + timedelta(seconds=1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM submissions
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at ASC, submission_id ASC
                """,
                (lo, hi),
            ).fetchall()

        records = []
        for row in rows:
            response = json.loads(row["response"])
            if response.get("character") != character:
                continue
            if passed is not None and bool(response.get("passed")) is not passed:
                continue
            created_at = datetime.fromisoformat(row["created_at"])
            created_at = _as_naive_utc(created_at)
            if not (_as_naive_utc(start) <= created_at <= _as_naive_utc(end)):
                continue
            records.append(
                {
                    "submission_id": row["submission_id"],
                    "request_hash": row["request_hash"],
                    "response": response,
                    "created_at": row["created_at"],
                }
            )
        return records

    def save_submission(
        self,
        submission_id: str,
        request_hash: str,
        response: dict,
        created_at: Optional[str] = None,
    ) -> tuple[dict, bool]:
        """写入提交记录。返回 (记录, 是否由本次调用插入)。

        并发下同键写入由主键约束兜底，以首个落库结果为准。
        created_at 缺省时取当前 UTC 时间；显式传入用于测试/回填场景。
        """
        now = created_at or _utcnow()
        inserted = False
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    """
                    INSERT INTO submissions (submission_id, request_hash, response, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (submission_id, request_hash, json.dumps(response), now),
                )
                inserted = True
            except sqlite3.IntegrityError:
                pass  # 并发重复：以首个写入为准
            row = self._conn.execute(
                "SELECT * FROM submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        record = {
            "submission_id": row["submission_id"],
            "request_hash": row["request_hash"],
            "response": json.loads(row["response"]),
            "created_at": row["created_at"],
        }
        return record, inserted


_db: Optional[Database] = None


def init_db(path: str) -> Database:
    global _db
    _db = Database(path)
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("数据库未初始化")
    return _db
