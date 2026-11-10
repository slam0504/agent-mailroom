"""Checkpoint C: upgrades to the final v7 (from v6 and from the 1a candidate v7)."""

import sqlite3

from delivery_helpers import WORKSPACE as STORE_WORKSPACE
from delivery_stack import ALICE_KEY, BOB_KEY, sha, started
from fastapi.testclient import TestClient
from test_delivery_lifecycle import cred_path
from test_delivery_migration import ROOM, backups, build_v6, snapshot, table_names, user_version

from agent_mailroom.api import create_app
from agent_mailroom.store import Store

DELIVERY_TABLES = {"delivery_credentials", "delivery_mode", "delivery_hosts", "delivery_claims"}
DELIVERY_INDEXES = {"delivery_credentials_current_host", "delivery_claims_key_lookup"}
USER_TABLES = ("rooms", "members", "messages", "bat_bindings", "notifications")


def index_names(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def legacy_v6(database):
    members = [
        (ROOM, sha(ALICE_KEY), "alice", "s-a", "t0", STORE_WORKSPACE, "active"),
        (ROOM, sha(BOB_KEY), "bob", "s-b", "t1", STORE_WORKSPACE, "active"),
    ]
    bindings = [
        ("bind-bob", ROOM, sha(BOB_KEY), "default", "bat-bob", STORE_WORKSPACE, "claude", None)
    ]
    notes = [(1, ROOM, sha(BOB_KEY), "bind-bob", "client-1", "accepted", 1, "t9", "d" * 64)]
    build_v6(database, members=members, bindings=bindings, notifications=notes)


def candidate_v7(database):
    """The 1a candidate: v7 without the credential registry, its index and the marker table.

    6a43b4b's schema differs from the final one by exactly those three objects.
    """
    legacy_v6(database)
    Store(database).initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE delivery_credentials")
        conn.execute("DROP TABLE delivery_mode")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7


def test_v6_upgrades_to_final_v7_with_backup_every_object_and_data_kept(database):
    legacy_v6(database)
    before = {t: snapshot(database, t) for t in USER_TABLES}
    Store(database).initialize()

    assert user_version(database) == 7
    (backup,) = backups(database)
    assert user_version(backup) == 6
    assert DELIVERY_TABLES <= table_names(database)
    assert DELIVERY_INDEXES <= index_names(database)
    assert {t: snapshot(database, t) for t in ("rooms", "members", "messages")} == {
        t: before[t] for t in ("rooms", "members", "messages")
    }
    assert snapshot(database, "notifications") == before["notifications"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM delivery_credentials").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM delivery_mode").fetchone()[0] == 0


def test_candidate_v7_gets_the_new_objects_without_another_backup_or_data_change(database):
    candidate_v7(database)
    (backup,) = backups(database)
    before = {t: snapshot(database, t) for t in USER_TABLES}
    assert not {"delivery_credentials", "delivery_mode"} & table_names(database)
    assert "delivery_credentials_current_host" not in index_names(database)

    Store(database).initialize()

    assert user_version(database) == 7
    assert backups(database) == [backup]  # no second backup, no re-upgrade
    assert {"delivery_credentials", "delivery_mode"} <= table_names(database)
    assert DELIVERY_INDEXES <= index_names(database)
    assert {t: snapshot(database, t) for t in USER_TABLES} == before
    Store(database).initialize()  # idempotent
    assert backups(database) == [backup]


def converted_without_marker(database):
    """A throw-away 1a candidate that already ran the conversion, which wrote no marker then."""
    candidate_v7(database)
    store = Store(database)
    store.initialize()
    store.convert_legacy_to_delivery()
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM delivery_mode")


def test_old_mode_start_on_a_candidate_database_follows_the_marker_rule(database):
    candidate_v7(database)
    with TestClient(create_app(database), base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200  # no marker: legacy start is legal
    assert Store(database).delivery_mode_marker() is None


def test_delivery_start_reruns_conversion_idempotently_and_resends_nothing(database, tmp_path):
    converted_without_marker(database)
    tables = ("bat_bindings", "binding_generations", "delivery_claims", "notifications", "messages")
    before = {t: snapshot(database, t) for t in tables}
    assert Store(database).delivery_mode_marker() is None

    with started(database, cred_path(tmp_path)) as stack:
        assert stack.sql("SELECT count(*) AS n FROM delivery_mode")[0]["n"] == 1
        assert {t: snapshot(database, t) for t in tables} == before
        epoch = stack.register()
        (binding,) = stack.pending(epoch)
        assert stack.confirm(binding, epoch).status_code == 200
        assert stack.candidates(epoch).json() == []  # the legacy attempt still blocks, no resend
    with started(database, cred_path(tmp_path)) as stack:  # another restart changes nothing more
        assert stack.sql("SELECT count(*) AS n FROM delivery_claims")[0]["n"] == 1
        assert stack.sql("SELECT count(*) AS n FROM delivery_mode")[0]["n"] == 1


def test_marked_database_is_refused_by_old_mode_after_the_rerun(database, tmp_path):
    converted_without_marker(database)
    with started(database, cred_path(tmp_path)):
        pass
    try:
        with TestClient(create_app(database)):
            raise AssertionError("legacy start must be refused")
    except RuntimeError as exc:
        assert "delivery mode" in str(exc)
