from __future__ import annotations

import json
import re
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl import functions
from telethon.tl.types import ChatBannedRights

from app.core.logging import get_logger
from app.services.ai_core_service import call_deepseek

logger = get_logger(__name__)

_SPAM_THRESHOLD = 80
_RULE_ONLY_HIGH_RISK = 92

_BANNED_KEYWORDS = [
    "무료수신", "광고", "선착순", "특가", "할인", "이벤트", "쿠폰", "증정",
    "당첨", "무료", "100%", "확인", "클릭", "지금", "바로", "필독",
    "대출", "투자", "수익", "로또", "복권", "적립", "캐시백", "리워드",
    "airdrop", "usdt", "무료 투자",
]

_SPAM_URL_PATTERNS = [
    re.compile(r"bit\.ly/", re.IGNORECASE),
    re.compile(r"tinyurl\.com/", re.IGNORECASE),
    re.compile(r"goo\.gl/", re.IGNORECASE),
    re.compile(r"shorturl\.at/", re.IGNORECASE),
    re.compile(r"open\.kakao", re.IGNORECASE),
    re.compile(r"t\.me/(joinchat|addstickers)", re.IGNORECASE),
]


@dataclass
class SpamCheckResult:
    score: int
    rule_score: int
    ai_score: int | None
    is_spam: bool
    reasons: list[str]
    action_taken: str | None = None


def _rule_score(message: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    risk = 0
    text = message.strip()
    if not text:
        return 0, reasons

    lower = text.lower()
    keyword_hits = [kw for kw in _BANNED_KEYWORDS if kw.lower() in lower]
    if keyword_hits:
        risk += min(55, len(keyword_hits) * 9)
        reasons.append(f"광고/투자 키워드 {len(keyword_hits)}개")

    urls = re.findall(r"https?://[^\s]+", text, flags=re.IGNORECASE)
    if len(urls) >= 2:
        risk += min(20, len(urls) * 8)
        reasons.append(f"다중 URL {len(urls)}개")
    elif len(urls) == 1:
        risk += 6

    if any(p.search(text) for p in _SPAM_URL_PATTERNS):
        risk += 22
        reasons.append("단축/초대 링크 패턴")

    emoji_count = len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))
    if emoji_count > 5:
        risk += min(12, emoji_count)
        reasons.append(f"과도한 이모지 {emoji_count}개")

    exclamation_count = text.count("!")
    if exclamation_count > 3:
        risk += 5
        reasons.append("느낌표 과다")

    if re.search(r"(.)\1{4,}", text):
        risk += 8
        reasons.append("반복 문자 패턴")

    if re.search(r"[\d,]+(?:원|\$|€|£)", text):
        risk += 10
        reasons.append("금액 직접 표기")

    return max(0, min(100, risk)), reasons


async def _ai_risk_score(message: str, rule_score: int, reasons: list[str]) -> tuple[int | None, str | None]:
    if rule_score < 45:
        return None, None

    system_prompt = (
        "너는 텔레그램 스팸 탐지기다. 메시지의 스팸 위험도를 0~100으로 평가한다. "
        "사기/과장광고/투자유도/피싱/무단홍보 가능성이 높을수록 점수를 높인다. "
        "반드시 JSON으로만 답한다: {\"score\": <0-100 정수>, \"reason\": \"짧은 근거\"}"
    )
    user_prompt = json.dumps(
        {
            "message": message[:1500],
            "rule_score": rule_score,
            "rule_reasons": reasons,
        },
        ensure_ascii=False,
    )
    reply, _, _ = await call_deepseek(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        max_tokens=120,
    )
    if not reply:
        return None, None
    try:
        parsed = json.loads(reply.strip())
        score = int(parsed.get("score", 0))
        score = max(0, min(100, score))
        reason = str(parsed.get("reason", "")).strip() or None
        return score, reason
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None


async def _apply_auto_action(client: TelegramClient, event) -> str:
    action_notes: list[str] = []

    try:
        await event.delete()
        action_notes.append("deleted")
    except Exception as exc:  # noqa: BLE001
        logger.warning("spam_delete_failed", error=str(exc))

    sender_id = getattr(event, "sender_id", None)
    if sender_id is None:
        return ",".join(action_notes) if action_notes else "none"

    try:
        chat = await event.get_chat()
        if getattr(event, "is_private", False):
            await client(functions.contacts.BlockRequest(id=sender_id))
            action_notes.append("blocked_dm")
        else:
            rights = ChatBannedRights(until_date=None, view_messages=True, send_messages=True)
            await client(functions.channels.EditBannedRequest(channel=chat, participant=sender_id, banned_rights=rights))
            action_notes.append("banned_chat")
    except Exception as exc:  # noqa: BLE001
        logger.warning("spam_block_failed", error=str(exc))

    return ",".join(action_notes) if action_notes else "none"


async def inspect_and_moderate_message(client: TelegramClient, event, account_id: str) -> SpamCheckResult:
    text = (event.raw_text or "").strip()
    if not text:
        return SpamCheckResult(score=0, rule_score=0, ai_score=None, is_spam=False, reasons=[])

    rule_score, reasons = _rule_score(text)
    ai_score, ai_reason = await _ai_risk_score(text, rule_score, reasons)
    if ai_reason:
        reasons.append(f"AI 판단: {ai_reason}")

    if ai_score is None:
        final_score = rule_score
    else:
        final_score = int(round(rule_score * 0.6 + ai_score * 0.4))
    final_score = max(0, min(100, final_score))

    auto_action_allowed = (final_score >= _SPAM_THRESHOLD and (ai_score is not None and ai_score >= 70)) or final_score >= _RULE_ONLY_HIGH_RISK
    is_spam = auto_action_allowed

    action_taken = None
    if is_spam:
        action_taken = await _apply_auto_action(client, event)
        logger.warning(
            "spam_auto_moderated",
            account_id=account_id,
            chat_id=getattr(event, "chat_id", None),
            sender_id=getattr(event, "sender_id", None),
            score=final_score,
            rule_score=rule_score,
            ai_score=ai_score,
            action=action_taken,
            reasons="; ".join(reasons[:4]),
        )

    return SpamCheckResult(
        score=final_score,
        rule_score=rule_score,
        ai_score=ai_score,
        is_spam=is_spam,
        reasons=reasons,
        action_taken=action_taken,
    )