import os
import secrets
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_mailroom.bridge import validate_url
from agent_mailroom.identity import IdentityStore, KeyIndex, open_identity, utc_now


def _record_in_subprocess(directory: str, member_name: str) -> None:
    # Top-level and picklable: each call opens its own KeyIndex on a shared directory,
    # mirroring real bridges, which are separate OS processes, not threads.
    KeyIndex(Path(directory)).record(
        room_id="room_concurrent",
        member_name=member_name,
        runtime="claude",
        profile_id="default",
        terminal_id=member_name,
        session_key="session_" + secrets.token_hex(16),
    )


def test_registration_allocates_distinct_sessions_and_recovers_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = IdentityStore(tmp_path / "sessions")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(pool.map(lambda _: store.create(), range(8)))
    assert len({key for key, _ in sessions}) == 8
    assert len({token for _, token in sessions}) == 8
    resumed = IdentityStore(tmp_path / "sessions")
    for key, token in sessions:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(resumed.token, [key] * 8)) == [token] * 8
        if os.name == "posix":
            assert (tmp_path / "sessions" / f"{key}.json").stat().st_mode & 0o777 == 0o600


def test_missing_session_is_not_recreated_and_cannot_escape_directory(tmp_path):
    store = IdentityStore(tmp_path)
    with pytest.raises(ValueError, match="Unknown session_key") as error:
        store.token("session_" + "0" * 32)
    guidance = str(error.value)
    assert "--state-dir" in guidance
    assert "reconnect_member" in guidance and "same room_id/member_name" in guidance
    assert "new BAT terminal" in guidance
    assert "do not create_room/join_room under a new member name" in guidance
    with pytest.raises(ValueError, match="Invalid session_key"):
        store.token("../another-agent")
    assert list(tmp_path.iterdir()) == []


def test_identity_is_persistent_private_and_exclusive(tmp_path):
    path = tmp_path / "identity.json"
    with open_identity(path) as first:
        assert len(first) == 43
        with pytest.raises(ValueError, match="already in use"), open_identity(path):
            pytest.fail("Two bridges acquired the same identity")
    with open_identity(path) as second:
        assert first == second
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 1, "token": "bad"}'])
def test_corrupt_identity_is_not_silently_replaced(tmp_path, content):
    path = tmp_path / "identity.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="Invalid identity"), open_identity(path):
        pytest.fail("Invalid identity accepted")
    assert path.read_text() == content


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://example.com",
        "http://127.0.0.1:8765/path",
        "http://user:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765?token=abc",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
    ],
)
def test_bridge_does_not_send_credentials_to_arbitrary_origins(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_key_index_is_lazy_and_created_with_private_permissions_on_first_write(tmp_path):
    index = KeyIndex(tmp_path / "sessions")
    assert index.path == tmp_path / "sessions" / "key_index.sqlite3"
    # Nothing is created until the first write: notification-only processing must never
    # cause the state directory or index file to come into existence.
    assert not index.path.exists()
    assert not (tmp_path / "sessions").exists()
    assert index.lookup(runtime="claude", profile_id="default", terminal_id="x") == []
    assert not index.path.exists()

    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "a" * 32,
    )
    assert index.path.is_file()
    if os.name == "posix":
        assert index.path.stat().st_mode & 0o777 == 0o600


def test_record_upserts_rebind_and_reconnect_by_room_and_member(tmp_path):
    index = KeyIndex(tmp_path)
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "a" * 32,
    )
    # A rebind (same member, new terminal) must update the existing row, not add one.
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-2",
        session_key="session_" + "a" * 32,
    )
    rows = index.lookup(runtime="claude", profile_id="default", terminal_id="term-2")
    assert len(rows) == 1
    assert rows[0]["session_key"] == "session_" + "a" * 32
    assert index.lookup(runtime="claude", profile_id="default", terminal_id="term-1") == []

    # A reconnect (same member, new session_key) must also replace the row in place.
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-2",
        session_key="session_" + "b" * 32,
    )
    rows = index.lookup(runtime="claude", profile_id="default", terminal_id="term-2")
    assert len(rows) == 1
    assert rows[0]["session_key"] == "session_" + "b" * 32


