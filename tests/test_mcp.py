import asyncio
import json
import sys
import time

import httpx
import pytest
from mcp import Client, StdioServerParameters

from agent_mailroom.identity import open_identity


def bridge(url, state_dir, identity_file=None):
    args = ["-m", "agent_mailroom.cli", "mcp", "--url", url, "--state-dir", str(state_dir)]
    if identity_file is not None:
        args.extend(["--identity-file", str(identity_file)])
    return Client(StdioServerParameters(command=sys.executable, args=args))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_two_stdio_bridges_exchange_mail_and_resume(live_url, tmp_path):
    state = tmp_path / "sessions"
    async with bridge(live_url, state) as claude, bridge(live_url, state) as codex:
        listed = await claude.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "create_room",
            "join_room",
            "reconnect_member",
            "resume_session",
            "list_members",
            "send_message",
            "receive_messages",
            "ack_message",
            "get_message",
            "set_collaboration_state",
            "notification_status",
        }
        a = await call(claude, "create_room", workspace="/work/project", member_name="claude")
        room = a["room"]["room_id"]
        b = await call(codex, "join_room", room_id=room, member_name="codex")
        assert a["member"]["member_id"] != b["member"]["member_id"]
        assert a["session_key"] != b["session_key"]
        assert a["room"] == b["room"]
        auth_a = {"room_id": room, "session_key": a["session_key"]}
        auth_b = {"room_id": room, "session_key": b["session_key"]}
        # A retry with the returned key must not allocate another room or member.
        assert (
            await call(
                claude,
                "create_room",
                workspace="/work/project",
                member_name="claude",
                session_key=a["session_key"],
            )
            == a
        )
        assert await call(codex, "join_room", member_name="codex", **auth_b) == b
        waiting = asyncio.create_task(call(codex, "receive_messages", wait_ms=5000, **auth_b))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        sent = await call(
            claude, "send_message", to="codex", text="請確認資料格式", request_id="q1", **auth_a
        )
        assert (await asyncio.wait_for(waiting, timeout=5))["messages"] == [sent]
        await call(codex, "ack_message", message_id=sent["message_id"], **auth_b)
        assert (await call(claude, "get_message", message_id=sent["message_id"], **auth_a))[
            "acknowledged_at"
        ]
        reply = await call(
            codex,
            "send_message",
            to="claude",
            text="格式已確認",
            request_id="a1",
            reply_to=sent["message_id"],
            **auth_b,
        )
        invalid = await codex.call_tool("receive_messages", {**auth_b, "wait_ms": -1})
        assert invalid.is_error
        missing = await codex.call_tool(
            "join_room", {"room_id": "room_typo", "member_name": "other"}
        )
        assert missing.is_error
        assert "Room does not exist" in str(missing.content)
    # A fresh process resolves the saved session key without per-agent startup configuration.
    async with bridge(live_url, state) as resumed:
        assert await call(resumed, "resume_session", **auth_a) == a
        assert (await call(resumed, "receive_messages", **auth_a))["messages"] == [reply]
        await call(resumed, "ack_message", message_id=reply["message_id"], **auth_a)
        start = time.monotonic()
        assert (await call(resumed, "receive_messages", wait_ms=200, **auth_a))["messages"] == []
        assert 0.15 <= time.monotonic() - start < 3


@pytest.mark.anyio
async def test_collaboration_tools_control_only_the_callers_membership(live_url, tmp_path):
    async with bridge(live_url, tmp_path / "sessions") as client:
        a = await call(client, "create_room", workspace="/workspace", member_name="claude")
        room = a["room"]["room_id"]
        b = await call(client, "join_room", room_id=room, member_name="codex")
        aa = {"room_id": room, "session_key": a["session_key"]}
        bb = {"room_id": room, "session_key": b["session_key"]}
        assert (await call(client, "set_collaboration_state", state="paused", **aa))[
            "state"
        ] == "paused"
        assert (await call(client, "notification_status", **aa))["state"] == "paused"
        assert (await call(client, "notification_status", **bb))["state"] == "active"
        sent = await call(
            client, "send_message", to="claude", text="retained", request_id="while-paused", **bb
        )
        assert (await call(client, "receive_messages", **aa))["messages"] == [sent]
        await call(client, "set_collaboration_state", state="left", **aa)
        denied = await client.call_tool(
            "send_message",
            {
                **bb,
                "to": "claude",
                "text": "new",
                "request_id": "after-leave",
            },
        )
        assert denied.is_error
        assert (await call(client, "get_message", message_id=sent["message_id"], **aa))[
            "text"
        ] == "retained"


