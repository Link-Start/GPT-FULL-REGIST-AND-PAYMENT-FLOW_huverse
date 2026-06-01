#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RuyiPage Firefox backend for the OpenAI -> Stripe -> PayPal flow.

Sensitive input is done through ruyiPage element/actions APIs so the browser
emits trusted BiDi input events. The browser identity is configured through
ruyiPage's smart fingerprint helper by default.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import random
import re
import select
import signal
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ruyipage import FirefoxOptions, FirefoxPage, Keys
from ruyipage.errors import BiDiError

PROCESS_STARTED_AT = time.monotonic()


ROOT = Path(__file__).resolve().parent
DEFAULT_CARD_JSON = ROOT / "card_info.json"
DEFAULT_FIREFOX_PATH = str(
    ROOT.parent / "firefox-fingerprintBrowser" / "browser" / "firefox" / "firefox"
)
SYSTEM_PROXY_MARKERS = {"", "system", "system://", "win", "windows", "os"}
DIRECT_PROXY_MARKERS = {"direct", "none", "off", "no", "false"}
SMS_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
LUBAN_SMS_BASE_URL = "https://lubansms.com/v2/api"
LUBAN_JP_PAYPAL_SERVICE_ID = "729637"


class FlowFailed(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result.get("reason") or "flow_failed"))
        self.result = result


def classify_unhandled_failure(exc: BaseException) -> str:
    text = str(exc)
    if re.search(r"Stripe amount check failed", text, re.I):
        return "stripe_amount_check_failed"
    if re.search(r"LubanSMS getNumber failed", text, re.I):
        if re.search(r"NO_NUMBER", text, re.I):
            return "sms_provider_no_number"
        if re.search(r"balance|余额|insufficient", text, re.I):
            return "sms_provider_balance_low"
        return "sms_provider_error"
    if re.search(r"LubanSMS getSms failed", text, re.I):
        return "sms_provider_error"
    return "unexpected_exception"


def linux_profile_root() -> Path:
    """Return the Linux profile root used by Linux firefox-fingerprintBrowser."""
    env_root = os.environ.get("RUYI_PROFILE_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return ROOT / "profiles"


def disable_local_debug_proxy_env() -> None:
    """Avoid routing Firefox BiDi localhost traffic through shell HTTP proxies.

    The business/browser proxy is configured explicitly on FirefoxOptions.  We
    intentionally clear process-wide proxy envs so websocket/BiDi localhost
    traffic never gets sent to a proxy.
    """
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1"


def log(message: str) -> None:
    elapsed = time.monotonic() - PROCESS_STARTED_AT
    print(f"[t+{elapsed:06.1f}s] {message}", flush=True)


class FirefoxStartupLock:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "FirefoxStartupLock":
        if not self.path:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        log(f"[ruyi] waiting firefox startup lock: {self.path}")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} startedAt={time.time()}\n")
        self.handle.flush()
        log("[ruyi] acquired firefox startup lock")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.handle:
            return
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
        self.handle = None
        log("[ruyi] released firefox startup lock")


def firefox_startup_lock() -> FirefoxStartupLock:
    value = os.environ.get("RUYI_FIREFOX_STARTUP_LOCK", "").strip()
    if not value or value.lower() in DIRECT_PROXY_MARKERS:
        return FirefoxStartupLock(None)
    return FirefoxStartupLock(Path(value))


def kill_firefox_processes(*, profile_dir: Path | None = None, port: int | None = None) -> int:
    """Terminate Firefox processes tied to a failed launch attempt."""
    profile_text = str(profile_dir) if profile_dir else ""
    port_text = f"--remote-debugging-port={port}" if port else ""
    proc_root = Path("/proc")
    if not proc_root.exists():
        return 0
    killed = 0
    own_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "ignore")
        if "firefox" not in cmdline.lower():
            continue
        if profile_text and profile_text not in cmdline:
            continue
        if port_text and port_text not in cmdline:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except OSError:
            continue
    return killed


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_local_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def apply_standard_tracking_protection(opts: FirefoxOptions) -> None:
    """Force Firefox Enhanced Tracking Protection UI to Standard."""
    opts.set_pref("browser.contentblocking.category", "standard")
    opts.set_pref("privacy.trackingprotection.enabled", False)
    opts.set_pref("privacy.trackingprotection.pbmode.enabled", True)
    opts.set_pref("privacy.trackingprotection.emailtracking.enabled", False)


def random_gmail() -> str:
    timestamp = (time.time_ns() // 1_000) % 1_000_000
    return f"cudaffll{timestamp:06d}uusl@gmail.com"


def random_letters(length: int) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length)).capitalize()


def address_country(address: dict[str, Any]) -> str:
    return str(address.get("country") or address.get("countryCode") or "").strip().upper()


def is_japan_address(address: dict[str, Any]) -> bool:
    return address_country(address) in {"JP", "JPN", "JAPAN", "日本"}


def paypal_phone_for_country(phone: str, country: str) -> str:
    digits = re.sub(r"\D+", "", phone)
    if country.upper() in {"JP", "JPN", "JAPAN"}:
        # PayPal Japan renders a +81 country selector; the field expects the
        # national significant number without the +81 prefix.
        if digits.startswith("81") and len(digits) >= 11:
            return digits[2:]
        if digits.startswith("0") and len(digits) >= 10:
            return digits[1:]
    return digits[-10:]


def parse_window_size(value: str, default: tuple[int, int] = (1280, 900)) -> tuple[int, int]:
    text = (value or "").strip().lower()
    if not text:
        return default
    match = re.match(r"^\s*(\d{3,5})\s*[x,]\s*(\d{3,5})\s*$", text)
    if not match:
        return default
    width = max(900, min(2560, int(match.group(1))))
    height = max(650, min(1600, int(match.group(2))))
    return width, height


def http_get_json(url: str, *, timeout: int = 25) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected JSON response: {payload!r}")
    return payload


def build_luban_api_url(path: str, params: dict[str, str]) -> str:
    return f"{LUBAN_SMS_BASE_URL}/{path}?" + urllib.parse.urlencode(params)


def _float_option(value: str, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def acquire_luban_sms_number(spec: str) -> tuple[str, str]:
    """Acquire a LubanSMS number and return (phone, poll_api_url).

    Supported line:
      luban://jp-paypal?apikey=...               # uses the cheapest observed JP PayPal service
      luban://jp-paypal?apikey=...&service_id=...
      luban://jp-paypal?apikey=...&number_retries=10
    """
    parsed = urllib.parse.urlparse(spec)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    apikey = query.get("apikey") or os.environ.get("LUBAN_SMS_APIKEY", "").strip()
    service_id = query.get("service_id") or os.environ.get("LUBAN_SMS_SERVICE_ID", "").strip() or LUBAN_JP_PAYPAL_SERVICE_ID
    if not apikey:
        raise ValueError("LubanSMS requires apikey query parameter or LUBAN_SMS_APIKEY")
    try:
        number_retries = int(str(query.get("number_retries") or os.environ.get("LUBAN_SMS_NUMBER_RETRIES", "10")).strip())
    except (TypeError, ValueError):
        number_retries = 10
    number_retries = max(0, number_retries)
    number_interval = _float_option(
        query.get("number_interval") or os.environ.get("LUBAN_SMS_NUMBER_INTERVAL", "1"),
        1.0,
    )
    if number_interval <= 0:
        number_interval = 1.0
    payload: dict[str, Any] = {}
    for attempt in range(1, number_retries + 2):
        payload = http_get_json(build_luban_api_url("getNumber", {"apikey": apikey, "service_id": service_id}))
        if str(payload.get("code")) == "0":
            break
        msg = str(payload.get("msg") or payload.get("message") or "")
        if not re.search(r"NO_NUMBER", msg, re.I):
            raise RuntimeError(f"LubanSMS getNumber failed: {payload}")
        if attempt > number_retries:
            raise RuntimeError(f"LubanSMS getNumber failed after {attempt} NO_NUMBER attempts: {payload}")
        log(f"[sms] LubanSMS no JP PayPal number yet; retrying in {number_interval:g}s attempt={attempt}")
        time.sleep(number_interval)
    number = re.sub(r"\D+", "", str(payload.get("number") or ""))
    request_id = str(payload.get("request_id") or "").strip()
    if not number or not request_id:
        raise RuntimeError(f"LubanSMS getNumber missing number/request_id: {payload}")
    log(f"[sms] LubanSMS acquired JP PayPal number request_id={request_id} phone=***{number[-4:]}")
    poll_url = "luban://poll?" + urllib.parse.urlencode({"apikey": apikey, "request_id": request_id})
    return number, poll_url


def parse_sms_line(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if text.startswith("luban://"):
        return acquire_luban_sms_number(text)
    if "|" not in text:
        raise ValueError("sms-line must be PHONE|API_URL")
    phone, url = text.split("|", 1)
    phone = re.sub(r"\D+", "", phone)
    if not phone or not url.strip():
        raise ValueError("sms-line must contain phone and api url")
    url = url.strip()
    if url.startswith("http://a.62-us.com/"):
        url = "https://" + url[len("http://") :]
    return phone, url


def _as_proxy_url(value: str, default_scheme: str = "http") -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"{default_scheme}://{value}"


def proxy_is_system(value: str) -> bool:
    return (value or "").strip().lower() in SYSTEM_PROXY_MARKERS


def proxy_is_direct(value: str) -> bool:
    return (value or "").strip().lower() in DIRECT_PROXY_MARKERS


def proxy_display(proxy: dict[str, Any] | None) -> str:
    if not proxy:
        return "direct"
    return f"{proxy.get('scheme', 'http')}://{proxy.get('host')}:{proxy.get('port')}"


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_windows_proxy_server(proxy_server: str) -> str:
    """Pick one usable proxy URL from Windows Internet Settings ProxyServer."""
    proxy_server = (proxy_server or "").strip()
    if not proxy_server:
        return ""
    entries: dict[str, str] = {}
    for part in proxy_server.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, raw = part.split("=", 1)
            entries[key.strip().lower()] = raw.strip()
    if entries:
        if entries.get("https"):
            return _as_proxy_url(entries["https"], "http")
        if entries.get("http"):
            return _as_proxy_url(entries["http"], "http")
        if entries.get("socks"):
            return _as_proxy_url(entries["socks"], "socks5")
        return ""
    return _as_proxy_url(proxy_server, "http")


def powershell_path() -> str:
    for candidate in (
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "powershell.exe",
        "pwsh.exe",
        "pwsh",
    ):
        if Path(candidate).exists():
            return candidate
        try:
            import shutil

            found = shutil.which(candidate)
            if found:
                return found
        except Exception:
            pass
    return ""


def read_windows_system_proxy() -> tuple[str, str]:
    ps = powershell_path()
    if not ps:
        return "", "PowerShell not found"
    command = (
        "$p=Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'; "
        "[pscustomobject]@{ProxyEnable=$p.ProxyEnable;ProxyServer=$p.ProxyServer;AutoConfigURL=$p.AutoConfigURL} "
        "| ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        data = json.loads((proc.stdout or "").strip() or "{}")
    except Exception as exc:
        return "", f"Windows proxy query failed: {exc}"
    proxy_enable = str(data.get("ProxyEnable", "")).strip().lower() in {"1", "true"}
    proxy_url = parse_windows_proxy_server(str(data.get("ProxyServer") or "")) if proxy_enable else ""
    if proxy_url:
        return proxy_url, "Windows system proxy"
    if data.get("AutoConfigURL"):
        return "", "Windows PAC proxy is not directly usable from WSL; set RUYI_PROXY explicitly"
    return "", "Windows system proxy disabled"


def resolve_proxy(raw_value: str) -> tuple[str, str]:
    """Resolve CLI/env proxy mode to an explicit proxy URL for Linux Firefox."""
    raw_value = (raw_value or "").strip()
    if proxy_is_direct(raw_value):
        return "", "direct"
    if not proxy_is_system(raw_value):
        # Keep vendor suffixes like host:port:user:pass(socks) intact so
        # parse_proxy_url() can select the intended scheme.
        return raw_value, "explicit proxy"
    for env_name in ("RUYI_SYSTEM_PROXY", "SYSTEM_PROXY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return _as_proxy_url(value, "http"), f"env {env_name}"
    return read_windows_system_proxy()


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
        if suffix_scheme:
            scheme = suffix_scheme
        # Some proxy vendors return scheme://host:port:user:pass instead of URI auth form.
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
        raise ValueError(
            "proxy URL must be like http://user:pass@host:port, socks5://host:port, host:port:user:pass, or socks5://host:port:user:pass"
        )
    scheme = parsed.scheme.lower()
    if scheme == "socks":
        scheme = "socks5"
    browser_scheme = "socks5" if scheme == "socks5h" else scheme
    request_scheme = "socks5h" if browser_scheme == "socks5" else browser_scheme
    captcha_type = "http" if browser_scheme in {"http", "https"} else browser_scheme
    if captcha_type not in {"http", "socks4", "socks5"}:
        raise ValueError("proxy scheme must be http, https, socks4, socks5 or socks5h")
    return {
        "scheme": browser_scheme,
        "requestScheme": request_scheme,
        "browserProxy": f"{browser_scheme}://{parsed.hostname}:{int(parsed.port)}",
        "host": parsed.hostname,
        "port": int(parsed.port),
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "proxyType": captcha_type,
        "proxyAddress": parsed.hostname,
        "proxyPort": int(parsed.port),
        "proxyLogin": urllib.parse.unquote(parsed.username or ""),
        "proxyPassword": urllib.parse.unquote(parsed.password or ""),
    }


def preflight_proxy_tcp(proxy: dict[str, Any], timeout: float = 8.0) -> None:
    """Fail fast when the proxy gateway itself is unreachable."""
    host = proxy.get("host")
    port = int(proxy.get("port") or 0)
    if not host or not port:
        return
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise RuntimeError(f"proxy gateway is unreachable: {host}:{port} ({exc})") from exc


class ChainedHttpProxy:
    """Local HTTP proxy: browser -> localhost -> local parent proxy -> upstream proxy."""

    def __init__(self, upstream: dict[str, Any], parent: dict[str, Any] | None = None, host: str = "127.0.0.1") -> None:
        self.upstream = upstream
        self.parent = parent
        self.host = host
        self.port = find_free_port(18080, 19999)
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
        self._thread = threading.Thread(target=self._serve, name="ruyi-proxy-chain", daemon=True)
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


def normalize_firefox_path(value: str) -> str:
    value = (value or "").strip()
    if not value:
        if Path(DEFAULT_FIREFOX_PATH).exists():
            return DEFAULT_FIREFOX_PATH
        return "firefox"
    if sys.platform != "win32":
        match = re.match(r"^([A-Za-z]):\\(.*)$", value)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2).replace("\\", "/")
            value = f"/mnt/{drive}/{rest}"
    path = Path(value)
    if path.is_dir():
        firefox_bin = path / "firefox"
        firefox_exe = path / "firefox.exe"
        value = str(firefox_bin if firefox_bin.exists() else firefox_exe if firefox_exe.exists() else path)
    return value


def find_free_port(start: int = 24000, end: int = 32000) -> int:
    candidates = list(range(start, end + 1))
    random.shuffle(candidates)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free local port found in {start}-{end}")


def fetch_sms_code(api_url: str) -> tuple[str, str, str]:
    if api_url.startswith("manual://"):
        return "", "manual://", ""
    if api_url.startswith("luban://"):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(api_url).query, keep_blank_values=True))
        apikey = params.get("apikey") or os.environ.get("LUBAN_SMS_APIKEY", "").strip()
        request_id = params.get("request_id", "").strip()
        if not apikey or not request_id:
            raise ValueError("LubanSMS poll URL requires apikey and request_id")
        payload = http_get_json(build_luban_api_url("getSms", {"apikey": apikey, "request_id": request_id}), timeout=20)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if str(payload.get("code")) == "0" and payload.get("msg") == "success":
            code = str(payload.get("sms_code") or extract_sms_code(raw))
            return code, f"{code}|{raw}", raw
        if str(payload.get("code")) == "0" and payload.get("msg") == "wait":
            return "", raw, raw
        if payload.get("msg") == "wrong_status":
            return "", raw, raw
        raise RuntimeError(f"LubanSMS getSms failed: {payload}")
    if api_url.startswith("http://a.62-us.com/"):
        api_url = "https://" + api_url[len("http://") :]
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    code = extract_sms_code(raw)
    return code, f"{code}|{raw.strip()}", raw


def _json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_json_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_json_strings(item))
        return out
    return []


def extract_sms_code(raw: str) -> str:
    """Extract a PayPal OTP from provider response text.

    Prefer the SMS message body over metadata. Some providers append expiry
    timestamps or wrap messages in JSON, so scanning the whole raw response
    first can race on unrelated digits.
    """
    candidates: list[str] = []
    text = str(raw or "")
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("sms_code", "code", "otp"):
                if payload.get(key):
                    candidates.append(str(payload.get(key)))
        candidates.extend(_json_strings(payload))
    except Exception:
        pass
    parts = [part.strip() for part in text.strip().split("|")]
    if len(parts) >= 2:
        candidates.append(parts[1])
    candidates.append(text)
    candidates = sorted(
        [c for c in candidates if c],
        key=lambda s: 0 if re.search(r"paypal|security|code|verification|验证码", s, re.I) else 1,
    )
    for candidate in candidates:
        match = SMS_CODE_RE.search(candidate)
        if match:
            return match.group(1)
    return ""


def capture_sms_baseline(api_url: str) -> str:
    try:
        return fetch_sms_code(api_url)[1]
    except Exception:
        return ""


def wait_fresh_sms_code(api_url: str, baseline: str = "", timeout: int = 90, interval: float = 2.0, stop_if: Any = None) -> str:
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        if stop_if and stop_if():
            return ""
        try:
            code, signature, raw = fetch_sms_code(api_url)
            last_text = raw
            if code and signature != baseline:
                return code
        except Exception as exc:
            last_text = repr(exc)
        time.sleep(interval)
    raise TimeoutError(f"SMS code not found before timeout; last response: {last_text[:300]}")


class SmsCodePoller:
    """Background fresh-code poller.

    The previous project starts SMS polling immediately after PayPal submit.
    That hides receiver latency behind PayPal page transition time.  Keep this
    poller independent from browser state so it cannot block BiDi interaction.
    """

    def __init__(self, api_url: str, baseline: str = "", timeout: int = 90, interval: float = 1.0) -> None:
        self.api_url = api_url
        self.baseline = baseline
        self.timeout = timeout
        self.interval = interval
        self._event = threading.Event()
        self._stop = threading.Event()
        self._code = ""
        self._last_text = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        deadline = time.time() + self.timeout
        while time.time() < deadline and not self._stop.is_set():
            try:
                code, signature, raw = fetch_sms_code(self.api_url)
                self._last_text = raw
                if code and signature != self.baseline:
                    self._code = code
                    self._event.set()
                    return
            except Exception as exc:
                self._last_text = repr(exc)
            self._stop.wait(self.interval)
        self._event.set()

    def result(self, timeout: float = 0.0) -> str:
        self._event.wait(max(0.0, timeout))
        return self._code

    def stop(self) -> None:
        self._stop.set()


def _announce_sms_code(flow: "RuyiPayPalFlow", code: str) -> None:
    if code and not flow.sms_code_received:
        flow.sms_code_received = True
        log("[sms] code received")


def split_expiry(expiry: str) -> tuple[str, str]:
    parts = re.split(r"[/-]", str(expiry).strip())
    if len(parts) != 2:
        raise ValueError(f"unsupported expiry format: {expiry!r}")
    if len(parts[0]) == 4:
        year = parts[0][-2:]
        month = parts[1].zfill(2)
    else:
        month = parts[0].zfill(2)
        year = parts[1]
        if len(year) == 4:
            year = year[-2:]
    return month, year


def redact(value: str, keep: int = 4) -> str:
    value = str(value or "")
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(0, len(value) - keep) + value[-keep:]


def first_value(data: dict[str, Any], names: list[str], default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


def normalize_card(card: dict[str, Any]) -> dict[str, str]:
    number = str(first_value(card, ["cardNumber", "card_number", "number", "pan"], "")).strip()
    cvv = str(first_value(card, ["cvv", "cvc", "securityCode", "security_code"], "")).strip()
    expiry = str(first_value(card, ["expiry", "expiration", "expirationDate", "expiration_date"], "")).strip()
    card_type = str(first_value(card, ["cardType", "card_type", "type", "brand"], "")).strip()
    if not number or not cvv or not expiry:
        raise ValueError("card json must contain cardNumber, cvv and expiry")
    month, year = split_expiry(expiry)
    return {
        "cardNumber": re.sub(r"\D+", "", number),
        "cvv": cvv,
        "expiry": f"{month}/{year}",
        "cardType": card_type,
    }


def is_zero_amount_text(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or "")).upper().replace(",", ".")
    zero_patterns = [
        r"^(?:\$|US\$|USD)?0(?:\.00)?$",
        r"^(?:EUR|€)?0(?:\.00)?$",
        r"^(?:GBP|£)?0(?:\.00)?$",
        r"^(?:JPY|¥)?0$",
        r"^0(?:\.00)?(?:USD|EUR|GBP)?$",
    ]
    return any(re.fullmatch(pattern, text) for pattern in zero_patterns)


def load_card(card_path: str) -> dict[str, str]:
    path = Path(card_path).expanduser()
    if path.exists():
        card = normalize_card(load_json(path))
        log(f"[card] loaded separate card json: {path} ({redact(card['cardNumber'])})")
        return card
    raise FileNotFoundError(f"card json not found: {path}")


class TwoCaptchaClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("2Captcha API key is empty")

    def post_json(self, url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def create_task(self, task: dict[str, Any]) -> int:
        response = self.post_json(
            "https://api.2captcha.com/createTask",
            {"clientKey": self.api_key, "task": task},
        )
        if response.get("errorId"):
            raise RuntimeError(f"2Captcha createTask failed: {response}")
        task_id = response.get("taskId")
        if not task_id:
            raise RuntimeError(f"2Captcha createTask did not return taskId: {response}")
        return int(task_id)

    def get_result(self, task_id: int) -> dict[str, Any]:
        return self.post_json(
            "https://api.2captcha.com/getTaskResult",
            {"clientKey": self.api_key, "taskId": task_id},
        )

    def wait_token_result(self, task_id: int, *, label: str, timeout: int = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            result = self.get_result(task_id)
            if result.get("errorId"):
                raise RuntimeError(f"2Captcha {label} getTaskResult failed: {result}")
            if result.get("status") == "ready":
                solution = result.get("solution") or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if not token:
                    raise RuntimeError(f"2Captcha {label} result missing token: {result}")
                log(f"[captcha] 2Captcha {label} token received")
                return token
            log(f"[captcha] 2Captcha {label} still processing")
        raise TimeoutError(f"2Captcha {label} solve timed out")

    def solve_recaptcha_v2_enterprise(
        self,
        *,
        website_url: str,
        website_key: str,
        user_agent: str = "",
        api_domain: str = "",
        is_invisible: bool = False,
        timeout: int = 300,
    ) -> str:
        task: dict[str, Any] = {
            "type": "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "isInvisible": bool(is_invisible),
        }
        if user_agent:
            task["userAgent"] = user_agent
        if api_domain:
            task["apiDomain"] = api_domain
        task_id = self.create_task(task)
        log(f"[captcha] 2Captcha reCAPTCHA task created: {task_id}")
        return self.wait_token_result(task_id, label="reCAPTCHA", timeout=timeout)

    def solve_hcaptcha(
        self,
        *,
        website_url: str,
        website_key: str,
        user_agent: str = "",
        is_invisible: bool = False,
        timeout: int = 300,
    ) -> str:
        task: dict[str, Any] = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "isInvisible": bool(is_invisible),
        }
        if user_agent:
            task["userAgent"] = user_agent
        task_id = self.create_task(task)
        log(f"[captcha] 2Captcha hCaptcha task created: {task_id}")
        return self.wait_token_result(task_id, label="hCaptcha", timeout=timeout)

    def solve_datadome(
        self,
        *,
        website_url: str,
        captcha_url: str,
        user_agent: str,
        proxy: dict[str, Any],
        timeout: int = 300,
    ) -> str:
        task = {
            "type": "DataDomeSliderTask",
            "websiteURL": website_url,
            "captchaUrl": captcha_url,
            "userAgent": user_agent,
            "proxyType": proxy["proxyType"],
            "proxyAddress": proxy["proxyAddress"],
            "proxyPort": proxy["proxyPort"],
        }
        if proxy.get("proxyLogin"):
            task["proxyLogin"] = proxy["proxyLogin"]
        if proxy.get("proxyPassword"):
            task["proxyPassword"] = proxy["proxyPassword"]
        task_id = self.create_task(task)
        log(f"[captcha] 2Captcha DataDome task created: {task_id}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            result = self.get_result(task_id)
            if result.get("errorId"):
                raise RuntimeError(f"2Captcha DataDome getTaskResult failed: {result}")
            if result.get("status") == "ready":
                solution = result.get("solution") or {}
                cookie = solution.get("cookie") or ""
                if not cookie:
                    raise RuntimeError(f"2Captcha DataDome result missing cookie: {result}")
                log("[captcha] 2Captcha DataDome cookie received")
                return cookie
            log("[captcha] 2Captcha DataDome still processing")
        raise TimeoutError("2Captcha DataDome solve timed out")


class RuyiPayPalFlow:
    def __init__(self, args: argparse.Namespace, profile: dict[str, Any]) -> None:
        self.args = args
        self.profile = profile
        self.page: FirefoxPage | Any | None = None
        self.root_page: FirefoxPage | Any | None = None
        self.chain_upstream = parse_proxy_url(args.proxy_chain_upstream) if args.proxy_chain_upstream else None
        self.chain_parent: dict[str, Any] | None = None
        self.chain_bridge: ChainedHttpProxy | None = None
        self.proxy: dict[str, Any] | None = None
        self.geo_proxy: dict[str, Any] | None = None
        if self.chain_upstream:
            parent_url, parent_note = resolve_proxy(args.proxy_chain_via)
            self.chain_parent = parse_proxy_url(parent_url) if parent_url else None
            self.proxy_note = f"chain via {parent_note}: {proxy_display(self.chain_parent)} -> {proxy_display(self.chain_upstream)}"
            self.proxy_mode = "manual"
        else:
            proxy_url, proxy_note = resolve_proxy(args.proxy)
            self.proxy_note = proxy_note
            self.proxy_mode = "direct" if not proxy_url else "manual"
            if self.proxy_mode == "manual":
                self.proxy = parse_proxy_url(proxy_url)
                self.geo_proxy = self.proxy
        self.captcha_proxy = parse_proxy_url(args.captcha_proxy) if args.captcha_proxy else None
        if not self.captcha_proxy and self.chain_upstream:
            self.captcha_proxy = self.chain_upstream
        if not self.captcha_proxy and self.proxy and not is_loopback_host(self.proxy["host"]):
            self.captcha_proxy = self.proxy
        self.profile_root = linux_profile_root()
        self.profile_dir = Path(args.user_dir) if args.user_dir else self.profile_root / f"ruyi_{int(time.time())}_{random.randint(1000, 9999)}"
        self.fpfile = self.profile_dir / "fpfile.txt"
        self.fp_context: Any | None = None
        self.sms_code_received = False
        self.sms_code_filled = False
        self.sms_advanced_without_confirmed_fill = False
        self.final_confirmation_accepted = False
        self.final_confirmation_state = ""
        self.success_network_capture_started = False
        self.captured_success_url = ""
        self.success_network_page: Any | None = None
        self.paypal_response_bodies: list[dict[str, Any]] = []

    def start(self) -> None:
        disable_local_debug_proxy_env()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        base_profile_dir = self.profile_dir
        browser_path = normalize_firefox_path(self.args.browser_path)
        fixed_port = int(self.args.port) if str(self.args.port).strip() else None
        port_attempts = 1 if fixed_port else 3
        log(f"[ruyi] Linux fingerprint browser: {browser_path}")
        if self.chain_upstream:
            self.chain_bridge = ChainedHttpProxy(self.chain_upstream, self.chain_parent)
            self.chain_bridge.start()
            self.proxy = parse_proxy_url(self.chain_bridge.url)
            self.geo_proxy = self.proxy
            log(f"[proxy] local chain bridge {self.chain_bridge.url} -> {proxy_display(self.chain_upstream)}")
        if self.proxy_mode == "manual":
            log(f"[proxy] browser uses {self.proxy_note}: {proxy_display(self.proxy)}")
        else:
            log("[proxy] direct mode")
        if self.geo_proxy and not self.args.no_smart_fingerprint:
            preflight_proxy_tcp(self.geo_proxy, timeout=float(self.args.fingerprint_geo_timeout))

        last_exc: Exception | None = None
        started_port = None
        with firefox_startup_lock():
            for index in range(1, port_attempts + 1):
                # Pick the debug port while holding the cross-process startup
                # lock. Preselecting ports before the lock can go stale when
                # another payment browser starts first.
                port = fixed_port if fixed_port else find_free_port()
                if index > 1 and not self.args.user_dir:
                    self.profile_dir = self.profile_root / f"{base_profile_dir.name}_r{index}_{random.randint(1000, 9999)}"
                    self.fpfile = self.profile_dir / "fpfile.txt"
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                if self.proxy and (self.proxy.get("username") or self.proxy.get("password")):
                    # smart_fingerprint writes the authenticated fpfile itself.
                    # This fallback fpfile is only used when smart fingerprint is disabled.
                    self.fpfile.write_text(
                        "httpauth.username:{}\nhttpauth.password:{}\n".format(
                            self.proxy.get("username", ""),
                            self.proxy.get("password", ""),
                        ),
                        encoding="utf-8",
                    )
                opts = FirefoxOptions()
                opts.set_port(port)
                opts.set_retry(times=8, interval=1)
                opts.quick_start(
                    browser_path=browser_path,
                    user_dir=str(self.profile_dir),
                    proxy=self.proxy["browserProxy"] if self.proxy else None,
                    close_on_exit=not self.args.keep_browser_open,
                    private=False,
                    headless=self.args.headless,
                    action_visual=self.args.action_visual,
                    human_algorithm=self.args.human_algorithm,
                    window_size=(1280, 900),
                    timeout_base=10,
                    timeout_page_load=45,
                    timeout_script=30,
                    trace=False,
                    failure_snapshot=True,
                    snapshot_dir=str(ROOT / "recordings" / "ruyi_snapshots"),
                    marionette=not self.args.disable_marionette,
                )
                locale = (self.args.locale or "en-US").strip()
                if locale.lower().startswith("ja"):
                    accept_languages = "ja-JP,ja,en-US,en"
                else:
                    accept_languages = "en-US,en"
                opts.set_pref("intl.accept_languages", accept_languages)
                opts.set_pref("privacy.resistFingerprinting", False)
                apply_standard_tracking_protection(opts)
                if self.proxy and self.proxy["scheme"].startswith("socks"):
                    # Keep DNS aligned with the upstream SOCKS exit IP.
                    opts.set_pref("network.proxy.socks_remote_dns", True)
                if not self.args.no_smart_fingerprint:
                    fp_proxy = self.geo_proxy
                    self.fp_context = opts.smart_fingerprint(
                        proxy_host=fp_proxy["host"] if fp_proxy else None,
                        proxy_port=fp_proxy["port"] if fp_proxy else None,
                        proxy_user=fp_proxy["username"] if fp_proxy else None,
                        proxy_pwd=fp_proxy["password"] if fp_proxy else None,
                        proxy_scheme=fp_proxy["requestScheme"] if fp_proxy else "http",
                        userdir=str(self.profile_dir),
                        base_dir=str(self.profile_root),
                        require_country=(self.args.fingerprint_country or "").strip().upper() or None,
                        geo_timeout=float(self.args.fingerprint_geo_timeout),
                        geo_retries=int(self.args.fingerprint_geo_retries),
                        fetch_ipv6=not self.args.no_fingerprint_ipv6,
                        set_proxy_on_opts=bool(self.proxy),
                        logger=log,
                    )
                elif self.fpfile.exists():
                    opts.set_fpfile(str(self.fpfile))
                try:
                    self.root_page = FirefoxPage(opts)
                    self.page = self.root_page
                    started_port = port
                    break
                except Exception as exc:
                    last_exc = exc
                    killed = kill_firefox_processes(profile_dir=self.profile_dir, port=port)
                    if killed:
                        log(f"[ruyi] killed {killed} failed Firefox process(es) for profile={self.profile_dir}")
                    if fixed_port or index == port_attempts:
                        raise
                    log(f"[ruyi] Firefox connect failed on port={port}; retrying another port")
        if not self.page:
            raise RuntimeError(f"Firefox backend failed to start: {last_exc}")
        if self.fp_context:
            log(self.fp_context.summary())
            self.fp_context.apply_emulation(self.page, logger=log)
        else:
            try:
                self.page.emulation.set_locale((self.args.locale or "en-US").strip() or "en-US")
                self.page.emulation.set_timezone(self.args.timezone)
            except Exception as exc:
                log(f"[ruyi] locale/timezone emulation skipped: {exc}")
        self.stabilize_window()
        log(f"[ruyi] Firefox backend started port={started_port} profile={self.profile_dir}")

    def close(self) -> None:
        self.stop_success_network_capture()
        target = self.root_page or self.page
        if target and not self.args.keep_browser_open:
            try:
                target.quit()
            except Exception:
                pass
        if self.chain_bridge:
            self.chain_bridge.stop()

    def stabilize_window(self) -> None:
        if self.args.headless or self.args.no_stabilize_window or not self.page:
            return
        width, height = parse_window_size(str(self.args.window_size))
        try:
            self.page.window.normal()
            self.page.window.center(width=width, height=height)
            time.sleep(0.2)
            info = {}
            viewport = {}
            try:
                info = dict(self.page.window.info or {})
            except Exception:
                pass
            try:
                viewport = self.js(
                    "(() => ({w: window.innerWidth || 0, h: window.innerHeight || 0}))()",
                    timeout=2,
                ) or {}
            except Exception:
                pass
            log(f"[ruyi] window stabilized target={width}x{height} info={info} viewport={viewport}")
        except Exception as exc:
            log(f"[ruyi] window stabilize skipped: {exc}")

    def recover_page_context(self) -> bool:
        """Switch to a live tab after redirects/new-tab navigation."""
        root = self.root_page or self.page
        if not root:
            return False
        try:
            tabs = root.get_tabs()
        except Exception:
            tabs = []
        preferred: Any | None = None
        fallback: Any | None = None
        for tab in reversed(tabs or []):
            try:
                url = str(tab.url or "")
            except Exception:
                continue
            if not fallback and url and not url.startswith("about:"):
                fallback = tab
            if re.search(r"pay\.openai\.com|stripe\.com|paypal\.com|chatgpt\.com", url, re.I):
                preferred = tab
                break
        try:
            latest = root.latest_tab
        except Exception:
            latest = None
        tab = preferred or fallback or latest
        if not tab:
            return False
        try:
            tab.activate()
        except Exception:
            pass
        self.page = tab
        return True

    def reopen_url_in_new_tab(self, url: str) -> bool:
        root = self.root_page or self.page
        if not root or not url:
            return False
        old_page = self.page
        try:
            log(f"[paypal] reopen stalled page in new tab: {url}")
            new_tab = root.new_tab(url, background=False)
            try:
                new_tab.activate()
            except Exception:
                pass
            self.page = new_tab
            if old_page and old_page is not new_tab:
                try:
                    old_page.close()
                except Exception:
                    pass
            self.wait_ready(timeout=30)
            return True
        except Exception as exc:
            log(f"[paypal] reopen new tab failed: {type(exc).__name__}: {exc}")
            self.page = old_page or self.page
            return False

    def close_auxiliary_tabs(self) -> None:
        """Close tabs opened by Stripe/PayPal legal/help links.

        These tabs can become active after a mis-click or browser focus change
        and then slow down URL polling.  Keep only the checkout/payment tabs on
        the critical path.
        """
        root = self.root_page or self.page
        if not root:
            return
        try:
            tabs = list(root.get_tabs() or [])
        except Exception:
            return
        active_candidate: Any | None = None
        for tab in tabs:
            try:
                url = str(tab.url or "")
            except Exception:
                continue
            if re.search(r"stripe\.com/(?:legal|privacy|terms)|paypal\.com/.*/(?:legal|privacy)", url, re.I):
                log(f"[tabs] close auxiliary tab: {url}")
                try:
                    tab.close()
                except Exception:
                    pass
                continue
            if re.search(r"pay\.openai\.com|paypal\.com|pm-redirects\.stripe\.com", url, re.I):
                active_candidate = tab
        if active_candidate:
            try:
                active_candidate.activate()
            except Exception:
                pass
            self.page = active_candidate

    def js(self, expression: str, timeout: float = 10.0) -> Any:
        if not self.page:
            raise RuntimeError("page is not started")
        for attempt in range(2):
            try:
                return self.page.run_js(expression, as_expr=True, timeout=timeout)
            except BiDiError as exc:
                message = str(exc)
                if attempt == 0 and re.search(r"no such frame|Browsing Context .* not found", message, re.I):
                    log("[ruyi] browsing context lost; switching to live tab")
                    if self.recover_page_context():
                        time.sleep(0.5)
                        continue
                raise

    def page_info(self) -> dict[str, Any]:
        return self.js(
            """
(() => ({url: location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 2500)}))()
""",
            timeout=5,
        ) or {}

    def inject_common_checkout_styles(self, *, include_datadome: bool = False) -> None:
        if not self.page:
            return
        include_datadome_js = "true" if include_datadome else "false"
        try:
            self.js(
                r"""
(() => {
  const id = 'ruyipage-common-checkout-styles';
  const selectors = ['.AddressAutocomplete-results'];
  if (__INCLUDE_DATADOME__) {
    selectors.push(
      'iframe[src*="geo.ddc.paypal.com"],iframe[src*="ddc.paypal.com"],iframe[src*="datadome"],iframe[src*="captcha-delivery"]',
      '[src*="geo.ddc.paypal.com"],[src*="ddc.paypal.com"],[src*="datadome"],[src*="captcha-delivery"]'
    );
  }
  const css = selectors.join(',') + '{display:none!important;height:0!important;min-height:0!important;max-height:0!important;width:0!important;min-width:0!important;max-width:0!important;overflow:hidden!important;opacity:0!important;pointer-events:none!important;visibility:hidden!important}';
  let st = document.getElementById(id);
  if (!st) {
    st = document.createElement('style');
    st.id = id;
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  } else if (st.textContent !== css) {
    st.textContent = css;
  }
  return true;
})()
""".replace("__INCLUDE_DATADOME__", include_datadome_js),
                timeout=2,
            )
        except Exception as exc:
            log(f"[page] checkout style injection skipped: {type(exc).__name__}: {exc}")

    def apply_datadome_css_bypass(self) -> bool:
        """Apply the checkout CSS from 新建文本文档 (2).txt as the first DataDome path."""
        try:
            before_url = str((self.page_info() or {}).get("url") or "")
        except Exception:
            before_url = ""
        self.inject_common_checkout_styles(include_datadome=True)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.35)
            state = self.captcha_state()
            url = str(state.get("url") or "")
            advanced = bool(url and before_url and url != before_url and "paypal.com/agreements/approve" not in url)
            if advanced or state.get("normalActionVisible") or state.get("otpActionVisible"):
                log("[captcha] DataDome CSS bypass advanced")
                return True
        log("[captcha] DataDome CSS bypass did not advance; falling back to local slider")
        return False

    def remove_common_checkout_styles(self) -> None:
        if not self.page:
            return
        try:
            self.js(
                r"""
(() => {
  const st = document.getElementById('ruyipage-common-checkout-styles');
  if (st) st.remove();
  return true;
})()
""",
                timeout=2,
            )
        except Exception as exc:
            log(f"[page] checkout style removal skipped: {type(exc).__name__}: {exc}")

    def is_secure_connection_failed_page(self, title: Any = None, text: str = "") -> bool:
        return bool(
            re.search(
                r"Secure Connection Failed|The page you are trying to view cannot be shown because the authenticity of the received data could not be verified|could not be verified|authenticity of the received data",
                str(title or "") + "\n" + str(text or ""),
                re.I,
            )
        )

    def refresh_secure_connection_failed_once(self) -> bool:
        if not self.page:
            return False
        try:
            info = self.page_info()
        except Exception:
            return False
        if not self.is_secure_connection_failed_page(info.get("title"), str(info.get("text") or "")):
            return False
        log("[page] secure connection failed; refresh current page")
        try:
            self.page.refresh()
        except Exception as exc:
            log(f"[page] secure connection refresh raised: {type(exc).__name__}: {exc}")
            url = str(info.get("url") or self.current_url())
            if url:
                try:
                    self.page.get(url, timeout=60)
                except Exception as nav_exc:
                    log(f"[page] secure connection re-open raised: {type(nav_exc).__name__}: {nav_exc}")
        return True

    def current_url(self) -> str:
        if not self.page:
            return ""
        for attempt in range(2):
            try:
                return str(self.page.url or "")
            except BiDiError as exc:
                if attempt == 0 and re.search(r"no such frame|Browsing Context .* not found", str(exc), re.I):
                    self.recover_page_context()
                    continue
                raise
            except Exception:
                if attempt == 0 and self.recover_page_context():
                    continue
                return ""
        return ""

    def compact_network_body(self, body: Any, *, limit: int = 1800) -> dict[str, Any]:
        if isinstance(body, bytes):
            text = body.decode("utf-8", errors="replace")
        else:
            text = str(body or "")
        # Keep diagnostics useful while avoiding raw long card/phone/token-like digits.
        text = re.sub(
            r"(?<!\d)\d{7,19}(?!\d)",
            lambda m: m.group(0)[:2] + "*" * max(0, len(m.group(0)) - 4) + m.group(0)[-2:],
            text,
        )

        def compact(value: Any, depth: int = 0) -> Any:
            if depth >= 4:
                return "<truncated>"
            if isinstance(value, dict):
                return {str(k)[:80]: compact(v, depth + 1) for k, v in list(value.items())[:40]}
            if isinstance(value, list):
                return [compact(v, depth + 1) for v in value[:20]]
            if isinstance(value, str):
                return value[:500]
            return value

        try:
            parsed = json.loads(text)
            return {"json": compact(parsed)}
        except Exception:
            return {"text": text[:limit]}

    def start_paypal_response_body_capture(self, label: str) -> bool:
        if os.environ.get("PAYPAL_CAPTURE_RESPONSE_BODIES", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        if not self.page:
            return False
        try:
            def response_handler(req: Any) -> None:
                url = str(getattr(req, "url", "") or "")
                status = int(getattr(req, "response_status", 0) or 0)
                interesting = bool(
                    "paypal.com" in url
                    and re.search(
                        r"graphql|payment-authentication|genericError|hostedchallenge|validatecaptcha|logclientdata",
                        url,
                        re.I,
                    )
                )
                continued = False
                try:
                    req.continue_response()
                    continued = True
                    if not interesting:
                        return
                    record = {
                        "label": label,
                        "status": status,
                        "url": url[:260],
                        "body": self.compact_network_body(getattr(req, "response_body", "")),
                    }
                    self.paypal_response_bodies.append(record)
                    self.paypal_response_bodies = self.paypal_response_bodies[-24:]
                    log(f"[{label}] response body captured status={status} url={url[:180]}")
                except Exception as exc:
                    if not continued:
                        try:
                            req.continue_response()
                        except Exception:
                            pass
                    log(f"[{label}] response body capture handler error: {type(exc).__name__}: {exc}")

            self.page.intercept.start_responses(response_handler, collect_response=True)
            return True
        except Exception as exc:
            log(f"[{label}] response body capture disabled: {type(exc).__name__}: {exc}")
            return False

    def stop_paypal_response_body_capture(self, label: str) -> None:
        if not self.page:
            return
        try:
            self.page.intercept.stop()
        except Exception as exc:
            log(f"[{label}] response body capture stop skipped: {type(exc).__name__}: {exc}")

    def start_success_network_capture(self) -> None:
        if self.success_network_capture_started or not self.page:
            return
        try:
            ok = self.page.events.start(
                ["network.responseCompleted"],
                contexts=[self.page.tab_id],
            )
        except Exception as exc:
            log(f"[result] success network capture disabled: {type(exc).__name__}: {exc}")
            return
        self.success_network_capture_started = bool(ok)
        if self.success_network_capture_started:
            self.success_network_page = self.page
            log("[result] success network capture started")

    def stop_success_network_capture(self) -> None:
        if not self.success_network_capture_started:
            return
        page = self.success_network_page or self.page
        try:
            if page:
                page.events.stop()
        except Exception:
            pass
        self.success_network_capture_started = False
        self.success_network_page = None

    def is_success_redirect_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(str(url or ""))
        if not parsed.netloc:
            return False
        host = parsed.netloc.lower()
        query = urllib.parse.parse_qs(parsed.query)
        if self.is_stripe_pm_redirect_success_url(str(url or "")):
            return True
        if host == "pay.openai.com" and (query.get("redirect_status") or [""])[0] == "succeeded":
            return True
        if host == "pay.openai.com" and (query.get("returned_from_redirect") or [""])[0].lower() == "true":
            return True
        if host == "chatgpt.com" and parsed.path.startswith("/payments/success"):
            return True
        return False

    def drain_success_network_events(self) -> str:
        if self.captured_success_url:
            return self.captured_success_url
        if not self.success_network_capture_started:
            return ""
        page = self.success_network_page or self.page
        events = getattr(page, "events", None)
        if not events:
            return ""
        for _ in range(20):
            try:
                event = events.wait(timeout=0.001)
            except Exception:
                return ""
            if not event:
                break
            if getattr(event, "method", "") != "network.responseCompleted":
                continue
            urls: list[str] = []
            response = event.response if isinstance(event.response, dict) else {}
            status = int(response.get("status") or 0)
            if status >= 400:
                continue
            for value in (getattr(event, "url", ""), response.get("url")):
                if value:
                    urls.append(str(value))
            for url in urls:
                if self.is_success_redirect_url(url):
                    self.captured_success_url = url
                    log(f"[result] captured success redirect: {url}")
                    return url
        return ""

    def wait_ready(self, timeout: int = 30) -> None:
        if not self.page:
            raise RuntimeError("page is not started")
        secure_refresh_done = False
        try:
            self.page.wait.doc_loaded(timeout=timeout)
            self.inject_common_checkout_styles()
            if not secure_refresh_done and self.refresh_secure_connection_failed_once():
                secure_refresh_done = True
                time.sleep(1)
            else:
                return
        except Exception:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not secure_refresh_done and self.refresh_secure_connection_failed_once():
                    secure_refresh_done = True
                    time.sleep(1)
                    continue
                state = self.js("document.readyState", timeout=2)
                if state in ("interactive", "complete"):
                    self.inject_common_checkout_styles()
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise TimeoutError("page did not become ready")

    def navigate(self, url: str) -> None:
        if not self.page:
            raise RuntimeError("page is not started")
        self.page.get(url, timeout=90)
        self.wait_ready(timeout=60)

    def wait_url_contains(self, needles: list[str], timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                info = self.page_info()
                url = str(info.get("url") or "")
                if any(n in url for n in needles):
                    return url
            except BiDiError:
                self.recover_page_context()
            root = self.root_page or self.page
            if root:
                try:
                    for tab in reversed(root.get_tabs()):
                        try:
                            tab_url = str(tab.url or "")
                        except Exception:
                            continue
                        if any(n in tab_url for n in needles):
                            try:
                                tab.activate()
                            except Exception:
                                pass
                            self.page = tab
                            return tab_url
                except Exception:
                    pass
            time.sleep(1)
        raise TimeoutError(f"URL did not contain {needles}")

    def wait_for_url_any(self, needles: list[str], timeout: int = 15) -> bool:
        try:
            self.wait_url_contains(needles, timeout=timeout)
            return True
        except TimeoutError:
            return False

    def wait_text(self, pattern: str, timeout: int = 60) -> dict[str, Any]:
        deadline = time.time() + timeout
        rx = re.compile(pattern, re.I)
        while time.time() < deadline:
            info = self.page_info()
            if rx.search(str(info.get("text") or "")) or rx.search(str(info.get("title") or "")):
                return info
            time.sleep(1)
        raise TimeoutError(f"text not found: {pattern}")

    def click_point(self, point: dict[str, Any]) -> None:
        if not self.page:
            raise RuntimeError("page is not started")
        self.page.actions.human_move({"x": point["x"], "y": point["y"]}).human_click().perform()

    def human_pause(self, low: float = 0.2, high: float = 0.8) -> None:
        if self.args.human_profile == "off":
            return
        time.sleep(random.uniform(low, high))

    def human_breathe(self, duration: float = 2.0, intensity: str = "normal") -> None:
        if self.args.human_profile == "off" or not self.page:
            time.sleep(max(0.0, duration))
            return
        end = time.time() + max(0.0, duration)
        while time.time() < end:
            try:
                viewport = self.js(
                    "(() => ({w: window.innerWidth || 1280, h: window.innerHeight || 900}))()",
                    timeout=2,
                ) or {"w": 1280, "h": 900}
                w = int(viewport.get("w") or 1280)
                h = int(viewport.get("h") or 900)
                radius_x = 160 if intensity == "normal" else 80
                radius_y = 90 if intensity == "normal" else 45
                point = {
                    "x": max(20, min(w - 20, w * 0.5 + random.randint(-radius_x, radius_x))),
                    "y": max(20, min(h - 20, h * 0.55 + random.randint(-radius_y, radius_y))),
                }
                self.page.actions.human_move(point, style=random.choice(["arc", "line_then_arc", None])).perform()
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.4))

    def click_point_from_script(self, script: str, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                point = self.js(script, timeout=3)
                if point and point.get("x") is not None and point.get("y") is not None:
                    self.click_point(point)
                    time.sleep(random.uniform(0.2, 0.6))
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def click_by_selector(self, selectors: list[str], timeout: int = 30) -> bool:
        script = f"""
(() => {{
  const selectors = {json.dumps(selectors)};
  for (const selector of selectors) {{
    let el = null;
    try {{ el = document.querySelector(selector); }} catch (_) {{ el = null; }}
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || el.disabled || style.visibility === 'hidden' || style.display === 'none') continue;
    el.scrollIntoView({{block:'center', inline:'center'}});
    const r2 = el.getBoundingClientRect();
    return {{x: r2.left + r2.width / 2, y: r2.top + r2.height / 2}};
  }}
  return false;
}})()
"""
        return self.click_point_from_script(script, timeout=timeout)

    def click_by_text(self, patterns: list[str], timeout: int = 30) -> bool:
        script = f"""
(() => {{
  const patterns = {json.dumps(patterns)}.map((x) => new RegExp(x, 'i'));
  function label(el) {{
    return [el.innerText || el.textContent || '', el.getAttribute('aria-label') || '', el.value || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }}
  const els = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')];
  const el = els.find((x) => {{
    const r = x.getBoundingClientRect();
    const style = window.getComputedStyle(x);
    if (r.width <= 0 || r.height <= 0 || x.disabled || style.visibility === 'hidden' || style.display === 'none') return false;
    const text = label(x);
    return patterns.some((p) => p.test(text));
  }});
  if (!el) return false;
  el.scrollIntoView({{block:'center', inline:'center'}});
  const r = el.getBoundingClientRect();
  return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
}})()
"""
        return self.click_point_from_script(script, timeout=timeout)

    def dom_click_by_script(self, script: str, timeout: float = 3.0) -> Any:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self.js(script, timeout=min(2.0, max(0.5, deadline - time.time())))
                if result:
                    return result
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def click_paypal_create_account(self, allow_geometry: bool = True, allow_hard_fallback: bool = True) -> bool:
        selectors = [
            "#startOnboardingFlow",
            "#guestCheckout",
            "#createAccount",
            "#signup",
            "button[name='createAccount']",
            "button[data-testid*='create']",
        ]
        if self.click_by_selector(selectors, timeout=1):
            log("[paypal] click Create an Account by selector")
            return True
        if self.click_by_text([r"^Create an Account$", r"\bCreate an Account\b"], timeout=2):
            log("[paypal] click Create an Account by text")
            return True

        if not allow_geometry:
            return False

        # Geometry fallback still reads the live DOM. It avoids hard-coded
        # viewport ratios when PayPal keeps loading hCaptcha assets.
        script = r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }
  const controls = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
    .filter(visible)
    .map((el) => ({el, text: label(el), rect: el.getBoundingClientRect()}));
  let match = controls.find((x) => /^Create an Account$/i.test(x.text) || /\bCreate an Account\b/i.test(x.text));
  if (!match) {
    const next = controls.find((x) => /^Next$/i.test(x.text));
    const lower = controls
      .filter((x) => x.rect.width >= 220 && x.rect.height >= 30 && (!next || x.rect.top > next.rect.bottom + 20))
      .sort((a, b) => a.rect.top - b.rect.top);
    match = lower[0] || null;
  }
  if (!match) return false;
  match.el.scrollIntoView({block: 'center', inline: 'center'});
  const r = match.el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: match.text};
})()
"""
        if self.click_point_from_script(script, timeout=2):
            log("[paypal] click Create an Account by live geometry")
            return True
        if not allow_hard_fallback:
            return False
        return self.click_paypal_create_account_fallback()

    def click_paypal_create_account_fallback(self) -> bool:
        """Last-resort fixed-layout click for the PayPal /pay onboarding page."""
        try:
            viewport = self.js(
                "(() => ({w: window.innerWidth || 1920, h: window.innerHeight || 1080}))()",
                timeout=1,
            ) or {"w": 1920, "h": 1080}
        except Exception:
            viewport = {"w": 1920, "h": 1080}
        w = int(viewport.get("w") or 1920)
        h = int(viewport.get("h") or 1080)
        point = {"x": round(w * 0.5), "y": round(h * 0.615)}
        log(f"[paypal] hard fallback click Create an Account at {point}")
        self.click_point(point)
        return True

    def visible_selector(self, selectors: list[str]) -> str:
        return str(
            self.js(
                f"""
(() => {{
  const selectors = {json.dumps(selectors)};
  for (const selector of selectors) {{
    let el = null;
    try {{ el = document.querySelector(selector); }} catch (_) {{ el = null; }}
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none') return selector;
  }}
  return '';
}})()
""",
                timeout=3,
            )
            or ""
        )

    def type_field(self, selectors: list[str], value: str, timeout: int = 10, *, fast: bool = False) -> bool:
        if not self.page:
            raise RuntimeError("page is not started")
        deadline = time.time() + timeout
        while time.time() < deadline:
            selector = self.visible_selector(selectors)
            if selector:
                ele = self.page.ele("css:" + selector, timeout=1)
                if ele:
                    try:
                        self.page.actions.human_click(ele).perform()
                    except Exception:
                        try:
                            ele.click()
                        except Exception:
                            pass
                    if not fast:
                        time.sleep(random.uniform(0.08, 0.25))
                    try:
                        ele.input(str(value), clear=True)
                        if not fast:
                            time.sleep(random.uniform(0.08, 0.25))
                        return True
                    except Exception as exc:
                        log(f"[input] BiDi input fallback for {selector}: {type(exc).__name__}")
                        ok = self.js(
                            f"""
(() => {{
  const selector = {json.dumps(selector)};
  const value = {json.dumps(str(value))};
  const el = document.querySelector(selector);
  if (!el) return false;
  el.scrollIntoView({{block:'center', inline:'center'}});
  el.focus();
  el.value = '';
  el.dispatchEvent(new Event('input', {{bubbles:true}}));
  el.value = value;
  el.dispatchEvent(new Event('input', {{bubbles:true}}));
  el.dispatchEvent(new Event('change', {{bubbles:true}}));
  return true;
}})()
""",
                            timeout=3,
                        )
                        if ok:
                            if not fast:
                                time.sleep(random.uniform(0.08, 0.25))
                            return True
            time.sleep(0.3)
        return False

    def fill_visible_fields_fast(self, fields: list[tuple[str, list[str], str]]) -> dict[str, bool]:
        """Fill known visible fields with one DOM pass.

        PayPal accepts password-manager/paste-style filling better than slow
        per-character typing. We still use normal ruyi input as fallback for
        fields that were not visible in the fast pass.
        """
        result = self.js(
            f"""
(() => {{
  const fields = {json.dumps(fields)};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function setValue(el, value) {{
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    el.scrollIntoView({{block:'center', inline:'center'}});
    el.focus();
    if (setter) setter.call(el, '');
    else el.value = '';
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    if (setter) setter.call(el, String(value));
    else el.value = String(value);
    el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:String(value)}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles:true}}));
  }}
  const out = {{}};
  for (const [name, selectors, value] of fields) {{
    let el = null;
    for (const selector of selectors) {{
      try {{
        const candidate = document.querySelector(selector);
        if (visible(candidate)) {{
          el = candidate;
          break;
        }}
      }} catch (_) {{}}
    }}
    if (!el) {{
      out[name] = false;
      continue;
    }}
    setValue(el, value);
    out[name] = true;
  }}
  return out;
}})()
""",
            timeout=5,
        )
        return result if isinstance(result, dict) else {}

    def field_value(self, selectors: list[str]) -> str:
        return str(
            self.js(
                f"""
