from __future__ import annotations

from pathlib import Path

from trial_payment_full_flow import (
    build_payment_cmd,
    classify_payment_retry,
    effective_proxy_chain_via,
    effective_payment_headless_default,
    payment_profile_paths_from_log,
    should_retry_email_pool,
)


def test_stripe_paypal_selection_failure_is_retryable():
    retry, reason = classify_payment_retry(
        "RuntimeError: Stripe PayPal payment method was not found or could not be selected",
        {},
        1,
    )
    assert retry is True
    assert reason == "stripe_paypal_method_not_found"


def test_stripe_amount_check_failure_is_not_retryable():
    retry, reason = classify_payment_retry(
        "RuntimeError: Stripe amount check failed; expected 0 amount: ['$20.00']",
        {},
        1,
    )
    assert retry is False
    assert reason == "stripe_amount_check_failed"


def test_paypal_hermes_r_error_is_retryable():
    retry, reason = classify_payment_retry(
        "",
        {"status": "failed", "reason": "paypal_hermes_r_error"},
        1,
    )
    assert retry is True
    assert reason == "paypal_hermes_r_error"


def test_datadome_blocked_verdict_is_not_regular_retry():
    retry, reason = classify_payment_retry(
        "",
        {"status": "failed", "reason": "captcha_solve_failed", "error": "DataDome returned t=bv; rotate proxy/IP before retrying"},
        1,
    )
    assert retry is False
    assert reason == "datadome_blocked_verdict"


def test_retryable_payment_failures_return_email_to_retry_pool():
    retry, reason = should_retry_email_pool(
        "payment_failed",
        [
            {
                "retryReason": "stripe_paypal_method_not_found",
                "result": {},
            }
        ],
    )
    assert retry is True
    assert reason == "payment_failed"


def test_card_rejection_returns_email_to_retry_pool_for_new_card():
    retry, reason = should_retry_email_pool(
        "payment_failed",
        [
            {
                "retryReason": "card_or_paypal_backend_rejected_after_sms",
                "result": {"reason": "paypal_signup_not_advanced_after_sms"},
            }
        ],
    )
    assert retry is True
    assert reason == "card_rejected_retry_with_new_card"


def test_server_deployment_skips_local_bridge_proxy():
    assert effective_proxy_chain_via("http://127.0.0.1:7897", root=Path("/opt/openaii")) == ""
    assert effective_proxy_chain_via("http://127.0.0.1:7897", root=Path("/workspace/GPT-FULL-REGIST-AND-PAYMENT-FLOW")) == "http://127.0.0.1:7897"


def test_server_deployment_defaults_payment_headless_on():
    assert effective_payment_headless_default(root=Path("/opt/openaii")) is True
    assert effective_payment_headless_default(root=Path("/workspace/GPT-FULL-REGIST-AND-PAYMENT-FLOW")) is False
    assert effective_payment_headless_default("off", root=Path("/opt/openaii")) is False
    assert effective_payment_headless_default("1", root=Path("/workspace/GPT-FULL-REGIST-AND-PAYMENT-FLOW")) is True


def test_build_payment_cmd_can_use_temp_proxy_override(tmp_path):
    class Args:
        payment_project = str(tmp_path)
        payment_python = "/usr/bin/python3"
        payment_script = ""
        address_json = str(tmp_path / "address.json")
        sms_line = "+10000000000----https://sms.example/api"
        keep_browser_open = False
        payment_headless = True
        no_captcha_handling = False
        payment_proxy = ""
        payment_extra_arg = []
        enable_payment_proxy = False

    cmd, _ = build_payment_cmd(
        Args(),
        "https://pay.openai.com/c/pay/cs_live_test",
        tmp_path / "card.json",
        tmp_path / "result.json",
        payment_proxy="us2.cliproxy.io:3010:user:pass(socks)",
    )

    assert "--proxy" in cmd
    assert cmd[cmd.index("--proxy") + 1] == "us2.cliproxy.io:3010:user:pass(socks)"


def test_build_payment_cmd_can_use_temp_proxy_bridge(tmp_path):
    class Args:
        payment_project = str(tmp_path)
        payment_python = "/usr/bin/python3"
        payment_script = ""
        address_json = str(tmp_path / "address.json")
        sms_line = "+10000000000----https://sms.example/api"
        keep_browser_open = False
        payment_headless = True
        no_captcha_handling = False
        payment_proxy = ""
        payment_extra_arg = []
        enable_payment_proxy = False

    cmd, _ = build_payment_cmd(
        Args(),
        "https://pay.openai.com/c/pay/cs_live_test",
        tmp_path / "card.json",
        tmp_path / "result.json",
        payment_proxy="us2.cliproxy.io:3010:user:pass(socks)",
        payment_proxy_chain=True,
    )

    assert "--proxy" not in cmd
    assert cmd[cmd.index("--proxy-chain-upstream") + 1] == "us2.cliproxy.io:3010:user:pass(socks)"
    assert cmd[cmd.index("--proxy-chain-via") + 1] == "direct"


def test_build_payment_cmd_can_use_primary_proxy_bridge(tmp_path):
    class Args:
        payment_project = str(tmp_path)
        payment_python = "/usr/bin/python3"
        payment_script = ""
        address_json = str(tmp_path / "address.json")
        sms_line = "+10000000000----https://sms.example/api"
        keep_browser_open = False
        payment_headless = True
        no_captcha_handling = False
        payment_proxy = ""
        payment_extra_arg = []
        enable_payment_proxy = False

    cmd, _ = build_payment_cmd(
        Args(),
        "https://pay.openai.com/c/pay/cs_live_test",
        tmp_path / "card.json",
        tmp_path / "result.json",
        payment_proxy="us2.cliproxy.io:3010:user:pass(http)",
        payment_proxy_chain=True,
    )

    assert "--proxy" not in cmd
    assert cmd[cmd.index("--proxy-chain-upstream") + 1] == "us2.cliproxy.io:3010:user:pass(http)"
    assert cmd[cmd.index("--proxy-chain-via") + 1] == "direct"


def test_payment_profile_paths_from_log(tmp_path):
    log_path = tmp_path / "payment.log"
    log_path.write_text(
        "\n".join(
            [
                "[ruyi] Firefox backend started port=1 profile=/opt/openaii/ruyipage/profiles/ruyi_1_111",
                "[fp] userdir /opt/openaii/ruyipage/profiles/ruyi_2_222",
                "[fp] userdir /tmp/not-matching",
            ]
        )
    )

    assert payment_profile_paths_from_log(log_path) == [
        "/opt/openaii/ruyipage/profiles/ruyi_1_111",
        "/opt/openaii/ruyipage/profiles/ruyi_2_222",
    ]
