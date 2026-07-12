# Telegram ToS-friendly self-imposed limits for the broadcast feature.
MAX_RECIPIENTS_PER_BROADCAST = 10
BROADCAST_MIN_INTERVAL_SECONDS = 60
# Small pause between individual sends within one broadcast, on top of the
# per-account cooldown above, so a 10-recipient job doesn't fire in a burst.
INTER_MESSAGE_DELAY_SECONDS = 2

MAX_MEDIA_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MEDIA_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

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
