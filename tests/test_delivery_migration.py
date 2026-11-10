"""Phase 1a storage tests: schema v7, backup upgrade and legacy conversion (S01-S08)."""

import hashlib
import sqlite3
import stat

import pytest
from delivery_helpers import ALICE, BOB, HOST, WORKSPACE, register_host, target

import agent_mailroom.store as store_module
from agent_mailroom.models import CreateRoomRequest, JoinRequest, SendRequest
from agent_mailroom.store import Store

# Verbatim schema 6 DDL (agent-mailroom d1c2a51); fixtures must keep exercising the real v6 shape.
V6_SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    workspace TEXT,
    created_at TEXT NOT NULL,
    creator_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS members (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    member_name TEXT NOT NULL,
    session_id TEXT,
    joined_at TEXT NOT NULL,
    workspace TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused', 'left')),
    PRIMARY KEY (room_id, member_id),
    UNIQUE (room_id, member_name)
);
CREATE TABLE IF NOT EXISTS revoked_members (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    PRIMARY KEY (room_id, member_id)
);
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    request_id TEXT NOT NULL,
    reply_to INTEGER REFERENCES messages(message_id),
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE (room_id, sender, request_id),
    FOREIGN KEY (room_id, sender) REFERENCES members(room_id, member_name),
    FOREIGN KEY (room_id, recipient) REFERENCES members(room_id, member_name)
);
CREATE INDEX IF NOT EXISTS pending_messages
    ON messages(room_id, recipient, message_id) WHERE acknowledged_at IS NULL;
