"""SQLite 只增事件库。

系统状态全部由事件重放得到；任何业务表都不原地改历史。关键约束：

* ``event_seq`` 单调递增，``(aggregate_type, aggregate_id, seq)`` 唯一；
* 提交件以内容哈希为幂等键：同一上传会话重复 complete、或同一会话
  再次 finalize，只返回已生成的 submission_id，**不会重复生成作品**；
* 上传分片以 ``(session_id, chunk_index)`` 去重，断点续传安全。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(aggregate_type, aggregate_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_event_agg ON event(aggregate_type, aggregate_id, id);
CREATE INDEX IF NOT EXISTS idx_event_type ON event(event_type);

CREATE TABLE IF NOT EXISTS upload_session (
    session_id TEXT PRIMARY KEY,
    uploader TEXT NOT NULL,
    media_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    total_chunks INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    declared_sha256 TEXT,
    created_at REAL NOT NULL,
    finalized_submission_id TEXT
);
CREATE TABLE IF NOT EXISTS upload_chunk (
    session_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    received_at REAL NOT NULL,
    PRIMARY KEY (session_id, chunk_index)
);

-- 幂等键：HTTP 请求 / 渠道回执去重
CREATE TABLE IF NOT EXISTS idempotency_record (
    scope TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (scope, idem_key)
);
"""


class ConflictError(RuntimeError):
    """事件版本冲突或业务规则拒绝（含非法状态转移）。"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._counters: dict[tuple[str, str], int] = {}

    def close(self):
        self._conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- 事件 ----------------------------------------------------------

    def next_seq(self, conn, aggregate_type: str, aggregate_id: str) -> int:
        key = (aggregate_type, aggregate_id)
        if key not in self._counters:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM event "
                "WHERE aggregate_type=? AND aggregate_id=?",
                key,
            ).fetchone()
            self._counters[key] = row["m"]
        self._counters[key] += 1
        return self._counters[key]

    def append(
        self,
        conn,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        seq = self.next_seq(conn, aggregate_type, aggregate_id)
        now = time.time()
        conn.execute(
            "INSERT INTO event(aggregate_type, aggregate_id, seq, event_type, "
            "actor, created_at, payload) VALUES (?,?,?,?,?,?,?)",
            (aggregate_type, aggregate_id, seq, event_type, actor,
             now, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        return {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "seq": seq,
            "event_type": event_type,
            "actor": actor,
            "created_at": now,
            "payload": payload,
        }

    def events(self, aggregate_type: str | None = None,
               aggregate_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM event"
        clauses, params = [], []
        if aggregate_type:
            clauses.append("aggregate_type=?")
            params.append(aggregate_type)
        if aggregate_id:
            clauses.append("aggregate_id=?")
            params.append(aggregate_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "seq": row["seq"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "created_at": row["created_at"],
            "payload": json.loads(row["payload"]),
        }

    # ---- 上传会话 ------------------------------------------------------

    def create_session(self, uploader: str, media_type: str, filename: str,
                       total_chunks: int, chunk_size: int,
                       declared_sha256: str | None) -> str:
        sid = uuid.uuid4().hex
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO upload_session(session_id, uploader, media_type, filename, "
                "total_chunks, chunk_size, declared_sha256, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, uploader, media_type, filename, total_chunks, chunk_size,
                 declared_sha256, time.time()),
            )
        return sid

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM upload_session WHERE session_id=?", (session_id,)
        ).fetchone()

    def received_chunks(self, session_id: str) -> set[int]:
        rows = self._conn.execute(
            "SELECT chunk_index FROM upload_chunk WHERE session_id=?", (session_id,)
        ).fetchall()
        return {r["chunk_index"] for r in rows}

    def mark_chunk(self, conn, session_id: str, chunk_index: int) -> bool:
        """登记分片；重复登记返回 False（断点续传时客户端重发同一片）。"""
        try:
            conn.execute(
                "INSERT INTO upload_chunk(session_id, chunk_index, received_at) "
                "VALUES (?,?,?)",
                (session_id, chunk_index, time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def attach_finalized(self, conn, session_id: str, submission_id: str) -> None:
        conn.execute(
            "UPDATE upload_session SET finalized_submission_id=? WHERE session_id=?",
            (submission_id, session_id),
        )

    # ---- 幂等 ----------------------------------------------------------

    def idem_get(self, scope: str, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT response FROM idempotency_record WHERE scope=? AND idem_key=?",
            (scope, key),
        ).fetchone()
        return json.loads(row["response"]) if row else None

    def idem_put(self, conn, scope: str, key: str, response: dict[str, Any]) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO idempotency_record(scope, idem_key, response, created_at) "
            "VALUES (?,?,?,?)",
            (scope, key, json.dumps(response, ensure_ascii=False), time.time()),
        )


def new_id() -> str:
    return uuid.uuid4().hex
