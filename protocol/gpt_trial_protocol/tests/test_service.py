from __future__ import annotations

from gpt_trial_protocol.email_code import CUSTOM_EMAIL_DOMAIN, EmailCodeClient, EmailCodeProvider, EmailCodeResult
from gpt_trial_protocol.models import CheckoutInput
from gpt_trial_protocol.service import EmailItem, RunOptions, effective_email_code_provider, generate_email_prefix, normalize_email_item, parse_email_items, random_display_name


def test_parse_email_items_accepts_lines_semicolons_and_password_notes() -> None:
    items = parse_email_items("a@icloud.com|pass\nb@icloud.com; c@icloud.com ")

    assert [item.email for item in items] == ["a@icloud.com", "b@icloud.com", "c@icloud.com"]
    assert items[0].note_password == "pass"
    assert items[1].note_password is None


def test_email_type_normalizes_bare_names() -> None:
    assert normalize_email_item(EmailItem("local"), email_type="custom").email == f"local@{CUSTOM_EMAIL_DOMAIN}"
    assert normalize_email_item(EmailItem("name"), email_type="icloud").email == "name@icloud.com"


def test_generated_email_prefix_uses_lu_by_default() -> None:
    value = generate_email_prefix()

    assert value.startswith("lu")
    assert len(value) == 12


def test_email_type_rejects_bare_auto() -> None:
    try:
        normalize_email_item(EmailItem("cuda"), email_type="auto")
    except ValueError as exc:
        assert "--email-type" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_email_type_selects_default_code_provider() -> None:
    assert effective_email_code_provider(RunOptions(email_type="custom"), f"local@{CUSTOM_EMAIL_DOMAIN}") == "openai_code_json"
    assert effective_email_code_provider(RunOptions(email_type="icloud"), "name@icloud.com") == "extract_json"


def test_checkout_default_region_is_usd() -> None:
    checkout = CheckoutInput()

    assert checkout.country == "US"
    assert checkout.currency == "USD"


def test_random_display_name_prefix_and_length() -> None:
    name = random_display_name("Lu")

    assert name.startswith("Lu")
    assert 7 <= len(name) <= 10


def test_openai_code_payload_maps_to_email_code_result() -> None:
    result = EmailCodeResult.from_openai_code_json_payload(
        {
            "recipient": f"local@{CUSTOM_EMAIL_DOMAIN}",
            "code": "271449",
            "receivedAt": "2026-05-19T06:09:10.000Z",
            "messageId": "19e3eda78dee0532",
            "source": "gmail",
        }
    )

    assert result.email == f"local@{CUSTOM_EMAIL_DOMAIN}"
    assert result.latest_code == "271449"
    assert result.latest_time is not None


def test_email_code_provider_auto_selects_custom_domain() -> None:
    client = EmailCodeClient(provider="auto")
    try:
        assert client._provider_for_email(f"local@{CUSTOM_EMAIL_DOMAIN}") is EmailCodeProvider.OPENAI_CODE_JSON
        assert client._provider_for_email("user@icloud.com") is EmailCodeProvider.EXTRACT_JSON
    finally:
        client.close()