CREATE TABLE IF NOT EXISTS bat_bindings (
    binding_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    runtime TEXT NOT NULL DEFAULT 'claude' CHECK (runtime IN ('claude', 'codex')),
    last_error TEXT,
    UNIQUE (room_id, member_id),
    FOREIGN KEY (room_id, member_id) REFERENCES members(room_id, member_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    message_id INTEGER PRIMARY KEY REFERENCES messages(message_id),
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('submitting', 'accepted', 'unknown', 'not_accepted')),
    queued INTEGER,
    updated_at TEXT NOT NULL,
    notification_key_hash TEXT
);
"""

ROOM = "room-1"

CLAIM_COLUMNS = {
    "claim_seq", "claim_id", "origin", "message_id", "room_id", "member_id", "recipient",
    "binding_id", "binding_version", "host_id", "host_epoch", "state", "unknown_reason",
    "send_deadline", "report_deadline", "notification_key_hash", "key_revoked_at", "evidence",
    "outcome_reported_at", "created_at", "updated_at",
}  # fmt: skip


def fingerprint(rows) -> str:
    """Compare credential-bearing rows without letting a failed assert print them."""
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


def snapshot(path, table, order="1"):
    with sqlite3.connect(path) as conn:
        return fingerprint(conn.execute(f"SELECT * FROM {table} ORDER BY {order}"))


def build_v6(path, *, notifications=(), messages=None, bindings=None, members=None):
    """Create a genuine schema-6 database holding old-mode (pre delivery) data."""
    members = members or [(ROOM, ALICE, "alice", "s-a", "t0", WORKSPACE, "active"),
                          (ROOM, BOB, "bob", "s-b", "t1", WORKSPACE, "active")]  # fmt: skip
    messages = messages or [
        (1, ROOM, "alice", "bob", "first", "r1", None, "t2", None),
        (2, ROOM, "alice", "bob", "second", "r2", None, "t3", None),
        (3, ROOM, "bob", "alice", "reply", "r3", 1, "t4", "acked"),
    ]
    if bindings is None:
        bindings = [("bind-bob", ROOM, BOB, "default", "bat-bob", WORKSPACE, "claude", None)]
    with sqlite3.connect(path) as conn:
        conn.executescript(V6_SCHEMA)
        conn.execute("INSERT INTO rooms VALUES (?, ?, ?, ?)", (ROOM, WORKSPACE, "t0", ALICE))
        conn.executemany("INSERT INTO members VALUES (?, ?, ?, ?, ?, ?, ?)", members)
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", messages)
        conn.executemany("INSERT INTO bat_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", bindings)
        conn.executemany(
            "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", notifications
        )
        conn.execute("PRAGMA user_version = 6")


def user_version(path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def schema_objects(path):
    with sqlite3.connect(path) as conn:
        return sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))


def table_names(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(path, table):
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def backups(path):
    return sorted(path.parent.glob("*backup*"))


@pytest.fixture
def live(database):
    """A fresh v7 database with a room, two members, one BAT binding and one host row."""
    store = Store(database)
    store.initialize()
    store.create_room("a", CreateRoomRequest(member_name="alice", workspace=WORKSPACE))
    with store.connection() as conn:
        room = conn.execute("SELECT room_id FROM rooms").fetchone()[0]
    store.join(room, "b", JoinRequest(member_name="bob"))
    store.configure_binding(room, "bob", "default", "bat-bob", WORKSPACE)
    with store.connection() as conn:
        conn.execute("INSERT INTO delivery_hosts VALUES (?, 1, 't')", (HOST,))
    return store, room


def send(store, room, request="q1"):
    return store.send(room, "a", SendRequest(to="bob", text="body", request_id=request))


def insert_claim(conn, **values):
    row = {
        "claim_id": "c1", "origin": "normal", "message_id": 1, "room_id": ROOM, "member_id": "b",
        "recipient": "bob", "binding_id": "bind-x", "binding_version": 1, "host_id": HOST,
        "host_epoch": 1, "state": "submitting", "unknown_reason": None,
        "send_deadline": "t+30s", "report_deadline": "t+10m", "notification_key_hash": "h1",
        "created_at": "t", "updated_at": "t",
    }  # fmt: skip
    row.update(values)
    conn.execute(
        f"INSERT INTO delivery_claims ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


# --- S01 ---------------------------------------------------------------------------------


def test_s01_new_database_gets_v7_schema_without_backup_or_legacy_grants(database):
    Store(database).initialize()
    assert user_version(database) == 7
    assert {"delivery_claims", "delivery_hosts", "binding_generations"} <= table_names(database)
    assert columns(database, "delivery_claims") == CLAIM_COLUMNS
    assert {"binding_state", "binding_version", "host_id"} <= columns(database, "bat_bindings")
    assert backups(database) == []
    with sqlite3.connect(database) as conn:
        for table in ("delivery_claims", "delivery_hosts", "binding_generations"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_s01_claim_table_constraints_keep_history_and_reject_fabricated_legacy_values(live):
    store, room = live
    first, second = send(store, room), send(store, room, "q2")
    with store.connection() as conn:
        insert_claim(conn, claim_id="c1", message_id=first.message_id, room_id=room)
        # A released attempt must not stop another attempt on the same message.
        conn.execute("UPDATE delivery_claims SET state = 'released'")
        insert_claim(
            conn,
            claim_id="c2",
            message_id=first.message_id,
            room_id=room,
            notification_key_hash="h2",
        )
        indexes = conn.execute("PRAGMA index_list(delivery_claims)").fetchall()
        for index in indexes:
            if index["unique"] and not index["partial"]:
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({index['name']})")]
                assert cols != ["message_id"], "UNIQUE(message_id) would block a retry attempt"
        # History is not tied to binding rows: removing the binding keeps the claims.
        conn.execute("DELETE FROM bat_bindings")
        assert conn.execute("SELECT count(*) FROM delivery_claims").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    bad_values = [
        {"host_id": None},  # normal claims need a host
        {"host_epoch": None},
        {"send_deadline": None},
        {"report_deadline": None},
        {"notification_key_hash": None},
        {"state": "unknown", "unknown_reason": None},
        {"state": "bogus"},
        {"origin": "bogus"},
        {"origin": "legacy"},  # legacy must not carry invented host/deadline values
        {"origin": "legacy", "host_id": None, "host_epoch": None, "send_deadline": None,
         "report_deadline": None, "state": "submitting"},
        {"claim_id": "c1", "notification_key_hash": "h9"},  # duplicate primary identity
        {"claim_id": "c9", "notification_key_hash": "h2"},  # duplicate key hash
    ]  # fmt: skip
    for values in bad_values:
        with store.connection() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                insert_claim(conn, **{"message_id": second.message_id, "room_id": room, **values})
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM delivery_claims").fetchone()[0] == 2


# --- S02 ---------------------------------------------------------------------------------


def test_s02_v6_upgrade_backs_up_first_and_keeps_ids_and_credential_identity(database):
    notification = (1, ROOM, BOB, "bind-bob", "client-1", "accepted", 1, "t5", "k" * 64)
    build_v6(database, notifications=[notification])
    tables = ("rooms", "members", "messages", "bat_bindings", "notifications")
    before = {t: snapshot(database, t) for t in tables}

    Store(database).initialize()

    [backup] = backups(database)
    assert backup.name.startswith("mailroom.v6-backup-") and backup.suffix == ".sqlite3"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert user_version(backup) == 6
    assert "delivery_claims" not in table_names(backup)
    assert {t: snapshot(backup, t) for t in tables} == before
    assert user_version(database) == 7
    assert {t: snapshot(database, t) for t in ("rooms", "members", "messages")} == {
        t: before[t] for t in ("rooms", "members", "messages")
    }
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT message_id FROM messages WHERE acknowledged_at IS NULL ORDER BY 1"
        ).fetchall() == [(1,), (2,)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # Upgrade is schema-only: no legacy backfill, no pending conversion, notification intact.
        assert conn.execute("SELECT count(*) FROM delivery_claims").fetchone()[0] == 0
        assert conn.execute(
            "SELECT binding_state, binding_version, host_id FROM bat_bindings"
        ).fetchall() == [(None, None, None)]
        assert conn.execute("SELECT count(*) FROM binding_generations").fetchone()[0] == 0
    assert snapshot(database, "notifications") == before["notifications"]
    assert snapshot(database, "bat_bindings", "binding_id") != before["bat_bindings"]  # new columns

    Store(database).initialize()  # rerun of an already-v7 database never takes another backup
    assert backups(database) == [backup]


# --- S08 ---------------------------------------------------------------------------------


class _FailBeforeVersionBump(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.strip().lower().startswith("pragma user_version = 7"):
            raise RuntimeError("interrupted after every DDL statement ran")
        return super().execute(sql, *args)


class _InterruptingSqlite:
    """Proxy for the store module's sqlite3 whose connections die right before the version bump."""

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    @staticmethod
    def connect(*args, **kwargs):
        return sqlite3.connect(*args, factory=_FailBeforeVersionBump, **kwargs)


