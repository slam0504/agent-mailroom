"""Phase 1a Store-level claim / candidates (C01-C10) and outcome (O01-O11) tests."""

import hashlib
import re
import sqlite3
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from delivery_helpers import ALICE, BOB, BODY, HOST, WORKSPACE, Clock, Keys, register_host, target
from test_delivery_migration import ROOM, binding_of, build_v6, legacy_messages, snapshot

from agent_mailroom.models import CreateRoomRequest, JoinRequest, MailroomError, SendRequest
from agent_mailroom.store import Store

OTHER_HOST = "host_" + "2" * 32
TABLES = ("delivery_claims", "bat_bindings", "binding_generations", "members", "messages")


def key_in(prompt: str) -> str:
    return re.search(r"notice_[a-f0-9]{64}", prompt).group(0)


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def claim(env, message=None, **override):
    args = {
        "binding_id": env.binding["binding_id"],
        "binding_version": env.binding["binding_version"],
        "message_id": (message or env.m1).message_id,
        "host_id": HOST,
        "host_epoch": 1,
        "key_factory": env.keys,
    }
    args.update(override)
    return env.store.claim_delivery(**args)


def claim_rows(env):
    with sqlite3.connect(env.store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM delivery_claims ORDER BY claim_seq")]


def tables(env):
    return {t: snapshot(env.store.path, t) for t in TABLES}


def refused(env, status, detail, **kwargs):
    """The claim is refused with exactly this status/detail and nothing at all is written."""
    before, minted = tables(env), env.keys.calls
    with pytest.raises(MailroomError) as caught:
        claim(env, **kwargs)
    assert (caught.value.status_code, caught.value.detail) == (status, detail)
    assert tables(env) == before
    assert env.keys.calls == minted  # the credential generator was never called


def set_claim(env, claim_id, **values):
    sets = ", ".join(f"{k} = ?" for k in values)
    with env.store.connection() as conn:
        conn.execute(
            f"UPDATE delivery_claims SET {sets} WHERE claim_id = ?", (*values.values(), claim_id)
        )


# --- C01 / C02 ---------------------------------------------------------------------------


def test_c01_valid_claim_persists_one_submitting_row_before_returning(env):
    result = claim(env)

    [row] = claim_rows(env)  # a separate connection already sees the complete record
    assert env.keys.calls == 1
    assert result["claim_id"] == row["claim_id"]
    assert (row["origin"], row["state"], row["unknown_reason"]) == ("normal", "submitting", None)
    assert (row["message_id"], row["room_id"], row["member_id"], row["recipient"]) == (
        env.m1.message_id, env.room, BOB, "bob",
    )  # fmt: skip
    assert (row["binding_id"], row["binding_version"]) == (
        env.binding["binding_id"], env.binding["binding_version"],
    )  # fmt: skip
    assert (row["host_id"], row["host_epoch"]) == (HOST, 1)
    assert row["notification_key_hash"] == digest(key_in(result["prompt"]))
    assert row["key_revoked_at"] is None and row["outcome_reported_at"] is None
    sent = env.clock.now
    assert result["send_deadline"] == (sent + timedelta(seconds=30)).isoformat(
        timespec="milliseconds"
    )
    assert result["report_deadline"] == (sent + timedelta(minutes=10)).isoformat(
        timespec="milliseconds"
    )
    assert (row["send_deadline"], row["report_deadline"]) == (
        result["send_deadline"], result["report_deadline"],
    )  # fmt: skip
    assert env.store.get_message(env.room, BOB, env.m1.message_id).acknowledged_at is None


def test_c02_prompt_has_only_addressing_and_the_notification_key(env):
    result = claim(env)
    prompt, key = result["prompt"], key_in(result["prompt"])
    assert f"room_id={env.room}" in prompt and f"message_id={env.m1.message_id}" in prompt
    for secret in (BODY, BOB, ALICE, HOST, "session_key=", "delivery"):
        assert secret not in prompt.replace("session_key: if you hold it", "")
    assert prompt.count("notice_") == 1
    with sqlite3.connect(env.store.path) as conn:
        dump = "\n".join(conn.iterdump())
    assert key not in dump and digest(key) in dump  # only the hash is stored


# --- C03 / C04 / C05 ---------------------------------------------------------------------


def test_c03_pending_and_rejected_targets_are_not_candidates_and_cannot_be_claimed(env):
    store = env.store
    assert [c["message_id"] for c in store.delivery_candidates(HOST, 1)] == [env.m1.message_id]
    moved = store.advance_binding_generation(env.room, BOB, target("bat-bob-2"), WORKSPACE)
    assert moved["binding_state"] == "pending"
    assert store.delivery_candidates(HOST, 1) == []
    refused(
        env, 409, "binding_not_confirmed",
        binding_id=moved["binding_id"], binding_version=moved["binding_version"],
    )  # fmt: skip
    with store.connection() as conn:
        conn.execute("UPDATE bat_bindings SET binding_state = 'rejected'")
    assert store.delivery_candidates(HOST, 1) == []
    refused(
        env, 409, "binding_not_confirmed",
        binding_id=moved["binding_id"], binding_version=moved["binding_version"],
    )  # fmt: skip
    # The earlier generation's coordinates no longer address any binding.
    refused(env, 409, "binding_not_current")


def test_c03_stale_version_on_a_confirmed_binding_is_refused(env):
    refused(
        env, 409, "binding_version_mismatch", binding_version=env.binding["binding_version"] - 1
    )
    refused(
        env, 409, "binding_version_mismatch", binding_version=env.binding["binding_version"] + 1
    )


def test_c04_epoch_and_owner_are_checked_against_stored_state_with_exact_errors(env):
    register_host(env.store, epoch=2)  # a newer instance replaced the one the caller knows
    refused(env, 409, "instance_superseded")
    with pytest.raises(MailroomError) as caught:
        env.store.delivery_candidates(HOST, 1)
    assert (caught.value.status_code, caught.value.detail) == (409, "instance_superseded")
    assert env.store.delivery_candidates(HOST, 2)  # the current epoch is accepted again

    with env.store.connection() as conn:
        conn.execute("INSERT INTO delivery_hosts VALUES (?, 7, 't')", (OTHER_HOST,))
    # A valid other host (its own current epoch) must not be mistaken for a superseded instance.
    refused(env, 409, "binding_host_mismatch", host_id=OTHER_HOST, host_epoch=7)
    refused(env, 409, "host_not_registered", host_id="host_" + "3" * 32, host_epoch=1)
    with env.store.connection() as conn:  # binding owned by another host, caller is current
        conn.execute("UPDATE bat_bindings SET host_id = ?", (OTHER_HOST,))
    refused(env, 409, "binding_host_mismatch", host_epoch=2)


def test_c04_binding_without_canonical_owner_metadata_is_refused(env):
    with env.store.connection() as conn:
        conn.execute("UPDATE bat_bindings SET host_id = NULL")
    refused(env, 409, "binding_host_mismatch", host_epoch=1)
    with env.store.connection() as conn:
        conn.execute("UPDATE bat_bindings SET binding_state = NULL, binding_version = NULL")
    refused(env, 409, "binding_not_confirmed")


@pytest.mark.parametrize("state", ["paused", "left"])
def test_c05_inactive_members_are_refused_even_with_a_confirmed_binding(env, state):
    with env.store.connection() as conn:  # keep the binding row: only the member state differs
        conn.execute("UPDATE members SET state = ? WHERE member_id = ?", (state, BOB))
    assert binding_of(env.store.path)["binding_state"] == "confirmed"
    assert env.store.delivery_candidates(HOST, 1) == []
    refused(env, 409, "member_not_active")


# --- C06 / C07 ---------------------------------------------------------------------------


def test_c06_pause_after_candidates_blocks_the_claim_until_active_again(env):
    [candidate] = env.store.delivery_candidates(HOST, 1)
    env.store.set_collaboration(env.room, BOB, "paused")
    refused(
        env, 409, "member_not_active",
        binding_id=candidate["binding_id"], binding_version=candidate["binding_version"],
        message_id=candidate["message_id"],
    )  # fmt: skip
    assert claim_rows(env) == [] and env.keys.calls == 0
    env.store.set_collaboration(env.room, BOB, "active")
    assert claim(env)["claim_id"] and env.keys.calls == 1 and len(claim_rows(env)) == 1


def test_c07_pause_after_the_claim_keeps_the_submitting_claim(env):
    result = claim(env)
    before = claim_rows(env)
    env.store.set_collaboration(env.room, BOB, "paused")
    assert claim_rows(env) == before  # not rolled back to released, grant not revoked
    assert before[0]["state"] == "submitting" and before[0]["key_revoked_at"] is None
    assert result["claim_id"] == before[0]["claim_id"]


# --- C08 ---------------------------------------------------------------------------------


def test_c08_only_the_oldest_unacknowledged_message_of_the_recipient_can_be_claimed(env):
    refused(env, 409, "message_not_oldest", message=env.m2)
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    refused(env, 409, "message_acknowledged", message=env.m1)
    assert claim(env, message=env.m2)["claim_id"]  # now the oldest unacknowledged one


def test_c08_wrong_recipient_wrong_room_and_unknown_messages_are_not_found(env):
    store = env.store
    to_alice = store.send(env.room, BOB, SendRequest(to="alice", text=BODY, request_id="r1"))
    refused(env, 404, "Message not found for this binding.", message=to_alice)
    other = store.create_room("c" * 64, CreateRoomRequest(member_name="carol", workspace=WORKSPACE))
    store.join(other.room.room_id, "d" * 64, JoinRequest(member_name="bob"))
    foreign = store.send(
        other.room.room_id, "c" * 64, SendRequest(to="bob", text=BODY, request_id="x")
    )
    refused(env, 404, "Message not found for this binding.", message=foreign)
    refused(
        env, 404, "Message not found for this binding.", message=SimpleNamespace(message_id=9999)
    )


# --- C09 ---------------------------------------------------------------------------------


def test_c09_two_stores_claiming_the_same_candidate_yield_exactly_one_grant(env):
    stores = [Store(env.store.path), Store(env.store.path)]  # separate locks and connections
    for store in stores:
        store.clock = env.clock
    keys, barrier, outcomes = Keys(), threading.Barrier(2), []

    def attempt(store):
        barrier.wait()
        try:
            outcomes.append(
                store.claim_delivery(
                    env.binding["binding_id"],
                    env.binding["binding_version"],
                    env.m1.message_id,
                    HOST,
                    1,
                    key_factory=keys,
                )  # fmt: skip
            )
        except MailroomError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    wins = [o for o in outcomes if isinstance(o, dict)]
    losses = [o for o in outcomes if isinstance(o, MailroomError)]
    assert len(wins) == 1 and len(losses) == 1
    assert (losses[0].status_code, losses[0].detail) == (409, "blocked_by_previous_claim")
    assert keys.calls == 1  # the loser never minted a second credential
    [row] = claim_rows(env)
    assert row["claim_id"] == wins[0]["claim_id"] and row["state"] == "submitting"


# --- C10 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("submitting", None),
        ("accepted", None),
        ("not_accepted", None),
        ("unknown", "report_timeout"),
        ("unknown", "startup_recovery"),
    ],
)
def test_c10_blocking_states_stop_the_same_and_later_messages_until_ack(env, state, reason):
    first = claim(env)
    set_claim(env, first["claim_id"], state=state, unknown_reason=reason)
    refused(env, 409, "blocked_by_previous_claim")
    refused(env, 409, "message_not_oldest", message=env.m2)

    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    assert second["claim_id"] != first["claim_id"]
    history = claim_rows(env)
    assert [r["claim_id"] for r in history] == [first["claim_id"], second["claim_id"]]
    assert history[0]["state"] == state  # the older attempt is still there, untouched


