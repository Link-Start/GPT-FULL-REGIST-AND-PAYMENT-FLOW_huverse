#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loose orchestration for GPT trial protocol registration + ruyi PayPal payment.

This file intentionally connects the two projects only through their CLI
contracts:

1. gpt_trial_protocol -> emits checkoutUrl in JSONL.
2. ruyipage/ruyi_paypal_flow.py -> consumes --start-url and writes result JSON.

No implementation modules are imported across project boundaries, so either
side can still be run, debugged, or restored independently.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import select
import json
import os
import random
import re
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from full_flow_pool import FullFlowPool, PoolItem


ROOT = Path(__file__).resolve().parent
DEFAULT_PROTOCOL_PROJECT = (
    ROOT.parent / "openai" / "gpt_trial_protocol"
    if (ROOT.parent / "openai" / "gpt_trial_protocol").exists()
    else ROOT / "protocol" / "gpt_trial_protocol"
)
DEFAULT_PAYMENT_PROJECT = ROOT / "ruyipage"
DEFAULT_ADDRESS_JSON = DEFAULT_PAYMENT_PROJECT / "address.example.json"
DEFAULT_SUCCESS_ACCOUNT_FILE = ROOT / "accfile" / "pwd" / "success_accounts.txt"
DEFAULT_ICLOUD_SUCCESS_ACCOUNT_FILE = ROOT / "accfile" / "pwd" / "icsuccess_accounts.txt"
DEFAULT_SUCCESS_GETRT_DIR = ROOT / "accfile" / "json"
DEFAULT_SUCCESS_SESSION_JSON_DIR = ROOT / "accfile" / "session_json"
DEFAULT_POOL_DB = ROOT / "accfile" / "pool" / "full_flow.sqlite3"
DEFAULT_GETRT_SCRIPT = ROOT / "getrt" / "codex_oauth_getrt.py"
DEFAULT_PAYMENT_SLOT_LOCK_DIR = ROOT / "runtime" / "payment_slots"
NO_PROXY_VALUES = {"", "0", "none", "no", "off", "direct", "false"}
SYSTEM_PROXY_VALUES = {"system", "win", "windows", "windows-system"}
SERVER_DEPLOY_ROOT = Path("/opt/openaii").resolve()
LOCAL_BRIDGE_PROXY_VALUES = {"http://127.0.0.1:7897", "127.0.0.1:7897"}
BOOL_TRUE_VALUES = {"1", "true", "yes", "on"}
BOOL_FALSE_VALUES = {"0", "false", "no", "off"}


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in env:
            env[key] = value


def redact_arg(arg: str) -> str:
    if "token=" in arg:
        return re.sub(r"token=[^&\s]+", "token=<redacted>", arg)
    if "key=" in arg:
        return re.sub(r"key=[^&\s]+", "key=<redacted>", arg)
    if re.fullmatch(r"\d{13,19}", re.sub(r"\D", "", arg or "")):
        digits = re.sub(r"\D", "", arg)
        return "*" * max(0, len(digits) - 4) + digits[-4:]
    return arg


def redacted_cmd(cmd: list[str]) -> list[str]:
    out: list[str] = []
    skip_value = False
    sensitive_value_for = {
        "--sms-line",
        "--card-line",
        "--phone-line",
        "--phone-number",
        "--phone-otp-api",
        "--phone-otp-cmd",
        "--phone-otp",
        "--proxy",
        "--proxy-chain-upstream",
        "--proxy-chain-via",
    }
    for item in cmd:
        if skip_value:
            out.append("<redacted>")
            skip_value = False
            continue
        out.append(redact_arg(item))
        if item in sensitive_value_for:
            skip_value = True
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


class PaymentSlotLease:
    def __init__(self, path: Path, handle: Any, slot: int, slots: int) -> None:
        self.path = path
        self.handle = handle
        self.slot = slot
        self.slots = slots
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
        except Exception:
            pass
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass
        log(f"[payment-slot] released slot {self.slot + 1}/{self.slots}")


def payment_slot_env(base_env: dict[str, str], lease: PaymentSlotLease | None) -> dict[str, str]:
    if not lease:
        return base_env
    slot_dir = lease.path.parent / f"slot_{lease.slot + 1}"
    profile_root = slot_dir / "profiles"
    tmp_dir = slot_dir / "tmp"
    xdg_dir = slot_dir / "xdg_runtime"
    home_dir = slot_dir / "home"
    cache_dir = slot_dir / "cache"
    config_dir = slot_dir / "config"
    for path in (profile_root, tmp_dir, xdg_dir, home_dir, cache_dir, config_dir):
        path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(xdg_dir, 0o700)
    except OSError:
        pass
    env = base_env.copy()
    env["RUYI_PROFILE_ROOT"] = str(profile_root)
    env["TMPDIR"] = str(tmp_dir)
    env["XDG_RUNTIME_DIR"] = str(xdg_dir)
    env["HOME"] = str(home_dir)
    env["XDG_CACHE_HOME"] = str(cache_dir)
    env["XDG_CONFIG_HOME"] = str(config_dir)
    env["RUYI_FIREFOX_STARTUP_LOCK"] = str(lease.path.parent / "firefox_startup.lock")
    env["RUYI_PAYMENT_SLOT"] = str(lease.slot + 1)
    return env


def acquire_payment_slot(lock_dir: Path, slots: int, *, owner: str, wait_interval: float = 1.0) -> PaymentSlotLease | None:
    """Cross-process semaphore for ruyi/Firefox payment sessions."""
    if slots <= 0:
        log("[payment-slot] disabled")
        return None
    slots = max(1, min(32, int(slots)))
    wait_interval = max(0.25, float(wait_interval))
    lock_dir.mkdir(parents=True, exist_ok=True)
    logged_wait = False
    while True:
        for slot in range(slots):
            path = lock_dir / f"payment_slot_{slot + 1}.lock"
            handle = path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            handle.seek(0)
            handle.truncate()
            handle.write(f"owner={owner}\npid={os.getpid()}\nstartedAt={datetime.now(timezone.utc).isoformat()}\n")
            handle.flush()
            log(f"[payment-slot] acquired slot {slot + 1}/{slots}")
            return PaymentSlotLease(path, handle, slot, slots)
        if not logged_wait:
            log(f"[payment-slot] all {slots} slot(s) busy; waiting before starting payment browser")
            logged_wait = True
        time.sleep(wait_interval)


def normalize_optional_proxy(value: str | None) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in NO_PROXY_VALUES else text


def randomize_proxy_session(value: str) -> tuple[str, bool]:
    """Rotate provider session IDs so concurrent payment browsers do not share one exit."""
    text = str(value or "").strip()
    if not text:
        return "", False
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sid = "".join(random.choice(alphabet) for _ in range(8))
    rotated, count = re.subn(r"(?<=-sid-)[^-:@/()]+(?=-t-)", sid, text, count=1)
    return rotated, bool(count)


