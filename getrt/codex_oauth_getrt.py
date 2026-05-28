#!/usr/bin/env python3
"""Protocol-first Codex OAuth refresh-token exporter.

This module intentionally lives under getrt/ and is isolated from the protocol
registrar and payment automation.  It reuses the protocol registrar package as
a library only to complete the OpenAI email-login session, then runs the Codex
OAuth PKCE authorize/token exchange in the same HTTP cookie jar.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse


DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"
DEFAULT_PROTOCOL_PROJECT = Path(__file__).resolve().parents[1] / "protocol" / "gpt_trial_protocol"
EMAIL_VERIFICATION_SHORT_TIMEOUT_SECONDS = 12.0
EMAIL_VERIFICATION_RESTARTS = 1
PHONE_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_datetime_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce() -> tuple[str, str]:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state() -> str:
    return b64url(secrets.token_bytes(32))


def parse_jwt_claims(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_auth_claim(claims: dict[str, Any]) -> dict[str, Any]:
    value = claims.get("https://api.openai.com/auth")
    return value if isinstance(value, dict) else {}


def extract_email(*claims_list: dict[str, Any]) -> str | None:
    for claims in claims_list:
        if isinstance(claims.get("email"), str):
            return str(claims["email"])
        profile = claims.get("https://api.openai.com/profile")
        if isinstance(profile, dict) and isinstance(profile.get("email"), str):
            return str(profile["email"])
    return None


def extract_account_id(*claims_list: dict[str, Any]) -> str | None:
    for claims in claims_list:
        auth_claim = extract_auth_claim(claims)
        for key in ("chatgpt_account_id", "account_id", "organization_id"):
            value = auth_claim.get(key) or claims.get(key)
            if isinstance(value, str) and value:
                return value
        orgs = claims.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            value = orgs[0].get("id")
            if isinstance(value, str) and value:
                return value
    return None


def build_authorize_url(
    *,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
    prompt: str | None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "codex_cli_simplified_flow": "true",
        "id_token_add_organizations": "true",
        "state": state,
    }
    if prompt:
        params["prompt"] = prompt
    return f"{issuer.rstrip('/')}/oauth/authorize?{urlencode(params)}"


def callback_code_from_url(url: str, expected_state: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return None
    params = parse_qs(parsed.query)
    error = params.get("error", [None])[0]
    if error:
        detail = params.get("error_description", [error])[0]
        raise RuntimeError(f"OAuth authorize returned error: {detail}")
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    if code and state != expected_state:
        raise RuntimeError("OAuth callback state mismatch")
    return code


def looks_like_html(content: bytes) -> bool:
    head = content[:512].lower()
    return b"<html" in head or b"<!doctype html" in head


def add_protocol_project_to_path(path: Path) -> None:
    src = path / "src"
    if not src.exists():
        raise FileNotFoundError(f"protocol project src not found: {src}")
    sys.path.insert(0, str(src))


def normalize_prompt(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def safe_token_preview(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-6:]}"


def is_fresh_email_code_timeout(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "fresh email code not found" in text or "fresh code not found" in text


def response_text_preview(response: Any, limit: int = 500) -> str:
    text = getattr(response, "text", "") or ""
    text = " ".join(text.split())
    return text[:limit]


def normalize_phone_number(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    prefix = "+" if text.startswith("+") else ""
    digits = re.sub(r"\D+", "", text)
    if not prefix and len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"{prefix}{digits}" if digits else ""


def redact_phone(value: str) -> str:
    text = normalize_phone_number(value)
    if len(text) <= 4:
        return "***"
    return "*" * max(3, len(text) - 4) + text[-4:]


def parse_phone_line(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "----" in text:
        phone, api = text.split("----", 1)
    elif "|" in text:
        phone, api = text.split("|", 1)
    else:
        return normalize_phone_number(text), ""
    return normalize_phone_number(phone), api.strip()


def extract_phone_otp(raw: str) -> str:
    candidates: list[str] = []
    text = str(raw or "")
    try:
        decoded = json.loads(text)
    except Exception:
        decoded = None
    stack: list[Any] = [decoded] if decoded is not None else []
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            candidates.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    parts = [part.strip() for part in text.split("|") if part.strip()]
    candidates.extend(parts)
    candidates.append(text)
    candidates = sorted(
        [candidate for candidate in candidates if candidate],
        key=lambda value: 0 if re.search(r"openai|chatgpt|code|verification|验证码|otp", value, re.I) else 1,
    )
    for candidate in candidates:
        match = PHONE_OTP_RE.search(candidate)
        if match:
            return match.group(1)
    return ""


def fetch_phone_otp_from_api(api_url: str) -> tuple[str, str, str]:
    if not api_url:
        return "", "", ""
    if api_url.startswith("http://a.62-us.com/"):
        api_url = "https://" + api_url[len("http://") :]
    req = urllib.request.Request(api_url, headers={"user-agent": "Mozilla/5.0", "accept": "text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    code = extract_phone_otp(raw)
    return code, f"{code}|{raw.strip()}", raw


@dataclass(slots=True)
class PhoneOtpConfig:
    phone_number: str = ""
    api_url: str = ""
    static_otp: str = ""
    otp_cmd: str = ""
    timeout: float = 90.0
    interval: float = 2.0

    def enabled(self) -> bool:
        return bool(self.phone_number and (self.api_url or self.static_otp or self.otp_cmd))

    def capture_baseline(self) -> str:
        if not self.api_url:
            return ""
        try:
            return fetch_phone_otp_from_api(self.api_url)[1]
        except Exception:
            return ""

    def read_cmd_otp(self) -> str:
        if not self.otp_cmd:
            return ""
        try:
            out = subprocess.check_output(self.otp_cmd, shell=True, text=True, timeout=20)
            return extract_phone_otp(out or "")
        except Exception:
            return ""

    def wait_code(self, *, baseline: str = "") -> str:
        static = extract_phone_otp(self.static_otp)
        if static:
            return static
        deadline = time.time() + max(10.0, float(self.timeout))
        last_text = ""
        while time.time() < deadline:
            cmd_code = self.read_cmd_otp()
            if cmd_code:
                return cmd_code
            if self.api_url:
                try:
                    code, signature, raw = fetch_phone_otp_from_api(self.api_url)
                    last_text = raw
                    if code and signature != baseline:
                        return code
                except Exception as exc:
                    last_text = repr(exc)
            time.sleep(max(0.5, float(self.interval)))
        raise TimeoutError(f"phone OTP not found before timeout; last response: {last_text[:300]}")


def build_phone_otp_config(args: argparse.Namespace) -> PhoneOtpConfig | None:
    line_phone, line_api = parse_phone_line(args.phone_line)
    phone = normalize_phone_number(args.phone_number) or line_phone
    api = (args.phone_otp_api or line_api or "").strip()
    config = PhoneOtpConfig(
        phone_number=phone,
        api_url=api,
        static_otp=args.phone_otp or "",
        otp_cmd=args.phone_otp_cmd or "",
        timeout=float(args.phone_otp_timeout),
    )
    return config if config.enabled() else None


def email_code_snapshot(code_client: Any, email: str) -> dict[str, Any]:
    try:
        result = code_client.extract(email, refresh=True)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    raw = result.raw if isinstance(result.raw, dict) else {}
    latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else {}
    return {
        "email": result.email or email,
        "latestCode": raw.get("latestCode") or latest.get("code") or result.latest_code or "",
        "latestCodeTime": raw.get("latestCodeTime") or latest.get("date") or raw.get("mailTime") or "",
        "mailTime": raw.get("mailTime") or latest.get("date") or raw.get("latestCodeTime") or "",
        "generatedAt": raw.get("generatedAt") or "",
        "source": raw.get("source") or "",
        "count": raw.get("count"),
    }


def import_protocol_modules() -> dict[str, Any]:
    from gpt_trial_protocol.chatgpt import ChatGPTProtocolClient
    from gpt_trial_protocol.email_code import DEFAULT_EMAIL_CODE_BASE_URL, EmailCodeClient, EmailCodeProvider
    from gpt_trial_protocol.flows import ProtocolRegistrarFlow
    from gpt_trial_protocol.http_client import ProtocolHttpClient, json_or_empty, require_ok
    from gpt_trial_protocol.models import BrowserProfile, ProtocolConfig
    from gpt_trial_protocol import sentinel_http
    from gpt_trial_protocol.sentinel_http import SentinelHttpTokenProvider
    from gpt_trial_protocol.service import EmailItem, effective_email_code_provider, normalize_email_item

    # The auth web app asks Sentinel for the "authorize_continue" flow before
    # posting username to /api/accounts/authorize/continue.  The protocol
    # registrar only needed "oauth_create_account"; extend the in-process map
    # here without changing that independent project.
    sentinel_http.DEFAULT_FLOW_BY_PURPOSE.setdefault("authorize_continue", "authorize_continue")

    return {
        "DEFAULT_EMAIL_CODE_BASE_URL": DEFAULT_EMAIL_CODE_BASE_URL,
        "BrowserProfile": BrowserProfile,
        "ChatGPTProtocolClient": ChatGPTProtocolClient,
        "EmailCodeClient": EmailCodeClient,
        "EmailCodeProvider": EmailCodeProvider,
        "EmailItem": EmailItem,
        "ProtocolConfig": ProtocolConfig,
        "ProtocolHttpClient": ProtocolHttpClient,
        "ProtocolRegistrarFlow": ProtocolRegistrarFlow,
        "SentinelHttpTokenProvider": SentinelHttpTokenProvider,
        "effective_email_code_provider": effective_email_code_provider,
        "json_or_empty": json_or_empty,
        "normalize_email_item": normalize_email_item,
        "require_ok": require_ok,
    }


class CodexOAuthProtocol:
    def __init__(
        self,
        *,
        http: Any,
        profile: Any,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        scope: str,
        trace_dir: Path | None,
        sentinel_provider: Any | None = None,
        phone_otp: PhoneOtpConfig | None = None,
    ) -> None:
        self.http = http
        self.profile = profile
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.trace_dir = trace_dir
        self.sentinel_provider = sentinel_provider
        self.phone_otp = phone_otp

    def request_authorize_code(
        self,
        *,
        prompt: str | None,
        email: str,
        chatgpt: Any,
        code_client: Any,
        code_timeout: float,
        max_redirects: int = 30,
    ) -> tuple[str | None, dict[str, Any]]:
        for attempt in range(EMAIL_VERIFICATION_RESTARTS + 1):
            verifier, challenge = generate_pkce()
            state = generate_state()
            oauth_not_before = datetime.now(timezone.utc)
            url = build_authorize_url(
                issuer=self.issuer,
                client_id=self.client_id,
                redirect_uri=self.redirect_uri,
                code_challenge=challenge,
                state=state,
                scope=self.scope,
                prompt=prompt,
            )
            referer = "https://chatgpt.com/"
            chain: list[dict[str, Any]] = []
            last_response: Any | None = None

            try:
                for _ in range(max_redirects + 1):
                    callback_code = callback_code_from_url(url, state)
                    if callback_code:
                        return callback_code, {"codeVerifier": verifier, "state": state, "authorizeUrl": url, "redirectChain": chain}

                    response = self.http.get(url, headers=self.profile.browser_headers(referer=referer))
                    last_response = response
                    status = int(getattr(response, "status_code", 0))
                    location = response.headers.get("location") if getattr(response, "headers", None) else None
                    chain.append({"url": str(getattr(response, "url", url)), "status": status, "location": location})
                    if status not in {301, 302, 303, 307, 308} or not location:
                        current_url = str(getattr(response, "url", url))
                        if urlparse(current_url).path.rstrip("/") == "/add-phone":
                            handled_url = self._handle_add_phone_verification(current_url)
                            if handled_url:
                                referer = current_url
                                url = handled_url
                                continue
                            self._save_authorize_blocker(response)
                            return None, {
                                "codeVerifier": verifier,
                                "state": state,
                                "authorizeUrl": url,
                                "redirectChain": chain,
                                "blockedAt": current_url,
                                "status": status,
                                "reason": "add_phone_required",
                                "requiresPhone": True,
                                "contentType": response.headers.get("content-type") if getattr(response, "headers", None) else None,
                                "textPreview": response_text_preview(response),
                            }
                        handled_url = self._handle_interactive_auth_page(
                            response,
                            email=email,
                            chatgpt=chatgpt,
                            code_client=code_client,
                            code_timeout=code_timeout,
                            not_before=oauth_not_before,
                        )
                        if handled_url:
                            referer = str(getattr(response, "url", url))
                            url = handled_url
                            continue
                        self._save_authorize_blocker(response)
                        return None, {
                            "codeVerifier": verifier,
                            "state": state,
                            "authorizeUrl": url,
                            "redirectChain": chain,
                            "blockedAt": str(getattr(response, "url", url)),
                            "status": status,
                            "contentType": response.headers.get("content-type") if getattr(response, "headers", None) else None,
                            "textPreview": response_text_preview(response),
                        }
                    referer = str(getattr(response, "url", url))
                    url = urljoin(referer, location)

                return None, {"codeVerifier": verifier, "state": state, "authorizeUrl": url, "redirectChain": chain, "reason": "too_many_redirects"}
            except TimeoutError as exc:
                if not is_fresh_email_code_timeout(exc):
                    raise
                if attempt < EMAIL_VERIFICATION_RESTARTS:
                    continue
                current_url = str(getattr(last_response, "url", url)) if last_response is not None else url
                status = int(getattr(last_response, "status_code", 0)) if last_response is not None else 408
                return None, {
                    "codeVerifier": verifier,
                    "state": state,
                    "authorizeUrl": url,
                    "redirectChain": chain,
                    "blockedAt": current_url,
                    "status": status or 408,
                    "reason": "fresh_email_code_timeout",
                    "retryable": True,
                    "attempt": attempt + 1,
                    "restarts": EMAIL_VERIFICATION_RESTARTS,
                    "contentType": last_response.headers.get("content-type") if last_response is not None and getattr(last_response, "headers", None) else None,
                    "textPreview": response_text_preview(last_response) if last_response is not None else "",
                }

        return None, {"reason": "fresh_email_code_timeout", "retryable": True, "restarts": EMAIL_VERIFICATION_RESTARTS}

    def _handle_interactive_auth_page(
        self,
        response: Any,
        *,
        email: str,
        chatgpt: Any,
        code_client: Any,
        code_timeout: float,
        not_before: datetime,
    ) -> str | None:
        """Handle OAuth challenge pages that can be completed by protocol.

        Observed Codex authorize path:
        /oauth/authorize -> /api/oauth/oauth2/auth -> /api/accounts/login
        -> /log-in.  The React login form posts to /api/accounts/authorize/continue
        with the username, then the existing passwordless OTP endpoints can be
        reused for the email code.
        """
        current_url = str(getattr(response, "url", "") or "")
        path = urlparse(current_url).path.rstrip("/")
        if path == "/log-in":
            return self._submit_authorize_continue(email=email, referer=current_url)
        if path in {"/log-in/password", "/create-account/password"}:
            return self._complete_passwordless_otp(
                email=email,
                referer=current_url,
                chatgpt=chatgpt,
                code_client=code_client,
                code_timeout=code_timeout,
                not_before=not_before,
            )
        if path == "/email-verification":
            return self._validate_email_otp(
                email=email,
                code_client=code_client,
                code_timeout=min(code_timeout, EMAIL_VERIFICATION_SHORT_TIMEOUT_SECONDS),
                fallback=current_url,
                not_before=not_before,
            )
        if path == "/sign-in-with-chatgpt/codex/consent":
            return self._submit_codex_consent(response, referer=current_url)
        return None

    def _phone_headers(self, referer: str) -> dict[str, str]:
        return self.profile.xhr_headers(
            referer=referer,
            content_type="application/json",
            accept="application/json",
            origin=self.issuer,
            fetch_site="same-origin",
        )

    def _handle_add_phone_verification(self, current_url: str) -> str | None:
        if not self.phone_otp or not self.phone_otp.enabled():
            return None
        print(f"[oauth] add-phone required; sending phone OTP to {redact_phone(self.phone_otp.phone_number)}")
        baseline = self.phone_otp.capture_baseline()
        send_response = self.http.post(
            f"{self.issuer}/api/accounts/add-phone/send",
            headers=self._phone_headers(f"{self.issuer}/add-phone"),
            json={"phone_number": self.phone_otp.phone_number},
        )
        if int(getattr(send_response, "status_code", 0)) >= 400:
            raise RuntimeError(f"add-phone/send failed: HTTP {send_response.status_code}: {response_text_preview(send_response, limit=500)}")
        send_payload = self._json_or_empty(send_response)
        send_next = self._payload_continue_url(send_payload, fallback=f"{self.issuer}/phone-verification")
        code = self.phone_otp.wait_code(baseline=baseline)
        validate_response = self.http.post(
            f"{self.issuer}/api/accounts/phone-otp/validate",
            headers=self._phone_headers(send_next or f"{self.issuer}/phone-verification"),
            json={"code": code},
        )
        if int(getattr(validate_response, "status_code", 0)) >= 400:
            raise RuntimeError(f"phone-otp/validate failed: HTTP {validate_response.status_code}: {response_text_preview(validate_response, limit=500)}")
        payload = self._json_or_empty(validate_response)
        location = validate_response.headers.get("location") if getattr(validate_response, "headers", None) else None
        if location:
            return urljoin(str(getattr(validate_response, "url", current_url)), location)
        next_url = self._payload_continue_url(payload, fallback=None)
        print("[oauth] add-phone verified")
        return next_url

    def _submit_authorize_continue(self, *, email: str, referer: str) -> str | None:
        sentinel_headers: dict[str, str] = {}
        if self.sentinel_provider is not None:
            bundle = self.sentinel_provider.get_openai_sentinel(purpose="authorize_continue")
            sentinel_headers = bundle.sentinel.as_headers()
        response = self.http.post(
            f"{self.issuer}/api/accounts/authorize/continue",
            headers=self.profile.xhr_headers(
                referer=referer,
                content_type="application/json",
                accept="application/json",
                origin=self.issuer,
                fetch_site="same-origin",
            )
            | sentinel_headers,
            json={"username": {"kind": "email", "value": email}},
        )
        location = response.headers.get("location") if getattr(response, "headers", None) else None
        if location:
            return urljoin(str(getattr(response, "url", referer)), location)
        payload = self._json_or_empty(response)
        return self._payload_continue_url(payload, fallback=None)

    def _complete_passwordless_otp(
        self,
        *,
        email: str,
        referer: str,
        chatgpt: Any,
        code_client: Any,
        code_timeout: float,
        not_before: datetime,
    ) -> str | None:
        sent = chatgpt.send_passwordless_otp(referer=referer)
        next_url = self._payload_continue_url(sent, fallback=f"{self.issuer}/email-verification")
        if next_url:
            response = self.http.get(next_url, headers=self.profile.browser_headers(referer=referer))
            # If the send endpoint itself advances to a redirect, continue there.
            location = response.headers.get("location") if getattr(response, "headers", None) else None
            if location and int(getattr(response, "status_code", 0)) in {301, 302, 303, 307, 308}:
                return urljoin(str(getattr(response, "url", next_url)), location)
        return self._validate_email_otp(
            email=email,
            code_client=code_client,
            code_timeout=code_timeout,
            fallback=f"{self.issuer}/email-verification",
            not_before=not_before,
        )

    def _validate_email_otp(
        self,
        *,
        email: str,
        code_client: Any,
        code_timeout: float,
        fallback: str | None,
        not_before: datetime | None = None,
    ) -> str | None:
        not_before = not_before or datetime.now(timezone.utc)
        result = code_client.wait_for_fresh_code(email, not_before=not_before, timeout=code_timeout)
        if not result.latest_code:
            raise TimeoutError(f"fresh code not found for {email}")
        response = self.http.post(
            f"{self.issuer}/api/accounts/email-otp/validate",
            headers=self.profile.browser_headers(
                referer=f"{self.issuer}/email-verification",
                content_type="application/json",
                accept="application/json",
            ),
            json={"code": result.latest_code},
        )
        payload = self._json_or_empty(response)
        return self._payload_continue_url(payload, fallback=fallback)

    def _submit_codex_consent(self, response: Any, *, referer: str) -> str | None:
        workspace_id = self._extract_workspace_id(getattr(response, "text", "") or "")
        if not workspace_id:
            return None
        response = self.http.post(
            f"{self.issuer}/api/accounts/workspace/select",
            headers=self.profile.xhr_headers(
                referer=referer,
                content_type="application/json",
                accept="application/json",
                origin=self.issuer,
                fetch_site="same-origin",
            ),
            json={"workspace_id": workspace_id},
        )
        location = response.headers.get("location") if getattr(response, "headers", None) else None
        if location:
            return urljoin(str(getattr(response, "url", referer)), location)
        payload = self._json_or_empty(response)
        return self._payload_continue_url(payload, fallback=None)

    @staticmethod
    def _extract_workspace_id(html_text: str) -> str | None:
        """Extract selected workspace id from React Router streamed loader data."""
        # First decode the React Router stream chunk if present.
        stream_texts: list[str] = []
        for match in re.finditer(r"streamController\.enqueue\((\".*?\")\)", html_text, flags=re.S):
            try:
                stream_texts.append(json.loads(match.group(1)))
            except Exception:
                continue
        stream_texts.append(html_text)
        for text in stream_texts:
            match = re.search(r'"workspaces",\[\d+\],\{.*?\},"id","([^"]+)"', text, flags=re.S)
            if match:
                return match.group(1)
            match = re.search(r'"current_workspace_id","([^"]+)"', text)
            if match:
                return match.group(1)
        return None

    def _payload_continue_url(self, payload: dict[str, Any], *, fallback: str | None) -> str | None:
        url = (
            payload.get("continue_url")
            or payload.get("continueUrl")
            or payload.get("redirect_url")
            or payload.get("redirectUrl")
            or payload.get("url")
            or fallback
        )
        if not url:
            return None
        url = str(url)
        if url.startswith("/"):
            return f"{self.issuer}{url}"
        return url

    @staticmethod
    def _json_or_empty(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        response = self.http.post(
            f"{self.issuer}/oauth/token",
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": self.profile.user_agent,
            },
            data=body,
        )
        if int(getattr(response, "status_code", 0)) >= 400:
            raise RuntimeError(f"token exchange failed: HTTP {response.status_code}: {response_text_preview(response, limit=1000)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("token exchange did not return a JSON object")
        return payload

    def _save_authorize_blocker(self, response: Any) -> None:
        if not self.trace_dir:
            return
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        content = getattr(response, "content", b"") or b""
        suffix = ".html" if looks_like_html(content) else ".txt"
        path = self.trace_dir / f"authorize_blocker{suffix}"
        path.write_bytes(content)


def jwt_datetime(claims: dict[str, Any], key: str) -> datetime | None:
    value = claims.get(key)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    return None


def datetime_from_any(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def nested_dict(value: Any, key: str) -> dict[str, Any]:
    child = value.get(key) if isinstance(value, dict) else None
    return child if isinstance(child, dict) else {}


def compact_json(value: Any) -> Any:
    if isinstance(value, list):
        compacted = [compact_json(item) for item in value]
        return [item for item in compacted if item is not None]
    if isinstance(value, dict):
        compacted = {
            key: compact_json(child)
            for key, child in value.items()
        }
        return {key: child for key, child in compacted.items() if child is not None} or None
    if value is None or value == "":
        return None
    return value


def epoch_seconds(value: Any) -> int:
    if value in {None, ""}:
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000 if number > 1e11 else number)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def synthetic_id_token(*, email: str | None, account_id: str | None, plan_type: str | None, user_id: str | None, expires_at: datetime | None) -> str | None:
    if not account_id:
        return None
    now = int(time.time())
    auth: dict[str, Any] = {"chatgpt_account_id": account_id}
    if plan_type:
        auth["chatgpt_plan_type"] = plan_type
    if user_id:
        auth["chatgpt_user_id"] = user_id
        auth["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now,
        # Keep the converter's placeholder shape.  CPA callers rely on the
        # access token; this only fills the id_token slot when no real one was issued.
        "ep": epoch_seconds(to_utc_z(expires_at)) if expires_at else now + 7776000,
        "https://api.openai.com/auth": auth,
    }
    if email:
        payload["email"] = email
    header = {"alg": "none", "typ": "JWT", "cpa_synthetic": True}
    return f"{b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))}.{b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}."


def token_metadata(tokens: dict[str, Any], *, email: str, client_id: str, plan_type: str) -> dict[str, Any]:
    token_obj = nested_dict(tokens, "token")
    credentials = nested_dict(tokens, "credentials")
    user = nested_dict(tokens, "user")
    account = nested_dict(tokens, "account")
    access_token = first_text(tokens.get("accessToken"), tokens.get("access_token"), token_obj.get("accessToken"), credentials.get("access_token")) or ""
    refresh_token = first_text(tokens.get("refreshToken"), tokens.get("refresh_token"), token_obj.get("refreshToken")) or ""
    raw_id_token = first_text(tokens.get("idToken"), tokens.get("id_token"), token_obj.get("idToken"))
    session_token = first_text(tokens.get("sessionToken"), tokens.get("session_token"), token_obj.get("sessionToken"))
    id_claims = parse_jwt_claims(raw_id_token)
    access_claims = parse_jwt_claims(access_token)
    expires_in = tokens.get("expires_in") if isinstance(tokens.get("expires_in"), (int, float)) else None
    issued_at = jwt_datetime(access_claims, "iat") or utc_datetime_now()
    expires_at = (
        datetime_from_any(access_claims.get("exp"))
        or datetime_from_any(tokens.get("expires"))
        or datetime_from_any(tokens.get("expired"))
        or datetime_from_any(tokens.get("expires_at"))
        or datetime.fromtimestamp(time.time() + float(expires_in if expires_in is not None else 3600), timezone.utc)
    )
    id_auth = extract_auth_claim(id_claims)
    access_auth = extract_auth_claim(access_claims)
    organizations = id_auth.get("organizations") if isinstance(id_auth.get("organizations"), list) else []
    default_org = next((org for org in organizations if isinstance(org, dict) and org.get("is_default")), None)
    organization_id = (
        id_auth.get("organization_id")
        or access_auth.get("organization_id")
        or (default_org.get("id") if isinstance(default_org, dict) else None)
        or (organizations[0].get("id") if organizations and isinstance(organizations[0], dict) else None)
    )
    chatgpt_account_id = (
        account.get("id")
        or tokens.get("account_id")
        or access_auth.get("chatgpt_account_id")
        or id_auth.get("chatgpt_account_id")
        or extract_account_id(id_claims, access_claims)
    )
    chatgpt_user_id = user.get("id") or access_auth.get("chatgpt_user_id") or id_auth.get("chatgpt_user_id") or id_auth.get("user_id")
    detected_plan_type = (
        account.get("planType")
        or account.get("plan_type")
        or id_auth.get("chatgpt_plan_type")
        or access_auth.get("chatgpt_plan_type")
        or plan_type
    )
    detected_email = first_text(
        user.get("email"),
        tokens.get("email"),
        credentials.get("email"),
        extract_email(id_claims, access_claims),
        email,
    )
    id_token = raw_id_token or synthetic_id_token(
        email=detected_email,
        account_id=chatgpt_account_id,
        plan_type=detected_plan_type,
        user_id=chatgpt_user_id,
        expires_at=expires_at,
    ) or ""
    return {
        "id_claims": id_claims,
        "access_claims": access_claims,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "email": detected_email,
        "account_id": chatgpt_account_id or extract_account_id(id_claims, access_claims),
        "chatgpt_account_id": chatgpt_account_id,
        "chatgpt_user_id": chatgpt_user_id,
        "organization_id": organization_id,
        "plan_type": detected_plan_type,
        "client_id": tokens.get("client_id") or access_claims.get("client_id") or client_id,
        "expires_in": expires_in if expires_in is not None else max(0, int(expires_at.timestamp() - time.time())),
        "expires_at": expires_at,
        "issued_at": issued_at,
    }


def build_codex_json_output(tokens: dict[str, Any], *, email: str, client_id: str, plan_type: str = "unknown") -> dict[str, Any]:
    meta = token_metadata(tokens, email=email, client_id=client_id, plan_type=plan_type)
    return {
        "type": "codex",
        "email": meta["email"],
        "access_token": meta["access_token"],
        "refresh_token": meta["refresh_token"],
        "id_token": meta["id_token"],
        "expires_in": meta["expires_in"],
        "expires_at": to_utc_z(meta["expires_at"]),
        "account_id": meta["account_id"],
        "chatgpt_user_id": meta["chatgpt_user_id"],
        "organization_id": meta["organization_id"],
        "plan_type": meta["plan_type"],
        "client_id": meta["client_id"],
        "created_at": utc_now(),
    }


def build_cpa_output(
    tokens: dict[str, Any],
    *,
    email: str,
    client_id: str,
    plan_type: str,
    proxy_url: str | None = None,
    proxy_strict: bool = False,
    prefix: str | None = None,
) -> dict[str, Any]:
    meta = token_metadata(tokens, email=email, client_id=client_id, plan_type=plan_type)
    now = utc_now()
    payload: dict[str, Any] = {
        "access_token": meta["access_token"],
        "account_id": meta["account_id"],
        "email": meta["email"],
        "expired": to_utc_z(meta["expires_at"]),
        "last_refresh": now,
        "refresh_token": meta["refresh_token"],
        "type": "codex",
    }
    if prefix:
        payload["prefix"] = prefix
    if proxy_url:
        payload["proxy_url"] = proxy_url
        payload["proxy_strict"] = proxy_strict
    return compact_json(payload) or {}


def build_sub2_output(
    tokens: dict[str, Any],
    *,
    email: str,
    client_id: str,
    plan_type: str,
    name_prefix: str,
    account_name: str | None,
    concurrency: int,
    priority: int,
) -> dict[str, Any]:
    meta = token_metadata(tokens, email=email, client_id=client_id, plan_type=plan_type)
    display_name = account_name or f"{name_prefix}{meta['email'] or meta['account_id'] or 'codex'}"
    account = {
        "name": display_name,
        "platform": "openai",
        "type": "oauth",
        "concurrency": concurrency,
        "priority": priority,
        "credentials": {
            "access_token": meta["access_token"],
            "refresh_token": meta["refresh_token"],
            "chatgpt_account_id": meta["chatgpt_account_id"],
            "chatgpt_user_id": meta["chatgpt_user_id"],
            "email": meta["email"],
            "expires_at": to_utc_z(meta["expires_at"]),
            "expires_in": meta["expires_in"],
            "plan_type": meta["plan_type"],
        },
        "extra": {
            "email": meta["email"],
            "name": display_name,
            "auth_provider": tokens.get("authProvider"),
            "source": "chatgpt_web_session",
            "last_refresh": utc_now(),
        },
    }
    return {
        "exported_at": utc_now(),
        "proxies": [],
        "accounts": [compact_json(account) or {}],
    }


def build_formatted_output(
    tokens: dict[str, Any],
    *,
    output_format: str,
    email: str,
    client_id: str,
    plan_type: str,
    proxy_url: str | None,
    proxy_strict: bool,
    prefix: str | None,
    name_prefix: str,
    account_name: str | None,
    concurrency: int,
    priority: int,
) -> dict[str, Any]:
    if output_format == "cpa":
        return build_cpa_output(
            tokens,
            email=email,
            client_id=client_id,
            plan_type=plan_type,
            proxy_url=proxy_url,
            proxy_strict=proxy_strict,
            prefix=prefix,
        )
    if output_format in {"sub2", "sub2api"}:
        return build_sub2_output(
            tokens,
            email=email,
            client_id=client_id,
            plan_type=plan_type,
            name_prefix=name_prefix,
            account_name=account_name,
            concurrency=concurrency,
            priority=priority,
        )
    if output_format in {"codex", "raw"}:
        return {"ok": True, "stage": "token", **build_codex_json_output(tokens, email=email, client_id=client_id, plan_type=plan_type)}
    raise ValueError(f"unsupported output format: {output_format}")


def add_no_refresh_metadata(payload: dict[str, Any], *, output_format: str, reason: str, probe: dict[str, Any]) -> dict[str, Any]:
    """Mark an otherwise normally formatted payload as a no-refresh-token result."""
    if "accounts" in payload and isinstance(payload.get("accounts"), list):
        for account in payload["accounts"]:
            if not isinstance(account, dict):
                continue
            account["refresh_token_missing_reason"] = reason
            credentials = account.get("credentials")
            if isinstance(credentials, dict):
                credentials["refresh_token_missing_reason"] = reason
    else:
        payload["refresh_token_missing_reason"] = reason
        payload.setdefault("refresh_token", "")
    payload["requires_phone"] = reason == "add_phone_required"
    if output_format in {"codex", "raw"}:
        payload["ok"] = True
        payload["stage"] = reason
        payload["has_refresh_token"] = False
        payload["probe"] = {
            "blockedAt": probe.get("blockedAt"),
            "reason": probe.get("reason"),
            "requiresPhone": probe.get("requiresPhone"),
        }
    return payload


def build_no_refresh_output(
    *,
    output_format: str,
    email: str,
    client_id: str,
    plan_type: str,
    reason: str,
    probe: dict[str, Any],
    session_access_token: str | None,
    proxy_url: str | None,
    proxy_strict: bool,
    prefix: str | None,
    name_prefix: str,
    account_name: str | None,
    concurrency: int,
    priority: int,
) -> dict[str, Any]:
    tokens = {
        "access_token": session_access_token or "",
        "refresh_token": "",
        "id_token": "",
        "expires_in": 0,
    }
    payload = build_formatted_output(
        tokens,
        output_format=output_format,
        email=email,
        client_id=client_id,
        plan_type=plan_type,
        proxy_url=proxy_url,
        proxy_strict=proxy_strict,
        prefix=prefix,
        name_prefix=name_prefix,
        account_name=account_name,
        concurrency=concurrency,
        priority=priority,
    )
    return add_no_refresh_metadata(payload, output_format=output_format, reason=reason, probe=probe)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Protocol-first Codex OAuth refresh-token exporter.")
    parser.add_argument("--email", required=True, help="Existing OpenAI account email or local-part when --email-type is custom/icloud.")
    parser.add_argument("--email-type", choices=["auto", "icloud", "custom"], default="auto")
    parser.add_argument("--email-code-provider", choices=["auto", "extract_json", "openai_code_json"], default="auto")
    parser.add_argument("--email-code-base-url")
    parser.add_argument("--proxy", default=os.environ.get("GPT_TRIAL_PROXY") or "http://127.0.0.1:7897")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--protocol-project", type=Path, default=DEFAULT_PROTOCOL_PROJECT)
    parser.add_argument("--issuer", default=DEFAULT_ISSUER)
    parser.add_argument("--client-id", default=os.environ.get("CODEX_CLIENT_ID", DEFAULT_CLIENT_ID))
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--output-format",
        "--format",
        dest="output_format",
        choices=["cpa", "sub2", "sub2api", "codex", "raw"],
        default="cpa",
        help="Output format. Default cpa. sub2api is also accepted. codex/raw is the generic getrt JSON.",
    )
    parser.add_argument("--plan-type", default="plus", help="Plan metadata for CPA/Sub2 output.")
    parser.add_argument("--name-prefix", default="[testplus]", help="Sub2 account name prefix.")
    parser.add_argument("--account-name", help="Sub2 explicit account display name.")
    parser.add_argument("--concurrency", type=int, default=10, help="Sub2 account concurrency.")
    parser.add_argument("--priority", type=int, default=1, help="Sub2 account priority.")
    parser.add_argument("--proxy-url", help="Proxy URL stored in CPA output metadata.")
    parser.add_argument("--proxy-strict", action="store_true", help="Store proxy_strict=true when --proxy-url is set for CPA.")
    parser.add_argument("--prefix", help="Optional CPA routing prefix.")
    parser.add_argument("--prompt", default="", help="OAuth prompt parameter. Empty means omit; use 'login' to force login.")
    parser.add_argument("--phone-line", default=os.environ.get("GETRT_PHONE_LINE", ""), help="PHONE|API_URL or PHONE----API_URL for add-phone OTP.")
    parser.add_argument("--phone-number", default=os.environ.get("GETRT_PHONE_NUMBER", os.environ.get("OPENAI_PHONE_NUMBER", "")), help="Phone number for OAuth add-phone.")
    parser.add_argument("--phone-otp-api", default=os.environ.get("GETRT_PHONE_OTP_API", ""), help="Polling API that returns the add-phone SMS body.")
    parser.add_argument("--phone-otp-cmd", default=os.environ.get("GETRT_PHONE_OTP_CMD", os.environ.get("OPENAI_PHONE_OTP_CMD", "")), help="Command whose stdout contains the add-phone OTP.")
    parser.add_argument("--phone-otp", default=os.environ.get("GETRT_PHONE_OTP", os.environ.get("OPENAI_PHONE_OTP", "")), help="Static add-phone OTP for one-shot debugging.")
    parser.add_argument("--phone-otp-timeout", type=float, default=float(os.environ.get("GETRT_PHONE_OTP_TIMEOUT", "90")))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--code-timeout", type=float, default=90.0)
    parser.add_argument("--backend", choices=["curl_cffi", "httpx"], default="curl_cffi")
    parser.add_argument("--trace-dir", type=Path, default=Path("getrt/runtime/traces"))
    parser.add_argument("--trace-sensitive", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("getrt/runtime/codex_oauth.json"))
    parser.add_argument("--probe-only", action="store_true", help="Stop after authorize; do not exchange the callback code.")
    parser.add_argument("--prelogin-chatgpt", action="store_true", help="Optional probe mode: first create a ChatGPT web session, then run Codex OAuth.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    add_protocol_project_to_path(args.protocol_project)
    modules = import_protocol_modules()
    proxy = None if args.no_proxy else args.proxy
    email_item = modules["normalize_email_item"](modules["EmailItem"](args.email), email_type=args.email_type)
    trace_dir = args.trace_dir / email_item.email.replace("@", "-at-").replace("/", "_") if args.trace_dir else None

    config = modules["ProtocolConfig"](
        timeout=args.timeout,
        code_receiver_base_url=args.email_code_base_url or modules["DEFAULT_EMAIL_CODE_BASE_URL"],
        trace_dir=trace_dir,
        profile=modules["BrowserProfile"](),
    )
    with modules["ProtocolHttpClient"](
        timeout=args.timeout,
        proxy=proxy,
        trace_dir=trace_dir,
        trace_name="getrt",
        trace_sensitive=args.trace_sensitive,
        backend=args.backend,
    ) as http, modules["EmailCodeClient"](
        base_url=args.email_code_base_url,
        provider=modules["effective_email_code_provider"](
            type(
                "Options",
                (),
                {
                    "email_code_provider": args.email_code_provider,
                    "email_type": args.email_type,
                },
            )(),
            email_item.email,
        ),
        timeout=20.0,
    ) as code_client:
        chatgpt = modules["ChatGPTProtocolClient"](config, http)
        if args.prelogin_chatgpt:
            print(f"[prelogin] email={email_item.email}")
            flow = modules["ProtocolRegistrarFlow"](chatgpt)
            login = flow.login_existing_account(email_item.email, code_provider=code_client, timeout=args.code_timeout)
            if not login.session.access_token:
                raise RuntimeError("prelogin completed but /api/auth/session did not contain accessToken")
            print(f"[prelogin] access_token={safe_token_preview(login.session.access_token)}")
        else:
            print(f"[oauth] email={email_item.email}")

        oauth = CodexOAuthProtocol(
            http=http,
            profile=config.profile,
            issuer=args.issuer,
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
            scope=args.scope,
            trace_dir=trace_dir,
            sentinel_provider=modules["SentinelHttpTokenProvider"](config=config, proxy=proxy),
            phone_otp=build_phone_otp_config(args),
        )
        print("[oauth] requesting authorize code")
        code, probe = oauth.request_authorize_code(
            prompt=normalize_prompt(args.prompt),
            email=email_item.email,
            chatgpt=chatgpt,
            code_client=code_client,
            code_timeout=args.code_timeout,
        )
        if not code:
            if probe.get("reason") == "add_phone_required":
                session_access_token = None
                try:
                    session = chatgpt.get_session()
                    session_access_token = session.access_token
                except Exception:
                    session_access_token = None
                payload = build_no_refresh_output(
                    output_format=args.output_format,
                    email=email_item.email,
                    client_id=args.client_id,
                    plan_type=args.plan_type,
                    reason="add_phone_required",
                    probe=probe,
                    session_access_token=session_access_token,
                    proxy_url=args.proxy_url,
                    proxy_strict=args.proxy_strict,
                    prefix=args.prefix,
                    name_prefix=args.name_prefix,
                    account_name=args.account_name,
                    concurrency=args.concurrency,
                    priority=args.priority,
                )
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("[oauth] add-phone required; wrote no-refresh-token output")
                print(f"[oauth] wrote {args.output_format} output: {args.out}")
                return 0
            if probe.get("reason") == "fresh_email_code_timeout":
                snapshot = email_code_snapshot(code_client, email_item.email)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "ok": False,
                    "stage": "email_verification",
                    "email": email_item.email,
                    "reason": "fresh_email_code_timeout",
                    "emailCodeSnapshot": snapshot,
                    "probe": probe,
                    "written_at": utc_now(),
                }
                args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("[oauth] fresh email code timeout; wrote probe result")
                print(f"[oauth] wrote {args.output_format} output: {args.out}")
                return 2
            args.out.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ok": False,
                "stage": "oauth_authorize",
                "email": email_item.email,
                "reason": "authorize did not return callback code",
                "probe": probe,
                "written_at": utc_now(),
            }
            args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[oauth] no callback code; wrote probe result: {args.out}")
            return 2
        print("[oauth] authorize code received")
        if args.probe_only:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            payload = {"ok": True, "stage": "oauth_authorize", "email": email_item.email, "hasCode": True, "probe": probe, "written_at": utc_now()}
            args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[oauth] probe successful; wrote: {args.out}")
            return 0

        tokens = oauth.exchange_code(code, probe["codeVerifier"])
        raw_result = build_codex_json_output(tokens, email=email_item.email, client_id=args.client_id, plan_type=args.plan_type)
        if not raw_result.get("refresh_token"):
            raise RuntimeError("OAuth token response did not include refresh_token")
        result = build_formatted_output(
            tokens,
            output_format=args.output_format,
            email=email_item.email,
            client_id=args.client_id,
            plan_type=args.plan_type,
            proxy_url=args.proxy_url,
            proxy_strict=args.proxy_strict,
            prefix=args.prefix,
            name_prefix=args.name_prefix,
            account_name=args.account_name,
            concurrency=args.concurrency,
            priority=args.priority,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[oauth] refresh_token received")
        print(f"[oauth] wrote {args.output_format} output: {args.out}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
