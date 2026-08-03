"""Unit tests for block/unblock chat actions, mocking the Telethon client.

These verify the code path against Telethon's real API surface (raw MTProto
BlockRequest/UnblockRequest via client.__call__) without needing a live
Telegram session.

IMPORTANT: the entity fixture is a real `telethon.tl.types.User`, not a bare
MagicMock. An earlier version of this test used a MagicMock and passed
`BlockRequest(id=entity)` directly — which type-checks and *serializes*
without error (TLObject.bytes() doesn't validate against the target type),
but produces the wrong bytes on the wire: it writes out the User's own TL
constructor instead of a proper InputPeer, which Telegram would reject or
misinterpret. A MagicMock hides this completely (`isinstance`/`is` checks
against the mock still pass). Using a real User forces the code through
`telethon.utils.get_input_peer()`'s actual type-cast logic, so a regression
back to passing the raw entity fails loudly here instead of shipping broken.
"""

from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import InputPeerUser, User

from app.services.chat_actions import block_user, unblock_user
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest


def _make_user(user_id: int, access_hash: int) -> User:
    return User(
        id=user_id, access_hash=access_hash,
        is_self=False, contact=False, mutual_contact=False, deleted=False,
        bot=False, bot_chat_history=False, bot_nochats=False, verified=False,
        restricted=False, min=False, bot_inline_geo=False, support=False,
        scam=False, apply_min_photo=False, fake=False, bot_attach_menu=False,
        premium=False, attach_menu_enabled=False, bot_can_edit=False,
        close_friend=False, stories_hidden=False, stories_unavailable=False,
        contact_require_premium=False, bot_business=False,
    )


@pytest.mark.asyncio
async def test_block_user_calls_block_request_with_valid_input_peer():
    client = AsyncMock()
    fake_entity = _make_user(user_id=999, access_hash=12345)
    client.get_entity = AsyncMock(return_value=fake_entity)

    await block_user(client, 12345)

    client.get_entity.assert_awaited_once_with(12345)
    client.assert_awaited_once()
    (request,), _ = client.call_args
    assert isinstance(request, BlockRequest)
    # Must be converted to a real InputPeer — passing the User object
    # directly would serialize the wrong bytes on the wire (see module docstring).
    assert isinstance(request.id, InputPeerUser)
    assert request.id.user_id == 999
    assert request.id.access_hash == 12345
    # Cross-check against Telethon's own conversion for the same entity, so
    # this test fails if get_input_peer's behavior ever changes underneath us.
    from telethon import utils
    assert bytes(request) == bytes(BlockRequest(id=utils.get_input_peer(fake_entity)))


@pytest.mark.asyncio
async def test_unblock_user_calls_unblock_request_with_valid_input_peer():
    client = AsyncMock()
    fake_entity = _make_user(user_id=999, access_hash=12345)
    client.get_entity = AsyncMock(return_value=fake_entity)

    await unblock_user(client, 12345)

    client.get_entity.assert_awaited_once_with(12345)
    client.assert_awaited_once()
    (request,), _ = client.call_args
    assert isinstance(request, UnblockRequest)
    assert isinstance(request.id, InputPeerUser)
    assert request.id.user_id == 999
    assert request.id.access_hash == 12345
