"""AI quality regression smoke test.

Runs a fixed set of representative questions through the non-streaming chat
service and reports answer length + structure so prompt/model changes can be
compared before/after. Used to catch "did my change make answers terse again?"

Usage:
    python -m scripts.ai_quality_regression [--json]

Exit code 0 if all answers meet the length floor, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.services.ai_chat_v2_service import _call_deepseek_nonstream

MIN_ANSWER_CHARS = 80
QUESTIONS = [
    "텔레그램 마케팅을 시작하려면 뭐부터 해야 할까요?",
    "파이썬으로 간단한 계산기 코드를 짜줘",
    "이직 고민이 있는데, 좋은 회사를 고르는 기준을 알려줘",
    "하루 30분으로 영어 회화 실력을 늘리는 방법?",
    "안녕하세요",
    "최근 다이어트에 도움이 되는 과학적 방법을 자세히 알려줘",
]


async def run() -> list[dict]:
    results = []
    for q in QUESTIONS:
        try:
            reply, _, _ = await _call_deepseek_nonstream(
                [{
                    "role": "system",
                    "content": (
                        "당신은 한국어로 상세하게 답변하는 전문가입니다. "
                        "일반 질문은 최소 10~15문장, 복잡 질문은 20문장 이상 답하세요. "
                        "한두 줄로 요약하지 마세요."
                    ),
                }, {"role": "user", "content": q}],
                max_tokens=2000,
            )
            length = len(reply or "")
            results.append({
                "question": q[:40],
                "length": length,
                "ok": length >= MIN_ANSWER_CHARS,
            })
            print(f"{'PASS' if length >= MIN_ANSWER_CHARS else 'FAIL'}  [{length:>5}자]  {q[:40]}")
        except Exception as exc:
            results.append({"question": q[:40], "length": 0, "ok": False, "error": str(exc)})
            print(f"ERROR [{q[:40]}]: {exc}")
    return results


async def main() -> int:
    ascii_out = "--json" in sys.argv
    results = await run()
    passed = sum(1 for r in results if r["ok"])
    if ascii_out:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n{passed}/{len(results)} 답변이 최소 길이({MIN_ANSWER_CHARS}자) 충족")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
