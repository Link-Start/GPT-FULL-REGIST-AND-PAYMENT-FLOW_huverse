"""SQLite proxy pool and fast proxy probes for the full-flow web UI."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_URLS = (
    "https://chatgpt.com/api/auth/csrf",
    "https://auth.openai.com/api/auth/session",
)
PAYMENT_STRIPE_URL = "https://js.stripe.com/v3/"
PAYMENT_PAYPAL_URL = "https://www.paypal.com/signin"
GEO_URL = "https://get.geojs.io/v1/ip/geo.json"


@dataclass(slots=True)
class CurlProxy:
    proxy: str
    user: str = ""
    scheme: str = "http"


def clean_proxy_value(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("（", "(").replace("）", ")")
    return text


def redact_proxy(value: Any) -> str:
    text = clean_proxy_value(value)
    if not text:
        return ""
    if "://" in text:
        return re.sub(r"//([^:@/]+):([^@/]+)@", r"//\1:***@", text)
    parts = text.split(":")
    if len(parts) >= 4:
        return ":".join([parts[0], parts[1], parts[2], "***"])
    return text


def parse_proxy_for_curl(value: str) -> CurlProxy:
    text = clean_proxy_value(value)
    if not text:
        raise ValueError("proxy value is empty")

    curl_match = re.search(r"\bcurl\b", text)
    if curl_match:
        tokens = shlex.split(text)
        proxy = ""
        user = ""
        for index, token in enumerate(tokens):
            if token in {"-x", "--proxy"} and index + 1 < len(tokens):
                proxy = tokens[index + 1]
            if token in {"-U", "--proxy-user", "--proxy-user"} and index + 1 < len(tokens):
                user = tokens[index + 1]
        if not proxy:
            raise ValueError("curl proxy command does not include -x/--proxy")
        return _proxy_parts_to_curl(proxy, user)

    scheme_hint = ""
    suffix = re.search(r"\(([^)]+)\)\s*$", text)
    if suffix:
        scheme_hint = suffix.group(1).strip().lower()
        text = text[: suffix.start()].strip()
    if "://" in text:
        return _proxy_parts_to_curl(text, "", scheme_hint=scheme_hint)

    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError("proxy must be host:port or host:port:user:pass")
    host, port = parts[0].strip(), parts[1].strip()
    user = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
    scheme = _scheme_from_hint(scheme_hint)
    proxy = f"{scheme}://{host}:{port}" if scheme != "http" else f"http://{host}:{port}"
    return CurlProxy(proxy=proxy, user=user, scheme=scheme)


def _scheme_from_hint(hint: str) -> str:
    text = hint.strip().lower()
    if text in {"socks", "socks5", "socks5h"}:
        return "socks5h"
    return "http"


def _proxy_parts_to_curl(proxy: str, user: str = "", *, scheme_hint: str = "") -> CurlProxy:
    text = clean_proxy_value(proxy)
    if "://" not in text:
        text = f"{_scheme_from_hint(scheme_hint)}://{text}"
    if text.startswith("socks5://"):
        text = "socks5h://" + text[len("socks5://") :]
    scheme = text.split("://", 1)[0].lower()
    if scheme in {"socks", "socks5"}:
        text = re.sub(r"^[^:]+://", "socks5h://", text)
        scheme = "socks5h"
    if "@" in text and not user:
        match = re.match(r"([^:]+://)([^@/]+)@(.+)", text)
        if match:
            user = match.group(2)
            text = match.group(1) + match.group(3)
    return CurlProxy(proxy=text, user=user, scheme=scheme)


class ProxyPool:
    """Manage proxy records in the same SQLite file as the resource pool."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS proxy_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    value TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    protocol_ok INTEGER NOT NULL DEFAULT 0,
                    payment_ok INTEGER NOT NULL DEFAULT 0,
                    protocol_latency_ms INTEGER NOT NULL DEFAULT 0,
                    payment_latency_ms INTEGER NOT NULL DEFAULT 0,
                    ip TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_test_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_proxy_pool_enabled ON proxy_pool(enabled, payment_ok, protocol_ok, id);
                """
            )

    def add_values(self, values: Iterable[str], *, label: str = "") -> int:
        cleaned = [clean_proxy_value(value) for value in values]
        cleaned = [value for value in cleaned if value and not value.startswith("#")]
        if not cleaned:
            return 0
        now = time.time()
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for value in cleaned:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO proxy_pool(value, label, enabled, created_at, updated_at)
                    VALUES(?, ?, 1, ?, ?)
                    """,
                    (value, label, now, now),
                )
                inserted += 1 if cursor.rowcount != 0 else 0
            conn.execute("COMMIT")
        return inserted

    def list_items(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM proxy_pool
                ORDER BY payment_ok DESC, protocol_ok DESC, last_test_at DESC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                "proxies_total": self._count(conn, "1=1"),
                "proxies_enabled": self._count(conn, "enabled=1"),
                "proxies_protocol_ok": self._count(conn, "protocol_ok=1"),
                "proxies_payment_ok": self._count(conn, "payment_ok=1"),
            }

    def delete_item(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM proxy_pool WHERE id=?", (int(item_id),))
            conn.execute("COMMIT")

    def set_enabled(self, item_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE proxy_pool SET enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, time.time(), int(item_id)))
            conn.execute("COMMIT")

    def get_value(self, item_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM proxy_pool WHERE id=?", (int(item_id),)).fetchone()
        if not row:
            raise ValueError(f"proxy #{item_id} not found")
        return str(row["value"])

    def test_item(self, item_id: int, role: str, *, timeout: float = 8.0) -> dict[str, Any]:
        value = self.get_value(item_id)
        result = test_proxy(value, role=role, timeout=timeout)
        now = time.time()
        protocol_ok = 1 if result.get("role") == "protocol" and result.get("ok") else None
        payment_ok = 1 if result.get("role") == "payment" and result.get("ok") else None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if protocol_ok is not None:
                conn.execute(
                    """
                    UPDATE proxy_pool
                    SET protocol_ok=?, protocol_latency_ms=?, ip=?, country=?, timezone=?, last_error=?, last_test_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        protocol_ok,
                        int(result.get("latencyMs") or 0),
                        str(result.get("ip") or ""),
                        str(result.get("country") or ""),
                        str(result.get("timezone") or ""),
                        str(result.get("error") or ""),
                        now,
                        now,
                        int(item_id),
                    ),
                )
            elif payment_ok is not None:
                conn.execute(
                    """
                    UPDATE proxy_pool
                    SET payment_ok=?, payment_latency_ms=?, ip=?, country=?, timezone=?, last_error=?, last_test_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        payment_ok,
                        int(result.get("latencyMs") or 0),
                        str(result.get("ip") or ""),
                        str(result.get("country") or ""),
                        str(result.get("timezone") or ""),
                        str(result.get("error") or ""),
                        now,
                        now,
                        int(item_id),
                    ),
                )
            conn.execute("COMMIT")
        return result

    def _count(self, conn: sqlite3.Connection, where: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM proxy_pool WHERE {where}").fetchone()
        return int(row["n"] if row else 0)

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "redacted": redact_proxy(row["value"]),
            "label": str(row["label"] or ""),
            "enabled": bool(row["enabled"]),
            "protocolOk": bool(row["protocol_ok"]),
            "paymentOk": bool(row["payment_ok"]),
            "protocolLatencyMs": int(row["protocol_latency_ms"] or 0),
            "paymentLatencyMs": int(row["payment_latency_ms"] or 0),
            "ip": str(row["ip"] or ""),
            "country": str(row["country"] or ""),
            "timezone": str(row["timezone"] or ""),
            "lastError": str(row["last_error"] or ""),
            "lastTestAt": float(row["last_test_at"] or 0.0),
            "createdAt": float(row["created_at"] or 0.0),
            "updatedAt": float(row["updated_at"] or 0.0),
        }


