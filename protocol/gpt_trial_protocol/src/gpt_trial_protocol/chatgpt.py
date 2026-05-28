from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

from .errors import MissingFieldError, UnsupportedProtocolStep
from .http_client import ProtocolHttpClient, json_or_empty, require_ok
from .models import AccountInput, AuthStart, CheckoutInput, CheckoutLink, ProtocolConfig, SessionInfo


@dataclass(frozen=True)
class SentinelHeaders:
    token: str | None = None
    so_token: str | None = None

    def as_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["openai-sentinel-token"] = self.token
        if self.so_token:
            headers["openai-sentinel-so-token"] = self.so_token
        return headers


class ChatGPTProtocolClient:
    def __init__(self, config: ProtocolConfig, http: ProtocolHttpClient) -> None:
        self.config = config
        self.http = http

    def chatgpt_url(self, path: str) -> str:
        return f"{self.config.chatgpt_base_url.rstrip('/')}/{path.lstrip('/')}"

    def auth_url(self, path: str) -> str:
        return f"{self.config.auth_base_url.rstrip('/')}/{path.lstrip('/')}"

    def get_csrf(self, *, referer: str = "https://chatgpt.com/") -> str:
        response = require_ok(
            self.http.get(
                self.chatgpt_url("/api/auth/csrf"),
                headers=self.config.profile.browser_headers(referer=referer, content_type="application/json"),
            )
        )
        payload = json_or_empty(response)
        token = payload.get("csrfToken")
        if not token:
            raise MissingFieldError("csrfToken", "/api/auth/csrf")
        return str(token)

    def start_openai_signin(
        self,
        email: str,
        *,
        csrf_token: str | None = None,
        mode: str = "register",
        referer: str = "https://chatgpt.com/",
        callback_url: str = "https://chatgpt.com/",
    ) -> AuthStart:
        csrf = csrf_token or self.get_csrf(referer=referer)
        params: dict[str, str] = {
            "login_hint": email,
            "ext-oai-did": self.config.profile.device_id or str(uuid.uuid4()),
        }
        if mode != "register":
            raise ValueError(f"unknown signin mode: {mode}")
        params.update(
            {
                "prompt": "login",
                "auth_session_logging_id": str(uuid.uuid4()),
                "ext-passkey-client-capabilities": "1111",
                "screen_hint": "login_or_signup",
            }
        )
        response = require_ok(
            self.http.post(
                self.chatgpt_url(f"/api/auth/signin/openai?{urlencode(params)}"),
                headers=self.config.profile.browser_headers(
                    referer=referer,
                    content_type="application/x-www-form-urlencoded",
                ),
                data={"callbackUrl": callback_url, "csrfToken": csrf, "json": "true"},
            )
        )
        payload = json_or_empty(response)
        url = payload.get("url") or response.headers.get("location")
        if not url:
            raise MissingFieldError("url", "/api/auth/signin/openai")
        return AuthStart(url=str(url), csrf_token=csrf, raw=payload)

    def open_auth_url(self, auth_start: AuthStart | str) -> Any:
        url = auth_start.url if isinstance(auth_start, AuthStart) else auth_start
        referer = "https://chatgpt.com/"
        response = self.http.get(url, headers=self.config.profile.browser_headers(referer=referer))
        for _ in range(10):
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("location")
            if not location:
                return response
            next_url = urljoin(str(response.url), location)
            referer = str(response.url)
            response = self.http.get(next_url, headers=self.config.profile.browser_headers(referer=referer))
        return response

    def send_passwordless_otp(self, *, referer: str) -> dict[str, Any]:
        response = require_ok(
            self.http.post(
                self.auth_url("/api/accounts/passwordless/send-otp"),
                headers=self.config.profile.xhr_headers(
                    referer=referer,
                    accept="application/json",
                    origin=self.config.auth_base_url.rstrip("/"),
                    fetch_site="same-origin",
                ),
            )
        )
        try:
            return json_or_empty(response)
        except ValueError:
            return {}

    def validate_email_otp(self, code: str) -> dict[str, Any]:
        response = require_ok(
            self.http.post(
                self.auth_url("/api/accounts/email-otp/validate"),
                headers=self.config.profile.browser_headers(
                    referer=self.auth_url("/email-verification"),
                    content_type="application/json",
                )
                | {"accept": "application/json"},
                json={"code": code},
            )
        )
        return json_or_empty(response)

    def open_continue_url(self, payload: dict[str, Any], *, fallback: str | None = None) -> Any | None:
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
            url = self.auth_url(url)
        return self.open_auth_url(url)

    def create_account(self, account: AccountInput, *, sentinel: SentinelHeaders) -> dict[str, Any]:
        if not sentinel.token:
            raise UnsupportedProtocolStep("create_account requires live openai-sentinel-token")
        response = require_ok(
            self.http.post(
                self.auth_url("/api/accounts/create_account"),
                headers=self.config.profile.browser_headers(
                    referer=self.auth_url("/about-you"),
                    content_type="application/json",
                )
                | {"accept": "application/json"}
                | sentinel.as_headers(),
                json={
                    "name": account.display_name or "LuProtocol",
                    "birthdate": account.birthdate or "2000-01-01",
                },
            )
        )
        return json_or_empty(response)

    def get_session(self) -> SessionInfo:
        response = require_ok(
            self.http.get(
                self.chatgpt_url("/api/auth/session"),
                headers=self.config.profile.browser_headers(referer="https://chatgpt.com/"),
            )
        )
        payload = json_or_empty(response)
        return SessionInfo(access_token=payload.get("accessToken"), expires=payload.get("expires"), raw=payload)

    def plus_status(self, access_token: str) -> dict[str, Any]:
        response = require_ok(
            self.http.get(
                self.chatgpt_url("/backend-api/accounts/check/v4-2023-04-27"),
                headers=self.config.profile.api_headers(access_token),
            )
        )
        return json_or_empty(response)

    def generate_checkout_link(self, access_token: str, checkout: CheckoutInput) -> CheckoutLink:
        payload = {
            "plan_name": checkout.plan_name,
            "billing_details": {"country": checkout.country, "currency": checkout.currency},
            "cancel_url": "https://chatgpt.com/#pricing",
            "promo_campaign": {
                "promo_campaign_id": checkout.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
            "checkout_ui_mode": "hosted",
        }
        response = require_ok(
            self.http.post(
                self.chatgpt_url("/backend-api/payments/checkout"),
                headers=self.config.profile.api_headers(access_token) | {"content-type": "application/json"},
                json=payload,
            )
        )
        data = json_or_empty(response)
        url = data.get("url") or data.get("stripe_hosted_url") or data.get("checkout_url")
        if not url:
            raise MissingFieldError("url", "/backend-api/payments/checkout")
        return CheckoutLink(
            url=str(url),
            checkout_session_id=data.get("checkout_session_id"),
            processor_entity=data.get("processor_entity"),
            raw=data,
        )
