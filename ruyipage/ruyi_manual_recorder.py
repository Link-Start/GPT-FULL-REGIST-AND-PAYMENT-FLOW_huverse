#!/usr/bin/env python3
"""Manual ruyiPage recorder for Stripe -> PayPal flow inspection."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ruyi_paypal_flow import (
    DEFAULT_FIREFOX_PATH,
    RuyiPayPalFlow,
    load_local_env,
    log,
)


ROOT = Path(__file__).resolve().parent


def redact_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(key=)[^&\\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(password|passwd|pwd|cvv|cvc|securityCode)([\"'=:\\s]+)[^&\\s\"']+", r"\1\2<redacted>", text)
    text = re.sub(r"(?<!\\d)\\d{13,19}(?!\\d)", "<card>", text)
    return text[:limit]


def write_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def build_flow_args(args: argparse.Namespace, profile_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        proxy=args.proxy,
        proxy_chain_upstream=args.proxy_chain_upstream,
        proxy_chain_via=args.proxy_chain_via,
        captcha_proxy="",
        user_dir=str(profile_dir),
        browser_path=args.browser_path,
        port=args.port,
        keep_browser_open=True,
        headless=False,
        action_visual=args.action_visual,
        disable_marionette=False,
        no_smart_fingerprint=args.no_smart_fingerprint,
        fingerprint_country=args.fingerprint_country,
        fingerprint_geo_timeout=args.fingerprint_geo_timeout,
        fingerprint_geo_retries=args.fingerprint_geo_retries,
        no_fingerprint_ipv6=args.no_fingerprint_ipv6,
        timezone=args.timezone,
        human_algorithm=args.human_algorithm,
        human_profile=args.human_profile,
    )


def dom_probe_js() -> str:
    return r"""
(() => {
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  }
  function label(el) {
    return [el.id || '', el.name || '', el.type || '', el.autocomplete || '', el.placeholder || '',
      el.getAttribute('aria-label') || '', el.getAttribute('data-testid') || '', el.maxLength > 0 ? `max=${el.maxLength}` : '']
      .join(' ').replace(/\s+/g, ' ').trim();
  }
  const text = document.body && document.body.innerText || '';
  const inputs = [...document.querySelectorAll('input, textarea, select')]
    .filter(visible)
    .slice(0, 80)
    .map((el) => ({tag: el.tagName, label: label(el), valueLen: String(el.value || '').length}));
  const buttons = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')]
    .filter(visible)
    .slice(0, 80)
    .map((el) => [el.innerText || el.textContent || '', el.value || '', el.getAttribute('aria-label') || '', el.id || '', el.name || '']
      .join(' ').replace(/\s+/g, ' ').trim().slice(0, 160));
  const frames = [...document.querySelectorAll('iframe')]
    .filter(visible)
    .map((el) => ({src: el.src || '', title: el.getAttribute('title') || ''}))
    .slice(0, 20);
  return {
    url: location.href,
    title: document.title,
    text: text.slice(0, 2400),
    markers: {
      otpText: /Enter your code|verification code|one[- ]time code|we sent|texted|6[- ]digit|confirm.*phone|security code/i.test(text),
      captchaText: /Security Challenge|not a robot|captcha|verify you are human|confirm you.?re human/i.test(text),
      signupCard: Boolean(document.querySelector('#cardNumber,input[name="cardNumber"]')),
      finalButton: buttons.some((x) => /Agree and Continue|Agree\s*&\s*Continue/i.test(x)),
    },
    inputs,
    buttons,
    frames,
  };
})()
"""


def recorder_events_js() -> str:
    return r"""
