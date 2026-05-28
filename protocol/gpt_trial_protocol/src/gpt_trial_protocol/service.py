from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .chatgpt import ChatGPTProtocolClient
from .email_code import AGIUNX_BASE_URL, EmailCodeClient, EmailCodeProvider, FRIMAIL_DOMAIN
from .flows import ProtocolRegistrarFlow
from .http_client import ProtocolHttpClient
from .models import AccountInput, BrowserProfile, CheckoutInput, ProtocolConfig
from .sentinel_http import SentinelHttpTokenProvider


@dataclass(frozen=True)
class EmailItem:
    email: str
    note_password: str | None = None


@dataclass(frozen=True)
class RunOptions:
    proxy: str | None = None
    login_existing: bool = False
    email_type: str = "auto"
    email_code_base_url: str | None = None
    email_code_provider: str = EmailCodeProvider.AUTO.value
    timeout: float = 30.0
    code_timeout: float = 90.0
    checkout_country: str = "US"
    checkout_currency: str = "USD"
    trace_dir: Path | None = None
    trace_sensitive: bool = False
    backend: str = "curl_cffi"
    birthdate: str = "2000-01-01"
    display_name_prefix: str = "Lu"
    session_output_dir: Path | None = None


def parse_email_items(text: str) -> list[EmailItem]:
    normalized = text.replace(";", "\n")
    items: list[EmailItem] = []
    for raw in normalized.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            email, note = line.split("|", 1)
            items.append(EmailItem(email=email.strip(), note_password=note.strip() or None))
        else:
            items.append(EmailItem(email=line))
    return items


def generate_email_prefix(*, prefix: str = "lu") -> str:
    from datetime import datetime

    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{prefix}{datetime.now().strftime('%H%M%S')}{suffix}"


def normalize_email_item(item: EmailItem, *, email_type: str = "auto") -> EmailItem:
    kind = email_type.lower().strip()
    email = item.email.strip()
    if kind == "auto":
        if "@" not in email:
            raise ValueError(f"bare email name requires --email-type icloud or --email-type frimail: {email}")
        return EmailItem(email=email, note_password=item.note_password)
    if kind == "icloud":
        if "@" not in email:
            email = f"{email}@icloud.com"
        return EmailItem(email=email, note_password=item.note_password)
    if kind == "frimail":
        if "@" not in email:
            email = f"{email}@{FRIMAIL_DOMAIN}"
        elif not email.lower().endswith(f"@{FRIMAIL_DOMAIN}"):
            raise ValueError(f"frimail only supports *@{FRIMAIL_DOMAIN}: {email}")
        return EmailItem(email=email, note_password=item.note_password)
    raise ValueError(f"unknown email type: {email_type}")


def effective_email_code_provider(options: RunOptions, email: str) -> str:
    if options.email_code_provider != EmailCodeProvider.AUTO.value:
        return options.email_code_provider
    if options.email_type == "frimail" or email.lower().endswith(f"@{FRIMAIL_DOMAIN}"):
        return EmailCodeProvider.FRIMAIL.value
    if options.email_type == "icloud":
        return EmailCodeProvider.AGIUNX.value
    return EmailCodeProvider.AUTO.value


def random_display_name(prefix: str = "Lu") -> str:
    length = random.randint(5, 8)
    suffix = "".join(random.choice(string.ascii_letters) for _ in range(length))
    return f"{prefix}{suffix}"


def run_one(item: EmailItem, options: RunOptions) -> dict[str, Any]:
    item = normalize_email_item(item, email_type=options.email_type)
    checkout = CheckoutInput(country=options.checkout_country, currency=options.checkout_currency)
    trace_dir = options.trace_dir / sanitize_email(item.email) if options.trace_dir else None
    config = ProtocolConfig(
        timeout=options.timeout,
        code_receiver_base_url=options.email_code_base_url or AGIUNX_BASE_URL,
        trace_dir=trace_dir,
        profile=BrowserProfile(),
    )
    with ProtocolHttpClient(
        timeout=options.timeout,
        proxy=options.proxy,
        trace_dir=trace_dir,
        trace_name="chatgpt",
        trace_sensitive=options.trace_sensitive,
        backend=options.backend,
    ) as http, EmailCodeClient(
        base_url=options.email_code_base_url,
        provider=effective_email_code_provider(options, item.email),
        timeout=20.0,
    ) as code_client:
        chatgpt = ChatGPTProtocolClient(config, http)
        flow = ProtocolRegistrarFlow(chatgpt)
        if options.login_existing:
            login = flow.login_existing_account(item.email, code_provider=code_client, timeout=options.code_timeout)
            session = login.session
            access_token = session.access_token
            registration_info: dict[str, Any] = {"mode": "login", "validation": login.validation_result}
        else:
            account = AccountInput(
                email=item.email,
                display_name=random_display_name(options.display_name_prefix),
                birthdate=options.birthdate,
            )
            sentinel = SentinelHttpTokenProvider(config=config, proxy=options.proxy)
            registration = flow.register_account_with_risk_provider(
                account,
                risk_provider=sentinel,
                code_provider=code_client,
                timeout=options.code_timeout,
            )
            session = registration.session
            access_token = session.access_token
            registration_info = {"mode": "register", "createAccount": registration.create_account_result}

        if not access_token:
            raise RuntimeError("ChatGPT session did not contain accessToken")

        session_output_path = write_session_output(item.email, session, options.session_output_dir)

        link = flow.checkout_link(access_token, checkout)
        return {
            "ok": True,
            "email": item.email,
            "stage": "checkout_link",
            "registration": registration_info,
            "checkoutUrl": link.url,
            "checkoutSessionId": link.checkout_session_id,
            "processorEntity": link.processor_entity,
            "rawCheckout": link.raw,
            "sessionOutputPath": session_output_path,
        }


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def sanitize_email(email: str) -> str:
    return email.replace("@", "-at-").replace("/", "_").replace("\\", "_")


def write_session_output(email: str, session: object, output_dir: Path | None) -> str:
    if not output_dir:
        return ""
    raw = getattr(session, "raw", None)
    if not isinstance(raw, dict):
        return ""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{sanitize_email(email)}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)