def test_c10_released_attempt_lets_the_oldest_message_be_claimed_again_with_a_new_key(env):
    first = claim(env)
    set_claim(env, first["claim_id"], state="released", evidence="busy:resting")
    second = claim(env)
    assert second["claim_id"] != first["claim_id"]
    assert key_in(second["prompt"]) != key_in(first["prompt"])
    assert [r["state"] for r in claim_rows(env)] == ["released", "submitting"]


def test_c10_older_generation_attempt_does_not_block_a_confirmed_newer_generation(env):
    old = claim(env)
    set_claim(env, old["claim_id"], state="accepted")  # accepted, not acknowledged
    moved = env.store.advance_binding_generation(env.room, BOB, target("bat-bob-2"), WORKSPACE)
    kwargs = {"binding_id": moved["binding_id"], "binding_version": moved["binding_version"]}
    refused(env, 409, "binding_not_confirmed", **kwargs)  # pending: still not claimable
    env.store.confirm_binding(moved["binding_id"], moved["binding_version"], HOST, 1)
    fresh = claim(env, **kwargs)
    assert fresh["claim_id"] != old["claim_id"]
    rows = claim_rows(env)
    assert [(r["state"], r["binding_version"]) for r in rows] == [
        ("accepted", 1), ("submitting", moved["binding_version"]),
    ]  # fmt: skip
    assert rows[0]["key_revoked_at"] is not None  # the old grant died with its generation


