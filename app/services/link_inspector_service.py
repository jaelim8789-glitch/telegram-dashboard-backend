import asyncio
import re

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready

from app.core.limits import MAX_DAILY_JOINS
from app.core.logging import get_logger
from app.crud import group_search as group_search_crud
from app.models.account import Account
from app.schemas.link_inspector import LinkJoinTarget
from app.services.telegram_actions import get_authorized_client

logger = get_logger(__name__)

# Reuses the group-search join audit trail (GroupJoinLog) and its daily counter —
# joining via pasted links is the same JoinChannelRequest action with the same
# ban risk, so it should draw from the same per-account daily budget rather than
# a separate one an operator could stack on top of the group-search limit.
JOIN_LOG_SOURCE = "bulk_link_inspector"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


class DailyJoinLimitExceededError(Exception):
    pass


def _classify_entity(entity) -> str | None:
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "megagroup" if entity.megagroup else "channel"
    return None


def parse_telegram_link(raw: str) -> tuple[str, str]:
    """Normalize a pasted t.me link/username into (kind, value).

    kind is "username", "invite", or "invalid".
    """
    s = raw.strip()
    if not s:
        return "invalid", raw

    if s.startswith("@"):
        uname = s[1:].strip()
        return ("username", uname) if USERNAME_RE.match(uname) else ("invalid", raw)

    s2 = s
    for prefix in ("https://", "http://"):
        if s2.startswith(prefix):
            s2 = s2[len(prefix):]
            break

    matched_domain = False
    for domain in ("t.me/", "telegram.me/", "telegram.dog/"):
        if s2.startswith(domain):
            s2 = s2[len(domain):]
            matched_domain = True
            break

    if not matched_domain:
        # Not a URL — maybe a bare username was pasted.
        return ("username", s2) if USERNAME_RE.match(s2) else ("invalid", raw)

    s2 = s2.split("?")[0].strip("/")
    if not s2:
        return "invalid", raw

    if s2.startswith("+"):
        return "invite", s2[1:]
    if s2.startswith("joinchat/"):
        return "invite", s2[len("joinchat/"):]
    if s2.startswith("c/"):
        # Numeric private-channel links (t.me/c/<id>/<msg>) aren't resolvable
        # without the account already being a member — treat as dead.
        return "invalid", raw

    uname = s2.split("/")[0]
    return ("username", uname) if USERNAME_RE.match(uname) else ("invalid", raw)


