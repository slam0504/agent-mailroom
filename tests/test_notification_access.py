"""Wake-up processing uses a persisted, message-scoped grant, never code-mode memory."""

import hashlib
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_mcp import bridge, call
from test_notifications import Probe, deliver

from agent_mailroom.api import create_app
from agent_mailroom.identity import IdentityStore
from agent_mailroom.models import CreateRoomRequest, JoinRequest, SendRequest
from agent_mailroom.notifications import BatSettings, NotificationWorker
from agent_mailroom.store import Store


@pytest.fixture
def notified(database, tmp_path):
    identities = IdentityStore(tmp_path / "original-identities")
    sender_key, sender_token = identities.create()
    recipient_key, recipient_token = identities.create()
    sender = hashlib.sha256(sender_token.encode()).hexdigest()
    recipient = hashlib.sha256(recipient_token.encode()).hexdigest()
    store = Store(database)
    store.initialize()
    room = store.create_room(
        sender, CreateRoomRequest(member_name="sender", workspace="/workspace")
    ).room.room_id
    store.join(room, recipient, JoinRequest(member_name="recipient"))
    settings = BatSettings(
        room_id=room,
        member_name="recipient",
        profile_id="default",
        session_id="bat-id",
        workspace="/workspace",
        url="wss://127.0.0.1:9876",
        fingerprint="0" * 64,
        token="bat-secret",
    )
    worker = NotificationWorker(store, settings)
    message = store.send(
        room, sender, SendRequest(to="recipient", text="finish then reply", request_id="incoming")
    )
    probe = Probe()
    deliver(store, worker, probe)
    prompt = probe.frames[0]["prompt"]
    key = re.search(r"notification_key=(notice_[a-f0-9]{64})", prompt)[1]
    assert all(
        secret not in prompt
        for secret in (sender_key, recipient_key, sender_token, recipient_token, "bat-secret")
    )
    return dict(
        store=store,
        room=room,
        sender=sender,
        recipient=recipient,
        mid=message.message_id,
        key=key,
        prompt=prompt,
        binding=worker.binding,
        identities=identities,
        recipient_key=recipient_key,
        recipient_token=recipient_token,
    )


def headers(n):
    return {"Authorization": "Bearer " + n["key"]}


def reply(n, request_id="reply"):
    return dict(to="sender", reply_to=n["mid"], text="completed", request_id=request_id)


def test_scoped_read_reply_ack_survive_server_restart_without_member_key(notified, database):
    n = notified
    root = f"/rooms/{n['room']}"
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        status = api.get(root + "/membership/notifications", headers=headers(n))
        assert status.status_code == 200 and status.json()["state"] == "active"
        assert n["key"] not in status.text and "notification_key_hash" not in status.text
        read = api.get(root + f"/messages/{n['mid']}", headers=headers(n))
        assert read.json()["text"] == "finish then reply"
        sent = api.post(root + "/messages", headers=headers(n), json=reply(n))
        assert sent.status_code == 200
        assert sent.json()["sender"] == "recipient"
    # Simulate the reply response being lost along with all runtime memory.
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        retried = api.post(root + "/messages", headers=headers(n), json=reply(n))
        assert retried.json() == sent.json()
        ack = api.post(root + f"/messages/{n['mid']}/ack", headers=headers(n))
        assert ack.status_code == 200 and ack.json()["acknowledged_at"]
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        assert api.post(root + f"/messages/{n['mid']}/ack", headers=headers(n)).json() == ack.json()
        assert api.get(root + f"/messages/{n['mid']}", headers=headers(n)).json()["acknowledged_at"]
        assert api.post(root + "/messages", headers=headers(n), json=reply(n)).json() == sent.json()
        assert (
            api.post(root + "/messages", headers=headers(n), json=reply(n, "new-reply")).status_code
            == 409
        )
    assert n["key"].encode() not in database.read_bytes()
    with n["store"].connection() as conn:
        assert (
            conn.execute("SELECT notification_key_hash FROM notifications").fetchone()[0]
            == hashlib.sha256(n["key"].encode()).hexdigest()
        )


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("get", "/membership", None),
        ("get", "/members", None),
        ("get", "/messages", None),
        ("put", "/membership/state", {"state": "left"}),
        ("post", "/members", {"member_name": "someone"}),
    ],
)
def test_notification_credential_cannot_be_used_as_full_member_credential(
    notified, database, method, suffix, body
):
    n = notified
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        response = api.request(
            method, f"/rooms/{n['room']}" + suffix, headers=headers(n), json=body
        )
        assert response.status_code == 403
        assert (
            api.post(
                "/rooms", headers=headers(n), json={"member_name": "x", "workspace": "/workspace"}
            ).status_code
            == 403
        )