def test_c10_same_generation_keeps_blocking_even_after_reconfirmation_and_epoch_change(env):
    old = claim(env)
    set_claim(env, old["claim_id"], state="accepted")
    register_host(env.store, epoch=2)
    env.store.confirm_binding(env.binding["binding_id"], env.binding["binding_version"], HOST, 2)
    refused(env, 409, "blocked_by_previous_claim", host_epoch=2)


def test_c10_blocking_is_scoped_to_room_member_message_not_to_the_current_binding_id(env):
    old = claim(env)
    set_claim(env, old["claim_id"], state="not_accepted")
    with env.store.connection() as conn:  # same generation, different binding_id (e.g. re-import)
        conn.execute("UPDATE bat_bindings SET binding_id = 'replacement-id'")
    refused(env, 409, "blocked_by_previous_claim", binding_id="replacement-id")


# --- S04 / S05 / S06 claim assertions deferred from group S ------------------------------

BOB_BINDING = [("bind-bob", ROOM, BOB, "default", "bat-bob", WORKSPACE, "claude", None)]


def legacy_env(database, *, bindings, members=None, notifications=None):
    build_v6(
        database,
        messages=legacy_messages()[:2],
        notifications=notifications
        or [(1, ROOM, BOB, "bind-old", "client-1", "accepted", 1, "t9", "o" * 64)],
        bindings=bindings,
        members=members,
    )
    store = Store(database)
    store.initialize()
    store.convert_legacy_to_delivery()
    register_host(store)
    return store