def test_lookup_filters_by_runtime_profile_terminal_and_optional_room(tmp_path):
    index = KeyIndex(tmp_path)
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="shared-terminal",
        session_key="session_" + "1" * 32,
    )
    index.record(
        room_id="room_b",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="shared-terminal",
        session_key="session_" + "2" * 32,
    )
    both = index.lookup(runtime="claude", profile_id="default", terminal_id="shared-terminal")
    assert {row["session_key"] for row in both} == {"session_" + "1" * 32, "session_" + "2" * 32}
    scoped = index.lookup(
        runtime="claude", profile_id="default", terminal_id="shared-terminal", room_id="room_a"
    )
    assert [row["session_key"] for row in scoped] == ["session_" + "1" * 32]
    assert index.lookup(runtime="codex", profile_id="default", terminal_id="shared-terminal") == []


def test_delete_row_removes_only_that_room_and_member(tmp_path):
    index = KeyIndex(tmp_path)
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "1" * 32,
    )
    index.record(
        room_id="room_a",
        member_name="codex",
        runtime="codex",
        profile_id="default",
        terminal_id="term-2",
        session_key="session_" + "2" * 32,
    )
    index.delete_row("room_a", "claude")
    assert index.lookup(runtime="claude", profile_id="default", terminal_id="term-1") == []
    assert len(index.lookup(runtime="codex", profile_id="default", terminal_id="term-2")) == 1


def test_purge_removes_only_rooms_untouched_for_over_two_days(tmp_path):
    index = KeyIndex(tmp_path)
    now = utc_now()
    index.record(
        room_id="stale_room",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "1" * 32,
        now=now,
    )
    index.record(
        room_id="fresh_room",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-2",
        session_key="session_" + "2" * 32,
        now=now,
    )
    # Push only the stale room's last_used_at more than 2 days into the past.
    old = (datetime.fromisoformat(now) - timedelta(days=3)).isoformat(timespec="microseconds")
    with sqlite3.connect(index.path) as conn:
        conn.execute("UPDATE key_index SET last_used_at = ? WHERE room_id = ?", (old, "stale_room"))

    index.purge(now=now)

    assert index.lookup(runtime="claude", profile_id="default", terminal_id="term-1") == []
    assert len(index.lookup(runtime="claude", profile_id="default", terminal_id="term-2")) == 1


def test_touch_updates_last_used_at_for_matching_session_key(tmp_path):
    index = KeyIndex(tmp_path)
    old = "2020-01-01T00:00:00.000000+00:00"
    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "1" * 32,
        now=old,
    )
    index.touch("session_" + "1" * 32, now=utc_now())
    with sqlite3.connect(index.path) as conn:
        row = conn.execute(
            "SELECT last_used_at FROM key_index WHERE session_key = ?", ("session_" + "1" * 32,)
        ).fetchone()
    assert row[0] > old


def test_key_index_recovers_from_a_file_that_exists_without_its_table(tmp_path):
    # Simulates a process that created key_index.sqlite3 (or lost the race to create it)
    # and crashed before CREATE TABLE, or another process racing in between: the file
    # exists but has no schema. Every read/no-op path must treat that as empty, not raise.
    directory = tmp_path / "sessions"
    directory.mkdir(mode=0o700)
    path = directory / "key_index.sqlite3"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)

    index = KeyIndex(directory)
    assert index.lookup(runtime="claude", profile_id="default", terminal_id="term-1") == []
    index.touch("session_" + "a" * 32)
    index.delete_row("room_a", "claude")
    index.purge()

    index.record(
        room_id="room_a",
        member_name="claude",
        runtime="claude",
        profile_id="default",
        terminal_id="term-1",
        session_key="session_" + "a" * 32,
    )
    rows = index.lookup(runtime="claude", profile_id="default", terminal_id="term-1")
    assert len(rows) == 1
    assert rows[0]["session_key"] == "session_" + "a" * 32
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_key_index_record_survives_concurrent_creation_from_separate_processes(tmp_path):
    # Real bridges are separate OS processes, so this races the initial CREATE TABLE
    # across processes rather than threads sharing one interpreter/connection.
    directory = tmp_path / "sessions"
    members = [f"agent-{i}" for i in range(8)]
    with ProcessPoolExecutor(max_workers=8) as pool:
        list(pool.map(_record_in_subprocess, [str(directory)] * len(members), members))

    index = KeyIndex(directory)
    for member in members:
        rows = index.lookup(runtime="claude", profile_id="default", terminal_id=member)
        assert len(rows) == 1
        assert rows[0]["member_name"] == member
    if os.name == "posix":
        assert index.path.stat().st_mode & 0o777 == 0o600
