# Telegram ToS-friendly self-imposed limits for the broadcast feature.
MAX_RECIPIENTS_PER_BROADCAST = 10
BROADCAST_MIN_INTERVAL_SECONDS = 60
# Small pause between individual sends within one broadcast, on top of the
# per-account cooldown above, so a 10-recipient job doesn't fire in a burst.
INTER_MESSAGE_DELAY_SECONDS = 2

MAX_MEDIA_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MEDIA_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
