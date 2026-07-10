from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure default values that must be overridden before production deployment.
# These match the shipped class-level defaults above.
_PRODUCTION_ENVIRONMENTS = {"production", "prod"}
_ADMIN_DEFAULTS = {"admin_username": "123123", "admin_password": "123456"}
_JWT_DEFAULT = "change-me-in-production"


class Settings(BaseSettings):
    database_url: str
    cors_origins: str = "http://localhost:3000"

    encryption_key: str
    # Kept as strings (not int) so a missing/placeholder value doesn't crash the whole app
    # on startup — only the Telegram-auth endpoints fail, with a clear error, until configured.
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    # Optional — only needed for the /autoreply remote-control bot (BotFather token).
    # The dashboard's own on/off toggle works without it.
    telegram_bot_token: str = ""

    environment: str = "development"
    debug: bool = True

    # Single fixed admin account — intentionally not hardcoded in source so it can be
    # changed per-deployment without a code change. The shipped defaults match what was
    # asked for, but "123456" is a very weak password: change it before any deployment
    # that isn't strictly localhost-only.
    admin_username: str = "123123"
    admin_password: str = "123456"
    admin_jwt_secret: str = "change-me-in-production"
    admin_jwt_expire_minutes: int = 60 * 24

    # SMS provider for phone-verification login (Sprint 6). "console" logs the code
    # instead of sending a real SMS — free, for local dev only. Switch to "twilio" or
    # "coolsms" (and fill in the matching credentials) for a real deployment.
    sms_provider: str = "console"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    coolsms_api_key: str = ""
    coolsms_api_secret: str = ""
    coolsms_phone_number: str = ""

    # Broadcast execution timeout in seconds (Sprint 23+).
    # Prevents a stuck delivery from blocking the scheduler indefinitely.
    # Configurable via BROADCAST_TIMEOUT_SECONDS env var.  Must be >= 1.
    broadcast_timeout_seconds: int = 300

    # Maximum number of manual retries for a failed broadcast (Sprint 26).
    # Configurable via BROADCAST_MAX_RETRIES env var.  Must be >= 0.
    # Set to 0 to disable retries entirely.
    broadcast_max_retries: int = 3

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        # Hosted Postgres providers (Render, Heroku, Railway, ...) hand out connection
        # strings using the sync `postgresql://`/`postgres://` scheme; SQLAlchemy's async
        # engine needs the asyncpg driver spelled out explicitly.
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://") :]
        return value

    @field_validator("broadcast_timeout_seconds")
    @classmethod
    def _validate_broadcast_timeout(cls, value: int) -> int:
        """Reject zero, negative, or unreasonably small values.

        A timeout < 1 second would make even a single-message send impossible;
        values between 1 and 9 are accepted (useful for testing) but warned against
        in production documentation.
        """
        if value < 1:
            raise ValueError(
                f"BROADCAST_TIMEOUT_SECONDS must be >= 1, got {value}. "
                "Set a positive integer (default 300) in your .env file."
            )
        return value

    @field_validator("broadcast_max_retries")
    @classmethod
    def _validate_broadcast_max_retries(cls, value: int) -> int:
        """Reject negative values.  Zero is accepted (disables retries)."""
        if value < 0:
            raise ValueError(
                f"BROADCAST_MAX_RETRIES must be >= 0, got {value}. "
                "Set a non-negative integer (default 3) in your .env file."
            )
        return value

    @model_validator(mode="after")
    def _reject_insecure_production_defaults(self) -> "Settings":
        env = self.environment.strip().lower()
        if env not in _PRODUCTION_ENVIRONMENTS:
            return self

        findings: list[str] = []

        for field, default in _ADMIN_DEFAULTS.items():
            if getattr(self, field) == default:
                findings.append(field)

        if self.admin_jwt_secret == _JWT_DEFAULT:
            findings.append("admin_jwt_secret")

        if not findings:
            return self

        raise ValueError(
            f"Production startup blocked: default {', '.join(findings)} "
            "must be overridden via environment variables or .env. "
            "Set secure values before deploying."
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def telegram_credentials(self) -> tuple[int, str]:
        if not self.telegram_api_id.isdigit() or not self.telegram_api_hash:
            raise RuntimeError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH가 설정되지 않았습니다. "
                "https://my.telegram.org 에서 발급받아 .env에 입력하세요."
            )
        return int(self.telegram_api_id), self.telegram_api_hash


settings = Settings()