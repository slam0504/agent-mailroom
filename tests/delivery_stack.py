"""Real-HTTP harness for the delivery control plane (credential file, TestClient, SQL helpers)."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager

from fastapi.testclient import TestClient

from agent_mailroom.api import create_app
from agent_mailroom.delivery import DeliverySettings

WORKSPACE = "/workspace"
ALICE_KEY, BOB_KEY, CAROL_KEY = "a" * 43, "b" * 43, "c" * 43


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def observed(session_id="bat-bob", **override):
    return {
        "profile_id": "default",
        "session_id": session_id,
        "runtime": "claude",
        "preset": "claude-code",
        "cwd": WORKSPACE,
        **override,
    }


class Stack:
    def __init__(self, client, database, cred_file):
        self.client, self.database, self.cred_file = client, database, cred_file

    @property
    def token(self):
        return json.loads(self.cred_file.read_text())["token"]

    @property
    def host_id(self):
        return json.loads(self.cred_file.read_text())["host_id"]

    def sql(self, query, params=()):
        with sqlite3.connect(self.database) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params)]

    def snapshot(self, *tables):
        return {t: self.sql(f"SELECT * FROM {t} ORDER BY 1, 2") for t in tables}

    def delivery(self, method, path, token=None, **kwargs):
        headers = auth(self.token if token is None else token)
        return self.client.request(method, path, headers=headers, **kwargs)

    def member(self, method, path, key, **kwargs):
        return self.client.request(method, path, headers=auth(key), **kwargs)

    def register(self, token=None):
        response = self.delivery("POST", "/delivery/hosts/register", token)
        assert response.status_code == 200, response.text
        return response.json()["host_epoch"]

    def make_room(self, key=ALICE_KEY, name="alice"):
        response = self.member(
            "POST", "/rooms", key, json={"workspace": WORKSPACE, "member_name": name}
        )
        assert response.status_code == 200, response.text
        return response.json()["room"]["room_id"]

    def join(self, room, key=BOB_KEY, name="bob", session="bat-bob", **extra):
        bat = {"runtime": "claude", "profile_id": "default", "session_id": session}
        return self.member(
            "POST",
            f"/rooms/{room}/members",
            key,
            json={"member_name": name, "bat": bat, **extra},
        )

    def send(self, room, request_id, key=ALICE_KEY, to="bob", text="UNTRUSTED-PEER-BODY"):
        response = self.member(
            "POST",
            f"/rooms/{room}/messages",
            key,
            json={"to": to, "text": text, "request_id": request_id},
        )
        assert response.status_code == 200, response.text
        return response.json()["message_id"]

    def status(self, room, key=BOB_KEY):
        response = self.member("GET", f"/rooms/{room}/membership/notifications", key)
        assert response.status_code == 200, response.text
        return response.json()

    def set_state(self, room, state, key=BOB_KEY):
        response = self.member("PUT", f"/rooms/{room}/membership/state", key, json={"state": state})
        assert response.status_code == 200, response.text

    def pending(self, epoch, token=None):
        response = self.delivery(
            "GET", "/delivery/bindings", token, params={"state": "pending", "host_epoch": epoch}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def confirm(self, binding, epoch, token=None, obs=None, version=None):
        return self.delivery(
            "POST",
            f"/delivery/bindings/{binding['binding_id']}/confirm",
            token,
            json={
                "host_epoch": epoch,
                "binding_version": binding["binding_version"] if version is None else version,
                "observed": obs or observed(binding["session_id"]),
            },
        )

    def reject(self, binding, epoch, token=None, obs=None):
        return self.delivery(
            "POST",
            f"/delivery/bindings/{binding['binding_id']}/reject",
            token,
            json={
                "host_epoch": epoch,
                "binding_version": binding["binding_version"],
                "observed": obs or observed(binding["session_id"], runtime="codex"),
            },
        )

    def candidates(self, epoch, token=None):
        return self.delivery("GET", "/delivery/candidates", token, params={"host_epoch": epoch})

    def claim(self, binding_id, version, message_id, epoch, token=None):
        return self.delivery(
            "POST",
            "/delivery/claims",
            token,
            json={
                "host_epoch": epoch,
                "binding_id": binding_id,
                "binding_version": version,
                "message_id": message_id,
            },
        )

    def outcome(self, claim, result, token=None, evidence=None, **override):
        body = {
            "binding_id": claim["binding_id"],
            "binding_version": claim["binding_version"],
            "result": result,
            "evidence": evidence,
            **override,
        }
        return self.delivery(
            "POST", f"/delivery/claims/{claim['claim_id']}/outcome", token, json=body
        )

    def confirmed_bob(self, room=None, session="bat-bob"):
        """Room with a really confirmed bob binding, driven only through HTTP."""
        room = room or self.make_room()
        assert self.join(room, session=session).status_code == 200
        epoch = self.register()
        (binding,) = [b for b in self.pending(epoch) if b["session_id"] == session]
        assert self.confirm(binding, epoch).status_code == 200
        return room, epoch, binding

    def full_claim(self, request_id="q1"):
        room, epoch, binding = self.confirmed_bob()
        message_id = self.send(room, request_id)
        response = self.claim(binding["binding_id"], binding["binding_version"], message_id, epoch)
        assert response.status_code == 200, response.text
        return room, epoch, binding, message_id, response.json()


@contextmanager
def started(database, cred_file):
    app = create_app(database, delivery=DeliverySettings(cred_file))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield Stack(client, database, cred_file)


OTHER_HOST = "host_" + "2" * 32
OTHER_TOKEN = "delivery_" + "7" * 64


def add_second_host(stack):
    """B03 fixture exception (plan option a): a second host row for authorization isolation only.

    Its token is checked by the real dependency against the registry hash; nothing else about
    the host (register, claims, outcomes) is faked.
    """
    with sqlite3.connect(stack.database) as conn:
        conn.execute(
            "INSERT INTO delivery_credentials VALUES (?, ?, 'current', 't', NULL)",
            (sha(OTHER_TOKEN), OTHER_HOST),
        )
    return OTHER_TOKEN
