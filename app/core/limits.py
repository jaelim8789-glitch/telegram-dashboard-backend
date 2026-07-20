# Telegram ToS-friendly self-imposed limits for the broadcast feature.
# No hard cap on recipients per broadcast — the plan's monthly_message_limit
# and per-account cooldown provide sufficient guardrails.
# 달라진 점: BROADCAST_MIN_INTERVAL_SECONDS 이제 batch_size 기반으로 동적 조정.
# 실제 유효 간격 = BROADCAST_MIN_INTERVAL_SECONDS / batch_size (최소 5초)
BROADCAST_MIN_INTERVAL_SECONDS = 60
# Small pause between individual sends within one broadcast, on top of the
# per-account cooldown above, so a burst doesn't hit Telegram's rate limits.
INTER_MESSAGE_DELAY_SECONDS = 2

# 최소 rate limit 간격 (batch_size 기반 동적 계산)
MINIMUM_INTERVAL_SECONDS = 5


def effective_broadcast_interval(batch_size: int | None = None) -> int:
    """batch_size에 따라 rate limit 간격을 동적 계산.
    batch_size=1 → 60초, 5→12초, 10→6초, 50→5초(최소)
    """
    if batch_size is None or batch_size <= 1:
        return BROADCAST_MIN_INTERVAL_SECONDS
    return max(MINIMUM_INTERVAL_SECONDS, BROADCAST_MIN_INTERVAL_SECONDS // batch_size)

MAX_MEDIA_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
ALLOWED_MEDIA_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska"}

# Phone verification (Sprint 6 user login) — SMS costs money, so codes are deliberately
# short-lived and resend is throttled per phone number.
OTP_CODE_LENGTH = 6
OTP_EXPIRE_MINUTES = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_ATTEMPTS = 5

# Per-IP rate limiting for public auth endpoints (no auth required to call these)
SEND_CODE_MAX_PER_IP = 10       # max send-code requests per IP in the window
SEND_CODE_PER_IP_WINDOW = 300   # 5-minute sliding window
VERIFY_CODE_MAX_PER_IP = 30     # max verify-code requests per IP in the window
VERIFY_CODE_PER_IP_WINDOW = 300 # 5-minute sliding window

# Reply macro limits
MACRO_MAX_TARGETS_PER_MACRO = 50  # max chats a single macro can target
MACRO_MIN_INTERVAL_HOURS = 1  # minimum interval_hours for interval-type macros

# Group search & join controls — user picks which groups to join, so it's not automated
# spam, but we still limit daily joins per account to stay well within Telegram's rate
# limits and avoid looking like a bot.
MAX_DAILY_JOINS = 5