def attempt(store, binding, epoch=1, message_id=1):
    return store.claim_delivery(
        binding["binding_id"], binding["binding_version"], message_id, HOST, epoch,
        key_factory=Keys(),
    )  # fmt: skip


def expect_refused(store, binding, detail, epoch=1):
    before = {t: snapshot(store.path, t) for t in TABLES}
    with pytest.raises(MailroomError) as caught:
        attempt(store, binding, epoch)
    assert (caught.value.status_code, caught.value.detail) == (409, detail)
    assert {t: snapshot(store.path, t) for t in TABLES} == before


def all_claims(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {r["claim_id"]: dict(r) for r in conn.execute("SELECT * FROM delivery_claims")}


def test_s04_migrated_binding_claims_only_after_a_newer_generation_is_confirmed(database):
    store = legacy_env(database, bindings=BOB_BINDING)
    pending = binding_of(database)
    assert (pending["binding_state"], pending["binding_version"]) == ("pending", 0)
    expect_refused(store, pending, "binding_not_confirmed")
    confirmed = store.confirm_binding("bind-bob", 0, HOST, 1)
    expect_refused(store, confirmed, "blocked_by_previous_claim")  # same-version confirm
    register_host(store, epoch=2)
    store.confirm_binding("bind-bob", 0, HOST, 2)
    expect_refused(store, confirmed, "blocked_by_previous_claim", epoch=2)  # epoch is no generation

    moved = store.advance_binding_generation(ROOM, BOB, target("bat-bob-2"), WORKSPACE)
    expect_refused(store, moved, "binding_not_confirmed", epoch=2)
    store.confirm_binding(moved["binding_id"], moved["binding_version"], HOST, 2)
    result = attempt(store, moved, epoch=2)
    claims = all_claims(database)
    assert (claims["legacy_1"]["state"], claims[result["claim_id"]]["state"]) == (
        "accepted", "submitting",
    )  # fmt: skip


def test_s05_orphan_legacy_member_claims_only_after_new_generation_is_confirmed(database):
    left = [(ROOM, ALICE, "alice", "s-a", "t0", WORKSPACE, "active"),
            (ROOM, BOB, "bob", "s-b", "t1", WORKSPACE, "left")]  # fmt: skip
    store = legacy_env(database, bindings=[], members=left)
    store.set_collaboration(ROOM, BOB, "active")
    created = store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    expect_refused(store, created, "binding_not_confirmed")
    store.confirm_binding(created["binding_id"], created["binding_version"], HOST, 1)
    result = attempt(store, created)
    claims = all_claims(database)
    assert claims["legacy_1"]["state"] == "accepted"  # the orphan attempt is history now
    assert claims[result["claim_id"]]["binding_version"] == created["binding_version"] > 0


def test_s06_existing_binding_with_orphan_legacy_needs_an_explicit_newer_generation(database):
    store = legacy_env(
        database,
        bindings=BOB_BINDING,
        notifications=[
            (1, ROOM, BOB, "bind-replaced", "client-1", "unknown", None, "t9", "p" * 64)
        ],
    )
    confirmed = store.confirm_binding("bind-bob", 0, HOST, 1)
    expect_refused(store, confirmed, "blocked_by_previous_claim")
    register_host(store, epoch=2)  # a host epoch update is not a generation
    store.confirm_binding("bind-bob", 0, HOST, 2)
    expect_refused(store, confirmed, "blocked_by_previous_claim", epoch=2)
    store.set_collaboration(ROOM, BOB, "left")
    store.set_collaboration(ROOM, BOB, "active")  # restoring active alone does not release it
    created = store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    store.confirm_binding(created["binding_id"], created["binding_version"], HOST, 2)
    assert attempt(store, created, epoch=2)["claim_id"] != "legacy_1"


# --- outcome helpers ---------------------------------------------------------------------


def report(env, made, result, evidence=None, **override):
    args = {
        "claim_id": made["claim_id"],
        "binding_id": made["binding_id"],
        "binding_version": made["binding_version"],
        "result": result,
        "evidence": evidence,
    }
    args.update(override)
    return env.store.record_outcome(**args)


def row_of(env, made):
    return next(r for r in claim_rows(env) if r["claim_id"] == made["claim_id"])


def refused_outcome(env, made, status, detail, result, evidence=None, **override):
    """Refused with exactly this status/detail; every table, including the grant, is unchanged."""
    before = tables(env)
    with pytest.raises(MailroomError) as caught:
        report(env, made, result, evidence, **override)
    assert (caught.value.status_code, caught.value.detail) == (status, detail)
    assert tables(env) == before


def time_out(env, made):
    env.clock.advance(minutes=10, seconds=1)
    assert env.store.expire_overdue_claims() == 1
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"], row["outcome_reported_at"]) == (
        "unknown", "report_timeout", None,
    )  # fmt: skip
    return row