(() => {{
  const selectors = {json.dumps(selectors)};
  for (const selector of selectors) {{
    let el = null;
    try {{ el = document.querySelector(selector); }} catch (_) {{ el = null; }}
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    return el.value || '';
  }}
  return '';
}})()
""",
                timeout=3,
            )
            or ""
        )

    def select_state(self, value: str) -> None:
        if not self.page:
            return
        candidates = [str(value or "").strip()]
        if candidates[0] in {"北海道", "Hokkaido", "JP-01"}:
            candidates = ["北海道", "Hokkaido", "JP-01"]
        selectors = [
            "select[name='state']",
            "select#state",
            "#billingState",
            "select[name='billingState']",
            "[name='billingAddress.state']",
            "#billingAdministrativeArea",
            "select[name='billingAdministrativeArea']",
        ]
        result = self.js(
            f"""
(() => {{
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  const selectors = {json.dumps(selectors)};
  const candidates = {json.dumps(candidates)};
  for (const selector of selectors) {{
    const el = document.querySelector(selector);
    if (!visible(el)) continue;
    const options = [...(el.options || [])];
    let found = null;
    for (const candidate of candidates) {{
      const wanted = String(candidate || '').trim().toLowerCase();
      found = options.find((opt) => {{
        const value = String(opt.value || '').trim().toLowerCase();
        const text = String(opt.textContent || '').trim().toLowerCase();
        return value === wanted || text === wanted || text.includes(wanted);
      }});
      if (found) break;
    }}
    if (!found) return {{ok:false, selector, reason:'state_option_not_found', candidates, options: options.map((o) => ({{value:o.value, text:o.textContent}})).slice(0, 80)}};
    el.value = found.value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    el.dispatchEvent(new Event('blur', {{bubbles:true}}));
    return {{ok:true, selector, value:found.value, text:found.textContent}};
  }}
  return {{ok:false, reason:'state_select_not_found'}};
}})()
""",
            timeout=3,
        )
        if isinstance(result, dict) and result.get("ok"):
            return
        if isinstance(result, dict) and result:
            log(f"[address] state select fallback needed: {result}")
        selector = self.visible_selector(selectors)
        if selector:
            ele = self.page.ele("css:" + selector, timeout=1)
            if ele:
                for candidate in candidates:
                    try:
                        ele.select.by_value(candidate)
                        return
                    except Exception:
                        pass
        self.js(
            f"""
(() => {{
  const selectors = {json.dumps(selectors)};
  const candidates = {json.dumps(candidates)};
  for (const selector of selectors) {{
    const el = document.querySelector(selector);
    if (!el) continue;
    let selected = '';
    const options = [...(el.options || [])];
    for (const candidate of candidates) {{
      const found = options.find((opt) =>
        String(opt.value || '').trim().toLowerCase() === String(candidate).trim().toLowerCase() ||
        String(opt.textContent || '').trim().toLowerCase() === String(candidate).trim().toLowerCase()
      );
      if (found) {{
        selected = found.value;
        break;
      }}
    }}
    el.value = selected || candidates[0] || '';
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    return true;
  }}
  return false;
}})()
""",
            timeout=3,
        )

    def press_key(self, key: str) -> None:
        if not self.page:
            return
        key_map = {
            "Enter": Keys.ENTER,
            "Tab": Keys.TAB,
            "Escape": Keys.ESCAPE,
            "ArrowDown": Keys.DOWN,
        }
        self.page.actions.press(key_map.get(key, key)).perform()

    def stripe_amounts(self) -> list[str]:
        values = self.js(
            """
(() => [...document.querySelectorAll('.CurrencyAmount,[data-testid*="amount" i],[class*="CurrencyAmount"]')]
  .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
  .filter(Boolean)
  .slice(0, 12))()
""",
            timeout=3,
        )
        return values if isinstance(values, list) else []

    def wait_stripe_content_or_refresh(self, timeout: int = 90, blank_seconds: int = 8) -> None:
        """Refresh once when the Stripe shell is visually blank for too long."""
        deadline = time.time() + timeout
        blank_deadline = time.time() + blank_seconds
        refreshed = False
        while time.time() < deadline:
            state = self.js(
                """
(() => {
  const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
  const hasStripeUi = /payment method|支払い方法|paypal|card|カード|subscribe|申し込/i.test(text);
  const hasControls = !!document.querySelector('button,input,select,[role="button"],[role="radio"]');
  return {url: location.href, textLen: text.length, hasStripeUi, hasControls};
})()
""",
                timeout=3,
            ) or {}
            if state.get("hasStripeUi") or (int(state.get("textLen") or 0) > 30 and state.get("hasControls")):
                return
            if not refreshed and time.time() >= blank_deadline:
                log("[stripe] page still blank after 8s; refresh")
                if self.page:
                    self.page.refresh()
                refreshed = True
                blank_deadline = time.time() + blank_seconds
            time.sleep(0.5)
        raise TimeoutError("Stripe page content did not load")

    def wait_stripe_amount_sample(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        latest: list[str] = []
        while time.time() < deadline:
            latest = self.stripe_amounts()
            if latest:
                log(f"[stripe] amount text: {' | '.join(latest[:4])}")
                if not any(is_zero_amount_text(x) for x in latest):
                    deferred = self.stripe_deferred_coupon_amount_state()
                    if deferred.get("ok"):
                        log(f"[stripe] accepted deferred coupon recurring amount: {deferred}")
                        return
                    raise RuntimeError(f"Stripe amount check failed; expected 0 amount: {latest[:4]}")
                return
            time.sleep(1)
        raise RuntimeError("Stripe amount check failed: amount text not found")

    def stripe_deferred_coupon_amount_state(self) -> dict[str, Any]:
        """Japanese Stripe may show only the post-coupon recurring fee, not today's 0 due."""
        try:
            state = self.js(
                r"""
(() => {
  const text = (document.body && document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const total = String(document.querySelector('[data-testid="product-summary-total-amount"], #ProductSummary-totalAmount')?.innerText || '').trim();
  const description = String(document.querySelector('[data-testid="product-summary-subscription-description"], #ProductSummary-description')?.innerText || '').trim();
  const hasDeferredCoupon =
    /クーポン失効後/.test(text) ||
    /after\s+(the\s+)?coupon\s+(expires|expiration)/i.test(text) ||
    /after\s+your\s+(trial|promotion)/i.test(text);
  const hasImmediateCharge =
    /(本日|今日|now|today|due now|支払額|請求額|本日の請求)/i.test(text) &&
    /[$¥€£]\s*[1-9][\d,.]*/.test(text);
  return {ok: Boolean(hasDeferredCoupon && !hasImmediateCharge), total, description, hasDeferredCoupon, hasImmediateCharge};
})()
""",
                timeout=3,
            )
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def select_stripe_address_autocomplete(self) -> bool:
        point = self.js(
            """
(() => {
  const selectors = [
    '.AddressAutocomplete-option',
    '[data-testid="address-autocomplete-option"]',
    '.AddressAutocomplete li',
    '[class*="autocomplete" i] li',
    '[class*="suggestion" i]',
    '[class*="Suggestion"]'
  ];
  for (const selector of selectors) {
    let el = null;
    try { el = document.querySelector(selector); } catch (_) { el = null; }
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    el.scrollIntoView({block:'center', inline:'center'});
    const r2 = el.getBoundingClientRect();
    return {x: r2.left + r2.width / 2, y: r2.top + r2.height / 2};
  }
  return false;
})()
""",
            timeout=3,
        )
        if point and point.get("x") is not None:
            self.click_point(point)
            time.sleep(0.5)
            return True
        return False

    def select_paypal_address_autocomplete(self) -> bool:
        point = self.js(
            """
(() => {
  const selectors = [
    '[role="option"]',
    '[id*="address-suggestion" i]',
    '[class*="address" i][class*="suggest" i]',
    '[class*="autocomplete" i] li',
    '[class*="suggestion" i]'
  ];
  for (const selector of selectors) {
    let items = [];
    try { items = [...document.querySelectorAll(selector)]; } catch (_) { items = []; }
    for (const el of items) {
      const r = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
      if (!text || /privacy|legal|paypal|policy|country|language/i.test(text)) continue;
      el.scrollIntoView({block:'center', inline:'center'});
      const r2 = el.getBoundingClientRect();
      return {x: r2.left + r2.width / 2, y: r2.top + r2.height / 2};
    }
  }
  return false;
})()
""",
            timeout=1,
        )
        if point and point.get("x") is not None:
            self.click_point(point)
            time.sleep(random.uniform(0.08, 0.16))
            return True
        return False

    def validate_stripe_completeness(self, address: dict[str, Any]) -> None:
        personal = self.profile.get("personalInfo") or {}
        full_name = f"{personal.get('firstName', '')} {personal.get('lastName', '')}".strip()
        checks = [
            (["#billingName", "input[name='billingName']", "input[name='name']"], full_name),
            (["#billingAddressLine1", "input[name='billingAddressLine1']"], address["street"]),
            (["#billingLocality", "#billingAddressCity", "input[name='billingLocality']"], address["city"]),
            (["#billingPostalCode", "input[name='billingPostalCode']"], address["zipCode"]),
            (["#billingAdministrativeArea", "select[name='billingAdministrativeArea']"], address["state"]),
        ]
        refilled = 0
        for selectors, value in checks:
            if not self.visible_selector(selectors):
                continue
            current = self.field_value(selectors)
            if current == "":
                if self.type_field(selectors, str(value), timeout=1):
                    refilled += 1
        if refilled:
            log(f"[stripe] refilled missing fields: {refilled}")

    def stripe_billing_state(self) -> dict[str, Any]:
        state = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function attrs(el) {
    return [
      el.id || '',
      el.name || '',
      el.placeholder || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('data-testid') || '',
      el.autocomplete || '',
      el.closest('label')?.innerText || '',
      el.parentElement?.innerText || ''
    ].join(' ');
  }
  const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible);
  const manualLinkVisible = [...document.querySelectorAll('button,a,[role="button"],span,div')].some((el) => {
    if (!visible(el)) return false;
    const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    return text.length > 0 && text.length <= 80 &&
      /enter address manually|manual address|住所を手動で入力|手動で入力/i.test(text);
  });
  const addressFields = inputs.filter((el) => {
    const a = attrs(el);
    return /address|住所|street|line1|billingAddressLine1/i.test(a) &&
      !/city|locality|postal|zip|country|state|phone|email|name|card|cvv|cvc|expiry/i.test(a);
  });
  const detailFields = inputs.filter((el) => {
    const a = attrs(el);
    return /city|locality|postal|zip|state|administrative/i.test(a) &&
      !/country|phone|email|name|card|cvv|cvc|expiry/i.test(a);
  });
  const line1Values = addressFields.map((el) => String(el.value || '').trim()).filter(Boolean);
  const detailValues = detailFields.map((el) => String(el.value || '').trim());
  const hasLine1 = line1Values.some((v) => v.length >= 2);
  const needsManualExpansion = hasLine1 && manualLinkVisible && detailFields.length === 0;
  const detailComplete = detailFields.length === 0 || detailValues.every((v) => v.length > 0);
  const invalids = inputs
    .filter((el) => {
      const a = attrs(el);
      if (/optional|phone|email|card|cvv|cvc|expiry/i.test(a)) return false;
      const empty = !String(el.value || '').trim();
      const invalid = el.required || el.getAttribute('aria-invalid') === 'true' || el.matches(':invalid');
      return empty && invalid;
    })
    .map((el) => ({
      id: el.id || '',
      name: el.name || '',
      placeholder: el.placeholder || '',
      label: attrs(el).replace(/\\s+/g, ' ').trim().slice(0, 100),
    }))
    .slice(0, 6);
  return {
    hasLine1,
    line1Values,
    manualLinkVisible,
    detailFieldCount: detailFields.length,
    detailValues,
    needsManualExpansion,
    invalids,
    ok: hasLine1 && !needsManualExpansion && detailComplete && invalids.length === 0,
  };
})()
""",
            timeout=3,
        )
        return state if isinstance(state, dict) else {"ok": False, "invalids": []}

    def fill_stripe_line1_dom(self, value: str) -> bool:
        return bool(
            self.js(
                f"""
(() => {{
  const value = {json.dumps(str(value))};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function attrs(el) {{
    return [
      el.id || '',
      el.name || '',
      el.placeholder || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('data-testid') || '',
      el.autocomplete || '',
      el.closest('label')?.innerText || '',
      el.parentElement?.innerText || ''
    ].join(' ');
  }}
  const candidates = [...document.querySelectorAll('input:not([type="hidden"]),textarea')]
    .filter(visible)
    .filter((el) => {{
      const a = attrs(el);
      return /address|住所|street|line1|billingAddressLine1/i.test(a) &&
        !/city|locality|postal|zip|country|state|phone|email|name|card|cvv|cvc|expiry/i.test(a);
    }});
  const el = candidates[0];
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  el.scrollIntoView({{block:'center', inline:'center'}});
  el.focus();
  if (setter) setter.call(el, '');
  else el.value = '';
  el.dispatchEvent(new Event('input', {{bubbles:true}}));
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:value}}));
  el.dispatchEvent(new Event('change', {{bubbles:true}}));
  return true;
}})()
""",
                timeout=3,
            )
        )

    def expand_stripe_manual_address(self) -> bool:
        point = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const patterns = [/enter address manually/i, /manual address/i, /住所を手動で入力/i, /手動で入力/i];
  const candidates = [...document.querySelectorAll('button,a,[role="button"],span,div')];
  let best = null;
  for (const el of candidates) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    if (!text || !patterns.some((p) => p.test(text))) continue;
    if (text.length > 80) continue;
    const target = el.closest('button,a,[role="button"]') || el;
    const r = target.getBoundingClientRect();
    const score = (target.tagName === 'BUTTON' || target.getAttribute('role') === 'button' ? 0 : 10) + text.length;
    if (!best || score < best.score) best = {target, text, score};
  }
  if (!best) return false;
  best.target.scrollIntoView({block:'center', inline:'center'});
  const r = best.target.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: best.text};
})()
""",
            timeout=2,
        )
        if point and point.get("x") is not None:
            log(f"[stripe] expand manual address: {point.get('text')}")
            self.click_point(point)
            time.sleep(0.35)
            return True
        return False

    def stripe_manual_address_expanded(self) -> bool:
        return bool(
            self.js(
                """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const selectors = [
    '#billingLocality',
    'input[name="billingLocality"]',
    '#billingPostalCode',
    'input[name="billingPostalCode"]',
    '#billingAdministrativeArea',
    'select[name="billingAdministrativeArea"]'
  ];
  return selectors.some((selector) => {
    try { return visible(document.querySelector(selector)); } catch (_) { return false; }
  });
})()
""",
                timeout=2,
            )
        )

    def ensure_stripe_billing_complete(self, address: dict[str, Any], timeout: float = 6.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        seed = self.address_lookup_seed(address)
        expanded = False
        last_signature = ""
        state = self.stripe_billing_state()
        while time.time() < deadline:
            signature = json.dumps(state, sort_keys=True)
            if signature == last_signature and state.get("hasLine1") and not state.get("ok") and not self.stripe_manual_address_expanded():
                expanded = False
            last_signature = signature
            if state.get("ok"):
                return state
            if state.get("hasLine1") and not state.get("ok") and not expanded:
                if self.expand_stripe_manual_address():
                    expanded = True
                    if not self.stripe_manual_address_expanded():
                        expanded = False
            if not state.get("hasLine1"):
                self.fill_stripe_line1_dom(seed)
                time.sleep(0.15)
                if not self.select_stripe_address_autocomplete():
                    self.press_key("ArrowDown")
                    time.sleep(0.08)
                    self.press_key("Enter")
                    time.sleep(0.12)
            state = self.stripe_billing_state()
            if state.get("ok"):
                return state
            if state.get("hasLine1") and not state.get("ok") and not expanded:
                if self.expand_stripe_manual_address():
                    expanded = True
                    if not self.stripe_manual_address_expanded():
                        expanded = False
                    state = self.stripe_billing_state()
            if not state.get("hasLine1"):
                self.fill_stripe_line1_dom(str(address["street"]))
            self.validate_stripe_completeness(address)
            state = self.stripe_billing_state()
            if state.get("ok"):
                return state
            time.sleep(0.25)
        return state

    def address_lookup_seed(self, address: dict[str, Any]) -> str:
        street = str(address.get("street") or "").strip()
        match = re.search(r"\d{2,8}", street)
        if match:
            return match.group(0)
        return street[:5] or "123"

    def fill_stripe_billing_address(self, address: dict[str, Any]) -> None:
        personal = self.profile.get("personalInfo") or {}
        full_name = f"{personal.get('firstName', '')} {personal.get('lastName', '')}".strip()
        japan_address = is_japan_address(address)
        if japan_address:
            self.ensure_stripe_country(address)
            seed = str(address["street"])
        else:
            seed = self.address_lookup_seed(address)
        line1 = [
            "#billingAddressLine1",
            "input[name='billingAddressLine1']",
            "input[name='billingAddress.line1']",
            "input[name='addressLine1']",
            "input[autocomplete='address-line1']",
            "input[placeholder*='address' i]",
            "input[placeholder*='住所']",
            "input[aria-label*='address' i]",
            "input[aria-label*='住所']",
        ]
        if not self.type_field(line1, seed, timeout=2, fast=True):
            self.fill_stripe_line1_dom(seed)
        time.sleep(random.uniform(0.12, 0.2))
        address_autofilled = False if japan_address else self.select_stripe_address_autocomplete()
        if not address_autofilled:
            if not japan_address:
                self.press_key("ArrowDown")
                time.sleep(0.15)
                self.press_key("Enter")
                time.sleep(random.uniform(0.2, 0.35))
        if len(self.field_value(line1).strip()) < 5:
            if not self.type_field(line1, address["street"], timeout=1, fast=True):
                self.fill_stripe_line1_dom(str(address["street"]))
        self.expand_stripe_manual_address()
        if japan_address and self.visible_selector(line1) and self.field_value(line1).strip() != str(address["street"]):
            if not self.type_field(line1, address["street"], timeout=1, fast=True):
                self.fill_stripe_line1_dom(str(address["street"]))
        line2_value = str(address.get("line2") or address.get("building") or "").strip()
        line2_selectors = [
            "#billingAddressLine2",
            "input[name='billingAddressLine2']",
            "input[name='billingAddress.line2']",
            "input[name='addressLine2']",
            "input[autocomplete='address-line2']",
            "input[placeholder*='住所 (2']",
            "input[aria-label*='住所 (2']",
        ]
        if japan_address and line2_value and self.visible_selector(line2_selectors) and self.field_value(line2_selectors).strip() != line2_value:
            self.type_field(line2_selectors, line2_value, timeout=1, fast=True)
        name_selectors = ["#billingName", "input[name='billingName']", "input[name='name']"]
        if full_name and self.visible_selector(name_selectors):
            self.type_field(name_selectors, full_name, timeout=1)
            time.sleep(random.uniform(0.12, 0.28))
        # If Stripe autocomplete filled locality/postal fields, do not rewrite them.
        city_selectors = ["#billingLocality", "input[name='billingLocality']"]
        if self.visible_selector(city_selectors) and (japan_address or not self.field_value(city_selectors)):
            self.type_field(city_selectors, address["city"], timeout=1)
        postal_selectors = ["#billingPostalCode", "input[name='billingPostalCode']"]
        if self.visible_selector(postal_selectors) and (japan_address or not self.field_value(postal_selectors)):
            self.type_field(postal_selectors, address["zipCode"], timeout=1)
        state_selectors = ["#billingAdministrativeArea", "select[name='billingAdministrativeArea']"]
        if self.visible_selector(state_selectors) and (japan_address or not self.field_value(state_selectors)):
            self.select_state(address["state"])
        state = self.ensure_stripe_billing_complete(address, timeout=4)
        if not state.get("ok"):
            raise RuntimeError(f"Stripe billing address incomplete: {state}")
        self.press_key("Escape")

    def ensure_stripe_country(self, address: dict[str, Any]) -> None:
        result = self.js(
            r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const selectors = [
    "select[name='billingAddressCountry']",
    "#billingAddressCountry",
    "select[name='billingCountry']",
    "#billingCountry",
    "select[name='billingAddress.country']",
    "select[autocomplete='country']",
    "select[autocomplete='country-name']",
    "select[name*='country' i]",
    "select[id*='country' i]"
  ];
  const desired = ['JP', 'JPN', 'Japan', '日本'];
  for (const selector of selectors) {
    let el = null;
    try { el = document.querySelector(selector); } catch (_) { el = null; }
    if (!visible(el)) continue;
    const options = [...el.options];
    const current = String(el.value || '').trim();
    const curOpt = options.find((o) => o.value === current);
    const curText = curOpt ? String(curOpt.textContent || '').trim() : '';
    if (desired.some((x) => current.toLowerCase() === x.toLowerCase() || curText.toLowerCase() === x.toLowerCase())) {
      return {ok:true, changed:false, selector, current, currentText:curText};
    }
    const found = options.find((opt) => desired.some((x) =>
      String(opt.value || '').trim().toLowerCase() === x.toLowerCase() ||
      String(opt.textContent || '').trim().toLowerCase() === x.toLowerCase()
    ));
    if (!found) return {ok:false, reason:'jp_option_not_found', selector, options: options.map((o) => ({value:o.value, text:o.textContent})).slice(0, 80)};
    el.value = found.value;
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return {ok:true, changed:true, selector, value:found.value, text:found.textContent};
  }
  return {ok:false, reason:'country_select_not_found'};
})()
""",
            timeout=5,
        )
        log(f"[stripe] ensure country JP: {result}")
        if isinstance(result, dict) and result.get("changed"):
            time.sleep(1.2)

    def stripe_paypal_selected(self) -> bool:
        return bool(
            self.js(
                """
(() => {
  const hasPaypal = (el) => /paypal/i.test([
    el.id || '',
    el.name || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('data-testid') || '',
    el.innerText || el.textContent || '',
    [...el.querySelectorAll?.('img,svg,use') || []].map((node) => [
      node.getAttribute('alt') || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('src') || '',
      node.getAttribute('href') || '',
      node.getAttribute('xlink:href') || '',
      node.className?.baseVal || node.className || ''
    ].join(' ')).join(' ')
  ].join(' '));
  for (const input of document.querySelectorAll('input[type="radio"], input[name*="payment"], input[id*="paypal" i], input[value*="paypal" i]')) {
    if (!input.checked) continue;
    const label = input.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null;
    const row = input.closest('label,[role="radio"],button,[data-testid],.PaymentMethodFormAccordionItem,.AccordionItem') || label;
    if (hasPaypal(input) || (label && hasPaypal(label)) || (row && hasPaypal(row))) return true;
  }
  for (const el of document.querySelectorAll('[role="radio"], [aria-checked], [data-testid*="paypal" i]')) {
    if (hasPaypal(el) && /true/i.test(el.getAttribute('aria-checked') || el.getAttribute('aria-selected') || el.getAttribute('data-selected') || '')) return true;
  }
  return false;
})()
""",
                timeout=2,
            )
        )

    def stripe_payment_method_required(self) -> bool:
        return bool(
            self.js(
                """
(() => /payment method required|select a payment method|payment method is required/i.test(document.body && document.body.innerText || ''))()
""",
                timeout=2,
            )
        )

    def select_stripe_paypal_method(self, timeout: int = 8) -> bool:
        if self.stripe_paypal_selected():
            return True
        script = """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [
      el.innerText || el.textContent || '',
      el.value || '',
      el.getAttribute('aria-label') || '',
      el.id || '',
      el.name || '',
      el.getAttribute('data-testid') || '',
      el.className?.baseVal || el.className || '',
      [...el.querySelectorAll?.('img,svg,use') || []].map((node) => [
        node.getAttribute('alt') || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('src') || '',
        node.getAttribute('href') || '',
        node.getAttribute('xlink:href') || '',
        node.className?.baseVal || node.className || ''
      ].join(' ')).join(' ')
    ]
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  function pointFor(el) {
    if (!el || !visible(el)) return null;
    el.scrollIntoView({block:'center', inline:'center'});
    const r = el.getBoundingClientRect();
    const x = r.left + Math.max(14, Math.min(36, r.width * 0.12));
    return {x, y: r.top + r.height / 2};
  }
  const direct = [
    '#payment-method-accordion-item-title-paypal',
    '.AccordionItemCover.PaymentMethodFormAccordionItem.paypal-accordion-item-cover',
    '[data-testid="paypal-payment-method"]',
    'button[aria-label*="PayPal" i]',
    'button[title*="PayPal" i]',
    'div[role="radio"][aria-label*="PayPal" i]',
    'label[for*="paypal" i]',
    'input[id*="paypal" i]',
    'input[value*="paypal" i]',
    '[data-testid*="paypal" i]'
  ];
  for (const selector of direct) {
    const el = document.querySelector(selector);
    if (!el) continue;
    const row = el.closest('label,[role="radio"],button,[data-testid],.PaymentMethodFormAccordionItem,.AccordionItem') || el;
    const p = pointFor(row);
    if (p && /paypal/i.test(label(row))) return {...p, selector};
  }
  const candidates = [...document.querySelectorAll('label,button,[role="radio"],[role="button"],[data-testid],.AccordionItemCover,.PaymentMethodFormAccordionItem,div,li,input[type="radio"]')];
  for (const el of candidates) {
    if (!visible(el)) continue;
    const text = label(el);
    if (!/paypal/i.test(text)) continue;
    const row = el.closest('label,[role="radio"],button,[data-testid],.PaymentMethodFormAccordionItem,.AccordionItem') || el;
    const p = pointFor(row);
    if (p) return {...p, selector:'paypal-text-row'};
  }
  return false;
})()
"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            point = self.js(script, timeout=2)
            if point and point.get("x") is not None:
                log(f"[stripe] select PayPal method: {point.get('selector')}")
                self.click_point(point)
                end = time.time() + 2.5
                while time.time() < end:
                    if self.stripe_paypal_selected():
                        return True
                    time.sleep(0.2)
            time.sleep(0.25)
        return self.stripe_paypal_selected()

    def submit_stripe_checkout(self, timeout: int = 4) -> bool:
        """Click Stripe submit quickly after validation, then let post-submit wait handle risk."""
        script = """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const selectors = [
    "button[data-testid='hosted-payment-submit-button']",
    "[data-testid='hosted-payment-submit-button']",
    ".SubmitButton-IconContainer",
    "button[type='submit']"
  ];
  for (const selector of selectors) {
    const raw = document.querySelector(selector);
    if (!raw || !visible(raw)) continue;
    const btn = raw.closest('button') || raw;
    if (!visible(btn) || btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
    btn.scrollIntoView({block:'center', inline:'center'});
    const r = btn.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2, selector};
  }
  const byText = [...document.querySelectorAll('button, input[type=submit], [role=button]')]
    .find((el) => visible(el) && !el.disabled && /subscribe|process|continue|申し込|プロセス/i.test(
      [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || ''].join(' ')
    ));
  if (!byText) return false;
  byText.scrollIntoView({block:'center', inline:'center'});
  const r = byText.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, selector:'text-submit'};
})()
"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            point = self.js(script, timeout=2)
            if point and point.get("x") is not None:
                log(f"[stripe] submit button ready: {point.get('selector')}")
                self.click_point(point)
                return True
            time.sleep(0.25)
        submitted = bool(
            self.js(
                """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const btn =
    document.querySelector("button[data-testid='hosted-payment-submit-button']") ||
    document.querySelector('.SubmitButton-IconContainer')?.closest('button') ||
    document.querySelector('button[type=submit]');
  if (!btn || !visible(btn) || btn.disabled) return false;
  if (document.activeElement) document.activeElement.blur();
  btn.click();
  const form = btn.closest('form');
  if (form) form.requestSubmit ? form.requestSubmit(btn) : form.submit();
  return true;
})()
""",
                timeout=3,
            )
        )
        if submitted:
            log("[stripe] submit fallback used")
        return submitted

    def wait_after_stripe_submit(self, min_wait: float = 1.0, max_wait: float = 5.5) -> bool:
        """Keep short post-submit dwell, but do not burn the old fixed 6-8s wait."""
        needles = ["paypal.com/pay", "paypal.com/agreements/approve", "paypal.com/checkoutweb/signup"]
        started = time.time()
        deadline = started + max_wait
        next_move = started
        while time.time() < deadline:
            url = self.current_url()
            if time.time() - started >= min_wait and any(n in url for n in needles):
                return True
            if time.time() >= next_move:
                if self.args.human_profile == "off" or not self.page:
                    time.sleep(0.12)
                else:
                    try:
                        viewport = self.js("(() => ({w: window.innerWidth || 1280, h: window.innerHeight || 900}))()", timeout=1) or {}
                        w = int(viewport.get("w") or 1280)
                        h = int(viewport.get("h") or 900)
                        point = {
                            "x": max(20, min(w - 20, w * 0.5 + random.randint(-80, 80))),
                            "y": max(20, min(h - 20, h * 0.55 + random.randint(-45, 45))),
                        }
                        self.page.actions.human_move(point, style=random.choice(["arc", "line_then_arc", None])).perform()
                    except Exception:
                        pass
                next_move = time.time() + random.uniform(0.7, 1.2)
            time.sleep(0.2)
        if self.wait_for_url_any(needles, timeout=1):
            return True
        processing = self.stripe_submit_processing_state()
        if processing.get("processing"):
            log(f"[stripe] submit still processing; wait for redirect: {processing}")
            extra_deadline = time.time() + 22
            while time.time() < extra_deadline:
                url = self.current_url()
                if any(n in url for n in needles):
                    return True
                if self.wait_for_url_any(needles, timeout=0.5):
                    return True
                processing = self.stripe_submit_processing_state()
                if not processing.get("processing") and time.time() - started >= min_wait:
                    break
                time.sleep(0.25)
        return self.wait_for_url_any(needles, timeout=1)

    def stripe_submit_processing_state(self) -> dict[str, Any]:
        try:
            state = self.js(
                r"""