def test_s08_interrupted_upgrade_rolls_back_and_keeps_backup(database, monkeypatch):
    build_v6(
        database, notifications=[(1, ROOM, BOB, "bind-bob", "c-1", "unknown", None, "t5", None)]
    )
    objects_before = schema_objects(database)
    rows_before = {t: snapshot(database, t) for t in ("messages", "members", "notifications")}

    monkeypatch.setattr(store_module, "sqlite3", _InterruptingSqlite())
    with pytest.raises(RuntimeError, match="interrupted"):
        Store(database).initialize()
    monkeypatch.undo()

    assert user_version(database) == 6
    assert schema_objects(database) == objects_before  # no half-created tables, columns or indexes
    assert "delivery_claims" not in table_names(database)
    assert "binding_state" not in columns(database, "bat_bindings")
    assert {t: snapshot(database, t) for t in rows_before} == rows_before
    [backup] = backups(database)  # the backup taken before the attempt is kept
    assert user_version(backup) == 6

    Store(database).initialize()  # a later clean attempt completes the upgrade
    assert user_version(database) == 7
    assert {t: snapshot(database, t) for t in rows_before} == rows_before
    assert len(backups(database)) == 2 and backup in backups(database)


def test_s08_newer_schema_is_rejected_untouched_without_backup(database):
    build_v6(database)
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version = 8")
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="newer"):
        Store(database).initialize()
    assert database.read_bytes() == before
    assert backups(database) == []
    assert "delivery_claims" not in table_names(database)


