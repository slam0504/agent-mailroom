"""Phase 1a group G: notification_status reads delivery claims, and the DTO carries them."""

import hashlib
import json

import pytest
from delivery_helpers import BOB, HOST, WORKSPACE, Clock, register_host, target
from fastapi.testclient import TestClient
from test_delivery_claims import claim, digest, key_in, legacy_env, report, time_out
from test_delivery_grants import cred, old_mode
from test_delivery_migration import ROOM

from agent_mailroom.api import create_app
from agent_mailroom.models import (
    BatRegistration,
    CreateRoomRequest,
    NotificationStatus,
    ReconnectRequest,
)
from agent_mailroom.store import Store

CLAIM_ONLY = {
    "message_id",
    "room_id",
    "member_id",
    "binding_id",
    "binding_version",
    "state",
    "unknown_reason",
    "outcome_reported_at",
    "updated_at",
}
OLD_MODE = {
    "message_id",
    "room_id",
    "member_id",
    "binding_id",
    "client_message_id",
    "state",
    "queued",
    "updated_at",
}


def pending_of(env, who=BOB):
    return env.store.notification_status(env.room, who)["pending_notification"]


def no_credentials(status, *secrets):
    text = json.dumps(status, default=str)
    assert "notification_key_hash" not in text and "notice_" not in text
    for secret in secrets:
        assert secret not in text and digest(secret) not in text


# --- claims drive pending_notification --------------------------------------------------------


def test_a_normal_claim_without_any_notifications_row_is_shown(env):
    made = claim(env)
    key = key_in(made["prompt"])
    with env.store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0

    status = env.store.notification_status(env.room, BOB)
    shown = status["pending_notification"]
    assert set(shown) == CLAIM_ONLY  # nothing faked: no client_message_id, no queued
    assert (shown["message_id"], shown["state"], shown["member_id"]) == (
        env.m1.message_id,
        "submitting",
        BOB,
    )
    assert (shown["binding_id"], shown["binding_version"]) == (
        made["binding_id"],
        made["binding_version"],
    )
    assert shown["unknown_reason"] is None and shown["outcome_reported_at"] is None
    no_credentials(status, key)


def test_the_oldest_unacknowledged_message_and_its_latest_claim_decide(env):
    assert pending_of(env) is None  # unclaimed mail: nothing attempted yet
    first = claim(env)
    report(env, first, "released", "expired")
    assert pending_of(env)["state"] == "released"  # latest attempt is the released one
    second = claim(env)
    shown = pending_of(env)
    assert shown["state"] == "submitting" and shown["message_id"] == env.m1.message_id
    report(env, second, "accepted")
    assert pending_of(env)["state"] == "accepted"
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    assert pending_of(env) is None  # now the oldest unread is m2, which has no attempt
    claim(env, message=env.m2)
    assert pending_of(env)["message_id"] == env.m2.message_id


def test_a_notification_credential_sees_only_its_own_message(env):
    first = claim(env)
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    status = env.store.notification_status(env.room, cred(key_in(second["prompt"])))
    assert status["pending_notification"]["message_id"] == env.m2.message_id
    assert status["pending_notification"]["state"] == "submitting"
    no_credentials(status, key_in(first["prompt"]), key_in(second["prompt"]))


def test_another_members_claims_and_other_rooms_are_not_shown(env):
    claim(env)
    other_room = env.store.create_room(
        "c" * 64, CreateRoomRequest(member_name="carol", workspace=WORKSPACE)
    ).room.room_id
    assert env.store.notification_status(other_room, "c" * 64)["pending_notification"] is None


def test_runtime_unknown_report_timeout_and_a_late_accepted_keep_their_reasons(env):
    first = claim(env)
    report(env, first, "unknown")
    shown = pending_of(env)
    assert (shown["state"], shown["unknown_reason"]) == ("unknown", "runtime_unknown")
    assert shown["outcome_reported_at"] is not None

    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    time_out(env, second)
    shown = pending_of(env)
    assert (shown["state"], shown["unknown_reason"], shown["outcome_reported_at"]) == (
        "unknown",
        "report_timeout",
        None,
    )
    report(env, second, "accepted")
    shown = pending_of(env)
    assert (shown["state"], shown["unknown_reason"]) == ("accepted", "report_timeout")
    assert shown["outcome_reported_at"] is not None  # accepted late, history kept


def test_converted_legacy_claims_show_their_preserved_state_and_legacy_reason(database):
    store = legacy_env(
        database,
        bindings=[("bind-bob", ROOM, BOB, "default", "bat-bob", WORKSPACE, "claude", None)],
        notifications=[(2, ROOM, BOB, "bind-bob", "client-2", "unknown", 1, "t9", "d" * 64)],
    )
    with store.connection() as conn:
        conn.execute("UPDATE messages SET acknowledged_at = 'acked' WHERE message_id = 1")
    shown = store.notification_status(ROOM, BOB)["pending_notification"]
    assert shown["message_id"] == 2 and set(shown) == CLAIM_ONLY
    assert (shown["state"], shown["unknown_reason"]) == ("unknown", "legacy")
    assert shown["outcome_reported_at"] is None  # a legacy unknown never had a result reported
    no_credentials(store.notification_status(ROOM, BOB))