(() => {
  const btn =
    document.querySelector("button[data-testid='hosted-payment-submit-button']") ||
    document.querySelector('.SubmitButton-IconContainer')?.closest('button') ||
    document.querySelector('button[type=submit]');
  if (!btn) return {processing:false, reason:'submit_not_found'};
  const text = [btn.innerText || btn.textContent || '', btn.value || '', btn.getAttribute('aria-label') || '']
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
  const disabled = Boolean(btn.disabled || btn.getAttribute('aria-disabled') === 'true');
  const processing = disabled && /process|processing|プロセス|処理|送信中/i.test(text);
  return {processing, disabled, text};
})()
""",
                timeout=2,
            )
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def save_stripe_checkout_diagnostics(self, label: str) -> None:
        try:
            data = self.js(
                r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function labelFor(el) {
    const id = el.id || '';
    const labels = id ? [...document.querySelectorAll(`label[for="${CSS.escape(id)}"]`)].map((x) => x.innerText || x.textContent || '') : [];
    const parent = el.closest('label');
    if (parent) labels.push(parent.innerText || parent.textContent || '');
    const group = el.closest('[data-testid], [role], form, section, div');
    if (group) labels.push((group.innerText || group.textContent || '').slice(0, 240));
    return labels.join(' | ').replace(/\s+/g, ' ').trim();
  }
  function attrs(el) {
    return {
      tag: el.tagName,
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      role: el.getAttribute('role') || '',
      testid: el.getAttribute('data-testid') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      checked: Boolean(el.checked),
      disabled: Boolean(el.disabled),
      value: String(el.value || '').slice(0, 80),
      text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 180),
      label: labelFor(el),
    };
  }
  const fields = [...document.querySelectorAll('input,select,textarea,button,[role="radio"],[role="button"],[data-testid]')]
    .filter(visible)
    .slice(0, 180)
    .map(attrs);
  const paypalHints = fields.filter((x) => /paypal/i.test([x.id, x.name, x.testid, x.aria, x.value, x.text, x.label].join(' '))).slice(0, 40);
  const errors = [...document.querySelectorAll('[role="alert"], .Error, .error, [class*="error" i], [data-testid*="error" i]')]
    .filter(visible)
    .map((el) => String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .slice(0, 40);
  return {
    url: location.href,
    title: document.title,
    active: document.activeElement ? attrs(document.activeElement) : null,
    paypalHints,
    errors,
    fields,
    text: (document.body && document.body.innerText || '').slice(0, 3500),
  };
})()
""",
                timeout=5,
            )
            out = Path(self.args.result_json).with_name(f"{Path(self.args.result_json).stem}_{label}.json")
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"[stripe] checkout diagnostic saved: {out}")
        except Exception as exc:
            log(f"[stripe] checkout diagnostic failed: {type(exc).__name__}: {exc}")

    def choose_paypal_on_stripe(self, address: dict[str, Any]) -> bool:
        self.wait_stripe_content_or_refresh(timeout=90, blank_seconds=8)
        try:
            self.wait_stripe_amount_sample(timeout=120)
        except Exception:
            self.save_stripe_checkout_diagnostics("stripe_amount_check_failed")
            raise
        if not self.select_stripe_paypal_method(timeout=8):
            self.save_stripe_checkout_diagnostics("stripe_paypal_select_failed")
            return False
        self.human_pause(0.2, 0.45)
        self.fill_stripe_billing_address(address)
        self.js(
            """
(() => {
  for (const el of document.querySelectorAll('#termsOfServiceConsentCheckbox,input[name="termsOfServiceConsentCheckbox"]')) {
    if (!el.checked) {
      el.click();
      el.dispatchEvent(new Event('change', {bubbles:true}));
    }
  }
  return true;
})()
""",
            timeout=3,
        )
        if not self.stripe_paypal_selected():
            log("[stripe] PayPal not selected after billing fill; retry select")
            if not self.select_stripe_paypal_method(timeout=4):
                self.save_stripe_checkout_diagnostics("stripe_paypal_lost_after_billing")
                return False
        billing_state = self.ensure_stripe_billing_complete(address, timeout=5)
        if not billing_state.get("ok"):
            raise RuntimeError(f"Stripe billing address incomplete before submit: {billing_state}")
        self.human_pause(0.1, 0.3)
        for attempt in range(2):
            clicked = self.submit_stripe_checkout(timeout=4)
            if clicked and self.wait_after_stripe_submit(min_wait=1.0, max_wait=5.5):
                return True
            if not clicked and self.wait_for_url_any(["paypal.com/pay", "paypal.com/agreements/approve", "paypal.com/checkoutweb/signup"], timeout=20):
                return True
            if self.stripe_payment_method_required():
                log("[stripe] payment method still required; reselect PayPal and resubmit")
                if not self.select_stripe_paypal_method(timeout=5):
                    self.save_stripe_checkout_diagnostics("stripe_payment_method_required_after_submit")
                    break
                self.human_pause(0.15, 0.35)
                continue
            if attempt == 0 and not self.stripe_paypal_selected():
                log("[stripe] no PayPal redirect and method not selected; retry select")
                if self.select_stripe_paypal_method(timeout=5):
                    continue
            break
        if not self.stripe_paypal_selected():
            self.save_stripe_checkout_diagnostics("stripe_paypal_submit_no_redirect")
            return False
        submitted = bool(
            self.js(
                """
(() => {
  const btn =
    document.querySelector("button[data-testid='hosted-payment-submit-button']") ||
    document.querySelector('.SubmitButton-IconContainer')?.closest('button') ||
    document.querySelector('button[type=submit]');
  if (!btn || btn.disabled) return false;
  if (document.activeElement) document.activeElement.blur();
  btn.click();
  const form = btn.closest('form');
  if (form) form.requestSubmit ? form.requestSubmit(btn) : form.submit();
  return true;
})()
""",
                timeout=3,
            )
        )
        if submitted:
            advanced = self.wait_after_stripe_submit(min_wait=1.0, max_wait=5.5)
            if not advanced:
                self.save_stripe_checkout_diagnostics("stripe_submit_no_redirect")
            return advanced
        self.save_stripe_checkout_diagnostics("stripe_submit_failed")
        return False

    def run(self) -> dict[str, Any]:
        personal = self.profile.get("personalInfo") or {}
        contact = self.profile.get("contact") or {}
        address = self.profile["address"]
        card = load_card(self.args.card_json)
        additional = self.profile.get("additional") or {}
        additional.setdefault("password", "Aa" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(10)) + "1!")
        exp_month, exp_year = split_expiry(card["expiry"])
        paypal_contact = dict(contact)
        paypal_contact["email"] = random_gmail()
        log(f"[paypal] generated email: {paypal_contact['email']}")

        log("[step] open start url")
        self.navigate(self.args.start_url)
        self.wait_stripe_content_or_refresh(timeout=90, blank_seconds=8)
        self.wait_text(r"OpenAI|Stripe|PayPal|Checkout|支払い方法", timeout=30)

        log("[step] choose PayPal on Stripe checkout")
        if not self.choose_paypal_on_stripe(address):
            raise RuntimeError("Stripe PayPal payment method was not found or could not be selected")
        self.close_auxiliary_tabs()
        self.wait_url_contains(["paypal.com/pay", "paypal.com/agreements/approve", "paypal.com/checkoutweb/signup"], timeout=120)

        if "agreements/approve" in self.page_info().get("url", ""):
            log("[step] move from PayPal login to signup/guest checkout")
        self.wait_paypal_signup_ready(paypal_contact, timeout=140)

        # Acquire paid SMS numbers only after PayPal's signup form is actually reachable.
        sms_phone, sms_api = parse_sms_line(self.args.sms_line)
        paypal_first_name = str(personal.get("firstName") or random_letters(5)).strip()
        paypal_last_name = str(personal.get("lastName") or random_letters(6)).strip()
        if is_japan_address(address):
            paypal_first_name = str(personal.get("firstNameKanji") or personal.get("firstName") or "太郎").strip()
            paypal_last_name = str(personal.get("lastNameKanji") or personal.get("lastName") or "田中").strip()
        paypal_submit_phone = paypal_phone_for_country(sms_phone, address_country(address))

        sms_poller = None
        sms_baseline = ""
        for form_attempt in range(2):
            log("[step] fill PayPal signup form" + (" (retry)" if form_attempt else ""))
            self.fill_signup_form(
                personal,
                paypal_contact,
                address,
                additional,
                card,
                sms_phone,
                exp_month,
                exp_year,
                paypal_first_name,
                paypal_last_name,
            )
            self.safe_handle_captcha_if_present(detect_wait=1)
            sms_baseline = capture_sms_baseline(sms_api)
            if not sms_api.startswith("manual://"):
                sms_poller = SmsCodePoller(
                    sms_api,
                    baseline=sms_baseline,
                    timeout=int(self.args.sms_timeout),
                    interval=1.0,
                )
            try:
                self.submit_paypal_signup(paypal_submit_phone, paypal_first_name, paypal_last_name)
                break
            except FlowFailed as exc:
                if sms_poller:
                    sms_poller.stop()
                    sms_poller = None
                if (
                    form_attempt == 0
                    and isinstance(exc.result, dict)
                    and exc.result.get("reason") == "paypal_required_fields_invalid_before_submit"
                ):
                    log("[paypal] required fields still invalid; refresh signup form and retry once")
                    if self.page:
                        self.page.refresh()
                    self.wait_ready(timeout=30)
                    self.wait_paypal_signup_ready(paypal_contact, timeout=60)
                    continue
                raise

        self.safe_handle_captcha_if_present(detect_wait=8)
        sms_ok = self.handle_sms_if_present(sms_api, sms_baseline, sms_poller)
        if sms_poller:
            sms_poller.stop()
        sms_failure = self.fail_if_sms_pending_without_code(sms_ok)
        if sms_failure is not None:
            return sms_failure
        self.safe_handle_captcha_if_present(detect_wait=1)
        final_state = self.handle_final_confirmation_if_present()
        if final_state == "login_fallback":
            log("[paypal] Hermes review fell back to login; retry onboarding/signup once")
            result = self.retry_paypal_onboarding_after_review_fallback(
                paypal_contact,
                personal,
                address,
                additional,
                card,
                sms_phone,
                sms_api,
                exp_month,
                exp_year,
                paypal_first_name,
                paypal_last_name,
            )
            if result is not None:
                return result
        if final_state == "no_eligible_funding":
            return self.annotate_result(
                {
                    "status": "failed",
                    "reason": "paypal_no_eligible_funding",
                    "url": self.current_url(),
                    "text": str(self.page_info().get("text") or "")[:1200],
                }
            )

        log("[step] wait for final redirect/result")
        result = self.wait_final_result(timeout=180)
        if result.get("reason") == "paypal_signup_not_advanced_after_sms":
            log("[paypal] still on signup form after SMS; retry same PayPal form once")
            self.fill_signup_form(
                personal,
                paypal_contact,
                address,
                additional,
                card,
                sms_phone,
                exp_month,
                exp_year,
                paypal_first_name,
                paypal_last_name,
            )
            self.safe_handle_captcha_if_present(detect_wait=1)
            retry_baseline = capture_sms_baseline(sms_api)
            retry_poller = None
            if not sms_api.startswith("manual://"):
                retry_poller = SmsCodePoller(
                    sms_api,
                    baseline=retry_baseline,
                    timeout=int(self.args.sms_timeout),
                    interval=1.0,
            )
            try:
                self.submit_paypal_signup(paypal_submit_phone, paypal_first_name, paypal_last_name)
                self.safe_handle_captcha_if_present(detect_wait=8)
                retry_sms_ok = self.handle_sms_if_present(sms_api, retry_baseline, retry_poller)
            finally:
                if retry_poller:
                    retry_poller.stop()
            retry_sms_failure = self.fail_if_sms_pending_without_code(retry_sms_ok)
            if retry_sms_failure is not None:
                return retry_sms_failure
            self.safe_handle_captcha_if_present(detect_wait=1)
            self.handle_final_confirmation_if_present()
            log("[step] wait for final redirect/result after PayPal form retry")
            result = self.wait_final_result(timeout=180)
        return result

    def retry_paypal_onboarding_after_review_fallback(
        self,
        paypal_contact: dict[str, Any],
        personal: dict[str, Any],
        address: dict[str, Any],
        additional: dict[str, Any],
        card: dict[str, Any],
        sms_phone: str,
        sms_api: str,
        exp_month: str,
        exp_year: str,
        first_name: str,
        last_name: str,
    ) -> dict[str, Any] | None:
        try:
            self.wait_paypal_signup_ready(paypal_contact, timeout=60)
        except Exception as exc:
            log(f"[paypal] review fallback onboarding recovery failed: {type(exc).__name__}: {exc}")
            return None
        self.fill_signup_form(
            personal,
            paypal_contact,
            address,
            additional,
            card,
            sms_phone,
            exp_month,
            exp_year,
            first_name,
            last_name,
        )
        self.safe_handle_captcha_if_present(detect_wait=1)
        baseline = capture_sms_baseline(sms_api)
        poller = None
        if not sms_api.startswith("manual://"):
            poller = SmsCodePoller(
                sms_api,
                baseline=baseline,
                timeout=int(self.args.sms_timeout),
                interval=1.0,
            )
        try:
            self.submit_paypal_signup(paypal_phone_for_country(sms_phone, address_country(address)), first_name, last_name)
            self.safe_handle_captcha_if_present(detect_wait=8)
            sms_ok = self.handle_sms_if_present(sms_api, baseline, poller)
        finally:
            if poller:
                poller.stop()
        sms_failure = self.fail_if_sms_pending_without_code(sms_ok)
        if sms_failure is not None:
            return sms_failure
        self.safe_handle_captcha_if_present(detect_wait=1)
        self.handle_final_confirmation_if_present()
        log("[step] wait for final redirect/result after Hermes login fallback retry")
        return self.wait_final_result(timeout=180)

    def wait_paypal_signup_ready(self, contact: dict[str, Any], timeout: int = 120) -> None:
        def state() -> dict[str, Any]:
            return self.js(
                r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const buttonLabels = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
    .filter(visible)
    .map((el) => [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || ''].join(' ').replace(/\\s+/g, ' ').trim());
  const q = (sel) => {
    try { return document.querySelector(sel); } catch (_) { return null; }
  };
  const text = document.body && document.body.innerText || '';
  const onboardingEmailEl = q('#onboardingFlowEmail') || q('#login_email') || q("input[placeholder='Enter email']");
  const createLabels = buttonLabels.filter((x) => /(Create\\s+(an?\\s+)?Account|アカウント作成|アカウントを作成)/i.test(x) && !/Agree|Continue|Payment|同意/i.test(x));
  return {
    url: location.href,
    title: document.title,
    text: text.slice(0, 1200),
    cardReady: visible(q('#cardNumber')) || visible(q("input[name='cardNumber']")),
    detailReady: visible(q('#firstName')) || visible(q('#lastName')) || visible(q('#phone')) || visible(q('#billingLine1')),
    signupSubmitReady: buttonLabels.some((x) => /Agree\s*&\s*Create Account|Agree and Create Account|同意.*アカウント|アカウント.*作成|同意して.*作成|同意して続行/i.test(x)),
    onboardingEmail: visible(onboardingEmailEl),
    onboardingEmailValue: onboardingEmailEl ? String(onboardingEmailEl.value || '') : '',
    createButton: visible(q('#startOnboardingFlow')) || visible(q('#guestCheckout')) || createLabels.length > 0,
    createButtonText: createLabels[0] || '',
	    continueButton: buttonLabels.some((x) => /^Continue$/i.test(x) || /Continue to Payment|続行|支払いに進む|支払いを続ける/i.test(x)),
    paypalClientCfci: /paypal_client_cfci/i.test(location.href),
    captcha: /authchallenge/i.test(location.href) || /captcha|robot|security check|Security Challenge/i.test(text),
    buttons: buttonLabels.slice(0, 20)
  };
})()
""",
                timeout=5,
            ) or {}

        def submit_onboarding_email() -> bool:
            email_selectors = [
                "#onboardingFlowEmail",
                "#login_email",
                "input[placeholder='Enter email']",
            ]
            if self.field_value(email_selectors) != contact["email"] and not self.type_field(email_selectors, contact["email"], timeout=6):
                return False
            time.sleep(0.4)
            return self.click_by_text([r"^Continue to Payment$", r"continue to payment", r"^continue$", r"支払いに進む", r"支払いを続ける", r"続行"], timeout=6)

        deadline = time.time() + timeout
        last_url = ""
        pay_url_seen_at = 0.0
        approve_url_seen_at = 0.0
        pay_fallback_clicks = 0
        first_create_click_at = 0.0
        last_create_click_at = 0.0
        last_email_submit_at = 0.0
        first_email_submit_at = 0.0
        email_submit_probe_until = 0.0
        approve_stall_logged_at = 0.0
        jp_signup_ready_since = 0.0
        while time.time() < deadline:
            cur = state()
            url = str(cur.get("url") or "")
            if url != last_url:
                log(f"[paypal] onboarding url: {url}")
                last_url = url
                pay_url_seen_at = time.time() if "paypal.com/pay" in url else 0.0
                approve_url_seen_at = time.time() if "paypal.com/agreements/approve" in url else 0.0
                approve_stall_logged_at = 0.0
                pay_fallback_clicks = 0
            if cur.get("cardReady") or cur.get("detailReady"):
                if "country.x=JP" in url or "locale.x=ja_JP" in url or "日本" in str(cur.get("text") or ""):
                    full_ready = bool(cur.get("cardReady") and cur.get("detailReady") and cur.get("signupSubmitReady"))
                    if full_ready:
                        if not jp_signup_ready_since:
                            jp_signup_ready_since = time.time()
                            time.sleep(0.25)
                            continue
                        if time.time() - jp_signup_ready_since >= 1.0:
                            return
                    else:
                        jp_signup_ready_since = 0.0
                        time.sleep(0.25)
                        continue
                return
            if cur.get("onboardingEmail"):
                if time.time() < email_submit_probe_until:
                    time.sleep(0.2)
                    continue
                if cur.get("captcha"):
                    self.safe_handle_captcha_if_present(detect_wait=1)
                if first_email_submit_at and time.time() - first_email_submit_at > 12:
                    self.save_paypal_signup_diagnostics("paypal_onboarding_email_stalled")
                    if cur.get("captcha"):
                        raise FlowFailed(
                            self.annotate_result(
                                {
                                    "status": "failed",
                                    "reason": "paypal_onboarding_authchallenge",
                                    "url": url,
                                }
                            )
                        )
                    raise RuntimeError("PayPal onboarding email did not advance after submit")
                if time.time() - last_email_submit_at >= 3.0:
                    log("[paypal] submit onboarding email")
                    if not first_email_submit_at:
                        first_email_submit_at = time.time()
                    last_email_submit_at = time.time()
                    email_submit_probe_until = time.time() + 1.4
                    if submit_onboarding_email():
                        time.sleep(1.2)
                        continue
                time.sleep(0.25)
                continue
            if cur.get("createButton"):
                if not first_create_click_at:
                    first_create_click_at = time.time()
                if "paypal.com/agreements/approve" in url and time.time() - first_create_click_at > 32:
                    self.save_paypal_signup_diagnostics("paypal_create_account_stalled")
                    raise RuntimeError(f"PayPal Create Account did not advance after {time.time() - first_create_click_at:.1f}s")
                if time.time() - last_create_click_at < 2.8:
                    time.sleep(0.5)
                    continue
                is_cfci = bool(cur.get("paypalClientCfci")) or "paypal_client_cfci" in url
                allow_geometry = not is_cfci
                allow_hard_fallback = "paypal.com/pay" in url and not is_cfci
                log("[paypal] click Create an Account")
                last_create_click_at = time.time()
                if self.click_paypal_create_account(allow_geometry=allow_geometry, allow_hard_fallback=allow_hard_fallback):
                    time.sleep(random.uniform(1.0, 1.6))
                else:
                    time.sleep(0.7)
                continue
            if (
                "paypal.com/pay" in url
                and "paypal_client_cfci" not in url
                and pay_url_seen_at
                and time.time() - pay_url_seen_at > 4
                and time.time() - last_create_click_at >= 2.8
                and pay_fallback_clicks < 3
                and not cur.get("onboardingEmail")
            ):
                pay_fallback_clicks += 1
                last_create_click_at = time.time()
                self.click_paypal_create_account(allow_geometry=True, allow_hard_fallback=True)
                time.sleep(random.uniform(1.2, 1.8))
                continue
            if cur.get("captcha") and self.args.no_captcha_handling:
                raise FlowFailed(
                    {
                        "status": "failed",
                        "reason": "paypal_security_challenge_blocked",
                        "url": url,
                        "title": cur.get("title"),
                        "text": str(cur.get("text") or "")[:800],
                    }
                )
            if not self.args.no_captcha_handling and (self.captcha_state().get("detected") or cur.get("captcha")):
                self.safe_handle_captcha_if_present()
                time.sleep(1)
                continue
            if "paypal.com/agreements/approve" in url and approve_url_seen_at and time.time() - approve_url_seen_at > 8:
                stalled_for = time.time() - approve_url_seen_at
                if stalled_for > 32:
                    raise RuntimeError(f"PayPal agreements/approve stalled before signup after {stalled_for:.1f}s")
                if time.time() - approve_stall_logged_at > 6:
                    log(f"[paypal] onboarding stalled; waiting without reopening ({stalled_for:.1f}s)")
                    approve_stall_logged_at = time.time()
                time.sleep(1)
                continue
            time.sleep(1)
        self.save_paypal_signup_diagnostics("paypal_signup_ready_timeout")
        raise TimeoutError("PayPal signup form did not become ready")

    def save_paypal_signup_diagnostics(self, label: str) -> None:
        try:
            data = self.js(
                r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function labelFor(el) {
    const id = el.id || '';
    const labels = id ? [...document.querySelectorAll(`label[for="${CSS.escape(id)}"]`)].map((x) => x.innerText || x.textContent || '') : [];
    const parent = el.closest('label');
    if (parent) labels.push(parent.innerText || parent.textContent || '');
    const group = el.closest('[class*="field"], [class*="form"], div, li');
    if (group) labels.push((group.innerText || group.textContent || '').slice(0, 160));
    return labels.join(' | ').replace(/\s+/g, ' ').trim();
  }
  const inputs = [...document.querySelectorAll('input,select,textarea')]
    .filter(visible)
    .map((el) => ({
      tag: el.tagName,
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      placeholder: el.getAttribute('placeholder') || '',
      aria: el.getAttribute('aria-label') || '',
      value: String(el.value || '').slice(0, 40),
      label: labelFor(el),
      options: el.tagName === 'SELECT' ? [...el.options].slice(0, 80).map((o) => ({value:o.value, text:o.textContent})) : []
    }));
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button]')]
    .filter(visible)
    .map((el) => [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || ''].join(' ').replace(/\s+/g, ' ').trim())
    .slice(0, 40);
  return {url: location.href, title: document.title, inputs, buttons, text: (document.body && document.body.innerText || '').slice(0, 2000)};
})()
""",
                timeout=5,
            )
            out = Path(self.args.result_json).with_name(f"{Path(self.args.result_json).stem}_{label}.json")
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"[paypal] signup diagnostic saved: {out}")
        except Exception as exc:
            log(f"[paypal] signup diagnostic failed: {type(exc).__name__}: {exc}")

    def ensure_paypal_country(self, address: dict[str, Any]) -> None:
        country = address_country(address)
        if country not in {"JP", "JPN", "JAPAN", "日本"}:
            return
        result = self.js(
            r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const selectors = [
    "select[name='country']",
    "#country",
    "select[name='countryCode']",
    "#countryCode",
    "select[name='billingCountry']",
    "#billingCountry",
    "select[name='billingAddress.country']",
    "select[autocomplete='country']",
    "select[autocomplete='country-name']",
    "select[name*='country' i]",
    "select[id*='country' i]"
  ];
  const desired = ['JP', 'JPN', 'Japan', '日本'];
  for (const selector of selectors) {
    let el = null;
    try { el = document.querySelector(selector); } catch (_) { el = null; }
    if (!visible(el)) continue;
    const current = String(el.value || '').trim();
    const options = [...el.options];
    const curOpt = options.find((o) => o.value === current);
    const curText = curOpt ? String(curOpt.textContent || '').trim() : '';
    if (desired.some((x) => current.toLowerCase() === x.toLowerCase() || curText.toLowerCase() === x.toLowerCase())) {
      return {ok:true, changed:false, selector, current, currentText:curText};
    }
    const found = options.find((opt) => desired.some((x) =>
      String(opt.value || '').trim().toLowerCase() === x.toLowerCase() ||
      String(opt.textContent || '').trim().toLowerCase() === x.toLowerCase()
    ));
    if (!found) return {ok:false, changed:false, selector, reason:'jp_option_not_found', options: options.map((o) => ({value:o.value, text:o.textContent})).slice(0, 80)};
    el.value = found.value;
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return {ok:true, changed:true, selector, value:found.value, text:found.textContent};
  }
  return {ok:false, changed:false, reason:'country_select_not_found'};
})()
""",
            timeout=5,
        )
        log(f"[paypal] ensure country JP: {result}")
        if isinstance(result, dict) and result.get("changed"):
            time.sleep(1.6)
            self.wait_ready(timeout=15)

    def fill_japanese_name_fields(self, personal: dict[str, Any]) -> dict[str, Any]:
        values = {
            "firstKana": str(personal.get("firstNameKana") or "タロウ"),
            "lastKana": str(personal.get("lastNameKana") or "タナカ"),
            "firstKanji": str(personal.get("firstNameKanji") or personal.get("firstName") or "太郎"),
            "lastKanji": str(personal.get("lastNameKanji") or personal.get("lastName") or "田中"),
        }
        result = self.js(
            f"""
