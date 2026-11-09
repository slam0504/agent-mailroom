"""SQLite persistence. Each operation owns its connection and transaction."""

import hashlib
import re
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .models import (
    BatRegistration,
    CreateRoomRequest,
    Inbox,
    JoinRequest,
    MailroomError,
    Member,
    Membership,
    Message,
    NotificationCredential,
    ReconnectRequest,
    RoomInfo,
    SendRequest,
)
from .prompt import notification_prompt

SCHEMA = """
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
    binding_state TEXT CHECK (binding_state IN ('pending', 'confirmed', 'rejected')),
    binding_version INTEGER CHECK (binding_version >= 0),
    host_id TEXT,
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
CREATE TABLE IF NOT EXISTS delivery_hosts (
    host_id TEXT PRIMARY KEY,
    current_epoch INTEGER NOT NULL CHECK (current_epoch >= 1),
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS binding_generations (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    highest_version INTEGER NOT NULL CHECK (highest_version >= 0),
    migration_baseline INTEGER CHECK (migration_baseline >= 0),
    PRIMARY KEY (room_id, member_id)
);
CREATE TABLE IF NOT EXISTS delivery_claims (
    claim_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL CHECK (origin IN ('normal', 'legacy')),
    message_id INTEGER NOT NULL REFERENCES messages(message_id),
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 0),
    host_id TEXT REFERENCES delivery_hosts(host_id),
    host_epoch INTEGER CHECK (host_epoch >= 1),
    state TEXT NOT NULL
        CHECK (state IN ('submitting', 'accepted', 'not_accepted', 'unknown', 'released')),
    unknown_reason TEXT
        CHECK (unknown_reason IN
            ('report_timeout', 'startup_recovery', 'runtime_unknown', 'legacy')),
    send_deadline TEXT,
    report_deadline TEXT,
    notification_key_hash TEXT,
    key_revoked_at TEXT,
    evidence TEXT,
    outcome_reported_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state != 'unknown' OR unknown_reason IS NOT NULL),
    CHECK (
        (origin = 'normal' AND host_id IS NOT NULL AND host_epoch IS NOT NULL
            AND send_deadline IS NOT NULL AND report_deadline IS NOT NULL
            AND notification_key_hash IS NOT NULL)
        OR (origin = 'legacy' AND host_id IS NULL AND host_epoch IS NULL
            AND send_deadline IS NULL AND report_deadline IS NULL
            AND state IN ('accepted', 'not_accepted', 'unknown'))
    )
);
CREATE INDEX IF NOT EXISTS delivery_claims_message ON delivery_claims(message_id, claim_seq);
CREATE INDEX IF NOT EXISTS delivery_claims_member ON delivery_claims(room_id, member_id);
CREATE UNIQUE INDEX IF NOT EXISTS delivery_claims_key_lookup
    ON delivery_claims(notification_key_hash) WHERE notification_key_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS delivery_claims_legacy_message
    ON delivery_claims(message_id) WHERE origin = 'legacy';
"""

SCHEMA_VERSION = 7
BINDING_DELIVERY_COLUMNS = {
    "binding_state": "TEXT CHECK (binding_state IN ('pending', 'confirmed', 'rejected'))",
    "binding_version": "INTEGER CHECK (binding_version >= 0)",
    "host_id": "TEXT",
}


SEND_DEADLINE = timedelta(seconds=30)
REPORT_DEADLINE = timedelta(minutes=10)
BLOCKING_STATES = ("submitting", "accepted", "not_accepted", "unknown")
OUTCOME_RESULTS = ("accepted", "not_accepted", "released", "unknown")
RELEASE_EVIDENCE = re.compile(r"busy(:[A-Za-z0-9_.-]{1,64})?|expired|not_invoked")


