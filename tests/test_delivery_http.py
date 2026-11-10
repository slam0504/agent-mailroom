"""Checkpoint B over real HTTP: B01 reconnect, B02 epoch/owner/claim, B03 authorization,
B04 binding lifecycle. Every state is reached through routes, never seeded as confirmed."""

import pytest
from delivery_stack import (
    ALICE_KEY,
    BOB_KEY,
    CAROL_KEY,
    WORKSPACE,
    add_second_host,
    auth,
    observed,
    sha,
)

BASE_TABLES = (
    "members",
    "bat_bindings",
    "binding_generations",
    "delivery_claims",
    "revoked_members",
    "delivery_hosts",
    "messages",
)


def detail(response):
    return response.status_code, response.json().get("detail")


# --- B01 -------------------------------------------------------------------------------------


def reconnect(stack, key=CAROL_KEY, session="bat-new", **override):
    room = override.pop("room")
    return stack.member(
        "POST",
        f"/rooms/{room}/members/reconnect",
        key,
        json={
            "member_name": "bob",
            "bat": {"runtime": "claude", "profile_id": "default", "session_id": session},
            **override,
        },
    )


def test_b01_keyless_reconnect_is_409_liveness_unknown_and_changes_nothing(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    before = dstack.snapshot(*BASE_TABLES)
    response = reconnect(dstack, room=room)
    assert detail(response) == (409, "liveness_unknown")
    assert dstack.snapshot(*BASE_TABLES) == before
    # the original key still works: no takeover happened
    assert dstack.status(room)["binding"]["binding_state"] == "confirmed"


def test_b01_reconnect_with_an_unusable_bearer_or_body_is_not_a_legal_request(dstack):
    room, _, _ = dstack.confirmed_bob()
    assert reconnect(dstack, key="short", room=room).status_code == 401
    bad = dstack.member(
        "POST", f"/rooms/{room}/members/reconnect", CAROL_KEY, json={"member_name": "bob"}
    )
    assert bad.status_code == 422


def test_b01_old_mode_reconnect_is_unchanged(api):
    response = api.post(
        "/rooms/room_x/members/reconnect",
        headers=auth("c" * 43),
        json={"member_name": "bob", "bat": {"runtime": "claude", "session_id": "s"}},
    )
    assert detail(response) == (
        503,
        "BAT notifications are disabled; reconnect proof is unavailable.",
    )


# --- B02 -------------------------------------------------------------------------------------


def entries(stack, binding, epoch):
    """One call per host-epoch entry, all aimed at a real binding."""
    return {
        "pending": lambda: stack.delivery(
            "GET", "/delivery/bindings", params={"state": "pending", "host_epoch": epoch}
        ),
        "confirm": lambda: stack.confirm(binding, epoch),
        "reject": lambda: stack.reject(binding, epoch),
        "candidates": lambda: stack.candidates(epoch),
        "claim": lambda: stack.claim(binding["binding_id"], binding["binding_version"], 1, epoch),
    }


def test_b02_unregistered_host_gets_host_not_registered_at_every_entry(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    (binding,) = dstack.sql("SELECT binding_id, binding_version, session_id FROM bat_bindings")
    before = dstack.snapshot(*BASE_TABLES)
    for name, call in entries(dstack, binding, 1).items():
        assert detail(call()) == (409, "host_not_registered"), name
    assert dstack.snapshot(*BASE_TABLES) == before


def test_b02_registration_issues_monotonic_epochs_and_old_epochs_are_superseded(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    assert dstack.register() == 1
    assert dstack.register() == 2
    (binding,) = dstack.sql("SELECT binding_id, binding_version, session_id FROM bat_bindings")
    before = dstack.snapshot(*BASE_TABLES)
    for name, call in entries(dstack, binding, 1).items():
        assert detail(call()) == (409, "instance_superseded"), name
    assert dstack.snapshot(*BASE_TABLES) == before
    assert dstack.sql("SELECT current_epoch FROM delivery_hosts")[0]["current_epoch"] == 2


def test_b02_pending_list_shows_only_pending_bindings_with_declared_terminal(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    epoch = dstack.register()
    (item,) = dstack.pending(epoch)
    assert set(item) == {
        "binding_id",
        "binding_version",
        "profile_id",
        "session_id",
        "runtime",
        "workspace",
    }
    assert (item["session_id"], item["runtime"], item["workspace"], item["binding_version"]) == (
        "bat-bob",
        "claude",
        WORKSPACE,
        1,
    )
    assert dstack.confirm(item, epoch).status_code == 200
    assert dstack.pending(epoch) == []  # confirmed leaves the pending list
    bad = dstack.delivery(
        "GET", "/delivery/bindings", params={"state": "confirmed", "host_epoch": epoch}
    )
    assert bad.status_code == 422


def test_b02_confirm_checks_the_observation_and_is_idempotent(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    epoch = dstack.register()
    (binding,) = dstack.pending(epoch)
    before = dstack.snapshot("bat_bindings", "binding_generations")

    for obs, expected in (
        (observed(runtime="codex"), (409, "observation_mismatch")),
        (observed(preset="codex-agent"), (409, "observation_mismatch")),
        (observed(preset="claude-cli"), (409, "observation_mismatch")),
        (observed(cwd="/elsewhere"), (409, "observation_mismatch")),
        (observed(session_id="other-terminal"), (409, "observation_not_for_binding")),
        (observed(profile_id="other"), (409, "observation_not_for_binding")),
    ):
        assert detail(dstack.confirm(binding, epoch, obs=obs)) == expected, obs
    assert dstack.snapshot("bat_bindings", "binding_generations") == before  # still pending

    stale = dstack.confirm(binding, epoch, version=binding["binding_version"] + 1)
    assert detail(stale) == (409, "Binding version is not the current delivery version.")
    missing = dstack.delivery(
        "POST",
        "/delivery/bindings/nope/confirm",
        json={"host_epoch": epoch, "binding_version": 1, "observed": observed()},
    )
    assert detail(missing) == (404, "Binding does not exist.")

    first = dstack.confirm(binding, epoch)
    assert first.status_code == 200
    assert first.json() == {
        "binding_id": binding["binding_id"],
        "binding_version": 1,  # confirm never bumps the version
        "binding_state": "confirmed",
        "host_id": dstack.host_id,
        "reason": None,
    }
    assert dstack.confirm(binding, epoch).json() == first.json()  # idempotent retry
    assert dstack.status(room)["binding"]["binding_state"] == "confirmed"


def test_b02_reject_only_on_an_explicit_mismatch_and_terminal_states_conflict(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    epoch = dstack.register()
    (binding,) = dstack.pending(epoch)

    matching = dstack.reject(binding, epoch, obs=observed())
    assert detail(matching) == (409, "observation_matches_binding")
    elsewhere = dstack.reject(binding, epoch, obs=observed(session_id="other"))
    assert detail(elsewhere) == (409, "observation_not_for_binding")
    assert dstack.status(room)["binding"]["binding_state"] == "pending"  # nothing inferred

    rejected = dstack.reject(binding, epoch, obs=observed(runtime="codex", preset="codex-agent"))
    assert rejected.status_code == 200
    body = rejected.json()
    assert (body["binding_state"], body["binding_version"]) == ("rejected", 1)
    assert body["reason"] == "observation_mismatch:runtime"
    assert dstack.reject(binding, epoch, obs=observed(cwd="/x")).json() == body  # idempotent
    assert dstack.status(room)["binding"]["last_error"] == "observation_mismatch:runtime"
    # the opposite terminal state conflicts, as does a stale version
    assert detail(dstack.confirm(binding, epoch)) == (409, "Binding is no longer pending.")
    stale = dstack.delivery(
        "POST",
        f"/delivery/bindings/{binding['binding_id']}/reject",
        json={"host_epoch": epoch, "binding_version": 9, "observed": observed(cwd="/x")},
    )
    assert detail(stale) == (409, "Binding version is not the current delivery version.")


def test_b02_reject_after_confirm_conflicts(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    response = dstack.reject(binding, epoch, obs=observed(cwd="/x"))
    assert detail(response) == (409, "Binding is no longer pending.")
    assert dstack.status(room)["binding"]["binding_state"] == "confirmed"


def test_b02_candidates_list_only_confirmed_bindings_with_their_oldest_unread(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    first = dstack.send(room, "q1")
    dstack.send(room, "q2")
    epoch = dstack.register()
    assert dstack.candidates(epoch).json() == []  # pending is not a candidate
    (binding,) = dstack.pending(epoch)
    assert dstack.confirm(binding, epoch).status_code == 200
    (candidate,) = dstack.candidates(epoch).json()
    assert candidate == {
        "binding_id": binding["binding_id"],
        "binding_version": 1,
        "room_id": room,
        "message_id": first,
    }


def test_b02_pending_binding_can_not_be_claimed_by_hand(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    message_id = dstack.send(room, "q1")
    epoch = dstack.register()
    (binding,) = dstack.pending(epoch)
    before = dstack.snapshot("delivery_claims")
    response = dstack.claim(binding["binding_id"], 1, message_id, epoch)
    assert detail(response) == (409, "binding_not_confirmed")
    assert dstack.snapshot("delivery_claims") == before


def test_b02_claim_mints_one_claim_and_stores_only_the_key_hash(dstack):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    assert set(claim) == {
        "claim_id",
        "binding_id",
        "binding_version",
        "room_id",
        "message_id",
        "send_deadline",
        "report_deadline",
        "prompt",
    }
    import re

    key = re.search(r"notice_[a-f0-9]{64}", claim["prompt"]).group(0)
    (row,) = dstack.sql("SELECT * FROM delivery_claims")
    assert (row["state"], row["host_id"], row["host_epoch"]) == (
        "submitting",
        dstack.host_id,
        epoch,
    )
    assert row["notification_key_hash"] == sha(key)
    assert key not in repr(dstack.snapshot("delivery_claims", "messages", "bat_bindings"))
    again = dstack.claim(binding["binding_id"], 1, message_id, epoch)
    assert detail(again) == (409, "blocked_by_previous_claim")
    assert len(dstack.sql("SELECT * FROM delivery_claims")) == 1


def test_b02_binding_owned_by_another_host_is_refused_with_no_writes(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    message_id = dstack.send(room, "q1")
    other = add_second_host(dstack)
    other_epoch = dstack.register(other)
    before = dstack.snapshot(*BASE_TABLES)
    response = dstack.claim(binding["binding_id"], 1, message_id, other_epoch, token=other)
    assert detail(response) == (409, "binding_host_mismatch")
    assert dstack.snapshot(*BASE_TABLES) == before


def test_b02_candidates_then_pause_makes_claim_409_without_a_claim_and_active_allows_it(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    message_id = dstack.send(room, "q1")
    (candidate,) = dstack.candidates(epoch).json()
    dstack.set_state(room, "paused")
    before = dstack.snapshot("delivery_claims")
    response = dstack.claim(candidate["binding_id"], 1, candidate["message_id"], epoch)
    assert detail(response) == (409, "member_not_active")
    assert dstack.snapshot("delivery_claims") == before  # no row, hence no key was minted
    dstack.set_state(room, "active")
    assert dstack.claim(candidate["binding_id"], 1, message_id, epoch).status_code == 200


# --- B03 -------------------------------------------------------------------------------------


def route_calls(stack, binding, headers):
    def call(method, path, **kwargs):
        return stack.client.request(method, path, headers=headers, **kwargs)

    obs = {"host_epoch": 1, "binding_version": 1, "observed": observed()}
    return {
        "pending": call("GET", "/delivery/bindings", params={"state": "pending", "host_epoch": 1}),
        "confirm": call("POST", f"/delivery/bindings/{binding}/confirm", json=obs),
        "reject": call("POST", f"/delivery/bindings/{binding}/reject", json=obs),
        "candidates": call("GET", "/delivery/candidates", params={"host_epoch": 1}),
        "claim": call(
            "POST",
            "/delivery/claims",
            json={"host_epoch": 1, "binding_id": binding, "binding_version": 1, "message_id": 1},
        ),
        "outcome": call(
            "POST",
            "/delivery/claims/claim_x/outcome",
            json={"binding_id": binding, "binding_version": 1, "result": "accepted"},
        ),
    }


def old_token(stack):
    from agent_mailroom.delivery import rotate_credential
    from agent_mailroom.store import Store

    stale = stack.token
    rotate_credential(Store(stack.database), stack.cred_file)
    return stale


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("missing", (401, "delivery_credential_required")),
        ("malformed", (401, "delivery_credential_required")),
        ("unknown", (401, "delivery_credential_rejected")),
        ("revoked", (401, "delivery_credential_rejected")),
        ("member", (401, "delivery_credential_rejected")),
        ("notification", (403, "delivery_credential_required")),
    ],
)
def test_b03_every_delivery_route_rejects_bad_credentials_without_side_effects(
    dstack, label, expected
):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    headers = {
        "missing": {},
        "malformed": {"Authorization": "Bearer too short"},
        "unknown": auth("delivery_" + "e" * 64),
        "revoked": auth(old_token(dstack)) if label == "revoked" else None,
        "member": auth(BOB_KEY),
        "notification": auth("notice_" + "f" * 64),
    }[label]
    before = dstack.snapshot(*BASE_TABLES, "delivery_credentials")
    for name, response in route_calls(dstack, binding["binding_id"], headers).items():
        assert detail(response) == expected, name
    assert dstack.snapshot(*BASE_TABLES, "delivery_credentials") == before


def test_b03_member_and_notification_tokens_can_not_manage_delivery_even_when_real(dstack):
    import re

    room, epoch, binding, message_id, claim = dstack.full_claim()
    key = re.search(r"notice_[a-f0-9]{64}", claim["prompt"]).group(0)
    before = dstack.snapshot(*BASE_TABLES)
    for token in (ALICE_KEY, BOB_KEY, key):
        for name, response in route_calls(dstack, binding["binding_id"], auth(token)).items():
            assert response.status_code in (401, 403), (name, response.status_code)
    assert dstack.snapshot(*BASE_TABLES) == before
    # the notice capability still does its message-scoped job, so BAT keeps its purpose
    status = dstack.member("GET", f"/rooms/{room}/membership/notifications", key)
    assert status.status_code == 200


def test_b03_same_host_old_epoch_may_report_but_another_valid_host_may_not(dstack):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    other = add_second_host(dstack)
    assert dstack.register(other) == 1  # real register through the real dependency
    assert dstack.register() == epoch + 1  # the claim's host moves to a newer epoch

    before = dstack.snapshot(*BASE_TABLES, "delivery_credentials")
    refused = dstack.outcome(claim, "accepted", token=other)
    assert detail(refused) == (403, "claim_host_mismatch")
    assert dstack.snapshot(*BASE_TABLES, "delivery_credentials") == before

    accepted = dstack.outcome(claim, "accepted")  # old epoch, same host: allowed
    assert accepted.status_code == 200
    row = accepted.json()
    assert (row["state"], row["host_id"], row["host_epoch"]) == (
        "accepted",
        dstack.host_id,
        epoch,
    )
    assert "notification_key_hash" not in row and row["outcome_reported_at"]
    assert dstack.outcome(claim, "accepted").json() == row  # same result is idempotent
    assert detail(dstack.outcome(claim, "unknown")) == (409, "conflicting_outcome")


def test_b03_outcome_ignores_a_body_host_and_the_current_binding_owner(dstack):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    other = add_second_host(dstack)
    dstack.register(other)
    response = dstack.outcome(claim, "accepted", token=other, host_id=dstack.host_id)
    assert response.status_code == 422  # the outcome body has no host field at all
    assert dstack.sql("SELECT state, outcome_reported_at FROM delivery_claims")[0] == {
        "state": "submitting",
        "outcome_reported_at": None,
    }


def test_b03_outcome_validation_keeps_the_1a_contract(dstack):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    assert detail(dstack.outcome(claim, "bogus")) == (422, "unknown_outcome_result")
    assert detail(dstack.outcome(claim, "released")) == (
        422,
        "released_requires_not_sent_evidence",
    )
    wrong = dstack.outcome(claim, "accepted", binding_version=99)
    assert detail(wrong) == (409, "binding_mismatch")
    gone = dstack.delivery(
        "POST",
        "/delivery/claims/claim_missing/outcome",
        json={"binding_id": "x", "binding_version": 1, "result": "accepted"},
    )
    assert detail(gone) == (404, "Claim not found.")
    released = dstack.outcome(claim, "released", evidence="not_invoked")
    assert released.status_code == 200 and released.json()["state"] == "released"


# --- B04 -------------------------------------------------------------------------------------


def binding_of(stack, room, key=BOB_KEY):
    return stack.status(room, key)["binding"]


def test_b04_new_member_join_creates_member_and_pending_binding_in_one_transaction(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200  # no BAT adapter involved
    binding = binding_of(dstack, room)
    assert (binding["binding_state"], binding["binding_version"], binding["session_id"]) == (
        "pending",
        1,
        "bat-bob",
    )
    assert dstack.sql("SELECT count(*) AS n FROM members")[0]["n"] == 2


def test_b04_create_room_with_bat_also_starts_pending(dstack):
    response = dstack.member(
        "POST",
        "/rooms",
        ALICE_KEY,
        json={
            "workspace": WORKSPACE,
            "member_name": "alice",
            "bat": {"runtime": "claude", "session_id": "bat-alice"},
        },
    )
    assert response.status_code == 200
    shown = response.json()["bat_binding"]
    assert (shown["binding_state"], shown["binding_version"]) == ("pending", 1)


@pytest.mark.parametrize("confirmed", [False, True])
def test_b04_same_key_same_target_retry_keeps_the_generation(dstack, confirmed):
    room = dstack.make_room()
    dstack.join(room)
    if confirmed:
        epoch = dstack.register()
        (binding,) = dstack.pending(epoch)
        dstack.confirm(binding, epoch)
    first = binding_of(dstack, room)
    before = dstack.snapshot("bat_bindings", "binding_generations")
    assert dstack.join(room).status_code == 200
    assert binding_of(dstack, room) == first
    assert dstack.snapshot("bat_bindings", "binding_generations") == before


def test_b04_key_rebind_to_a_new_terminal_starts_a_new_pending_generation(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    assert dstack.join(room, session="bat-new").status_code == 200
    shown = binding_of(dstack, room)
    assert (shown["binding_state"], shown["binding_version"], shown["session_id"]) == (
        "pending",
        2,
        "bat-new",
    )
    assert shown["binding_id"] != binding["binding_id"]
    # the member key never changed
    assert dstack.member("GET", f"/rooms/{room}/membership", BOB_KEY).status_code == 200
    # the old generation can no longer be confirmed
    assert detail(dstack.confirm(binding, epoch)) == (404, "Binding does not exist.")


def test_b04_rejected_binding_retried_with_the_same_target_bumps_the_version_once(dstack):
    room = dstack.make_room()
    dstack.join(room)
    epoch = dstack.register()
    (binding,) = dstack.pending(epoch)
    assert dstack.reject(binding, epoch).status_code == 200
    assert binding_of(dstack, room)["binding_state"] == "rejected"
    assert dstack.join(room).status_code == 200
    shown = binding_of(dstack, room)
    assert (shown["binding_state"], shown["binding_version"]) == ("pending", 2)
    assert dstack.join(room).status_code == 200  # now an ordinary pending retry
    assert binding_of(dstack, room)["binding_version"] == 2


def test_b04_left_member_must_be_restored_active_before_rebinding(dstack):
    room, epoch, binding = dstack.confirmed_bob()
    dstack.set_state(room, "left")
    refused = dstack.join(room)
    assert detail(refused) == (
        409,
        "Binding requires an active or paused member with the exact workspace.",
    )
    assert dstack.sql("SELECT * FROM bat_bindings") == []
    dstack.set_state(room, "active")
    assert dstack.join(room).status_code == 200
    shown = binding_of(dstack, room)
    assert (shown["binding_state"], shown["binding_version"]) == ("pending", 2)


def test_b04_failed_registrations_leave_no_half_member_or_binding(dstack):
    room = dstack.make_room()
    assert dstack.join(room).status_code == 200
    before = dstack.snapshot("members", "bat_bindings", "binding_generations")
    # terminal already bound to bob in this room
    taken = dstack.join(room, key=CAROL_KEY, name="carol", session="bat-bob")
    assert detail(taken) == (
        409,
        "This BAT terminal is already bound to another member in this room.",
    )
    assert dstack.snapshot("members", "bat_bindings", "binding_generations") == before
    # metadata change on an existing key is refused without touching the binding
    changed = dstack.join(room, session="bat-bob", workspace="/other")
    assert changed.status_code == 409
    assert dstack.snapshot("members", "bat_bindings", "binding_generations") == before
    # the failed carol attempts did not consume her name or key
    assert dstack.join(room, key=CAROL_KEY, name="carol", session="bat-c").status_code == 200
