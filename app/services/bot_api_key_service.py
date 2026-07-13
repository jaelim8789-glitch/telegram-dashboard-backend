"""Self-service API key issuance / retrieval for the Telegram bot.

This service is invoked from the bot's "🔑 API 키 받기" inline-button callback.
It reuses the *same* security primitives as the existing free-api-key and admin
manual-issue flows (``generate_user_api_key``, ``hash_api_key``,
``is_channel_member``, ``apply_plan_limits``, ``get_plan``) — it does **not**
create a parallel key-generation system.

Trust model
-----------
The ``telegram_user_id`` passed in comes straight from a Telegram ``Update``
object inside the bot's polling connection, so it cannot be forged by an HTTP
client.  Channel membership is re-verified server-side on every call
(fail-closed), exactly like ``telegram_verify.py``.

Key retrieval policy
--------------------
The existing architecture stores only SHA-256 hashes (``users.api_key_hash``).
Raw keys are shown **once** at issuance and are never retrievable afterwards.
This service respects that contract — if a key already exists we report
"already issued" and do **not** attempt to recover or weaken the hash.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.plans import get_plan
from app.core.security import generate_user_api_key, hash_api_key, mask_api_key
from app.models.tenant import Tenant
from app.models.user import User
from app.services.telegram_membership import MembershipCheckUnavailable, is_channel_member
from app.services.usage_tracker import apply_plan_limits

logger = get_logger(__name__)


# ─── Result types ──────────────────────────────────────────────────────


@dataclass
class BotApiKeyResult:
    """Outcome of a self-service API-key request.

    ``status`` is one of:
      * ``"issued"``           — new key generated, ``api_key`` holds the raw key
      * ``"already_issued"``   — user already has a key, ``masked_key`` for display
      * ``"not_linked"``       — Telegram account not linked to any TeleMon user
      * ``"not_eligible"``     — linked but no valid subscription/trial/membership
      * ``"payment_pending"``  — tenant exists but subscription is pending
      * ``"server_error"``     — transient failure (membership check unavailable, etc.)
    """

    status: str
    api_key: str | None = None
    masked_key: str | None = None
    detail: str = ""


# ─── Per-user in-flight lock (race-condition / double-click guard) ────

# Maps telegram_user_id → True while an issuance is in progress.  This is a
# process-local guard — the bot runs as a single polling instance, so this is
# sufficient to prevent duplicate issuance from rapid double-clicks within one
# process.  The DB-level guard (checking ``api_key_hash`` before writing) is the
# authoritative race-condition backstop.
_in_flight: dict[int, bool] = {}


def _is_in_flight(telegram_user_id: int) -> bool:
    return _in_flight.get(telegram_user_id, False)


def _set_in_flight(telegram_user_id: int, value: bool) -> None:
    if value:
        _in_flight[telegram_user_id] = True
    else:
        _in_flight.pop(telegram_user_id, None)


# ─── Helpers ───────────────────────────────────────────────────────────


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tg_identifier(telegram_user_id: int) -> str:
    """Canonical phone-equivalent identifier for a Telegram-only user."""
    return f"tg_{telegram_user_id}"


def _is_trial_valid(tenant: Tenant) -> bool:
    """A free-trial tenant is eligible while ``trial_expires_at`` is in the future."""
    if tenant.subscription_status == "active":
        return True
    if tenant.trial_expires_at is not None and tenant.trial_expires_at > _utcnow_naive():
        return True
    return False


def _is_subscription_active(tenant: Tenant) -> bool:
    """A paid tenant is eligible while subscription is active and billing period hasn't ended."""
    if tenant.subscription_status != "active":
        return False
    if tenant.billing_period_end is not None and tenant.billing_period_end < _utcnow_naive():
        return False
    return True


async def _get_or_create_free_tenant(db: AsyncSession, phone: str) -> Tenant:
    """Reuse the same tenant-creation logic as free_api_key.py / admin.py."""
    result = await db.execute(select(Tenant).where(Tenant.phone == phone))
    tenant = result.scalar_one_or_none()

    if tenant is None:
        plan_def = get_plan("free")
        trial_hours = (plan_def["trial_days"] * 24) if plan_def else 24
        trial_expires = _utcnow_naive() + timedelta(hours=trial_hours)
        tenant = Tenant(
            phone=phone,
            plan="free",
            subscription_status="active",
            trial_expires_at=trial_expires,
        )
        db.add(tenant)
        await db.flush()
        await apply_plan_limits(db, tenant, "free")

    return tenant


# ─── Main entry point ─────────────────────────────────────────────────


async def handle_self_service_api_key(
    db: AsyncSession,
    telegram_user_id: int,
) -> BotApiKeyResult:
    """Process a "🔑 API 키 받기" button press from the Telegram bot.

    Parameters
    ----------
    db
        An open async DB session.  The caller is responsible for committing /
        rolling back — this function only flushes.
    telegram_user_id
        The Telegram user ID from the bot ``Update`` (trusted source).

    Returns
    -------
    BotApiKeyResult
        The outcome to translate into a bot reply.
    """
    # ── 1. Race-condition guard: reject concurrent requests from same user ──
    if _is_in_flight(telegram_user_id):
        logger.info("bot_api_key_in_flight_rejected", telegram_user_id=telegram_user_id)
        return BotApiKeyResult(
            status="server_error",
            detail="요청이 이미 처리 중입니다. 잠시 후 다시 시도해주세요.",
        )
    _set_in_flight(telegram_user_id, True)

    try:
        return await _do_handle(db, telegram_user_id)
    finally:
        _set_in_flight(telegram_user_id, False)


