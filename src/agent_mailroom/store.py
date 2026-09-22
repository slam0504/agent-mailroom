"""SQLite persistence. Each operation owns its connection and transaction."""

import hashlib
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
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


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        # One server owns the DB (enforced by the API lifespan's file lock).
        # Serialize lifecycle changes with the final outbound socket write, not BAT's reply.
        self.delivery_lock = threading.RLock()
        self.dispatch_binding_ids: set[str] = set()

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
            if version > 6:
                raise RuntimeError("Database was created by a newer Agent Mailroom version.")
            old_members = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'members'"
            ).fetchone()
            if old_members and version < 6:
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
            notification_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(notifications)")
            }
            if "notification_key_hash" not in notification_columns:
                conn.execute("ALTER TABLE notifications ADD COLUMN notification_key_hash TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS notification_key_lookup "
                "ON notifications(notification_key_hash) WHERE notification_key_hash IS NOT NULL"
            )
            conn.execute("PRAGMA user_version = 6")

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

    def bind_member(self, conn, room_id, member_id, target: BatRegistration, workspace):
        """Register and bind atomically using the caller's authenticated identity."""
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
        existing = self.member_binding(conn, room_id, member_id)
        if existing and (
            existing["profile_id"],
            existing["session_id"],
            existing["workspace"],
            existing["runtime"],
        ) == (target.profile_id, target.session_id, workspace, target.runtime):
            return existing
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
    def notification_grant(conn, room_id, credential, *, status_only=False):
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
            return {
                "state": member["state"],
                "binding": dict(binding) if binding else None,
                "worker_enabled": bool(
                    binding and binding["binding_id"] in self.dispatch_binding_ids
                ),
                "pending_notification": {
                    k: pending[k] for k in pending.keys() if k != "notification_key_hash"
                }
                if pending and pending["message_id"]
                else None,
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

    def binding_error(self, binding_id, error):
        with self.connection() as conn:
            conn.execute(
                "UPDATE bat_bindings SET last_error = ? WHERE binding_id = ?", (error, binding_id)
            )
