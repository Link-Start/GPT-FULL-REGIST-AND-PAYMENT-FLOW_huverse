from __future__ import annotations

from getrt.codex_oauth_getrt import build_cpa_output, build_sub2_output, extract_phone_otp, parse_phone_line


def test_cpa_output_matches_compact_cpa_shape():
    payload = build_cpa_output(
        {
            "access_token": "access.token.value",
            "refresh_token": "rt_value",
            "account_id": "org_test",
            "email": "user@example.com",
            "expires_in": 3600,
        },
        email="user@example.com",
        client_id="client_test",
        plan_type="plus",
    )

    assert list(payload.keys()) == [
        "access_token",
        "account_id",
        "email",
        "expired",
        "last_refresh",
        "refresh_token",
        "type",
    ]
    assert payload["type"] == "codex"
    assert payload["refresh_token"] == "rt_value"


def test_phone_line_accepts_payment_sms_separator():
    phone, api = parse_phone_line("+18350001111----https://sms.example/api?token=x")

    assert phone == "+18350001111"
    assert api == "https://sms.example/api?token=x"


def test_phone_line_normalizes_us_country_code_without_plus():
    phone, api = parse_phone_line("18350001111----https://sms.example/api?token=x")

    assert phone == "+18350001111"
    assert api == "https://sms.example/api?token=x"


def test_extract_phone_otp_from_json_body():
    assert extract_phone_otp('{"message":"Your OpenAI verification code is 123456."}') == "123456"


def test_sub2_output_keeps_refresh_token_for_orchestrator_detection():
    payload = build_sub2_output(
        {
            "access_token": "access.token.value",
            "refresh_token": "rt_value",
            "account_id": "org_test",
            "email": "user@example.com",
            "expires_in": 3600,
        },
        email="user@example.com",
        client_id="client_test",
        plan_type="plus",
        name_prefix="[testplus]",
        account_name=None,
        concurrency=10,
        priority=1,
    )

    assert payload["accounts"][0]["credentials"]["refresh_token"] == "rt_value"
