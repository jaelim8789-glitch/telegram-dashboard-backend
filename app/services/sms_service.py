import asyncio
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class SmsSendError(Exception):
    pass


async def send_verification_sms(phone: str, code: str) -> None:
    provider = settings.sms_provider
    if provider == "console":
        # No external call, no cost — logs the code so it can be read straight out of
        # `docker compose logs` / the terminal during local development or testing.
        logger.info("sms_code_console", phone=phone, code=code)
        return
    if provider == "twilio":
        await _send_via_twilio(phone, code)
        return
    if provider == "coolsms":
        await _send_via_coolsms(phone, code)
        return
    raise SmsSendError(f"알 수 없는 SMS_PROVIDER 설정입니다: {provider!r} (console, twilio, coolsms 중 하나여야 합니다)")


def _message_text(code: str) -> str:
    return f"[Telegram Dashboard] 인증번호: {code} (5분 이내 입력)"


async def _send_via_twilio(phone: str, code: str) -> None:
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number):
        raise SmsSendError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER가 설정되지 않았습니다.")

    from twilio.base.exceptions import TwilioRestException  # lazy: only needed for this provider
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        # Twilio's client is synchronous; run it off the event loop so one SMS send
        # doesn't block the whole FastAPI process.
        await asyncio.to_thread(
            client.messages.create, body=_message_text(code), from_=settings.twilio_phone_number, to=phone
        )
    except TwilioRestException as exc:
        raise SmsSendError(f"Twilio SMS 발송 실패: {exc}") from exc


async def _send_via_coolsms(phone: str, code: str) -> None:
    if not (settings.coolsms_api_key and settings.coolsms_api_secret and settings.coolsms_phone_number):
        raise SmsSendError("COOLSMS_API_KEY / COOLSMS_API_SECRET / COOLSMS_PHONE_NUMBER가 설정되지 않았습니다.")

    # Coolsms v4 HMAC auth: signature = HMAC-SHA256(secret, date + salt).
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    salt = secrets.token_hex(16)
    signature = hmac.new(
        settings.coolsms_api_secret.encode("utf-8"), (date + salt).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "Authorization": (
            f"HMAC-SHA256 apiKey={settings.coolsms_api_key}, date={date}, salt={salt}, signature={signature}"
        ),
        "Content-Type": "application/json",
    }
    payload = {
        "message": {
            "to": phone.lstrip("+"),
            "from": settings.coolsms_phone_number,
            "text": _message_text(code),
        }
    }

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(
                "https://api.coolsms.co.kr/messages/v4/send", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SmsSendError(f"Coolsms SMS 발송 실패: {exc}") from exc

    if response.status_code >= 400:
        raise SmsSendError(f"Coolsms SMS 발송 실패 ({response.status_code}): {response.text}")