# --- O01 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "reason"),
    [("accepted", None), ("not_accepted", None), ("unknown", "runtime_unknown")],
)
def test_o01_first_outcome_is_persisted_and_accepted_is_not_an_ack(env, result, reason):
    made = claim(env)
    others = {t: v for t, v in tables(env).items() if t != "delivery_claims"}
    env.clock.advance(seconds=5)
    report(env, made, result, "runtime-said-so")
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"], row["evidence"]) == (
        result,
        reason,
        "runtime-said-so",
    )
    assert row["outcome_reported_at"] == env.clock.now.isoformat(timespec="milliseconds")
    assert row["key_revoked_at"] is None
    assert {t: v for t, v in tables(env).items() if t != "delivery_claims"} == others
    assert env.store.get_message(env.room, BOB, env.m1.message_id).acknowledged_at is None
    refused(env, 409, "blocked_by_previous_claim")  # none of these results releases the message


# --- O02 / O03 / O04 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence", ["busy", "busy:resting", "busy:pending_permission", "expired", "not_invoked"]
)
def test_o02_o03_released_needs_explicit_not_sent_evidence_and_revokes_only_that_grant(
    env, evidence
):
    first = claim(env)
    report(env, first, "released", evidence)
    row = row_of(env, first)
    assert (row["state"], row["evidence"]) == ("released", evidence)
    assert row["key_revoked_at"] is not None and row["outcome_reported_at"] is not None
    second = claim(env)  # same message again: a new attempt with a new credential
    assert second["claim_id"] != first["claim_id"] and key_in(second["prompt"]) != key_in(
        first["prompt"]
    )
    rows = claim_rows(env)
    assert [r["state"] for r in rows] == ["released", "submitting"]
    assert rows[1]["key_revoked_at"] is None


