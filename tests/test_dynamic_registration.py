import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_bat_poc import tls_bat_peer as tls_bat_peer
from test_notification_integration import wait_for

from agent_mailroom.api import create_app
from agent_mailroom.notifications import BatConnectionSettings
from agent_mailroom.store import Store


def auth(name):
    return {"Authorization": "Bearer " + name * 43}


def target(name="claude-one", runtime="claude"):
    return {"session_id": name, "runtime": runtime, "profile_id": "default"}


def add_target(peer, name, runtime, workspace="/workspace"):
    meta = {"cwd": workspace}
    if runtime == "codex":
        meta.update(codexSandboxMode="workspace-write", codexApprovalPolicy="on-request")
    peer.targets[name] = {
        "terminal": {
            "id": name,
            "cwd": workspace,
            "agentPreset": "claude-code" if runtime == "claude" else "codex-agent",
        },
        "meta": meta,
        "result": {"ok": True, "accepted": True, "queued": False}
        if runtime == "claude"
        else {"ok": True},
    }


@pytest.fixture
def bat(tls_bat_peer):
    peer, url, pin = tls_bat_peer
    for suffix in ("one", "two"):
        add_target(peer, "claude-" + suffix, "claude")
        add_target(peer, "codex-" + suffix, "codex")
    return peer, BatConnectionSettings(url, pin, "test-token")


def status(api, room, name):
    return api.get(f"/rooms/{room}/membership/notifications", headers=auth(name)).json()


