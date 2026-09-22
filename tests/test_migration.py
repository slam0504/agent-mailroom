import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_mailroom.api import create_app
from agent_mailroom.store import Store


def test_v01_upgrade_backs_up_and_preserves_room_message_ids_and_auth(database):
    token_a, token_b = "a" * 43, "b" * 43
    a = hashlib.sha256(token_a.encode()).hexdigest()
    b = hashlib.sha256(token_b.encode()).hexdigest()
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE members (
                room_id TEXT NOT NULL, member_id TEXT NOT NULL, member_name TEXT NOT NULL,
                session_id TEXT, joined_at TEXT NOT NULL,
                PRIMARY KEY(room_id, member_id), UNIQUE(room_id, member_name)
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
                request_id TEXT NOT NULL, reply_to INTEGER REFERENCES messages(message_id),
                created_at TEXT NOT NULL, acknowledged_at TEXT,
                UNIQUE(room_id, sender, request_id),
                FOREIGN KEY(room_id, sender) REFERENCES members(room_id, member_name),
                FOREIGN KEY(room_id, recipient) REFERENCES members(room_id, member_name)
            );
        """)
        conn.executemany(
            "INSERT INTO members VALUES (?, ?, ?, ?, ?)",
            [
                ("old-room", a, "claude", "native-a", "2026-09-15T00:00:00+00:00"),
                ("old-room", b, "codex", None, "2026-09-15T01:00:00+00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (41, "old-room", "claude", "codex", "pending", "old-1", None, "created", None),
                (42, "old-room", "codex", "claude", "done", "old-2", 41, "created", "acked"),
            ],
        )
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        auth = {"Authorization": f"Bearer {token_b}"}
        restored = api.get("/rooms/old-room/membership", headers=auth).json()
        assert restored["room"]["room_id"] == "old-room"
        assert restored["room"]["workspace"] is None
        assert restored["member"]["member_id"] == b
        assert restored["member"]["workspace"] is None
        pending = api.get("/rooms/old-room/messages", headers=auth).json()["messages"]
        assert len(pending) == 1 and pending[0]["message_id"] == 41
        assert (
            api.get("/rooms/old-room/messages/42", headers=auth).json()["acknowledged_at"]
            == "acked"
        )
        assert (
            api.post(
                "/rooms/old-room/messages",
                headers=auth,
                json={"to": "claude", "text": "new", "request_id": "new-1", "reply_to": 41},
            ).json()["message_id"]
            == 43
        )
    backups = list(database.parent.glob("*.v1-backup-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    Store(database).initialize()
    assert len(list(database.parent.glob("*.v1-backup-*.sqlite3"))) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_newer_schema_is_rejected_without_changes(database):
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version = 99")
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="newer"):
        Store(database).initialize()
    assert database.read_bytes() == before


def test_v5_upgrade_adds_room_scoped_revocations_and_backup(database):
    store = Store(database)
    store.initialize()
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE revoked_members")
        conn.execute("PRAGMA user_version = 5")
    store.initialize()
    assert len(list(database.parent.glob("*.v5-backup-*.sqlite3"))) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'revoked_members'"
        ).fetchone()
