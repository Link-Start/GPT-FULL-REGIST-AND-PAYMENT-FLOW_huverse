from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .service import EmailItem, RunOptions, append_jsonl, generate_email_prefix, normalize_email_item, parse_email_items, run_one


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpt-trial", description="Protocol-only GPT account registrar and trial checkout link generator.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="register/login account(s) and generate checkout link")
    run.add_argument("--email", action="append", default=[], help="single email; can be used multiple times")
    run.add_argument("--emails", default="", help="newline/semicolon separated email list")
    run.add_argument("--emails-file", type=Path, help="file containing one email per line")
    run.add_argument("--generate-email", action="store_true", help="generate one mailbox local-part; intended for --email-type custom/icloud")
    run.add_argument("--generated-email-prefix", default="lu", help="prefix for --generate-email, default lu")
    run.add_argument("--login-existing", action="store_true", help="login existing accounts instead of creating account profile")
    run.add_argument("--proxy", help="HTTP/SOCKS proxy for ChatGPT/Auth/Sentinel")
    run.add_argument("--email-type", choices=["auto", "icloud", "custom"], default="auto", help="mailbox type; custom accepts local-part input and appends GPT_TRIAL_CUSTOM_EMAIL_DOMAIN")
    run.add_argument("--email-code-provider", choices=["auto", "extract_json", "openai_code_json"], default="auto")
    run.add_argument("--email-code-base-url", default=None)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--code-timeout", type=float, default=90.0)
    run.add_argument("--checkout-country", default="US")
    run.add_argument("--checkout-currency", default="USD")
    run.add_argument("--trace-dir", type=Path, default=Path("runtime/traces"))
    run.add_argument("--no-trace", action="store_true")
    run.add_argument("--trace-sensitive", action="store_true")
    run.add_argument("--backend", choices=["curl_cffi", "httpx"], default="curl_cffi")
    run.add_argument("--birthdate", default="2000-01-01")
    run.add_argument("--display-name-prefix", default="Lu")
    run.add_argument("--session-output-dir", type=Path, default=None, help="Optional directory to write raw ChatGPT web session JSON per email.")
    run.add_argument("--out", type=Path, default=Path("runtime/results.jsonl"))
    return parser


def collect_email_items(args: argparse.Namespace) -> list[EmailItem]:
    text_parts: list[str] = []
    text_parts.extend(args.email or [])
    if args.emails:
        text_parts.append(args.emails)
    if args.emails_file:
        text_parts.append(args.emails_file.read_text(encoding="utf-8"))
    if args.generate_email:
        text_parts.append(generate_email_prefix(prefix=args.generated_email_prefix))
    items = parse_email_items("\n".join(text_parts))
    if not items:
        raise SystemExit("no email provided")
    return items


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    items = collect_email_items(args)
    options = RunOptions(
        proxy=args.proxy,
        login_existing=args.login_existing,
        email_type=args.email_type,
        email_code_base_url=args.email_code_base_url,
        email_code_provider=args.email_code_provider,
        timeout=args.timeout,
        code_timeout=args.code_timeout,
        checkout_country=args.checkout_country,
        checkout_currency=args.checkout_currency,
        trace_dir=None if args.no_trace else args.trace_dir,
        trace_sensitive=args.trace_sensitive,
        backend=args.backend,
        birthdate=args.birthdate,
        display_name_prefix=args.display_name_prefix,
        session_output_dir=args.session_output_dir,
    )
    success = 0
    failure = 0
    for index, item in enumerate(items, start=1):
        try:
            item = normalize_email_item(item, email_type=args.email_type)
        except Exception as exc:
            failure += 1
            result = {
                "ok": False,
                "email": item.email,
                "stage": "email_input",
                "reason": str(exc),
                "exceptionType": type(exc).__name__,
            }
            append_jsonl(args.out, [result])
            emit(result | {"event": "failure", "index": index, "total": len(items)})
            continue
        emit({"event": "start", "index": index, "total": len(items), "email": item.email, "mode": "login" if args.login_existing else "register"})
        try:
            result = run_one(item, options)
            append_jsonl(args.out, [result])
            if result.get("ok"):
                success += 1
                emit(
                    {
                        "event": "success",
                        "email": item.email,
                        "stage": result.get("stage"),
                        "checkoutUrl": result.get("checkoutUrl"),
                        "sessionOutputPath": result.get("sessionOutputPath") or "",
                    }
                )
            else:
                failure += 1
                emit({"event": "failure", "email": item.email, "stage": result.get("stage"), "reason": result.get("reason")})
        except Exception as exc:
            failure += 1
            result = {
                "ok": False,
                "email": item.email,
                "stage": "unexpected",
                "reason": str(exc),
                "exceptionType": type(exc).__name__,
            }
            append_jsonl(args.out, [result])
            emit(result | {"event": "failure"})
            traceback.print_exc(file=sys.stderr)
    emit({"event": "done", "success": success, "failure": failure, "out": str(args.out)})
    return 0 if failure == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
