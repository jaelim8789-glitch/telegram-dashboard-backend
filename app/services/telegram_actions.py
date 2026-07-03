import asyncio

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from app.core.crypto import decrypt_session
from app.core.limits import INTER_MESSAGE_DELAY_SECONDS
from app.models.account import Account
from app.services.telethon_pool import pool


class AccountNotAuthenticatedError(Exception):
    pass


async def get_authorized_client(account: Account) -> TelegramClient:
    if not account.session_data:
        raise AccountNotAuthenticatedError(
            "계정이 아직 인증되지 않았습니다. 먼저 '계정 등록'에서 Telegram 인증을 완료해주세요."
        )
    session_string = decrypt_session(account.session_data)
    client = await pool.get_client(account.id, session_string)
    if not await client.is_user_authorized():
        raise AccountNotAuthenticatedError("텔레그램 세션이 만료되었습니다. 다시 인증해주세요.")
    return client


def _classify_entity(entity) -> str | None:
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "megagroup" if entity.megagroup else "channel"
    return None


async def list_groups(account: Account) -> list[dict]:
    client = await get_authorized_client(account)
    groups: list[dict] = []
    async for dialog in client.iter_dialogs():
        group_type = _classify_entity(dialog.entity)
        if group_type is None:
            continue  # skip 1:1 conversations — only groups/channels are valid broadcast targets
        groups.append(
            {
                "id": str(dialog.id),
                "title": dialog.name or "(제목 없음)",
                "type": group_type,
                "participants_count": getattr(dialog.entity, "participants_count", None),
            }
        )
    return groups


def _resolve_target(recipient: str) -> int | str:
    stripped = recipient.lstrip("-")
    return int(recipient) if stripped.isdigit() else recipient


async def run_broadcast(
    account: Account,
    recipients: list[str],
    message: str,
    media_path: str | None,
) -> tuple[bool, str | None]:
    """Sends to each recipient with a pacing delay between sends.

    Continues past individual failures so one blocked/invalid recipient doesn't
    abort the rest of an already-small (<=10) batch. Returns (all_succeeded, error_message).
    """
    client = await get_authorized_client(account)
    errors: list[str] = []

    for index, recipient in enumerate(recipients):
        target = _resolve_target(recipient)
        try:
            if media_path:
                await client.send_file(target, media_path, caption=message)
            else:
                await client.send_message(target, message)
        except Exception as exc:  # noqa: BLE001 — recorded per-recipient, not swallowed
            errors.append(f"{recipient}: {exc}")

        if index < len(recipients) - 1:
            await asyncio.sleep(INTER_MESSAGE_DELAY_SECONDS)

    if errors:
        return False, "; ".join(errors)
    return True, None
