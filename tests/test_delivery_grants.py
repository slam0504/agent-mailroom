"""Phase 1a group G1: one revocation authority for notification grants.

A hash with a delivery_claims row is judged only by that row's key_revoked_at; only a hash with
no claim at all (a not-yet-converted legacy notification) uses the original notifications JOIN.
"""

import hashlib
import sqlite3

import pytest
from delivery_helpers import ALICE, BOB, HOST, WORKSPACE, target
from test_delivery_claims import claim, claim_rows, digest, key_in, report, row_of
from test_delivery_migration import ROOM, build_v6, legacy_messages

from agent_mailroom.models import (
    BatRegistration,
    CreateRoomRequest,
    JoinRequest,
    MailroomError,
    NotificationCredential,
    ReconnectRequest,
    SendRequest,
)
from agent_mailroom.store import Store


def cred(key: str) -> NotificationCredential:
    return NotificationCredential(digest=hashlib.sha256(key.encode()).hexdigest())


def usable(store, room, key, message_id) -> bool:
    """True only if the notification credential can read its own message (and status)."""
    try:
        store.get_message(room, cred(key), message_id)
        store.notification_status(room, cred(key))
    except MailroomError as exc:
        assert exc.status_code == 403
        return False
    return True


def status_only(store, room, key) -> bool:
    try:
        store.notification_status(room, cred(key))
    except MailroomError as exc:
        assert exc.status_code == 403
        return False
    return True


# --- not converted: the original path is untouched (G04) ----------------------------------


def old_mode(database):
    store = Store(database)
    store.initialize()
    store.create_room(ALICE, CreateRoomRequest(member_name="alice", workspace=WORKSPACE))
    with store.connection() as conn:
        room = conn.execute("SELECT room_id FROM rooms").fetchone()[0]
    store.join(room, BOB, JoinRequest(member_name="bob"))
    binding = store.configure_binding(room, "bob", "default", "bat-bob", WORKSPACE)
    message = store.send(room, ALICE, SendRequest(to="bob", text="body", request_id="q1"))
    key = "notice_" + "7" * 64
    candidate = store.next_notification(binding["binding_id"])
    assert store.submit_notification(candidate, "client-1", lambda: None, key)
    return store, room, message, key


def test_unconverted_notification_keeps_the_original_join_lookup(database):
    store, room, message, key = old_mode(database)
    assert usable(store, room, key, message.message_id)
    store.set_collaboration(room, BOB, "paused")
    assert status_only(store, room, key)
    with pytest.raises(MailroomError) as caught:
        store.get_message(room, cred(key), message.message_id)
    assert caught.value.status_code == 403  # paused: limited, but not revoked
    store.set_collaboration(room, BOB, "active")
    assert usable(store, room, key, message.message_id)
    store.configure_binding(room, "bob", "default", "bat-bob", WORKSPACE)  # same target: no-op
    assert usable(store, room, key, message.message_id)
    store.configure_binding(room, "bob", "default", "bat-bob-2", WORKSPACE)  # real rebind
    assert not usable(store, room, key, message.message_id)  # revoked through the old JOIN
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM delivery_claims").fetchone() == (0,)
    # the member credential never depended on the notification grant
    assert store.get_message(room, BOB, message.message_id).message_id == message.message_id


def test_converted_grants_decide_exactly_as_the_original_lookup_did(database):
    valid_key, replaced_key = "notice_" + "1" * 64, "notice_" + "2" * 64
    build_v6(
        database,
        messages=legacy_messages()[:2],
        bindings=[("bind-bob", ROOM, BOB, "default", "bat-bob", WORKSPACE, "claude", None)],
        notifications=[
            (1, ROOM, BOB, "bind-bob", "c1", "accepted", 1, "t9", digest(valid_key)),
            (2, ROOM, BOB, "bind-replaced", "c2", "accepted", 1, "t9", digest(replaced_key)),
        ],
    )
    store = Store(database)
    store.initialize()
    before = {
        (key, mid): usable(store, ROOM, key, mid)
        for key, mid in ((valid_key, 1), (replaced_key, 2), (valid_key, 2), (replaced_key, 1))
    }
    assert before == {
        (valid_key, 1): True,
        (replaced_key, 2): False,
        (valid_key, 2): False,
        (replaced_key, 1): False,
    }
    store.set_collaboration(ROOM, BOB, "paused")
    paused_before = (status_only(store, ROOM, valid_key), usable(store, ROOM, valid_key, 1))
    store.set_collaboration(ROOM, BOB, "active")

    store.convert_legacy_to_delivery()

    after = {
        (key, mid): usable(store, ROOM, key, mid)
        for key, mid in ((valid_key, 1), (replaced_key, 2), (valid_key, 2), (replaced_key, 1))
    }
    assert after == before
    store.set_collaboration(ROOM, BOB, "paused")
    assert (status_only(store, ROOM, valid_key), usable(store, ROOM, valid_key, 1)) == paused_before
    assert not status_only(store, ROOM, replaced_key)  # revoked stays revoked, even status-only
    with sqlite3.connect(database) as conn:
        revoked = dict(
            conn.execute("SELECT message_id, key_revoked_at IS NOT NULL FROM delivery_claims")
        )
    assert revoked == {1: 0, 2: 1}  # the conversion recorded the old verdict once