@pytest.mark.anyio
async def test_shared_bridge_never_uses_last_registered_identity(live_url, tmp_path):
    async with bridge(live_url, tmp_path / "sessions") as client:
        a = await call(client, "create_room", workspace="same-workspace", member_name="claude")
        room = a["room"]["room_id"]
        b = await call(client, "join_room", room_id=room, member_name="codex")
        c = await call(client, "join_room", room_id=room, member_name="observer")
        aa = {"room_id": room, "session_key": a["session_key"]}
        bb = {"room_id": room, "session_key": b["session_key"]}
        cc = {"room_id": room, "session_key": c["session_key"]}
        no_identity = await client.call_tool("receive_messages", {"room_id": room})
        assert no_identity.is_error
        message = await call(
            client, "send_message", to="codex", text="only for codex", request_id="shared-1", **aa
        )
        assert message["sender"] == "claude"
        inbox_a, inbox_b, inbox_c = await asyncio.gather(
            call(client, "receive_messages", **aa),
            call(client, "receive_messages", **bb),
            call(client, "receive_messages", **cc),
        )
        assert inbox_a["messages"] == inbox_c["messages"] == []
        assert inbox_b["messages"] == [message]
        denied = await client.call_tool("ack_message", {**aa, "message_id": message["message_id"]})
        assert denied.is_error
        listed = await client.call_tool("list_members", aa)
        public = json.dumps(listed.structured_content)
        for registration in (a, b, c):
            assert registration["session_key"] not in public
        unknown_key = await client.call_tool(
            "receive_messages", {"room_id": room, "session_key": "session_" + "0" * 32}
        )
        assert unknown_key.is_error


@pytest.mark.anyio
async def test_legacy_identity_can_be_imported_for_resume(live_url, tmp_path):
    identity_file = tmp_path / "old-identity.json"
    with open_identity(identity_file) as token:
        async with httpx.AsyncClient(trust_env=False) as http:
            result = await http.post(
                live_url + "/rooms",
                headers={"Authorization": f"Bearer {token}"},
                json={"workspace": "/old", "member_name": "old-member"},
            )
            registered = result.json()
    room = registered["room"]["room_id"]
    state = tmp_path / "sessions"
    async with bridge(live_url, state, identity_file) as client:
        restored = await call(client, "resume_session", room_id=room)
        assert restored["member"] == registered["member"]
    async with bridge(live_url, state) as client:
        assert (
            await call(client, "resume_session", room_id=room, session_key=restored["session_key"])
            == restored
        )


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["create_room", "join_room"])
async def test_registration_retry_after_lost_http_response(
    live_url, tmp_path, database, monkeypatch, operation
):
    import re
    import sqlite3

    from agent_mailroom.bridge import create_mcp
    from agent_mailroom.identity import IdentityStore

    server = create_mcp(live_url, IdentityStore(tmp_path / "sessions"))
    async with Client(server) as client:
        arguments = {"workspace": "/work/project", "member_name": "creator"}
        target = "/rooms"
        if operation == "join_room":
            created = await call(client, "create_room", **arguments)
            room = created["room"]["room_id"]
            arguments = {"room_id": room, "member_name": "peer"}
            target = f"/rooms/{room}/members"
        original = httpx.AsyncClient.request
        dropped = False

        async def lose_once(self, method, url, **kwargs):
            nonlocal dropped
            response = await original(self, method, url, **kwargs)
            if not dropped and method == "POST" and url == target:
                assert response.status_code == 200
                dropped = True
                raise httpx.ReadError("simulated response loss", request=response.request)
            return response

        monkeypatch.setattr(httpx.AsyncClient, "request", lose_once)
        failed = await client.call_tool(operation, arguments)
        assert failed.is_error and dropped
        key = re.search(r"session_[a-f0-9]{32}", str(failed.content)).group()
        retried = await call(client, operation, session_key=key, **arguments)
        assert retried["session_key"] == key
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT count(*) FROM rooms").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM members").fetchone()[0] == (
                1 if operation == "create_room" else 2
            )