def create(api, name="a", bat_target=None):
    result = api.post(
        "/rooms",
        headers=auth(name),
        json={
            "member_name": "claude",
            "workspace": "/workspace",
            "bat": bat_target or target(),
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


def join(api, room, name="b", bat_target=None, member_name="codex"):
    return api.post(
        f"/rooms/{room}/members",
        headers=auth(name),
        json={
            "member_name": member_name,
            "bat": bat_target or target("codex-one", "codex"),
        },
    )


def test_empty_server_accepts_two_collaborations_and_restores_them(database, bat):
    peer, settings = bat
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        assert Store(database).list_bindings() == []
        rooms = []
        for suffix, sender, recipient in [("one", "a", "b"), ("two", "c", "d")]:
            registration = create(api, sender, target("claude-" + suffix))
            room = registration["room"]["room_id"]
            rooms.append((room, sender, recipient))
            assert registration["bat_binding"]["session_id"] == "claude-" + suffix
            joined = join(api, room, recipient, target("codex-" + suffix, "codex"))
            assert joined.status_code == 200, joined.text
            assert status(api, room, sender)["worker_enabled"]
            assert status(api, room, recipient)["worker_enabled"]
            assert len(api.get(f"/rooms/{room}/members", headers=auth(sender)).json()) == 2
        assert rooms[0][0] != rooms[1][0]
        # A failed claim must roll back the joining member along with its binding.
        room = rooms[0][0]
        collision = join(api, room, "e", target(), "impostor")
        assert collision.status_code == 409
        assert len(api.get(f"/rooms/{room}/members", headers=auth("a")).json()) == 2
        for room, sender, recipient in rooms:
            message = api.post(
                f"/rooms/{room}/messages",
                headers=auth(sender),
                json={
                    "to": "codex",
                    "text": "private handoff",
                    "request_id": "first",
                },
            ).json()
            wait_for(
                lambda room=room, recipient=recipient: (
                    (status(api, room, recipient)["pending_notification"] or {}).get("state")
                    == "accepted"
                )
            )
            assert (
                api.post(
                    f"/rooms/{room}/messages/{message['message_id']}/ack", headers=auth(recipient)
                ).status_code
                == 200
            )
            reply = api.post(
                f"/rooms/{room}/messages",
                headers=auth(recipient),
                json={
                    "to": "claude",
                    "text": "done",
                    "request_id": "reply",
                    "reply_to": message["message_id"],
                },
            ).json()
            wait_for(
                lambda room=room, sender=sender: (
                    (status(api, room, sender)["pending_notification"] or {}).get("state")
                    == "accepted"
                )
            )
            assert (
                api.post(
                    f"/rooms/{room}/messages/{reply['message_id']}/ack", headers=auth(sender)
                ).status_code
                == 200
            )
        sent = [f for f in peer.sent if f.get("channel") == "claude:send-message"]
        assert {f["params"]["sessionId"] for f in sent} == set(peer.targets)
        assert len(sent) == 4
        assert all("private handoff" not in f["params"]["prompt"] for f in sent)
    # No targets in startup config: the four persisted registrations are restored.
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        for room, a, b in rooms:
            assert status(api, room, a)["worker_enabled"]
            assert status(api, room, b)["worker_enabled"]
        assert len([f for f in peer.sent if f.get("channel") == "claude:send-message"]) == 4
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        assert not status(api, rooms[0][0], "a")["worker_enabled"]
    assert b"test-token" not in database.read_bytes()


def test_existing_member_binds_rebinds_and_leaves_without_restart(database, bat):
    _, settings = bat
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        room = api.post(
            "/rooms", headers=auth("a"), json={"member_name": "claude", "workspace": "/workspace"}
        ).json()["room"]["room_id"]
        path = f"/rooms/{room}/membership"
        assert status(api, room, "a")["binding"] is None
        assert join(api, room, "a", target(), "claude").status_code == 200
        original = status(api, room, "a")["binding"]
        assert join(api, room, "a", target(), "claude").status_code == 200
        assert status(api, room, "a")["binding"] == original
        # The authenticated member cannot update another member by supplying its name.
        assert join(api, room, "b", target("codex-one", "codex"), "claude").status_code == 409
        assert (
            api.put(path + "/state", headers=auth("a"), json={"state": "paused"}).status_code == 200
        )
        assert join(api, room, "a", target("claude-two"), "claude").status_code == 200
        current = status(api, room, "a")
        assert current["state"] == "paused" and current["worker_enabled"]
        assert current["binding"]["binding_id"] != original["binding_id"]
        assert current["binding"]["session_id"] == "claude-two"
        assert (
            api.put(path + "/state", headers=auth("a"), json={"state": "left"}).status_code == 200
        )
        assert status(api, room, "a")["binding"] is None
        assert not status(api, room, "a")["worker_enabled"]
        assert join(api, room, "a", target(), "claude").status_code == 409
        api.put(path + "/state", headers=auth("a"), json={"state": "active"})
        assert status(api, room, "a")["binding"] is None
        assert join(api, room, "a", target(), "claude").status_code == 200
        assert status(api, room, "a")["worker_enabled"]


def test_disconnected_session_can_reconnect_with_new_identity_and_pending_mail(database, bat):
    import re

    peer, settings = bat
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        registered = create(api, "a", target("claude-one"))
        room = registered["room"]["room_id"]
        assert join(api, room, "b", bat_target=None, member_name="sender").status_code == 200
        message = api.post(
            f"/rooms/{room}/messages",
            headers=auth("b"),
            json={"to": "claude", "text": "continue after reset", "request_id": "handoff"},
        ).json()
        wait_for(
            lambda: (
                (status(api, room, "a")["pending_notification"] or {}).get("state") == "accepted"
            )
        )
        old_prompt = next(
            frame["params"]["prompt"]
            for frame in reversed(peer.sent)
            if frame.get("channel") == "claude:send-message"
            and frame["params"]["sessionId"] == "claude-one"
        )
        old_notice = re.search(r"notification_key=(notice_[a-f0-9]{64})", old_prompt)[1]

        request = {
            "member_name": "claude",
            "bat": target("claude-two"),
        }
        still_connected = api.post(
            f"/rooms/{room}/members/reconnect", headers=auth("n"), json=request
        )
        assert still_connected.status_code == 409
        assert "still connected" in still_connected.json()["detail"]

        del peer.targets["claude-one"]
        replaced = api.post(f"/rooms/{room}/members/reconnect", headers=auth("n"), json=request)
        assert replaced.status_code == 200, replaced.text
        replacement = replaced.json()
        new_member_id = hashlib.sha256(("n" * 43).encode()).hexdigest()
        assert replacement["room"] == registered["room"]
        assert replacement["member"]["member_id"] == new_member_id
        assert replacement["member"]["joined_at"] == registered["member"]["joined_at"]
        assert replacement["bat_binding"]["session_id"] == "claude-two"
        assert replacement["bat_binding"]["binding_id"] != registered["bat_binding"]["binding_id"]

        assert api.get(f"/rooms/{room}/membership", headers=auth("a")).status_code == 403
        assert (
            api.post(
                f"/rooms/{room}/members",
                headers=auth("a"),
                json={"member_name": "stale-session"},
            ).status_code
            == 403
        )
        assert (
            api.get(
                f"/rooms/{room}/messages/{message['message_id']}",
                headers={"Authorization": f"Bearer {old_notice}"},
            ).status_code
            == 403
        )
        assert api.get(f"/rooms/{room}/messages", headers=auth("n")).json()["messages"] == [message]
        assert (
            api.post(f"/rooms/{room}/members/reconnect", headers=auth("n"), json=request).json()
            == replacement
        )
        wait_for(
            lambda: any(
                frame.get("channel") == "claude:send-message"
                and frame["params"]["sessionId"] == "claude-two"
                for frame in peer.sent
            )
        )
        current = status(api, room, "n")
        assert current["worker_enabled"]
        assert current["pending_notification"]["state"] == "accepted"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("runtime", "same profile and runtime"),
        ("profile", "same profile and runtime"),
        ("workspace", "workspace"),
        ("missing", "replacement target"),
    ],
)
def test_reconnect_rejects_changed_identity_boundaries(database, bat, change, expected):
    peer, settings = bat
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        room = create(api, "a", target("claude-one"))["room"]["room_id"]
        del peer.targets["claude-one"]
        requested = target("claude-two")
        body = {"member_name": "claude", "bat": requested}
        if change == "runtime":
            requested["runtime"] = "codex"
        elif change == "profile":
            requested["profile_id"] = "other"
        elif change == "workspace":
            body["workspace"] = "/other"
        elif change == "missing":
            requested["session_id"] = "absent"
        response = api.post(f"/rooms/{room}/members/reconnect", headers=auth("n"), json=body)
        assert response.status_code in {409, 503}
        assert expected in response.json()["detail"]
        assert api.get(f"/rooms/{room}/membership", headers=auth("a")).status_code == 200
        assert api.get(f"/rooms/{room}/membership", headers=auth("n")).status_code == 403