def _dedupe_links(links: list[str]) -> tuple[list[str], int]:
    """Returns (deduped_links, duplicates_removed_count).

    Dedupe key is the normalized (kind, value) pair so "t.me/foo", "@foo", and
    "https://t.me/foo?x=1" all collapse to the same link before we spend a
    Telethon round-trip on each.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[str] = []
    duplicates = 0
    for link in links:
        kind, value = parse_telegram_link(link)
        key = (kind, value.lower())
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(link)
    return deduped, duplicates


async def _inspect_one(client: TelegramClient, raw_link: str) -> dict:
    kind, value = parse_telegram_link(raw_link)

    if kind == "invalid":
        return {
            "raw_link": raw_link,
            "status": "dead",
            "accessible": False,
            "reason": "유효하지 않은 링크 형식입니다.",
        }

    try:
        if kind == "username":
            entity = await client.get_entity(value)
            chat_type = _classify_entity(entity)
            if chat_type is None:
                return {
                    "raw_link": raw_link,
                    "status": "dead",
                    "accessible": False,
                    "reason": "그룹 또는 채널이 아닙니다 (사용자 계정 링크).",
                }
            return {
                "raw_link": raw_link,
                "status": "active",
                "accessible": True,
                "title": entity.title,
                "chat_type": chat_type,
                "username": value,
                "chat_id": str(entity.id),
                "participants_count": getattr(entity, "participants_count", None),
            }

        # kind == "invite"
        result = await client(CheckChatInviteRequest(value))
        if isinstance(result, ChatInviteAlready):
            chat = result.chat
            chat_type = _classify_entity(chat)
            return {
                "raw_link": raw_link,
                "status": "active",
                "accessible": True,
                "title": getattr(chat, "title", None),
                "chat_type": chat_type,
                "username": getattr(chat, "username", None),
                "chat_id": str(chat.id),
                "participants_count": getattr(chat, "participants_count", None),
            }

        # ChatInvite — valid invite, account has not joined yet.
        chat_type = "megagroup" if result.megagroup else ("channel" if result.channel else "group")
        request_needed = bool(result.request_needed)
        return {
            "raw_link": raw_link,
            "status": "private" if request_needed else "active",
            "accessible": not request_needed,
            "title": result.title,
            "chat_type": chat_type,
            "username": None,
            "chat_id": None,
            "participants_count": result.participants_count,
            "reason": "가입 승인이 필요한 그룹/채널입니다." if request_needed else None,
        }

    except FloodWaitError as exc:
        return {
            "raw_link": raw_link,
            "status": "flood_wait",
            "accessible": False,
            "reason": f"텔레그램 속도 제한: {exc.seconds}초 후 다시 시도해주세요.",
        }
    except (UsernameNotOccupiedError, UsernameInvalidError, InviteHashInvalidError, InviteHashExpiredError):
        return {
            "raw_link": raw_link,
            "status": "dead",
            "accessible": False,
            "reason": "존재하지 않거나 만료된 링크입니다.",
        }
    except ChannelPrivateError:
        return {
            "raw_link": raw_link,
            "status": "private",
            "accessible": False,
            "reason": "비공개 채널/그룹이라 접근할 수 없습니다.",
        }
    except RPCError as exc:
        logger.warning("link_inspect_rpc_error", link=raw_link, error=str(exc))
        return {
            "raw_link": raw_link,
            "status": "error",
            "accessible": False,
            "reason": "텔레그램에서 요청을 처리할 수 없습니다.",
        }
    except Exception as exc:
        logger.warning("link_inspect_unexpected_error", link=raw_link, error=str(exc))
        return {
            "raw_link": raw_link,
            "status": "error",
            "accessible": False,
            "reason": "알 수 없는 오류가 발생했습니다.",
        }


async def inspect_links(account: Account, links: list[str]) -> tuple[list[dict], int]:
    client = await get_authorized_client(account)
    deduped, duplicates_removed = _dedupe_links(links)

    semaphore = asyncio.Semaphore(10)

    async def _inspect_one_with_semaphore(link: str) -> dict:
        async with semaphore:
            return await _inspect_one(client, link)

    items = await asyncio.gather(*[_inspect_one_with_semaphore(link) for link in deduped])

    return items, duplicates_removed


async def join_selected_links(account: Account, targets: list[LinkJoinTarget]) -> list[dict]:
    """Join the links a user selected from an inspection result.

    Username-based links join via get_entity + JoinChannelRequest, same as
    group_search_service.join_selected_groups. Invite-hash links join via
    ImportChatInviteRequest directly, since CheckChatInviteRequest during
    inspection never resolves a chat_id/username for a not-yet-joined invite.
    Both paths share group_search's GroupJoinLog audit trail and MAX_DAILY_JOINS
    budget — the two entry points intentionally draw from one daily counter.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    from app.database import async_session_maker

    client = await get_authorized_client(account)
    results: list[dict] = []

    async with async_session_maker() as db:
        joined_today = await group_search_crud.count_today_joins(db, account.id)
        remaining = MAX_DAILY_JOINS - joined_today
        if remaining <= 0:
            raise DailyJoinLimitExceededError(f"일일 가입 한도 초과 (최대 {MAX_DAILY_JOINS}회)")

        to_process = targets[:remaining]

        for target in to_process:
            success = False
            error_msg = None
            chat_id: str | None = None
            username: str | None = None
            try:
                kind, value = parse_telegram_link(target.raw_link)
                if kind == "username":
                    entity = await client.get_entity(value)
                    username = value
                    chat_id = str(entity.id)
                    await client(JoinChannelRequest(entity))
                elif kind == "invite":
                    updates = await client(ImportChatInviteRequest(value))
                    joined_chat = updates.chats[0] if getattr(updates, "chats", None) else None
                    chat_id = str(joined_chat.id) if joined_chat is not None else None
                else:
                    raise ValueError("유효하지 않은 링크입니다.")
                success = True
                logger.info("link_inspector_joined", account_id=account.id, title=target.title)
            except Exception as exc:
                error_msg = str(exc)
                logger.warning("link_inspector_join_failed", account_id=account.id, title=target.title, error=error_msg)

            await group_search_crud.create_join_log(
                db,
                account_id=account.id,
                chat_id=chat_id or "",
                title=target.title,
                username=username,
                keyword=JOIN_LOG_SOURCE,
                success=success,
                error_message=error_msg,
            )
            results.append({
                "chat_id": chat_id,
                "title": target.title,
                "success": success,
                "error": error_msg,
            })

    return results