def test_scope_rejects_other_mail_rooms_senders_and_forged_credentials(notified, database):
    n = notified
    other = n["store"].send(
        n["room"], n["sender"], SendRequest(to="recipient", text="other", request_id="other")
    )
    other_room = (
        n["store"]
        .create_room(
            "other-creator", CreateRoomRequest(member_name="other", workspace="/workspace")
        )
        .room.room_id
    )
    root = f"/rooms/{n['room']}"
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        for suffix in (f"/messages/{other.message_id}", f"/messages/{other.message_id}/ack"):
            method = "post" if suffix.endswith("ack") else "get"
            assert api.request(method, root + suffix, headers=headers(n)).status_code == 403
        assert (
            api.get(f"/rooms/{other_room}/membership/notifications", headers=headers(n)).status_code
            == 403
        )
        for changes in ({"reply_to": other.message_id}, {"reply_to": None}, {"to": "recipient"}):
            assert (
                api.post(
                    root + "/messages", headers=headers(n), json={**reply(n), **changes}
                ).status_code
                == 403
            )
        for fake in ("notice_" + "0" * 64, n["recipient"], n["binding"]["binding_id"]):
            assert (
                api.get(
                    root + f"/messages/{n['mid']}", headers={"Authorization": "Bearer " + fake}
                ).status_code
                == 403
            )
        api.post(root + f"/messages/{n['mid']}/ack", headers=headers(n))
        # An old grant must not reveal notification details for newer mail.
        assert (
            api.get(root + "/membership/notifications", headers=headers(n)).json()[
                "pending_notification"
            ]
            is None
        )


@pytest.mark.parametrize("change", ["paused", "left", "rebind"])
def test_revocation_is_checked_on_every_operation(notified, database, change):
    n = notified
    root = f"/rooms/{n['room']}"
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        assert api.get(root + f"/messages/{n['mid']}", headers=headers(n)).status_code == 200
        if change == "rebind":
            n["store"].configure_binding(
                n["room"], "recipient", "default", "replacement", "/workspace"
            )
        else:
            n["store"].set_collaboration(n["room"], n["recipient"], change)
        status = api.get(root + "/membership/notifications", headers=headers(n))
        if change == "paused":
            assert status.json()["state"] == "paused"
        else:
            assert status.status_code == 403
        assert api.get(root + f"/messages/{n['mid']}", headers=headers(n)).status_code == 403
        assert api.post(root + f"/messages/{n['mid']}/ack", headers=headers(n)).status_code == 403
        assert api.post(root + "/messages", headers=headers(n), json=reply(n)).status_code == 403


def test_concurrent_replies_create_only_one_message(notified, database):
    n = notified
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda key: api.post(
                        f"/rooms/{n['room']}/messages", headers=headers(n), json=reply(n, key)
                    ),
                    ["one", "two"],
                )
            )
        assert sorted(r.status_code for r in results) == [200, 409]
    assert len(n["store"].receive(n["room"], n["sender"], 0, 100).messages) == 1


def test_v05_upgrade_keeps_old_attempts_unknown_without_issuing_or_resending_keys(
    notified, database
):
    n = notified
    with n["store"].connection() as conn:
        conn.execute("DROP INDEX notification_key_lookup")
        conn.execute("ALTER TABLE notifications DROP COLUMN notification_key_hash")
        conn.execute("UPDATE notifications SET state = 'submitting'")
        conn.execute("PRAGMA user_version = 4")
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        assert (
            api.get(f"/rooms/{n['room']}/messages/{n['mid']}", headers=headers(n)).status_code
            == 403
        )
        assert (
            n["store"].notification_status(n["room"], n["recipient"])["pending_notification"][
                "state"
            ]
            == "unknown"
        )
        assert n["store"].next_notification(n["binding"]["binding_id"]) is None
    backups = list(database.parent.glob("*.v4-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "notification_key_hash" not in {
            r[1] for r in conn.execute("PRAGMA table_info(notifications)")
        }


@pytest.mark.anyio
async def test_fresh_stdio_processes_process_notice_with_empty_credential_directory(
    notified, live_url, tmp_path
):
    n = notified
    state = tmp_path / "empty-bridge-state"
    arguments = {"room_id": n["room"], "notification_key": n["key"]}
    async with bridge(live_url, state) as first_runtime:
        assert (await call(first_runtime, "notification_status", **arguments))["state"] == "active"
        assert (await call(first_runtime, "get_message", message_id=n["mid"], **arguments))[
            "acknowledged_at"
        ] is None
        mixed = await first_runtime.call_tool(
            "get_message", {**arguments, "message_id": n["mid"], "session_key": n["recipient_key"]}
        )
        assert mixed.is_error and "not both" in str(mixed.content)
    # New OS process + empty identity directory: no JS store/load or remembered session_key.
    async with bridge(live_url, state) as second_runtime:
        sent = await call(second_runtime, "send_message", **reply(n), **arguments)
        assert sent["sender"] == "recipient"
        assert (await call(second_runtime, "ack_message", message_id=n["mid"], **arguments))[
            "acknowledged_at"
        ]
    async with bridge(live_url, state) as third_runtime:
        assert (await call(third_runtime, "get_message", message_id=n["mid"], **arguments))[
            "acknowledged_at"
        ]
        assert await call(third_runtime, "send_message", **reply(n), **arguments) == sent
    assert not state.exists()  # No credential files were scanned, created, or restored.
    assert n["store"].resume(n["room"], n["recipient"]).member.member_name == "recipient"


def test_delivered_key_still_works_when_acceptance_reply_was_lost(notified, database):
    n = notified
    with n["store"].connection() as conn:
        conn.execute("UPDATE notifications SET state = 'submitting'")
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        status = api.get(f"/rooms/{n['room']}/membership/notifications", headers=headers(n)).json()
        assert status["pending_notification"]["state"] == "unknown"
        assert (
            api.get(f"/rooms/{n['room']}/messages/{n['mid']}", headers=headers(n)).status_code
            == 200
        )
        assert (
            api.post(f"/rooms/{n['room']}/messages/{n['mid']}/ack", headers=headers(n)).status_code
            == 200
        )
    assert n["store"].next_notification(n["binding"]["binding_id"]) is None