@pytest.mark.parametrize("change", ["runtime", "missing", "cwd", "pin", "disabled"])
def test_failed_validation_does_not_create_room_or_member(database, bat, change):
    peer, settings = bat
    requested = target()
    if change == "runtime":
        requested["runtime"] = "codex"
    elif change == "missing":
        requested["session_id"] = "absent"
    elif change == "cwd":
        peer.targets["claude-one"]["terminal"]["cwd"] = "/other"
        peer.targets["claude-one"]["meta"]["cwd"] = "/other"
    elif change == "pin":
        settings = BatConnectionSettings(settings.url, "0" * 64, settings.token)
    elif change == "disabled":
        settings = None
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        response = api.post(
            "/rooms",
            headers=auth("a"),
            json={
                "member_name": "claude",
                "workspace": "/workspace",
                "bat": requested,
            },
        )
        assert response.status_code == 503
        expected = {
            "runtime": "runtime",
            "missing": "runtime",
            "cwd": "cwd",
            "pin": "fingerprint",
            "disabled": "disabled",
        }
        assert expected[change] in response.json()["detail"].lower()
        with Store(database).connection() as conn:
            for table in ("rooms", "members", "bat_bindings"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        if change in ("pin", "disabled"):
            assert not peer.sent  # No token leaks before certificate verification.


def test_concurrent_terminal_claims_leave_exactly_one_registered_member(database, bat):
    _, settings = bat
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:
        room = create(api)["room"]["room_id"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda name: join(api, room, name, member_name=name), ("b", "c"))
            )
        assert sorted(r.status_code for r in results) == [200, 409]
        members = api.get(f"/rooms/{room}/members", headers=auth("a")).json()
        assert len(members) == 2
        assert len(Store(database).list_bindings()) == 2
        winner = next(r.json()["member_id"] for r in results if r.status_code == 200)
        assert winner in {hashlib.sha256((c * 43).encode()).hexdigest() for c in ("b", "c")}


@pytest.fixture
def dynamic_url(request, monkeypatch, bat):
    import conftest

    _, settings = bat
    monkeypatch.setattr(conftest, "create_app", lambda database: create_app(database, settings))
    return request.getfixturevalue("live_url")


@pytest.mark.anyio
async def test_stdio_mcp_registration_binds_both_agents_and_keeps_private_keys(
    dynamic_url, tmp_path
):
    from test_mcp import bridge, call

    async with bridge(dynamic_url, tmp_path / "sessions") as client:
        first = await call(
            client, "create_room", workspace="/workspace", member_name="claude", bat=target()
        )
        room = first["room"]["room_id"]
        second = await call(
            client, "join_room", room_id=room, member_name="codex", bat=target("codex-one", "codex")
        )
        assert first["session_key"] != second["session_key"]
        assert first["bat_binding"]["runtime"] == "claude"
        assert second["bat_binding"]["runtime"] == "codex"
        assert second["room"] == first["room"]
        for registration in (first, second):
            observed = await call(
                client, "notification_status", room_id=room, session_key=registration["session_key"]
            )
            assert observed["worker_enabled"]
            assert observed["binding"] == registration["bat_binding"]
        retried = await call(
            client,
            "create_room",
            workspace="/workspace",
            member_name="claude",
            bat=target(),
            session_key=first["session_key"],
        )
        assert retried == first
        rebound = await call(
            client,
            "join_room",
            room_id=room,
            member_name="codex",
            session_key=second["session_key"],
            bat=target("codex-two", "codex"),
        )
        assert rebound["session_key"] == second["session_key"]
        assert rebound["member"] == second["member"]
        assert rebound["bat_binding"]["session_id"] == "codex-two"
        peers = await call(client, "list_members", room_id=room, session_key=first["session_key"])
        assert all("bat_binding" not in p and "session_key" not in p for p in peers)
        assert "test-token" not in str((first, second, rebound, peers))