@pytest.mark.parametrize(
    "evidence", [None, "", "timeout", "exception", "accepted", "busyx", "busy:", "not_invoked2"]
)
def test_o04_released_without_allowed_evidence_is_rejected_and_nothing_changes(env, evidence):
    made = claim(env)
    refused_outcome(env, made, 422, "released_requires_not_sent_evidence", "released", evidence)
    assert row_of(env, made)["state"] == "submitting"


def test_o04_unknown_result_name_is_rejected(env):
    made = claim(env)
    refused_outcome(env, made, 422, "unknown_outcome_result", "released-ish")


# --- O05 / O06 ---------------------------------------------------------------------------


@pytest.mark.parametrize("result", ["accepted", "not_accepted", "released", "unknown"])
def test_o05_same_result_retry_is_idempotent_and_keeps_the_first_evidence(env, result):
    made = claim(env)
    first_evidence = "busy:resting" if result == "released" else "first"
    report(env, made, result, first_evidence)
    after_first = tables(env)
    again = report(env, made, result, "busy:streaming" if result == "released" else "second")
    assert tables(env) == after_first  # nothing rewritten, no second attempt
    assert again["state"] == result and again["evidence"] == first_evidence
    assert len(claim_rows(env)) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("accepted", "not_accepted"),
        ("accepted", "released"),
        ("accepted", "unknown"),
        ("not_accepted", "accepted"),
        ("unknown", "accepted"),
        ("released", "accepted"),
        ("released", "unknown"),
    ],
)
def test_o06_contradictory_result_is_409_and_changes_nothing(env, first, second):
    made = claim(env)
    report(env, made, first, "busy" if first == "released" else None)
    evidence = "busy" if second == "released" else None
    refused_outcome(env, made, 409, "conflicting_outcome", second, evidence)


def test_o06_wrong_creation_tuple_and_unknown_claim_fail_without_side_effects(env):
    made = claim(env)
    refused_outcome(env, made, 409, "binding_mismatch", "accepted", binding_id="other-binding")
    refused_outcome(env, made, 409, "binding_mismatch", "accepted", binding_version=99)
    refused_outcome(env, made, 404, "Claim not found.", "accepted", claim_id="claim_" + "0" * 32)
    assert row_of(env, made)["state"] == "submitting"


# --- O07 / O08 / O09 ---------------------------------------------------------------------