# --- S03 / S04 / S07: explicit, rerunnable conversion --------------------------------------


def legacy_messages():
    rows = [(i, ROOM, "alice", "bob", f"m{i}", f"r{i}", None, f"t{i}", None) for i in range(1, 8)]
    rows[4] = (5, ROOM, "alice", "bob", "m5", "r5", None, "t5", "acked")  # already acknowledged
    return rows


def legacy_notifications():
    def row(message_id, state, binding="bind-bob", digest="d"):
        h = None if digest is None else f"{digest}{message_id}".ljust(64, "0")
        return (message_id, ROOM, BOB, binding, f"client-{message_id}", state, 1, "t9", h)

    return [
        row(1, "submitting"),
        row(2, "accepted"),
        row(3, "not_accepted"),
        row(4, "unknown"),
        row(5, "accepted"),  # acknowledged message
        row(6, "accepted", binding="bind-replaced"),  # grant already revoked by an old rebind
        row(7, "unknown", digest=None),  # very old row that never stored a key hash
    ]


@pytest.fixture
def legacy_db(database):
    build_v6(database, messages=legacy_messages(), notifications=legacy_notifications())
    store = Store(database)
    store.initialize()
    return store


def claims_by_message(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {r["message_id"]: dict(r) for r in conn.execute("SELECT * FROM delivery_claims")}


def test_s03_each_legacy_state_is_preserved_and_only_submitting_becomes_unknown(legacy_db):
    path = legacy_db.path
    notifications_before = snapshot(path, "notifications")
    with sqlite3.connect(path) as conn:
        hashes = dict(conn.execute("SELECT message_id, notification_key_hash FROM notifications"))

    summary = legacy_db.convert_legacy_to_delivery()

    assert summary == {"bindings_imported": 1, "legacy_claims_created": 7, "members_baselined": 1}
    claims = claims_by_message(path)
    assert {m: (c["state"], c["unknown_reason"]) for m, c in claims.items()} == {
        1: ("unknown", "legacy"),  # was submitting
        2: ("accepted", None),
        3: ("not_accepted", None),
        4: ("unknown", "legacy"),
        5: ("accepted", None),
        6: ("accepted", None),
        7: ("unknown", "legacy"),
    }
    for message_id, claim in claims.items():
        assert claim["origin"] == "legacy" and claim["claim_id"] == f"legacy_{message_id}"
        assert (claim["room_id"], claim["member_id"], claim["recipient"]) == (ROOM, BOB, "bob")
        assert (claim["host_id"], claim["host_epoch"]) == (None, None)
        assert (claim["send_deadline"], claim["report_deadline"]) == (None, None)
        assert claim["outcome_reported_at"] is None and claim["evidence"] is None
        # The stored hash is carried over verbatim: nothing was reissued or invented.
        assert claim["notification_key_hash"] == hashes[message_id]
    assert claims[2]["binding_id"] == "bind-bob" and claims[6]["binding_id"] == "bind-replaced"
    # A grant whose binding no longer exists was already revoked and must not be revived.
    assert claims[6]["key_revoked_at"] is not None
    assert [m for m, c in claims.items() if c["key_revoked_at"] is not None] == [6]
    assert claims[5]["state"] == "accepted"  # acked history is kept, never a new candidate
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT acknowledged_at FROM messages WHERE message_id = 5"
        ).fetchone() == ("acked",)
        assert conn.execute(
            "SELECT count(*) FROM delivery_claims WHERE origin = 'normal'"
        ).fetchone() == (0,)
    assert snapshot(path, "notifications") == notifications_before  # old-mode table untouched
    # The old worker path still treats every converted message as already notified (no resend).
    assert legacy_db.next_notification("bind-bob") is None


