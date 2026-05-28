"""SQLite-backed resource pools for the full-flow orchestrator.

The pool is intentionally small and dependency-free so the orchestrator can
optionally lease resources from a persistent database without coupling the
protocol or payment projects to any queue implementation.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(slots=True)
class PoolItem:
    id: int
    value: str
    bucket: str = ""
    attempts: int = 0
    retry_count: int = 0
    last_reason: str = ""
    last_card_value: str = ""
    last_phone_value: str = ""
    lease_id: str = ""
    lease_until: float = 0.0


class FullFlowPool:
    """Persist email/card/phone resources in a small SQLite database."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        lease_seconds: int = 7200,
        worker_id: str = "",
        max_email_retries: int = 3,
    ) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = max(60, int(lease_seconds))
        self.worker_id = (worker_id or f"worker-{uuid.uuid4().hex[:8]}").strip()
        self.max_email_retries = max(0, int(max_email_retries))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS email_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL UNIQUE,
                    bucket TEXT NOT NULL DEFAULT 'main',
                    state TEXT NOT NULL DEFAULT 'available',
                    lease_id TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    last_run_id TEXT NOT NULL DEFAULT '',
                    last_card_value TEXT NOT NULL DEFAULT '',
                    last_phone_value TEXT NOT NULL DEFAULT '',
                    last_used_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_email_pool_main ON email_pool(bucket, state, lease_until, last_used_at, id);

                CREATE TABLE IF NOT EXISTS card_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'available',
                    lease_id TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    last_used_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_card_pool_state ON card_pool(state, lease_until, last_used_at, id);

                CREATE TABLE IF NOT EXISTS phone_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'available',
                    lease_id TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_phone_pool_state ON phone_pool(state, lease_until, last_used_at, id);
                """
            )
            self._ensure_column(conn, "email_pool", "last_run_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "email_pool", "last_card_value", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "email_pool", "last_phone_value", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _now(self) -> float:
        return time.time()

    def _lease_token(self, kind: str) -> str:
        return f"{self.worker_id}:{kind}:{uuid.uuid4().hex}"

    def seed_values(self, kind: str, values: Iterable[str]) -> int:
        table = self._table_for(kind)
        cleaned = [self._clean_value(value) for value in values]
        cleaned = [value for value in cleaned if value]
        if not cleaned:
            return 0
        now = self._now()
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for value in cleaned:
                if table == "email_pool":
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO email_pool(value, bucket, state, created_at, updated_at) VALUES(?, 'main', 'available', ?, ?)",
                        (value, now, now),
                    )
                elif table == "card_pool":
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO card_pool(value, state, created_at, updated_at) VALUES(?, 'available', ?, ?)",
                        (value, now, now),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO phone_pool(value, state, created_at, updated_at) VALUES(?, 'available', ?, ?)",
                        (value, now, now),
                    )
                inserted += 1 if cursor.rowcount != 0 else 0
            conn.execute("COMMIT")
        return inserted

    def seed_file(self, kind: str, path: Path | str) -> int:
        file_path = Path(path)
        if not file_path.exists():
            return 0
        values = []
        for raw in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            values.append(text)
        return self.seed_values(kind, values)

    def snapshot(self) -> dict[str, int]:
        with self._connect() as conn:
            now = self._now()
            self._reclaim_expired_locked(conn, now)
            return {
                "emails_main": self._count(conn, "email_pool", "bucket='main' AND state='available'"),
                "emails_retry": self._count(conn, "email_pool", "bucket='retry' AND state='available'"),
                "emails_failed": self._count(conn, "email_pool", "bucket='failed' OR state='failed'"),
                "cards": self._count(conn, "card_pool", "state='available'"),
                "phones": self._count(conn, "phone_pool", "state='available'"),
            }

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            now = self._now()
            self._reclaim_expired_locked(conn, now)
            return {
                "emails_main": self._count(conn, "email_pool", "bucket='main' AND state='available'"),
                "emails_retry": self._count(conn, "email_pool", "bucket='retry' AND state='available'"),
                "emails_failed": self._count(conn, "email_pool", "bucket='failed' OR state='failed'"),
                "emails_leased": self._count(conn, "email_pool", "state='leased'"),
                "emails_total": self._count(conn, "email_pool", "1=1"),
                "cards_available": self._count(conn, "card_pool", "state='available'"),
                "cards_leased": self._count(conn, "card_pool", "state='leased'"),
                "cards_total": self._count(conn, "card_pool", "1=1"),
                "phones_available": self._count(conn, "phone_pool", "state='available'"),
                "phones_leased": self._count(conn, "phone_pool", "state='leased'"),
                "phones_total": self._count(conn, "phone_pool", "1=1"),
            }

    def list_items(
        self,
        kind: str,
        *,
        state: str = "",
        bucket: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        table = self._table_for(kind)
        limit = max(1, min(1000, int(limit)))
        now = self._now()
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if table == "email_pool" and bucket:
            clauses.append("bucket=?")
            params.append(bucket)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        if table == "email_pool" and (state == "failed" or bucket == "failed"):
            order = "ORDER BY updated_at DESC, id DESC"
        else:
            order = "ORDER BY last_used_at ASC, id ASC"
        with self._connect() as conn:
            self._reclaim_expired_locked(conn, now)
            rows = conn.execute(
                f"""
                SELECT *
                FROM {table}
                {where}
                {order}
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._row_to_dict(table, row, now) for row in rows]

    def acquire_email(self) -> PoolItem | None:
        return self._acquire_email_like("email_pool", bucket_promote=True)

    def acquire_card(self, *, exclude_values: Iterable[str] | None = None) -> PoolItem | None:
        return self._acquire_simple("card_pool", exclude_values=exclude_values)

    def acquire_phone(self, *, exclude_values: Iterable[str] | None = None) -> PoolItem | None:
        return self._acquire_simple("phone_pool", exclude_values=exclude_values)

    def acquire_item(self, kind: str) -> PoolItem | None:
        table = self._table_for(kind)
        if table == "email_pool":
            return self.acquire_email()
        if table == "card_pool":
            return self.acquire_card()
        return self.acquire_phone()

    def finalize_email(
        self,
        item_id: int,
        *,
        success: bool,
        retryable: bool,
        reason: str = "",
        run_id: str = "",
        card_value: str = "",
        phone_value: str = "",
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, retry_count FROM email_pool WHERE id=?", (item_id,)).fetchone()
            if not row:
                conn.execute("COMMIT")
                return
            retry_count = int(row["retry_count"] or 0) if "retry_count" in row.keys() else 0
            should_retry = bool(retryable and not success and (self.max_email_retries <= 0 or retry_count < self.max_email_retries))
            if should_retry:
                conn.execute(
                    """
                    UPDATE email_pool
                    SET bucket='retry',
                        state='available',
                        lease_id='',
                        lease_until=0,
                        retry_count=retry_count + 1,
                        last_reason=?,
                        last_run_id=?,
                        last_card_value=?,
                        last_phone_value=?,
                        last_used_at=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (reason, run_id, card_value, phone_value, now, now, item_id),
                )
            elif success:
                conn.execute("DELETE FROM email_pool WHERE id=?", (item_id,))
            else:
                conn.execute(
                    """
                    UPDATE email_pool
                    SET bucket='failed',
                        state='failed',
                        lease_id='',
                        lease_until=0,
                        last_reason=?,
                        last_run_id=?,
                        last_card_value=?,
                        last_phone_value=?,
                        last_used_at=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (reason, run_id, card_value, phone_value, now, now, item_id),
                )
            conn.execute("COMMIT")

    def consume_card(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM card_pool WHERE id=?", (item_id,))
            conn.execute("COMMIT")

    def release_phone(self, item_id: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM phone_pool WHERE id=?", (item_id,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE phone_pool
                    SET state='available', lease_id='', lease_until=0, updated_at=?
                    WHERE id=?
                    """,
                    (now, item_id),
                )
            conn.execute("COMMIT")

    def _acquire_email_like(self, table: str, *, bucket_promote: bool = False) -> PoolItem | None:
        now = self._now()
        lease_id = self._lease_token("email")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reclaim_expired_locked(conn, now)
            if bucket_promote:
                main_available = self._count(conn, table, "bucket='main' AND state='available'")
                if main_available <= 0:
                    conn.execute(
                        """
                        UPDATE email_pool
                        SET bucket='main', updated_at=?
                        WHERE bucket='retry' AND state='available'
                        """,
                        (now,),
                    )
            row = conn.execute(
                """
                SELECT id, value, bucket, attempts, retry_count, last_reason, last_card_value, last_phone_value
                FROM email_pool
                WHERE bucket='main' AND state='available'
                ORDER BY last_used_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE email_pool
                SET state='leased', lease_id=?, lease_until=?, attempts=attempts + 1, last_used_at=?, updated_at=?
                WHERE id=?
                """,
                (lease_id, now + self.lease_seconds, now, now, row["id"]),
            )
            conn.execute("COMMIT")
            return PoolItem(
                id=int(row["id"]),
                value=str(row["value"]),
                bucket=str(row["bucket"] or "main"),
                attempts=int(row["attempts"] or 0) + 1,
                retry_count=int(row["retry_count"] or 0),
                last_reason=str(row["last_reason"] or "") if "last_reason" in row.keys() else "",
                last_card_value=str(row["last_card_value"] or "") if "last_card_value" in row.keys() else "",
                last_phone_value=str(row["last_phone_value"] or "") if "last_phone_value" in row.keys() else "",
                lease_id=lease_id,
                lease_until=now + self.lease_seconds,
            )

    def _acquire_simple(self, table: str, *, exclude_values: Iterable[str] | None = None) -> PoolItem | None:
        now = self._now()
        lease_id = self._lease_token(table)
        excluded = [self._clean_value(value) for value in (exclude_values or []) if self._clean_value(value)]
        exclude_clause = ""
        params: list[Any] = []
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f" AND value NOT IN ({placeholders})"
            params.extend(excluded)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reclaim_expired_locked(conn, now)
            row = conn.execute(
                f"""
                SELECT id, value, use_count
                FROM {table}
                WHERE state='available'{exclude_clause}
                ORDER BY last_used_at ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            conn.execute(
                f"""
                UPDATE {table}
                SET state='leased', lease_id=?, lease_until=?, use_count=use_count + 1, last_used_at=?, updated_at=?
                WHERE id=?
                """,
                (lease_id, now + self.lease_seconds, now, now, row["id"]),
            )
            conn.execute("COMMIT")
            return PoolItem(
                id=int(row["id"]),
                value=str(row["value"]),
                attempts=int(row["use_count"] or 0) + 1,
                lease_id=lease_id,
                lease_until=now + self.lease_seconds,
            )

    def _reclaim_expired_locked(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("UPDATE email_pool SET state='available', lease_id='', lease_until=0 WHERE state='leased' AND lease_until > 0 AND lease_until <= ?", (now,))
        conn.execute("UPDATE card_pool SET state='available', lease_id='', lease_until=0 WHERE state='leased' AND lease_until > 0 AND lease_until <= ?", (now,))
        conn.execute("UPDATE phone_pool SET state='available', lease_id='', lease_until=0 WHERE state='leased' AND lease_until > 0 AND lease_until <= ?", (now,))

    def _table_for(self, kind: str) -> str:
        text = self._clean_value(kind).lower()
        if text in {"email", "emails"}:
            return "email_pool"
        if text in {"card", "cards"}:
            return "card_pool"
        if text in {"phone", "phones", "sms"}:
            return "phone_pool"
        raise ValueError(f"unsupported pool kind: {kind}")

    def _email_bucket(self, value: str, *, default: str = "main") -> str:
        text = self._clean_value(value).lower() or default
        if text not in {"main", "retry", "failed"}:
            return default
        return text

    def _count(self, conn: sqlite3.Connection, table: str, where: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}").fetchone()
        return int(row["n"] if row else 0)

    def promote_retry_emails(self) -> int:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reclaim_expired_locked(conn, now)
            cursor = conn.execute(
                """
                UPDATE email_pool
                SET bucket='main', updated_at=?
                WHERE bucket='retry' AND state='available'
                """,
                (now,),
            )
            conn.execute("COMMIT")
            return int(cursor.rowcount or 0)

    def restore_failed_emails(self, *, bucket: str = "retry") -> int:
        target_bucket = self._email_bucket(bucket, default="retry")
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE email_pool
                SET bucket=?,
                    state='available',
                    lease_id='',
                    lease_until=0,
                    updated_at=?
                WHERE bucket='failed' OR state='failed'
                """,
                (target_bucket, now),
            )
            conn.execute("COMMIT")
            return int(cursor.rowcount or 0)

    def delete_failed_emails(self) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM email_pool WHERE bucket='failed' OR state='failed'")
            conn.execute("COMMIT")
            return int(cursor.rowcount or 0)

    def restore_email(self, item_id: int, *, bucket: str = "retry") -> None:
        target_bucket = self._email_bucket(bucket, default="retry")
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE email_pool
                SET bucket=?,
                    state='available',
                    lease_id='',
                    lease_until=0,
                    updated_at=?
                WHERE id=?
                """,
                (target_bucket, now, int(item_id)),
            )
            conn.execute("COMMIT")

    def delete_item(self, kind: str, item_id: int) -> None:
        table = self._table_for(kind)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DELETE FROM {table} WHERE id=?", (int(item_id),))
            conn.execute("COMMIT")

    def release_item(self, kind: str, item_id: int, *, reason: str = "") -> None:
        table = self._table_for(kind)
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if table == "phone_pool":
                conn.execute(
                    """
                    UPDATE phone_pool
                    SET state='available', lease_id='', lease_until=0, updated_at=?
                    WHERE id=?
                    """,
                    (now, int(item_id)),
                )
            elif table == "email_pool":
                conn.execute(
                    """
                    UPDATE email_pool
                    SET state='available', lease_id='', lease_until=0, last_reason=?, updated_at=?
                    WHERE id=?
                    """,
                    (reason, now, int(item_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE card_pool
                    SET state='available', lease_id='', lease_until=0, last_reason=?, updated_at=?
                    WHERE id=?
                    """,
                    (reason, now, int(item_id)),
                )
            conn.execute("COMMIT")

    def get_item(self, kind: str, item_id: int) -> dict[str, Any] | None:
        table = self._table_for(kind)
        now = self._now()
        with self._connect() as conn:
            self._reclaim_expired_locked(conn, now)
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (int(item_id),)).fetchone()
        return self._row_to_dict(table, row, now) if row else None

    def _row_to_dict(self, table: str, row: sqlite3.Row, now: float) -> dict[str, Any]:
        lease_until = float(row["lease_until"] or 0.0)
        lease_remaining = max(0.0, lease_until - now)
        out: dict[str, Any] = {
            "id": int(row["id"]),
            "value": str(row["value"]),
            "state": str(row["state"] or "available"),
            "leaseId": str(row["lease_id"] or ""),
            "leaseUntil": lease_until,
            "leaseRemaining": round(lease_remaining, 3),
            "lastUsedAt": float(row["last_used_at"] or 0.0),
            "createdAt": float(row["created_at"] or 0.0),
            "updatedAt": float(row["updated_at"] or 0.0),
        }
        if table == "email_pool":
            out.update(
                {
                    "kind": "email",
                    "bucket": str(row["bucket"] or "main"),
                    "attempts": int(row["attempts"] or 0),
                    "retryCount": int(row["retry_count"] or 0),
                    "lastReason": str(row["last_reason"] or ""),
                    "lastRunId": str(row["last_run_id"] or "") if "last_run_id" in row.keys() else "",
                    "lastCardValue": str(row["last_card_value"] or "") if "last_card_value" in row.keys() else "",
                    "lastPhoneValue": str(row["last_phone_value"] or "") if "last_phone_value" in row.keys() else "",
                }
            )
        elif table == "card_pool":
            out.update(
                {
                    "kind": "card",
                    "useCount": int(row["use_count"] or 0),
                    "lastReason": str(row["last_reason"] or ""),
                }
            )
        else:
            out.update(
                {
                    "kind": "phone",
                    "useCount": int(row["use_count"] or 0),
                }
            )
        return out

    def _clean_value(self, value: Any) -> str:
        return str(value or "").strip()