def test_proxy(value: str, *, role: str, timeout: float = 8.0) -> dict[str, Any]:
    if not shutil.which("curl"):
        return {"ok": False, "role": role, "error": "curl not found"}
    role = role.strip().lower()
    if role not in {"protocol", "payment"}:
        raise ValueError("role must be protocol or payment")
    parsed = parse_proxy_for_curl(value)
    start = time.time()
    geo = _curl_json(GEO_URL, parsed, timeout=timeout)
    if role == "protocol":
        probes = [_curl_status(url, parsed, timeout=timeout) for url in PROTOCOL_URLS]
        ok = all(_ok_non_5xx(item) for item in probes)
    else:
        stripe = _curl_status(PAYMENT_STRIPE_URL, parsed, timeout=timeout)
        paypal = _curl_status(PAYMENT_PAYPAL_URL, parsed, timeout=timeout)
        probes = [stripe, paypal]
        ok = stripe.get("code") == 200 and _ok_non_5xx(paypal)
    errors = [str(item.get("error") or "") for item in [geo, *probes] if item.get("error")]
    return {
        "ok": bool(ok and not geo.get("error")),
        "role": role,
        "latencyMs": int((time.time() - start) * 1000),
        "scheme": parsed.scheme,
        "ip": str(geo.get("ip") or ""),
        "country": str(geo.get("country") or geo.get("country_code") or ""),
        "timezone": str(geo.get("timezone") or ""),
        "probes": probes,
        "error": "; ".join(errors),
    }


def _ok_non_5xx(result: dict[str, Any]) -> bool:
    code = int(result.get("code") or 0)
    return 200 <= code < 500


def _curl_base_args(parsed: CurlProxy, timeout: float) -> list[str]:
    args = [
        "curl",
        "-L",
        "-sS",
        "--connect-timeout",
        str(max(2.0, min(timeout, 10.0))),
        "--max-time",
        str(max(3.0, timeout)),
        "--proxy",
        parsed.proxy,
    ]
    if parsed.user:
        args.extend(["-U", parsed.user])
    return args


def _curl_status(url: str, parsed: CurlProxy, *, timeout: float) -> dict[str, Any]:
    cmd = _curl_base_args(parsed, timeout) + ["-o", "/dev/null", "-w", "%{http_code} %{time_total}", url]
    started = time.time()
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(5.0, timeout + 2.0), check=False)
    code = 0
    total = 0.0
    fields = (proc.stdout or "").strip().split()
    if fields:
        try:
            code = int(fields[0])
        except ValueError:
            code = 0
    if len(fields) > 1:
        try:
            total = float(fields[1])
        except ValueError:
            total = 0.0
    return {
        "url": url,
        "code": code,
        "ms": int((total or (time.time() - started)) * 1000),
        "error": proc.stderr.strip() if proc.returncode != 0 else "",
    }


def _curl_json(url: str, parsed: CurlProxy, *, timeout: float) -> dict[str, Any]:
    cmd = _curl_base_args(parsed, timeout) + [url]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(5.0, timeout + 2.0), check=False)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()}
    try:
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else {"error": "geo response is not an object"}
    except json.JSONDecodeError as exc:
        return {"error": f"geo json parse failed: {exc}"}
