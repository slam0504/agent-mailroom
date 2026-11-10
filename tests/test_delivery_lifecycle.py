"""Checkpoint B: B05 legacy/cross-generation over HTTP, B06 lifespan order, lazy expiry and the
two mode protections."""

import re
import sqlite3

import pytest
from delivery_helpers import HOST
from delivery_helpers import WORKSPACE as STORE_WORKSPACE
from delivery_stack import (
    ALICE_KEY,
    BOB_KEY,
    CAROL_KEY,
    add_second_host,
    auth,
    observed,
    sha,
    started,
)
from fastapi.testclient import TestClient
from filelock import Timeout
from test_delivery_migration import ROOM, build_v6

from agent_mailroom.api import create_app
from agent_mailroom.delivery import DeliverySettings
from agent_mailroom.store import Store

PAST = "2000-01-01T00:00:00.000+00:00"
KEY_RE = re.compile(r"notice_[a-f0-9]{64}")


def detail(response):
    return response.status_code, response.json().get("detail")


def cred_path(tmp_path):
    return tmp_path / "creds" / "delivery-credential.json"


def v6_database(database, *, accepted_message=1):
    members = [
        (ROOM, sha(ALICE_KEY), "alice", "s-a", "t0", STORE_WORKSPACE, "active"),
        (ROOM, sha(BOB_KEY), "bob", "s-b", "t1", STORE_WORKSPACE, "active"),
    ]
    bindings = [
        ("bind-bob", ROOM, sha(BOB_KEY), "default", "bat-bob", STORE_WORKSPACE, "claude", None)
    ]
    notifications = [
        (
            accepted_message,
            ROOM,
            sha(BOB_KEY),
            "bind-bob",
            "client-1",
            "accepted",
            1,
            "t9",
            "d" * 64,
        )
    ]
    build_v6(database, members=members, bindings=bindings, notifications=notifications)


# --- B05 -------------------------------------------------------------------------------------


def test_b05_first_start_converts_legacy_data_and_same_version_confirm_does_not_release(
    database, tmp_path
):
    v6_database(database)
    with started(database, cred_path(tmp_path)) as stack:
        assert stack.sql("SELECT count(*) AS n FROM delivery_mode")[0]["n"] == 1
        (claim,) = stack.sql("SELECT origin, state, binding_version FROM delivery_claims")
        assert (claim["origin"], claim["state"], claim["binding_version"]) == (
            "legacy",
            "accepted",
            0,
        )
        epoch = stack.register()
        (binding,) = stack.pending(epoch)
        assert binding["binding_version"] == 0
        assert stack.confirm(binding, epoch).status_code == 200
        assert stack.candidates(epoch).json() == []  # the legacy attempt still blocks message 1
        # the original member key is untouched by the conversion
        assert stack.member("GET", f"/rooms/{ROOM}/membership", BOB_KEY).status_code == 200


def test_b05_only_a_new_key_rebind_confirmed_over_http_releases_the_legacy_block(
    database, tmp_path
):
    v6_database(database)
    with started(database, cred_path(tmp_path)) as stack:
        epoch = stack.register()
        (binding,) = stack.pending(epoch)
        assert stack.confirm(binding, epoch).status_code == 200
        rebound = stack.join(ROOM, session="bat-new", session_id="s-b")
        assert rebound.status_code == 200
        (fresh,) = stack.pending(epoch)
        assert fresh["binding_version"] == 1  # above the migration baseline of 0
        assert stack.candidates(epoch).json() == []  # pending is not claimable
        assert stack.confirm(fresh, epoch, obs=observed("bat-new")).status_code == 200
        (candidate,) = stack.candidates(epoch).json()
        assert candidate["message_id"] == 1
        assert stack.member("GET", f"/rooms/{ROOM}/membership", BOB_KEY).status_code == 200


def test_b05_legacy_outcome_is_not_accepted_over_http(database, tmp_path):
    v6_database(database)
    with started(database, cred_path(tmp_path)) as stack:
        epoch = stack.register()
        (binding,) = stack.pending(epoch)
        before = stack.snapshot("delivery_claims")
        response = stack.delivery(
            "POST",
            "/delivery/claims/legacy_1/outcome",
            json={"binding_id": "bind-bob", "binding_version": 0, "result": "accepted"},
        )
        assert detail(response) == (409, "claim_not_accepting_outcome")
        assert stack.snapshot("delivery_claims") == before