(() => {{
  const values = {json.dumps(values, ensure_ascii=False)};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function textFor(el) {{
    const parts = [el.id || '', el.name || '', el.autocomplete || '', el.placeholder || '', el.getAttribute('aria-label') || ''];
    if (el.id) {{
      parts.push(...[...document.querySelectorAll(`label[for="${{CSS.escape(el.id)}}"]`)].map((x) => x.innerText || x.textContent || ''));
    }}
    const parent = el.closest('label');
    if (parent) parts.push(parent.innerText || parent.textContent || '');
    const group = el.closest('[class*="field"], [class*="form"], div, li');
    if (group) parts.push((group.innerText || group.textContent || '').slice(0, 180));
    return parts.join(' ').replace(/\\s+/g, ' ');
  }}
	  function setValue(el, value) {{
	    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
	    el.scrollIntoView({{block:'center', inline:'center'}});
	    el.focus();
	    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:value}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
	  }}
	  const out = {{}};
	  const direct = [
	    ['firstKana', '#countrySpecificFirstName'],
	    ['lastKana', '#countrySpecificLastName'],
	    ['firstKanji', '#firstName'],
	    ['lastKanji', '#lastName']
	  ];
	  for (const [key, selector] of direct) {{
	    const el = document.querySelector(selector);
	    if (visible(el)) {{
	      setValue(el, values[key]);
	      out[key] = {{id: el.id || '', name: el.name || '', value: values[key], selector}};
	    }}
	  }}
	  const inputs = [...document.querySelectorAll('input')].filter(visible);
	  for (const el of inputs) {{
	    if (/billing|address|postal|city|phone|card|cvv|expiry|birth|password|email/i.test([el.id || '', el.name || '', el.autocomplete || ''].join(' '))) continue;
	    const t = textFor(el);
	    if (!/(name|first|last|given|family|姓|名|カナ|かな|フリガナ|氏名)/i.test(t)) continue;
	    const isKana = /(kana|カナ|かな|フリガナ)/i.test(t);
    const isLast = /(last|family|surname|姓|sei)/i.test(t);
    const isFirst = /(first|given|名|mei)/i.test(t) && !isLast;
    let key = '';
    if (isKana && isLast) key = 'lastKana';
    else if (isKana && isFirst) key = 'firstKana';
    else if (!isKana && isLast) key = 'lastKanji';
    else if (!isKana && isFirst) key = 'firstKanji';
    if (!key || out[key]) continue;
    setValue(el, values[key]);
    out[key] = {{id: el.id || '', name: el.name || '', value: values[key], text: t.slice(0, 120)}};
  }}
  return out;
}})()
""",
            timeout=5,
        )
        return result if isinstance(result, dict) else {}

    def fill_signup_form(
        self,
        personal,
        contact,
        address,
        additional,
        card,
        sms_phone,
        exp_month,
        exp_year,
        first_name: str,
        last_name: str,
    ) -> None:
        japan_address = is_japan_address(address)
        if japan_address:
            self.save_paypal_signup_diagnostics("paypal_jp_signup_before_fill")
            self.ensure_paypal_country(address)
        paypal_phone = paypal_phone_for_country(sms_phone, address_country(address))
        birth_date = str(
            personal.get("dateOfBirth")
            or personal.get("birthDate")
            or additional.get("dateOfBirth")
            or additional.get("birthDate")
            or "19800101"
        )
        fields = [
            ("email", ["input[name='email']", "#email", "#login_email", "#onboardingFlowEmail", "input[name='login_email'][type='email']", "input[type='email']"], contact["email"]),
            ("phone", ["#phone", "input[name='phoneNumber']", "input[type='tel']", "input[name='phone']"], paypal_phone),
            ("password", ["input[name='password']", "#password", "input[type='password']"], additional["password"]),
            ("firstName", ["#firstName", "input[name='firstName']", "input[name='fname']", "#cardFirstName", "input[autocomplete='given-name']", "input[aria-label='First name']"], first_name),
            ("lastName", ["#lastName", "input[name='lastName']", "input[name='lname']", "#cardLastName", "input[autocomplete='family-name']", "input[aria-label='Last name']"], last_name),
            ("cardNumber", ["input[name='cardNumber']", "#cardNumber", "input[aria-label='Card number']"], card["cardNumber"]),
            ("expiryDate", ["input[name='expiryDate']", "#expiryDate", "#cardExpiry", "input[aria-label='MM / YY']"], f"{exp_month}/{exp_year}"),
            ("cvv", ["input[name='cvvNumber']", "#cvv", "#cardCvv", "input[aria-label='CSC']"], str(card["cvv"])),
        ]
        if japan_address:
            fields.append(("dateOfBirth", ["#dateOfBirth", "input[name='dateOfBirth']", "input[aria-label*='生年月日']", "input[placeholder*='生年月日']"], birth_date))
        line1_selectors = ["input[name='line1']", "#line1", "#billingLine1", "input[autocomplete='address-line1']"]
        city_selectors = ["input[name='city']", "#city", "#billingCity", "input[autocomplete='address-level2']"]
        postal_selectors = ["input[name='postalCode']", "#postalCode", "#billingPostalCode", "#zipCode", "input[autocomplete='postal-code']"]
        paypal_line1 = str(address["street"]) if japan_address else self.address_lookup_seed(address)
        fast_fields = [(name, selectors, str(value)) for name, selectors, value in fields]
        fast_fields.extend(
            [
                ("line1", line1_selectors, paypal_line1),
                ("city", city_selectors, str(address["city"])),
                ("postalCode", postal_selectors, str(address["zipCode"])),
            ]
        )
        fast_result = self.fill_visible_fields_fast(fast_fields)
        bidi_required = {"phone", "cardNumber", "expiryDate", "cvv", "dateOfBirth"} if japan_address else set()
        for name, selectors, value in fields:
            ok = bool(fast_result.get(name))
            if name in bidi_required:
                ok = self.type_field(selectors, str(value), timeout=2, fast=True)
            elif not ok:
                ok = self.type_field(selectors, str(value), timeout=2, fast=True)
            log(f"  field {name}: {'ok' if ok else 'missing'}")
            if not ok:
                time.sleep(random.uniform(0.08, 0.18))
        ok = bool(fast_result.get("line1"))
        if not ok:
            ok = self.type_field(line1_selectors, paypal_line1, timeout=1, fast=True)
        log(f"  field line1: {'ok' if ok else 'missing'}")
        time.sleep(random.uniform(0.03, 0.08))
        if japan_address and self.visible_selector(line1_selectors) and self.field_value(line1_selectors).strip() != str(address["street"]):
            self.type_field(line1_selectors, str(address["street"]), timeout=1, fast=True)
        if not japan_address and not self.select_paypal_address_autocomplete() and len(self.field_value(line1_selectors).strip()) < 5:
            self.type_field(line1_selectors, str(address["street"]), timeout=1, fast=True)
        if self.visible_selector(city_selectors) and (japan_address or (not fast_result.get("city") and not self.field_value(city_selectors))):
            self.type_field(city_selectors, str(address["city"]), timeout=0.7, fast=True)
        if self.visible_selector(postal_selectors) and (japan_address or (not fast_result.get("postalCode") and not self.field_value(postal_selectors))):
            self.type_field(postal_selectors, str(address["zipCode"]), timeout=0.7, fast=True)
        line2 = str(address.get("line2") or address.get("building") or "").strip()
        if line2:
            line2_selectors = [
                "input[name='line2']",
                "#line2",
                "#billingLine2",
                "input[autocomplete='address-line2']",
                "input[placeholder*='建物']",
                "input[aria-label*='建物']",
            ]
            if self.visible_selector(line2_selectors) and (japan_address or not self.field_value(line2_selectors)):
                self.type_field(line2_selectors, line2, timeout=0.7, fast=True)
        state_selectors = ["select[name='state']", "select#state", "#billingState", "select[name='billingState']", "[name='billingAddress.state']"]
        if self.visible_selector(state_selectors):
            self.select_state(address["state"])
        if japan_address:
            self.force_japan_paypal_address_fields(address)
            jp_names = self.fill_japanese_name_fields(personal)
            log(f"[paypal] JP name fields: {jp_names}")
        self.click_by_selector(["#cardAddButton", "button[name='cardAddButton']"], timeout=1)

    def force_japan_paypal_address_fields(self, address: dict[str, Any]) -> None:
        values = {
            "billingPostalCode": str(address.get("zipCode") or "0788381"),
            "billingState": str(address.get("state") or "北海道"),
            "billingCity": str(address.get("city") or "旭川市"),
            "billingLine1": str(address.get("street") or "西神楽一線十七号"),
            "billingLine2": str(address.get("line2") or address.get("building") or "グリーンヒル203"),
        }
        result = self.js(
            f"""
(() => {{
  const values = {json.dumps(values, ensure_ascii=False)};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function setInput(id, value) {{
    const el = document.getElementById(id);
    if (!visible(el)) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:value}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    el.dispatchEvent(new Event('blur', {{bubbles:true}}));
    return true;
  }}
  function setSelect(id, value) {{
    const el = document.getElementById(id);
    if (!visible(el)) return false;
    const options = [...el.options];
    const found = options.find((o) => String(o.value || '').trim() === value || String(o.textContent || '').trim() === value);
    if (!found) return false;
    el.value = found.value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    el.dispatchEvent(new Event('blur', {{bubbles:true}}));
    return true;
  }}
  return {{
    postal: setInput('billingPostalCode', values.billingPostalCode),
    state: setSelect('billingState', values.billingState),
    city: setInput('billingCity', values.billingCity),
    line1: setInput('billingLine1', values.billingLine1),
    line2: setInput('billingLine2', values.billingLine2),
  }};
}})()
""",
            timeout=3,
        )
        log(f"[paypal] JP address fields forced: {result}")

    def submit_paypal_signup(self, sms_phone: str, first_name: str, last_name: str) -> None:
        def diagnostics_state() -> dict[str, Any]:
            result = self.js(
                f"""
(() => {{
  const phone = document.querySelector('#phone');
  if (phone) {{
    phone.removeAttribute('pattern');
    phone.setCustomValidity('');
  }}
  const expiry = document.querySelector('#cardExpiry, #expiryDate');
  if (expiry) {{
    expiry.removeAttribute('pattern');
    expiry.setCustomValidity('');
  }}
  const btn = [...document.querySelectorAll('button, input[type=submit], [role=button]')]
    .find((el) => {{
      const r = el.getBoundingClientRect();
      const text = [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || ''].join(' ');
	      return r.width > 0 && r.height > 0 && /Agree\\s*&\\s*Create Account|Agree and Create Account|同意.*アカウント|アカウント.*作成|同意して.*作成|同意して続行/i.test(text);
    }});
  const form = btn && btn.closest('form');
  return {{
    hasButton: Boolean(btn),
    formValid: form ? form.checkValidity() : null,
    invalids: form ? [...form.querySelectorAll('input,select,textarea')]
      .filter((el) => !el.checkValidity())
      .map((el) => ({{id: el.id, name: el.name, message: el.validationMessage}})) : []
  }};
}})()
""",
                timeout=5,
            )
            return result if isinstance(result, dict) else {}

        diagnostics = diagnostics_state()
        log(f"[paypal] submit diagnostics: {diagnostics}")
        invalids = diagnostics.get("invalids") if isinstance(diagnostics, dict) else []
        if any(re.search(r"firstName|fname|First name", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] first name invalid before submit; refill visible first name field")
            self.type_field(["#firstName", "input[name='firstName']", "input[name='fname']", "input[autocomplete='given-name']", "#cardFirstName"], first_name, timeout=2, fast=True)
        if any(re.search(r"lastName|lname|Last name", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] last name invalid before submit; refill visible last name field")
            self.type_field(["#lastName", "input[name='lastName']", "input[name='lname']", "input[autocomplete='family-name']", "#cardLastName"], last_name, timeout=2, fast=True)
        if any(re.search(r"phone", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] phone invalid before submit; refill visible phone field")
            self.type_field(["#phone", "input[name='phoneNumber']", "input[type='tel']", "input[name='phone']"], sms_phone[-10:], timeout=2, fast=True)
        if any(re.search(r"cardNumber|cardnumber", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] card number invalid before submit; refill visible card field")
            card = load_card(self.args.card_json)
            self.type_field(["input[name='cardNumber']", "#cardNumber", "input[aria-label='Card number']"], card["cardNumber"], timeout=2, fast=True)
        if any(re.search(r"cardExpiry|expiry|exp-date", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] expiry invalid before submit; refill visible expiry field")
            card = load_card(self.args.card_json)
            exp_month, exp_year = split_expiry(card["expiry"])
            self.type_field(["input[name='expiryDate']", "#expiryDate", "#cardExpiry", "input[aria-label='MM / YY']"], f"{exp_month}/{exp_year}", timeout=2, fast=True)
        if any(re.search(r"cardCvv|cvv|csc", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] cvv invalid before submit; refill visible cvv field")
            card = load_card(self.args.card_json)
            self.type_field(["input[name='cvvNumber']", "#cvv", "#cardCvv", "input[aria-label='CSC']"], str(card["cvv"]), timeout=2, fast=True)
        if any(re.search(r"dateOfBirth|birth|生年月日|date", " ".join(str(x.get(k, "")) for k in ("id", "name", "message")), re.I) for x in invalids or []):
            log("[paypal] date of birth invalid before submit; refill visible DOB field")
            self.type_field(["#dateOfBirth", "input[name='dateOfBirth']", "input[aria-label*='生年月日']", "input[placeholder*='生年月日']"], "19800101", timeout=2, fast=True)
        if invalids:
            time.sleep(0.2)
            diagnostics = diagnostics_state()
            log(f"[paypal] submit diagnostics after required refill: {diagnostics}")
            invalids = diagnostics.get("invalids") if isinstance(diagnostics, dict) else []
        if invalids:
            raise FlowFailed(
                {
                    "status": "failed",
                    "reason": "paypal_required_fields_invalid_before_submit",
                    "url": self.current_url(),
                    "invalids": invalids,
                }
            )
        before = self.page_info().get("url", "")
        submit_network_started = False
        submit_network_seen: list[dict[str, Any]] = []
        try:
            if self.page:
                submit_network_started = bool(
                    self.page.events.start(
                        ["network.beforeRequestSent", "network.responseCompleted"],
                        contexts=[self.page.tab_id],
                    )
                )
        except Exception as exc:
            log(f"[paypal] submit network capture disabled: {type(exc).__name__}: {exc}")

        def drain_submit_network() -> None:
            if not submit_network_started or not self.page:
                return
            events = getattr(self.page, "events", None)
            if not events:
                return
            for _ in range(30):
                try:
                    event = events.wait(timeout=0.001)
                except Exception:
                    return
                if not event:
                    break
                method = str(getattr(event, "method", "") or "")
                if method not in {"network.beforeRequestSent", "network.responseCompleted"}:
                    continue
                response = event.response if isinstance(getattr(event, "response", None), dict) else {}
                request = event.request if isinstance(getattr(event, "request", None), dict) else {}
                status = int(response.get("status") or 0)
                url = str(response.get("url") or request.get("url") or getattr(event, "url", "") or "")
                if not url or "paypal.com" not in url:
                    continue
                record = {"phase": "request" if method == "network.beforeRequestSent" else "response", "status": status, "url": url[:260]}
                submit_network_seen.append(record)
                if re.search(r"signup|onboard|xo|graphql|api|auth|challenge|risk", url, re.I) or status >= 400:
                    log(f"[paypal] submit network {record['phase']} status={status} url={url[:220]}")

        clicked = self.click_by_text(
            [r"agree\s*&\s*create account", r"agree and create account", r"同意.*アカウント", r"同意して.*作成", r"同意して続行", r"アカウント.*作成"],
            timeout=10,
        )
        log(f"[paypal] Agree & Create Account clicked: {clicked}")
        deadline = time.time() + 8
        extended_for_otp_init = False
        auth_challenge_recoveries = 0
        last_auth_recovery_at = 0.0
        while time.time() < deadline:
            drain_submit_network()
            auth_challenge_seen_now = any(
                re.search(r"/auth/validatecaptcha|authchallenge|hostedchallenge|securitychallenge", str(item.get("url") or ""), re.I)
                for item in submit_network_seen
            )
            if (
                auth_challenge_seen_now
                and not self.args.enable_paypal_recaptcha_2captcha
                and auth_challenge_recoveries < 3
                and time.time() - last_auth_recovery_at >= 1.2
            ):
                auth_challenge_recoveries += 1
                last_auth_recovery_at = time.time()
                self.safe_handle_captcha_if_present(detect_wait=1)
                retried = self.click_by_text(
                    [r"agree\s*&\s*create account", r"agree and create account", r"同意.*アカウント", r"同意して.*作成", r"同意して続行", r"アカウント.*作成"],
                    timeout=2,
                )
                log(f"[paypal] authchallenge DOM removed; retry signup submit attempt={auth_challenge_recoveries} clicked={retried}")
                deadline = max(deadline, time.time() + 8.0)
                time.sleep(0.5)
                continue
            if any(
                item.get("phase") == "request"
                and re.search(r"graphql|signup|onboard|risk|phone|identity|auth", str(item.get("url") or ""), re.I)
                for item in submit_network_seen
            ):
                deadline = max(deadline, time.time() + 15)
            if not extended_for_otp_init and any(
                "InitiateRiskBasedTwoFactorPhoneConfirmationMutation" in str(item.get("url") or "")
                for item in submit_network_seen
            ):
                extended_for_otp_init = True
                deadline = max(deadline, time.time() + 12)
                log("[paypal] OTP initiation API seen; waiting for verification UI")
            info = self.page_info()
            url = str(info.get("url") or "")
            text = str(info.get("text") or "")
            still_signup_form = bool(
                self.js(
                    """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const hasCard = visible(document.querySelector('#cardNumber, input[name="cardNumber"]'));
	  const hasCreate = [...document.querySelectorAll('button, input[type=submit], [role=button]')]
	    .some((el) => visible(el) && /Agree\\s*&\\s*Create Account|Agree and Create Account|同意.*アカウント|アカウント.*作成|同意して.*作成|同意して続行/i.test([el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || ''].join(' ')));
  return hasCard && hasCreate;
})()
""",
                    timeout=3,
                )
            )
            otp_inputs = bool(
                self.js(
                    """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
  }
  return visible(document.querySelector('input[name^="ciBasic-"]')) ||
    visible(document.querySelector('input[id^="ci-ciBasic-"]')) ||
    visible(document.querySelector('input[autocomplete="one-time-code"]')) ||
    visible(document.querySelector('#otc_code, input[name="otc_code"]'));
})()
""",
                    timeout=2,
                )
            )
            real_otp = otp_inputs or re.search(r"Enter your code|we sent|texted|sent.*code|one[- ]time code|verification code", text, re.I)
            terminal_or_progress_url = (
                url != before
                and not re.search(r"authchallenge|securitychallenge|/auth/validatecaptcha", url, re.I)
            )
            if terminal_or_progress_url or re.search(r"genericError", url + " " + text, re.I) or otp_inputs or (real_otp and not still_signup_form):
                if submit_network_started and self.page:
                    try:
                        self.page.events.stop()
                    except Exception:
                        pass
                return
            time.sleep(1)
        log("[paypal] submit did not advance within 8s")
        drain_submit_network()
        if submit_network_seen:
            log(f"[paypal] submit network summary: {submit_network_seen[-12:]}")
        auth_challenge_seen = any(
            re.search(r"/auth/validatecaptcha|authchallenge|hostedchallenge|securitychallenge", str(item.get("url") or ""), re.I)
            for item in submit_network_seen
        )
        if submit_network_started and self.page:
            try:
                self.page.events.stop()
            except Exception:
                pass
        self.save_paypal_signup_diagnostics("paypal_signup_submit_not_advanced")
        submit_requests_seen = any(
            item.get("phase") == "request"
            and re.search(r"graphql|signup|onboard|risk|phone|identity|auth", str(item.get("url") or ""), re.I)
            for item in submit_network_seen
        )
        reason = (
            "paypal_authchallenge_not_solved"
            if auth_challenge_seen
            else ("paypal_signup_submit_request_not_completed" if submit_requests_seen else "paypal_signup_submit_not_advanced")
        )
        captcha_details: dict[str, Any] = {}
        if auth_challenge_seen:
            try:
                captcha_details["state"] = self.captcha_state()
            except Exception as exc:
                captcha_details["stateError"] = f"{type(exc).__name__}: {exc}"
            try:
                captcha_details["params"] = self.extract_recaptcha_params()
            except Exception as exc:
                captcha_details["paramsError"] = f"{type(exc).__name__}: {exc}"
        raise FlowFailed(
            {
                "status": "failed",
                    "reason": reason,
                    "url": self.current_url(),
                    "authChallengeSeen": auth_challenge_seen,
                    "submitNetwork": submit_network_seen[-20:],
                    "captcha": captcha_details,
                }
            )

    def get_user_agent(self) -> str:
        try:
            return str(self.js("navigator.userAgent", timeout=3) or "")
        except Exception:
            return ""

    def extract_recaptcha_params(self) -> dict[str, Any]:
        params = self.js(
            r"""
(() => {
  function paramsFromUrl(url) {
    try {
      const u = new URL(url, location.href);
      const out = Object.fromEntries(u.searchParams.entries());
      const hash = (u.hash || '').replace(/^#/, '');
      const query = hash.includes('?') ? hash.split('?').pop() : hash;
      for (const [k, v] of new URLSearchParams(query).entries()) {
        if (!(k in out)) out[k] = v;
      }
      return out;
    } catch (e) {
      return {};
    }
  }
  const frames = [...document.querySelectorAll('iframe')].map((x) => x.src || '').filter(Boolean);
  const scripts = [...document.querySelectorAll('script')].map((x) => x.src || '').filter(Boolean);
  const form = document.querySelector('form');
  const fields = {};
  if (form) {
    const fd = new FormData(form);
    for (const [k, v] of fd.entries()) fields[k] = String(v);
  }
  for (const el of document.querySelectorAll('input[type=hidden], input[name], textarea[name]')) {
    if (el.name && !(el.name in fields)) fields[el.name] = String(el.value || '');
  }
  const plugin = document.querySelector('#captcha-standalone, #ads-plugin [data-sitekey]');
  const pluginAttrs = {};
  if (plugin) {
    for (const attr of plugin.attributes) pluginAttrs[attr.name] = attr.value;
  }
  let captchaType = '';
  let siteKey = fields._adsRecaptchaSiteKey || fields.siteKey || pluginAttrs['data-sitekey'] || '';
  let isEnterprise = Boolean(fields._recaptchaEnterpriseEnabled === 'true');
  if (pluginAttrs['data-recaptcha-enterprise-enabled'] === 'true') isEnterprise = true;
  let isInvisible = false;
  for (const src of frames) {
    const p = paramsFromUrl(src);
    if (/hcaptcha/i.test(src)) captchaType = 'hcaptcha';
    if (/recaptcha/i.test(src)) captchaType = captchaType || 'recaptcha';
    siteKey = siteKey || p.sitekey || p.siteKey || p.k || p.render || '';
    if (/enterprise/i.test(src) || p.reCaptchaEnterpriseEnabled === 'true') isEnterprise = true;
    if (/invisible/i.test(src) || p.size === 'invisible') isInvisible = true;
  }
  if (!captchaType && document.querySelector('[data-sitekey].h-captcha, .h-captcha, iframe[src*="hcaptcha"]')) captchaType = 'hcaptcha';
  if (!captchaType) captchaType = 'recaptcha';
  const formAction = form ? new URL(form.getAttribute('action') || location.pathname, location.href).href : '';
  const allUrls = frames.concat(scripts).join(' ');
  const apiDomain = /recaptcha\.net/i.test(allUrls) || /paypal\.com\/authchallenge/i.test(location.href)
    ? 'recaptcha.net'
    : '';
  return {url: location.href, captchaType, siteKey, isEnterprise, isInvisible, apiDomain, formAction, fields, frames, scripts, pluginAttrs};
})()
""",
            timeout=5,
        ) or {}
        if not params.get("siteKey"):
            params["siteKey"] = "6LeZ6egUAAAAAGwL8CjkDE8dcSw2DtvuVpdwTkwG"
            params["isEnterprise"] = True
            params["captchaType"] = "recaptcha"
        return params

    def captcha_state(self) -> dict[str, Any]:
        return self.js(
            r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function center(el) {
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2, width: r.width, height: r.height};
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }
  function paramsFromUrl(url) {
    try {
      const u = new URL(url, location.href);
      const out = Object.fromEntries(u.searchParams.entries());
      const hash = (u.hash || '').replace(/^#/, '');
      const query = hash.includes('?') ? hash.split('?').pop() : hash;
      for (const [k, v] of new URLSearchParams(query).entries()) {
        if (!(k in out)) out[k] = v;
      }
      return out;
    } catch (_) {
      return {};
    }
  }
  const text = document.body && document.body.innerText || '';
  const url = location.href;
  const iframes = [...document.querySelectorAll('iframe')].map((el) => ({
    el,
    src: el.src || '',
    title: el.getAttribute('title') || '',
    visible: visible(el),
    box: visible(el) ? center(el) : null
  }));
  const challengeFrames = iframes.filter((f) =>
    f.visible &&
    (f.box.width > 20 || f.box.height > 20) &&
    /hcaptcha|turnstile|recaptcha|captcha|challenge|arkose|px-captcha/i.test(f.src + ' ' + f.title)
  );
  const datadomeFrame = iframes.find((f) =>
    f.visible &&
    /geo\.ddc\.paypal\.com\/captcha|datadome|captcha-delivery|ddc\.paypal\.com/i.test(f.src + ' ' + f.title)
  );
  const pluginChallengeEl = document.querySelector('#captcha-standalone, .captcha-container[data-captcha-type], [data-app="authchallenge_response"]');
  const pluginChallenge = visible(pluginChallengeEl);
  const buttonCandidates = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a, div.ctp-checkbox-container, #challenge-stage')]
    .filter(visible)
    .map((el) => ({el, text: label(el)}));
  const challengeButton = buttonCandidates.find((x) =>
    /I'm not a robot|not a robot|Verify|Confirm|human|captcha/i.test(x.text) ||
    /ctp-checkbox|challenge-stage/i.test(String(x.el.className || '') + ' ' + x.el.id)
  );
  const sliderSelectors = [
    '#captcha__frame__bottom .slider',
    '#captcha__frame__bottom .sliderIcon',
    '.sliderContainer .slider',
    '.sliderContainer .sliderIcon',
    '.slider',
    '.sliderIcon',
    "[class*='slider']",
    "[class*='Slider']",
    "[data-testid*='slider']",
    '.geetest_slider_button',
    '.nc_iconfont.btn_slide',
    '.nc_slider',
    '#nc_1_n1z',
    '#challenge-container',
    "[aria-label*='slider' i]",
    "[role='slider']"
  ];
  let slider = null;
  for (const selector of sliderSelectors) {
    try {
      const el = document.querySelector(selector);
      if (visible(el)) {
        const box = center(el);
        const container = el.closest('.sliderContainer,[class*="slider-container"],[class*="SliderContainer"],.geetest_slider,.nc_scale') ||
          document.querySelector('#captcha__frame__bottom .sliderContainer,.sliderContainer,[class*="slider-container"],[class*="SliderContainer"],.geetest_slider,.nc_scale');
        const cBox = visible(container) ? container.getBoundingClientRect() : null;
        const distance = cBox ? Math.max(80, cBox.width - box.width + 6) : 310;
        slider = {selector, point: {x: box.x, y: box.y}, distance};
        break;
      }
    } catch (_) {}
  }
  let siteKey = '';
  let isEnterprise = false;
  let isInvisible = false;
  let captchaType = '';
  for (const node of document.querySelectorAll('[data-sitekey]')) {
    if (!siteKey) siteKey = node.getAttribute('data-sitekey') || '';
    if (/h-captcha|hcaptcha/i.test(String(node.className || '') + ' ' + node.id)) captchaType = 'hcaptcha';
  }
  for (const f of iframes) {
    const src = f.src || '';
    const p = paramsFromUrl(src);
    if (/hcaptcha/i.test(src)) captchaType = 'hcaptcha';
    if (/recaptcha/i.test(src)) captchaType = captchaType || 'recaptcha';
    siteKey = siteKey || p.sitekey || p.siteKey || p.k || p.render || '';
    if (/enterprise/i.test(src) || p.reCaptchaEnterpriseEnabled === 'true') isEnterprise = true;
    if (/invisible/i.test(src) || p.size === 'invisible') isInvisible = true;
  }
  const hasDataDomeText = /DataDome|You have been blocked|Confirm you.?re human|Try the challenge again|captcha__puzzle|captcha__audio/i.test(text);
  const hasBlockingText = /We couldn.t load the security challenge|You have been blocked|Return to merchant|Security Challenge|セキュリティチェック|Drag the slider|Move the slider|not a robot|verify you are human|confirm you.?re human|human verification/i.test(text);
  const otpActionVisible = /Enter your code|verification code|one[- ]time code|we sent|texted|6[- ]digit|confirm.*phone|security code/i.test(text) ||
    visible(document.querySelector('input[name^="ciBasic-"]')) ||
    visible(document.querySelector('input[id^="ci-ciBasic-"]')) ||
    visible(document.querySelector('input[inputmode="numeric"]')) ||
    visible(document.querySelector('input[autocomplete="one-time-code"]')) ||
    visible(document.querySelector('input[maxlength="6"]')) ||
    visible(document.querySelector('input[maxlength="1"]'));
  const normalActionVisible = buttonCandidates.some((x) => /Create an Account|Continue to Payment|Agree & Create Account|Agree and Continue|Agree & Continue/i.test(x.text)) ||
    visible(document.querySelector('#cardNumber')) ||
    visible(document.querySelector('#onboardingFlowEmail')) ||
    visible(document.querySelector('#login_email')) ||
    visible(document.querySelector('#otc_code')) ||
    visible(document.querySelector("input[name='otc_code']")) ||
    otpActionVisible;
  const authUrl = /authchallenge|validatecaptcha|hostedchallenge|securitychallenge|verifycard/i.test(url);
  const hasExplicitChallenge = Boolean(authUrl || pluginChallenge || slider || (challengeButton && !otpActionVisible) || (challengeFrames.length && !normalActionVisible) || datadomeFrame || hasDataDomeText);
  const textOnlyBlocking = hasBlockingText && !normalActionVisible;
  const detected = Boolean(hasExplicitChallenge || textOnlyBlocking);
  const datadomeParams = datadomeFrame ? paramsFromUrl(datadomeFrame.src) : {};
  return {
    detected,
    authUrl,
    datadome: datadomeFrame ? {
      src: datadomeFrame.src,
      title: datadomeFrame.title,
      point: datadomeFrame.box,
      t: datadomeParams.t || '',
      cid: datadomeParams.cid || '',
      blockedIp: datadomeParams.t === 'bv'
    } : null,
    hasDataDomeText,
    hasBlockingText,
    otpActionVisible,
    textOnlyBlocking,
    normalActionVisible,
    text: text.slice(0, 1200),
    url,
    captchaType: captchaType || (siteKey ? 'recaptcha' : ''),
    siteKey,
    isEnterprise,
    isInvisible,
    slider,
    pluginChallenge,
    button: challengeButton ? {text: challengeButton.text, point: center(challengeButton.el)} : null,
    frame: challengeFrames[0] ? {src: challengeFrames[0].src, title: challengeFrames[0].title, point: challengeFrames[0].box} : null,
    frames: challengeFrames.map((f) => ({src: f.src, title: f.title})).slice(0, 5)
  };
})()
""",
            timeout=5,
        ) or {}

    def solve_visible_challenge(self, state: dict[str, Any]) -> bool:
        if not self.page:
            return False
        slider = state.get("slider") or {}
        if slider.get("point"):
            log(f"[captcha] slider detected: {slider.get('selector')}")
            p = slider["point"]
            distance = float(slider.get("distance") or 310)
            self.page.actions.drag_to(
                {"x": p["x"], "y": p["y"]},
                {"x": p["x"] + distance, "y": p["y"] + random.uniform(-2, 2)},
                duration=random.randint(650, 1100),
                steps=random.randint(24, 38),
            ).perform()
            time.sleep(3)
            return True
        button = state.get("button") or {}
        if button.get("point"):
            log(f"[captcha] challenge button detected: {button.get('text')}")
            self.click_point(button["point"])
            time.sleep(3)
            return True
        frame = state.get("frame") or {}
        if frame.get("point") and not re.search(r"recaptcha", str(frame.get("src") or ""), re.I):
            log(f"[captcha] challenge frame detected: {frame.get('title') or frame.get('src')}")
            self.click_point(frame["point"])
            time.sleep(3)
            return True
        return False

    def _ddc_slider_probe_js(self) -> str:
        return r"""
(() => {
  const selectors = [
    '.slider',
    '[role="slider"]',
    '.slider-handle',
    '.sliderIcon',
    '#captcha__frame__bottom .slider',
    '#captcha__frame__bottom .sliderIcon',
    '#ddv1-captcha-container .slider',
    'div[class*="slider"]',
    'button[class*="slider"]',
    'div[class*="Slider"]',
    'button[class*="Slider"]',
    'div[class*="handle"]',
    'button[class*="handle"]',
    'div[class*="Handle"]',
    'button[class*="Handle"]',
    'input[type="range"]',
    '[class*="ddv1"]',
    '[id*="ddv1"]',
    '[class*="captcha"] [class*="slider"]',
    'span[class*="slider"]',
    '[draggable="true"]',
    '[aria-label*="slider" i]',
    '[data-testid*="slider" i]'
  ];
  const kw = /将滑块|确认您是人类|Slide the puzzle|move the slider|Move the slider|drag the slider|press and hold|press & hold|滑动到最右|captcha__puzzle|DataDome/i;
  function visible(el) {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity || 1) <= 0.02) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  }
  function box(el) {
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  }
  const text = (document.body && document.body.innerText || '').slice(0, 1400);
  let handle = null;
  let selector = '';
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (visible(el)) {
        handle = el;
        selector = sel;
        break;
      }
    } catch (_) {}
  }
  let container = null;
  if (handle) {
    container = handle.closest(
      '.sliderContainer,[class*="slider-container"],[class*="SliderContainer"],.geetest_slider,.nc_scale,#captcha__frame__bottom,#ddv1-captcha-container'
    );
  }
  if (!container) {
    container = document.querySelector(
      '#captcha__frame__bottom .sliderContainer,.sliderContainer,[class*="slider-container"],[class*="SliderContainer"],.geetest_slider,.nc_scale,#ddv1-captcha-container'
    );
  }
  return {
    url: location.href,
    text,
    hasText: kw.test(text),
    hasHandle: Boolean(handle),
    selector,
    handleBox: handle ? box(handle) : null,
    containerBox: visible(container) ? box(container) : null
  };
})()
"""

    def _ddc_action_probe_js(self) -> str:
        return r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity || 1) <= 0.02) return false;
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8;
  }
  function box(el) {
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  }
  const text = (document.body && document.body.innerText || '').slice(0, 1000);
  const ddcText = /DataDome|captcha|verify you are human|confirm you.?re human|验证|确认您是人类|robot/i.test(text);
  const els = [...document.querySelectorAll("button,[role='button'],input[type='button'],input[type='submit'],a")];
  for (const el of els) {
    if (!visible(el)) continue;
    const label = [
      el.innerText || '',
      el.value || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || ''
    ].join(' ').trim();
    if (/(feedback|having trouble|report|privacy|terms|contact|help|反馈|帮助|隐私|条款)/i.test(label)) {
      continue;
    }
    if (/(verify|continue|submit|start|agree|human|robot|验证|继续|确认)/i.test(label)) {
      return {hasAction: true, text: label.slice(0, 120), box: box(el), ddcText};
    }
  }
  return {hasAction: false, text: '', box: null, ddcText};
})()
"""

    def _visible_iframe_boxes(self) -> list[dict[str, Any]]:
        if not self.page:
            return []
        return self.js(
            r"""