(() => {
  function describe(el) {
    if (!el) return {};
    return {
      tag: el.tagName,
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      autocomplete: el.autocomplete || '',
      placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '',
      testid: el.getAttribute('data-testid') || '',
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
      valueLen: String(el.value || '').length,
      maxLength: el.maxLength || 0,
    };
  }
  if (!window.__ruyiManualRecorder) {
    window.__ruyiManualRecorder = {events: []};
    for (const type of ['click', 'input', 'change', 'submit']) {
      document.addEventListener(type, (ev) => {
        window.__ruyiManualRecorder.events.push({
          ts: Date.now(),
          type,
          url: location.href,
          target: describe(ev.target),
        });
        if (window.__ruyiManualRecorder.events.length > 300) {
          window.__ruyiManualRecorder.events.splice(0, 100);
        }
      }, true);
    }
  }
  const out = window.__ruyiManualRecorder.events.splice(0);
  return out;
})()
"""


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-url", default=os.environ.get("START_URL", "about:blank"))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--proxy", default=os.environ.get("RUYI_PROXY") or "system")
    parser.add_argument("--proxy-chain-upstream", default=os.environ.get("RUYI_PROXY_CHAIN_UPSTREAM", ""))
    parser.add_argument("--proxy-chain-via", default=os.environ.get("RUYI_PROXY_CHAIN_VIA", "system"))
    parser.add_argument("--browser-path", default=os.environ.get("RUYI_FIREFOX_PATH", DEFAULT_FIREFOX_PATH))
    parser.add_argument("--port", default=os.environ.get("RUYI_REMOTE_PORT", ""))
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--fingerprint-country", default=os.environ.get("RUYI_FP_COUNTRY", "US"))
    parser.add_argument("--fingerprint-geo-timeout", default=os.environ.get("RUYI_FP_GEO_TIMEOUT", "8"))
    parser.add_argument("--fingerprint-geo-retries", default=os.environ.get("RUYI_FP_GEO_RETRIES", "1"))
    parser.add_argument("--no-fingerprint-ipv6", action="store_true")
    parser.add_argument("--no-smart-fingerprint", action="store_true")
    parser.add_argument("--action-visual", action="store_true")
    parser.add_argument("--human-algorithm", choices=["bezier", "windmouse"], default=os.environ.get("RUYI_HUMAN_ALGORITHM", "windmouse"))
    parser.add_argument("--human-profile", choices=["off", "fast", "conservative"], default=os.environ.get("RUYI_HUMAN_PROFILE", "conservative"))
    parser.add_argument("--screenshot-interval", type=float, default=8.0)
    parser.add_argument("--dom-interval", type=float, default=1.0)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "recordings" / f"manual_ruyi_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = ROOT / "profiles" / f"manual_ruyi_{stamp}"

    flow_args = build_flow_args(args, profile_dir)
    flow = RuyiPayPalFlow(flow_args, {"personalInfo": {}, "contact": {}, "address": {}, "additional": {}})
    stop = {"value": False}

    def on_signal(_signum, _frame):
        stop["value"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    flow.start()
    page = flow.page
    assert page is not None

    net_path = out_dir / "network.jsonl"
    dom_path = out_dir / "dom_states.jsonl"
    action_path = out_dir / "actions.jsonl"
    meta_path = out_dir / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "startedAt": time.time(),
                "outDir": str(out_dir),
                "profileDir": str(profile_dir),
                "startUrl": args.start_url,
                "proxyNote": flow.proxy_note,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    def net_handler(req):
        try:
            rec = {
                "ts": time.time(),
                "phase": "response" if req.is_response_phase else "request",
                "method": req.method,
                "url": redact_text(req.url, 3000),
                "requestId": req.request_id,
            }
            if req.is_response_phase:
                rec["status"] = req.response_status
                rec["headers"] = {k.lower(): redact_text(v, 300) for k, v in (req.response_headers or {}).items() if k.lower() in {"content-type", "location", "x-csrf-token", "paypal-debug-id"}}
                write_jsonl(net_path, rec)
                req.continue_response()
            else:
                if req.method and req.method.upper() != "GET":
                    try:
                        rec["body"] = redact_text(req.body, 3000)
                    except Exception as exc:
                        rec["bodyError"] = type(exc).__name__
                rec["headers"] = {k.lower(): redact_text(v, 300) for k, v in (req.headers or {}).items() if k.lower() in {"content-type", "referer", "origin", "accept-language"}}
                write_jsonl(net_path, rec)
                req.continue_request()
        except Exception as exc:
            try:
                write_jsonl(net_path, {"ts": time.time(), "handlerError": repr(exc)})
                if req.is_response_phase:
                    req.continue_response()
                else:
                    req.continue_request()
            except Exception:
                pass

    try:
        page.intercept.start(net_handler, phases=["beforeRequestSent", "responseStarted"])
        log(f"[record] output: {out_dir}")
        log(f"[record] profile: {profile_dir}")
        if args.start_url and args.start_url != "about:blank":
            flow.navigate(args.start_url)
        else:
            flow.navigate("about:blank")
        log("[record] recorder ready; manual operation can start")

        last_url = ""
        last_shot = 0.0
        next_dom = 0.0
        while not stop["value"]:
            flow.recover_page_context()
            now = time.time()
            try:
                url = flow.current_url()
                if url and url != last_url:
                    log(f"[record] url: {url}")
                    last_url = url
                events = flow.js(recorder_events_js(), timeout=2) or []
                for event in events:
                    event["tsLocal"] = time.time()
                    write_jsonl(action_path, event)
                if now >= next_dom:
                    next_dom = now + args.dom_interval
                    state = flow.js(dom_probe_js(), timeout=3) or {}
                    state["ts"] = time.time()
                    write_jsonl(dom_path, state)
                if now - last_shot >= args.screenshot_interval:
                    last_shot = now
                    shot = out_dir / f"screenshot_{int(now)}.png"
                    try:
                        flow.page.screenshot(str(shot), full_page=True)
                    except Exception:
                        pass
            except Exception as exc:
                write_jsonl(dom_path, {"ts": time.time(), "error": repr(exc)})
                time.sleep(1)
            time.sleep(0.5)
    finally:
        try:
            page.intercept.stop()
        except Exception:
            pass
        try:
            state = flow.page_info()
            (out_dir / "final_page.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            flow.page.screenshot(str(out_dir / "final_page.png"), full_page=True)
        except Exception:
            pass
        flow.close()
        log(f"[record] stopped: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
