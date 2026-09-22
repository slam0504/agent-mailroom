import hashlib
import time

import pytest
from fastapi.testclient import TestClient
from test_bat_poc import tls_bat_peer as tls_bat_peer

from agent_mailroom.api import create_app
from agent_mailroom.models import CreateRoomRequest, JoinRequest
from agent_mailroom.notifications import BatConnectionSettings
from agent_mailroom.store import Store


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Expected notification state was not reached")


@pytest.mark.parametrize(
    "preset,runtime,stop_before_reply",
    [
        ("claude-code", "claude", False),
        ("codex-agent", "claude", False),
        ("codex-agent", "codex", False),
        ("codex-agent", "codex", True),
        ("claude-code", "claude", True),
    ],
)
def test_http_mail_background_worker_tls_notification_and_member_ack(
    database, tls_bat_peer, preset, runtime, stop_before_reply
):
    peer, url, fingerprint = tls_bat_peer
    peer.preset = preset
    peer.fail_send = stop_before_reply
    if preset == "codex-agent":
        peer.meta.update(codexSandboxMode="workspace-write", codexApprovalPolicy="on-request")
        peer.result = {"ok": True}
    a, b = "a" * 43, "b" * 43
    store = Store(database)
    store.initialize()
    rid = store.create_room(
        hashlib.sha256(a.encode()).hexdigest(),
        CreateRoomRequest(member_name="sender", workspace="/workspace"),
    ).room.room_id
    member_id = hashlib.sha256(b.encode()).hexdigest()
    store.join(rid, member_id, JoinRequest(member_name="claude"))
    store.configure_binding(rid, "claude", "default", "bat-id", "/workspace", runtime)
    settings = BatConnectionSettings(url=url, fingerprint=fingerprint, token="test-token")
    recipient = {"Authorization": f"Bearer {b}"}
    membership = f"/rooms/{rid}/membership"
    with TestClient(create_app(database, settings), base_url="http://127.0.0.1") as api:

        def status():
            return api.get(membership + "/notifications", headers=recipient).json()

        assert status()["worker_enabled"] is True
        message = api.post(
            f"/rooms/{rid}/messages",
            headers={"Authorization": f"Bearer {a}"},
            json={"to": "claude", "text": "private peer content", "request_id": "q1"},
        ).json()
        mid = message["message_id"]
        if stop_before_reply:
            wait_for(
                lambda: (
                    status()["pending_notification"]
                    and any(f.get("channel") == "claude:send-message" for f in peer.sent)
                )
            )
            assert status()["pending_notification"]["state"] == "submitting"
            # Context exit must close the socket, not wait for the 330 second RPC timeout.
        elif preset == "codex-agent" and runtime == "claude":
            wait_for(lambda: status()["binding"]["last_error"])
            assert not any(f.get("channel") == "claude:send-message" for f in peer.sent)
            assert status()["pending_notification"] is None
        else:
            verify_accepted(api, status, peer, rid, mid, recipient, membership)
    if stop_before_reply:
        assert (
            Store(database).notification_status(rid, member_id)["pending_notification"]["state"]
            == "unknown"
        )
    assert b.encode() not in database.read_bytes()
    assert b"test-token" not in database.read_bytes()


def verify_accepted(api, status, peer, rid, mid, recipient, membership):
    wait_for(
        lambda: (
            status()["pending_notification"]
            and status()["pending_notification"]["state"] == "accepted"
        )
    )
    frames = [f for f in peer.sent if f.get("channel") == "claude:send-message"]
    assert len(frames) == 1
    assert frames[0]["contextId"] == "pc-one"
    assert frames[0]["params"]["sessionId"] == "bat-id"
    assert f"message_id={mid}" in frames[0]["params"]["prompt"]
    assert "private peer content" not in frames[0]["params"]["prompt"]
    # Simulate Claude's MCP-backed read/ack. Model behavior itself is a live acceptance gate.
    read = api.get(f"/rooms/{rid}/messages/{mid}", headers=recipient).json()
    assert read["text"] == "private peer content" and read["acknowledged_at"] is None
    assert (
        api.put(membership + "/state", headers=recipient, json={"state": "paused"}).json()["state"]
        == "paused"
    )
    assert api.post(f"/rooms/{rid}/messages/{mid}/ack", headers=recipient).json()["acknowledged_at"]
    assert status()["pending_notification"] is None
