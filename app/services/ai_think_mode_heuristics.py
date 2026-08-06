"""Heuristic to auto-skip "think mode" for trivial short messages.

Surfacing the model's reasoning pass costs latency and (per
ai_chat_v2_service._store_usage's char-based credit accounting) credits even
when think_mode is off the model always reasons internally, but showing that
reasoning to the user is what think_mode gates. For a one-off "감사합니다"
there's nothing useful to reason about, so we force think_mode off
server-side regardless of what the frontend toggle sent, saving the
reasoning-stream overhead on those turns.

Deliberately loose: false negatives (an actual short question slipping
through with think_mode still on) are cheap -- the user just sees reasoning
they didn't need. False positives (killing think_mode on a real short
question) are the failure to avoid, so the keyword/question-mark checks below
err on the side of leaving think_mode alone.
"""

from __future__ import annotations

import re

# Any of these appearing anywhere in the message means "this might actually
# need reasoning" -- don't skip.
_HELP_SEEKING_PATTERN = re.compile(
    r"[?？]|"
    r"왜|어떻게|어떡|뭐|무엇|뭔가|알려|설명|추천|비교|분석|도와|도움|"
    r"방법|이유|차이|얼마|언제|어디|누구|어느|가능|해줘|해주세요|"
    r"how|why|what|when|where|which|who|explain|help|recommend|compare",
    re.IGNORECASE,
)

# Short greeting/acknowledgement messages that never warrant a reasoning pass.
_MAX_SKIP_LENGTH = 20


def should_skip_think_mode(message: str) -> bool:
    """Return True if think_mode should be forced off for this message.

    Loose heuristic: short message (< 20 chars after trimming) with no
    question mark and none of the help-seeking keywords/patterns above.
    """
    text = (message or "").strip()
    if not text or len(text) >= _MAX_SKIP_LENGTH:
        return False
    if _HELP_SEEKING_PATTERN.search(text):
        return False
    return True
