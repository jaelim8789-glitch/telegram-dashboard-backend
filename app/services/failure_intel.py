"""Broadcast failure classification — deterministic, operator-safe.

Maps the canonical DeliveryStatus and raw error patterns into a normalized
failure_intel structure for frontend recovery display.

No AI, no external services, no large subsystem.

Every error_message string produced by this module is safe for operator
display — no stack traces, secrets, paths, or internal identifiers.
"""

from __future__ import annotations

from typing import Literal

# The canonical failure categories that the frontend can act on.
# Each corresponds to a different operator recovery path.
FailureCategory = Literal[
    "unauthorized",
    "banned",
    "rate_limited",
    "invalid_recipient",
    "media_error",
    "temporary_network",
    "configuration",
    "timeout",
    "unknown",
]

Retryability = Literal["retryable", "not_retryable", "conditional"]

RecoveryAction = Literal[
    "reauthenticate_account",
    "account_is_banned",
    "wait_and_retry",
    "check_recipient",
    "check_media",
    "check_configuration",
    "retry_broadcast",
    "contact_support",
    "none",
]


class FailureIntel:
    """Normalized failure information for a broadcast.

    This is an additive, optional field on BroadcastRead.  Successful
    broadcasts and broadcasts without error data get None here.
    """

    category: FailureCategory
    retryable: Retryability
    recovery_action: RecoveryAction
    summary: str

    def __init__(
        self,
        category: FailureCategory,
        retryable: Retryability,
        recovery_action: RecoveryAction,
        summary: str,
    ) -> None:
        self.category = category
        self.retryable = retryable
        self.recovery_action = recovery_action
        self.summary = summary

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            "summary": self.summary,
        }