# --- the claim row is the single authority --------------------------------------------------


def test_claim_row_decides_even_when_the_old_join_would_still_find_the_grant(env):
    made = claim(env)
    key = key_in(made["prompt"])
    # A legacy-shaped row for the same hash with a live binding: the old JOIN would accept it.
    with env.store.connection() as conn:
        conn.execute(
            "INSERT INTO notifications (message_id, room_id, member_id, binding_id, "
            "client_message_id, state, queued, updated_at, notification_key_hash) "
            "VALUES (?, ?, ?, ?, 'legacy-shadow', 'accepted', 1, 't', ?)",
            (
                env.m1.message_id,
                env.room,
                BOB,
                env.binding["binding_id"],
                row_of(env, made)["notification_key_hash"],
            ),
        )
        found_by_old_join = conn.execute(
            "SELECT 1 FROM notifications n JOIN bat_bindings b ON b.binding_id = n.binding_id "
            "AND b.room_id = n.room_id AND b.member_id = n.member_id "
            "WHERE n.notification_key_hash = ? AND n.room_id = ?",
            (cred(key).digest, env.room),
        ).fetchone()
    assert found_by_old_join and usable(env.store, env.room, key, env.m1.message_id)
    with env.store.connection() as conn:
        conn.execute("UPDATE delivery_claims SET key_revoked_at = 'revoked-by-claim'")
        assert conn.execute(  # the old lookup is still positive; only the claim row says no
            "SELECT 1 FROM notifications n JOIN bat_bindings b ON b.binding_id = n.binding_id "
            "WHERE n.notification_key_hash = ?",
            (cred(key).digest,),
        ).fetchone()
    assert not usable(env.store, env.room, key, env.m1.message_id)
    assert not status_only(env.store, env.room, key)


def test_converted_legacy_row_revoked_in_the_claim_is_refused_despite_a_live_join(database):
    key = "notice_" + "3" * 64
    build_v6(
        database,
        messages=legacy_messages()[:1],
        bindings=[("bind-bob", ROOM, BOB, "default", "bat-bob", WORKSPACE, "claude", None)],
        notifications=[
            (
                1,
                ROOM,
                BOB,
                "bind-bob",
                "c1",
                "accepted",
                1,
                "t9",
                hashlib.sha256(key.encode()).hexdigest(),
            )
        ],
    )
    store = Store(database)
    store.initialize()
    store.convert_legacy_to_delivery()
    assert usable(store, ROOM, key, 1)
    with store.connection() as conn:
        conn.execute("UPDATE delivery_claims SET key_revoked_at = 'revoked-by-claim'")
        assert conn.execute(  # notifications + live binding: the original query still finds it
            "SELECT 1 FROM notifications n JOIN bat_bindings b ON b.binding_id = n.binding_id "
            "AND b.room_id = n.room_id AND b.member_id = n.member_id "
            "WHERE n.notification_key_hash = ?",
            (cred(key).digest,),
        ).fetchone()
    assert not usable(store, ROOM, key, 1)


def test_a_known_claim_hash_is_never_valid_in_another_room(env):
    key = key_in(claim(env)["prompt"])
    other = env.store.create_room(
        "c" * 64, CreateRoomRequest(member_name="carol", workspace=WORKSPACE)
    )
    with pytest.raises(MailroomError) as caught:
        env.store.notification_status(
            other.room.room_id,
            cred(key),
        )
    assert caught.value.status_code == 403


