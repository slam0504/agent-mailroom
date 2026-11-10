"""Checkpoint A: the persisted mode marker is written in the same transaction as the conversion."""

import sqlite3

import pytest
from test_delivery_migration import build_v6, legacy_messages, legacy_notifications

from agent_mailroom.store import Store

CONVERTED_TABLES = ("bat_bindings", "binding_generations", "delivery_claims", "delivery_mode")


def dump(path):
    with sqlite3.connect(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            for table in CONVERTED_TABLES
        }


def claim_shape(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT claim_id, origin, state, unknown_reason, binding_version "
            "FROM delivery_claims ORDER BY claim_seq"
        ).fetchall()


@pytest.fixture
def legacy_db(database):
    build_v6(database, messages=legacy_messages(), notifications=legacy_notifications())
    store = Store(database)
    store.initialize()
    return store


def test_marker_table_is_a_singleton_and_empty_after_initialize(database):
    Store(database).initialize()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM delivery_mode").fetchone()[0] == 0
        conn.execute("INSERT INTO delivery_mode VALUES (1, 't')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO delivery_mode VALUES (2, 't')")


def test_initialize_and_unconverted_legacy_data_have_no_marker(legacy_db):
    assert legacy_db.delivery_mode_marker() is None
    legacy_db.initialize()
    assert legacy_db.delivery_mode_marker() is None
    assert dump(legacy_db.path)["delivery_claims"] == []


def test_conversion_writes_data_and_marker_together_and_rerun_keeps_the_first_marker(legacy_db):
    legacy_db.convert_legacy_to_delivery()
    first = legacy_db.delivery_mode_marker()
    assert first is not None and first["converted_at"]
    data = dump(legacy_db.path)
    assert data["delivery_claims"] and data["binding_generations"]
    assert {row[8] for row in data["bat_bindings"]} == {"pending"}

    legacy_db.convert_legacy_to_delivery()
    assert legacy_db.delivery_mode_marker() == first


# Failure injected by a trigger that aborts at one write of the conversion transaction, from the
# first data statement to the marker insert that comes last. Every point must roll back
# everything: "data converted, marker missing" and "marker present, data half converted" are
# both unreachable because there is no durable state between the two.
@pytest.mark.parametrize(
    "event",
    [
        "BEFORE INSERT ON binding_generations",
        "BEFORE UPDATE ON bat_bindings",
        "BEFORE INSERT ON delivery_claims",
        "BEFORE INSERT ON delivery_mode",
    ],
)
def test_failure_at_any_conversion_write_rolls_back_data_and_marker_together(legacy_db, event):
    before = dump(legacy_db.path)
    with sqlite3.connect(legacy_db.path) as conn:
        conn.execute(
            f"CREATE TRIGGER inject_failure {event} BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        legacy_db.convert_legacy_to_delivery()
    assert dump(legacy_db.path) == before
    assert legacy_db.delivery_mode_marker() is None

    with sqlite3.connect(legacy_db.path) as conn:
        conn.execute("DROP TRIGGER inject_failure")
    legacy_db.convert_legacy_to_delivery()
    after_rerun = dump(legacy_db.path)
    assert legacy_db.delivery_mode_marker() is not None

    clean = legacy_db.path.with_name("clean.sqlite3")
    build_v6(clean, messages=legacy_messages(), notifications=legacy_notifications())
    clean_store = Store(clean)
    clean_store.initialize()
    clean_store.convert_legacy_to_delivery()
    assert after_rerun["bat_bindings"] == dump(clean)["bat_bindings"]
    assert after_rerun["binding_generations"] == dump(clean)["binding_generations"]
    assert claim_shape(legacy_db.path) == claim_shape(clean)