def _classify_from_status(status_value: str, error_message: str | None) -> FailureIntel:
    """Classify a failure from the stored DeliveryStatus value + error_message.

    The DeliveryStatus (from app.services.delivery.DeliveryStatus) is our
    primary signal because the delivery pipeline already classifies Telethon
    exceptions.  For legacy broadcasts that only have an error_message string
    (no structured status), we fall back to pattern matching.
    """
    if not error_message:
        return _unknown("알 수 없는 오류입니다.")

    emsg = error_message.lower()

    # ---- Primary: classify by DeliveryStatus value ----
    if status_value == "session_expired":
        return FailureIntel(
            category="unauthorized",
            retryable="conditional",
            recovery_action="reauthenticate_account",
            summary="Telegram 세션이 만료되었습니다. 계정 등록 탭에서 재인증해주세요.",
        )

    if status_value == "banned":
        return FailureIntel(
            category="banned",
            retryable="not_retryable",
            recovery_action="account_is_banned",
            summary="계정이 Telegram에서 차단되었습니다. 복구가 필요합니다.",
        )

    if status_value == "flood_wait":
        return FailureIntel(
            category="rate_limited",
            retryable="retryable",
            recovery_action="wait_and_retry",
            summary="Telegram 속도 제한이 걸렸습니다. 시간을 두고 다시 시도하세요.",
        )

    if status_value == "invalid_recipient":
        return FailureIntel(
            category="invalid_recipient",
            retryable="not_retryable",
            recovery_action="check_recipient",
            summary="유효하지 않은 수신자입니다. 대상 ID를 확인해주세요.",
        )

    if status_value == "forbidden":
        return FailureIntel(
            category="invalid_recipient",
            retryable="not_retryable",
            recovery_action="check_recipient",
            summary="해당 채팅방에 메시지를 보낼 권한이 없습니다.",
        )

    if status_value == "network_error":
        return FailureIntel(
            category="temporary_network",
            retryable="retryable",
            recovery_action="wait_and_retry",
            summary="네트워크 오류가 발생했습니다. 다시 시도해주세요.",
        )

    if status_value == "internal_error":
        return FailureIntel(
            category="configuration",
            retryable="conditional",
            recovery_action="check_configuration",
            summary="내부 설정 오류가 발생했습니다. 계정 상태를 확인해주세요.",
        )

    if status_value == "permanent_failure":
        return FailureIntel(
            category="unknown",
            retryable="conditional",
            recovery_action="retry_broadcast",
            summary="Telegram에서 요청을 처리할 수 없습니다. 다시 시도해보세요.",
        )

    # ---- Fallback: pattern-match the error message string ----
    # Covers legacy/imported broadcasts stored before structured status
    if "세션" in emsg or "인증" in emsg or "재인증" in emsg:
        return FailureIntel(
            category="unauthorized",
            retryable="conditional",
            recovery_action="reauthenticate_account",
            summary="Telegram 세션이 만료되었습니다. 계정 등록 탭에서 재인증해주세요.",
        )

    if "차단" in emsg or "banned" in emsg:
        return FailureIntel(
            category="banned",
            retryable="not_retryable",
            recovery_action="account_is_banned",
            summary="계정이 Telegram에서 차단되었습니다.",
        )

    if "제한" in emsg or "rate" in emsg or "flood" in emsg:
        return FailureIntel(
            category="rate_limited",
            retryable="retryable",
            recovery_action="wait_and_retry",
            summary="Telegram 속도 제한이 걸렸습니다. 시간을 두고 다시 시도하세요.",
        )

    if "계정을 찾을 수 없" in emsg:
        return FailureIntel(
            category="configuration",
            retryable="not_retryable",
            recovery_action="check_configuration",
            summary="연결된 계정을 찾을 수 없습니다. 계정 상태를 확인해주세요.",
        )

    if "수신자" in emsg or "recipient" in emsg or "찾을 수 없" in emsg:
        return FailureIntel(
            category="invalid_recipient",
            retryable="not_retryable",
            recovery_action="check_recipient",
            summary="유효하지 않은 수신자입니다. 대상 ID를 확인해주세요.",
        )

    if "권한" in emsg or "forbidden" in emsg:
        return FailureIntel(
            category="invalid_recipient",
            retryable="not_retryable",
            recovery_action="check_recipient",
            summary="해당 채팅방에 메시지를 보낼 권한이 없습니다.",
        )

    if "시간이 초과" in emsg:
        return FailureIntel(
            category="timeout",
            retryable="retryable",
            recovery_action="retry_broadcast",
            summary="발송 시간이 초과되었습니다. 다시 시도해주세요.",
        )

    if "네트워크" in emsg or "network" in emsg or "timeout" in emsg or "초과" in emsg:
        return FailureIntel(
            category="temporary_network",
            retryable="retryable",
            recovery_action="wait_and_retry",
            summary="네트워크 오류 또는 시간 초과가 발생했습니다. 다시 시도해주세요.",
        )

    if "시간이 초과" in emsg or (status_value == "failed" and error_message and "초" in error_message and "제한" not in emsg):
        return FailureIntel(
            category="timeout",
            retryable="retryable",
            recovery_action="retry_broadcast",
            summary="발송 시간이 초과되었습니다. 다시 시도해주세요.",
        )

    if "계정을 찾을 수 없" in emsg:
        return FailureIntel(
            category="configuration",
            retryable="not_retryable",
            recovery_action="check_configuration",
            summary="연결된 계정을 찾을 수 없습니다. 계정 상태를 확인해주세요.",
        )

    if "미디어" in emsg or "파일" in emsg or "media" in emsg or "file" in emsg:
        return FailureIntel(
            category="media_error",
            retryable="conditional",
            recovery_action="check_media",
            summary="미디어 파일 오류가 발생했습니다. 파일을 확인해주세요.",
        )

    if "최대 재시도" in emsg or "retry" in emsg:
        return FailureIntel(
            category="configuration",
            retryable="not_retryable",
            recovery_action="contact_support",
            summary="최대 재시도 횟수에 도달했습니다. 새 발송을 생성해주세요.",
        )

    return _unknown(error_message)


def _unknown(error_message: str | None) -> FailureIntel:
    return FailureIntel(
        category="unknown",
        retryable="conditional",
        recovery_action="retry_broadcast",
        summary=error_message or "알 수 없는 오류가 발생했습니다.",
    )


def classify_failure(
    status: str | None,
    error_message: str | None,
    *,
    delivery_status_value: str | None = None,
) -> dict | None:
    """Classify a broadcast failure into normalized FailureIntel.

    Args:
        status: The broadcast status string ("failed", "sent", etc.).
        error_message: The stored error_message on the broadcast.
        delivery_status_value: The canonical DeliveryStatus value, if available
            (from MessageLog.status or similar).  When absent, falls back to
            pattern-matching error_message.

    Returns:
        A FailureIntel dict if the broadcast is failed and has error data,
        or None for successful broadcasts / broadcasts with no error data.
    """
    if status != "failed" or not error_message:
        return None

    intel = _classify_from_status(delivery_status_value or "", error_message)
    return intel.to_dict()