def test_s04_existing_binding_is_imported_pending_at_generation_zero(legacy_db):
    legacy_db.convert_legacy_to_delivery()
    with legacy_db.connection() as conn:
        binding = dict(conn.execute("SELECT * FROM bat_bindings").fetchone())
        generation = dict(conn.execute("SELECT * FROM binding_generations").fetchone())
    assert (binding["binding_state"], binding["binding_version"], binding["host_id"]) == (
        "pending", 0, None,
    )  # fmt: skip
    assert (generation["highest_version"], generation["migration_baseline"]) == (0, 0)
    assert {c["binding_version"] for c in claims_by_message(legacy_db.path).values()} == {0}


def test_s04_same_version_confirmation_does_not_lift_legacy_data_blocks(legacy_db):
    legacy_db.convert_legacy_to_delivery()
    with legacy_db.connection() as conn:
        conn.execute("INSERT INTO delivery_hosts VALUES (?, 4, 't')", (HOST,))
    claims_before = snapshot(legacy_db.path, "delivery_claims")

    confirmed = legacy_db.confirm_binding("bind-bob", 0, HOST, 4)

    assert (confirmed["binding_state"], confirmed["binding_version"], confirmed["host_id"]) == (
        "confirmed", 0, HOST,
    )  # fmt: skip
    assert snapshot(legacy_db.path, "delivery_claims") == claims_before
    with legacy_db.connection() as conn:
        highest = conn.execute("SELECT highest_version FROM binding_generations").fetchone()[0]
        legacy_versions = {
            r[0] for r in conn.execute("SELECT binding_version FROM delivery_claims")
        }
    # Legacy blocks stay >= the confirmed version, so only a strictly newer generation can lift
    # them. (Claim rejection itself is asserted once the claim operation exists: group C.)
    assert highest == 0 and min(legacy_versions) >= confirmed["binding_version"]
    assert legacy_db.confirm_binding("bind-bob", 0, HOST, 4) == confirmed  # idempotent retry


@pytest.mark.parametrize(
    ("args", "status", "detail"),
    [
        (("bind-bob", 1, HOST, 4), 409, None),  # not the binding's version
        (("bind-bob", 0, HOST, 3), 409, "instance_superseded"),
        (("bind-bob", 0, "host_" + "2" * 32, 1), 409, "host_not_registered"),
        (("missing", 0, HOST, 4), 404, None),
    ],
)
def test_confirm_binding_rejections_leave_the_binding_unchanged(legacy_db, args, status, detail):
    legacy_db.convert_legacy_to_delivery()
    with legacy_db.connection() as conn:
        conn.execute("INSERT INTO delivery_hosts VALUES (?, 4, 't')", (HOST,))
    before = snapshot(legacy_db.path, "bat_bindings")
    with pytest.raises(store_module.MailroomError) as caught:
        legacy_db.confirm_binding(*args)
    assert caught.value.status_code == status
    if detail:
        assert caught.value.detail == detail
    assert snapshot(legacy_db.path, "bat_bindings") == before


def test_confirm_before_conversion_is_rejected(legacy_db):
    with legacy_db.connection() as conn:
        conn.execute("INSERT INTO delivery_hosts VALUES (?, 4, 't')", (HOST,))
    with pytest.raises(store_module.MailroomError) as caught:
        legacy_db.confirm_binding("bind-bob", 0, HOST, 4)
    assert caught.value.status_code == 409


