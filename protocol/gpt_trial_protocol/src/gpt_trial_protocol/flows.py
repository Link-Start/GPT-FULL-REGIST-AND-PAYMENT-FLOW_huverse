from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

from .chatgpt import ChatGPTProtocolClient
from .email_code import EmailCodeResult
from .errors import ProtocolResponseError
from .models import AccountInput, CheckoutInput, CheckoutLink, SessionInfo
from .risk_tokens import RiskTokenProvider


class FreshCodeProvider(Protocol):
    def wait_for_fresh_code(self, email: str, *, not_before: datetime | None, timeout: float = ..., **kwargs: object) -> EmailCodeResult:
        ...


@dataclass(frozen=True)
class RegistrationResult:
    email: str
    session: SessionInfo
    create_account_result: dict


@dataclass(frozen=True)
class LoginResult:
    email: str
    session: SessionInfo
    validation_result: dict


def validation_continue_is_session_callback(validation: dict) -> bool:
    url = (
        validation.get("continue_url")
        or validation.get("continueUrl")
        or validation.get("redirect_url")
        or validation.get("redirectUrl")
        or validation.get("url")
        or ""
    )
    return "/api/auth/callback/" in str(url)


def protocol_error_is_invalid_auth_step(exc: ProtocolResponseError) -> bool:
    body = (exc.body or "").lower()
    return exc.status_code == 400 and ("invalid_auth_step" in body or "invalid authorization step" in body)


def _response_url(response: object) -> str:
    return str(getattr(response, "url", "") or "")


def auth_response_requires_passwordless_otp(response: object) -> bool:
    path = urlparse(_response_url(response)).path.rstrip("/")
    return path in {"/create-account/password", "/log-in/password", "/login/password"}


class ProtocolRegistrarFlow:
    def __init__(self, chatgpt: ChatGPTProtocolClient) -> None:
        self.chatgpt = chatgpt

    def _open_auth_and_trigger_passwordless_if_needed(self, auth: object) -> object:
        response = self.chatgpt.open_auth_url(auth)  # type: ignore[arg-type]
        if auth_response_requires_passwordless_otp(response):
            sent = self.chatgpt.send_passwordless_otp(referer=_response_url(response))
            self.chatgpt.open_continue_url(sent, fallback=self.chatgpt.auth_url("/email-verification"))
        return response

    def register_account_with_risk_provider(
        self,
        account: AccountInput,
        *,
        risk_provider: RiskTokenProvider,
        code_provider: FreshCodeProvider,
        timeout: float = 90.0,
    ) -> RegistrationResult:
        not_before = datetime.now(timezone.utc)
        auth = self.chatgpt.start_openai_signin(account.email, mode="register")
        self._open_auth_and_trigger_passwordless_if_needed(auth)
        code = code_provider.wait_for_fresh_code(account.email, not_before=not_before, timeout=timeout).latest_code
        if not code:
            raise TimeoutError(f"fresh code not found for {account.email}")
        validation = self.chatgpt.validate_email_otp(code)
        self.chatgpt.open_continue_url(validation, fallback=self.chatgpt.auth_url("/about-you"))
        if validation_continue_is_session_callback(validation):
            session = self.chatgpt.get_session()
            if session.access_token:
                return RegistrationResult(
                    email=account.email,
                    session=session,
                    create_account_result={"skipped": True, "reason": "otp_continue_created_session"},
                )
        bundle = risk_provider.get_openai_sentinel(purpose="register")
        try:
            created = self.chatgpt.create_account(account, sentinel=bundle.sentinel)
        except ProtocolResponseError as exc:
            if not protocol_error_is_invalid_auth_step(exc):
                raise
            session = self.chatgpt.get_session()
            if not session.access_token:
                raise
            return RegistrationResult(
                email=account.email,
                session=session,
                create_account_result={"skipped": True, "reason": "invalid_auth_step_after_otp_continue"},
            )
        self.chatgpt.open_continue_url(created)
        session = self.chatgpt.get_session()
        return RegistrationResult(email=account.email, session=session, create_account_result=created)

    def login_existing_account(
        self,
        email: str,
        *,
        code_provider: FreshCodeProvider,
        timeout: float = 90.0,
    ) -> LoginResult:
        not_before = datetime.now(timezone.utc)
        auth = self.chatgpt.start_openai_signin(email, mode="register")
        self._open_auth_and_trigger_passwordless_if_needed(auth)
        code = code_provider.wait_for_fresh_code(email, not_before=not_before, timeout=timeout).latest_code
        if not code:
            raise TimeoutError(f"fresh code not found for {email}")
        validation = self.chatgpt.validate_email_otp(code)
        self.chatgpt.open_continue_url(validation, fallback=self.chatgpt.chatgpt_url("/"))
        session = self.chatgpt.get_session()
        return LoginResult(email=email, session=session, validation_result=validation)

    def checkout_link(self, access_token: str, checkout: CheckoutInput = CheckoutInput()) -> CheckoutLink:
        return self.chatgpt.generate_checkout_link(access_token, checkout)