(() => [...document.querySelectorAll('iframe')].map((el, index) => {
  const s = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const visible = s.display !== 'none' && s.visibility !== 'hidden' && r.width > 5 && r.height > 5;
  return {
    index,
    src: el.src || '',
    title: el.getAttribute('title') || '',
    visible,
    box: visible ? {x: r.x, y: r.y, width: r.width, height: r.height} : null
  };
}).filter(x => x.visible))()
""",
            timeout=3,
        ) or []

    def _frame_run_js(self, ctx: Any, expression: str, timeout: float = 3.0) -> Any:
        return ctx.run_js(expression, as_expr=True, timeout=timeout)

    def _viewport_size(self) -> tuple[float, float]:
        try:
            vp = self.js("(() => ({w: innerWidth || 1280, h: innerHeight || 900}))()", timeout=2) or {}
            return float(vp.get("w") or 1280), float(vp.get("h") or 900)
        except Exception:
            return 1280.0, 900.0

    def _ddc_context_candidates(self) -> list[dict[str, Any]]:
        if not self.page:
            return []
        candidates: list[dict[str, Any]] = []
        probe_js = self._ddc_slider_probe_js()
        try:
            main_probe = self.js(probe_js, timeout=3) or {}
            if main_probe.get("hasHandle") or main_probe.get("hasText"):
                candidates.append({"label": "main", "probe": main_probe, "iframeBox": None})
        except Exception as exc:
            log(f"[captcha] ddc main probe skipped: {type(exc).__name__}: {exc}")

        frame_boxes = self._visible_iframe_boxes()
        try:
            frames = self.page.get_frames()
        except Exception as exc:
            log(f"[captcha] ddc get_frames skipped: {type(exc).__name__}: {exc}")
            frames = []

        for idx, frame in enumerate(frames):
            iframe_box = frame_boxes[idx].get("box") if idx < len(frame_boxes) else None
            try:
                frame_url = str(getattr(frame, "url", "") or "")
            except Exception:
                frame_url = ""
            try:
                probe = self._frame_run_js(frame, probe_js, timeout=3) or {}
            except Exception:
                probe = {}
            frame_text = str(probe.get("text") or "")
            is_ddc = bool(
                re.search(r"geo\.ddc\.paypal\.com|ct\.ddc\.paypal\.com|datadome|ads-dd-captcha|captcha", frame_url, re.I)
                or re.search(r"DataDome|move the slider|Slide the puzzle|确认您是人类|滑块", frame_text, re.I)
                or probe.get("hasHandle")
            )
            if is_ddc:
                candidates.append(
                    {
                        "label": f"frame[{idx}] url={frame_url[:80]!r}",
                        "probe": probe,
                        "iframeBox": iframe_box,
                    }
                )

        if not candidates:
            for item in frame_boxes:
                src = str(item.get("src") or "")
                title = str(item.get("title") or "")
                if re.search(r"geo\.ddc\.paypal\.com|ct\.ddc\.paypal\.com|datadome|ads-dd-captcha|captcha", src + " " + title, re.I):
                    candidates.append(
                        {
                            "label": f"fallback-iframe[{item.get('index')}] src={src[:80]!r}",
                            "probe": {},
                            "iframeBox": item.get("box"),
                        }
                    )
        return candidates

    def try_click_datadome_action(self) -> bool:
        if not self.page:
            return False
        probe_js = self._ddc_action_probe_js()
        candidates: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        try:
            probe = self.js(probe_js, timeout=2) or {}
            if probe.get("hasAction"):
                candidates.append(("main", probe, None))
        except Exception:
            pass
        frame_boxes = self._visible_iframe_boxes()
        try:
            frames = self.page.get_frames()
        except Exception:
            frames = []
        for idx, frame in enumerate(frames):
            iframe_box = frame_boxes[idx].get("box") if idx < len(frame_boxes) else None
            try:
                frame_url = str(getattr(frame, "url", "") or "")
            except Exception:
                frame_url = ""
            try:
                probe = self._frame_run_js(frame, probe_js, timeout=2) or {}
            except Exception:
                probe = {}
            if probe.get("hasAction") and (
                probe.get("ddcText")
                or re.search(r"geo\.ddc\.paypal\.com|ct\.ddc\.paypal\.com|datadome|captcha", frame_url, re.I)
            ):
                candidates.append((f"frame[{idx}] url={frame_url[:80]!r}", probe, iframe_box))
        for label, probe, iframe_box in candidates:
            box = probe.get("box") or {}
            sx = float(box.get("x") or 0) + float(box.get("width") or 0) / 2
            sy = float(box.get("y") or 0) + float(box.get("height") or 0) / 2
            if iframe_box:
                sx += float(iframe_box.get("x") or 0)
                sy += float(iframe_box.get("y") or 0)
            w, h = self._viewport_size()
            sx = min(max(6.0, sx), max(8.0, w - 8.0))
            sy = min(max(6.0, sy), max(8.0, h - 8.0))
            log(f"[captcha] DataDome visible action {label}: {str(probe.get('text') or '')[:80]!r}")
            self.click_point({"x": sx, "y": sy})
            deadline = time.time() + 4
            while time.time() < deadline:
                if self._datadome_cleared():
                    log("[captcha] DataDome cleared by visible action")
                    return True
                time.sleep(0.5)
        return False

    def save_datadome_debug_snapshot(self, label: str) -> None:
        if not self.page:
            return
        result_base = Path(self.args.result_json)
        debug_path = result_base.with_name(f"{result_base.stem}_{label}.json")
        try:
            data = self.js(
                r"""
(() => ({
  url: location.href,
  title: document.title || '',
  text: (document.body && document.body.innerText || '').slice(0, 1800),
  viewport: {w: innerWidth || 0, h: innerHeight || 0},
  iframes: [...document.querySelectorAll('iframe')].map((el, index) => {
    const r = el.getBoundingClientRect();
    return {
      index,
      src: el.src || '',
      title: el.getAttribute('title') || '',
      box: {x: r.x, y: r.y, width: r.width, height: r.height},
      visible: r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none'
    };
  })
}))()
""",
                timeout=3,
            ) or {}
            try:
                frames = self.page.get_frames()
            except Exception:
                frames = []
            data["frameCount"] = len(frames)
            data["frames"] = []
            probe_js = self._ddc_slider_probe_js()
            action_js = self._ddc_action_probe_js()
            for idx, frame in enumerate(frames):
                try:
                    url = str(getattr(frame, "url", "") or "")
                except Exception:
                    url = ""
                item: dict[str, Any] = {"index": idx, "url": url}
                try:
                    probe = self._frame_run_js(frame, probe_js, timeout=2) or {}
                    item["sliderProbe"] = {
                        "hasText": bool(probe.get("hasText")),
                        "hasHandle": bool(probe.get("hasHandle")),
                        "selector": probe.get("selector") or "",
                        "handleBox": probe.get("handleBox"),
                        "containerBox": probe.get("containerBox"),
                    }
                except Exception as exc:
                    item["sliderProbeError"] = f"{type(exc).__name__}: {exc}"
                try:
                    action = self._frame_run_js(frame, action_js, timeout=2) or {}
                    item["actionProbe"] = {
                        "hasAction": bool(action.get("hasAction")),
                        "text": action.get("text") or "",
                        "box": action.get("box"),
                        "ddcText": bool(action.get("ddcText")),
                    }
                except Exception as exc:
                    item["actionProbeError"] = f"{type(exc).__name__}: {exc}"
                data["frames"].append(item)
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"[captcha] DataDome debug saved: {debug_path}")
        except Exception as exc:
            log(f"[captcha] DataDome debug save failed: {type(exc).__name__}: {exc}")

    def _ddc_drag_variants(self, candidate: dict[str, Any]) -> list[tuple[float, float, float, float, str]]:
        probe = candidate.get("probe") or {}
        iframe_box = candidate.get("iframeBox") or None
        label = str(candidate.get("label") or "ddc")
        handle_box = probe.get("handleBox") or None
        container_box = probe.get("containerBox") or None
        variants: list[tuple[float, float, float, float, str]] = []

        if handle_box:
            hx = float(handle_box.get("x") or 0)
            hy = float(handle_box.get("y") or 0)
            hw = float(handle_box.get("width") or 0)
            hh = float(handle_box.get("height") or 0)
            if iframe_box:
                ix = float(iframe_box.get("x") or 0)
                iy = float(iframe_box.get("y") or 0)
                iw = float(iframe_box.get("width") or 0)
                ih = float(iframe_box.get("height") or 0)
                end_rel_x = iw - 12
                if container_box:
                    cx = float(container_box.get("x") or 0)
                    cw = float(container_box.get("width") or 0)
                    if 0 < cx + cw <= iw + 80:
                        end_rel_x = max(end_rel_x, cx + cw - 8)
                variants.append((ix + hx + hw / 2, iy + hy + hh / 2, ix + end_rel_x, iy + hy + hh / 2, f"{label}/handle-rel"))
                if hx > ix - 20 and hy > iy - 20:
                    variants.append((hx + hw / 2, hy + hh / 2, ix + iw - 12, hy + hh / 2, f"{label}/handle-abs"))
                variants.append((ix + max(55, iw * 0.10), iy + ih * 0.55, ix + iw - 15, iy + ih * 0.55, f"{label}/generic-iframe"))
            else:
                sx = hx + hw / 2
                sy = hy + hh / 2
                if container_box:
                    ex = float(container_box.get("x") or 0) + float(container_box.get("width") or 0) - 8
                else:
                    vp = self.js("(() => ({w: innerWidth || 1365, h: innerHeight || 768}))()", timeout=2) or {}
                    ex = float(vp.get("w") or 1365) - 35
                variants.append((sx, sy, ex, sy, f"{label}/handle-main"))
        elif iframe_box:
            ix = float(iframe_box.get("x") or 0)
            iy = float(iframe_box.get("y") or 0)
            iw = float(iframe_box.get("width") or 0)
            ih = float(iframe_box.get("height") or 0)
            variants.append((ix + max(55, iw * 0.10), iy + ih * 0.55, ix + iw - 15, iy + ih * 0.55, f"{label}/generic-iframe"))
        return self._clamp_ddc_drag_variants(variants)

    def _clamp_ddc_drag_variants(self, variants: list[tuple[float, float, float, float, str]]) -> list[tuple[float, float, float, float, str]]:
        w, h = self._viewport_size()
        out: list[tuple[float, float, float, float, str]] = []
        for sx, sy, ex, ey, label in variants:
            sx = min(max(8.0, sx), max(8.0, w - 140.0))
            sy = min(max(8.0, sy), max(8.0, h - 8.0))
            ey = min(max(8.0, ey), max(8.0, h - 8.0))
            # DataDome iframes can report oversized CSS boxes in Firefox headless.
            # Keep the drag inside the visible viewport instead of aiming past it.
            ex = min(max(ex, sx + 180.0), max(sx + 90.0, w - 12.0))
            if ex - sx < 90:
                continue
            out.append((sx, sy, ex, ey, label))
        return out

    def _smooth_drag(self, sx: float, sy: float, ex: float, ey: float) -> None:
        if not self.page:
            return
        pre_x = sx - random.uniform(20, 40)
        pre_y = sy + random.uniform(-5, 5)
        self.page.actions.move_to({"x": pre_x, "y": pre_y}, duration=random.randint(80, 160)).perform()
        time.sleep(random.uniform(0.12, 0.28))
        actions = self.page.actions.move_to({"x": sx, "y": sy}, duration=random.randint(70, 130))
        actions.wait(random.uniform(0.08, 0.16)).hold().wait(random.uniform(0.10, 0.22))
        steps = random.randint(32, 48)
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            x = sx + (ex - sx) * eased
            y = sy + (ey - sy) * eased + random.uniform(-1.8, 1.8)
            actions.move_to({"x": x, "y": y}, duration=random.randint(12, 28))
        actions.wait(random.uniform(0.08, 0.18)).release().perform()

    def _datadome_cleared(self) -> bool:
        state = self.captcha_state()
        return not (state.get("detected") and (state.get("datadome") or state.get("slider") or state.get("hasDataDomeText")))

    def _datadome_frame_urls(self) -> list[str]:
        if not self.page:
            return []
        urls: list[str] = []
        try:
            frames = self.page.get_frames()
        except Exception:
            frames = []
        for frame in frames:
            try:
                url = str(getattr(frame, "url", "") or "")
            except Exception:
                url = ""
            if re.search(r"geo\.ddc\.paypal\.com|ct\.ddc\.paypal\.com|datadome|ddc\.paypal\.com", url, re.I):
                urls.append(url)
        return urls

    def datadome_blocked_ip_present(self) -> bool:
        for url in self._datadome_frame_urls():
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                if (query.get("t") or [""])[0] == "bv":
                    log(f"[captcha] DataDome blocked verdict in frame: {url[:160]}")
                    return True
            except Exception:
                if "t=bv" in url:
                    log(f"[captcha] DataDome blocked verdict in frame: {url[:160]}")
                    return True
        return False

    def try_solve_datadome_slider(self, attempts: int = 3) -> bool:
        if not self.page:
            return False
        log("[captcha] trying local DataDome slider drag")
        time.sleep(4.0)
        if self.datadome_blocked_ip_present():
            raise RuntimeError("DataDome returned t=bv; current IP/session is blocked")
        if self.try_click_datadome_action():
            return True
        for attempt in range(attempts):
            candidates = self._ddc_context_candidates()
            if not candidates:
                log(f"[captcha] DataDome slider candidate not found attempt={attempt + 1}")
                time.sleep(1.0)
                continue
            for candidate in candidates:
                variants = self._ddc_drag_variants(candidate)
                if not variants:
                    log(f"[captcha] no drag variants for {candidate.get('label')}")
                    continue
                for sx, sy, ex, ey, label in variants:
                    log(f"[captcha] DataDome drag attempt={attempt + 1} {label} start=({sx:.0f},{sy:.0f}) end=({ex:.0f},{ey:.0f})")
                    try:
                        self._smooth_drag(sx, sy, ex, ey)
                    except Exception as exc:
                        log(f"[captcha] DataDome drag error: {type(exc).__name__}: {exc}")
                        try:
                            self.page.actions.release_all()
                        except Exception:
                            pass
                        continue
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        if self._datadome_cleared():
                            log("[captcha] DataDome slider cleared by local drag")
                            return True
                        time.sleep(0.8)
            time.sleep(random.uniform(1.0, 1.8))
        log("[captcha] local DataDome slider drag did not clear")
        self.save_datadome_debug_snapshot("datadome_local_failed")
        return False

    def apply_datadome_cookie(self, cookie_header: str, page_url: str) -> bool:
        match = re.search(r"(?:^|;\s*)datadome=([^;]+)", cookie_header)
        if not match:
            raise RuntimeError(f"DataDome solution did not contain datadome cookie: {cookie_header[:120]}")
        value = match.group(1)
        cookies = [
            {"name": "datadome", "value": value, "domain": ".paypal.com", "path": "/", "secure": True},
            {"name": "datadome", "value": value, "domain": ".ddc.paypal.com", "path": "/", "secure": True},
        ]
        if self.page:
            self.page.set_cookies(cookies)
        log("[captcha] DataDome cookie applied")
        return True

    def handle_datadome_captcha(self, state: dict[str, Any]) -> bool:
        datadome = state.get("datadome") or {}
        if not datadome:
            return False
        captcha_url = str(datadome.get("src") or "")
        if not captcha_url:
            return False
        log(f"[captcha] DataDome detected: t={datadome.get('t') or '-'}")
        if self.apply_datadome_css_bypass():
            return True
        self.remove_common_checkout_styles()
        if datadome.get("blockedIp") or self.datadome_blocked_ip_present():
            raise RuntimeError("DataDome returned t=bv; rotate proxy/IP before retrying")
        if self.try_solve_datadome_slider(attempts=3):
            return True
        if not self.args.enable_datadome_2captcha:
            raise RuntimeError("DataDome local slider solver failed")

        captcha_proxy = self.captcha_proxy or self.chain_upstream
        if not captcha_proxy and self.proxy and not is_loopback_host(self.proxy["host"]):
            captcha_proxy = self.proxy
        if not captcha_proxy:
            raise RuntimeError(
                "DataDome 2Captcha fallback requires --proxy or --captcha-proxy with the same exit IP as the browser."
            )
        log(
            f"[captcha] DataDome captcha proxy: {proxy_display(captcha_proxy)} "
            f"(browser={self.proxy_note})"
        )
        api_key = self.args.captcha_api_key or os.environ.get("APIKEY_2CAPTCHA") or os.environ.get("TWOCAPTCHA_API_KEY") or ""
        if not api_key:
            raise RuntimeError("DataDome detected but no 2Captcha key was supplied")
        solver = TwoCaptchaClient(api_key)
        cookie = solver.solve_datadome(
            website_url=state.get("url") or self.page_info().get("url", ""),
            captcha_url=captcha_url,
            user_agent=self.get_user_agent(),
            proxy=captcha_proxy,
            timeout=int(self.args.captcha_wait),
        )
        self.apply_datadome_cookie(cookie, str(state.get("url") or ""))
        if self.page:
            self.page.refresh()
        self.wait_ready(timeout=60)
        wait_until = time.time() + 30
        while time.time() < wait_until:
            next_state = self.captcha_state()
            if not next_state.get("detected") or not next_state.get("datadome"):
                log("[captcha] DataDome cleared")
                return True
            time.sleep(1)
        if self.refresh_secure_connection_failed_once():
            self.wait_ready(timeout=60)
            wait_until = time.time() + 20
            while time.time() < wait_until:
                next_state = self.captcha_state()
                if not next_state.get("detected") or not next_state.get("datadome"):
                    log("[captcha] DataDome cleared after secure-page recovery")
                    return True
                time.sleep(1)
        raise TimeoutError("DataDome cookie applied but challenge did not clear")

    def inject_captcha_token(self, token: str, params: dict[str, Any]) -> bool:
        result = self.js(
            f"""
