import os
from typing import List
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PRODUCTION_ENVIRONMENTS = {"production", "prod"}
_ADMIN_DEFAULTS = {"admin_username": "sksk2929", "admin_password": "ysjr0508"}
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
    # The dashboard's own on/off toggle works without it. This same bot also handles
    # official-channel membership verification for the free-trial signup gate below.
    telegram_bot_token: str = ""
    # Public @username of that bot (no leading @), used only to build the
    # t.me/<username>?start=<token> deep link shown to users — not a secret.
    telegram_bot_username: str = ""
    # The official TeleMon channel to require membership in before a free trial can be
    # created. Accepts either a numeric chat id (e.g. "-1001234567890") or a
    # "@channelusername" — whatever the Bot API's getChatMember chat_id accepts.
    # Required for the channel-verification-gated signup flow; the bot must be an
    # admin of this channel to call getChatMember on non-admin members.
    telegram_official_channel_id: str = "@TeleMon_2"

    @property
    def telegram_official_channel_url(self) -> str:
        cid = self.telegram_official_channel_id
        if cid.startswith("@"):
            return f"https://t.me/{cid[1:]}"
        if cid.startswith("-100"):
            return f"https://t.me/c/{cid[3:]}"
        return f"https://t.me/{cid}"

    # JSON object mapping guide-hub button keys (see GUIDE_HUB_BUTTONS in
    # app/services/guide_hub_service.py) to the official channel's guide post URLs, e.g.
    # {"free_trial": "https://t.me/TeleMon_2/12", "auto_reply": "https://t.me/TeleMon_2/15"}.
    # Kept out of source so guide posts can be re-linked without a code change; a key
    # missing from this map simply omits that button rather than erroring.
    telegram_guide_hub_links_json: str = "{}"

    environment: str = "development"
    debug: bool = True

    @field_validator("debug", mode="before")
    @classmethod
    def _coerce_debug(cls, value: str | bool) -> bool:
        if isinstance(value, bool):
            return value
        return value.strip().lower() in ("true", "1", "yes", "on")

    # Single fixed admin account — intentionally not hardcoded in source so it can be
    # changed per-deployment without a code change. The shipped defaults match what was
    # asked for, but "123456" is a very weak password: change it before any deployment
    # that isn't strictly localhost-only.
    admin_username: str = "sksk2929"
    admin_password: str = "ysjr0508"
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
    broadcast_timeout_seconds: int = 600

    # Maximum number of manual retries for a failed broadcast (Sprint 26).
    # Configurable via BROADCAST_MAX_RETRIES env var.  Must be >= 0.
    # Set to 0 to disable retries entirely.
    broadcast_max_retries: int = 3

    # USDT payment wallet address (TRC20 recommended).
    # Used by the USDT watcher scheduler task.
    usdt_wallet_address: str = ""
    usdt_network: str = "TRC20"

    # Bot-facing support contact and announcement text (Telegram ops menu).
    # Both are plain config — no admin UI/DB model yet; update via env var + redeploy.
    telegram_support_username: str = "@telemon_support"
    bot_announcement_text: str = ""

    # Frontend URL for cross-domain redirects and payment success links.
    frontend_url: str = "http://localhost:3000"

    # DeepSeek API (bot "AI Chat" menu). Empty key => feature degrades gracefully
    # (ai_chat_service returns a "not configured" server_error instead of crashing).
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    ai_chat_system_prompt: str = "너는 TeleMon 서비스의 AI Chat 어시스턴트야. 친절하고 간결하게 한국어로 답해줘."
    # How many past turns (user+assistant pairs) to include as context for DeepSeek.
    ai_chat_history_turns: int = 10

    # Graphiti 장기 메모리 (off by default). Set graphiti_enabled=true and fill
    # the connection fields to activate temporal context graph memory for AI features.
    graphiti_enabled: bool = False
    graphiti_uri: str = ""
    graphiti_user: str = ""
    graphiti_password: str = ""
    graphiti_group_id: str = "default"

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

        # In production, debug must be explicitly set to false.
        # The class default is True, which would expose /docs, /redoc, and
        # detailed error traces to the public internet.
        if self.debug:
            findings.append("debug (must be false in production)")

        # In production, sms_provider must not be "console" — that would log
        # verification codes to the server log instead of sending them via SMS,
        # making phone-verification login non-functional for real users.
        if self.sms_provider.strip().lower() == "console":
            findings.append("sms_provider (must be 'twilio' or 'coolsms' in production)")

        # In production, frontend_url must point to the real production domain,
        # not localhost.  Payment success redirects and cross-domain links
        # would break if this still points to localhost.
        if "localhost" in self.frontend_url:
            findings.append("frontend_url (must be production URL, not localhost)")

        # In production, cors_origins must not contain localhost — browser
        # CORS checks would fail for real production origins.
        if "localhost" in self.cors_origins:
            findings.append("cors_origins (must be production origins, not localhost)")

        if not findings:
            return self

        raise ValueError(
            f"Production startup blocked: {', '.join(findings)} "
            "must be overridden via environment variables or .env. "
            "Set secure values before deploying."
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def telegram_guide_hub_links(self) -> dict[str, str]:
        """Parsed button-key -> URL map. Malformed JSON degrades to "no links
        configured" (all buttons omitted) rather than crashing settings load."""
        import json

        try:
            parsed = json.loads(self.telegram_guide_hub_links_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items() if v}

    # ─── MCP Gateway & MCP servers (TeleMon AI Platform Phase 1) ───────────
    # The MCP Gateway exposes registered MCP tool servers (Telegram PoC, Grafana,
    # ...) behind a single authenticated /api/mcp-gateway endpoint. Each server is
    # enabled independently and degrades gracefully when unconfigured.
    mcp_gateway_enabled: bool = True
    # Telegram MCP (PoC) — talks to the TeleMon bot-facing Telegram surface using
    # the existing MTProto/Telethon pool where possible, falling back to config.
    telegram_mcp_enabled: bool = False
    # Grafana MCP — queries Grafana datasources (Prometheus/Loki) over HTTP API.
    grafana_mcp_enabled: bool = False
    grafana_base_url: str = ""
    grafana_api_token: str = ""
    grafana_datasource_uid: str = "prometheus"

    @property
    def telegram_credentials(self) -> tuple[int, str]:
        if not self.telegram_api_id.isdigit() or not self.telegram_api_hash:
            raise RuntimeError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH가 설정되지 않았습니다. "
                "https://my.telegram.org 에서 발급받아 .env에 입력하세요."
            )
        return int(self.telegram_api_id), self.telegram_api_hash

    # NOWPayments configuration
    NOWPAYMENTS_API_KEY: str = os.getenv("NOWPAYMENTS_API_KEY", "")
    NOWPAYMENTS_PUBLIC_KEY: str = os.getenv("NOWPAYMENTS_PUBLIC_KEY", "")
    NOWPAYMENTS_IPN_SECRET: str = os.getenv("NOWPAYMENTS_IPN_SECRET", "")


settings = Settings()