def test_b05_late_outcome_of_an_old_generation_neither_changes_eligibility_nor_revives_grant(
    dstack,
):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    key = KEY_RE.search(claim["prompt"]).group(0)
    assert dstack.member("GET", f"/rooms/{room}/membership/notifications", key).status_code == 200
    assert dstack.join(room, session="bat-new").status_code == 200  # new generation (v2)
    revoked = dstack.member("GET", f"/rooms/{room}/membership/notifications", key)
    assert revoked.status_code == 403

    reported = dstack.outcome(claim, "accepted")  # same host, old binding generation
    assert reported.status_code == 200 and reported.json()["state"] == "accepted"
    again = dstack.member("GET", f"/rooms/{room}/membership/notifications", key)
    assert again.status_code == 403  # the grant stays revoked
    shown = dstack.status(room)["binding"]
    assert (shown["binding_state"], shown["binding_version"]) == ("pending", 2)
    assert dstack.candidates(epoch).json() == []  # still pending: eligibility unchanged


# --- B06: lifespan order, restart, conversion failure ------------------------------------------


def test_b06_a_second_server_on_the_same_database_is_refused_by_the_single_lock(database, tmp_path):
    with started(database, cred_path(tmp_path)):
        with pytest.raises(Timeout):
            with TestClient(create_app(database, delivery=DeliverySettings(cred_path(tmp_path)))):
                pass


def test_b06_startup_recovery_runs_before_any_lazy_expiry(database, tmp_path):
    with started(database, cred_path(tmp_path)) as stack:
        room, epoch, binding, message_id, claim = stack.full_claim()
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE delivery_claims SET report_deadline = ?", (PAST,))
    with started(database, cred_path(tmp_path)) as stack:
        (row,) = stack.sql("SELECT state, unknown_reason FROM delivery_claims")
        assert (row["state"], row["unknown_reason"]) == ("unknown", "startup_recovery")
        epoch = stack.register()
        assert stack.candidates(epoch).status_code == 200  # first lazy expiry changes nothing
        (row,) = stack.sql("SELECT state, unknown_reason FROM delivery_claims")
        assert (row["state"], row["unknown_reason"]) == ("unknown", "startup_recovery")
        late = stack.outcome(claim, "accepted")  # startup_recovery does not accept late results
        assert detail(late) == (409, "claim_not_accepting_outcome")


def test_b06_ordinary_restart_keeps_confirmed_generation_grants_and_queue(database, tmp_path):
    with started(database, cred_path(tmp_path)) as stack:
        room, epoch, binding, message_id, claim = stack.full_claim()
        stack.outcome(claim, "accepted")
        before = stack.snapshot(
            "bat_bindings", "binding_generations", "delivery_claims", "messages"
        )
    with started(database, cred_path(tmp_path)) as stack:
        assert (
            stack.snapshot("bat_bindings", "binding_generations", "delivery_claims", "messages")
            == before
        )
        assert stack.sql("SELECT count(*) AS n FROM notifications")[0]["n"] == 0
        assert stack.status(room)["binding"]["binding_state"] == "confirmed"


def test_b06_failed_conversion_stops_startup_changes_nothing_and_rerun_converts_once(
    database, tmp_path
):
    v6_database(database)
    Store(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON delivery_mode "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        with started(database, cred_path(tmp_path)):
            pass
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM delivery_mode").fetchone()[0] == 0
        assert conn.execute("SELECT binding_state FROM bat_bindings").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM delivery_claims").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM delivery_credentials").fetchone()[0] == 0
        conn.execute("DROP TRIGGER boom")
    with started(database, cred_path(tmp_path)) as stack:
        assert [
            r["binding_state"] for r in stack.sql("SELECT binding_state FROM bat_bindings")
        ] == ["pending"]
        assert len(stack.sql("SELECT * FROM delivery_claims")) == 1
    with started(database, cred_path(tmp_path)) as stack:  # and a further restart adds nothing
        assert len(stack.sql("SELECT * FROM delivery_claims")) == 1
        assert stack.sql("SELECT count(*) AS n FROM notifications")[0]["n"] == 1  # legacy row only


