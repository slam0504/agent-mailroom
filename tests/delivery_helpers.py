"""Pure helpers shared by the delivery tests (constants, clock, key counter, host/target setup)."""

from datetime import UTC, datetime, timedelta

from agent_mailroom.models import BatRegistration

WORKSPACE = "/workspace"
ALICE, BOB = "a" * 64, "b" * 64  # member_id values are key digests in production
HOST = "host_" + "1" * 32
BODY = "UNTRUSTED-PEER-BODY"


class Keys:
    """Counting notification-key generator: proves when (and how often) a key was minted."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return "notice_" + f"{self.calls:064x}"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def target(session_id):
    return BatRegistration(runtime="claude", profile_id="default", session_id=session_id)


def register_host(store, epoch=1):
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO delivery_hosts VALUES (?, ?, 't') ON CONFLICT (host_id) "
            "DO UPDATE SET current_epoch = excluded.current_epoch",
            (HOST, epoch),
        )
