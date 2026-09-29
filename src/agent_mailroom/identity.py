"""Automatically persisted per-registration credentials and legacy identity import."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


def utc_now() -> str:
    # Fixed-width microseconds keep lexical string ordering == chronological ordering.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _is_missing_table(exc: sqlite3.OperationalError) -> bool:
    # "file exists but table missing" (a crash or race before CREATE TABLE) must read as
    # empty/no-op, not raise; any other OperationalError is a real problem and re-raised.
    return "no such table" in str(exc)


class IdentityStore:
    """Immutable credential files, addressed by private random session keys, never member names."""

    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()

    def create(self, token: str | None = None) -> tuple[str, str]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = f"session_{uuid4().hex}"
        token = token or secrets.token_urlsafe(32)
        fd = os.open(self.directory / f"{key}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"version": 1, "token": token}, file)
            file.flush()
            os.fsync(file.fileno())
        return key, token

    def token(self, session_key: str) -> str:
        if not re.fullmatch(r"session_[a-f0-9]{32}", session_key):
            raise ValueError("Invalid session_key.")
        path = self.directory / f"{session_key}.json"
        if not path.is_file():
            raise ValueError(
                "Unknown session_key. Check that the bridge uses the original --state-dir. "
                "If you are sure this key is yours and its credential file is truly lost, "
                "use reconnect_member with the same room_id/member_name and your new BAT terminal; "
                "do not create_room/join_room under a new member name."
            )
        return load_token(path)


class KeyIndex:
    """Local, best-effort index mapping a BAT terminal ID back to its own session_key.

    Backed by a SQLite file in the bridge's state dir, shared by every bridge process on
    this machine. Never stores bearer tokens; only session_key strings (already private
    capabilities addressed by IdentityStore) and the BAT identifiers used to look them up.

    Entirely lazy: the constructor touches no disk. The directory, the database file and
    its schema are created only by record(), the first actual write. Notification-only
    processing (notification_key, never session_key) never calls record(), so it must
    never see this index directory come into existence; every other method is a no-op
    (or returns an empty result) while the file does not yet exist.
    """

    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()
        self.path = self.directory / "key_index.sqlite3"

    def _ensure_created(self) -> None:
        # Keyed on the schema, not the file: a process may create the file (or lose the
        # race to another process doing so) and crash before CREATE TABLE, leaving a file
        # that exists but has no table. Running this unconditionally, with a cheap
        # CREATE TABLE IF NOT EXISTS, makes every record() self-healing instead of
        # permanently broken.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            # Not O_EXCL: multiple bridge processes may race to create this file; losing the
            # race just means opening the file the winner created, which is fine.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        with self._connect() as conn:
            # WAL is a persistent file-level setting; setting it on every call is harmless.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS key_index (
                    room_id TEXT NOT NULL,
                    member_name TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    terminal_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    PRIMARY KEY (room_id, member_name)
                )
                """
            )

    @contextmanager
    def _connect(self):
        # A fresh connection per call, closed after use: simplest safe approach given
        # multiple OS processes touch this file concurrently.
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(
        self,
        *,
        room_id: str,
        member_name: str,
        runtime: str,
        profile_id: str,
        terminal_id: str,
        session_key: str,
        now: str | None = None,
    ) -> None:
        self._ensure_created()
        now = now if now is not None else utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO key_index
                    (room_id, member_name, runtime, profile_id, terminal_id,
                     session_key, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id, member_name) DO UPDATE SET
                    runtime = excluded.runtime,
                    profile_id = excluded.profile_id,
                    terminal_id = excluded.terminal_id,
                    session_key = excluded.session_key,
                    last_used_at = excluded.last_used_at
                """,
                (room_id, member_name, runtime, profile_id, terminal_id, session_key, now),
            )

    def touch(self, session_key: str, *, now: str | None = None) -> None:
        if not self.path.exists():
            return
        now = now if now is not None else utc_now()
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE key_index SET last_used_at = ? WHERE session_key = ?",
                    (now, session_key),
                )
        except sqlite3.OperationalError as exc:
            if not _is_missing_table(exc):
                raise

    def lookup(
        self, *, runtime: str, profile_id: str, terminal_id: str, room_id: str | None = None
    ) -> list[sqlite3.Row]:
        if not self.path.exists():
            return []
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = (
                    "SELECT * FROM key_index "
                    "WHERE runtime = ? AND profile_id = ? AND terminal_id = ?"
                )
                params: list[str] = [runtime, profile_id, terminal_id]
                if room_id is not None:
                    query += " AND room_id = ?"
                    params.append(room_id)
                return conn.execute(query, params).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_table(exc):
                return []
            raise

    def delete_row(self, room_id: str, member_name: str) -> None:
        if not self.path.exists():
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM key_index WHERE room_id = ? AND member_name = ?",
                    (room_id, member_name),
                )
        except sqlite3.OperationalError as exc:
            if not _is_missing_table(exc):
                raise

    def purge(self, *, now: str | None = None) -> None:
        if not self.path.exists():
            return
        now = now if now is not None else utc_now()
        cutoff = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat(
            timespec="microseconds"
        )
        try:
            with self._connect() as conn:
                rooms = [
                    row[0]
                    for row in conn.execute(
                        "SELECT room_id FROM key_index GROUP BY room_id "
                        "HAVING MAX(last_used_at) < ?",
                        (cutoff,),
                    ).fetchall()
                ]
                if rooms:
                    conn.executemany(
                        "DELETE FROM key_index WHERE room_id = ?", [(room,) for room in rooms]
                    )
        except sqlite3.OperationalError as exc:
            if not _is_missing_table(exc):
                raise


def load_token(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Invalid identity file; restore it instead of replacing its token."
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("token"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data["token"])
    ):
        raise ValueError("Invalid identity file; restore it instead of replacing its token.")
    return data["token"]


@contextmanager
def open_identity(path: Path):
    path = path.expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError("Identity is already in use by another MCP bridge.") from exc
    try:
        if not path.exists():
            token = secrets.token_urlsafe(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump({"version": 1, "token": token}, file)
                file.flush()
                os.fsync(file.fileno())
        token = load_token(path)
        os.chmod(path, 0o600)
        yield token
    finally:
        lock.release()
