from __future__ import annotations

from full_flow_proxy_pool import ProxyPool, parse_proxy_for_curl, redact_proxy


def test_parse_colon_socks_proxy_for_curl():
    parsed = parse_proxy_for_curl("proxy.example.net:1080:user:pass(socks)")
    assert parsed.proxy == "socks5h://proxy.example.net:1080"
    assert parsed.user == "user:pass"
    assert parsed.scheme == "socks5h"


def test_parse_colon_http_proxy_for_curl():
    parsed = parse_proxy_for_curl("proxy.example.net:1080:user:pass(http)")
    assert parsed.proxy == "http://proxy.example.net:1080"
    assert parsed.user == "user:pass"
    assert parsed.scheme == "http"


def test_parse_curl_proxy_command():
    parsed = parse_proxy_for_curl('curl -x proxy.example.net:1080 -U "user:pass" mayips.com')
    assert parsed.proxy == "http://proxy.example.net:1080"
    assert parsed.user == "user:pass"


def test_redact_colon_proxy():
    assert redact_proxy("host:123:user:pass(http)") == "host:123:user:***"


def test_proxy_pool_add_delete_and_stats(tmp_path):
    pool = ProxyPool(tmp_path / "pool.sqlite3")
    assert pool.add_values(["host:123:user:pass(http)"]) == 1
    assert pool.add_values(["host:123:user:pass(http)"]) == 0
    stats = pool.stats()
    assert stats["proxies_total"] == 1
    item = pool.list_items()[0]
    assert item["redacted"] == "host:123:user:***"
    pool.set_enabled(item["id"], False)
    assert pool.stats()["proxies_enabled"] == 0
    pool.delete_item(item["id"])
    assert pool.stats()["proxies_total"] == 0