# --- lifecycle of a normal grant ------------------------------------------------------------


def member_still_works(env, message_id):
    store = env.store
    assert store.get_message(env.room, BOB, message_id).message_id == message_id
    assert store.receive(env.room, BOB, 0, 10).messages
    assert store.notification_status(env.room, BOB)["state"] in ("active", "paused")


def test_pause_and_same_target_retry_do_not_revoke_but_rebind_and_leave_do_forever(env):
    store, mid = env.store, env.m1.message_id
    key = key_in(claim(env)["prompt"])
    assert usable(store, env.room, key, mid)

    store.set_collaboration(env.room, BOB, "paused")
    assert status_only(store, env.room, key) and not usable(store, env.room, key, mid)
    assert row_of(env, {"claim_id": claim_rows(env)[0]["claim_id"]})["key_revoked_at"] is None
    store.set_collaboration(env.room, BOB, "active")
    assert usable(store, env.room, key, mid)

    current = env.store.configure_binding(env.room, "bob", "default", "bat-bob", WORKSPACE)
    assert current["binding_id"] == env.binding["binding_id"]  # same target: nothing re-created
    assert usable(store, env.room, key, mid)

    moved = store.advance_binding_generation(env.room, BOB, target("bat-bob-2"), WORKSPACE)
    assert not usable(store, env.room, key, mid) and not status_only(store, env.room, key)
    member_still_works(env, mid)
    store.confirm_binding(moved["binding_id"], moved["binding_version"], HOST, 1)
    assert not status_only(store, env.room, key)  # confirming the new generation revives nothing
    store.set_collaboration(env.room, BOB, "left")
    store.set_collaboration(env.room, BOB, "active")  # same member key restores activity...
    member_still_works(env, mid)
    assert not status_only(store, env.room, key)  # ...but never the revoked grant


def test_leave_revokes_the_grant_and_restoring_active_does_not_revive_it(env):
    key = key_in(claim(env)["prompt"])
    env.store.set_collaboration(env.room, BOB, "left")
    assert not status_only(env.store, env.room, key)
    env.store.set_collaboration(env.room, BOB, "active")
    assert not status_only(env.store, env.room, key)
    assert row_of(env, claim_rows(env)[0])["key_revoked_at"] is not None
    assert env.store.get_message(env.room, BOB, env.m1.message_id).message_id == env.m1.message_id


def test_retargeting_through_bind_member_revokes_the_grant(env):
    key = key_in(claim(env)["prompt"])
    env.store.configure_binding(env.room, "bob", "default", "bat-elsewhere", WORKSPACE)
    assert not status_only(env.store, env.room, key)
    member_still_works(env, env.m1.message_id)


def test_old_mode_reconnect_revokes_the_previous_members_claim_grants(env):
    key = key_in(claim(env)["prompt"])
    new_id = "f" * 64
    env.store.reconnect(
        env.room,
        new_id,
        ReconnectRequest(
            member_name="bob", bat=BatRegistration(runtime="claude", session_id="bat-new")
        ),
        lambda *args: None,
    )
    assert not status_only(env.store, env.room, key)
    assert row_of(env, claim_rows(env)[0])["key_revoked_at"] is not None


def test_new_generation_then_new_claim_gives_a_working_new_grant_only(env):
    old_key = key_in(claim(env)["prompt"])
    moved = env.store.advance_binding_generation(env.room, BOB, target("bat-bob-2"), WORKSPACE)
    env.store.confirm_binding(moved["binding_id"], moved["binding_version"], HOST, 1)
    fresh = claim(env, binding_id=moved["binding_id"], binding_version=moved["binding_version"])
    new_key = key_in(fresh["prompt"])
    assert usable(env.store, env.room, new_key, env.m1.message_id)
    assert not usable(env.store, env.room, old_key, env.m1.message_id)
    reply = env.store.send(
        env.room,
        cred(new_key),
        SendRequest(
            to="alice", text="done", request_id="notice-reply-1", reply_to=env.m1.message_id
        ),
    )
    assert reply.sender == "bob"
    env.store.get_message(env.room, cred(new_key), env.m1.message_id, ack=True)
    assert env.store.get_message(env.room, BOB, env.m1.message_id).acknowledged_at is not None


# --- released revokes exactly one grant -----------------------------------------------------


