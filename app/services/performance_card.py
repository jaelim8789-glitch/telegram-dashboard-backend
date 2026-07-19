"""Monthly performance card — a shareable PNG summarizing a tenant's delivery
stats, generated with Pillow so users can screenshot-free share their results
(e.g. in a Telegram chat or on social media) as a growth/referral hook.

Reuses delivery_analytics.get_logical_summary for the numbers (recipients,
success rate) — no new data model, just a rendering layer on top of the
existing analytics.
"""

import io
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from app.api.deps import Identity
from app.config import settings
from app.database import async_session_maker
from app.models.tenant import Tenant
from app.services.delivery_analytics import get_logical_summary

CARD_WIDTH = 1080
CARD_HEIGHT = 1080

_BG_TOP = (30, 41, 59)      # slate-800
_BG_BOTTOM = (15, 23, 42)   # slate-900
_ACCENT = (56, 189, 248)    # sky-400
_WHITE = (248, 250, 252)
_MUTED = (148, 163, 184)    # slate-400

# Debian's fonts-nanum package (installed in the runtime image, see Dockerfile)
# — the only Korean-capable TTF we can rely on being present in the container.
# Only NanumGothic.ttf and NanumGothicBold.ttf actually ship in that package
# (no ExtraBold variant exists).
_FONT_BOLD = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [_FONT_BOLD, _FONT_REGULAR] if bold else [_FONT_REGULAR, _FONT_BOLD]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _vertical_gradient(width: int, height: int, top: tuple, bottom: tuple) -> Image.Image:
    base = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(base)
    for y in range(height):
        t = y / max(height - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    return base


def _centered_text(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


async def generate_performance_card(identity: Identity, days: int = 30) -> bytes:
    """Render a tenant's `days`-window performance as a PNG card. Returns PNG bytes."""
    summary = await get_logical_summary(identity, days=days)

    tenant_name = ""
    if identity.tenant_id:
        async with async_session_maker() as db:
            tenant = await db.get(Tenant, identity.tenant_id)
            if tenant is not None:
                tenant_name = tenant.name or ""

    return _render_card(tenant_name, days, summary)


def _render_card(tenant_name: str, days: int, summary) -> bytes:
    """Pure rendering step, split out from generate_performance_card so it can
    be exercised in tests without a database."""
    img = _vertical_gradient(CARD_WIDTH, CARD_HEIGHT, _BG_TOP, _BG_BOTTOM)
    draw = ImageDraw.Draw(img)
    cx = CARD_WIDTH // 2

    font_brand = _load_font(40, bold=True)
    font_label = _load_font(32)
    font_number = _load_font(140, bold=True)
    font_sub = _load_font(30)
    font_footer = _load_font(26)

    _centered_text(draw, cx, 70, "TeleMon", font_brand, _ACCENT)

    period_label = f"최근 {days}일 성과" if not tenant_name else f"{tenant_name} · 최근 {days}일 성과"
    _centered_text(draw, cx, 150, period_label, font_label, _MUTED)

    draw.line([(120, 240), (CARD_WIDTH - 120, 240)], fill=(51, 65, 85), width=2)

    _centered_text(draw, cx, 300, "총 발송", font_sub, _MUTED)
    _centered_text(draw, cx, 345, f"{summary.total_recipients:,}", font_number, _WHITE)

    _centered_text(draw, cx, 560, "성공률", font_sub, _MUTED)
    _centered_text(draw, cx, 605, f"{summary.success_rate:.1f}%", font_number, _ACCENT)

    col_y = 850
    _centered_text(draw, cx - 220, col_y, "성공", font_sub, _MUTED)
    _centered_text(draw, cx - 220, col_y + 45, f"{summary.successful:,}", font_label, _WHITE)
    _centered_text(draw, cx + 220, col_y, "실패", font_sub, _MUTED)
    _centered_text(draw, cx + 220, col_y + 45, f"{summary.failed:,}", font_label, _WHITE)

    draw.line([(120, CARD_HEIGHT - 130), (CARD_WIDTH - 120, CARD_HEIGHT - 130)], fill=(51, 65, 85), width=2)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    handle = f"t.me/{settings.telegram_bot_username}" if settings.telegram_bot_username else "TeleMon"
    _centered_text(draw, cx, CARD_HEIGHT - 100, f"{handle} · {generated}", font_footer, _MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
