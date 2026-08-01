import asyncio

from app.services.random_reply_service import _build_candidate_pool


class FakeSender:
    def __init__(self, sender_id: str, *, bot: bool = False, is_self: bool = False):
        self.id = sender_id
        self.bot = bot
        self.is_self = is_self


class FakeMessage:
    def __init__(self, sender_id: str, *, text: str = "hello", out: bool = False, msg_id: int = 42, bot: bool = False, is_self: bool = False):
        self.id = msg_id
        self.out = out
        self.text = text
        self._sender = FakeSender(sender_id, bot=bot, is_self=is_self)
        self._sender_name = "sender"

    async def get_sender(self):
        return self._sender


def test_build_candidate_pool_filters_bot_self_and_empty_messages_and_duplicates():
    messages = [
        FakeMessage("1", text="hello"),
        FakeMessage("2", text="", out=False),
        FakeMessage("3", bot=True, text="ignored"),
        FakeMessage("4", is_self=True, text="ignored"),
        FakeMessage("1", text="duplicate-user", out=False),
        FakeMessage("5", text="world"),
    ]

    candidates = asyncio.run(
        _build_candidate_pool(messages, chat_id="chat-1", used_pairs={("chat-1", "1")})
    )

    assert [uid for uid, _ in candidates] == ["5"]


def test_build_candidate_pool_returns_empty_when_everything_is_filtered():
    messages = [
        FakeMessage("1", text="", out=False),
        FakeMessage("2", bot=True, text="skip"),
        FakeMessage("3", is_self=True, text="skip"),
    ]

    candidates = asyncio.run(
        _build_candidate_pool(messages, chat_id="chat-1", used_pairs={("chat-1", "1")})
    )

    assert candidates == []
