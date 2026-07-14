"""Regression tests for POST /api/accounts/{account_id}/reply-macros.

Production symptom: the frontend switched to sending multipart/form-data
(so a file could be attached), but the endpoint only ever accepted a JSON
body (`payload: ReplyMacroCreate`) — every create-with-file attempt 422'd,
and the file was silently dropped even when the request happened to parse.
"""

import io

import pytest


async def _create_account(client, phone="+821099990000"):
    res = await client.post("/api/accounts", json={"phone": phone, "name": "매크로 테스트 계정"})
    assert res.status_code == 201
    return res.json()["id"]


def _macro_form(target_chats=None, name="테스트 매크로", message_content="안녕하세요"):
    import json

    return {
        "name": name,
        "target_chats": json.dumps(target_chats or ["-100111"]),
        "message_content": message_content,
        "schedule_type": "interval",
        "interval_hours": "24",
        "fixed_time": "",
        "max_sends_per_day": "10",
        "is_active": "true",
    }


@pytest.mark.asyncio
async def test_create_reply_macro_without_file(client):
    """The no-attachment path must keep working now that the endpoint is
    multipart-only (frontend always sends FormData, with or without a file)."""
    account_id = await _create_account(client)
    res = await client.post(f"/api/accounts/{account_id}/reply-macros", data=_macro_form())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "테스트 매크로"
    assert body["media_path"] is None


@pytest.mark.asyncio
async def test_create_reply_macro_with_file_persists_media_path(client):
    account_id = await _create_account(client)
    files = {"file": ("attachment.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 32), "image/png")}
    res = await client.post(
        f"/api/accounts/{account_id}/reply-macros", data=_macro_form(name="파일첨부 매크로"), files=files
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["media_path"] is not None
    assert body["media_path"].endswith(".png")


@pytest.mark.asyncio
async def test_create_reply_macro_rejects_disallowed_file_type(client):
    account_id = await _create_account(client)
    files = {"file": ("payload.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    res = await client.post(f"/api/accounts/{account_id}/reply-macros", data=_macro_form(), files=files)
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_create_reply_macro_invalid_target_chats_json_is_422(client):
    account_id = await _create_account(client)
    data = _macro_form()
    data["target_chats"] = "not-json"
    res = await client.post(f"/api/accounts/{account_id}/reply-macros", data=data)
    assert res.status_code == 422