def test_s07_rerun_keeps_baseline_and_attempts_and_covers_late_legacy_notifications(database):
    build_v6(
        database,
        messages=[(i, ROOM, "alice", "bob", f"m{i}", f"r{i}", None, f"t{i}", None) for i in (1, 2)],
        notifications=[(1, ROOM, BOB, "bind-bob", "client-1", "accepted", 1, "t9", "e" * 64)],
    )
    store = Store(database)
    store.initialize()
    store.initialize()  # schema rerun: nothing is converted behind a legacy server's back
    assert claims_by_message(database) == {}

    # The still-legacy server keeps working after the upgrade and records new attempts.
    store.get_message(ROOM, BOB, 1, ack=True)
    candidate = store.next_notification("bind-bob")
    assert candidate["message_id"] == 2
    assert store.submit_notification(candidate, "client-late", lambda: None, "notice_late")

    first = store.convert_legacy_to_delivery()
    assert first["legacy_claims_created"] == 2  # the upgrade-time row and the post-upgrade row
    claims = claims_by_message(database)
    assert claims[2]["state"] == "unknown" and claims[2]["unknown_reason"] == "legacy"
    assert claims[2]["notification_key_hash"] == hashlib.sha256(b"notice_late").hexdigest()

    frozen = {t: snapshot(database, t) for t in ("delivery_claims", "binding_generations")}
    bindings = snapshot(database, "bat_bindings")
    assert store.convert_legacy_to_delivery() == {
        "bindings_imported": 0, "legacy_claims_created": 0, "members_baselined": 0,
    }  # fmt: skip
    store.initialize()
    assert {t: snapshot(database, t) for t in frozen} == frozen
    assert snapshot(database, "bat_bindings") == bindings

    # More old-mode activity after a first conversion is picked up on the next run only for
    # new messages; earlier attempts are not copied again.
    store.get_message(ROOM, BOB, 2, ack=True)
    store.send(ROOM, ALICE, SendRequest(to="bob", text="later", request_id="late-1"))
    later = store.next_notification("bind-bob")
    assert later["message_id"] == 3
    assert store.submit_notification(later, "client-later", lambda: None, "notice_later")
    assert store.convert_legacy_to_delivery()["legacy_claims_created"] == 1
    after = claims_by_message(database)
    assert sorted(after) == [1, 2, 3]
    assert {m: after[m] for m in (1, 2)} == {m: claims[m] for m in (1, 2)}
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT highest_version, migration_baseline FROM binding_generations"
        ).fetchall() == [(0, 0)]


# --- binding generations: S05 / S06 and their storage primitives ----------------------------


def generation(path, member_id=BOB):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT highest_version, migration_baseline FROM binding_generations "
            "WHERE member_id = ?",
            (member_id,),
        ).fetchone()


def binding_of(path, member_id=BOB):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM bat_bindings WHERE member_id = ?", (member_id,)
        ).fetchone()
        return dict(row) if row else None


def test_generation_allocates_max_of_history_plus_one_and_survives_leave(live):
    store, room = live
    old = binding_of(store.path, "b")
    assert old["binding_state"] is None  # legacy binding until a generation/conversion exists

    first = store.advance_binding_generation(room, "b", target("bat-bob-2"), WORKSPACE)
    assert (first["binding_state"], first["binding_version"], first["host_id"]) == (
        "pending", 1, None,
    )  # fmt: skip
    assert first["binding_id"] != old["binding_id"] and first["session_id"] == "bat-bob-2"
    assert binding_of(store.path, "b") == first  # the previous binding row is gone, not duplicated
    assert generation(store.path, "b") == (1, None)

    store.set_collaboration(room, "b", "left")
    assert binding_of(store.path, "b") is None
    assert generation(store.path, "b") == (1, None)  # leave/delete never resets the history
    snapshot_left = snapshot(store.path, "binding_generations")
    with pytest.raises(store_module.MailroomError) as caught:
        store.advance_binding_generation(room, "b", target("bat-bob-3"), WORKSPACE)
    assert caught.value.status_code == 409  # left members must be re-activated first
    assert snapshot(store.path, "binding_generations") == snapshot_left
    assert binding_of(store.path, "b") is None

    store.set_collaboration(room, "b", "active")
    second = store.advance_binding_generation(room, "b", target("bat-bob-3"), WORKSPACE)
    assert second["binding_version"] == 2 and generation(store.path, "b") == (2, None)
    # Another member's history is independent: its first generation is 1.
    assert (
        store.advance_binding_generation(room, "a", target("bat-alice"), WORKSPACE)[
            "binding_version"
        ]
        == 1
    )


