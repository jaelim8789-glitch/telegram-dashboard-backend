"""Tests for broadcast failure intelligence (no async/DB dependencies).

Pure function tests that do not need a database session.
"""

from datetime import datetime

from app.services.failure_intel import classify_failure


def test_successful_broadcast_returns_none():
    assert classify_failure("sent", None) is None
    assert classify_failure("sent", "") is None
    assert classify_failure("pending", "error") is None


def test_no_error_message_returns_none():
    assert classify_failure("failed", None) is None
    assert classify_failure("failed", "") is None


def test_non_failed_broadcast_returns_none():
    assert classify_failure("pending", None) is None
    assert classify_failure("sending", None) is None


def test_classify_session_expired():
    result = classify_failure("failed", "Telegram  .")
    assert result is not None
    assert result["category"] == "unauthorized"
    assert result["retryable"] == "conditional"
    assert result["recovery_action"] == "reauthenticate_account"


def test_classify_banned():
    result = classify_failure("failed", "  .")
    assert result is not None
    assert result["category"] == "banned"
    assert result["retryable"] == "not_retryable"
    assert result["recovery_action"] == "account_is_banned"


def test_classify_rate_limited():
    result = classify_failure("failed", " :  1 1   .")
    assert result is not None
    assert result["category"] == "rate_limited"
    assert result["retryable"] == "retryable"
    assert result["recovery_action"] == "wait_and_retry"


def test_classify_timeout():
    result = classify_failure("failed", "   (300).")
    assert result is not None
    assert result["category"] == "timeout"
    assert result["retryable"] == "retryable"
    assert result["recovery_action"] == "retry_broadcast"


def test_classify_flood_wait():
    result = classify_failure("failed", "  : 60  ")
    assert result is not None
    assert result["category"] == "rate_limited"
    assert result["retryable"] == "retryable"


def test_classify_network_error():
    result = classify_failure("failed", "  .  .")
    assert result is not None
    assert result["category"] == "temporary_network"
    assert result["retryable"] == "retryable"


def test_classify_invalid_recipient():
    result = classify_failure("failed", "  .")
    assert result is not None
    assert result["category"] == "invalid_recipient"
    assert result["retryable"] == "not_retryable"
    assert result["recovery_action"] == "check_recipient"


def test_classify_unknown_fallback():
    result = classify_failure("failed", "Some unusual error occurred")
    assert result is not None
    assert result["category"] == "unknown"
    assert result["retryable"] == "conditional"
    assert result["recovery_action"] == "retry_broadcast"


def test_classify_account_not_found():
    result = classify_failure("failed", "   .")
    assert result is not None
    assert result["category"] == "configuration"
    assert result["retryable"] == "not_retryable"
    assert result["recovery_action"] == "check_configuration"


def test_classify_max_retries_exceeded():
    result = classify_failure("failed", "  (3) .")
    assert result is not None
    assert result["category"] == "configuration"
    assert result["retryable"] == "not_retryable"
    assert result["recovery_action"] == "contact_support"


def test_classify_forbidden():
    result = classify_failure("failed", "     .")
    assert result is not None
    assert result["category"] == "invalid_recipient"
    assert result["retryable"] == "not_retryable"


def test_partial_success_returns_none():
    """Broadcasts with status=sent (partial success) get no failure_info."""
    result = classify_failure("sent", "   :  ")
    assert result is None


def test_failure_info_never_exposes_raw_details():
    result = classify_failure("failed", "  .")
    assert result is not None
    for key, value in result.items():
        if isinstance(value, str):
            assert "Exception" not in value
            assert "Traceback" not in value
            assert "secret" not in value.lower()


def test_failure_info_dict_shape():
    result = classify_failure("failed", " ")
    assert result is not None
    assert set(result.keys()) == {"category", "retryable", "recovery_action", "summary"}
    assert isinstance(result["category"], str)
    assert isinstance(result["retryable"], str)
    assert isinstance(result["recovery_action"], str)
    assert isinstance(result["summary"], str)
