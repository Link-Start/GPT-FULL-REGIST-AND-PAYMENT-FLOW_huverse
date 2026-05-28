#!/usr/bin/env python3
"""Run queue concurrency gates until resources are exhausted or target is met."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advance full-flow concurrency gates with a strict success/time rule.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--start", type=int, default=3)
    parser.add_argument("--end", type=int, default=8)
    parser.add_argument("--threshold-seconds", type=float, default=150.0)
    parser.add_argument("--required-streak", type=int, default=2)
    parser.add_argument("--max-attempts-per-level", type=int, default=6)
    parser.add_argument("--max-runs-factor", type=int, default=2)
    parser.add_argument("--payment-proxy-id", type=int, default=2)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--email-type", default="icloud")
    parser.add_argument("--email-code-provider", default="agiunx")
    return parser


def http_json(base_url: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    method = "GET"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            payload = {"ok": False, "error": raw.decode("utf-8", errors="replace")}
        if isinstance(payload, dict):
            return payload
        return {"ok": False, "error": str(payload)}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected JSON response: {payload!r}")
    return payload


def available_resources(overview: dict[str, Any]) -> tuple[int, int, int]:
    pool = overview.get("pool") if isinstance(overview.get("pool"), dict) else {}
    emails = int(pool.get("emails_main") or 0) + int(pool.get("emails_retry") or 0)
    cards = int(pool.get("cards_available") or 0)
    phones = int(pool.get("phones_available") or 0)
    return emails, cards, phones


def wait_parent_summary(base_url: str, run_id: str, poll_seconds: float) -> dict[str, Any]:
    while True:
        overview = http_json(base_url, "/api/overview")
        if int(overview.get("activeJobs") or 0) == 0:
            log = http_json(base_url, f"/api/log?runId={run_id}&file=summary.json")
            text = str(log.get("text") or "")
            if text.strip():
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"status": "summary_parse_failed", "raw": text}
        time.sleep(max(1.0, poll_seconds))


def queue_children(base_url: str, run_id: str) -> list[dict[str, Any]]:
    payload = http_json(base_url, f"/api/queue-children?runId={run_id}")
    children = payload.get("children") if isinstance(payload.get("children"), list) else []
    return [item for item in children if isinstance(item, dict)]


def run_gate(args: argparse.Namespace, n: int) -> dict[str, Any]:
    body = {
        "workers": n,
        "maxRuns": n * max(1, int(args.max_runs_factor)),
        "successTarget": n,
        "paymentBrowserSlots": n,
        "paymentMode": "force_proxy",
        "paymentProxyId": int(args.payment_proxy_id),
        "sessionJson": False,
        "getrt": False,
        "getrtAddPhone": False,
        "emailType": args.email_type,
        "emailCodeProvider": args.email_code_provider,
    }
    started = http_json(args.base_url, "/api/queue/start", body)
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error") or started, "started": started}
    run_id = str(started.get("runId") or "")
    print(f"[gate] n={n} runId={run_id}", flush=True)
    summary = wait_parent_summary(args.base_url, run_id, float(args.poll_seconds))
    children = queue_children(args.base_url, run_id)
    success_count = sum(1 for child in children if str(child.get("status") or "").startswith("success"))
    seconds = float(summary.get("seconds") or 0.0)
    passed = success_count >= n and seconds < float(args.threshold_seconds)
    return {
        "ok": True,
        "runId": run_id,
        "seconds": seconds,
        "success": success_count,
        "target": n,
        "passed": passed,
        "summaryStatus": summary.get("status"),
        "children": children,
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.start < 1 or args.end < args.start:
        raise SystemExit("--start/--end invalid")
    for n in range(int(args.start), int(args.end) + 1):
        streak = 0
        attempts = 0
        while streak < int(args.required_streak):
            overview = http_json(args.base_url, "/api/overview")
            emails, cards, phones = available_resources(overview)
            if emails < n or cards < n or phones < n:
                print(
                    f"[gate] stop n={n}: resources insufficient "
                    f"emails={emails}/{n} cards={cards}/{n} phones={phones}/{n}",
                    flush=True,
                )
                return 2
            attempts += 1
            if attempts > int(args.max_attempts_per_level):
                print(f"[gate] stop n={n}: max attempts reached streak={streak}/{args.required_streak}", flush=True)
                return 3
            result = run_gate(args, n)
            if not result.get("ok"):
                print(f"[gate] start failed n={n}: {result.get('error')}", flush=True)
                return 4
            print(
                f"[gate] result n={n} runId={result.get('runId')} "
                f"success={result.get('success')}/{n} seconds={result.get('seconds')} "
                f"passed={int(bool(result.get('passed')))}",
                flush=True,
            )
            if result.get("passed"):
                streak += 1
            else:
                streak = 0
        print(f"[gate] advanced past n={n} after streak={streak}", flush=True)
    print(f"[gate] complete through n={args.end}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