def test_generation_revokes_only_this_members_older_grants_and_keeps_history(live):
    store, room = live
    first, second = send(store, room), send(store, room, "q2")
    with store.connection() as conn:
        common = {"room_id": room}
        insert_claim(
            conn, claim_id="c1", message_id=first.message_id, state="accepted",
            notification_key_hash="h1", **common,
        )  # fmt: skip
        insert_claim(
            conn, claim_id="c2", message_id=second.message_id, state="released",
            notification_key_hash="h2", key_revoked_at="earlier", **common,
        )  # fmt: skip
        insert_claim(
            conn, claim_id="c3", message_id=second.message_id, member_id="a", recipient="alice",
            notification_key_hash="h3", **common,
        )  # fmt: skip
    before = {r["claim_id"]: dict(r) for r in rows(store, "SELECT * FROM delivery_claims")}

    store.advance_binding_generation(room, "b", target("bat-bob-2"), WORKSPACE)

    after = {r["claim_id"]: dict(r) for r in rows(store, "SELECT * FROM delivery_claims")}
    assert after["c1"]["key_revoked_at"] is not None
    assert after["c2"]["key_revoked_at"] == "earlier"  # an existing revocation is not rewritten
    assert after["c3"] == before["c3"]  # other members are untouched
    for claim_id in ("c1", "c2"):  # only the grant changes; the attempt history stays as it was
        assert {**after[claim_id], "key_revoked_at": None} == {
            **before[claim_id],
            "key_revoked_at": None,
        }


def rows(store, sql, params=()):
    with store.connection() as conn:
        return conn.execute(sql, params).fetchall()


@pytest.mark.parametrize("failure", ["occupied_terminal", "workspace", "unknown_member"])
def test_generation_failure_changes_nothing(live, failure):
    store, room = live
    store.configure_binding(room, "alice", "default", "bat-alice", WORKSPACE)
    tables = ("bat_bindings", "binding_generations", "delivery_claims")
    before = {t: snapshot(store.path, t) for t in tables}
    args = {
        "occupied_terminal": ("b", target("bat-alice"), WORKSPACE),
        "workspace": ("b", target("bat-x"), "/elsewhere"),
        "unknown_member": ("nobody", target("bat-x"), WORKSPACE),
    }[failure]
    with pytest.raises(store_module.MailroomError) as caught:
        store.advance_binding_generation(room, *args)
    assert caught.value.status_code in (403, 409)
    assert {t: snapshot(store.path, t) for t in tables} == before


def test_s05_orphan_legacy_keeps_block_data_until_a_newer_generation_is_confirmed(database):
    left_bob = [(ROOM, ALICE, "alice", "s-a", "t0", WORKSPACE, "active"),
                (ROOM, BOB, "bob", "s-b", "t1", WORKSPACE, "left")]  # fmt: skip
    build_v6(
        database,
        members=left_bob,
        bindings=[],  # the binding was deleted when bob left; the notification row survived
        notifications=[(1, ROOM, BOB, "bind-gone", "client-1", "accepted", 1, "t9", "o" * 64)],
    )
    store = Store(database)
    store.initialize()
    assert store.convert_legacy_to_delivery() == {
        "bindings_imported": 0, "legacy_claims_created": 1, "members_baselined": 1,
    }  # fmt: skip
    legacy = claims_by_message(database)[1]
    assert (legacy["room_id"], legacy["member_id"], legacy["recipient"], legacy["message_id"]) == (
        ROOM, BOB, "bob", 1,
    )  # fmt: skip
    assert legacy["state"] == "accepted" and legacy["binding_version"] == 0
    assert legacy["key_revoked_at"] is not None and binding_of(database) is None
    assert generation(database) == (0, 0)
    legacy_rows = snapshot(database, "delivery_claims")

    with pytest.raises(store_module.MailroomError) as caught:  # left: restore active first
        store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    assert caught.value.status_code == 409 and binding_of(database) is None
    store.set_collaboration(ROOM, BOB, "active")
    register_host(store)

    created = store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    # Not claimable yet: still pending. (Group C asserts that claim refuses it.)
    assert (created["binding_state"], created["binding_version"]) == ("pending", 1)
    assert created["binding_version"] > legacy["binding_version"]
    confirmed = store.confirm_binding(created["binding_id"], 1, HOST, 1)
    assert (confirmed["binding_state"], confirmed["binding_version"]) == ("confirmed", 1)
    assert generation(database) == (1, 0)  # baseline kept, history raised
    assert snapshot(database, "delivery_claims") == legacy_rows  # the old attempt is untouched