@pytest.mark.anyio
async def test_stdio_mcp_reconnects_member_without_previous_session_key(dynamic_url, bat, tmp_path):
    from test_mcp import bridge, call

    peer, _ = bat
    original_state = tmp_path / "original-state"
    async with bridge(dynamic_url, original_state) as original:
        first = await call(
            original,
            "create_room",
            workspace="/workspace",
            member_name="claude",
            bat=target("claude-one"),
        )
        room = first["room"]["room_id"]

    del peer.targets["claude-one"]
    replacement_state = tmp_path / "replacement-state"
    async with bridge(dynamic_url, replacement_state) as replacement:
        current = await call(
            replacement,
            "reconnect_member",
            room_id=room,
            member_name="claude",
            bat=target("claude-two"),
        )
        assert current["room"] == first["room"]
        assert current["member"]["member_name"] == "claude"
        assert current["member"]["member_id"] != first["member"]["member_id"]
        assert current["bat_binding"]["session_id"] == "claude-two"
        assert current["session_key"] != first["session_key"]
        assert (
            await call(
                replacement,
                "reconnect_member",
                room_id=room,
                member_name="claude",
                bat=target("claude-two"),
                session_key=current["session_key"],
            )
            == current
        )
        assert (
            await call(
                replacement,
                "resume_session",
                room_id=room,
                session_key=current["session_key"],
            )
            == current
        )

    async with bridge(dynamic_url, original_state) as stale:
        denied = await stale.call_tool(
            "resume_session", {"room_id": room, "session_key": first["session_key"]}
        )
        assert denied.is_error


@pytest.mark.anyio
async def test_tls_notifications_work_after_both_agents_lose_runtime_memory(
    dynamic_url, bat, tmp_path
):
    import re

    from test_mcp import bridge, call

    peer, _ = bat
    async with bridge(dynamic_url, tmp_path / "registered-state") as registration_runtime:
        a = await call(
            registration_runtime,
            "create_room",
            workspace="/workspace",
            member_name="claude",
            bat=target(),
        )
        room = a["room"]["room_id"]
        await call(
            registration_runtime,
            "join_room",
            room_id=room,
            member_name="codex",
            bat=target("codex-one", "codex"),
        )
        original = await call(
            registration_runtime,
            "send_message",
            room_id=room,
            session_key=a["session_key"],
            to="codex",
            text="Please finish and reply",
            request_id="wake-after-restart",
        )

    # Registered bridge is gone; the new runtimes receive ONLY their BAT notification.
    def notification(session_id):
        frames = [
            f
            for f in peer.sent
            if f.get("channel") == "claude:send-message" and f["params"]["sessionId"] == session_id
        ]
        return frames[-1]["params"]["prompt"] if frames else None

    codex_prompt = wait_for(lambda: notification("codex-one"))
    codex_key = re.search(r"notification_key=(notice_[a-f0-9]{64})", codex_prompt)[1]
    codex_state = tmp_path / "new-codex-state"
    async with bridge(dynamic_url, codex_state) as restarted_codex:
        args = {"room_id": room, "notification_key": codex_key}
        assert (await call(restarted_codex, "notification_status", **args))["state"] == "active"
        assert (
            await call(restarted_codex, "get_message", message_id=original["message_id"], **args)
        )["text"] == original["text"]
        response = await call(
            restarted_codex,
            "send_message",
            to="claude",
            text="done",
            reply_to=original["message_id"],
            request_id=f"notice-reply-{original['message_id']}",
            **args,
        )
        await call(restarted_codex, "ack_message", message_id=original["message_id"], **args)
    claude_prompt = wait_for(lambda: notification("claude-one"))
    claude_key = re.search(r"notification_key=(notice_[a-f0-9]{64})", claude_prompt)[1]
    claude_state = tmp_path / "new-claude-state"
    async with bridge(dynamic_url, claude_state) as restarted_claude:
        args = {"room_id": room, "notification_key": claude_key}
        assert (
            await call(restarted_claude, "get_message", message_id=response["message_id"], **args)
        )["text"] == "done"
        await call(restarted_claude, "ack_message", message_id=response["message_id"], **args)
    assert not codex_state.exists() and not claude_state.exists()