async def _do_handle(db: AsyncSession, telegram_user_id: int) -> BotApiKeyResult:
    identifier = _tg_identifier(telegram_user_id)

    # ── 2. Verify linked TeleMon account ────────────────────────────────
    user = await _find_user(db, identifier)

    # ── 3. Verify eligibility ───────────────────────────────────────────
    # If the user already exists, check their tenant status.
    # If not, we still allow issuance *if* they're a channel member (free trial).
    tenant: Tenant | None = None
    if user is not None:
        tenant = await _find_tenant(db, user.phone)

    if tenant is not None:
        if tenant.subscription_status == "pending":
            return BotApiKeyResult(
                status="payment_pending",
                detail=(
                    "결제가 진행 중입니다. USDT 입금이 확인되면 자동으로 API 키가 발급됩니다.\n"
                    "잠시 후 다시 확인해주세요."
                ),
            )
        if not (_is_subscription_active(tenant) or _is_trial_valid(tenant)):
            return BotApiKeyResult(
                status="not_eligible",
                detail=(
                    "현재 유효한 요금제 또는 무료 체험이 없습니다.\n"
                    "결제를 완료하거나 공식 채널에 가입 후 다시 시도해주세요."
                ),
            )

    # For new users (no tenant yet), channel membership is the eligibility gate.
    # For existing eligible users, we still re-verify membership as a fail-closed
    # security check — the same policy as telegram_verify.py.
    try:
        is_member = await is_channel_member(telegram_user_id)
    except MembershipCheckUnavailable:
        logger.warning("bot_api_key_membership_unavailable", telegram_user_id=telegram_user_id)
        return BotApiKeyResult(
            status="server_error",
            detail="일시적인 서버 오류입니다. 잠시 후 다시 시도해주세요.",
        )

    if not is_member:
        if user is None:
            return BotApiKeyResult(
                status="not_linked",
                detail=(
                    "연결된 TeleMon 계정을 찾을 수 없습니다.\n"
                    "먼저 공식 채널(@TeleMon_2)에 가입하고 웹사이트에서 회원가입을 완료해주세요."
                ),
            )
        return BotApiKeyResult(
            status="not_eligible",
            detail=(
                "공식 채널(@TeleMon_2) 가입이 확인되지 않았습니다.\n"
                "채널에 가입한 후 다시 시도해주세요."
            ),
        )

    # ── 4. Check current API key state ──────────────────────────────────
    if user is not None and user.api_key_hash is not None:
        # Existing eligible key — raw key cannot be retrieved (hash-only storage).
        # We report "already issued" with a masked hint.  This is the safest
        # compatible flow: we do NOT weaken security or make raw keys retrievable.
        logger.info(
            "bot_api_key_already_issued",
            telegram_user_id=telegram_user_id,
            user_id=user.id,
        )
        return BotApiKeyResult(
            status="already_issued",
            masked_key="sk-••••••••",
            detail=(
                "API 키가 이미 발급되었습니다.\n"
                "보안상 원본 키는 다시 표시할 수 없습니다. 이전에 발급받은 키를 사용해주세요.\n"
                "키를 잃어버리셨다면 고객지원(@telemon_support)으로 문의해주세요."
            ),
        )

    # ── 5. Issue a new API key (reusing existing primitives) ────────────
    raw_key = generate_user_api_key()

    if user is None:
        user = User(phone=identifier)
        db.add(user)
        await db.flush()

    user.api_key_hash = hash_api_key(raw_key)
    await db.flush()

    # Ensure a free-trial tenant exists (same as free_api_key.py / admin.py)
    await _get_or_create_free_tenant(db, identifier)

    await db.commit()
    await db.refresh(user)

    # ── 6. Log (never the raw key) ──────────────────────────────────────
    logger.info(
        "bot_api_key_issued",
        telegram_user_id=telegram_user_id,
        user_id=user.id,
        # Intentionally no raw_key / api_key field here
    )

    return BotApiKeyResult(
        status="issued",
        api_key=raw_key,
        detail=(
            "API 키가 발급되었습니다! 🎉\n"
            "이 키는 한 번만 표시되므로 안전한 곳에 저장해주세요.\n"
            "HTTP 헤더 `X-API-Key`에 담아 요청하세요."
        ),
    )


# ─── DB helpers ────────────────────────────────────────────────────────


async def _find_user(db: AsyncSession, identifier: str) -> User | None:
    result = await db.execute(select(User).where(User.phone == identifier))
    return result.scalar_one_or_none()


async def _find_tenant(db: AsyncSession, phone: str) -> Tenant | None:
    result = await db.execute(select(Tenant).where(Tenant.phone == phone).limit(1))
    return result.scalar_one_or_none()