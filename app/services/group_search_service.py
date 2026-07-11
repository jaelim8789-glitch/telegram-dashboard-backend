import re

import httpx
from bs4 import BeautifulSoup
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import Channel, Chat

from app.core.limits import MAX_DAILY_JOINS
from app.core.logging import get_logger
from app.crud import group_search as group_search_crud
from app.database import async_session_maker
from app.models.account import Account
from app.services.telegram_actions import get_authorized_client

logger = get_logger(__name__)

TELEGRAM_WEB_SEARCH_URL = "https://t.me/s/"


class DailyJoinLimitExceededError(Exception):
    pass


async def search_public_groups(account: Account, keyword: str) -> list[dict]:
    """Search Telegram for public groups/channels matching a keyword.

    Uses the Telegram web search (t.me/s?q=keyword) to find public groups,
    then resolves their info via Telethon.

    Returns a list of dicts with keys: chat_id, title, chat_type, username,
    participants_count, about.
    """
    client = await get_authorized_client(account)
    found: list[dict] = []
    seen_ids: set[str] = set()

    # --- Step 1: Scrape t.me/s?q=... ---
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(TELEGRAM_WEB_SEARCH_URL, params={"q": keyword})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for card in soup.select("a.tgme_search_card"):
                href = card.get("href", "")
                if not href:
                    continue
                username_or_hash = href.strip("/")
                if not username_or_hash:
                    continue

                title_el = card.select_one(".tgme_search_card_title")
                title = title_el.get_text(strip=True) if title_el else username_or_hash
                desc_el = card.select_one(".tgme_search_card_description")
                about = desc_el.get_text(strip=True) if desc_el else None

                members_el = card.select_one(".tgme_search_card_members")
                members_text = members_el.get_text(strip=True) if members_el else None
                participants = _parse_member_count(members_text)

                found.append({
                    "username": username_or_hash,
                    "title": title,
                    "about": about,
                    "participants_count": participants,
                })
    except Exception as exc:
        logger.warning("web_search_scrape_failed", keyword=keyword, error=str(exc))

    # --- Step 2: Resolve each via Telethon to get chat_id ---
    resolved_results: list[dict] = []
    for entry in found:
        try:
            entity = await client.get_entity(entry["username"])
            chat_id = str(entity.id)

            if chat_id in seen_ids:
                continue
            seen_ids.add(chat_id)

            chat_type = _classify_entity(entity)
            if chat_type is None:
                continue

            resolved_results.append({
                "chat_id": chat_id,
                "title": entry["title"],
                "chat_type": chat_type,
                "username": entry["username"],
                "participants_count": getattr(entity, "participants_count", entry.get("participants_count")),
                "about": entry.get("about"),
            })
        except Exception as exc:
            logger.debug("resolve_entity_failed", username=entry["username"], error=str(exc))
            continue

    # --- Step 3: Save to DB ---
    async with async_session_maker() as db:
        await group_search_crud.save_search_results(db, account.id, keyword, resolved_results)

    return resolved_results


async def join_selected_groups(account: Account, result_ids: list[str]) -> list[dict]:
    """Join groups the user has selected from search results.

    Enforces daily join limit. Returns list of join results with status per group.
    """
    client = await get_authorized_client(account)
    results = []

    async with async_session_maker() as db:
        rows = await group_search_crud.get_results_by_ids(db, result_ids)
        # get_results_by_ids matches purely on id, with no account filter — the caller
        # (API router) only verifies tenant ownership of the *first* row's account, so
        # without this filter a result_ids list mixing in another account's rows would
        # have this account's Telegram session join groups it never searched for.
        rows = [row for row in rows if row.account_id == account.id]
        joined_today = await group_search_crud.count_today_joins(db, account.id)
        remaining = MAX_DAILY_JOINS - joined_today

        if remaining <= 0:
            raise DailyJoinLimitExceededError(f"일일 가입 한도 초과 (최대 {MAX_DAILY_JOINS}회)")

        to_process = rows[:remaining]

        for row in to_process:
            success = False
            error_msg = None
            try:
                if row.username:
                    entity = await client.get_entity(row.username)
                else:
                    entity = await client.get_entity(int(row.chat_id))
                await client(JoinChannelRequest(entity))
                success = True
                logger.info("group_joined", account_id=account.id, title=row.title, chat_id=row.chat_id)
            except Exception as exc:
                error_msg = str(exc)
                logger.warning("group_join_failed", account_id=account.id, title=row.title, error=error_msg)

            await group_search_crud.create_join_log(
                db,
                account_id=account.id,
                chat_id=row.chat_id,
                title=row.title,
                username=row.username,
                keyword=row.keyword,
                success=success,
                error_message=error_msg,
            )
            results.append({"chat_id": row.chat_id, "title": row.title, "success": success, "error": error_msg})

        joined_ids = [row.id for row in to_process]
        await group_search_crud.mark_results_joined(db, joined_ids)

    return results


def _classify_entity(entity) -> str | None:
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "megagroup" if entity.megagroup else "channel"
    return None


def _parse_member_count(text: str | None) -> int | None:
    if not text:
        return None
    text = text.replace("\xa0", " ").strip()
    match = re.search(r"([\d\s.,]+)([KkMm])?", text)
    if not match:
        return None
    num_str = match.group(1).replace(" ", "").replace(",", "").replace(".", "")
    try:
        num = int(num_str)
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        num *= 1000
    elif suffix == "m":
        num *= 1000000
    return num
