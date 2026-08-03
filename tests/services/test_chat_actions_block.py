"""Unit tests for block/unblock chat actions, mocking the Telethon client.

These verify the code path against Telethon's real API surface (raw MTProto
BlockRequest/UnblockRequest via client.__call__) without needing a live
Telegram session.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat_actions import block_user, unblock_user
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest


@pytest.mark.asyncio
async def test_block_user_calls_block_request():
    client = AsyncMock()
    fake_entity = MagicMock(name="entity")
    client.get_entity = AsyncMock(return_value=fake_entity)

    await block_user(client, 12345)

    client.get_entity.assert_awaited_once_with(12345)
    client.assert_awaited_once()
    (request,), _ = client.call_args
    assert isinstance(request, BlockRequest)
    assert request.id is fake_entity


@pytest.mark.asyncio
async def test_unblock_user_calls_unblock_request():
    client = AsyncMock()
    fake_entity = MagicMock(name="entity")
    client.get_entity = AsyncMock(return_value=fake_entity)

    await unblock_user(client, 12345)

    client.get_entity.assert_awaited_once_with(12345)
    client.assert_awaited_once()
    (request,), _ = client.call_args
    assert isinstance(request, UnblockRequest)
    assert request.id is fake_entity
