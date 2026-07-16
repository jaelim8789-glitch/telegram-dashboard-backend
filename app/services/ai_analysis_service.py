"""Shared "analyze this data with DeepSeek and extract anomaly/insight lines"
logic, used by both app.api.ai_assist (on-demand, operator-triggered) and
app.services.ai_ops_service (periodic scheduled report). Extracted so the two
call sites share one system-prompt-building + parsing implementation instead
of duplicating it. Reuses _call_deepseek — no new provider.
"""

from app.services.ai_chat_service import _call_deepseek

_ANOMALY_KEYWORDS = ["이상", "주의", "경고", "위험", "증가", "감소"]

# Shared with app.services.ai_ops_service's periodic report job so both the
# on-demand endpoint and the scheduled job analyze delivery data the same way.
DELIVERY_SYSTEM_PROMPT = (
    "너는 TeleMon 서비스의 AI 운영 분석가야. "
    "전달 분석 데이터를 보고 운영자에게 의미 있는 인사이트를 제공해줘.\n\n"
    "응답 형식:\n"
    "1. 먼저 전반적인 운영 상태를 3-4문장으로 요약\n"
    "2. 발견된 이상 징후(anomalies)를 \";\"로 구분해서 나열\n"
    "3. 각 이상 징후에 대해 원인과 해결 방향을 간단히 제시\n\n"
    "주의:\n"
    "- 한국어로 답변\n"
    "- 구체적인 수치를 포함\n"
    "- 전주 대비 변화가 있다면 강조\n"
    "- 운영자가 바로 조치할 수 있는 액션 아이템 위주로"
)


async def analyze_text_report(
    system_prompt: str, user_prompt: str, *, max_anomalies: int = 5
) -> tuple[str | None, list[str]]:
    """Sends system_prompt + user_prompt to DeepSeek and splits out anomaly/
    insight lines (those containing ";" plus one of _ANOMALY_KEYWORDS) from the
    full report text. Returns (None, []) if the DeepSeek call fails."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    reply = await _call_deepseek(messages)
    if reply is None:
        return None, []

    report = reply.strip()
    anomalies = [
        line.strip()
        for line in report.split("\n")
        if ";" in line and any(kw in line for kw in _ANOMALY_KEYWORDS)
    ]
    return report, anomalies[:max_anomalies]
