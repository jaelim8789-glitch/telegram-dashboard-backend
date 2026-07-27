"""Fortune Assistant  AI based daily fortune service."""

import hashlib
import random
from datetime import date, datetime, timezone, timedelta
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_DAILY_FORTUNE_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_STARTUP_DATE = date.today()


def _birth_key(birth_date: str | None) -> str:
    """Generate cache key from birth date + today."""
    if birth_date:
        birth_dt = datetime.strptime(birth_date, "%Y-%m-%d").date()
        base = f"{birth_dt.isoformat()}:{date.today().isoformat()}"
    else:
        base = date.today().isoformat()
    return hashlib.md5(base.encode()).hexdigest()


def _seeded_random(birth_date: str | None, salt: str = "") -> random.Random:
    seed = _birth_key(birth_date) + salt
    return random.Random(int(hashlib.md5(seed.encode()).hexdigest(), 16) % (2**31))


def _lucky_numbers(rng: random.Random) -> list[int]:
    return [rng.randint(1, 99) for _ in range(3)]


def _lucky_colors(rng: random.Random) -> list[str]:
    palette = ["Red", "Blue", "Green", "Gold", "Silver",
               "Purple", "Orange", "White", "Black", "Pink",
               "Teal", "Coral"]
    return rng.sample(palette, 2)


def _fortune_score(rng: random.Random) -> dict[str, int]:
    return {
        "relationships": rng.randint(40, 100),
        "wealth": rng.randint(30, 100),
        "health": rng.randint(50, 100),
        "business": rng.randint(40, 100),
        "luck": rng.randint(45, 100),
    }


def _zodiac_sign(birth_date: str | None) -> str:
    if not birth_date:
        return "Aquarius"
    try:
        bd = datetime.strptime(birth_date, "%Y-%m-%d").date()
        month, day = bd.month, bd.day
        signs = [
            (1, 20, "Capricorn"), (2, 19, "Aquarius"), (3, 21, "Pisces"),
            (4, 20, "Aries"), (5, 21, "Taurus"), (6, 22, "Gemini"),
            (7, 23, "Cancer"), (8, 23, "Leo"), (9, 23, "Virgo"),
            (10, 23, "Libra"), (11, 23, "Scorpio"), (12, 22, "Sagittarius"),
        ]
        for m, d, sign in signs:
            if (month == m and day >= d) or (month == m + 1 and day < d):
                return sign
            if month == 1 and day < 20:
                return "Capricorn"
        return ""
    except ValueError:
        return ""


def _today_broadcast_advice(rng: random.Random) -> dict[str, str]:
    hours = list(range(6, 23))
    rng.shuffle(hours)
    return {
        "broadcast_best_time": f"{hours[0]:02d}:{rng.randint(0, 59):02d}",
        "group_engage_time": f"{hours[1]:02d}:{rng.randint(0, 59):02d}",
        "reply_peak_time": f"{hours[2]:02d}:{rng.randint(0, 59):02d}",
    }


def _lucky_keywords(rng: random.Random) -> list[str]:
    pool = [
        "Innovation", "Connection", "Growth", "Focus", "Harmony",
        "Opportunity", "Courage", "Wisdom", "Patience", "Action",
        "Collaboration", "Creativity", "Balance", "Network", "Strategy",
    ]
    return rng.sample(pool, 3)


def _avoid_list(rng: random.Random) -> list[str]:
    pool = [
        "Avoid hasty decisions",
        "Avoid overcommitting",
        "Avoid negative comparisons",
        "Avoid procrastination",
        "Avoid unnecessary risks",
    ]
    return rng.sample(pool, 3)


def _core_missions(rng: random.Random) -> list[str]:
    pool = [
        "Complete 1 important task first",
        "Reach out to a key contact",
        "Review and refine your strategy",
        "Take time to rest",
        "Organize your priorities",
    ]
    return rng.sample(pool, 3)


def _weekly_outlook(rng: random.Random) -> dict[str, Any]:
    return {
        "trend": rng.choice(["Rising", "Steady", "Challenging", "Promising", "Unstable"]),
        "focus": rng.choice(["Relationships", "Planning", "Reflection",
                             "Execution", "Exploration"]),
        "risk": rng.choice(["Overwork", "Distraction", "Miscommunication",
                            "Rash decisions", "Burnout"]),
    }


def _monthly_flow(rng: random.Random) -> dict[str, Any]:
    return {
        "overall_mood": rng.choice(["Energetic", "Reflective", "Growth-focused",
                                     "Cautious", "Ambitious", "Balanced"]),
        "peak_week": rng.randint(1, 4),
        "opportunity": rng.choice([
            "New partnership or collaboration",
            "Skill development opportunity",
            "Financial planning window",
            "Network expansion phase",
            "Creative / strategic breakthrough",
        ]),
    }


async def get_daily_fortune(
    tenant_id: str,
    birth_date: str | None = None,
) -> dict[str, Any]:
    """Generate and return daily fortune for the user."""

    cache_key = _birth_key(birth_date)
    cached = _DAILY_FORTUNE_CACHE.get(tenant_id)
    if cached and cached[0] == cache_key:
        return cached[1]

    rng = _seeded_random(birth_date)
    zodiac = _zodiac_sign(birth_date)
    scores = _fortune_score(rng)
    advice = _today_broadcast_advice(rng)
    keywords = _lucky_keywords(rng)
    avoids = _avoid_list(rng)
    missions = _core_missions(rng)
    weekly = _weekly_outlook(rng)
    monthly = _monthly_flow(rng)
    numbers = _lucky_numbers(rng)
    colors = _lucky_colors(rng)

    # Calculate overall grade
    total = sum(scores.values()) // len(scores)
    if total >= 85:
        summary = "Excellent day ahead! Everything aligns in your favor."
        grade = "Excellent (A)"
    elif total >= 70:
        summary = "Great day! Opportunities are around you."
        grade = "Great (B)"
    elif total >= 55:
        summary = "Decent day. Stay mindful and focused."
        grade = "Fair (C)"
    else:
        summary = "Tough day. Take things slow and be cautious."
        grade = "Challenging (D)"

    result = {
        "date": date.today().isoformat(),
        "zodiac": zodiac,
        "grade": grade,
        "summary": summary,
        "scores": scores,
        "overall_score": total,
        "advice": advice,
        "lucky_keywords": keywords,
        "avoid_today": avoids,
        "core_missions": missions,
        "weekly": weekly,
        "monthly": monthly,
        "lucky_numbers": numbers,
        "lucky_colors": colors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    _DAILY_FORTUNE_CACHE[tenant_id] = (cache_key, result)
    return result
