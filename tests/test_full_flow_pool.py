from __future__ import annotations

from full_flow_pool import FullFlowPool
from trial_payment_full_flow import (
    append_success_account,
    protocol_summary_indicates_already_paid,
    randomize_proxy_session,
    should_consume_card_pool,
)


def test_email_retry_bucket_promotes_when_main_is_empty(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["alpha@example.com", "beta@example.com"])

    first = pool.acquire_email()
    assert first is not None
    assert first.value == "alpha@example.com"
    pool.finalize_email(first.id, success=False, retryable=True, reason="protocol_failed", run_id="run_alpha")

    second = pool.acquire_email()
    assert second is not None
    assert second.value == "beta@example.com"
    pool.finalize_email(second.id, success=True, retryable=False, reason="success")

    third = pool.acquire_email()
    assert third is not None
    assert third.value == "alpha@example.com"
    pool.finalize_email(third.id, success=True, retryable=False, reason="success")

    assert pool.snapshot() == {"emails_main": 0, "emails_retry": 0, "emails_failed": 0, "cards": 0, "phones": 0}


def test_card_consumption_and_phone_rotation(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("card", ["4111111111111111|03|2030|123"])
    pool.seed_values("phone", ["+15550000001|https://sms.example/api"])

    card = pool.acquire_card()
    assert card is not None
    pool.consume_card(card.id)
    assert pool.acquire_card() is None

    phone = pool.acquire_phone()
    assert phone is not None
    pool.release_phone(phone.id)
    again = pool.acquire_phone()
    assert again is not None
    assert again.value == phone.value


def test_pool_stats_listing_release_and_delete(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["alpha@example.com"])
    pool.seed_values("card", ["4111111111111111|03|2030|123"])
    pool.seed_values("phone", ["+15550000001|https://sms.example/api"])

    assert pool.stats()["emails_main"] == 1

    email = pool.acquire_item("email")
    assert email is not None
    assert pool.stats()["emails_leased"] == 1
    leased = pool.list_items("email", state="leased")
    assert leased[0]["id"] == email.id
    assert leased[0]["state"] == "leased"

    pool.release_item("email", email.id, reason="manual_release")
    available = pool.get_item("email", email.id)
    assert available is not None
    assert available["state"] == "available"
    assert available["lastReason"] == "manual_release"

    pool.delete_item("email", email.id)
    assert pool.get_item("email", email.id) is None


def test_promote_retry_emails_is_manual_api(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["alpha@example.com"])
    email = pool.acquire_email()
    assert email is not None
    pool.finalize_email(email.id, success=False, retryable=True, reason="payment_failed", run_id="run_alpha")

    assert pool.snapshot()["emails_retry"] == 1
    retry_row = pool.list_items("email", bucket="retry")[0]
    assert retry_row["lastRunId"] == "run_alpha"
    promoted = pool.promote_retry_emails()
    assert promoted == 1
    assert pool.snapshot()["emails_main"] == 1


def test_email_retry_limit_moves_exhausted_retry_item_to_failed_bucket(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test", max_email_retries=1)
    pool.seed_values("email", ["alpha@example.com"])

    first = pool.acquire_email()
    assert first is not None
    pool.finalize_email(first.id, success=False, retryable=True, reason="transient", run_id="run_retry_1")

    second = pool.acquire_email()
    assert second is not None
    assert second.value == "alpha@example.com"
    pool.finalize_email(second.id, success=False, retryable=True, reason="transient", run_id="run_retry_2")

    assert pool.acquire_email() is None
    assert pool.snapshot()["emails_retry"] == 0
    assert pool.snapshot()["emails_failed"] == 1
    failed = pool.list_items("email", state="failed", bucket="failed")
    assert failed[0]["value"] == "alpha@example.com"
    assert failed[0]["lastReason"] == "transient"
    assert failed[0]["lastRunId"] == "run_retry_2"

    restored = pool.restore_failed_emails()
    assert restored == 1
    assert pool.snapshot()["emails_retry"] == 1
    assert pool.snapshot()["emails_failed"] == 0


def test_non_retryable_email_failure_is_visible_in_failed_bucket(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["hard@example.com"])

    email = pool.acquire_email()
    assert email is not None
    pool.finalize_email(email.id, success=False, retryable=False, reason="stripe_amount_check_failed", run_id="run_hard")

    assert pool.acquire_email() is None
    assert pool.stats()["emails_failed"] == 1
    failed = pool.list_items("email", state="failed")
    assert failed[0]["bucket"] == "failed"
    assert failed[0]["lastReason"] == "stripe_amount_check_failed"
    assert failed[0]["lastRunId"] == "run_hard"

    pool.restore_email(failed[0]["id"], bucket="main")
    again = pool.acquire_email()
    assert again is not None
    assert again.value == "hard@example.com"


def test_delete_failed_emails(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["hard1@example.com", "hard2@example.com", "ok@example.com"])

    for run_id in ("run_hard_1", "run_hard_2"):
        email = pool.acquire_email()
        assert email is not None
        pool.finalize_email(email.id, success=False, retryable=False, reason="hard_fail", run_id=run_id)

    assert pool.stats()["emails_failed"] == 2
    deleted = pool.delete_failed_emails()
    assert deleted == 2
    assert pool.stats()["emails_failed"] == 0
    assert pool.stats()["emails_main"] == 1


def test_retry_email_tracks_previous_card_and_phone_for_next_attempt(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["retry@example.com"])
    pool.seed_values("card", ["card-old", "card-new"])
    pool.seed_values("phone", ["phone-old", "phone-new"])

    email = pool.acquire_email()
    assert email is not None
    old_card = pool.acquire_card()
    old_phone = pool.acquire_phone()
    assert old_card is not None
    assert old_phone is not None

    pool.finalize_email(
        email.id,
        success=False,
        retryable=True,
        reason="payment_failed",
        run_id="run_retry",
        card_value=old_card.value,
        phone_value=old_phone.value,
    )
    pool.consume_card(old_card.id)
    pool.release_phone(old_phone.id)

    retry_email = pool.acquire_email()
    assert retry_email is not None
    assert retry_email.value == "retry@example.com"
    assert retry_email.last_card_value == "card-old"
    assert retry_email.last_phone_value == "phone-old"

    next_card = pool.acquire_card(exclude_values=[retry_email.last_card_value])
    next_phone = pool.acquire_phone(exclude_values=[retry_email.last_phone_value])
    assert next_card is not None
    assert next_card.value == "card-new"
    assert next_phone is not None
    assert next_phone.value == "phone-new"


def test_retry_email_exposes_last_reason_for_targeted_resource_rotation(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["retry@example.com"])
    email = pool.acquire_email()
    assert email is not None

    pool.finalize_email(
        email.id,
        success=False,
        retryable=True,
        reason="card_rejected_retry_with_new_card",
        run_id="run_retry",
        card_value="card-old",
        phone_value="phone-old",
    )

    retry_email = pool.acquire_email()
    assert retry_email is not None
    assert retry_email.last_reason == "card_rejected_retry_with_new_card"
    assert retry_email.last_card_value == "card-old"
    assert retry_email.last_phone_value == "phone-old"


def test_randomize_proxy_session_rotates_sid_token():
    value = "us2.cliproxy.io:3010:user-region-US-sid-HfQtVsaJ-t-5:pass"
    rotated, changed = randomize_proxy_session(value)
    assert changed is True
    assert rotated != value
    assert "-sid-HfQtVsaJ-t-" not in rotated
    assert rotated.startswith("us2.cliproxy.io:3010:user-region-US-sid-")
    assert rotated.endswith("-t-5:pass")

    unchanged, changed = randomize_proxy_session("host:2000:user:pass")
    assert changed is False
    assert unchanged == "host:2000:user:pass"


def test_already_paid_success_removes_email_from_pool(tmp_path):
    pool = FullFlowPool(tmp_path / "pool.sqlite3", lease_seconds=60, worker_id="test")
    pool.seed_values("email", ["paid@example.com"])
    email = pool.acquire_email()
    assert email is not None

    pool.finalize_email(
        email.id,
        success=True,
        retryable=False,
        reason='already_paid | checkout failed with HTTP 400: {"detail":"User is already paid"}',
        run_id="run_paid",
    )

    assert pool.stats()["emails_failed"] == 0
    assert pool.stats()["emails_total"] == 0


def test_protocol_already_paid_detection_and_success_dedupe(tmp_path):
    summary = {
        "protocol": {
            "events": [
                {
                    "error": 'POST https://chatgpt.com/backend-api/payments/checkout failed with HTTP 400: {"detail":"User is already paid"}',
                    "exceptionType": "ProtocolResponseError",
                }
            ]
        }
    }
    assert protocol_summary_indicates_already_paid(summary) is True

    success_file = tmp_path / "icsuccess_accounts.txt"
    append_success_account(success_file, "paid@example.com")
    append_success_account(success_file, "paid@example.com")
    lines = success_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0] == "paid@example.com"


def test_card_pool_consumption_detects_whether_card_reached_paypal(tmp_path):
    stalled_log = tmp_path / "stalled.log"
    stalled_log.write_text("PayPal agreements/approve stalled before signup after 32.4s\n", encoding="utf-8")
    assert should_consume_card_pool(
        "payment_failed",
        [{"retryReason": "PayPal agreements/approve stalled before signup", "log": str(stalled_log)}],
    ) is False

    submitted_log = tmp_path / "submitted.log"
    submitted_log.write_text("field cardNumber: ok\n[sms] code received\n", encoding="utf-8")
    assert should_consume_card_pool(
        "payment_failed",
        [{"retryReason": "nonzero_without_result", "log": str(submitted_log)}],
    ) is True

    assert should_consume_card_pool(
        "payment_failed",
        [{"result": {"reason": "paypal_blocked", "status": "failed"}}],
    ) is True