# --- first protection: marker refuses legacy startup ------------------------------------------


class WorkerBuilt(AssertionError):
    pass


@pytest.mark.parametrize("with_bat", [False, True])
def test_b06_marker_refuses_legacy_mode_before_any_worker_is_built(
    database, tmp_path, monkeypatch, with_bat
):
    with started(database, cred_path(tmp_path)):
        pass  # first delivery start writes the marker
    before = sqlite3.connect(database).execute("SELECT * FROM delivery_mode").fetchall()

    def forbidden(*args, **kwargs):
        raise WorkerBuilt

    monkeypatch.setattr("agent_mailroom.notifications.NotificationService", forbidden)
    app = create_app(database, object() if with_bat else None)
    with pytest.raises(RuntimeError, match="delivery mode"):
        with TestClient(app):
            pass
    assert sqlite3.connect(database).execute("SELECT * FROM delivery_mode").fetchall() == before


def test_b06_unconverted_legacy_database_still_starts_in_legacy_mode(database):
    v6_database(database)
    with TestClient(create_app(database), base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
    assert (
        sqlite3.connect(database).execute("SELECT count(*) FROM delivery_mode").fetchone()[0] == 0
    )


# --- lazy expiry ------------------------------------------------------------------------------


def force_overdue(stack):
    with sqlite3.connect(stack.database) as conn:
        conn.execute("UPDATE delivery_claims SET report_deadline = ?", (PAST,))


def claim_row(stack):
    (row,) = stack.sql(
        "SELECT state, unknown_reason, outcome_reported_at, updated_at FROM delivery_claims"
    )
    return row


ENTRY = {
    "candidates": lambda s, ctx: s.candidates(ctx["epoch"]),
    "claim": lambda s, ctx: s.claim(
        ctx["binding"]["binding_id"], 1, ctx["message_id"], ctx["epoch"]
    ),
    "outcome": lambda s, ctx: s.outcome(ctx["claim"], "accepted"),
    "status": lambda s, ctx: s.member(
        "GET", f"/rooms/{ctx['room']}/membership/notifications", BOB_KEY
    ),
}


@pytest.fixture
def overdue_claim(dstack):
    room, epoch, binding, message_id, claim = dstack.full_claim()
    force_overdue(dstack)
    return dstack, dict(room=room, epoch=epoch, binding=binding, message_id=message_id, claim=claim)


@pytest.mark.parametrize("entry", list(ENTRY))
def test_b06_first_authorized_entry_turns_the_overdue_claim_into_report_timeout(
    overdue_claim, entry
):
    stack, ctx = overdue_claim
    assert claim_row(stack)["state"] == "submitting"
    ENTRY[entry](stack, ctx)
    row = claim_row(stack)
    assert row["unknown_reason"] == "report_timeout"
    # outcome then accepts the one late result and keeps the historical reason
    assert row["state"] == ("accepted" if entry == "outcome" else "unknown")
    if entry != "outcome":
        late = stack.outcome(ctx["claim"], "accepted")
        assert late.status_code == 200
        after = claim_row(stack)
        assert (after["state"], after["unknown_reason"]) == ("accepted", "report_timeout")


def unauthorized(entry):
    def run(stack, ctx):
        other = add_second_host(stack)
        other_epoch = stack.register(other)
        calls = {
            "candidates": [
                lambda: stack.candidates(ctx["epoch"] + 5),
                lambda: stack.candidates(ctx["epoch"], token="delivery_" + "e" * 64),
                lambda: stack.candidates(ctx["epoch"], token=ALICE_KEY),
            ],
            "claim": [
                lambda: stack.claim(
                    ctx["binding"]["binding_id"], 1, ctx["message_id"], other_epoch, token=other
                ),
                lambda: stack.claim(
                    ctx["binding"]["binding_id"], 1, ctx["message_id"], ctx["epoch"] + 5
                ),
                lambda: stack.claim("nope", 1, ctx["message_id"], ctx["epoch"]),
            ],
            "outcome": [
                lambda: stack.outcome(ctx["claim"], "accepted", token=other),
                lambda: stack.outcome(ctx["claim"], "accepted", token="delivery_" + "e" * 64),
                lambda: stack.outcome({**ctx["claim"], "claim_id": "claim_nope"}, "accepted"),
            ],
            "status": [
                lambda: stack.member(
                    "GET", f"/rooms/{ctx['room']}/membership/notifications", CAROL_KEY
                ),
                lambda: stack.client.get(
                    f"/rooms/{ctx['room']}/membership/notifications",
                    headers=auth("notice_" + "9" * 64),
                ),
            ],
        }[entry]
        return calls

    return run


@pytest.mark.parametrize("entry", list(ENTRY))
def test_b06_unauthorized_requests_never_trigger_expiry_writes(overdue_claim, entry):
    stack, ctx = overdue_claim
    calls = unauthorized(entry)(stack, ctx)  # registers the second host first
    before = stack.snapshot("delivery_claims", "bat_bindings")
    for call in calls:
        assert call().status_code >= 400, entry
    assert stack.snapshot("delivery_claims", "bat_bindings") == before
    assert claim_row(stack)["state"] == "submitting"


def test_b06_lazy_expiry_does_not_create_claims_or_grants(overdue_claim):
    stack, ctx = overdue_claim
    stack.candidates(ctx["epoch"])
    assert len(stack.sql("SELECT * FROM delivery_claims")) == 1  # only the existing row changed
    assert stack.sql("SELECT key_revoked_at FROM delivery_claims")[0]["key_revoked_at"] is None


# --- second protection: the legacy candidate query ---------------------------------------------


def claim_for(env, message=None):
    return env.store.claim_delivery(
        binding_id=env.binding["binding_id"],
        binding_version=env.binding["binding_version"],
        message_id=(message or env.m1).message_id,
        host_id=HOST,
        host_epoch=1,
        key_factory=env.keys,
    )


def notification_rows(env):
    with env.store.connection() as conn:
        return conn.execute("SELECT count(*) FROM notifications").fetchone()[0]


def test_b06_legacy_path_without_any_claim_keeps_its_behaviour(env):
    candidate = env.store.next_notification(env.binding["binding_id"])
    assert candidate["message_id"] == env.m1.message_id
    sent = []
    assert env.store.submit_notification(candidate, "client-1", lambda: sent.append(1)) is True
    assert sent == [1] and notification_rows(env) == 1


def test_b06_accepted_normal_claim_without_a_notifications_row_has_no_legacy_candidate(env):
    made = claim_for(env)
    env.store.record_outcome(
        made["claim_id"], made["binding_id"], made["binding_version"], "accepted"
    )
    assert notification_rows(env) == 0
    assert env.store.next_notification(env.binding["binding_id"]) is None


def test_b06_released_claim_is_not_handed_back_to_the_legacy_worker(env):
    made = claim_for(env)
    env.store.record_outcome(
        made["claim_id"],
        made["binding_id"],
        made["binding_version"],
        "released",
        "not_invoked",
    )
    assert env.store.next_notification(env.binding["binding_id"]) is None


def test_b06_the_oldest_message_decides_so_a_claimed_one_does_not_let_a_newer_one_pass(env):
    claim_for(env)  # m1 claimed; m2 has nothing
    assert env.store.next_notification(env.binding["binding_id"]) is None


def test_b06_a_claim_created_after_candidates_were_read_stops_the_legacy_send(env):
    candidate = env.store.next_notification(env.binding["binding_id"])
    assert candidate["message_id"] == env.m1.message_id
    claim_for(env)  # the delivery path takes the message in between
    sent = []
    assert env.store.submit_notification(candidate, "client-1", lambda: sent.append(1)) is False
    assert sent == [] and notification_rows(env) == 0


def test_b06_every_claim_state_is_excluded(env):
    from agent_mailroom.store import OUTCOME_RESULTS

    for result in OUTCOME_RESULTS:
        made = claim_for(env)
        evidence = "not_invoked" if result == "released" else None
        env.store.record_outcome(
            made["claim_id"], made["binding_id"], made["binding_version"], result, evidence
        )
        assert env.store.next_notification(env.binding["binding_id"]) is None, result
        with env.store.connection() as conn:
            conn.execute("DELETE FROM delivery_claims")