def test_after_a_takeover_the_new_identity_does_not_inherit_the_old_attempt(env):
    claim(env)
    assert pending_of(env) is not None
    new_id = "f" * 64
    env.store.reconnect(
        env.room,
        new_id,
        ReconnectRequest(
            member_name="bob", bat=BatRegistration(runtime="claude", session_id="bat-new")
        ),
        lambda *args: None,
    )
    assert pending_of(env, new_id) is None


def test_old_mode_keeps_the_original_path_and_fields(database):
    store, room, message, key = old_mode(database)
    status = store.notification_status(room, cred(key))
    shown = status["pending_notification"]
    assert set(shown) == OLD_MODE
    assert (shown["state"], shown["client_message_id"], shown["message_id"]) == (
        "submitting",
        "client-1",
        message.message_id,
    )
    no_credentials(status, key)
    assert hashlib.sha256(key.encode()).hexdigest() not in json.dumps(status)


# --- the response model really carries it -----------------------------------------------------


def headers(who):
    return {"Authorization": f"Bearer {who * 43}"}


@pytest.fixture
def served(api, database):
    """An API-created room (real session keys) plus a delivery-mode binding on the same DB."""
    created = api.post(
        "/rooms",
        headers=headers("a"),
        json={"workspace": WORKSPACE, "member_name": "alice"},
    )
    assert created.status_code == 200
    room = created.json()["room"]["room_id"]
    assert (
        api.post(
            f"/rooms/{room}/members", headers=headers("b"), json={"member_name": "bob"}
        ).status_code
        == 200
    )
    bob = hashlib.sha256(("b" * 43).encode()).hexdigest()
    store = Store(database)
    store.clock = Clock()
    register_host(store)
    pending = store.advance_binding_generation(room, bob, target("bat-bob"), WORKSPACE)
    binding = store.confirm_binding(pending["binding_id"], pending["binding_version"], HOST, 1)
    for number in (1, 2):
        assert (
            api.post(
                f"/rooms/{room}/messages",
                headers=headers("a"),
                json={"to": "bob", "text": "x", "request_id": f"q{number}"},
            ).status_code
            == 200
        )

    def attempt(message_id):
        return store.claim_delivery(
            binding["binding_id"], binding["binding_version"], message_id, HOST, 1
        )

    def read(who="b"):
        response = api.get(f"/rooms/{room}/membership/notifications", headers=headers(who))
        assert response.status_code == 200
        raw = response.text
        assert "notification_key_hash" not in raw and "notice_" not in raw
        return NotificationStatus.model_validate(response.json())  # what the bridge does

    def report_outcome(made, result, evidence=None):
        return store.record_outcome(
            made["claim_id"], made["binding_id"], made["binding_version"], result, evidence
        )

    return store, attempt, read, report_outcome


def test_serialized_status_reads_back_released_runtime_unknown_and_late_accepted(served):
    store, attempt, read, report_outcome = served

    first = attempt(1)
    assert read().pending_notification.state == "submitting"
    report_outcome(first, "released", "busy")
    shown = read().pending_notification
    assert shown.state == "released" and shown.message_id == 1

    second = attempt(1)
    report_outcome(second, "unknown")
    shown = read().pending_notification
    assert (shown.state, shown.unknown_reason) == ("unknown", "runtime_unknown")
    assert shown.client_message_id is None and shown.queued is None  # nothing invented

    with store.connection() as conn:
        conn.execute("UPDATE messages SET acknowledged_at = 't' WHERE message_id = 1")
    third = attempt(2)
    store.clock.advance(minutes=10, seconds=1)
    assert store.expire_overdue_claims() == 1
    shown = read().pending_notification
    assert (shown.state, shown.unknown_reason, shown.outcome_reported_at) == (
        "unknown",
        "report_timeout",
        None,
    )
    report_outcome(third, "accepted")
    shown = read().pending_notification
    assert (shown.state, shown.unknown_reason) == ("accepted", "report_timeout")
    assert shown.outcome_reported_at is not None
    assert shown.binding_version == 1


def test_the_old_mode_response_still_validates_and_keeps_its_fields(database):
    store, room, message, key = old_mode(database.parent / "old.sqlite3")
    with TestClient(create_app(store.path), base_url="http://127.0.0.1") as old_api:
        response = old_api.get(
            f"/rooms/{room}/membership/notifications",
            headers={"Authorization": "Bearer " + key},
        )
    shown = NotificationStatus.model_validate(response.json()).pending_notification
    assert (shown.state, shown.client_message_id, shown.queued) == (
        "unknown",
        "client-1",
        None,
    )  # the app start recovered it
    assert shown.unknown_reason is None and shown.binding_version is None