@pytest.fixture
def bob_with_orphan_legacy(database):
    """bob has a current binding AND an older attempt whose binding was replaced (orphan)."""
    build_v6(
        database,
        notifications=[
            (1, ROOM, BOB, "bind-replaced", "client-1", "unknown", None, "t9", "p" * 64)
        ],
    )
    store = Store(database)
    store.initialize()
    store.convert_legacy_to_delivery()
    register_host(store)
    return store


def test_s06_same_version_confirmation_and_epoch_change_never_lift_the_block(
    bob_with_orphan_legacy,
):
    store = bob_with_orphan_legacy
    legacy = claims_by_message(store.path)[1]
    assert legacy["binding_id"] == "bind-replaced" and legacy["binding_version"] == 0
    binding = store.confirm_binding("bind-bob", 0, HOST, 1)
    register_host(store, epoch=2)  # a new host instance
    again = store.confirm_binding("bind-bob", 0, HOST, 2)
    assert again == binding and binding["binding_version"] == 0
    assert generation(store.path) == (0, 0)  # epoch is not a generation
    assert binding_of(store.path)["binding_version"] <= legacy["binding_version"]
    assert claims_by_message(store.path)[1] == legacy


@pytest.mark.parametrize("route", ["retarget", "leave_then_restore"])
def test_s06_only_an_explicit_newer_generation_goes_above_the_legacy_baseline(
    bob_with_orphan_legacy, route
):
    store = bob_with_orphan_legacy
    store.confirm_binding("bind-bob", 0, HOST, 1)
    legacy_rows = snapshot(store.path, "delivery_claims")
    if route == "leave_then_restore":
        store.set_collaboration(ROOM, BOB, "left")
        assert generation(store.path) == (0, 0)  # leaving does not reset or raise anything
        store.set_collaboration(ROOM, BOB, "active")
    created = store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    assert (created["binding_state"], created["binding_version"], created["host_id"]) == (
        "pending", 1, None,
    )  # fmt: skip
    confirmed = store.confirm_binding(created["binding_id"], 1, HOST, 1)
    assert confirmed["binding_state"] == "confirmed" and confirmed["binding_version"] == 1
    assert confirmed["binding_version"] > claims_by_message(store.path)[1]["binding_version"]
    assert generation(store.path) == (1, 0)
    assert snapshot(store.path, "delivery_claims") == legacy_rows


def test_s07_rerun_after_a_new_generation_keeps_baseline_and_history(bob_with_orphan_legacy):
    store = bob_with_orphan_legacy
    created = store.advance_binding_generation(ROOM, BOB, target("bat-new"), WORKSPACE)
    frozen = {
        t: snapshot(store.path, t)
        for t in ("delivery_claims", "binding_generations", "bat_bindings")
    }
    assert generation(store.path) == (1, 0)

    assert store.convert_legacy_to_delivery() == {
        "bindings_imported": 0, "legacy_claims_created": 0, "members_baselined": 0,
    }  # fmt: skip
    assert {t: snapshot(store.path, t) for t in frozen} == frozen
    assert binding_of(store.path)["binding_id"] == created["binding_id"]

    # A legacy attempt that appears late is recorded at the member's current generation, so it
    # cannot be lifted by merely confirming the generation that already exists.
    store.get_message(ROOM, BOB, 1, ack=True)
    candidate = store.next_notification(created["binding_id"])
    assert store.submit_notification(candidate, "client-late", lambda: None, "notice_late")
    assert store.convert_legacy_to_delivery()["legacy_claims_created"] == 1
    late = claims_by_message(store.path)[candidate["message_id"]]
    assert late["binding_version"] == 1 and generation(store.path) == (1, 0)

    # Deleting the binding must not let a rerun lower the stored history to what is visible now.
    store.set_collaboration(ROOM, BOB, "left")
    store.convert_legacy_to_delivery()
    assert binding_of(store.path) is None and generation(store.path) == (1, 0)
