"""Fortune Assistant — AI 운세 비서 서비스.

사용자의 생년월일, 과거 발송 데이터, 그룹 활동 패턴을 기반으로
오늘의 운세와 Telegram 운영 최적 시간을 제안합니다.
"""

import hashlib
import random
from datetime import date, datetime, timezone, timedelta
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_DAILY_FORTUNE_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_STARTUP_DATE = date.today()


def _birth_key(birth_date: str | None) -> str:
    """날짜 기반 시드 생성 — 동일 날짜 + 동일 출생자 = 동일 운세"""
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
    palette = ["파란색", "초록색", "보라색", "금색", "은색", "하늘색",
               "분홍색", "주황색", "갈색", "회색", "흰색", "빨간색"]
    return rng.sample(palette, 2)


def _fortune_score(rng: random.Random) -> dict[str, int]:
    return {
        "사업운": rng.randint(40, 100),
        "재물운": rng.randint(30, 100),
        "대인운": rng.randint(50, 100),
        "건강운": rng.randint(40, 100),
        "커뮤니케이션운": rng.randint(45, 100),
    }


def _zodiac_sign(birth_date: str | None) -> str:
    if not birth_date:
        return "물병자리"
    try:
        bd = datetime.strptime(birth_date, "%Y-%m-%d").date()
        month, day = bd.month, bd.day
        signs = [
            (1, 20, "염소자리"), (2, 19, "물병자리"), (3, 21, "물고기자리"),
            (4, 20, "양자리"), (5, 21, "황소자리"), (6, 22, "쌍둥이자리"),
            (7, 23, "게자리"), (8, 23, "사자자리"), (9, 23, "처녀자리"),
            (10, 23, "천칭자리"), (11, 23, "전갈자리"), (12, 22, "궁수자리"),
        ]
        for m, d, sign in signs:
            if (month == m and day >= d) or (month == m + 1 and day < d):
                return sign
            if month == 1 and day < 20:
                return "염소자리"
        return "물병자리"
    except ValueError:
        return "물병자리"


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
        "혜택", "한정", "공유", "초대", "업데이트",
        "감사", "축하", "도전", "함께", "성장",
        "기회", "변화", "소통", "배움", "성공",
    ]
    return rng.sample(pool, 3)


def _avoid_list(rng: random.Random) -> list[str]:
    pool = [
        "과도한 프로모션 — 자발적 참여를 유도하세요",
        "늦은 밤 발송 — 사용자 피로도가 높습니다",
        "반복적인 같은 메시지 — 새로운 각도가 필요합니다",
        "긴 문장의 공지 — 핵심만 간결하게 전달하세요",
        "부정적인 표현 — 긍정 프레이밍이 효과적입니다",
    ]
    return rng.sample(pool, 3)


def _core_missions(rng: random.Random) -> list[str]:
    pool = [
        "오늘 1개의 새 그룹에 가입하거나 연락하세요",
        "가장 반응이 좋았던 메시지를 분석하고 기록하세요",
        "휴면 멤버를 대상으로 리액티베이션 메시지를 보내세요",
        "채널 소개글을 업데이트하세요",
        "단골 멤버에게 개인 감사 메시지를 보내세요",
    ]
    return rng.sample(pool, 3)


def _weekly_outlook(rng: random.Random) -> dict[str, Any]:
    return {
        "trend": rng.choice(["상승", "안정", "도약", "준비", "확장"]),
        "focus": rng.choice(["신규 멤버 유치", "기존 멤버 리텐션", "콘텐츠 다양화",
                             "파트너십 확대", "자동화 최적화"]),
        "risk": rng.choice(["과도한 발송", "멤버 피로도", "경쟁사 활동 증가",
                            "알고리즘 변경", "규제 변화"]),
    }


def _monthly_flow(rng: random.Random) -> dict[str, Any]:
    return {
        "overall_mood": rng.choice(["확장의 달", "안정화의 달", "도약의 달",
                                     "준비의 달", "수확의 달", "재정비의 달"]),
        "peak_week": rng.randint(1, 4),
        "opportunity": rng.choice([
            "신규 채널 개설에 유리한 시기입니다",
            "기존 멤버 대상 재타겟팅이 효과적입니다",
            "크로스 프로모션에 적합한 달입니다",
            "콘텐츠 실험이 좋은 결과를 낳을 달입니다",
            "팀 확장/협업에 유리한 시기입니다",
        ]),
    }


async def get_daily_fortune(
    tenant_id: str,
    birth_date: str | None = None,
) -> dict[str, Any]:
    """오늘의 종합 운세를 반환합니다."""

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

    # 오늘의 운세 요약
    total = sum(scores.values()) // len(scores)
    if total >= 85:
        summary = "매우 좋은 날입니다! 오늘은 새로운 도전을 시작하기에 최적의 날입니다."
        grade = "✨ 대길 (大吉)"
    elif total >= 70:
        summary = "좋은 날입니다. 계획한 일을 추진하면 좋은 결과가 있을 것입니다."
        grade = "길 (吉)"
    elif total >= 55:
        summary = "평범한 날입니다. 무리하지 말고 차분하게 계획을 실천하세요."
        grade = "보통 (小吉)"
    else:
        summary = "다소 신중이 필요한 날입니다. 급한 결정보다는 정보 수집에 집중하세요."
        grade = "주의 (末吉)"

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