def parse_optional_bool(value: str | None, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if text in BOOL_TRUE_VALUES:
        return True
    if text in BOOL_FALSE_VALUES:
        return False
    return bool(default)


def effective_payment_headless_default(value: str | None = None, *, root: Path | None = None) -> bool:
    runtime_root = root.resolve() if root is not None else ROOT.resolve()
    return parse_optional_bool(value, default=runtime_root == SERVER_DEPLOY_ROOT)


def effective_proxy_chain_via(value: str | None, *, root: Path | None = None) -> str:
    text = normalize_optional_proxy(value)
    if not text:
        return ""
    runtime_root = root.resolve() if root is not None else ROOT.resolve()
    if runtime_root == SERVER_DEPLOY_ROOT and text in LOCAL_BRIDGE_PROXY_VALUES:
        return ""
    return text


def _as_proxy_url(value: str, default_scheme: str = "http") -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"{default_scheme}://{value}"


def parse_proxy_url(value: str) -> dict[str, Any] | None:
    value = (value or "").strip()
    if not value:
        return None
    suffix_scheme = ""
    suffix = re.search(r"\((https?|socks5?|socks5h)\)\s*$", value, re.I)
    if suffix:
        suffix_scheme = suffix.group(1).lower()
        if suffix_scheme == "socks":
            suffix_scheme = "socks5"
        value = value[: suffix.start()].strip()
    if "://" not in value:
        parts = value.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host = parts[0]
            port = parts[1]
            username = parts[2]
            password = ":".join(parts[3:])
            value = (
                (suffix_scheme or "http")
                + "://"
                + urllib.parse.quote(username, safe="")
                + ":"
                + urllib.parse.quote(password, safe="")
                + f"@{host}:{port}"
            )
        elif len(parts) == 2 and parts[1].isdigit():
            value = f"{suffix_scheme or 'http'}://{value}"
    else:
        scheme, rest = value.split("://", 1)
        if "@" not in rest:
            parts = rest.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host = parts[0]
                port = parts[1]
                username = parts[2]
                password = ":".join(parts[3:])
                value = (
                    f"{scheme}://"
                    + urllib.parse.quote(username, safe="")
                    + ":"
                    + urllib.parse.quote(password, safe="")
                    + f"@{host}:{port}"
                )
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError("proxy must be URL, host:port, or host:port:user:pass with optional (socks)/(http) suffix")
    scheme = parsed.scheme.lower()
    if scheme == "socks":
        scheme = "socks5"
    return {
        "scheme": scheme,
        "host": parsed.hostname,
        "port": int(parsed.port),
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
    }


def proxy_display(proxy: dict[str, Any] | None) -> str:
    if not proxy:
        return "direct"
    return f"{proxy.get('scheme')}://{proxy.get('host')}:{proxy.get('port')}"


def find_free_port(start: int = 20000, end: int = 24000) -> int:
    for _ in range(200):
        port = random.randint(start, end)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("failed to find a free local proxy port")


class ChainedHttpProxy:
    """Local HTTP proxy: client -> localhost -> parent proxy -> upstream proxy."""

    def __init__(self, upstream: dict[str, Any], parent: dict[str, Any] | None = None, host: str = "127.0.0.1") -> None:
        self.upstream = upstream
        self.parent = parent
        self.host = host
        self.port = find_free_port()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(128)
        server.settimeout(0.5)
        self._sock = server
        self._thread = threading.Thread(target=self._serve, name="fullflow-proxy-chain", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(20)
        upstream_sock: socket.socket | None = None
        try:
            header = self._read_header(client)
            if not header:
                return
            head = header.decode("iso-8859-1", errors="replace")
            first, _, rest = head.partition("\r\n")
            parts = first.split()
            if len(parts) < 3:
                return
            method, target, version = parts[0].upper(), parts[1], parts[2]
            if method == "CONNECT":
                host, port = self._split_host_port(target, 443)
                upstream_sock = self._connect_target(host, port)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(client, upstream_sock)
                return
            host, port, path = self._parse_http_target(target, rest)
            upstream_sock = self._connect_target(host, port)
            first_out = f"{method} {path} {version}\r\n"
            headers = []
            for line in rest.split("\r\n"):
                if not line:
                    continue
                if line.lower().startswith(("proxy-connection:", "proxy-authorization:")):
                    continue
                headers.append(line)
            upstream_sock.sendall((first_out + "\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
            self._relay(client, upstream_sock)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
        finally:
            for sock in (client, upstream_sock):
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _read_header(self, sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _split_host_port(self, value: str, default_port: int) -> tuple[str, int]:
        if value.startswith("[") and "]" in value:
            host, _, tail = value[1:].partition("]")
            port = int(tail[1:]) if tail.startswith(":") else default_port
            return host, port
        if ":" in value:
            host, port = value.rsplit(":", 1)
            if port.isdigit():
                return host, int(port)
        return value, default_port

    def _parse_http_target(self, target: str, headers: str) -> tuple[str, int, str]:
        parsed = urllib.parse.urlparse(target)
        if parsed.hostname:
            path = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, parsed.fragment))
            return parsed.hostname, int(parsed.port or (443 if parsed.scheme == "https" else 80)), path
        host_header = ""
        for line in headers.split("\r\n"):
            if line.lower().startswith("host:"):
                host_header = line.split(":", 1)[1].strip()
                break
        host, port = self._split_host_port(host_header, 80)
        return host, port, target or "/"

    def _connect_to_upstream_endpoint(self) -> socket.socket:
        parent = self.parent
        if not parent:
            return socket.create_connection((self.upstream["host"], int(self.upstream["port"])), timeout=20)
        if parent["scheme"].startswith("socks"):
            import socks

            sock = socks.socksocket()
            proxy_type = socks.SOCKS5 if parent["scheme"].startswith("socks5") else socks.SOCKS4
            sock.set_proxy(
                proxy_type,
                parent["host"],
                int(parent["port"]),
                username=parent.get("username") or None,
                password=parent.get("password") or None,
                rdns=True,
            )
            sock.settimeout(20)
            sock.connect((self.upstream["host"], int(self.upstream["port"])))
            return sock
        sock = socket.create_connection((parent["host"], int(parent["port"])), timeout=20)
        self._http_connect(sock, self.upstream["host"], int(self.upstream["port"]), parent)
        return sock

    def _connect_target(self, host: str, port: int) -> socket.socket:
        sock = self._connect_to_upstream_endpoint()
        if self.upstream["scheme"].startswith("socks"):
            self._socks_connect(sock, host, port, self.upstream)
        else:
            self._http_connect(sock, host, port, self.upstream)
        return sock

    def _http_connect(self, sock: socket.socket, host: str, port: int, proxy: dict[str, Any]) -> None:
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}", "Proxy-Connection: keep-alive"]
        if proxy.get("username") or proxy.get("password"):
            token = base64.b64encode(f"{proxy.get('username', '')}:{proxy.get('password', '')}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        response = self._read_header(sock).decode("iso-8859-1", errors="replace")
        if not re.search(r"^HTTP/\d(?:\.\d)?\s+2\d\d\b", response):
            raise OSError(f"proxy CONNECT failed: {response.splitlines()[:1]}")

    def _socks_connect(self, sock: socket.socket, host: str, port: int, proxy: dict[str, Any]) -> None:
        username = (proxy.get("username") or "").encode()
        password = (proxy.get("password") or "").encode()
        methods = b"\x00" + (b"\x02" if username or password else b"")
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        resp = sock.recv(2)
        if len(resp) != 2 or resp[0] != 5 or resp[1] == 0xFF:
            raise OSError("SOCKS5 method negotiation failed")
        if resp[1] == 0x02:
            sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth = sock.recv(2)
            if len(auth) != 2 or auth[1] != 0:
                raise OSError("SOCKS5 auth failed")
        host_b = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(port).to_bytes(2, "big"))
        resp = sock.recv(4)
        if len(resp) != 4 or resp[1] != 0:
            raise OSError(f"SOCKS5 connect failed: {resp!r}")
        atyp = resp[3]
        if atyp == 1:
            to_read = 4
        elif atyp == 3:
            length = sock.recv(1)
            to_read = length[0] if length else 0
        elif atyp == 4:
            to_read = 16
        else:
            raise OSError("SOCKS5 invalid address type")
        if to_read:
            sock.recv(to_read)
        sock.recv(2)

    def _relay(self, a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        sockets = [a, b]
        deadline = time.time() + 600
        while time.time() < deadline:
            readable, _, errored = select.select(sockets, [], sockets, 1)
            if errored:
                return
            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    return


def start_proxy_chain(upstream_value: str, via_value: str, label: str) -> ChainedHttpProxy | None:
    upstream_text = normalize_optional_proxy(upstream_value)
    if not upstream_text:
        return None
    via_text = effective_proxy_chain_via(via_value)
    parent = None if not via_text or via_text.lower() in SYSTEM_PROXY_VALUES else parse_proxy_url(via_text)
    upstream = parse_proxy_url(upstream_text)
    if not upstream:
        return None
    bridge = ChainedHttpProxy(upstream, parent)
    bridge.start()
    via_label = proxy_display(parent) if parent else "direct"
    log(f"[{label}] proxy chain {bridge.url} -> {via_label} -> {proxy_display(upstream)}")
    return bridge


def parse_json_line(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_streamed(cmd: list[str], cwd: Path, log_path: Path, env: dict[str, str], dry_run: bool = False) -> tuple[int, list[dict[str, Any]], float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"[cmd] cwd={cwd}")
    log("[cmd] " + " ".join(shlex.quote(x) for x in redacted_cmd(cmd)))
    if dry_run:
        log_path.write_text("[dry-run]\n" + " ".join(shlex.quote(x) for x in redacted_cmd(cmd)) + "\n", encoding="utf-8")
        return 0, [], 0.0

    started = time.time()
    events: list[dict[str, Any]] = []
    with log_path.open("w", encoding="utf-8") as fp:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            fp.write(line)
            fp.flush()
            print(line, end="", flush=True)
            event = parse_json_line(line)
            if event is not None:
                events.append(event)
        rc = proc.wait()
    return rc, events, time.time() - started


def sanitize_sensitive_output(line: str) -> str:
    """Keep logs useful without exposing OAuth or provider secrets."""
    text = re.sub(r"(refresh_token\s*(?:received|=|:)\s*)[^\s,\"']+", r"\1<redacted>", line, flags=re.I)
    text = re.sub(r'("refresh_token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text, flags=re.I)
    text = re.sub(r"(access_token\s*(?:=|:)\s*)[^\s,\"']+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(r'("access_token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text, flags=re.I)
    text = re.sub(r"(id_token\s*(?:=|:)\s*)[^\s,\"']+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(r'("id_token"\s*:\s*")[^"]+(")', r'\1<redacted>\2', text, flags=re.I)
    return redact_arg(text)


def run_streamed_sanitized(cmd: list[str], cwd: Path, log_path: Path, env: dict[str, str], dry_run: bool = False) -> tuple[int, list[dict[str, Any]], float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"[cmd] cwd={cwd}")
    log("[cmd] " + " ".join(shlex.quote(x) for x in redacted_cmd(cmd)))
    if dry_run:
        log_path.write_text("[dry-run]\n" + " ".join(shlex.quote(x) for x in redacted_cmd(cmd)) + "\n", encoding="utf-8")
        return 0, [], 0.0

    started = time.time()
    events: list[dict[str, Any]] = []
    with log_path.open("w", encoding="utf-8") as fp:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            safe_line = sanitize_sensitive_output(line)
            fp.write(safe_line)
            fp.flush()
            print(safe_line, end="", flush=True)
            event = parse_json_line(safe_line)
            if event is not None:
                events.append(event)
        rc = proc.wait()
    return rc, events, time.time() - started


def normalize_sms_line(value: str) -> str:
    text = str(value or "").strip()
    if "|" in text:
        return text
    if "----" in text:
        phone, url = text.split("----", 1)
        return f"{phone.strip()}|{url.strip()}"
    raise ValueError("sms line must be PHONE|URL or PHONE----URL")


def parse_card_line(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if "|" in text:
        parts = [x.strip() for x in text.split("|") if x.strip()]
        if len(parts) != 4:
            raise ValueError("--card-line with | must be CARD|MM|YYYY|CVV")
        number, month, year, cvv = parts
    else:
        tokens = re.findall(r"\d+", text)
        if len(tokens) < 5:
            raise ValueError("--card-line must contain card number, month, year, cvv")
        cvv = tokens[-1]
        year = tokens[-2]
        month = tokens[-3]
        number = "".join(tokens[:-3])
    if len(year) == 2:
        year = "20" + year
    return {
        "cardType": "VISA",
        "cardNumber": re.sub(r"\D+", "", number),
        "expiry": f"{month.zfill(2)}/{year}",
        "cvv": cvv,
    }


def write_card_json(card_line: str, out_dir: Path) -> Path:
    card = parse_card_line(card_line)
    path = out_dir / "card.json"
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def is_payment_checkout_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or ""))
    return parsed.scheme == "https" and parsed.netloc == "pay.openai.com" and parsed.path.startswith("/c/pay/")


def raw_checkout_from_events(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        url = event.get("checkoutUrl")
        if isinstance(url, str) and url.startswith("https://pay.openai.com/"):
            return url
    return ""


def raw_checkout_from_jsonl(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        event = parse_json_line(raw)
        if not event:
            continue
        url = event.get("checkoutUrl")
        if isinstance(url, str) and url.startswith("https://pay.openai.com/"):
            return url
    return ""


def checkout_from_events(events: list[dict[str, Any]]) -> str:
    url = raw_checkout_from_events(events)
    return url if is_payment_checkout_url(url) else ""


def checkout_from_jsonl(path: Path) -> str:
    url = raw_checkout_from_jsonl(path)
    return url if is_payment_checkout_url(url) else ""


def email_from_events(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        email = event.get("email")
        if isinstance(email, str) and "@" in email:
            return email
    return ""


def email_from_jsonl(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        event = parse_json_line(raw)
        if not event:
            continue
        email = event.get("email")
        if isinstance(email, str) and "@" in email:
            return email
    return ""


def session_output_from_events(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        path = event.get("sessionOutputPath")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return ""


def session_output_from_jsonl(path: Path) -> str:
    if not path.exists():
        return ""
    for raw in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        event = parse_json_line(raw)
        if not event:
            continue
        session_path = event.get("sessionOutputPath")
        if isinstance(session_path, str) and session_path.strip():
            return session_path.strip()
    return ""


def success_account_file_for(args: argparse.Namespace, email: str) -> Path:
    if args.success_account_file:
        return Path(args.success_account_file).resolve()
    lowered_email = (email or "").strip().lower()
    if args.email_type == "icloud" or lowered_email.endswith(("@icloud.com", "@me.com", "@mac.com")):
        return DEFAULT_ICLOUD_SUCCESS_ACCOUNT_FILE.resolve()
    return DEFAULT_SUCCESS_ACCOUNT_FILE.resolve()


def append_success_account(path: Path, email: str) -> None:
    if not email:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_email = email.strip().lower()
    line = f"{normalized_email}\n"
    with path.open("a+", encoding="utf-8") as fp:
        locked = False
        fcntl_mod = None
        try:
            import fcntl as fcntl_mod

            fcntl_mod.flock(fp.fileno(), fcntl_mod.LOCK_EX)
            locked = True
        except Exception:
            fcntl_mod = None
        try:
            fp.seek(0)
            for existing_line in fp:
                for part in existing_line.replace(",", " ").split():
                    if part.strip().lower() == normalized_email:
                        return
            fp.seek(0, os.SEEK_END)
            fp.write(line)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except Exception:
                pass
        finally:
            try:
                if locked and fcntl_mod is not None:
                    fcntl_mod.flock(fp.fileno(), fcntl_mod.LOCK_UN)
            except Exception:
                pass


def safe_email_json_name(email: str) -> str:
    name = (email or "").strip()
    if not name:
        return ""
    return re.sub(r"[^A-Za-z0-9@._+-]+", "_", name) + ".json"


def read_getrt_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def has_refresh_token(path: Path) -> bool:
    payload = read_getrt_result(path)
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            token = item.get("refresh_token")
            if isinstance(token, str) and token.strip():
                return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def has_access_token(path: Path) -> bool:
    payload = read_getrt_result(path)
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("access_token", "accessToken"):
                token = item.get(key)
                if isinstance(token, str) and token.strip():
                    return True
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def getrt_missing_reason(path: Path) -> str:
    payload = read_getrt_result(path)
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("refresh_token_missing_reason", "missing_reason"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if item.get("requires_phone") is True or item.get("requiresPhone") is True:
                return "add_phone_required"
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def getrt_requires_phone(path: Path) -> bool:
    return getrt_missing_reason(path) == "add_phone_required"


def archive_getrt_result(result_path: Path, archive_dir: Path, email: str) -> Path | None:
    name = safe_email_json_name(email)
    if not name or not result_path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / name
    shutil.copy2(result_path, dest)
    return dest


def pool_item_summary(item: PoolItem | None) -> dict[str, Any]:
    if not item:
        return {}
    return {
        "id": item.id,
        "bucket": item.bucket,
        "attempts": item.attempts,
        "retryCount": item.retry_count,
        "lastReason": item.last_reason,
        "lastCardValue": item.last_card_value,
        "lastPhoneValue": item.last_phone_value,
    }


def should_retry_email_pool(summary_status: str, payment_attempts: list[dict[str, Any]]) -> tuple[bool, str]:
    if summary_status.startswith("success"):
        return False, "success"
    if summary_status == "protocol_no_payment_checkout":
        return False, "non_payment_checkout_url"
    hard_fail_reasons = {
        "stripe_amount_check_failed",
    }
    for attempt in payment_attempts:
        retry_reason = str(attempt.get("retryReason") or "")
        result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
        result_reason = str((result or {}).get("reason") or "")
        if retry_reason in hard_fail_reasons:
            return False, retry_reason
        if retry_reason == "card_or_paypal_backend_rejected_after_sms" or result_reason in {
            "paypal_signup_not_advanced_after_sms",
            "paypal_card_rejected",
            "card_or_paypal_backend_rejected_after_sms",
        }:
            return True, "card_rejected_retry_with_new_card"
    if summary_status == "protocol_failed":
        return True, "protocol_failed"
    return True, summary_status or "retryable_failure"


def short_pool_reason(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def detailed_email_pool_reason(
    summary_status: str,
    summary: dict[str, Any],
    payment_attempts: list[dict[str, Any]],
    base_reason: str,
) -> str:
    if summary_status.startswith("success"):
        return "success"
    parts: list[str] = []
    for value in (base_reason, summary.get("reason")):
        text = short_pool_reason(value)
        if text and text not in parts:
            parts.append(text)
    if summary_status == "protocol_failed":
        protocol = summary.get("protocol") if isinstance(summary.get("protocol"), dict) else {}
        events = protocol.get("events") if isinstance(protocol.get("events"), list) else []
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            reason = short_pool_reason(event.get("reason") or event.get("error") or event.get("stage"))
            exc = short_pool_reason(event.get("exceptionType"))
            stage = short_pool_reason(event.get("stage"))
            detail = reason
            if stage and stage not in detail:
                detail = f"{stage}: {detail}" if detail else stage
            if exc and exc not in detail:
                detail = f"{detail} ({exc})" if detail else exc
            if detail:
                parts.append(detail)
                break
    elif summary_status == "payment_failed":
        for attempt in reversed(payment_attempts):
            if not isinstance(attempt, dict):
                continue
            result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
            detail_items = [
                short_pool_reason(attempt.get("retryReason")),
                short_pool_reason((result or {}).get("reason")),
                short_pool_reason((result or {}).get("error")),
            ]
            detail = "; ".join(item for item in detail_items if item)
            if detail:
                attempt_id = attempt.get("attempt")
                parts.append(f"attempt {attempt_id}: {detail}" if attempt_id else detail)
                break
    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return short_pool_reason(" | ".join(unique) or summary_status)


def protocol_summary_indicates_already_paid(summary: dict[str, Any]) -> bool:
    protocol = summary.get("protocol") if isinstance(summary.get("protocol"), dict) else {}
    candidates: list[Any] = [summary.get("reason"), summary.get("error")]
    for event in protocol.get("events") if isinstance(protocol.get("events"), list) else []:
        if isinstance(event, dict):
            candidates.extend((event.get("reason"), event.get("error"), event.get("stage"), event.get("detail")))
    text = " ".join(short_pool_reason(value, limit=2000) for value in candidates if value)
    return bool(re.search(r"\balready[_ ]paid\b|user is already paid", text, re.I))


def should_consume_card_pool(final_status: str, payment_attempts: list[dict[str, Any]]) -> bool:
    if final_status.startswith("success"):
        return True
    card_result_reasons = {
        "paypal_blocked",
        "paypal_signup_not_advanced_after_sms",
        "paypal_card_rejected",
        "card_or_paypal_backend_rejected_after_sms",
    }
    for attempt in payment_attempts or []:
        if not isinstance(attempt, dict):
            continue
        result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
        reason = str((result or {}).get("reason") or attempt.get("retryReason") or "")
        if (result or {}).get("status") == "success" or reason in card_result_reasons:
            return True
        log_path = Path(str(attempt.get("log") or ""))
        if log_path and log_path.exists():
            tail = read_tail(log_path, max_chars=20000)
            if re.search(r"field cardNumber: ok|Agree & Create Account clicked|\\[sms\\] code received|final confirmation", tail, re.I):
                return True
    return False


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _clean_json(value: Any) -> Any:
    if isinstance(value, list):
        cleaned = [_clean_json(item) for item in value]
        return [item for item in cleaned if item is not None]
    if isinstance(value, dict):
        cleaned = {key: _clean_json(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item is not None}
    if value in (None, ""):
        return None
    return value


def _base64url_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt_payload(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") < 1:
        return {}
    try:
        part = token.split(".", 2)[1]
        padded = part + ("=" * (-len(part) % 4))
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _to_epoch(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000) if number > 1e11 else int(number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if re.fullmatch(r"\d+(\.\d+)?", text):
            number = float(text)
            return int(number / 1000) if number > 1e11 else int(number)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def _to_iso(value: Any) -> str:
    epoch = _to_epoch(value)
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _expires_in(expires: str) -> int | None:
    epoch = _to_epoch(expires)
    if not epoch:
        return None
    return max(0, epoch - int(time.time()))


def _jwt_exp_to_iso(payload: dict[str, Any]) -> str:
    value = payload.get("exp")
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return _to_iso(number if number > 1e9 else number * 1000)


def _synthetic_id_token(email: str, account_id: str, plan_type: str, user_id: str, expires: str) -> str:
    if not account_id:
        return ""
    now = int(time.time())
    auth = {"chatgpt_account_id": account_id}
    if plan_type:
        auth["chatgpt_plan_type"] = plan_type
    if user_id:
        auth["chatgpt_user_id"] = user_id
        auth["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now,
        "ep": _to_epoch(expires) or now + 7776000,
        "https://api.openai.com/auth": auth,
    }
    if email:
        payload["email"] = email
    return f"{_base64url_json({'alg': 'none', 'typ': 'JWT', 'cpa_synthetic': True})}.{_base64url_json(payload)}."


def _find_session_records(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack: list[Any] = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            ident = id(item)
            if ident in seen:
                continue
            seen.add(ident)
            token = _first(
                item.get("accessToken"),
                item.get("access_token"),
                item.get("token", {}).get("accessToken") if isinstance(item.get("token"), dict) else None,
                item.get("credentials", {}).get("access_token") if isinstance(item.get("credentials"), dict) else None,
            )
            has_identity = isinstance(item.get("user"), dict) or bool(_first(item.get("email"), item.get("name")))
            if token and has_identity:
                found.append(item)
                continue
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def convert_web_session_record(record: dict[str, Any], *, source: str, output_format: str) -> dict[str, Any]:
    token_obj = record.get("token") if isinstance(record.get("token"), dict) else {}
    credentials = record.get("credentials") if isinstance(record.get("credentials"), dict) else {}
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    account = record.get("account") if isinstance(record.get("account"), dict) else {}

    access_token = _first(record.get("accessToken"), record.get("access_token"), token_obj.get("accessToken"), credentials.get("access_token"))
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("session JSON missing accessToken")
    session_token = _first(record.get("sessionToken"), record.get("session_token"), token_obj.get("sessionToken"))
    refresh_token = _first(record.get("refreshToken"), record.get("refresh_token"), token_obj.get("refreshToken"))
    id_token = _first(record.get("idToken"), record.get("id_token"), token_obj.get("idToken"))

    access_payload = _jwt_payload(access_token)
    id_payload = _jwt_payload(id_token)
    access_auth = access_payload.get("https://api.openai.com/auth") if isinstance(access_payload.get("https://api.openai.com/auth"), dict) else {}
    id_auth = id_payload.get("https://api.openai.com/auth") if isinstance(id_payload.get("https://api.openai.com/auth"), dict) else {}

    expires = _first(_jwt_exp_to_iso(access_payload), _to_iso(record.get("expires")), _to_iso(record.get("expired")))
    email = _first(user.get("email"), record.get("email"), credentials.get("email"), id_payload.get("email"), access_payload.get("email"))
    account_id = _first(account.get("id"), record.get("account_id"), access_auth.get("chatgpt_account_id"), id_auth.get("chatgpt_account_id"))
    user_id = _first(user.get("id"), access_auth.get("chatgpt_user_id"), id_auth.get("chatgpt_user_id"))
    plan_type = _first(account.get("planType"), account.get("plan_type"), access_auth.get("chatgpt_plan_type"), id_auth.get("chatgpt_plan_type"))
    name = str(_first(email, source, "Account"))
    generated_id_token = id_token or _synthetic_id_token(str(email or ""), str(account_id or ""), str(plan_type or ""), str(user_id or ""), str(expires or ""))
    last_refresh = datetime.now(timezone.utc).isoformat()

    cpa = _clean_json(
        {
            "type": "codex",
            "account_id": account_id,
            "email": email,
            "name": name,
            "plan_type": plan_type,
            "id_token": generated_id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session_token": session_token,
            "last_refresh": last_refresh,
            "expired": expires,
        }
    )
    cockpit = _clean_json(
        {
            "type": "codex",
            "id_token": generated_id_token,
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "account_id": account_id,
            "last_refresh": last_refresh,
            "email": email,
            "expired": expires,
        }
    )
    sub2api = _clean_json(
        {
            "name": name,
            "platform": "openai",
            "type": "oauth",
            "concurrency": 10,
            "priority": 1,
            "credentials": {
                "access_token": access_token,
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "email": email,
                "expires_at": expires,
                "expires_in": _expires_in(str(expires or "")),
                "plan_type": plan_type,
            },
            "extra": {
                "email": email,
                "name": name,
                "auth_provider": record.get("authProvider"),
                "source": "chatgpt_web_session",
                "last_refresh": last_refresh,
            },
        }
    )
    if output_format == "cpa":
        return cpa if isinstance(cpa, dict) else {}
    if output_format == "cockpit":
        return cockpit if isinstance(cockpit, dict) else {}
    if output_format == "sub2api":
        return {"exported_at": last_refresh, "proxies": [], "accounts": [sub2api]}
    if output_format == "raw":
        return record
    raise ValueError(f"unsupported web session output format: {output_format}")


def convert_web_session_json(source_path: Path, output_path: Path, *, output_format: str = "cpa") -> dict[str, Any]:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    records = _find_session_records(payload)
    if not records and isinstance(payload, dict):
        records = [payload]
    if not records:
        raise ValueError("no ChatGPT web session record found")
    converted = convert_web_session_record(records[0], source=str(source_path), output_format=output_format)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "recordCount": len(records),
        "hasAccessToken": has_access_token(output_path),
        "outputPath": str(output_path),
        "outputFormat": output_format,
    }


def classify_payment_retry(reason_text: str, payload: dict[str, Any], rc: int) -> tuple[bool, str]:
    text = " ".join(
        [
            reason_text or "",
            str(payload.get("reason") or ""),
            str(payload.get("error") or ""),
            str(payload.get("status") or ""),
        ]
    )
    if re.search(r"Stripe amount check failed", text, re.I):
        return False, "stripe_amount_check_failed"
    if re.search(r"DataDome returned t=bv|blocked verdict|rotate proxy/IP", text, re.I):
        return False, "datadome_blocked_verdict"
    if re.search(r"Stripe PayPal payment method was not found or could not be selected|PayPal option missing", text, re.I):
        return True, "stripe_paypal_method_not_found"
    if re.search(r"paypal_hermes_r_error|Hermes fallback reason=R_ERROR", text, re.I):
        return True, "paypal_hermes_r_error"
    retry_patterns = [
        r"amount text not found",
        r"PayPal signup form did not become ready",
        r"PayPal agreements/approve stalled before signup",
        r"PageDisconnectedError",
        r"连接已断开",
        r"Connection to remote host was lost",
        r"WebSocket 接收错误",
        r"proxy server is refusing connections",
        r"TimeoutError",
    ]
    if payload.get("status") == "success":
        return False, "success"
    if payload.get("reason") == "paypal_signup_not_advanced_after_sms":
        return False, "card_or_paypal_backend_rejected_after_sms"
    for pattern in retry_patterns:
        if re.search(pattern, text, re.I):
            return True, pattern
    return (rc != 0 and not payload), "nonzero_without_result"


def read_tail(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace")
    return data[-max_chars:]


def payment_profile_paths_from_log(path: Path) -> list[str]:
    text = read_tail(path, max_chars=30000)
    paths: list[str] = []
    for match in re.finditer(r"(?:profile=|userdir\s+)(/[^\s]+/ruyipage/profiles/ruyi_[^\s]+)", text):
        profile = match.group(1).rstrip("'\",")
        if profile not in paths:
            paths.append(profile)
    return paths


def terminate_profile_browsers(profile_paths: list[str]) -> list[int]:
    killed: list[int] = []
    proc_root = Path("/proc")
    if not profile_paths or not proc_root.exists():
        return killed
    self_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        if "firefox-fingerprintBrowser" not in cmd:
            continue
        if not any(profile in cmd for profile in profile_paths):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    if killed:
        time.sleep(0.8)
    for pid in list(killed):
        proc = proc_root / str(pid)
        if not proc.exists():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return killed


def close_retry_payment_browser(log_path: Path) -> None:
    profiles = payment_profile_paths_from_log(log_path)
    killed = terminate_profile_browsers(profiles)
    if killed:
        log(f"[payment] closed retry browser pids={killed}")


def build_protocol_cmd(args: argparse.Namespace, protocol_out: Path, trace_dir: Path) -> tuple[list[str], Path]:
    project = Path(args.protocol_project).resolve()
    bin_path = Path(args.protocol_bin) if args.protocol_bin else project / ".venv" / "bin" / "gpt-trial"
    if bin_path.exists():
        cmd = [str(bin_path), "run"]
    else:
        cmd = [sys.executable, "-m", "gpt_trial_protocol.cli", "run"]

    if args.generate_email:
        cmd.append("--generate-email")
        cmd.extend(["--generated-email-prefix", args.generated_email_prefix])
    if args.email:
        for email in args.email:
            cmd.extend(["--email", email])
    if args.emails_file:
        cmd.extend(["--emails-file", str(Path(args.emails_file).resolve())])
    if args.login_existing:
        cmd.append("--login-existing")
    protocol_proxy = "" if args.no_protocol_proxy else normalize_optional_proxy(args.protocol_proxy)
    if protocol_proxy:
        cmd.extend(["--proxy", protocol_proxy])
    cmd.extend(
        [
            "--email-type",
            args.email_type,
            "--email-code-provider",
            args.email_code_provider,
            "--checkout-country",
            args.checkout_country,
            "--checkout-currency",
            args.checkout_currency,
            "--timeout",
            str(args.protocol_timeout),
            "--code-timeout",
            str(args.email_code_timeout),
            "--backend",
            args.protocol_backend,
            "--out",
            str(protocol_out),
            "--trace-dir",
            str(trace_dir),
        ]
    )
    if args.email_code_base_url:
        cmd.extend(["--email-code-base-url", args.email_code_base_url])
    if args.no_protocol_trace:
        cmd.append("--no-trace")
    if args.trace_sensitive:
        cmd.append("--trace-sensitive")
    if args.enable_session_json:
        cmd.extend(["--session-output-dir", str(protocol_out.parent / "protocol_sessions")])
    for extra in args.protocol_extra_arg or []:
        cmd.append(extra)
    return cmd, project


def build_payment_cmd(
    args: argparse.Namespace,
    checkout_url: str,
    card_json: Path,
    payment_result: Path,
    payment_proxy: str | None = None,
    payment_proxy_chain: bool = False,
) -> tuple[list[str], Path]:
    project = Path(args.payment_project).resolve()
    py = Path(args.payment_python) if args.payment_python else project / ".venv" / "bin" / "python"
    script = Path(args.payment_script) if args.payment_script else project / "ruyi_paypal_flow.py"
    address_json = Path(args.address_json).resolve()
    sms_line = normalize_sms_line(args.sms_line)
    cmd = [
        str(py),
        str(script),
        "--start-url",
        checkout_url,
        "--card-json",
        str(card_json.resolve()),
        "--address-json",
        str(address_json),
        "--sms-line",
        sms_line,
        "--result-json",
        str(payment_result),
    ]
    if args.keep_browser_open:
        cmd.append("--keep-browser-open")
    if args.payment_headless:
        cmd.append("--headless")
    if int(args.payment_browser_slots) > 1:
        # BiDi remote debugging is sufficient for ruyi. In concurrent payment
        # slots, dropping Marionette removes one extra startup channel that has
        # caused intermittent Firefox connect failures.
        cmd.append("--disable-marionette")
        # IPv6 enrichment is useful but not decisive for checkout. Skipping it
        # in concurrent slots removes one serialized network probe per browser.
        cmd.append("--no-fingerprint-ipv6")
    if args.no_captcha_handling:
        cmd.append("--no-captcha-handling")
    selected_proxy = args.payment_proxy if payment_proxy is None else payment_proxy
    if str(selected_proxy or "").strip():
        if payment_proxy_chain:
            cmd.extend(["--proxy-chain-upstream", str(selected_proxy).strip(), "--proxy-chain-via", "direct"])
        else:
            cmd.extend(["--proxy", str(selected_proxy).strip()])
    for extra in args.payment_extra_arg or []:
        cmd.append(extra)
    return cmd, project


def build_getrt_cmd(args: argparse.Namespace, email: str, out_path: Path) -> tuple[list[str], Path]:
    script = Path(args.getrt_script).resolve()
    py = Path(args.getrt_python).resolve() if args.getrt_python else Path(args.protocol_project).resolve() / ".venv" / "bin" / "python"
    protocol_project = Path(args.protocol_project).resolve()
    cmd = [
        str(py),
        str(script),
        "--email",
        email,
        "--email-type",
        args.getrt_email_type or args.email_type,
        "--email-code-provider",
        args.getrt_email_code_provider or args.email_code_provider,
        "--protocol-project",
        str(protocol_project),
        "--timeout",
        str(args.getrt_timeout),
        "--code-timeout",
        str(args.getrt_code_timeout),
        "--backend",
        args.getrt_backend,
        "--output-format",
        args.getrt_output_format,
        "--plan-type",
        args.getrt_plan_type,
        "--name-prefix",
        args.getrt_name_prefix,
        "--concurrency",
        str(args.getrt_concurrency),
        "--priority",
        str(args.getrt_priority),
        "--out",
        str(out_path),
    ]
    if args.getrt_account_name:
        cmd.extend(["--account-name", args.getrt_account_name])
    if args.getrt_proxy_url:
        cmd.extend(["--proxy-url", args.getrt_proxy_url])
    if args.getrt_proxy_strict:
        cmd.append("--proxy-strict")
    if args.getrt_prefix:
        cmd.extend(["--prefix", args.getrt_prefix])
    if args.getrt_email_code_base_url or args.email_code_base_url:
        cmd.extend(["--email-code-base-url", args.getrt_email_code_base_url or args.email_code_base_url])
    getrt_proxy = "" if args.no_getrt_proxy else normalize_optional_proxy(args.getrt_proxy)
    if getrt_proxy:
        cmd.extend(["--proxy", getrt_proxy])
    else:
        cmd.append("--no-proxy")
    if args.getrt_trace_sensitive:
        cmd.append("--trace-sensitive")
    if args.getrt_prelogin_chatgpt:
        cmd.append("--prelogin-chatgpt")
    if args.enable_getrt_add_phone:
        phone_line = args.getrt_phone_line or args.sms_line
        if phone_line:
            cmd.extend(["--phone-line", phone_line])
        if args.getrt_phone_number:
            cmd.extend(["--phone-number", args.getrt_phone_number])
        if args.getrt_phone_otp_api:
            cmd.extend(["--phone-otp-api", args.getrt_phone_otp_api])
        if args.getrt_phone_otp_cmd:
            cmd.extend(["--phone-otp-cmd", args.getrt_phone_otp_cmd])
        if args.getrt_phone_otp:
            cmd.extend(["--phone-otp", args.getrt_phone_otp])
        cmd.extend(["--phone-otp-timeout", str(args.getrt_phone_otp_timeout)])
    for extra in args.getrt_extra_arg or []:
        cmd.append(extra)
    return cmd, ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run GPT protocol registration, then pay the generated checkout URL with ruyiPage.")
    parser.add_argument("--run-id", default="", help="Output run id; default current timestamp.")
    parser.add_argument("--out-dir", default="", help="Default: ./runtime/full_flow/<run-id>.")
    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument("--protocol-project", default=str(DEFAULT_PROTOCOL_PROJECT))
    parser.add_argument("--protocol-bin", default="")
    parser.add_argument(
        "--protocol-proxy",
        default=os.environ.get("GPT_TRIAL_PROXY", "http://127.0.0.1:7897"),
        help="Proxy for protocol registration. Use --no-protocol-proxy or values like direct/none/off to disable.",
    )
    parser.add_argument("--no-protocol-proxy", action="store_true", help="Run protocol stage without --proxy.")
    parser.add_argument("--protocol-proxy-chain-upstream", default=os.environ.get("PROTOCOL_PROXY_CHAIN_UPSTREAM", ""))
    parser.add_argument("--protocol-proxy-chain-via", default=os.environ.get("PROTOCOL_PROXY_CHAIN_VIA", "http://127.0.0.1:7897"))
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--emails-file", default="")
    parser.add_argument("--generate-email", action="store_true")
    parser.add_argument("--generated-email-prefix", default="lu")
    parser.add_argument("--login-existing", action="store_true")
    parser.add_argument("--email-type", choices=["auto", "icloud", "frimail"], default="frimail")
    parser.add_argument("--email-code-provider", choices=["auto", "agiunx", "frimail"], default="auto")
    parser.add_argument("--email-code-base-url", default="")
    parser.add_argument("--checkout-country", default="US")
    parser.add_argument("--checkout-currency", default="USD")
    parser.add_argument("--protocol-timeout", type=float, default=30.0)
    parser.add_argument("--email-code-timeout", type=float, default=90.0)
    parser.add_argument("--protocol-backend", choices=["curl_cffi", "httpx"], default="curl_cffi")
    parser.add_argument("--no-protocol-trace", action="store_true")
    parser.add_argument("--trace-sensitive", action="store_true")
    parser.add_argument("--protocol-extra-arg", action="append", default=[])

    parser.add_argument("--checkout-url", default="", help="Skip protocol stage and use this checkout URL.")
    parser.add_argument("--skip-payment", action="store_true")
    parser.add_argument("--payment-project", default=str(DEFAULT_PAYMENT_PROJECT))
    parser.add_argument("--payment-python", default="")
    parser.add_argument("--payment-script", default="")
    parser.add_argument("--card-json", default="")
    parser.add_argument("--card-line", default="", help="CARD|MM|YYYY|CVV or free-form digits.")
    parser.add_argument("--address-json", default=os.environ.get("ADDRESS_JSON", str(DEFAULT_ADDRESS_JSON)))
    parser.add_argument("--sms-line", default=os.environ.get("SMS_LINE", ""))
    parser.add_argument("--keep-browser-open", action="store_true")
    parser.add_argument("--payment-proxy", default=os.environ.get("PAYMENT_PROXY", ""), help="Primary payment proxy value used when --enable-payment-proxy is active.")
    parser.add_argument(
        "--enable-payment-proxy",
        action="store_true",
        default=parse_optional_bool(os.environ.get("PAYMENT_PROXY_ENABLED"), default=False),
        help="Start the payment stage through --payment-proxy. If --payment-proxy is empty, reuse --payment-temp-proxy.",
    )
    parser.add_argument("--disable-payment-proxy", action="store_true", help="Disable PAYMENT_PROXY/PAYMENT_PROXY_ENABLED for this run.")
    parser.add_argument(
        "--payment-proxy-use-bridge",
        action="store_true",
        default=parse_optional_bool(os.environ.get("PAYMENT_PROXY_USE_BRIDGE"), default=True),
        help="Use the local payment proxy bridge when --enable-payment-proxy is active.",
    )
    parser.add_argument("--no-payment-proxy-bridge", action="store_true", help="Pass --payment-proxy directly to ruyi instead of using the local bridge.")
    parser.add_argument("--payment-temp-proxy", default=os.environ.get("PAYMENT_TEMP_PROXY", ""), help="Fallback proxy used only after payment direct hits DataDome t=bv.")
    parser.add_argument(
        "--enable-payment-temp-proxy",
        action="store_true",
        default=parse_optional_bool(os.environ.get("PAYMENT_TEMP_PROXY_ENABLED"), default=False),
        help="Retry the current payment stage once through --payment-temp-proxy when DataDome returns t=bv.",
    )
    parser.add_argument("--disable-payment-temp-proxy", action="store_true", help="Disable PAYMENT_TEMP_PROXY_ENABLED for this run.")
    parser.add_argument(
        "--payment-headless",
        action="store_true",
        default=effective_payment_headless_default(os.environ.get("PAYMENT_HEADLESS")),
    )
    parser.add_argument("--no-captcha-handling", action="store_true")
    parser.add_argument("--payment-retries", type=int, default=2, help="Retry payment stage on transient browser/page failures.")
    parser.add_argument("--payment-retry-delay", type=float, default=3.0)
    parser.add_argument(
        "--success-account-file",
        default=os.environ.get("SUCCESS_ACCOUNT_FILE", ""),
        help="Override success-account txt. Default: frimail -> success_accounts.txt, icloud -> icsuccess_accounts.txt.",
    )
    parser.add_argument("--payment-extra-arg", action="append", default=[])
    parser.add_argument(
        "--payment-browser-slots",
        type=int,
        default=int(os.environ.get("FULL_FLOW_PAYMENT_BROWSER_SLOTS", "1")),
        help="Cross-process ruyi/Firefox payment browser slots. Default 1 serializes payment browsers for stable queue runs; set 0 to disable.",
    )
    parser.add_argument(
        "--payment-slot-lock-dir",
        default=os.environ.get("FULL_FLOW_PAYMENT_SLOT_LOCK_DIR", str(DEFAULT_PAYMENT_SLOT_LOCK_DIR)),
        help="Directory for payment browser slot lock files.",
    )
    parser.add_argument(
        "--payment-slot-wait-interval",
        type=float,
        default=float(os.environ.get("FULL_FLOW_PAYMENT_SLOT_WAIT_INTERVAL", "1")),
        help="Seconds between payment slot acquisition checks.",
    )
    parser.add_argument(
        "--pool-db",
        default=os.environ.get("FULL_FLOW_POOL_DB", ""),
        help=f"Optional SQLite resource pool DB for email/card/phone leasing. Default: {DEFAULT_POOL_DB}",
    )
    parser.add_argument("--pool-lease-seconds", type=int, default=7200)
    parser.add_argument("--pool-max-email-retries", type=int, default=int(os.environ.get("FULL_FLOW_POOL_MAX_EMAIL_RETRIES", "3")))
    parser.add_argument("--pool-phone-wait-seconds", type=float, default=float(os.environ.get("FULL_FLOW_POOL_PHONE_WAIT_SECONDS", "180")))
    parser.add_argument("--pool-phone-wait-interval", type=float, default=float(os.environ.get("FULL_FLOW_POOL_PHONE_WAIT_INTERVAL", "2")))
    parser.add_argument("--pool-worker-id", default="")
    parser.add_argument("--pool-seed-emails-file", default="")
    parser.add_argument("--pool-seed-cards-file", default="")
    parser.add_argument("--pool-seed-phones-file", default="")

    parser.add_argument("--enable-getrt", action="store_true", help="After protocol+payment success, export Codex OAuth refresh_token through getrt CLI.")
    parser.add_argument("--getrt-script", default=str(DEFAULT_GETRT_SCRIPT))
    parser.add_argument("--getrt-python", default="", help="Default: protocol project .venv/bin/python.")
    parser.add_argument("--getrt-proxy", default=os.environ.get("GETRT_PROXY", os.environ.get("GPT_TRIAL_PROXY", "http://127.0.0.1:7897")))
    parser.add_argument("--no-getrt-proxy", action="store_true")
    parser.add_argument("--getrt-proxy-chain-upstream", default=os.environ.get("GETRT_PROXY_CHAIN_UPSTREAM", os.environ.get("PROTOCOL_PROXY_CHAIN_UPSTREAM", "")))
    parser.add_argument("--getrt-proxy-chain-via", default=os.environ.get("GETRT_PROXY_CHAIN_VIA", os.environ.get("PROTOCOL_PROXY_CHAIN_VIA", "http://127.0.0.1:7897")))
    parser.add_argument("--getrt-email-type", choices=["", "auto", "icloud", "frimail"], default="", help="Default: --email-type.")
    parser.add_argument("--getrt-email-code-provider", choices=["", "auto", "agiunx", "frimail"], default="", help="Default: --email-code-provider.")
    parser.add_argument("--getrt-email-code-base-url", default="", help="Default: --email-code-base-url.")
    parser.add_argument("--getrt-timeout", type=float, default=30.0)
    parser.add_argument("--getrt-code-timeout", type=float, default=90.0)
    parser.add_argument("--getrt-backend", choices=["curl_cffi", "httpx"], default="curl_cffi")
    parser.add_argument("--getrt-output-format", "--getrt-format", "--getrt-json-format", choices=["cpa", "sub2", "sub2api", "codex", "raw"], default="cpa")
    parser.add_argument("--getrt-plan-type", default="plus")
    parser.add_argument("--getrt-name-prefix", default="[testplus]")
    parser.add_argument("--getrt-account-name", default="")
    parser.add_argument("--getrt-concurrency", type=int, default=10)
    parser.add_argument("--getrt-priority", type=int, default=1)
    parser.add_argument("--getrt-proxy-url", default="")
    parser.add_argument("--getrt-proxy-strict", action="store_true")
    parser.add_argument("--getrt-prefix", default="")
    parser.add_argument("--getrt-trace-sensitive", action="store_true")
    parser.add_argument("--getrt-prelogin-chatgpt", action="store_true")
    parser.add_argument("--enable-getrt-add-phone", action="store_true", help="Allow getrt to complete OAuth add-phone with a phone OTP source.")
    parser.add_argument("--getrt-phone-line", default="", help="PHONE|API_URL or PHONE----API_URL for getrt add-phone. Default under --enable-getrt-add-phone: --sms-line.")
    parser.add_argument("--getrt-phone-number", default="")
    parser.add_argument("--getrt-phone-otp-api", default="")
    parser.add_argument("--getrt-phone-otp-cmd", default="")
    parser.add_argument("--getrt-phone-otp", default="")
    parser.add_argument("--getrt-phone-otp-timeout", type=float, default=90.0)
    parser.add_argument("--getrt-extra-arg", action="append", default=[])
    parser.add_argument("--success-getrt-dir", default=str(DEFAULT_SUCCESS_GETRT_DIR))
    parser.add_argument(
        "--enable-session-json",
        "--enable-web-session-json",
        action="store_true",
        help="After protocol+payment success, convert ChatGPT web session JSON with token转换.html-compatible logic.",
    )
    parser.add_argument("--session-json-format", choices=["cpa", "sub2api", "cockpit", "raw"], default="cpa")
    parser.add_argument("--success-session-json-dir", default=str(DEFAULT_SUCCESS_SESSION_JSON_DIR))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.disable_payment_proxy:
        args.enable_payment_proxy = False
        args.payment_proxy = ""
    if args.no_payment_proxy_bridge:
        args.payment_proxy_use_bridge = False
    if args.disable_payment_temp_proxy:
        args.enable_payment_temp_proxy = False

    run_id = args.run_id or now_id()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else ROOT / "runtime" / "full_flow" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    load_env_file(Path(args.protocol_project) / ".env", env)
    load_env_file(Path(args.payment_project) / ".env", env)

    summary: dict[str, Any] = {
        "runId": run_id,
        "outDir": str(out_dir),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "protocol": {},
        "payment": {},
    }
    summary_path = out_dir / "summary.json"
    started = time.time()
    pool: FullFlowPool | None = None
    pool_email_item: PoolItem | None = None
    pool_card_item: PoolItem | None = None
    pool_phone_item: PoolItem | None = None
    pool_active = bool(args.pool_db) and not args.dry_run and not args.skip_payment
    if pool_active:
        pool = FullFlowPool(
            args.pool_db,
            lease_seconds=int(args.pool_lease_seconds),
            worker_id=args.pool_worker_id or run_id,
            max_email_retries=int(args.pool_max_email_retries),
        )
        for kind, seed_file in (
            ("email", args.pool_seed_emails_file),
            ("card", args.pool_seed_cards_file),
            ("phone", args.pool_seed_phones_file),
        ):
            if seed_file:
                pool.seed_file(kind, Path(seed_file))

    def finalize_pooled_resources(final_status: str, payment_attempts: list[dict[str, Any]] | None = None) -> None:
        nonlocal pool_email_item, pool_card_item, pool_phone_item
        if not pool:
            return
        attempts = payment_attempts or []
        if pool_email_item is not None:
            retryable, reason = should_retry_email_pool(final_status, attempts)
            reason = detailed_email_pool_reason(final_status, summary, attempts, reason)
            pool.finalize_email(
                pool_email_item.id,
                success=final_status.startswith("success"),
                retryable=retryable,
                reason=reason,
                run_id=run_id,
                card_value=pool_card_item.value if pool_card_item else "",
                phone_value=pool_phone_item.value if pool_phone_item else "",
            )
            pool_email_item = None
        if pool_card_item is not None:
            if should_consume_card_pool(final_status, attempts):
                pool.consume_card(pool_card_item.id)
            else:
                pool.release_item("card", pool_card_item.id, reason=f"unused:{final_status}")
            pool_card_item = None
        if pool_phone_item is not None:
            pool.release_phone(pool_phone_item.id)
            pool_phone_item = None

    def write_early_pool_failure(reason: str) -> int:
        summary["status"] = "payment_failed"
        summary["reason"] = reason
        summary["checkoutUrl"] = checkout_url
        finalize_pooled_resources(summary["status"])
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        summary["seconds"] = round(time.time() - started, 3)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"[summary] {summary_path}")
        return 2

    def retry_email_exclusions() -> tuple[list[str], list[str]]:
        if not pool_email_item:
            return [], []
        is_retry = pool_email_item.bucket == "retry" or pool_email_item.retry_count > 0
        if not is_retry:
            return [], []
        card_excludes = [pool_email_item.last_card_value] if pool_email_item.last_card_value else []
        phone_excludes = [pool_email_item.last_phone_value] if pool_email_item.last_phone_value else []
        if re.search(r"card_rejected|paypal_signup_not_advanced_after_sms|paypal_card_rejected|card_or_paypal_backend", pool_email_item.last_reason, re.I):
            # The failed variable was the card/funding path; keep phone reuse fast and only force a new card.
            phone_excludes = []
        if card_excludes or phone_excludes:
            log(
                "[pool] retry email will avoid previous resources "
                f"card={bool(card_excludes)} phone={bool(phone_excludes)}"
            )
        return card_excludes, phone_excludes

    def pool_has_non_excluded(kind: str, exclude_values: list[str]) -> bool:
        if not pool or not exclude_values:
            return True
        excluded = {str(value or "").strip() for value in exclude_values if str(value or "").strip()}
        if not excluded:
            return True
        for row in pool.list_items(kind, limit=1000):
            if str(row.get("value") or "").strip() not in excluded:
                return True
        return False

    def acquire_phone_with_wait(exclude_values: list[str] | None = None) -> PoolItem | None:
        if not pool:
            return None
        excludes = exclude_values or []
        if excludes and not pool_has_non_excluded("phone", excludes):
            log("[pool] retry email has no alternate phone resource available")
            return None
        deadline = time.time() + max(0.0, float(args.pool_phone_wait_seconds))
        interval = max(0.5, float(args.pool_phone_wait_interval))
        logged_wait = False
        while True:
            item = pool.acquire_phone(exclude_values=exclude_values)
            if item:
                if logged_wait:
                    log(f"[pool] acquired phone id={item.id}")
                return item
            if time.time() >= deadline:
                return None
            if not logged_wait:
                log("[pool] phone pool is empty; waiting for reusable phone release")
                logged_wait = True
            time.sleep(min(interval, max(0.0, deadline - time.time())))

    checkout_url = args.checkout_url
    protocol_email = str(args.email[0]).strip() if args.checkout_url and args.email else ""
    protocol_session_path = ""
    if not checkout_url:
        if not (args.email or args.emails_file or args.generate_email or pool):
            parser.error("provide --generate-email, --email, --emails-file, or --checkout-url")
        if pool and not (args.email or args.emails_file or args.generate_email):
            pool_email_item = pool.acquire_email()
            if not pool_email_item:
                parser.error("email pool is empty")
            args.email = [pool_email_item.value]
            protocol_email = pool_email_item.value
        protocol_out = out_dir / "protocol_results.jsonl"
        trace_dir = out_dir / "protocol_traces"
        protocol_bridge = None
        original_protocol_proxy = args.protocol_proxy
        try:
            if not args.dry_run and not args.no_protocol_proxy:
                protocol_bridge = start_proxy_chain(args.protocol_proxy_chain_upstream, args.protocol_proxy_chain_via, "protocol")
                if protocol_bridge:
                    args.protocol_proxy = protocol_bridge.url
            protocol_cmd, protocol_cwd = build_protocol_cmd(args, protocol_out, trace_dir)
            rc, events, seconds = run_streamed(protocol_cmd, protocol_cwd, out_dir / "protocol.log", env, dry_run=args.dry_run)
        finally:
            args.protocol_proxy = original_protocol_proxy
            if protocol_bridge:
                protocol_bridge.stop()
        raw_checkout_url = raw_checkout_from_events(events) or raw_checkout_from_jsonl(protocol_out)
        checkout_url = checkout_from_events(events) or checkout_from_jsonl(protocol_out)
        protocol_email = email_from_events(events) or email_from_jsonl(protocol_out)
        protocol_session_path = session_output_from_events(events) or session_output_from_jsonl(protocol_out)
        summary["protocol"] = {
            "returnCode": rc,
            "seconds": round(seconds, 3),
            "out": str(protocol_out),
            "traceDir": str(trace_dir),
            "checkoutUrl": checkout_url,
            "rawCheckoutUrl": raw_checkout_url,
            "email": protocol_email,
            "sessionOutputPath": protocol_session_path,
            "events": events[-8:],
        }
        if rc != 0 and not checkout_url:
            if protocol_summary_indicates_already_paid(summary):
                protocol_email = protocol_email or (str(args.email[0]).strip() if args.email else "")
                success_account_file = success_account_file_for(args, protocol_email)
                append_success_account(success_account_file, protocol_email)
                summary["status"] = "success"
                summary["reason"] = "already_paid"
                summary["successAccountFile"] = str(success_account_file)
                summary["successEmail"] = protocol_email
                finalize_pooled_resources(summary["status"])
                summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
                summary["seconds"] = round(time.time() - started, 3)
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                log(f"[summary] {summary_path}")
                return 0
            summary["status"] = "protocol_failed"
            finalize_pooled_resources(summary["status"])
            summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
            summary["seconds"] = round(time.time() - started, 3)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return rc or 2
        if raw_checkout_url and not checkout_url:
            summary["status"] = "protocol_no_payment_checkout"
            summary["reason"] = "protocol returned a non-payment pay.openai.com URL"
            summary["checkoutUrl"] = ""
            summary["rawCheckoutUrl"] = raw_checkout_url
            finalize_pooled_resources(summary["status"])
            summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
            summary["seconds"] = round(time.time() - started, 3)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"[summary] {summary_path}")
            return 2

    if args.skip_payment or args.dry_run:
        summary["status"] = "checkout_ready" if checkout_url else "dry_run"
        summary["checkoutUrl"] = checkout_url
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        summary["seconds"] = round(time.time() - started, 3)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"[summary] {summary_path}")
        return 0

    if not checkout_url:
        parser.error("protocol stage did not produce hosted /c/pay checkoutUrl")
    if not is_payment_checkout_url(checkout_url):
        parser.error(f"checkout URL is not a hosted payment URL: {checkout_url}")
    retry_card_excludes, retry_phone_excludes = retry_email_exclusions()

    if not args.sms_line and pool:
        pool_phone_item = acquire_phone_with_wait(retry_phone_excludes)
        if not pool_phone_item:
            reason = "phone_pool_no_alternate_for_retry_email" if retry_phone_excludes else "phone_pool_empty"
            return write_early_pool_failure(reason)
        args.sms_line = pool_phone_item.value
    if not args.sms_line:
        parser.error("--sms-line is required for payment")
    if args.card_json:
        card_json = Path(args.card_json)
    elif args.card_line:
        card_json = write_card_json(args.card_line, out_dir)
    elif pool:
        if retry_card_excludes and not pool_has_non_excluded("card", retry_card_excludes):
            return write_early_pool_failure("card_pool_no_alternate_for_retry_email")
        pool_card_item = pool.acquire_card(exclude_values=retry_card_excludes)
        if not pool_card_item:
            reason = "card_pool_no_alternate_for_retry_email" if retry_card_excludes else "card_pool_empty"
            return write_early_pool_failure(reason)
        card_json = write_card_json(pool_card_item.value, out_dir)
    else:
        parser.error("--card-json or --card-line is required for payment")

    attempts: list[dict[str, Any]] = []
    payment_payload: dict[str, Any] = {}
    payment_events: list[dict[str, Any]] = []
    payment_seconds = 0.0
    rc = 1
    payment_result = out_dir / "payment_result.json"
    normal_max_attempts = max(1, int(args.payment_retries) + 1)
    payment_temp_proxy = normalize_optional_proxy(args.payment_temp_proxy)
    payment_primary_proxy = normalize_optional_proxy(args.payment_proxy)
    if args.enable_payment_proxy and not payment_primary_proxy:
        payment_primary_proxy = payment_temp_proxy
    payment_primary_proxy_active = bool(args.enable_payment_proxy and payment_primary_proxy)
    payment_temp_proxy_available = bool(args.enable_payment_temp_proxy and payment_temp_proxy)
    payment_temp_proxy_active = False
    payment_temp_proxy_used = False
    attempt = 1
    payment_slot_lease = acquire_payment_slot(
        Path(args.payment_slot_lock_dir).resolve(),
        int(args.payment_browser_slots),
        owner=run_id,
        wait_interval=float(args.payment_slot_wait_interval),
    )
    payment_env = payment_slot_env(env, payment_slot_lease)
    try:
        while True:
            max_attempts_label = normal_max_attempts + (1 if payment_temp_proxy_active else 0)
            suffix = "" if attempt == 1 else f"_attempt{attempt}"
            attempt_result = out_dir / f"payment_result{suffix}.json"
            attempt_log = out_dir / f"payment{suffix}.log"
            if attempt_result.exists():
                attempt_result.unlink()
            if payment_temp_proxy_active:
                attempt_proxy = payment_temp_proxy
                attempt_proxy_mode = "temp_proxy"
                attempt_proxy_chain = True
            elif payment_primary_proxy_active:
                attempt_proxy = payment_primary_proxy
                attempt_proxy_mode = "configured_proxy"
                attempt_proxy_chain = bool(args.payment_proxy_use_bridge)
            else:
                attempt_proxy = ""
                attempt_proxy_mode = "direct"
                attempt_proxy_chain = False
            if attempt_proxy:
                attempt_proxy, proxy_session_rotated = randomize_proxy_session(attempt_proxy)
                if proxy_session_rotated:
                    log(f"[payment] rotated proxy session for {attempt_proxy_mode}")
            log(f"[payment] attempt {attempt}/{max_attempts_label} mode={attempt_proxy_mode}")
            payment_cmd, payment_cwd = build_payment_cmd(
                args,
                checkout_url,
                card_json,
                attempt_result,
                payment_proxy=attempt_proxy,
                payment_proxy_chain=attempt_proxy_chain,
            )
            attempt_rc, attempt_events, attempt_seconds = run_streamed(payment_cmd, payment_cwd, attempt_log, payment_env, dry_run=False)
            payment_seconds += attempt_seconds
            attempt_payload: dict[str, Any] = {}
            if attempt_result.exists():
                try:
                    attempt_payload = json.loads(attempt_result.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    attempt_payload = {}
            retry, retry_reason = classify_payment_retry(read_tail(attempt_log), attempt_payload, attempt_rc)
            datadome_blocked = retry_reason == "datadome_blocked_verdict"
            switch_to_temp_proxy = bool(
                datadome_blocked
                and payment_temp_proxy_available
                and not payment_temp_proxy_active
                and not payment_temp_proxy_used
                and attempt_proxy_mode == "direct"
            )
            regular_retry = bool(retry and attempt < normal_max_attempts and not payment_temp_proxy_active)
            will_retry = bool(switch_to_temp_proxy or regular_retry)
            recorded_retry_reason = "datadome_blocked_verdict_temp_proxy" if switch_to_temp_proxy else retry_reason
            attempts.append(
                {
                    "attempt": attempt,
                    "proxyMode": attempt_proxy_mode,
                    "returnCode": attempt_rc,
                    "seconds": round(attempt_seconds, 3),
                    "log": str(attempt_log),
                    "resultJson": str(attempt_result),
                    "result": attempt_payload,
                    "retry": will_retry,
                    "retryReason": recorded_retry_reason,
                }
            )
            rc = attempt_rc
            payment_payload = attempt_payload
            payment_events = attempt_events
            payment_result = attempt_result
            if attempt_payload.get("status") == "success":
                break
            if switch_to_temp_proxy:
                if args.keep_browser_open:
                    close_retry_payment_browser(attempt_log)
                payment_temp_proxy_active = True
                payment_temp_proxy_used = True
                attempt += 1
                log("[payment] DataDome t=bv on non-temp payment; retrying once with payment temp proxy")
                time.sleep(max(0.0, float(args.payment_retry_delay)))
                continue
            if datadome_blocked:
                log("[payment] DataDome t=bv; payment temp proxy is disabled or already used")
                break
            if not regular_retry:
                break
            log(f"[payment] retrying after transient failure: {retry_reason}")
            if args.keep_browser_open:
                close_retry_payment_browser(attempt_log)
            attempt += 1
            time.sleep(max(0.0, float(args.payment_retry_delay)))
    finally:
        if payment_slot_lease:
            payment_slot_lease.release()

    summary["checkoutUrl"] = checkout_url
    summary["payment"] = {
        "returnCode": rc,
        "seconds": round(payment_seconds, 3),
        "resultJson": str(payment_result),
        "result": payment_payload,
        "attempts": attempts,
        "events": payment_events[-4:],
    }
    summary["status"] = "success" if payment_payload.get("status") == "success" else "payment_failed"
    if summary["status"] == "success":
        success_account_file = success_account_file_for(args, protocol_email)
        append_success_account(success_account_file, protocol_email)
        summary["successAccountFile"] = str(success_account_file)
        summary["successEmail"] = protocol_email
        if args.enable_session_json:
            session_json_result = out_dir / "web_session_result.json"
            session_archive = None
            try:
                if not protocol_session_path:
                    raise FileNotFoundError("protocol session JSON was not produced")
                source_session = Path(protocol_session_path)
                converted = convert_web_session_json(source_session, session_json_result, output_format=args.session_json_format)
                session_archive = archive_getrt_result(session_json_result, Path(args.success_session_json_dir).resolve(), protocol_email)
                summary["webSessionJson"] = {
                    "status": "success",
                    "outputFormat": args.session_json_format,
                    "sourcePath": str(source_session),
                    "outputPath": str(session_json_result),
                    "archivePath": str(session_archive) if session_archive else "",
                    "hasAccessToken": bool(converted.get("hasAccessToken")),
                    "recordCount": converted.get("recordCount", 0),
                }
            except Exception as exc:
                summary["webSessionJson"] = {
                    "status": "failed",
                    "outputFormat": args.session_json_format,
                    "sourcePath": protocol_session_path,
                    "outputPath": str(session_json_result),
                    "archivePath": "",
                    "reason": str(exc),
                    "exceptionType": type(exc).__name__,
                }
                summary["status"] = "success_session_json_failed"
        if args.enable_getrt:
            if not protocol_email:
                summary["getrt"] = {
                    "status": "skipped",
                    "reason": "missing_protocol_email",
                    "outputPath": str(out_dir / "getrt_result.json"),
                    "hasRefreshToken": False,
                }
                summary["status"] = "success_getrt_skipped"
            else:
                getrt_result = out_dir / "getrt_result.json"
                getrt_log = out_dir / "getrt.log"
                getrt_bridge = None
                original_getrt_proxy = args.getrt_proxy
                try:
                    if not args.no_getrt_proxy:
                        getrt_bridge = start_proxy_chain(args.getrt_proxy_chain_upstream, args.getrt_proxy_chain_via, "getrt")
                        if getrt_bridge:
                            args.getrt_proxy = getrt_bridge.url
                    getrt_cmd, getrt_cwd = build_getrt_cmd(args, protocol_email, getrt_result)
                    getrt_rc, getrt_events, getrt_seconds = run_streamed_sanitized(getrt_cmd, getrt_cwd, getrt_log, env, dry_run=False)
                finally:
                    args.getrt_proxy = original_getrt_proxy
                    if getrt_bridge:
                        getrt_bridge.stop()
                missing_reason = getrt_missing_reason(getrt_result)
                requires_phone = getrt_requires_phone(getrt_result)
                access_ok = getrt_rc == 0 and has_access_token(getrt_result)
                refresh_ok = getrt_rc == 0 and has_refresh_token(getrt_result)
                acceptable_no_rt = getrt_rc == 0 and requires_phone
                archive_path = None
                if refresh_ok or acceptable_no_rt:
                    archive_path = archive_getrt_result(getrt_result, Path(args.success_getrt_dir).resolve(), protocol_email)
                summary["getrt"] = {
                    "status": "success" if refresh_ok else ("requires_phone" if acceptable_no_rt else "failed"),
                    "returnCode": getrt_rc,
                    "seconds": round(getrt_seconds, 3),
                    "outputFormat": args.getrt_output_format,
                    "outputPath": str(getrt_result),
                    "hasAccessToken": bool(access_ok),
                    "hasRefreshToken": bool(refresh_ok),
                    "refreshTokenMissingReason": missing_reason,
                    "requiresPhone": bool(requires_phone),
                    "archivePath": str(archive_path) if archive_path else "",
                    "log": str(getrt_log),
                    "events": getrt_events[-4:],
                }
                if not refresh_ok and not acceptable_no_rt:
                    summary["status"] = "success_getrt_failed"
    pool_summary = None
    if pool:
        pool_summary = {
            "db": str(pool.path),
            "workerId": pool.worker_id,
            "email": pool_item_summary(pool_email_item),
            "card": pool_item_summary(pool_card_item),
            "phone": pool_item_summary(pool_phone_item),
        }
    finalize_pooled_resources(summary.get("status", ""), attempts)
    if pool_summary is not None:
        pool_summary["snapshot"] = pool.snapshot()
        summary["pool"] = pool_summary
    summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
    summary["seconds"] = round(time.time() - started, 3)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"[summary] {summary_path}")
    if summary.get("status") == "success_getrt_failed":
        return 3
    if summary.get("status") != "success":
        return rc or 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