def new_notification_key() -> str:
    return "notice_" + secrets.token_hex(32)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        # One server owns the DB (enforced by the API lifespan's file lock).
        # Serialize lifecycle changes with the final outbound socket write, not BAT's reply.
        self.delivery_lock = threading.RLock()
        self.dispatch_binding_ids: set[str] = set()
        # Delivery deadlines and timeouts read this clock so tests can move time explicitly.
        self.clock = lambda: datetime.now(UTC)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                with conn:
                    yield conn
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise MailroomError(503, "Database busy; retry the same request.") from exc
                raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Database was created by a newer Agent Mailroom version.")
            old_members = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'members'"
            ).fetchone()
            if old_members and version < SCHEMA_VERSION:
                label = max(1, version)
                backup = self.path.with_name(
                    f"{self.path.stem}.v{label}-backup-{uuid4().hex}.sqlite3"
                )
                backup.touch(mode=0o600, exist_ok=False)
                with closing(sqlite3.connect(backup)) as target:
                    conn.backup(target)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
            if "workspace" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN workspace TEXT")
            if "state" not in columns:
                conn.execute("ALTER TABLE members ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
            # Preserve implicit v0.1 rooms and their mail; their workspace is unknown.
            conn.execute(
                "INSERT OR IGNORE INTO rooms (room_id, created_at) "
                "SELECT room_id, MIN(joined_at) FROM members GROUP BY room_id"
            )
            binding_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(bat_bindings)")
            }
            if "runtime" not in binding_columns:
                conn.execute(
                    "ALTER TABLE bat_bindings ADD COLUMN runtime TEXT NOT NULL DEFAULT 'claude' "
                    "CHECK (runtime IN ('claude', 'codex'))"
                )
            # Delivery columns stay NULL until the explicit conversion: initialize never
            # changes how a still-legacy server sees its bindings.
            for column, definition in BINDING_DELIVERY_COLUMNS.items():
                if column not in binding_columns:
                    conn.execute(f"ALTER TABLE bat_bindings ADD COLUMN {column} {definition}")
            notification_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(notifications)")
            }
            if "notification_key_hash" not in notification_columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN notification_key_hash TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS notification_key_lookup "
                "ON notifications(notification_key_hash) WHERE notification_key_hash IS NOT NULL"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def room(conn: sqlite3.Connection, room_id: str) -> RoomInfo:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        if row is None:
            raise MailroomError(404, "Room does not exist. Ask the creator for the room_id.")
        return RoomInfo(**dict(row))

    def create_room(self, member_id: str, request: CreateRoomRequest) -> Membership:
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT room_id, workspace FROM rooms WHERE creator_id = ?", (member_id,)
            ).fetchone()
            if existing:
                if existing["workspace"] != request.workspace:
                    raise MailroomError(
                        409, "This session already created a room for another workspace."
                    )
                room_id = existing["room_id"]
            else:
                room_id = f"room_{uuid4().hex}"
                conn.execute(
                    "INSERT INTO rooms VALUES (?, ?, ?, ?)",
                    (room_id, request.workspace, now(), member_id),
                )
            member = self.join_member(conn, room_id, member_id, request)
            if request.bat is not None:
                self.bind_member(conn, room_id, member_id, request.bat, member.workspace)
            return Membership(
                room=self.room(conn, room_id),
                member=member,
                bat_binding=self.member_binding(conn, room_id, member_id),
            )

    @staticmethod
    def member(conn: sqlite3.Connection, room_id: str, member_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM members WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        ).fetchone()
        if row is None:
            raise MailroomError(403, "Join this room with this identity first.")
        return row

    def join(self, room_id: str, member_id: str, request: JoinRequest) -> Member:
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            member = self.join_member(conn, room_id, member_id, request)
            if request.bat is not None:
                self.bind_member(conn, room_id, member_id, request.bat, member.workspace)
            return member

    def join_member(self, conn, room_id: str, member_id: str, request: JoinRequest) -> Member:
        room = self.room(conn, room_id)
        if conn.execute(
            "SELECT 1 FROM revoked_members WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        ).fetchone():
            raise MailroomError(403, "This previous session was replaced and cannot rejoin.")
        existing = conn.execute(
            "SELECT * FROM members WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        ).fetchone()
        if existing is not None:
            if (existing["member_name"], existing["session_id"]) != (
                request.member_name,
                request.session_id,
            ) or (request.workspace is not None and existing["workspace"] != request.workspace):
                raise MailroomError(409, "Identity already joined with different member metadata.")
            return Member(**dict(existing))
        occupied = conn.execute(
            "SELECT 1 FROM members WHERE room_id = ? AND member_name = ?",
            (room_id, request.member_name),
        ).fetchone()
        if occupied:
            raise MailroomError(409, "Member name is already registered in this room.")
        conn.execute(
            "INSERT INTO members "
            "(room_id, member_id, member_name, session_id, joined_at, workspace) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                room_id,
                member_id,
                request.member_name,
                request.session_id,
                now(),
                request.workspace if request.workspace is not None else room.workspace,
            ),
        )
        return Member(**dict(self.member(conn, room_id, member_id)))

    def reconnect(
        self,
        room_id: str,
        member_id: str,
        request: ReconnectRequest,
        validate_replacement,
    ) -> Membership:
        """Move a member to a new identity after BAT proves the old target is gone."""

        with self.delivery_lock:
            with self.connection() as conn:
                self.room(conn, room_id)
                existing = conn.execute(
                    "SELECT * FROM members WHERE room_id = ? AND member_name = ?",
                    (room_id, request.member_name),
                ).fetchone()
                if existing is None:
                    raise MailroomError(404, "Member is not registered in this room.")
                binding = self.member_binding(conn, room_id, existing["member_id"])
                if existing["member_id"] == member_id:
                    if (
                        existing["state"] == "left"
                        or (
                            request.workspace is not None
                            and request.workspace != existing["workspace"]
                        )
                        or (
                            request.session_id is not None
                            and request.session_id != existing["session_id"]
                        )
                        or binding is None
                        or (
                            binding["profile_id"],
                            binding["session_id"],
                            binding["workspace"],
                            binding["runtime"],
                        )
                        != (
                            request.bat.profile_id,
                            request.bat.session_id,
                            existing["workspace"],
                            request.bat.runtime,
                        )
                    ):
                        raise MailroomError(
                            409, "This reconnect key is already registered with different metadata."
                        )
                    return Membership(
                        room=self.room(conn, room_id),
                        member=Member(**dict(existing)),
                        bat_binding=binding,
                    )
                if existing["state"] == "left" or binding is None:
                    raise MailroomError(
                        409, "Reconnect requires an active or paused member with a BAT binding."
                    )
                if request.workspace is not None and request.workspace != existing["workspace"]:
                    raise MailroomError(409, "Reconnect workspace must match the existing member.")
                if conn.execute(
                    "SELECT 1 FROM members WHERE member_id = ? LIMIT 1", (member_id,)
                ).fetchone():
                    raise MailroomError(
                        409, "Reconnect requires a newly allocated session identity."
                    )
                if (
                    request.bat.profile_id != binding["profile_id"]
                    or request.bat.runtime != binding["runtime"]
                    or request.bat.session_id == binding["session_id"]
                ):
                    raise MailroomError(
                        409,
                        "Reconnect must use a new BAT terminal in the same profile and runtime.",
                    )
                old_member_id = existing["member_id"]
                old_binding = dict(binding)
                workspace = existing["workspace"]

            # The callback uses one authenticated BAT snapshot to prove that the old target is
            # absent and the replacement is live. Keep delivery_lock held so the retired worker
            # cannot submit another notification between that proof and the database update.
            validate_replacement(old_binding, request.bat, workspace)

            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT * FROM members WHERE room_id = ? AND member_name = ?",
                    (room_id, request.member_name),
                ).fetchone()
                current_binding = (
                    self.member_binding(conn, room_id, current["member_id"]) if current else None
                )
                if (
                    current is None
                    or current["member_id"] != old_member_id
                    or current_binding is None
                    or current_binding["binding_id"] != old_binding["binding_id"]
                ):
                    raise MailroomError(409, "Member changed while reconnecting; retry from BAT.")
                if conn.execute(
                    "SELECT 1 FROM members WHERE room_id = ? AND member_id = ?",
                    (room_id, member_id),
                ).fetchone():
                    raise MailroomError(409, "The new identity is already used in this room.")

                # Re-arm unfinished mail for the new BAT session and revoke all old notification
                # credentials. A disconnected target cannot finish those submitted prompts.
                conn.execute(
                    "DELETE FROM notifications WHERE message_id IN "
                    "(SELECT message_id FROM messages WHERE room_id = ? AND recipient = ? "
                    "AND acknowledged_at IS NULL)",
                    (room_id, request.member_name),
                )
                self.revoke_claim_grants(conn, room_id, old_member_id, at=self.stamp())
                conn.execute(
                    "DELETE FROM bat_bindings WHERE binding_id = ?",
                    (old_binding["binding_id"],),
                )
                conn.execute(
                    "INSERT INTO revoked_members (room_id, member_id, revoked_at) VALUES (?, ?, ?)",
                    (room_id, old_member_id, now()),
                )
                conn.execute(
                    "UPDATE rooms SET creator_id = ? WHERE room_id = ? AND creator_id = ?",
                    (member_id, room_id, old_member_id),
                )
                conn.execute(
                    "UPDATE members SET member_id = ?, session_id = ? "
                    "WHERE room_id = ? AND member_id = ?",
                    (
                        member_id,
                        request.session_id
                        if request.session_id is not None
                        else current["session_id"],
                        room_id,
                        old_member_id,
                    ),
                )
                self.bind_member(conn, room_id, member_id, request.bat, workspace)
                member = Member(**dict(self.member(conn, room_id, member_id)))
                return Membership(
                    room=self.room(conn, room_id),
                    member=member,
                    bat_binding=self.member_binding(conn, room_id, member_id),
                )

    def resume(self, room_id: str, member_id: str) -> Membership:
        with self.connection() as conn:
            member = Member(**dict(self.member(conn, room_id, member_id)))
            return Membership(
                room=self.room(conn, room_id),
                member=member,
                bat_binding=self.member_binding(conn, room_id, member_id),
            )

    def list_members(self, room_id: str, member_id: str) -> list[Member]:
        with self.connection() as conn:
            self.member(conn, room_id, member_id)
            rows = conn.execute(
                "SELECT * FROM members WHERE room_id = ? ORDER BY member_name", (room_id,)
            )
            return [Member(**dict(row)) for row in rows]

    def send(
        self, room_id: str, member_id: str | NotificationCredential, request: SendRequest
    ) -> Message:
        with (
            self.delivery_lock if isinstance(member_id, NotificationCredential) else nullcontext(),
            self.connection() as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
            grant = None
            if isinstance(member_id, NotificationCredential):
                grant = self.notification_grant(conn, room_id, member_id)
                if request.reply_to != grant["message_id"] or request.to != grant["sender"]:
                    raise MailroomError(
                        403, "Notification credentials may only reply to this message's sender."
                    )
                member_id = grant["member_id"]
            member = self.member(conn, room_id, member_id)
            sender = member["member_name"]
            existing = conn.execute(
                "SELECT * FROM messages WHERE room_id = ? AND sender = ? AND request_id = ?",
                (room_id, sender, request.request_id),
            ).fetchone()
            if existing:
                if (existing["recipient"], existing["text"], existing["reply_to"]) != (
                    request.to,
                    request.text,
                    request.reply_to,
                ):
                    raise MailroomError(409, "request_id was already used for different content.")
                return Message(**dict(existing))
            if grant is not None:
                if grant["acknowledged_at"] is not None:
                    raise MailroomError(
                        409, "Message already acknowledged; no new reply is allowed."
                    )
                if conn.execute(
                    "SELECT 1 FROM messages WHERE room_id = ? AND sender = ? AND reply_to = ?",
                    (room_id, sender, grant["message_id"]),
                ).fetchone():
                    raise MailroomError(
                        409, "This notification already has a reply; retry its original request_id."
                    )
            if sender == request.to:
                raise MailroomError(422, "Choose another member as the recipient.")
            if member["state"] == "left":
                raise MailroomError(
                    409, "You left this collaboration; explicitly activate to send."
                )
            recipient = conn.execute(
                "SELECT state FROM members WHERE room_id = ? AND member_name = ?",
                (room_id, request.to),
            ).fetchone()
            if not recipient:
                raise MailroomError(404, "Recipient has not joined this room.")
            if recipient["state"] == "left":
                raise MailroomError(409, "Recipient left this collaboration.")
            if request.reply_to is not None:
                original = conn.execute(
                    "SELECT 1 FROM messages WHERE message_id = ? AND room_id = ? "
                    "AND sender = ? AND recipient = ?",
                    (request.reply_to, room_id, request.to, sender),
                ).fetchone()
                if not original:
                    raise MailroomError(404, "Reply target is not a message from this recipient.")
            cursor = conn.execute(
                "INSERT INTO messages "
                "(room_id, sender, recipient, text, request_id, reply_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    sender,
                    request.to,
                    request.text,
                    request.request_id,
                    request.reply_to,
                    now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (cursor.lastrowid,)
            ).fetchone()
            return Message(**dict(row))

    def receive(self, room_id: str, member_id: str, after: int, limit: int) -> Inbox:
        with self.connection() as conn:
            recipient = self.member(conn, room_id, member_id)["member_name"]
            rows = conn.execute(
                "SELECT * FROM messages WHERE room_id = ? AND recipient = ? "
                "AND acknowledged_at IS NULL AND message_id > ? ORDER BY message_id LIMIT ?",
                (room_id, recipient, after, limit + 1),
            ).fetchall()
            messages = [Message(**dict(row)) for row in rows[:limit]]
            return Inbox(
                messages=messages,
                next_cursor=messages[-1].message_id if messages else after,
                has_more=len(rows) > limit,
            )

    def get_message(
        self, room_id: str, member_id: str | NotificationCredential, message_id: int, *, ack=False
    ) -> Message:
        with self.delivery_lock if ack else nullcontext(), self.connection() as conn:
            if ack:
                conn.execute("BEGIN IMMEDIATE")
            if isinstance(member_id, NotificationCredential):
                grant = self.notification_grant(conn, room_id, member_id)
                if grant["message_id"] != message_id:
                    raise MailroomError(
                        403, "Notification credentials only allow this notification's message."
                    )
                member_id = grant["member_id"]
            name = self.member(conn, room_id, member_id)["member_name"]
            row = conn.execute(
                "SELECT * FROM messages WHERE room_id = ? AND message_id = ?",
                (room_id, message_id),
            ).fetchone()
            if row is None or name not in (row["sender"], row["recipient"]):
                raise MailroomError(404, "Message not found for this member.")
            if ack:
                if row["recipient"] != name:
                    raise MailroomError(403, "Only the recipient can acknowledge a message.")
                conn.execute(
                    "UPDATE messages SET acknowledged_at = COALESCE(acknowledged_at, ?) "
                    "WHERE message_id = ?",
                    (now(), message_id),
                )
                row = conn.execute(
                    "SELECT * FROM messages WHERE message_id = ?", (message_id,)
                ).fetchone()
            return Message(**dict(row))

    def set_collaboration(self, room_id: str, member_id: str, state: str) -> Member:
        if state not in {"active", "paused", "left"}:
            raise MailroomError(422, "Invalid collaboration state.")
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.member(conn, room_id, member_id)
            conn.execute(
                "UPDATE members SET state = ? WHERE room_id = ? AND member_id = ?",
                (state, room_id, member_id),
            )
            if state == "left":
                self.revoke_claim_grants(conn, room_id, member_id, at=self.stamp())
                conn.execute(
                    "DELETE FROM bat_bindings WHERE room_id = ? AND member_id = ?",
                    (room_id, member_id),
                )
            return Member(**dict(self.member(conn, room_id, member_id)))

    @staticmethod
    def member_binding(conn, room_id, member_id):
        row = conn.execute(
            "SELECT * FROM bat_bindings WHERE room_id = ? AND member_id = ?",
            (room_id, member_id),
        ).fetchone()
        return dict(row) if row else None

    def require_bindable(self, conn, room_id, member_id, target: BatRegistration, workspace):
        member = self.member(conn, room_id, member_id)
        if not workspace or member["workspace"] != workspace or member["state"] == "left":
            raise MailroomError(
                409, "Binding requires an active or paused member with the exact workspace."
            )
        occupied = conn.execute(
            "SELECT 1 FROM bat_bindings WHERE room_id = ? AND profile_id = ? "
            "AND session_id = ? AND member_id != ?",
            (room_id, target.profile_id, target.session_id, member_id),
        ).fetchone()
        if occupied:
            raise MailroomError(
                409, "This BAT terminal is already bound to another member in this room."
            )

    def bind_member(self, conn, room_id, member_id, target: BatRegistration, workspace):
        """Register and bind atomically using the caller's authenticated identity."""
        self.require_bindable(conn, room_id, member_id, target, workspace)
        existing = self.member_binding(conn, room_id, member_id)
        if existing and (
            existing["profile_id"],
            existing["session_id"],
            existing["workspace"],
            existing["runtime"],
        ) == (target.profile_id, target.session_id, workspace, target.runtime):
            return existing
        self.revoke_claim_grants(conn, room_id, member_id, at=self.stamp())
        conn.execute(
            "DELETE FROM bat_bindings WHERE room_id = ? AND member_id = ?", (room_id, member_id)
        )
        conn.execute(
            "INSERT INTO bat_bindings "
            "(binding_id, room_id, member_id, profile_id, session_id, workspace, runtime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                room_id,
                member_id,
                target.profile_id,
                target.session_id,
                workspace,
                target.runtime,
            ),
        )
        return self.member_binding(conn, room_id, member_id)

    def configure_binding(
        self, room_id, member_name, profile_id, session_id, workspace, runtime="claude"
    ) -> dict:
        """Internal helper for existing bindings; HTTP callers bind through registration."""
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            member = conn.execute(
                "SELECT member_id FROM members WHERE room_id = ? AND member_name = ?",
                (room_id, member_name),
            ).fetchone()
            if member is None:
                raise MailroomError(
                    409, "Binding requires a joined member with the exact workspace."
                )
            return self.bind_member(
                conn,
                room_id,
                member["member_id"],
                BatRegistration(profile_id=profile_id, session_id=session_id, runtime=runtime),
                workspace,
            )

    def list_bindings(self):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT b.*, m.member_name FROM bat_bindings b JOIN members m "
                    "ON b.room_id = m.room_id AND b.member_id = m.member_id "
                    "WHERE m.state != 'left'"
                )
            ]

    @staticmethod
    def revoke_claim_grants(conn, room_id, member_id, *, at, claim_id=None):
        """Revoke notification grants in the caller's transaction, keeping claim history.

        Without claim_id it covers every generation of the member; key_revoked_at is only set
        where still empty, so the original revoke time survives. `at` is the caller's store-clock
        stamp. Callers invoke this before they delete or replace the binding so both land in one
        commit.
        """
        sql = (
            "UPDATE delivery_claims SET key_revoked_at = ? WHERE room_id = ? AND member_id = ? "
            "AND notification_key_hash IS NOT NULL AND key_revoked_at IS NULL"
        )
        args = [at, room_id, member_id]
        if claim_id is not None:
            sql += " AND claim_id = ?"
            args.append(claim_id)
        conn.execute(sql, args)

    @staticmethod
    def notification_grant(conn, room_id, credential, *, status_only=False):
        # A hash with a delivery_claims row is judged only by that row's key_revoked_at, with no
        # fallback to the notifications/binding join. Only hashes without a claim use that join.
        claim = conn.execute(
            "SELECT key_revoked_at FROM delivery_claims WHERE notification_key_hash = ? "
            "AND room_id = ?",
            (credential.digest, room_id),
        ).fetchone()
        if claim is not None:
            row = (
                None
                if claim["key_revoked_at"] is not None
                else conn.execute(
                    "SELECT c.member_id, c.message_id, m.sender, m.acknowledged_at, u.state "
                    "FROM delivery_claims c "
                    "JOIN members u ON u.room_id = c.room_id AND u.member_id = c.member_id "
                    "JOIN messages m ON m.message_id = c.message_id AND m.room_id = c.room_id "
                    "AND m.recipient = u.member_name "
                    "WHERE c.notification_key_hash = ? AND c.room_id = ?",
                    (credential.digest, room_id),
                ).fetchone()
            )
        else:
            row = conn.execute(
                "SELECT n.member_id, n.message_id, m.sender, m.acknowledged_at, u.state "
                "FROM notifications n JOIN bat_bindings b ON b.binding_id = n.binding_id "
                "AND b.room_id = n.room_id AND b.member_id = n.member_id "
                "JOIN members u ON u.room_id = n.room_id AND u.member_id = n.member_id "
                "JOIN messages m ON m.message_id = n.message_id AND m.room_id = n.room_id "
                "AND m.recipient = u.member_name "
                "WHERE n.notification_key_hash = ? AND n.room_id = ?",
                (credential.digest, room_id),
            ).fetchone()
        if row is None or row["state"] == "left":
            raise MailroomError(
                403, "Notification credential is invalid or its binding was revoked."
            )
        if row["state"] != "active" and not status_only:
            raise MailroomError(403, "Collaboration is paused; stop processing this notification.")
        return row

    def notification_status(self, room_id, member_id) -> dict:
        with self.connection() as conn:
            notified_message = None
            if isinstance(member_id, NotificationCredential):
                grant = self.notification_grant(conn, room_id, member_id, status_only=True)
                member_id = grant["member_id"]
                notified_message = grant["message_id"]
            member = self.member(conn, room_id, member_id)
            binding = conn.execute(
                "SELECT * FROM bat_bindings WHERE room_id = ? AND member_id = ?",
                (room_id, member_id),
            ).fetchone()
            pending = conn.execute(
                "SELECT n.* FROM messages m LEFT JOIN notifications n USING(message_id) "
                "WHERE m.room_id = ? AND m.recipient = ? AND m.acknowledged_at IS NULL "
                "AND (? IS NULL OR m.message_id = ?) "
                "ORDER BY m.message_id LIMIT 1",
                (room_id, member["member_name"], notified_message, notified_message),
            ).fetchone()
            # A message with a delivery claim reports its newest attempt (the claim table is the
            # source for converted and normal mail); only claimless mail reads the old table.
            latest = conn.execute(
                "SELECT * FROM delivery_claims WHERE room_id = ? AND member_id = ? "
                "AND message_id = (SELECT message_id FROM messages WHERE room_id = ? "
                "AND recipient = ? AND acknowledged_at IS NULL AND (? IS NULL OR message_id = ?) "
                "ORDER BY message_id LIMIT 1) ORDER BY claim_seq DESC LIMIT 1",
                (
                    room_id,
                    member_id,
                    room_id,
                    member["member_name"],
                    notified_message,
                    notified_message,
                ),
            ).fetchone()
            if latest is not None:
                attempt = {
                    k: latest[k]
                    for k in (
                        "message_id",
                        "room_id",
                        "member_id",
                        "binding_id",
                        "binding_version",
                        "state",
                        "unknown_reason",
                        "outcome_reported_at",
                        "updated_at",
                    )
                }
            elif pending and pending["message_id"]:
                attempt = {k: pending[k] for k in pending.keys() if k != "notification_key_hash"}
            else:
                attempt = None
            return {
                "state": member["state"],
                "binding": dict(binding) if binding else None,
                "worker_enabled": bool(
                    binding and binding["binding_id"] in self.dispatch_binding_ids
                ),
                "pending_notification": attempt,
            }

    def next_notification(self, binding_id) -> dict | None:
        with self.connection() as conn:
            binding = conn.execute(
                "SELECT b.*, m.member_name FROM bat_bindings b JOIN members m "
                "ON b.room_id = m.room_id AND b.member_id = m.member_id "
                "WHERE b.binding_id = ? AND m.state = 'active'",
                (binding_id,),
            ).fetchone()
            if not binding:
                return None
            message = conn.execute(
                "SELECT m.message_id, n.state AS notification_state FROM messages m "
                "LEFT JOIN notifications n USING(message_id) "
                "WHERE m.room_id = ? AND m.recipient = ? "
                "AND m.acknowledged_at IS NULL ORDER BY m.message_id LIMIT 1",
                (binding["room_id"], binding["member_name"]),
            ).fetchone()
            if not message or message["notification_state"] is not None:
                return None  # One outstanding notification; only recipient ack releases the next.
            return {**dict(binding), "message_id": message["message_id"]}

    def submit_notification(self, candidate, client_message_id, send_frame, notification_key=None):
        """Persist intent before sending. Pause/leave cannot pass this socket-write boundary."""
        with self.delivery_lock:
            current = self.next_notification(candidate["binding_id"])
            if current is None or current["message_id"] != candidate["message_id"]:
                return False
            with self.connection() as conn:
                conn.execute(
                    "INSERT INTO notifications "
                    "(message_id, room_id, member_id, binding_id, client_message_id, state, "
                    "queued, updated_at, notification_key_hash) "
                    "VALUES (?, ?, ?, ?, ?, 'submitting', NULL, ?, ?)",
                    (
                        candidate["message_id"],
                        candidate["room_id"],
                        candidate["member_id"],
                        candidate["binding_id"],
                        client_message_id,
                        now(),
                        hashlib.sha256(notification_key.encode()).hexdigest()
                        if notification_key
                        else None,
                    ),
                )
            # A crash here leaves 'submitting': startup converts it to unknown, never retries.
            send_frame()
            return True

    def finish_notification(self, message_id, state, queued=None):
        with self.connection() as conn:
            conn.execute(
                "UPDATE notifications SET state = ?, queued = ?, updated_at = ? "
                "WHERE message_id = ?",
                (state, queued, now(), message_id),
            )

    def recover_notifications(self):
        with self.connection() as conn:
            conn.execute(
                "UPDATE notifications SET state = 'unknown', updated_at = ? "
                "WHERE state = 'submitting'",
                (now(),),
            )

    def convert_legacy_to_delivery(self) -> dict:
        """Rerunnable data conversion for switching to delivery mode.

        Schema creation (initialize) never does this: a still-legacy server must keep its
        behaviour. Existing baselines and legacy attempts are never recomputed or copied again,
        so a rerun only picks up rows added by the legacy server since the previous run.
        """
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stamp = now()
            baselined = (
                "SELECT count(*) FROM binding_generations WHERE migration_baseline IS NOT NULL"
            )
            baselined_before = conn.execute(baselined).fetchone()[0]
            # The baseline is the member's highest known generation when first seen; no version
            # means 0. COALESCE keeps an already-frozen baseline.
            conn.execute(
                "INSERT INTO binding_generations (room_id, member_id, highest_version, "
                "migration_baseline) "
                "SELECT room_id, member_id, MAX(COALESCE(binding_version, 0)), "
                "MAX(COALESCE(binding_version, 0)) FROM ("
                "SELECT room_id, member_id, binding_version FROM bat_bindings "
                "UNION ALL SELECT room_id, member_id, NULL FROM notifications) "
                "WHERE true GROUP BY room_id, member_id "
                "ON CONFLICT (room_id, member_id) DO UPDATE SET "
                "highest_version = MAX(highest_version, excluded.highest_version), "
                "migration_baseline = COALESCE(migration_baseline, "
                "MAX(highest_version, excluded.highest_version))"
            )
            baselined_after = conn.execute(baselined).fetchone()[0]
            imported = conn.execute(
                "UPDATE bat_bindings SET binding_state = 'pending', "
                "binding_version = COALESCE(binding_version, 0) WHERE binding_state IS NULL"
            ).rowcount
            created = conn.execute(
                "INSERT INTO delivery_claims (claim_id, origin, message_id, room_id, member_id, "
                "recipient, binding_id, binding_version, state, unknown_reason, "
                "notification_key_hash, key_revoked_at, created_at, updated_at) "
                "SELECT 'legacy_' || n.message_id, 'legacy', n.message_id, n.room_id, n.member_id, "
                "m.recipient, n.binding_id, g.highest_version, "
                "CASE n.state WHEN 'submitting' THEN 'unknown' ELSE n.state END, "
                "CASE WHEN n.state IN ('submitting', 'unknown') THEN 'legacy' END, "
                "n.notification_key_hash, "
                "CASE WHEN n.notification_key_hash IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM bat_bindings b WHERE b.binding_id = n.binding_id "
                "AND b.room_id = n.room_id AND b.member_id = n.member_id) THEN ? END, "
                "n.updated_at, ? "
                "FROM notifications n JOIN messages m ON m.message_id = n.message_id "
                "JOIN binding_generations g ON g.room_id = n.room_id AND g.member_id = n.member_id "
                "WHERE NOT EXISTS (SELECT 1 FROM delivery_claims c "
                "WHERE c.origin = 'legacy' AND c.message_id = n.message_id)",
                (stamp, stamp),
            ).rowcount
            return {
                "bindings_imported": imported,
                "legacy_claims_created": created,
                "members_baselined": baselined_after - baselined_before,
            }

    def advance_binding_generation(
        self, room_id, member_id, target: BatRegistration, workspace
    ) -> dict:
        """Start a new pending binding generation for this member in one transaction.

        The version is max(highest known, migration baseline) + 1, so leaving or deleting a
        binding never lowers it. Notification grants of every older generation are revoked; the
        attempts themselves stay as history. The caller decides *when* a generation is due.
        """
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.require_bindable(conn, room_id, member_id, target, workspace)
            current = conn.execute(
                "SELECT highest_version, migration_baseline FROM binding_generations "
                "WHERE room_id = ? AND member_id = ?",
                (room_id, member_id),
            ).fetchone()
            existing = self.member_binding(conn, room_id, member_id)
            version = (
                max(
                    current["highest_version"] if current else 0,
                    (current["migration_baseline"] if current else None) or 0,
                    (existing["binding_version"] if existing else None) or 0,
                )
                + 1
            )
            conn.execute(
                "INSERT INTO binding_generations (room_id, member_id, highest_version) "
                "VALUES (?, ?, ?) ON CONFLICT (room_id, member_id) "
                "DO UPDATE SET highest_version = excluded.highest_version",
                (room_id, member_id, version),
            )
            self.revoke_claim_grants(conn, room_id, member_id, at=self.stamp())
            conn.execute(
                "DELETE FROM bat_bindings WHERE room_id = ? AND member_id = ?",
                (room_id, member_id),
            )
            conn.execute(
                "INSERT INTO bat_bindings (binding_id, room_id, member_id, profile_id, session_id, "
                "workspace, runtime, binding_state, binding_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    uuid4().hex,
                    room_id,
                    member_id,
                    target.profile_id,
                    target.session_id,
                    workspace,
                    target.runtime,
                    version,
                ),
            )
            return self.member_binding(conn, room_id, member_id)

    def confirm_binding(self, binding_id, binding_version, host_id, host_epoch) -> dict:
        """Pending -> confirmed for exactly this version, owned by a currently valid host."""
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            host = conn.execute(
                "SELECT current_epoch FROM delivery_hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
            if host is None:
                raise MailroomError(409, "host_not_registered")
            if host["current_epoch"] != host_epoch:
                raise MailroomError(409, "instance_superseded")
            binding = conn.execute(
                "SELECT * FROM bat_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if binding is None:
                raise MailroomError(404, "Binding does not exist.")
            if binding["binding_state"] is None or binding["binding_version"] != binding_version:
                raise MailroomError(409, "Binding version is not the current delivery version.")
            if binding["binding_state"] == "confirmed" and binding["host_id"] == host_id:
                return dict(binding)
            if binding["binding_state"] != "pending":
                raise MailroomError(409, "Binding is no longer pending.")
            conn.execute(
                "UPDATE bat_bindings SET binding_state = 'confirmed', host_id = ? "
                "WHERE binding_id = ?",
                (host_id, binding_id),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM bat_bindings WHERE binding_id = ?", (binding_id,)
                ).fetchone()
            )

    def stamp(self) -> str:
        return self.clock().isoformat(timespec="milliseconds")

    @staticmethod
    def require_current_host(conn, host_id, host_epoch):
        host = conn.execute(
            "SELECT current_epoch FROM delivery_hosts WHERE host_id = ?", (host_id,)
        ).fetchone()
        if host is None:
            raise MailroomError(409, "host_not_registered")
        if host["current_epoch"] != host_epoch:
            raise MailroomError(409, "instance_superseded")

    @staticmethod
    def claim_blocked(conn, binding, message_id) -> bool:
        """The newest attempt for this room/member/message blocks while it is unresolved.

        An attempt from an older generation does not: only an explicit newer, confirmed
        generation can move an unacknowledged message past a blocking attempt.
        """
        latest = conn.execute(
            "SELECT state, binding_version FROM delivery_claims "
            "WHERE room_id = ? AND member_id = ? AND message_id = ? "
            "ORDER BY claim_seq DESC LIMIT 1",
            (binding["room_id"], binding["member_id"], message_id),
        ).fetchone()
        return (
            latest is not None
            and latest["state"] in BLOCKING_STATES
            and latest["binding_version"] >= binding["binding_version"]
        )

    @staticmethod
    def oldest_unacknowledged(conn, room_id, recipient):
        row = conn.execute(
            "SELECT message_id FROM messages WHERE room_id = ? AND recipient = ? "
            "AND acknowledged_at IS NULL ORDER BY message_id LIMIT 1",
            (room_id, recipient),
        ).fetchone()
        return row["message_id"] if row else None

    def delivery_candidates(self, host_id, host_epoch) -> list[dict]:
        """Read-only: confirmed bindings of this host whose oldest unacked message is free."""
        with self.connection() as conn:
            self.require_current_host(conn, host_id, host_epoch)
            found = []
            for binding in conn.execute(
                "SELECT b.*, u.member_name FROM bat_bindings b JOIN members u "
                "ON u.room_id = b.room_id AND u.member_id = b.member_id "
                "WHERE b.binding_state = 'confirmed' AND b.host_id = ? AND u.state = 'active' "
                "ORDER BY b.binding_id",
                (host_id,),
            ).fetchall():
                oldest = self.oldest_unacknowledged(
                    conn, binding["room_id"], binding["member_name"]
                )
                if oldest is not None and not self.claim_blocked(conn, binding, oldest):
                    found.append(
                        {
                            "binding_id": binding["binding_id"],
                            "binding_version": binding["binding_version"],
                            "room_id": binding["room_id"],
                            "message_id": oldest,
                        }
                    )
            return found

    def claim_delivery(
        self,
        binding_id,
        binding_version,
        message_id,
        host_id,
        host_epoch,
        *,
        key_factory=new_notification_key,
    ) -> dict:
        """Re-check everything in one transaction, then persist `submitting` and mint the key.

        Nothing is written and key_factory is not called unless every check passes; the new
        notification key is stored only as a hash and appears solely inside the returned prompt.
        """
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.require_current_host(conn, host_id, host_epoch)
            binding = conn.execute(
                "SELECT * FROM bat_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if binding is None:
                raise MailroomError(409, "binding_not_current")
            if binding["binding_state"] != "confirmed":
                raise MailroomError(409, "binding_not_confirmed")
            if binding["binding_version"] != binding_version:
                raise MailroomError(409, "binding_version_mismatch")
            if binding["host_id"] != host_id:
                raise MailroomError(409, "binding_host_mismatch")
            member = self.member(conn, binding["room_id"], binding["member_id"])
            if member["state"] != "active":
                raise MailroomError(409, "member_not_active")
            message = conn.execute(
                "SELECT * FROM messages WHERE message_id = ? AND room_id = ? AND recipient = ?",
                (message_id, binding["room_id"], member["member_name"]),
            ).fetchone()
            if message is None:
                raise MailroomError(404, "Message not found for this binding.")
            if message["acknowledged_at"] is not None:
                raise MailroomError(409, "message_acknowledged")
            if self.oldest_unacknowledged(conn, binding["room_id"], member["member_name"]) != (
                message_id
            ):
                raise MailroomError(409, "message_not_oldest")
            if self.claim_blocked(conn, binding, message_id):
                raise MailroomError(409, "blocked_by_previous_claim")
            key = key_factory()
            sent = self.clock()
            claim_id = f"claim_{uuid4().hex}"
            deadlines = (
                (sent + SEND_DEADLINE).isoformat(timespec="milliseconds"),
                (sent + REPORT_DEADLINE).isoformat(timespec="milliseconds"),
            )
            conn.execute(
                "INSERT INTO delivery_claims (claim_id, origin, message_id, room_id, member_id, "
                "recipient, binding_id, binding_version, host_id, host_epoch, state, "
                "send_deadline, report_deadline, notification_key_hash, created_at, updated_at) "
                "VALUES (?, 'normal', ?, ?, ?, ?, ?, ?, ?, ?, 'submitting', ?, ?, ?, ?, ?)",
                (
                    claim_id,
                    message_id,
                    binding["room_id"],
                    binding["member_id"],
                    member["member_name"],
                    binding_id,
                    binding_version,
                    host_id,
                    host_epoch,
                    *deadlines,
                    hashlib.sha256(key.encode()).hexdigest(),
                    self.stamp(),
                    self.stamp(),
                ),
            )
            result = {
                "claim_id": claim_id,
                "binding_id": binding_id,
                "binding_version": binding_version,
                "room_id": binding["room_id"],
                "message_id": message_id,
                "send_deadline": deadlines[0],
                "report_deadline": deadlines[1],
                "prompt": notification_prompt(
                    {"room_id": binding["room_id"], "message_id": message_id}, key
                ),
            }
        return result

    def record_outcome(self, claim_id, binding_id, binding_version, result, evidence=None) -> dict:
        """Record the runtime result of exactly one claim.

        Allowed only for `submitting`, or for a `report_timeout` unknown that has not yet
        received its single late result. `outcome_reported_at` is the one marker that a runtime
        result was received. The claim's grant is revoked only by `released`.
        """
        if result not in OUTCOME_RESULTS:
            raise MailroomError(422, "unknown_outcome_result")
        if result == "released" and not (evidence and RELEASE_EVIDENCE.fullmatch(evidence)):
            raise MailroomError(422, "released_requires_not_sent_evidence")
        with self.delivery_lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = conn.execute(
                "SELECT * FROM delivery_claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if claim is None:
                raise MailroomError(404, "Claim not found.")
            if claim["origin"] == "legacy":
                raise MailroomError(409, "claim_not_accepting_outcome")
            if (claim["binding_id"], claim["binding_version"]) != (binding_id, binding_version):
                raise MailroomError(409, "binding_mismatch")
            late = claim["state"] == "unknown" and claim["unknown_reason"] == "report_timeout"
            if claim["outcome_reported_at"] is not None:
                if claim["state"] != result:
                    raise MailroomError(409, "conflicting_outcome")
                return self.public_claim(claim)
            if claim["state"] != "submitting" and not late:
                raise MailroomError(409, "claim_not_accepting_outcome")
            stamp = self.stamp()
            conn.execute(
                "UPDATE delivery_claims SET state = ?, unknown_reason = ?, evidence = ?, "
                "outcome_reported_at = ?, updated_at = ? "
                "WHERE claim_id = ?",
                (
                    result,
                    claim["unknown_reason"] or ("runtime_unknown" if result == "unknown" else None),
                    evidence,
                    stamp,
                    stamp,
                    claim_id,
                ),
            )
            if result == "released":
                self.revoke_claim_grants(
                    conn, claim["room_id"], claim["member_id"], claim_id=claim_id, at=stamp
                )
            return self.public_claim(
                conn.execute(
                    "SELECT * FROM delivery_claims WHERE claim_id = ?", (claim_id,)
                ).fetchone()
            )

    @staticmethod
    def public_claim(row) -> dict:
        return {k: row[k] for k in row.keys() if k != "notification_key_hash"}

    def expire_overdue_claims(self) -> int:
        """Past report_deadline a submitting claim becomes unknown(report_timeout), not released."""
        stamp = self.stamp()
        with self.delivery_lock, self.connection() as conn:
            return conn.execute(
                "UPDATE delivery_claims SET state = 'unknown', unknown_reason = 'report_timeout', "
                "updated_at = ? WHERE origin = 'normal' AND state = 'submitting' "
                "AND report_deadline < ?",
                (stamp, stamp),
            ).rowcount

    def recover_delivery_claims(self) -> int:
        """Server-start recovery: unfinished attempts become unknown(startup_recovery)."""
        with self.delivery_lock, self.connection() as conn:
            return conn.execute(
                "UPDATE delivery_claims SET state = 'unknown', "
                "unknown_reason = 'startup_recovery', "
                "updated_at = ? WHERE origin = 'normal' AND state = 'submitting'",
                (self.stamp(),),
            ).rowcount

    def binding_error(self, binding_id, error):
        with self.connection() as conn:
            conn.execute(
                "UPDATE bat_bindings SET last_error = ? WHERE binding_id = ?", (error, binding_id)
            )