def test_released_revokes_only_its_own_grant_not_other_messages_members_or_rooms(env):
    store = env.store
    first = claim(env)
    store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)  # a different message of the same member

    store.join(env.room, "c" * 64, JoinRequest(member_name="carol"))
    carol = store.advance_binding_generation(env.room, "c" * 64, target("bat-carol"), WORKSPACE)
    store.confirm_binding(carol["binding_id"], carol["binding_version"], HOST, 1)
    to_carol = store.send(env.room, ALICE, SendRequest(to="carol", text="x", request_id="c1"))
    third = store.claim_delivery(
        carol["binding_id"],
        carol["binding_version"],
        to_carol.message_id,
        HOST,
        1,
        key_factory=env.keys,
    )

    other = store.create_room("d" * 64, CreateRoomRequest(member_name="dave", workspace=WORKSPACE))
    other_room = other.room.room_id
    store.join(other_room, "e" * 64, JoinRequest(member_name="erin"))
    erin = store.advance_binding_generation(other_room, "e" * 64, target("bat-erin"), WORKSPACE)
    store.confirm_binding(erin["binding_id"], erin["binding_version"], HOST, 1)
    to_erin = store.send(other_room, "d" * 64, SendRequest(to="erin", text="x", request_id="e1"))
    fourth = store.claim_delivery(
        erin["binding_id"],
        erin["binding_version"],
        to_erin.message_id,
        HOST,
        1,
        key_factory=env.keys,
    )
    keys = {
        n: key_in(m["prompt"])
        for n, m in (("1", first), ("2", second), ("3", third), ("4", fourth))
    }
    rooms = {"1": env.room, "2": env.room, "3": env.room, "4": other_room}
    mids = {
        "1": env.m1.message_id,
        "2": env.m2.message_id,
        "3": to_carol.message_id,
        "4": to_erin.message_id,
    }
    assert all(usable(store, rooms[n], keys[n], mids[n]) for n in keys)
    before = {r["claim_id"]: r for r in claim_rows(env)}

    report(env, first, "released", "busy:resting")

    assert [usable(store, rooms[n], keys[n], mids[n]) for n in "1234"] == [False, True, True, True]
    after = {r["claim_id"]: r for r in claim_rows(env)}
    for made in (second, third, fourth):
        assert after[made["claim_id"]] == before[made["claim_id"]]  # no other claim row changed
    # The released attempt itself keeps its history, and it can never be revived by a late report.
    assert after[first["claim_id"]]["state"] == "released"
    assert after[first["claim_id"]]["key_revoked_at"] is not None
    with pytest.raises(MailroomError):
        report(env, first, "accepted")
    assert not usable(store, env.room, keys["1"], mids["1"])


def test_a_later_revocation_keeps_the_original_revoke_time_and_claim_history(env):
    first = claim(env)
    report(env, first, "released", "expired")
    stamped = row_of(env, first)
    assert stamped["key_revoked_at"] is not None
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    before = {r["claim_id"]: r for r in claim_rows(env)}

    env.clock.advance(hours=3)
    env.store.set_collaboration(env.room, BOB, "left")  # revokes every grant, once

    after = {r["claim_id"]: r for r in claim_rows(env)}
    assert after[first["claim_id"]] == before[first["claim_id"]]  # time and history untouched
    assert after[second["claim_id"]]["key_revoked_at"] is not None
    assert {k: v for k, v in after[second["claim_id"]].items() if k != "key_revoked_at"} == {
        k: v for k, v in before[second["claim_id"]].items() if k != "key_revoked_at"
    }


# --- every revocation reads the store's clock ---------------------------------------------------


def revoked_at(env, made):
    return row_of(env, made)["key_revoked_at"]


def retarget(e):
    return e.store.configure_binding(e.room, "bob", "default", "bat-elsewhere", WORKSPACE)


def reconnect_bob(e):
    return e.store.reconnect(
        e.room,
        "f" * 64,
        ReconnectRequest(
            member_name="bob", bat=BatRegistration(runtime="claude", session_id="bat-clock")
        ),
        lambda *args: None,
    )


REVOKERS = {
    "advance": lambda e: e.store.advance_binding_generation(
        e.room, BOB, target("bat-clock"), WORKSPACE
    ),
    "leave": lambda e: e.store.set_collaboration(e.room, BOB, "left"),
    "retarget": retarget,
    "reconnect": reconnect_bob,
    "released": lambda e: report(e, e.made, "released", "expired"),
}