def test_o07_report_deadline_makes_a_timeout_unknown_that_still_blocks(env):
    made = claim(env)
    env.clock.advance(minutes=9, seconds=59)
    assert env.store.expire_overdue_claims() == 0  # the deadline has not passed
    assert row_of(env, made)["state"] == "submitting"
    env.clock.advance(seconds=2)
    assert env.store.expire_overdue_claims() == 1
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"], row["key_revoked_at"]) == (
        "unknown", "report_timeout", None,
    )  # fmt: skip
    assert row["outcome_reported_at"] is None  # nothing was received
    refused(env, 409, "blocked_by_previous_claim")
    assert env.store.expire_overdue_claims() == 0 and len(claim_rows(env)) == 1


def test_o07_expiry_only_touches_overdue_submitting_claims(env):
    resolved = claim(env)
    report(env, resolved, "accepted")
    env.clock.advance(hours=1)
    before = tables(env)
    assert env.store.expire_overdue_claims() == 0
    assert tables(env) == before


@pytest.mark.parametrize("result", ["accepted", "not_accepted", "released"])
def test_o08_late_result_after_report_timeout_is_recorded_exactly_once(env, result):
    made = claim(env)
    time_out(env, made)
    evidence = "expired" if result == "released" else "late"
    env.clock.advance(minutes=1)
    report(env, made, result, evidence)
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"], row["evidence"]) == (
        result,
        "report_timeout",
        evidence,
    )
    assert row["outcome_reported_at"] == env.clock.now.isoformat(timespec="milliseconds")
    assert (row["key_revoked_at"] is not None) == (result == "released")
    after = tables(env)
    report(env, made, result, "different-evidence-on-retry" if result != "released" else "busy")
    assert tables(env) == after  # identical retry is idempotent
    other = {"accepted": "not_accepted", "not_accepted": "accepted", "released": "accepted"}[result]
    refused_outcome(env, made, 409, "conflicting_outcome", other, None)


def test_o08_late_released_frees_the_message_for_a_new_attempt(env):
    made = claim(env)
    time_out(env, made)
    report(env, made, "released", "busy:resting")
    assert claim(env)["claim_id"] != made["claim_id"]


def test_o09_late_unknown_still_consumes_the_single_late_report(env):
    made = claim(env)
    time_out(env, made)
    env.clock.advance(minutes=1)
    report(env, made, "unknown", "still-unsure")
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"]) == ("unknown", "report_timeout")  # cause kept
    assert row["outcome_reported_at"] is not None and row["evidence"] == "still-unsure"
    after = tables(env)
    report(env, made, "unknown", "retry")
    assert tables(env) == after
    for other, evidence in (("accepted", None), ("not_accepted", None), ("released", "busy")):
        refused_outcome(env, made, 409, "conflicting_outcome", other, evidence)
    refused(env, 409, "blocked_by_previous_claim")


# --- O10 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("result", ["accepted", "not_accepted", "unknown", "released"])
def test_o10_startup_recovery_unknown_does_not_accept_any_result(env, result):
    made = claim(env)
    assert env.store.recover_delivery_claims() == 1
    row = row_of(env, made)
    assert (row["state"], row["unknown_reason"], row["outcome_reported_at"]) == (
        "unknown", "startup_recovery", None,
    )  # fmt: skip
    env.clock.advance(hours=1)  # even long past the report deadline
    refused_outcome(
        env, made, 409, "claim_not_accepting_outcome", result,
        "busy" if result == "released" else None,
    )  # fmt: skip
    refused(env, 409, "blocked_by_previous_claim")
    assert env.store.expire_overdue_claims() == 0  # and the timeout sweep leaves it alone


def test_o10_recovery_only_converts_submitting_normal_claims(env):
    first = claim(env)
    report(env, first, "accepted")
    env.store.get_message(env.room, BOB, env.m1.message_id, ack=True)
    second = claim(env, message=env.m2)
    time_out(env, second)
    before = {c["claim_id"]: c for c in claim_rows(env)}
    assert env.store.recover_delivery_claims() == 0
    assert {c["claim_id"]: c for c in claim_rows(env)} == before


