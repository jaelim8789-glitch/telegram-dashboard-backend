from app.services.ai_think_mode_heuristics import should_skip_think_mode


def test_skips_plain_greeting():
    assert should_skip_think_mode("안녕하세요") is True


def test_skips_thanks():
    assert should_skip_think_mode("감사합니다!") is True


def test_does_not_skip_short_question():
    # "왜요?" -- short, but a real question. Must not be skipped.
    assert should_skip_think_mode("왜요?") is False


def test_does_not_skip_question_mark_only():
    assert should_skip_think_mode("이거 맞아요?") is False


def test_does_not_skip_help_seeking_keyword():
    assert should_skip_think_mode("이유가 뭔가요") is False


def test_does_not_skip_long_message():
    long_msg = "이것은 스무 글자를 훌쩍 넘는 긴 문장입니다 정말로 깁니다"
    assert should_skip_think_mode(long_msg) is False


def test_empty_message_not_skipped():
    assert should_skip_think_mode("") is False
    assert should_skip_think_mode("   ") is False


def test_skips_short_english_greeting():
    assert should_skip_think_mode("thanks!") is True


def test_does_not_skip_english_question():
    assert should_skip_think_mode("why not?") is False