@pytest.mark.parametrize("operation", sorted(REVOKERS))
def test_each_revocation_uses_the_store_clock_exactly(env, operation):
    env.made = claim(env)
    env.clock.advance(hours=7, seconds=3)
    expected = env.store.stamp()

    REVOKERS[operation](env)

    assert revoked_at(env, env.made) == expected  # the controlled time, not the wall clock


@pytest.mark.parametrize("operation", ["advance", "leave", "retarget", "reconnect"])
def test_an_earlier_revoke_time_is_not_rewritten_and_released_touches_only_itself(env, operation):
    first = claim(env)
    report(env, first, "released", "busy")
    released_time = revoked_at(env, first)
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    others = {r["claim_id"]: r for r in claim_rows(env)}
    assert revoked_at(env, second) is None  # the released claim did not touch its sibling
    env.clock.advance(days=2)
    expected = env.store.stamp()

    REVOKERS[operation](env)

    assert revoked_at(env, first) == released_time != expected  # untouched by the later revoke
    assert revoked_at(env, second) == expected
    changed = {r["claim_id"]: r for r in claim_rows(env)}
    assert changed[first["claim_id"]] == others[first["claim_id"]]


def test_the_revoke_time_is_a_required_argument(env):
    with env.store.connection() as conn, pytest.raises(TypeError):
        Store.revoke_claim_grants(conn, env.room, BOB)
    with env.store.connection() as conn, pytest.raises(TypeError):
        Store.revoke_claim_grants(conn, env.room, BOB, None, "2026-01-01T00:00:00.000+00:00")


# --- revocation and binding replacement commit together -------------------------------------


class Interrupting(sqlite3.Connection):
    """After the binding row is deleted, let a second connection look, then fail the next step."""

    observe = None
    armed = False

    def execute(self, sql, *args):
        if type(self).armed:
            type(self).armed = False
            type(self).observe()
            raise RuntimeError("interrupted right after the binding delete")
        result = super().execute(sql, *args)
        if sql.lstrip().upper().startswith("DELETE FROM BAT_BINDINGS"):
            type(self).armed = True
        return result


class _Sqlite:
    def __getattr__(self, name):
        return getattr(sqlite3, name)

    @staticmethod
    def connect(*args, **kwargs):
        return sqlite3.connect(*args, factory=Interrupting, **kwargs)


OPERATIONS = {
    "advance": lambda e: e.store.advance_binding_generation(
        e.room, BOB, target("bat-x"), WORKSPACE
    ),
    "leave": lambda e: e.store.set_collaboration(e.room, BOB, "left"),
    "retarget": lambda e: e.store.configure_binding(e.room, "bob", "default", "bat-y", WORKSPACE),
    "reconnect": lambda e: e.store.reconnect(
        e.room,
        "f" * 64,
        ReconnectRequest(
            member_name="bob", bat=BatRegistration(runtime="claude", session_id="bat-z")
        ),
        lambda *args: None,
    ),
}


def paired_state(path, binding_id):
    """One snapshot: (is the old binding row still there, how many grants are still unrevoked)."""
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM bat_bindings WHERE binding_id = ?), "
            "(SELECT count(*) FROM delivery_claims WHERE member_id = ? AND key_revoked_at IS NULL "
            "AND notification_key_hash IS NOT NULL)",
            (binding_id, BOB),
        ).fetchone()


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_binding_replacement_and_grant_revocation_are_one_commit(env, monkeypatch, operation):
    import agent_mailroom.store as store_module

    claim(env)
    binding_id = env.binding["binding_id"]
    assert paired_state(env.store.path, binding_id) == (1, 1)
    seen = []
    Interrupting.observe = lambda: seen.append(paired_state(env.store.path, binding_id))

    monkeypatch.setattr(store_module, "sqlite3", _Sqlite())
    with pytest.raises(RuntimeError, match="interrupted"):
        OPERATIONS[operation](env)
    monkeypatch.undo()
    Interrupting.armed = False

    assert seen == [(1, 1)]  # mid-transaction nothing was visible to another connection
    assert paired_state(env.store.path, binding_id) == (1, 1)  # and the rollback kept both

    OPERATIONS[operation](env)
    assert paired_state(env.store.path, binding_id) == (0, 0)  # both changed together
