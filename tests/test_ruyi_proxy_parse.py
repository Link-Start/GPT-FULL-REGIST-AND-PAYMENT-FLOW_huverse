from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ruyipage"))

from ruyi_paypal_flow import RuyiPayPalFlow, parse_proxy_url, resolve_proxy


def test_vendor_socks_suffix_survives_resolve_proxy():
    raw = "us2.cliproxy.io:3010:user:pass(socks)"
    resolved, note = resolve_proxy(raw)
    proxy = parse_proxy_url(resolved)

    assert note == "explicit proxy"
    assert proxy is not None
    assert proxy["scheme"] == "socks5"
    assert proxy["browserProxy"] == "socks5://us2.cliproxy.io:3010"
    assert proxy["requestScheme"] == "socks5h"


def test_vendor_socks_suffix_overrides_added_http_scheme():
    proxy = parse_proxy_url("http://us2.cliproxy.io:3010:user:pass(socks)")

    assert proxy is not None
    assert proxy["scheme"] == "socks5"


def test_success_urls_are_host_grounded_and_no_text_maybe_success():
    flow = object.__new__(RuyiPayPalFlow)

    assert flow.is_success_redirect_url("https://pay.openai.com/c/pay/cs_live_x?redirect_status=succeeded")
    assert flow.is_success_redirect_url("https://pay.openai.com/c/pay/cs_live_x?returned_from_redirect=true")
    assert flow.is_success_redirect_url("https://chatgpt.com/payments/success")
    assert flow.is_success_redirect_url("https://pm-redirects.stripe.com/return/x?status=success")
    assert not flow.is_success_redirect_url("https://example.com/?redirect_status=succeeded")
    assert flow.classify_payment_url("https://chatgpt.com/") is None
    assert flow.classify_payment_result({"url": "https://example.com/", "text": "thank you complete success"}) is None