(async () => {{
  const token = {json.dumps(token)};
  const params = {json.dumps(params)};
  const now = Date.now();
  const renderStart = now - 1000;
  function setField(name, value) {{
    let el = document.querySelector(`[name="${{CSS.escape(name)}}"]`);
    if (!el) {{
      el = document.createElement(/captcha-response/.test(name) ? 'textarea' : 'input');
      el.name = name;
      el.style.display = 'none';
      const form = document.querySelector('form') || document.body;
      form.appendChild(el);
    }}
    el.value = value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
  }}
  try {{
    window.postMessage(JSON.stringify({{
      source: 'recaptchav2iframe',
      token,
      renderData: {{
        grcRenderStartTime: renderStart,
        grcRenderEndTime: now,
        grcVerificationTime: now,
        isPolicyBasedChallenge: Boolean(params.fields && params.fields.isPolicyBasedChallenge === 'true')
      }}
    }}), window.location.origin);
    await new Promise((resolve) => setTimeout(resolve, 500));
  }} catch (e) {{}}
  setField('g-recaptcha-response', token);
  setField('recaptcha', token);
  setField('h-captcha-response', token);
  setField('hcaptcha', token);
  setField('grc_render_start_time_utc', String(renderStart));
  setField('grc_render_end_time_utc', String(now));
  setField('grc_verification_time_utc', String(now));
  for (const el of document.querySelectorAll('[data-callback]')) {{
    const name = el.getAttribute('data-callback');
    const fn = name && name.split('.').reduce((obj, key) => obj && obj[key], window);
    if (typeof fn === 'function') {{
      try {{ fn(token); }} catch (e) {{}}
    }}
  }}
  const submit = [...document.querySelectorAll('button, input[type=submit], [role=button]')]
    .find((el) => {{
      const text = [el.innerText || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || ''].join(' ');
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && /continue|submit|verify|next/i.test(text);
    }});
  if (submit) {{
    submit.click();
    return {{mode:'clicked'}};
  }}
  const form = document.querySelector('form');
  if (form) {{
    form.submit();
    return {{mode:'form-submit'}};
  }}
  if (params.formAction) {{
    const body = new URLSearchParams(params.fields || {{}});
    body.set('recaptcha', token);
    body.set('g-recaptcha-response', token);
    body.set('h-captcha-response', token);
    body.set('grc_verification_time_utc', String(Date.now()));
    const res = await fetch(params.formAction, {{
      method: 'POST',
      credentials: 'include',
      headers: {{'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'}},
      body
    }});
    return {{mode:'fetch', status: res.status, text: (await res.text()).slice(0, 500)}};
  }}
  return {{mode:'token-only'}};
}})()
""",
            timeout=20,
        )
        log(f"[captcha] token injected: {result}")
        return True

    def remove_paypal_recaptcha_dom(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const targets = new Set();
  const selectors = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="captcha"][src*="paypalobjects.com"]',
    '#captcha-standalone',
    '[data-app="authchallenge_response"]',
    '.captcha-container',
    '.captcha-overlay',
    '.recaptcha-container',
    '[id*="recaptcha" i]',
    '[class*="recaptcha" i]'
  ];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) targets.add(el);
  }
  for (const frame of document.querySelectorAll('iframe')) {
    const src = frame.src || '';
    if (!/recaptcha|paypalobjects\\.com\\/.*captcha/i.test(src)) continue;
    targets.add(frame);
    const container = frame.closest('#captcha-standalone,[data-app="authchallenge_response"],.captcha-container,.recaptcha-container,.captcha-overlay');
    if (container) targets.add(container);
  }
  const removed = [];
  for (const el of [...targets]) {
    if (!el || !el.parentNode) continue;
    removed.push({
      tag: (el.tagName || '').toLowerCase(),
      id: el.id || '',
      cls: String(el.className || '').slice(0, 120),
      visible: visible(el)
    });
    el.remove();
  }
  for (const el of document.querySelectorAll('textarea[name*="recaptcha" i], input[name*="recaptcha" i], textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"]')) {
    if (!el.parentNode) continue;
    removed.push({tag: (el.tagName || '').toLowerCase(), id: el.id || '', name: el.name || '', hiddenField: true});
    el.remove();
  }
  return {removedCount: removed.length, removed: removed.slice(0, 20), url: location.href};
})()
""",
            timeout=5,
        )
        if not isinstance(result, dict):
            result = {"raw": result}
        log(f"[captcha] PayPal reCAPTCHA DOM removed: {result}")
        return result

    def handle_captcha_if_present(self, detect_wait: int | None = None) -> None:
        if self.args.no_captcha_handling:
            return
        detect_deadline = time.time() + int(detect_wait if detect_wait is not None else self.args.captcha_detect_wait)
        while time.time() < detect_deadline:
            info = self.page_info()
            url = str(info.get("url") or "")
            text = str(info.get("text") or "")
            if self.url_has_terminal_reason(url) or re.search(r"genericError|redirect_status=(failed|canceled)", url + " " + text, re.I):
                return
            state = self.captcha_state()
            if state.get("otpActionVisible") and not (
                state.get("authUrl")
                or state.get("datadome")
                or state.get("slider")
                or state.get("frames")
            ):
                log("[captcha] skipped; SMS/OTP page is visible")
                return
            if state.get("detected"):
                log(
                    "[captcha] detected: "
                    f"authUrl={state.get('authUrl')} "
                    f"datadome={bool(state.get('datadome'))} "
                    f"text={state.get('hasBlockingText')} "
                    f"frames={len(state.get('frames') or [])}"
                )
                if state.get("datadome"):
                    if self.handle_datadome_captcha(state):
                        return
                if self.solve_visible_challenge(state):
                    wait_until = time.time() + 20
                    while time.time() < wait_until:
                        next_state = self.captcha_state()
                        if not next_state.get("detected"):
                            log("[captcha] visible challenge cleared")
                            return
                        time.sleep(1)
                if self.is_paypal_authchallenge(state) and not self.args.enable_paypal_recaptcha_2captcha:
                    self.remove_paypal_recaptcha_dom(state)
                    return
                log("[captcha] solving with 2Captcha")
                api_key = self.args.captcha_api_key or os.environ.get("APIKEY_2CAPTCHA") or os.environ.get("TWOCAPTCHA_API_KEY") or ""
                if not api_key:
                    raise RuntimeError("captcha detected but no 2Captcha key was supplied")
                params = self.extract_recaptcha_params()
                if not params.get("siteKey"):
                    raise RuntimeError(f"could not extract CAPTCHA site key: {params}")
                solver = TwoCaptchaClient(api_key)
                captcha_type = str(params.get("captchaType") or state.get("captchaType") or "recaptcha").lower()
                if captcha_type == "hcaptcha":
                    token = solver.solve_hcaptcha(
                        website_url=params.get("url") or url,
                        website_key=params["siteKey"],
                        user_agent=self.get_user_agent(),
                        is_invisible=bool(params.get("isInvisible")),
                        timeout=int(self.args.captcha_wait),
                    )
                else:
                    token = solver.solve_recaptcha_v2_enterprise(
                        website_url=params.get("url") or url,
                        website_key=params["siteKey"],
                        user_agent=self.get_user_agent(),
                        api_domain=str(params.get("apiDomain") or ""),
                        is_invisible=bool(params.get("isInvisible")),
                        timeout=int(self.args.captcha_wait),
                    )
                self.inject_captcha_token(token, params)
                wait_until = time.time() + 60
                while time.time() < wait_until:
                    info = self.page_info()
                    current = str(info.get("url") or "")
                    text_now = str(info.get("text") or "")
                    next_state = self.captcha_state()
                    if not next_state.get("detected") or (
                        "authchallenge" not in current
                        and "Security Challenge" not in text_now
                        and not re.search(r"You have been blocked|couldn.?t load the security challenge", text_now, re.I)
                    ):
                        log("[captcha] passed")
                        return
                    if re.search(r"code|verification|confirm|security", text_now, re.I) and not re.search(
                        r"Security Challenge|You have been blocked|couldn.?t load the security challenge",
                        text_now,
                        re.I,
                    ):
                        return
                    time.sleep(2)
                raise TimeoutError("captcha token submitted but challenge did not advance")
            if re.search(r"confirm|verification|code|security code|Enter your code", text, re.I):
                return
            time.sleep(1)
        log("[captcha] not detected")

    def is_paypal_authchallenge(self, state: dict[str, Any]) -> bool:
        url = str(state.get("url") or self.current_url() or "")
        frame = state.get("frame") if isinstance(state.get("frame"), dict) else {}
        frame_src = str(frame.get("src") or "")
        text = str(state.get("text") or "")
        if "paypal.com" not in url and "paypalobjects.com" not in frame_src:
            return False
        return bool(
            state.get("pluginChallenge")
            or re.search(r"authchallenge|validatecaptcha|securitychallenge", url + " " + frame_src, re.I)
            or (
                state.get("siteKey")
                and re.search(r"recaptcha|captcha", frame_src + " " + text, re.I)
            )
        )

    def safe_handle_captcha_if_present(self, detect_wait: int | None = None) -> bool:
        try:
            self.handle_captcha_if_present(detect_wait=detect_wait)
            return True
        except FlowFailed:
            raise
        except Exception as exc:
            info = {}
            try:
                info = self.page_info()
            except Exception:
                info = {"url": self.current_url(), "title": "", "text": ""}
            raise FlowFailed(
                {
                    "status": "failed",
                    "reason": "captcha_solve_failed",
                    "url": info.get("url"),
                    "title": info.get("title"),
                    "text": str(info.get("text") or "")[:800],
                    "error": str(exc),
                }
            ) from exc

    def handle_sms_if_present(self, sms_api: str, baseline: str = "", poller: SmsCodePoller | None = None) -> bool:
        if sms_api.startswith("manual://"):
            return self.wait_manual_sms_flow()

        def sms_state() -> dict[str, Any]:
            return self.js(
                r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const text = document.body && document.body.innerText || '';
  const signupForm = visible(document.querySelector('#cardNumber, input[name="cardNumber"]')) &&
    [...document.querySelectorAll('button, input[type=submit], [role=button]')]
      .some((el) => visible(el) && /Agree\\s*&\\s*Create Account|Agree and Create Account|同意.*アカウント|アカウント.*作成|同意して.*作成|同意して続行/i.test([el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || ''].join(' ')));
  const otpText = /Enter your code|enter code|verification code|one[- ]time code|we sent|texted|sent.*code|check your phone|sms|text message|6[- ]digit|confirm.*phone|verify.*(you|identity)|security check/i.test(text);
  const codeSelectors = [
    'input[name^="ciBasic-"]',
    'input[id^="ci-ciBasic-"]',
    'input[inputmode="numeric"]',
    'input[name*="code" i]',
    'input[autocomplete="one-time-code"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[data-testid*="code" i]',
    'input[maxlength="6"]',
    'input[maxlength="1"]',
    '#otc_code',
    "input[name='otc_code']"
  ];
  const codeInput = codeSelectors.some((sel) => {
    try { return visible(document.querySelector(sel)); } catch (_) { return false; }
  });
  const passwordOtp = !signupForm && otpText && visible(document.querySelector('#password'));
  const genericInput = otpText && [...document.querySelectorAll('input')]
    .some((el) => visible(el) &&
      !['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(String(el.type || '').toLowerCase()) &&
      !/postal|zip|country|phone|card|cvv|cvc|expiry|email|city|state|first|last|line|address/i.test([el.id || '', el.name || '', el.placeholder || '', el.getAttribute('aria-label') || ''].join(' ')));
  const buttons = [...document.querySelectorAll('button, input[type=submit], [role=button], a')]
    .filter(visible)
    .map((el) => [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || ''].join(' ').replace(/\\s+/g, ' ').trim());
  const params = new URLSearchParams(location.search);
  const hasTerminalReason = params.has('reason') &&
    !/paypal\.com\/webapps\/hermes|billingweb\/review|billingLite=1/i.test(location.href);
  const rawFinalButton = buttons.some((x) => /Agree and Continue|Agree\\s*&\\s*Continue|同意して続行|同意して支払う|同意する|支払う/i.test(x));
  return {
    url: location.href,
    text: text.slice(0, 1200),
    signupForm,
    otpVisible: Boolean(codeInput || passwordOtp || (!signupForm && (otpText || genericInput))),
    invalidCode: Boolean((codeInput || otpText) && /コードを確認して、?再度お試しください|コードが正しくありません|確認コードが違います|invalid\\s+code|incorrect\\s+code|wrong\\s+code|try\\s+again/i.test(text)),
    finalButton: Boolean(rawFinalButton && !signupForm && !codeInput && !passwordOtp && !genericInput),
    failed: /genericError|redirect_status=(failed|canceled)/i.test(location.href) || hasTerminalReason
  };
})()
""",
                timeout=5,
            ) or {}

        deadline = time.time() + int(self.args.sms_page_wait)
        code = ""
        last_poll = 0.0
        while time.time() < deadline:
            if not self.args.no_captcha_handling:
                captcha_state = self.captcha_state()
                if captcha_state.get("detected"):
                    self.safe_handle_captcha_if_present(detect_wait=4)
                    time.sleep(0.5)
                    continue
            state = sms_state()
            if state.get("failed"):
                return False
            if not code and poller:
                code = poller.result(timeout=0)
                _announce_sms_code(self, code)
            if not code and not poller and time.time() - last_poll >= 1:
                last_poll = time.time()
                try:
                    fetched, signature, _raw = fetch_sms_code(sms_api)
                    if fetched and signature != baseline:
                        code = fetched
                        _announce_sms_code(self, code)
                except Exception:
                    pass
            if state.get("otpVisible"):
                break
            if state.get("finalButton"):
                log("[sms] no SMS page; final confirmation is already visible")
                return False
            if not state.get("signupForm") and re.search(r"code|verification|confirm|security code|one[- ]time|sent.*text|text message|sms", str(state.get("text") or ""), re.I):
                self.click_by_text([r"send code", r"text me", r"send text", r"continue", r"confirm", r"next"], timeout=3)
            time.sleep(0.7)
        else:
            log("[sms] no SMS verification screen detected")
            return False

        log("[sms] polling verification code")
        def already_advanced() -> bool:
            if self.classify_payment_url(self.current_url()):
                return True
            try:
                state = sms_state()
                return bool(state.get("finalButton"))
            except Exception:
                return False

        if not code and poller:
            code = poller.result(timeout=int(self.args.sms_timeout))
            _announce_sms_code(self, code)
        if not code and poller:
            if already_advanced():
                log("[sms] page advanced before API code was available")
                return True
            log("[sms] code not found by background poller")
            return False
        if not code:
            code = wait_fresh_sms_code(
                sms_api,
                baseline=baseline,
                timeout=int(self.args.sms_timeout),
                interval=1.0,
                stop_if=already_advanced,
            )
            _announce_sms_code(self, code)
        if not code:
            log("[sms] page advanced before API code was available")
            return True
        verify_deadline = time.time() + 8
        while time.time() < verify_deadline:
            state = sms_state()
            if state.get("otpVisible"):
                break
            if state.get("failed") or state.get("finalButton"):
                return bool(state.get("finalButton"))
            time.sleep(0.5)
        _announce_sms_code(self, code)
        filled = self.fill_sms_code(code)
        self.sms_code_filled = bool(filled)
        log(f"[sms] code filled: {filled}")
        if not filled:
            if already_advanced():
                log("[sms] page advanced before code fill completed")
                self.sms_advanced_without_confirmed_fill = True
                return True
            self.save_otp_fill_diagnostics("sms_code_fill_failed")
            return False
        sms_submit_network_started = False
        sms_submit_network_seen: list[dict[str, Any]] = []
        sms_body_capture_started = self.start_paypal_response_body_capture("sms-body")
        try:
            if self.page:
                sms_submit_network_started = bool(
                    self.page.events.start(
                        ["network.beforeRequestSent", "network.responseCompleted"],
                        contexts=[self.page.tab_id],
                    )
                )
        except Exception as exc:
            log(f"[sms] submit network capture disabled: {type(exc).__name__}: {exc}")

        def drain_sms_submit_network() -> None:
            if not sms_submit_network_started or not self.page:
                return
            events = getattr(self.page, "events", None)
            if not events:
                return
            for _ in range(30):
                try:
                    event = events.wait(timeout=0.001)
                except Exception:
                    return
                if not event:
                    break
                method = str(getattr(event, "method", "") or "")
                if method not in {"network.beforeRequestSent", "network.responseCompleted"}:
                    continue
                response = event.response if isinstance(getattr(event, "response", None), dict) else {}
                request = event.request if isinstance(getattr(event, "request", None), dict) else {}
                status = int(response.get("status") or 0)
                url = str(response.get("url") or request.get("url") or getattr(event, "url", "") or "")
                if not url or "paypal.com" not in url:
                    continue
                record = {"phase": "request" if method == "network.beforeRequestSent" else "response", "status": status, "url": url[:260]}
                sms_submit_network_seen.append(record)
                if re.search(r"graphql|phone|confirm|otp|code|risk|auth|challenge", url, re.I) or status >= 400:
                    log(f"[sms] submit network {record['phase']} status={status} url={url[:220]}")

        clicked = self.click_sms_submit_button()
        if not clicked:
            clicked = self.click_by_text(
                [
                    r"^continue$",
                    r"confirm",
                    r"submit",
                    r"verify",
                    r"next",
                    r"続行",
                    r"確認",
                    r"認証",
                    r"送信",
                    r"次へ",
                ],
                timeout=3,
            )
        if not clicked:
            try:
                self.page.actions.press(Keys.ENTER).perform()
                clicked = True
            except Exception:
                clicked = False
        log(f"[sms] verification submitted: {clicked}")
        deadline = time.time() + 4.0
        post_sms_authchallenge_seen = False
        progress_seen = 0
        invalid_code_retried = False
        while time.time() < deadline:
            before_count = len(sms_submit_network_seen)
            drain_sms_submit_network()
            new_records = sms_submit_network_seen[before_count:]
            try:
                state = sms_state()
            except Exception:
                state = {}
            if state.get("invalidCode"):
                if invalid_code_retried:
                    self.save_otp_fill_diagnostics(
                        "sms_code_invalid",
                        extra={
                            "smsSubmitNetwork": sms_submit_network_seen[-20:],
                            "paypalResponseBodies": self.paypal_response_bodies[-12:],
                        },
                    )
                    raise FlowFailed(
                        self.annotate_result(
                            {
                                "status": "failed",
                                "reason": "sms_code_invalid",
                                "url": self.current_url(),
                                "smsSubmitNetwork": sms_submit_network_seen[-20:],
                                "paypalResponseBodies": self.paypal_response_bodies[-12:],
                            }
                        )
                    )
                invalid_code_retried = True
                log("[sms] PayPal reported invalid OTP; refilling code once")
                try:
                    fetched, _signature, _raw = fetch_sms_code(sms_api)
                    if fetched:
                        code = fetched
                        _announce_sms_code(self, code)
                except Exception as exc:
                    log(f"[sms] failed to refresh OTP before retry: {type(exc).__name__}: {exc}")
                if not self.fill_sms_code(code):
                    self.save_otp_fill_diagnostics("sms_code_refill_failed")
                    raise FlowFailed(
                        self.annotate_result(
                            {
                                "status": "failed",
                                "reason": "sms_code_refill_failed",
                                "url": self.current_url(),
                                "smsSubmitNetwork": sms_submit_network_seen[-20:],
                            }
                        )
                    )
                clicked = self.click_sms_submit_button()
                if not clicked:
                    clicked = self.click_by_text([r"続行", r"確認", r"認証", r"送信", r"continue", r"confirm", r"verify"], timeout=2)
                log(f"[sms] verification resubmitted after invalid OTP: {clicked}")
                deadline = time.time() + 8.0
                time.sleep(0.3)
                continue
            if not post_sms_authchallenge_seen and any(
                re.search(r"authchallenge|hostedchallenge|securitychallenge|/auth/validatecaptcha", str(item.get("url") or ""), re.I)
                for item in sms_submit_network_seen
            ):
                post_sms_authchallenge_seen = True
                log("[sms] PayPal authchallenge appeared after OTP submit")
                self.safe_handle_captcha_if_present(detect_wait=1)
                deadline = min(deadline, time.time() + 2.0)
            if new_records and any(
                re.search(r"graphql|phone|confirm|otp|code|risk|auth|threeds", str(item.get("url") or ""), re.I)
                for item in new_records
            ):
                if not post_sms_authchallenge_seen:
                    progress_seen += 1
                    deadline = max(deadline, time.time() + 8.0)
                    if progress_seen <= 3:
                        log("[sms] PayPal backend progress after OTP; extending advance wait")
            if already_advanced():
                if sms_submit_network_started and self.page:
                    try:
                        self.page.events.stop()
                    except Exception:
                        pass
                if sms_body_capture_started:
                    self.stop_paypal_response_body_capture("sms-body")
                return True
            time.sleep(0.2)
        drain_sms_submit_network()
        if sms_submit_network_started and self.page:
            try:
                self.page.events.stop()
            except Exception:
                pass
        if sms_body_capture_started:
            self.stop_paypal_response_body_capture("sms-body")
        if post_sms_authchallenge_seen:
            self.save_otp_fill_diagnostics(
                "paypal_authchallenge_after_sms",
                extra={
                    "smsSubmitNetwork": sms_submit_network_seen[-20:],
                    "paypalResponseBodies": self.paypal_response_bodies[-12:],
                },
            )
            raise FlowFailed(
                self.annotate_result(
                    {
                        "status": "failed",
                        "reason": "paypal_authchallenge_after_sms",
                        "url": self.current_url(),
                        "smsSubmitNetwork": sms_submit_network_seen[-20:],
                        "paypalResponseBodies": self.paypal_response_bodies[-12:],
                    }
                )
            )
        self.save_otp_fill_diagnostics(
            "sms_submit_not_advanced",
            extra={
                "smsSubmitNetwork": sms_submit_network_seen[-20:],
                "paypalResponseBodies": self.paypal_response_bodies[-12:],
            },
        )
        raise FlowFailed(
            self.annotate_result(
                {
                    "status": "failed",
                    "reason": "sms_submit_not_advanced",
                    "url": self.current_url(),
                    "smsSubmitNetwork": sms_submit_network_seen[-20:],
                    "paypalResponseBodies": self.paypal_response_bodies[-12:],
                }
            )
        )

    def wait_manual_sms_flow(self) -> bool:
        log("[sms] manual mode; waiting for manual verification to advance")
        deadline = time.time() + int(self.args.sms_timeout)
        while time.time() < deadline:
            classified = self.classify_payment_url(self.current_url())
            if classified:
                return True
            try:
                info = self.page_info()
                text = str(info.get("text") or "")
                url = str(info.get("url") or "")
                if re.search(r"genericError|redirect_status=(failed|canceled)", url + " " + text, re.I):
                    return False
                if re.search(r"Agree and Continue|Agree\s*&\s*Continue|Set up once\. Pay faster next time", text, re.I):
                    return True
            except Exception:
                pass
            time.sleep(1)
        log("[sms] manual verification did not advance before timeout")
        return False

    def paypal_sms_or_final_state(self) -> dict[str, Any]:
        return self.js(
            r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }
  const text = document.body && document.body.innerText || '';
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible)
    .map(label);
  const signupForm = visible(document.querySelector('#cardNumber, input[name="cardNumber"]')) &&
    buttons.some((x) => /Agree\s*&\s*Create Account|Agree and Create Account|同意.*アカウント|アカウント.*作成|同意して.*作成|同意して続行/i.test(x));
  const otpInput = inputs.some((el) => {
    const meta = [el.id || '', el.name || '', el.placeholder || '', el.getAttribute('aria-label') || '', el.autocomplete || '', el.inputMode || ''].join(' ');
    return /otp|code|verification|security|one[- ]time|sms|pin/i.test(meta) || Number(el.maxLength) === 1;
  });
  const otpText = /Enter your code|enter code|verification code|one[- ]time code|we sent|texted|sent.*code|check your phone|sms|text message|6[- ]digit|confirm.*phone|verify.*(you|identity)|security check/i.test(text);
  const rawFinalButton = buttons.some((x) => /Agree and Continue|Agree\s*&\s*Continue|同意して続行|同意して支払う|同意する|支払う/i.test(x));
  return {
    url: location.href,
    otpVisible: Boolean(otpInput || otpText),
    finalButton: Boolean(rawFinalButton && !signupForm && !(otpInput || otpText)),
    failed: /genericError|redirect_status=(failed|canceled)/i.test(location.href)
  };
})()
""",
            timeout=5,
        ) or {}

    def fail_if_sms_pending_without_code(self, sms_ok: bool) -> dict[str, Any] | None:
        if sms_ok:
            return None
        try:
            state = self.paypal_sms_or_final_state()
        except Exception:
            state = {}
        otp_waiting = bool(state.get("otpVisible"))
        if not otp_waiting:
            try:
                captcha_state = self.captcha_state()
                otp_waiting = bool(captcha_state.get("otpActionVisible")) and not bool(captcha_state.get("detected"))
            except Exception:
                otp_waiting = False
        if otp_waiting and not state.get("finalButton") and not state.get("failed"):
            log("[sms] verification code unavailable while OTP page is still waiting; fail fast")
            return self.annotate_result(
                {
                    "status": "failed",
                    "reason": "sms_code_not_found",
                    "url": state.get("url") or self.current_url(),
                }
            )
        return None

    def click_sms_submit_button(self) -> bool:
        point = self.js(
            r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function text(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
  }
  const inputs = [...document.querySelectorAll(
    'input[name^="ciBasic-"], input[id^="ci-ciBasic-"], input[autocomplete="one-time-code"], input[name*="code" i], input[id*="code" i], input[maxlength="6"], input[maxlength="1"]'
  )].filter(visible);
  if (!inputs.length) return false;
  const inputRect = inputs.reduce((best, el) => {
    const r = el.getBoundingClientRect();
    if (!best || r.top > best.top) return r;
    return best;
  }, null);
  const root = inputs[0].closest('[role="dialog"], [aria-modal="true"], form, [class*="modal"], [class*="dialog"]') || document;
  const candidates = [...root.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible)
    .map((el) => {
      const r = el.getBoundingClientRect();
      return {el, r, label: text(el)};
    })
    .filter((x) => /continue|confirm|submit|verify|next|続行|確認|認証|送信|次へ/i.test(x.label));
  if (!candidates.length) return false;
  const below = candidates
    .filter((x) => !inputRect || x.r.top >= inputRect.bottom - 8)
    .sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
  const picked = below[0] || candidates[0];
  picked.el.scrollIntoView({block:'center', inline:'center'});
  const r2 = picked.el.getBoundingClientRect();
  return {x: r2.left + r2.width / 2, y: r2.top + r2.height / 2, text: picked.label};
})()
""",
            timeout=3,
        )
        if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
            log(f"[sms] submit button near OTP: {point.get('text')}")
            self.click_point(point)
            time.sleep(random.uniform(0.15, 0.35))
            return True
        return False

    def save_otp_fill_diagnostics(self, reason: str, extra: dict[str, Any] | None = None) -> None:
        try:
            out = Path(self.args.result_json).with_name(f"{Path(self.args.result_json).stem}_{reason}.json")
            diag = self.js(
                """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function attrs(el) {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      autocomplete: el.autocomplete || '',
      placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '',
      testid: el.getAttribute('data-testid') || '',
      inputMode: el.inputMode || '',
      maxLength: el.maxLength,
      visible: visible(el),
      disabled: Boolean(el.disabled),
      valueLen: String(el.value || '').length,
      rect: {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)}
    };
  }
  const inputs = [...document.querySelectorAll('input')].map(attrs);
  const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible)
    .map((el) => [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim())
    .filter(Boolean)
    .slice(0, 20);
  return {
    url: location.href,
    title: document.title || '',
    text: (document.body && document.body.innerText || '').slice(0, 1200),
    inputs,
    buttons,
    frames: [...document.querySelectorAll('iframe')].map((el) => ({src: el.src || '', id: el.id || '', name: el.name || ''}))
  };
})()
""",
                timeout=3,
            ) or {}
            if extra:
                diag.update(extra)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"reason": reason, "diagnostic": diag}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"[sms] OTP fill diagnostic saved: {out}")
        except Exception as exc:
            log(f"[sms] OTP fill diagnostic failed: {type(exc).__name__}: {exc}")

    def fill_sms_code(self, code: str) -> bool:
        ci_basic = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const inputs = [...document.querySelectorAll('input[name^="ciBasic-"], input[id^="ci-ciBasic-"]')]
    .filter(visible)
    .sort((a, b) => {
      const ax = [a.name || '', a.id || ''].join(' ').match(/(\\d+)/);
      const bx = [b.name || '', b.id || ''].join(' ').match(/(\\d+)/);
      return Number(ax && ax[1] || 0) - Number(bx && bx[1] || 0);
    })
    .slice(0, 8);
  if (inputs.length < 4) return false;
  inputs.forEach((el) => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); });
  const points = inputs.map((el) => {
    el.scrollIntoView({block:'center', inline:'center'});
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  });
  return {points, valueLen: inputs.map((el) => el.value || '').join('').length};
})()
""",
            timeout=3,
        )
        if isinstance(ci_basic, dict) and isinstance(ci_basic.get("points"), list) and len(ci_basic["points"]) >= len(code):
            try:
                self.click_point(ci_basic["points"][0])
                time.sleep(random.uniform(0.03, 0.08))
                self.page.actions.type(code, interval=random.randint(12, 25)).perform()
                time.sleep(random.uniform(0.08, 0.16))
                value_len = self.js(
                    """
(() => [...document.querySelectorAll('input[name^="ciBasic-"], input[id^="ci-ciBasic-"]')]
  .map((el) => el.value || '')
  .join('')
  .length)()
""",
                    timeout=2,
                )
                if int(value_len or 0) >= len(code):
                    return True
            except Exception:
                pass

        fast_dom = self.js(
            f"""
(() => {{
  const code = {json.dumps(code)};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function setValue(el, value) {{
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    el.scrollIntoView({{block:'center', inline:'center'}});
    el.focus();
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:value}}));
    el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles:true, key:String(value).slice(-1)}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
  }}
  const byIndex = (a, b) => {{
    const ax = [a.name || '', a.id || '', a.getAttribute('aria-label') || ''].join(' ').match(/(\\d+)/);
    const bx = [b.name || '', b.id || '', b.getAttribute('aria-label') || ''].join(' ').match(/(\\d+)/);
    return Number(ax && ax[1] || 0) - Number(bx && bx[1] || 0);
  }};
  const ci = [...document.querySelectorAll('input[name^="ciBasic-"], input[id^="ci-ciBasic-"]')]
    .filter(visible).sort(byIndex);
  if (ci.length >= code.length) {{
    [...code].forEach((digit, i) => setValue(ci[i], digit));
    return {{mode:'ciBasic', value: ci.map((el) => el.value || '').join('')}};
  }}
  const inputs = [...document.querySelectorAll('input')]
    .filter(visible)
    .filter((el) => {{
      const type = String(el.type || 'text').toLowerCase();
      if (['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(type)) return false;
      const attrs = [el.id || '', el.name || '', el.autocomplete || '', el.placeholder || '', el.getAttribute('aria-label') || '', el.getAttribute('data-testid') || '', el.inputMode || '', el.maxLength || ''].join(' ');
      if (/postal|zip|country|phone|card|cvv|cvc|expiry|email|city|state|first|last|line|address/i.test(attrs)) return false;
      return true;
    }});
  const one = inputs.find((el) => Number(el.maxLength) >= code.length || /one-time-code|otp|code|numeric/i.test([el.autocomplete || '', el.name || '', el.id || '', el.inputMode || '', el.getAttribute('aria-label') || ''].join(' ')));
  if (one) {{
    setValue(one, code);
    return {{mode:'single', value: one.value || ''}};
  }}
  const split = inputs.filter((el) => Number(el.maxLength) === 1).sort(byIndex);
  if (split.length >= code.length) {{
    [...code].forEach((digit, i) => setValue(split[i], digit));
    return {{mode:'split', value: split.map((el) => el.value || '').join('')}};
  }}
  return false;
}})()
""",
            timeout=3,
        )
        if isinstance(fast_dom, dict) and len(str(fast_dom.get("value") or "")) >= len(code):
            return True

        split_points = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  const inputs = [...document.querySelectorAll('input[maxlength="1"]')]
    .filter(visible)
    .slice(0, 8);
  if (inputs.length < 4) return [];
  inputs.forEach((el) => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); });
  return inputs.map((el) => {
    el.scrollIntoView({block:'center', inline:'center'});
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  });
})()
""",
            timeout=3,
        )
        if isinstance(split_points, list) and len(split_points) >= len(code):
            try:
                for point, digit in zip(split_points, code):
                    self.click_point(point)
                    time.sleep(random.uniform(0.04, 0.12))
                    self.page.actions.type(digit, interval=random.randint(40, 90)).perform()
                return True
            except Exception:
                pass

        selectors = [
            "#otc_code",
            "input[name='otc_code']",
            "input[name^='ciBasic-']",
            "input[id^='ci-ciBasic-']",
            "input[inputmode='numeric']",
            "input[maxlength='6']",
            "#password",
            "#otp",
            "#otpCode",
            "#securityCode",
            "#verificationCode",
            "input[name='pin']",
            "input[name='otp']",
            "input[name='otpCode']",
            "input[name='code']",
            "input[name='securityCode']",
            "input[name='verificationCode']",
            "input[autocomplete='one-time-code']",
            "input[aria-label*='code' i]",
            "input[placeholder*='code' i]",
            "input[data-testid*='otp' i]",
            "input[data-testid*='code' i]",
        ]
        if self.type_field(selectors, code, timeout=5):
            return True
        point = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function attrs(el) {
    return [el.id || '', el.name || '', el.autocomplete || '', el.placeholder || '', el.getAttribute('aria-label') || '', el.getAttribute('data-testid') || '', el.type || ''].join(' ');
  }
  const text = document.body && document.body.innerText || '';
  if (!/Enter your code|enter code|verification code|one[- ]time code|we sent|texted|sent.*code|check your phone/i.test(text)) return false;
  const inputs = [...document.querySelectorAll('input')]
    .filter((el) => visible(el) && !['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(String(el.type || '').toLowerCase()))
    .filter((el) => !/postal|zip|country|phone|card|cvv|cvc|expiry|password|email|city|state|first|last|line|address/i.test(attrs(el)));
  const el = inputs[0];
  if (!el) return false;
  el.scrollIntoView({block:'center', inline:'center'});
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
})()
""",
            timeout=3,
        )
        if point and point.get("x") is not None:
            self.click_point(point)
            time.sleep(0.2)
            try:
                self.page.actions.type(code, interval=random.randint(50, 110)).perform()
                return True
            except Exception:
                pass
        result = self.js(
            f"""
(() => {{
  const code = {json.dumps(code)};
  function visible(el) {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }}
  function setValue(el, value) {{
    el.scrollIntoView({{block:'center', inline:'center'}});
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event('input', {{bubbles:true}}));
    el.dispatchEvent(new Event('change', {{bubbles:true}}));
    el.dispatchEvent(new Event('keyup', {{bubbles:true}}));
  }}
  const visibleInputs = [...document.querySelectorAll('input')]
    .filter((el) => visible(el) && !['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(String(el.type || '').toLowerCase()));
  const text = document.body && document.body.innerText || '';
  const genericOtpInputs = visibleInputs.filter((el) => {{
    const a = [el.id || '', el.name || '', el.autocomplete || '', el.placeholder || '', el.getAttribute('aria-label') || '', el.getAttribute('data-testid') || '', el.type || ''].join(' ');
    return /Enter your code|enter code|verification code|one[- ]time code|we sent|texted|sent.*code|check your phone/i.test(text) &&
      !/postal|zip|country|phone|card|cvv|cvc|expiry|password|email|city|state|first|last|line|address/i.test(a);
  }});
  if (genericOtpInputs.length === 1) {{
    setValue(genericOtpInputs[0], code);
    return {{mode:'generic-single'}};
  }}
  const splitInputs = visibleInputs.filter((el) => Number(el.maxLength) === 1);
  if (splitInputs.length >= code.length) {{
    [...code].forEach((digit, index) => setValue(splitInputs[index], digit));
    return {{mode:'split', count: splitInputs.length}};
  }}
  return false;
}})()
""",
            timeout=5,
        )
        return bool(result)

    def handle_final_confirmation_if_present(self, timeout: int = 90) -> str:
        log("[step] check PayPal final confirmation")
        self.start_success_network_capture()
        deadline = time.time() + timeout
        click_attempts = 0
        non_review_started: float | None = None
        while time.time() < deadline:
            if self.drain_success_network_events():
                self.final_confirmation_accepted = True
                self.final_confirmation_state = "accepted"
                return "accepted"
            url = self.current_url()
            classified = self.classify_payment_url(url)
            if classified:
                self.final_confirmation_state = "already_advanced" if classified.get("status") == "success" else "failed"
                return self.final_confirmation_state
            if not self.is_paypal_review_url(url):
                if "paypal.com" not in str(url or ""):
                    self.final_confirmation_state = "not_review"
                    return "not_review"
                if non_review_started is None:
                    non_review_started = time.time()
                if time.time() - non_review_started > 12:
                    self.final_confirmation_state = "not_review"
                    return "not_review"
                time.sleep(0.25)
                continue
            non_review_started = None
            state = self.paypal_final_review_state()
            if not self.args.no_captcha_handling and state.get("captcha"):
                self.safe_handle_captcha_if_present()
                time.sleep(0.5)
                continue
            if state.get("otp"):
                log("[paypal] final confirmation wait paused; OTP page is visible")
                self.final_confirmation_state = "otp"
                return "otp"
            if state.get("loginFallback"):
                log("[paypal] Hermes review rendered login fallback")
                self.final_confirmation_state = "login_fallback"
                return "login_fallback"
            if state.get("finalButton"):
                clicked = self.click_paypal_final_confirmation_button(timeout=1.2)
                click_attempts += 1
                advanced = False
                after_url = url
                for _ in range(20):
                    time.sleep(0.15)
                    if self.drain_success_network_events():
                        advanced = True
                        break
                    after_url = self.current_url()
                    advanced = bool(after_url != url and not self.is_paypal_review_url(after_url))
                    if advanced:
                        break
                log(
                    f"[paypal] final confirmation clicked: {clicked} "
                    f"accepted: {advanced} attempt={click_attempts}"
                )
                if advanced:
                    self.final_confirmation_accepted = True
                    self.final_confirmation_state = "accepted"
                    return "accepted"
                if click_attempts >= 3:
                    self.save_final_confirmation_diagnostics("final_confirmation_not_accepted")
                    self.final_confirmation_state = "clicked_unaccepted" if clicked else "not_clicked"
                    return self.final_confirmation_state
                continue
            if state.get("noEligibleFunding"):
                log("[paypal] Hermes review has no eligible funding source and no consent button")
                self.save_final_confirmation_diagnostics("paypal_no_eligible_funding")
                self.final_confirmation_state = "no_eligible_funding"
                return "no_eligible_funding"
            time.sleep(0.5)
        log("[paypal] final confirmation button not detected")
        self.save_final_confirmation_diagnostics("final_confirmation_not_detected")
        self.final_confirmation_state = "not_detected"
        return "not_detected"

    def annotate_result(self, result: dict[str, Any]) -> dict[str, Any]:
        result.setdefault("automation", {})
        result["automation"].update(
            {
                "smsCodeReceived": self.sms_code_received,
                "smsCodeFilled": self.sms_code_filled,
                "smsAdvancedWithoutConfirmedFill": self.sms_advanced_without_confirmed_fill,
                "finalConfirmationAccepted": self.final_confirmation_accepted,
                "finalConfirmationState": self.final_confirmation_state,
                "manualAssistancePossible": bool(
                    result.get("status") == "success"
                    and (
                        (self.sms_code_received and not self.sms_code_filled)
                        or (
                            self.sms_code_received
                            and not self.final_confirmation_accepted
                        )
                    )
                ),
            }
        )
        if self.paypal_response_bodies:
            result.setdefault("paypalResponseBodies", self.paypal_response_bodies[-12:])
        return result

    def save_result_screenshot(self, result: dict[str, Any]) -> None:
        if not self.page:
            return
        try:
            result_path = Path(str(self.args.result_json or "recordings/last_ruyi_paypal_result.json"))
            reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(result.get("reason") or result.get("status") or "result")).strip("_")
            screenshot_path = result_path.with_name(f"{result_path.stem}_{reason or 'result'}.png")
            self.page.screenshot(str(screenshot_path))
            result["screenshotPath"] = str(screenshot_path)
            log(f"[screenshot] saved {screenshot_path}")
        except Exception as exc:
            log(f"[screenshot] save failed: {type(exc).__name__}: {exc}")

    def save_final_confirmation_diagnostics(self, reason: str) -> None:
        try:
            out = Path(self.args.result_json).with_name(f"{Path(self.args.result_json).stem}_{reason}.json")
            diag = self.paypal_final_review_state()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"reason": reason, "diagnostic": diag}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log(f"[paypal] final confirmation diagnostic saved: {out}")
        except Exception as exc:
            log(f"[paypal] final confirmation diagnostic failed: {type(exc).__name__}: {exc}")

    def paypal_final_review_state(self) -> dict[str, Any]:
        return self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '', el.getAttribute('data-testid') || '']
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible)
    .map(label);
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  const text = document.body && document.body.innerText || '';
  const emailInput = inputs.some((el) => /email|login_email/i.test([el.id || '', el.name || '', el.placeholder || '', el.getAttribute('aria-label') || ''].join(' ')));
  const otp = visible(document.querySelector('#otc_code')) ||
    visible(document.querySelector("input[name='otc_code']")) ||
    visible(document.querySelector("input[autocomplete='one-time-code']")) ||
    visible(document.querySelector("input[name^='ciBasic-']")) ||
    visible(document.querySelector("input[id^='ci-ciBasic-']"));
  const finalButton = buttons.some((x) => /Agree and Continue|Agree\\s*&\\s*Continue|consentButton|同意して続行|同意して支払う|同意する|支払う/i.test(x));
  const noEligibleFunding = /対象となるカードが登録されていません|新しい支払方法を追加してください|No eligible funding|add a new payment method|Add a bank or card/i.test(text);
  const createAccount = buttons.some((x) => /Create\\s+(an?\\s+)?Account/i.test(x));
  const passkey = buttons.some((x) => /Log in with Passkey/i.test(x));
  const next = buttons.some((x) => /^Next$/i.test(x));
  return {
    url: location.href,
    finalButton,
    noEligibleFunding,
    loginFallback: emailInput && (createAccount || passkey || next),
    captcha: /authchallenge|captcha|robot|Security Challenge/i.test(location.href + ' ' + buttons.join(' ')),
    otp,
    buttons: buttons.slice(0, 12)
  };
})()
""",
            timeout=2,
        ) or {}

    def click_paypal_final_confirmation_button(self, timeout: float = 1.2) -> bool:
        target = self.js(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '', el.getAttribute('data-testid') || '']
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  const selectors = ['#consentButton', '[data-testid="consentButton"]', 'button[name="consentButton"]'];
  for (const selector of selectors) {
    let el = null;
    try { el = document.querySelector(selector); } catch (_) { el = null; }
    if (!visible(el)) continue;
    el.scrollIntoView({block:'center', inline:'center'});
    const r = el.getBoundingClientRect();
    return {mode:'selector', selector, x:r.left + r.width / 2, y:r.top + r.height / 2, label: label(el).slice(0, 80)};
  }
  const controls = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible);
  const el = controls.find((x) => /Agree and Continue|Agree\\s*&\\s*Continue|同意して続行|同意して支払う|同意する|支払う/i.test(label(x)));
  if (!el) return false;
  el.scrollIntoView({block:'center', inline:'center'});
  const r = el.getBoundingClientRect();
  return {mode:'text', x:r.left + r.width / 2, y:r.top + r.height / 2, label: label(el).slice(0, 80)};
})()
""",
            timeout=timeout,
        )
        if isinstance(target, dict) and target.get("x") is not None:
            log(f"[paypal] final confirmation element click: {target}")
            try:
                self.click_point({"x": float(target["x"]), "y": float(target["y"])})
                return True
            except Exception as exc:
                log(f"[paypal] final confirmation element click failed: {type(exc).__name__}: {exc}")
            selector = str(target.get("selector") or "").strip()
            if selector:
                try:
                    selector_json = json.dumps(selector)
                    dom_clicked = self.js(
                        f"""
(() => {{
  const el = document.querySelector({selector_json});
  if (!el) return false;
  el.scrollIntoView({{block:'center', inline:'center'}});
  el.click();
  return true;
}})()
""",
                        timeout=0.8,
                    )
                    if dom_clicked:
                        log(f"[paypal] final confirmation dom selector fallback click: {selector}")
                        return True
                except Exception as exc:
                    log(f"[paypal] final confirmation dom selector click failed: {type(exc).__name__}: {exc}")
        result = self.dom_click_by_script(
            """
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && !el.disabled && style.visibility !== 'hidden' && style.display !== 'none';
  }
  function label(el) {
    return [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '', el.getAttribute('data-testid') || '']
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  }
  const controls = [...document.querySelectorAll('#consentButton, [data-testid="consentButton"], button[name="consentButton"], button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible);
  const el = controls.find((x) => /consentButton|Agree and Continue|Agree\\s*&\\s*Continue|同意して続行|同意して支払う|同意する|支払う/i.test(label(x)));
  if (!el) return false;
  el.scrollIntoView({block:'center', inline:'center'});
  el.click();
  return {mode:'dom', label: label(el).slice(0, 80)};
})()
""",
            timeout=timeout,
        )
        if result:
            log(f"[paypal] final confirmation dom click: {result}")
            return True
        return False

    def is_paypal_review_url(self, url: str) -> bool:
        return bool(re.search(r"paypal\.com/webapps/hermes.*(?:billingweb/review|billingLite=1)", str(url or ""), re.I))

    def is_paypal_hermes_transition_url(self, url: str) -> bool:
        """PayPal may attach reason=CARD_GENERIC_ERROR before rendering review.

        The manual trace showed this sequence:
        webapps/hermes?...fallback=1&reason=CARD_GENERIC_ERROR
        -> webapps/hermes?...fallback=1&reason=CARD_GENERIC_ERROR&billingLite=1#/billingweb/review
        -> Agree and Continue -> redirect_status=succeeded.

        So a reason= parameter on webapps/hermes is not a terminal failure by
        itself. checkoutweb/genericError remains a real failure.
        """
        return bool(re.search(r"paypal\.com/webapps/hermes", str(url or ""), re.I))

    def paypal_hermes_fallback_reason(self, url: str) -> str:
        if not self.is_paypal_hermes_transition_url(url) or self.is_paypal_review_url(url):
            return ""
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query)
        if (query.get("fallback") or [""])[0] != "1" or "reason" not in query:
            return ""
        return self.decode_reason(url) or (query.get("reason") or [""])[0]

    def is_stripe_pm_redirect_success_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query)
        return (
            parsed.netloc.lower() == "pm-redirects.stripe.com"
            and "/return/" in parsed.path
            and (query.get("status") or [""])[0] == "success"
        )

    def is_security_risk_page(self, title: Any = None, text: str = "") -> bool:
        return bool(
            re.search(
                r"Warning: Security Risk|Potential Security Risk|security risk",
                str(title or "") + "\n" + str(text or ""),
                re.I,
            )
        )

    def retry_stripe_pm_redirect_once(self, url: str) -> None:
        log("[stripe] pm-redirects security warning; re-open return URL once")
        if not self.page:
            return
        try:
            self.page.get(url, timeout=60)
            self.wait_ready(timeout=30)
        except Exception as exc:
            log(f"[stripe] pm-redirects retry navigation raised: {type(exc).__name__}: {exc}")

    def url_has_terminal_reason(self, url: str) -> bool:
        """Return true only for a real query parameter named reason.

        PayPal signup URLs commonly include modxo_redirect_reason=guest_user.
        That is an onboarding marker, not a terminal failure reason.
        """
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query)
        return "reason" in query and not (
            self.is_paypal_review_url(url) or self.is_paypal_hermes_transition_url(url)
        )

    def classify_payment_url(self, url: str, title: Any = None, text: str = "") -> dict[str, Any] | None:
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query)
        host = parsed.netloc.lower()
        redirect_status = (query.get("redirect_status") or [""])[0]
        if self.is_stripe_pm_redirect_success_url(url):
            return {"status": "success", "reason": "stripe_pm_redirect_success", "url": url, "title": title, "text": text[:800]}
        if host == "chatgpt.com" and parsed.path.startswith("/payments/success"):
            return {"status": "success", "reason": "chatgpt_payment_success", "url": url, "title": title, "text": text[:800]}
        if host == "pay.openai.com" and redirect_status == "succeeded":
            return {"status": "success", "reason": "stripe_redirect_succeeded", "url": url, "title": title, "text": text[:800]}
        if host == "pay.openai.com" and (query.get("returned_from_redirect") or [""])[0].lower() == "true":
            return {"status": "success", "reason": "stripe_returned_from_redirect", "url": url, "title": title, "text": text[:800]}
        if redirect_status in {"failed", "canceled"}:
            return {"status": "failed", "reason": f"stripe_redirect_{redirect_status}", "url": url, "title": title, "text": text[:800]}
        if "paypal.com/checkoutweb/genericError" in url:
            paypal_code = self.decode_query_base64(url, "code")
            if paypal_code:
                normalized = re.sub(r"[^a-z0-9]+", "_", paypal_code.lower()).strip("_")
                return {
                    "status": "failed",
                    "reason": f"paypal_{normalized}" if normalized else "paypal_generic_error",
                    "paypalErrorCode": paypal_code,
                    "url": url,
                    "title": title,
                    "text": text[:800],
                }
            return {"status": "failed", "reason": "paypal_generic_error", "url": url, "title": title, "text": text[:800]}
        if any(
            marker in url
            for marker in (
                "paypal.com/myaccount/transfer/homepage",
                "paypal.com/restricted",
                "paypal.com/hostedchallenge",
                "paypal.com/verifycard",
            )
        ):
            return {"status": "failed", "reason": "paypal_blocked", "url": url, "title": title, "text": text[:800]}
        if self.is_paypal_review_url(url) or self.is_paypal_hermes_transition_url(url):
            return None
        if self.url_has_terminal_reason(url):
            return {"status": "failed", "url": url, "reason": self.decode_reason(url), "title": title, "text": text[:800]}
        return None

    def wait_final_result(self, timeout: int = 180) -> dict[str, Any]:
        self.start_success_network_capture()
        deadline = time.time() + timeout
        next_text_check = time.time() + 2.0
        last_url = ""
        hermes_r_error_started = 0.0
        hermes_r_error_logged = False
        pm_redirect_retried: set[str] = set()
        while time.time() < deadline:
            success_url = self.drain_success_network_events()
            if success_url:
                return {
                    "status": "success",
                    "reason": "captured_success_redirect",
                    "url": success_url,
                    "title": None,
                    "text": "",
                }
            url = self.current_url()
            if url != last_url:
                log(f"[result] url: {url}")
                last_url = url
                hermes_r_error_started = 0.0
                hermes_r_error_logged = False
            classified = self.classify_payment_url(url)
            if classified:
                return classified
            hermes_reason = self.paypal_hermes_fallback_reason(url)
            if hermes_reason == "R_ERROR":
                if not hermes_r_error_started:
                    hermes_r_error_started = time.time()
                if not hermes_r_error_logged:
                    log("[paypal] Hermes fallback reason=R_ERROR; waiting briefly for review")
                    hermes_r_error_logged = True
                if time.time() - hermes_r_error_started >= 5:
                    try:
                        info = self.page_info()
                    except Exception:
                        info = {"url": url, "title": "", "text": ""}
                    return {
                        "status": "failed",
                        "reason": "paypal_hermes_r_error",
                        "paypalReason": hermes_reason,
                        "url": info.get("url") or url,
                        "title": info.get("title"),
                        "text": str(info.get("text") or "")[:800],
                    }
            needs_text = (
                self.is_stripe_pm_redirect_success_url(url)
                or "paypal.com/checkoutweb/signup" in url
                or time.time() >= next_text_check
            )
            if needs_text:
                next_text_check = time.time() + 4.0
                info = self.page_info()
                info_url = str(info.get("url") or url)
                if (
                    self.is_stripe_pm_redirect_success_url(info_url)
                    and self.is_security_risk_page(info.get("title"), str(info.get("text") or ""))
                    and info_url not in pm_redirect_retried
                ):
                    pm_redirect_retried.add(info_url)
                    self.retry_stripe_pm_redirect_once(info_url)
                    next_text_check = 0.0
                    time.sleep(1)
                    continue
                classified = self.classify_payment_result(info)
                if classified:
                    return classified
            time.sleep(0.25)
        try:
            info = self.page_info()
        except Exception:
            info = {"url": self.current_url(), "title": "", "text": ""}
        return {"status": "timeout", "url": info.get("url"), "title": info.get("title"), "text": info.get("text", "")[:800]}

    def classify_payment_result(self, info: dict[str, Any]) -> dict[str, Any] | None:
        url = str(info.get("url") or "")
        text = str(info.get("text") or "")
        title = info.get("title")
        url_result = self.classify_payment_url(url, title=title, text=text)
        if url_result:
            return url_result
        if re.search(r"You have been blocked|couldn.?t load the security challenge|Security Challenge", text, re.I):
            return {"status": "failed", "reason": "paypal_security_challenge_blocked", "url": url, "title": title, "text": text[:800]}
        if self.is_stripe_pm_redirect_success_url(url) and self.is_security_risk_page(title, text):
            return {
                "status": "failed",
                "reason": "stripe_pm_redirect_security_warning",
                "url": url,
                "title": title,
                "text": text[:800],
            }
        if re.search(r"Agree and Continue|Agree\s*&\s*Continue|Set up once\. Pay faster next time", text, re.I):
            return None
        if "paypal.com/checkoutweb/signup" in url and re.search(
            r"We weren.?t able to add this card|try a different card|Check all the details are correct",
            text,
            re.I,
        ):
            return {
                "status": "failed",
                "reason": "paypal_card_rejected",
                "url": url,
                "title": title,
                "text": text[:800],
            }
        if "paypal.com/checkoutweb/signup" in url and re.search(
            r"Pay with debit or credit card.*PayPal will text you a code to verify this number.*Create an account to get PayPal benefits",
            text,
            re.I | re.S,
        ):
            return {
                "status": "failed",
                "reason": "paypal_signup_not_advanced_after_sms",
                "url": url,
                "title": title,
                "text": text[:800],
            }
        if re.search(r"Please add a debit or credit card|Things don.t appear to be working|Return to merchant|Your account is limited", text, re.I):
            return {"status": "failed", "reason": "paypal_page_error", "url": url, "title": title, "text": text[:800]}
        return None

    def decode_reason(self, url: str) -> str:
        return self.decode_query_base64(url, "reason")

    def decode_query_base64(self, url: str, key: str) -> str:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            raw = (query.get(key) or [""])[0]
            if not raw:
                return ""
            padded = raw + "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            return ""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-url", default=os.environ.get("START_URL", ""), help="Initial checkout URL; can also use env START_URL.")
    parser.add_argument("--address-json", default=os.environ.get("ADDRESS_JSON", ""), help="Address/profile json generated from the cardholder data.")
    parser.add_argument("--card-json", default=os.environ.get("CARD_JSON", str(DEFAULT_CARD_JSON)), help="Separate card json: cardNumber/cvv/expiry/cardType.")
    parser.add_argument("--sms-line", default=os.environ.get("SMS_LINE", ""), help="PHONE|SMS_API_URL; can also use env SMS_LINE.")
    parser.add_argument("--captcha-api-key", default="", help="2Captcha API key. Prefer env APIKEY_2CAPTCHA.")
    parser.add_argument(
        "--proxy",
        default=os.environ.get("RUYI_PROXY") or os.environ.get("PAYPAL_PROXY") or os.environ.get("PROXY", "system"),
        help=(
            "Proxy mode/URL for Linux Firefox and smart_fingerprint. Default: system "
            "(read Windows system proxy from WSL). Use direct/none to disable."
        ),
    )
    parser.add_argument(
        "--proxy-chain-upstream",
        default=os.environ.get("RUYI_PROXY_CHAIN_UPSTREAM", ""),
        help="Public upstream proxy reached through --proxy-chain-via. Example: gate:1000:user:pass(http).",
    )
    parser.add_argument(
        "--proxy-chain-via",
        default=os.environ.get("RUYI_PROXY_CHAIN_VIA", "system"),
        help="Parent proxy used to reach --proxy-chain-upstream. Default reads Windows system proxy.",
    )
    parser.add_argument(
        "--captcha-proxy",
        default=os.environ.get("RUYI_CAPTCHA_PROXY", ""),
        help="Optional public proxy URL for DataDome 2Captcha fallback. Ignored unless --enable-datadome-2captcha is set.",
    )
    parser.add_argument("--browser-path", default=os.environ.get("RUYI_FIREFOX_PATH", DEFAULT_FIREFOX_PATH), help="Path to Linux ruyi Firefox executable or directory.")
    parser.add_argument("--port", default=os.environ.get("RUYI_REMOTE_PORT", ""), help="Firefox remote debugging port. Default picks a free high port.")
    parser.add_argument("--user-dir", default="", help="Firefox profile directory. Default creates a fresh profile under ./profiles.")
    parser.add_argument("--no-smart-fingerprint", action="store_true", help="Disable ruyiPage smart_fingerprint. Enabled by default.")
    parser.add_argument("--fingerprint-country", default=os.environ.get("RUYI_FP_COUNTRY", "US"), help="Required egress country for smart_fingerprint; empty disables country check.")
    parser.add_argument("--fingerprint-geo-timeout", default=os.environ.get("RUYI_FP_GEO_TIMEOUT", "8"))
    parser.add_argument("--fingerprint-geo-retries", default=os.environ.get("RUYI_FP_GEO_RETRIES", "1"))
    parser.add_argument("--no-fingerprint-ipv6", action="store_true", help="Skip smart_fingerprint IPv6 enrichment.")
    parser.add_argument("--keep-browser-open", action="store_true", help="Do not close Firefox when the script exits.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--action-visual", action="store_true")
    parser.add_argument("--disable-marionette", action="store_true", help="Launch without --marionette if this Firefox build crashes with it.")
    parser.add_argument("--window-size", default=os.environ.get("RUYI_WINDOW_SIZE", "1280x900"), help="Stabilized Firefox window size, e.g. 1280x900.")
    parser.add_argument("--no-stabilize-window", action="store_true", help="Do not center/resize Firefox after startup.")
    parser.add_argument("--locale", default=os.environ.get("RUYI_LOCALE", "en-US"))
    parser.add_argument("--timezone", default=os.environ.get("RUYI_TIMEZONE", "America/New_York"))
    parser.add_argument("--human-algorithm", default=os.environ.get("RUYI_HUMAN_ALGORITHM", "windmouse"), choices=["bezier", "windmouse"])
    parser.add_argument("--human-profile", default=os.environ.get("RUYI_HUMAN_PROFILE", "conservative"), choices=["off", "fast", "conservative"])
    parser.add_argument("--no-captcha-handling", action="store_true", help="Disable reCAPTCHA/hCaptcha/DataDome handling.")
    parser.add_argument("--enable-captcha-handling", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--enable-paypal-recaptcha-2captcha",
        action="store_true",
        default=os.environ.get("PAYPAL_RECAPTCHA_2CAPTCHA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Enable 2Captcha for PayPal reCAPTCHA Enterprise authchallenge. Default fails fast because solved tokens have still returned PayPal RESTRICTED_USER in JP tests.",
    )
    parser.add_argument(
        "--enable-datadome-2captcha",
        action="store_true",
        help="Enable legacy 2Captcha DataDome cookie fallback. Default is local ruyi/DDC slider solving only.",
    )
    parser.add_argument("--captcha-wait", default="300", help="Seconds to wait for and solve CAPTCHA.")
    parser.add_argument("--captcha-detect-wait", default="60", help="Seconds to watch for a CAPTCHA page before continuing.")
    parser.add_argument("--sms-page-wait", default="120", help="Seconds to wait for the SMS verification page.")
    parser.add_argument("--sms-timeout", default="75")
    parser.add_argument("--result-json", default="recordings/last_ruyi_paypal_result.json")
    return parser


def main() -> int:
    load_local_env()
    parser = build_arg_parser()
    args = parser.parse_args()
    if not args.start_url:
        parser.error("--start-url or env START_URL is required")
    if not args.sms_line:
        parser.error("--sms-line or env SMS_LINE is required")
    if not args.address_json:
        parser.error("--address-json or env ADDRESS_JSON is required")
    profile = load_json(Path(args.address_json))
    flow = RuyiPayPalFlow(args, profile)
    try:
        flow.start()
        try:
            result = flow.run()
        except FlowFailed as exc:
            result = exc.result
        except Exception as exc:
            result = flow.annotate_result(
                {
                    "status": "failed",
                    "reason": classify_unhandled_failure(exc),
                    "error": str(exc),
                    "exceptionType": type(exc).__name__,
                    "url": flow.current_url(),
                }
            )
        result = flow.annotate_result(result)
        if result.get("status") != "success" and not args.headless:
            flow.save_result_screenshot(result)
        out = Path(args.result_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    finally:
        flow.close()


if __name__ == "__main__":
    raise SystemExit(main())
