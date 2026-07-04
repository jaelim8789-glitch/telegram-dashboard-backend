from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