def test_o10_initialize_and_reads_never_trigger_startup_recovery(env):
    made = claim(env)
    Store(env.store.path).initialize()
    env.store.delivery_candidates(HOST, 1)
    env.store.notification_status(env.room, BOB)
    env.store.receive(env.room, BOB, 0, 10)
    assert row_of(env, made)["state"] == "submitting"


@pytest.mark.parametrize("result", ["accepted", "unknown", "released"])
def test_o10_legacy_claims_never_accept_outcomes(database, result):
    store = legacy_env(database, bindings=BOB_BINDING)
    legacy = all_claims(database)["legacy_1"]
    before = snapshot(database, "delivery_claims")
    store.clock = Clock()
    store.clock.advance(days=30)
    for _ in range(2):  # a same-result retry is no exception either
        with pytest.raises(MailroomError) as caught:
            store.record_outcome(
                "legacy_1", legacy["binding_id"], legacy["binding_version"], result,
                "busy" if result == "released" else None,
            )  # fmt: skip
        assert (caught.value.status_code, caught.value.detail) == (
            409,
            "claim_not_accepting_outcome",
        )
    assert store.expire_overdue_claims() == 0 and store.recover_delivery_claims() == 0
    assert snapshot(database, "delivery_claims") == before


def test_unknown_reason_vocabulary_has_no_reported_and_runtime_unknown_is_used(env):
    with env.store.connection() as conn:
        conn.execute("SELECT 1")
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'delivery_claims'"
        ).fetchone()[0]
    assert "reported'" not in schema.replace("outcome_reported_at", "")
    first = claim(env)
    with pytest.raises(sqlite3.IntegrityError):
        set_claim(env, first["claim_id"], state="unknown", unknown_reason="reported")
    report(env, first, "unknown")
    assert row_of(env, first)["unknown_reason"] == "runtime_unknown"
    reasons = {r["unknown_reason"] for r in claim_rows(env) if r["unknown_reason"]}
    assert reasons <= {"report_timeout", "startup_recovery", "runtime_unknown", "legacy"}


# --- O11 ---------------------------------------------------------------------------------


def test_o11_old_claim_result_is_recorded_after_binding_and_epoch_changed(env):
    made = claim(env)
    key_hash = row_of(env, made)["notification_key_hash"]
    moved = env.store.advance_binding_generation(env.room, BOB, target("bat-bob-2"), WORKSPACE)
    register_host(env.store, epoch=2)  # the instance that made the claim is superseded
    revoked_at = row_of(env, made)["key_revoked_at"]
    assert revoked_at is not None
    bindings, generations = (
        snapshot(env.store.path, "bat_bindings"),
        snapshot(env.store.path, "binding_generations"),
    )

    report(env, made, "accepted", "late-but-real")  # no epoch parameter: it is not new work

    row = row_of(env, made)
    assert (row["state"], row["evidence"]) == ("accepted", "late-but-real")
    assert (row["key_revoked_at"], row["notification_key_hash"]) == (revoked_at, key_hash)
    assert snapshot(env.store.path, "bat_bindings") == bindings  # new binding untouched
    assert snapshot(env.store.path, "binding_generations") == generations
    # The report did not change the new generation's eligibility: pending still refused, and
    # once confirmed the new binding may claim the message the old attempt reported on.
    refused(env, 409, "binding_not_confirmed", binding_id=moved["binding_id"],
            binding_version=moved["binding_version"], host_epoch=2)  # fmt: skip
    env.store.confirm_binding(moved["binding_id"], moved["binding_version"], HOST, 2)
    fresh = claim(
        env, binding_id=moved["binding_id"], binding_version=moved["binding_version"], host_epoch=2
    )
    assert fresh["claim_id"] != made["claim_id"]
    assert (
        row_of(env, made)["state"] == "accepted"
        and row_of(env, made)["key_revoked_at"] == revoked_at
    )
