from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_mailroom.api import create_app


def headers(who):
    return {"Authorization": f"Bearer {who * 43}"}


def create(api, who="z", workspace="/work/project", **extra):
    return api.post(
        "/rooms",
        headers=headers(who),
        json={"workspace": workspace, "member_name": who, **extra},
    )


@pytest.fixture
def room(api):
    response = create(api)
    assert response.status_code == 200
    return response.json()["room"]["room_id"]


def join(api, who, room, name=None, **extra):
    return api.post(
        f"/rooms/{room}/members",
        headers=headers(who),
        json={"member_name": name or who, **extra},
    )


def send(api, room, request_id="request-1", who="a", **extra):
    return api.post(
        f"/rooms/{room}/messages",
        headers=headers(who),
        json={"to": "b", "text": "請檢查這個修改", "request_id": request_id, **extra},
    )


def test_create_assigns_room_id_and_retry_is_idempotent(api):
    first = create(api, "a", session_id="bat-session-a").json()
    assert first["room"]["room_id"].startswith("room_")
    assert first["room"]["workspace"] == "/work/project"
    assert first["member"]["workspace"] == "/work/project"
    assert first["member"]["session_id"] == "bat-session-a"
    assert create(api, "a", session_id="bat-session-a").json() == first
    assert create(api, "a", workspace="/work/other").status_code == 409
    assert create(api, "a", member_name="renamed").status_code == 409
    second = create(api, "b").json()
    assert second["room"]["room_id"] != first["room"]["room_id"]
    assert second["room"]["workspace"] == first["room"]["workspace"]


def test_join_never_creates_missing_room(api, database):
    import sqlite3

    for who in ("a", "b"):
        assert join(api, who, "room_typo").status_code == 404
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM rooms").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM members").fetchone()[0] == 0


def test_identity_and_membership_cannot_be_taken_over(api, room):
    first = join(api, "a", room, session_id="claude-session").json()
    assert first["workspace"] == "/work/project"
    assert join(api, "a", room, session_id="claude-session").json() == first
    assert join(api, "a", room, session_id="different-session").status_code == 409
    assert join(api, "a", room, name="renamed").status_code == 409
    assert join(api, "b", room, name="a").status_code == 409
    assert api.get(f"/rooms/{room}/members").status_code == 401
    assert api.get(f"/rooms/{room}/members", headers=headers("b")).status_code == 403
    assert api.get(f"/rooms/{room}/membership", headers=headers("b")).status_code == 403
    own = api.get(f"/rooms/{room}/membership", headers=headers("a")).json()
    assert own["member"] == first
    # A peer can work from a different worktree without changing the room's workspace.
    peer = join(api, "b", room, workspace="/work/other-worktree").json()
    assert peer["workspace"] == "/work/other-worktree"
    assert (
        api.get(f"/rooms/{room}/membership", headers=headers("b")).json()["room"]["workspace"]
        == "/work/project"
    )


def test_round_trip_and_acknowledgment_ownership(api, room):
    for who in ("a", "b", "c"):
        assert join(api, who, room).status_code == 200
    message = send(api, room).json()
    mid = message["message_id"]
    assert message["sender"] == "a"
    assert message["acknowledged_at"] is None
    inbox = f"/rooms/{room}/messages"
    assert api.get(inbox, headers=headers("b")).json()["messages"] == [message]
    assert api.get(inbox, headers=headers("b")).json()["messages"] == [message]
    assert api.get(inbox, headers=headers("a")).json()["messages"] == []
    assert api.get(f"{inbox}/{mid}", headers=headers("c")).status_code == 404
    assert api.post(f"{inbox}/{mid}/ack", headers=headers("a")).status_code == 403
    reply = send(api, room, "reply-1", who="b", to="a", text="檢查完成", reply_to=mid)
    assert reply.status_code == 200
    assert reply.json()["reply_to"] == mid
    ack = api.post(f"{inbox}/{mid}/ack", headers=headers("b")).json()
    assert ack["acknowledged_at"] is not None
    assert api.post(f"{inbox}/{mid}/ack", headers=headers("b")).json() == ack
    assert api.get(f"{inbox}/{mid}", headers=headers("a")).json() == ack
    assert api.get(inbox, headers=headers("b")).json()["messages"] == []
    assert api.get(inbox, headers=headers("a")).json()["messages"] == [reply.json()]


def test_concurrent_retries_store_one_message(api, room):
    join(api, "a", room)
    join(api, "b", room)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: send(api, room), range(8)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["message_id"] for response in responses}) == 1
    assert send(api, room, text="Changed content").status_code == 409
    assert len(api.get(f"/rooms/{room}/messages", headers=headers("b")).json()["messages"]) == 1


def test_concurrent_create_retry_returns_one_room(api):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: create(api), range(8)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["room"]["room_id"] for response in responses}) == 1


def test_reply_cannot_reference_another_room_or_pair(api, room):
    other = create(api, "y").json()["room"]["room_id"]
    for target in (room, other):
        for who in ("a", "b", "c"):
            join(api, who, target)
    original = send(api, room).json()["message_id"]
    assert send(api, other, "r1", who="b", to="a", reply_to=original).status_code == 404
    assert send(api, room, "r2", who="c", to="a", reply_to=original).status_code == 404
    assert send(api, room, "r3", to="a").status_code == 422
    assert send(api, room, "r4", to="missing").status_code == 404
    assert api.get(f"/rooms/{other}/messages/{original}", headers=headers("b")).status_code == 404


def test_pagination_does_not_implicitly_ack(api, room):
    join(api, "a", room)
    join(api, "b", room)
    expected = [send(api, room, f"r{i}").json()["message_id"] for i in range(3)]
    path = f"/rooms/{room}/messages"
    first = api.get(path, headers=headers("b"), params={"limit": 2}).json()
    assert [m["message_id"] for m in first["messages"]] == expected[:2]
    assert first["has_more"] is True
    second = api.get(
        path, headers=headers("b"), params={"after": first["next_cursor"], "limit": 2}
    ).json()
    assert [m["message_id"] for m in second["messages"]] == expected[2:]
    assert second["has_more"] is False
    assert len(api.get(path, headers=headers("b")).json()["messages"]) == 3


def test_restart_preserves_members_pending_mail_and_ack(database):
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        created = create(api, "a").json()
        room = created["room"]["room_id"]
        join(api, "b", room)
        done = send(api, room, "done").json()["message_id"]
        pending = send(api, room, "pending").json()
        api.post(f"/rooms/{room}/messages/{done}/ack", headers=headers("b"))
    with TestClient(create_app(database), base_url="http://127.0.0.1") as api:
        assert api.get(f"/rooms/{room}/membership", headers=headers("a")).json() == created
        assert api.get(f"/rooms/{room}/messages", headers=headers("b")).json()["messages"] == [
            pending
        ]
        assert api.get(f"/rooms/{room}/messages/{done}", headers=headers("a")).json()[
            "acknowledged_at"
        ]
        assert send(api, room, "pending").json() == pending


@pytest.mark.parametrize(
    "params", [{"wait_ms": -1}, {"wait_ms": 30001}, {"limit": 0}, {"after": -1}]
)
def test_invalid_receive_bounds(api, room, params):
    join(api, "a", room)
    assert (
        api.get(f"/rooms/{room}/messages", headers=headers("a"), params=params).status_code == 422
    )


@pytest.mark.parametrize("extra", [{"text": "  "}, {"text": "x" * 32769}, {"sender": "c"}])
def test_invalid_send_and_forged_sender_rejected(api, room, extra):
    join(api, "a", room)
    join(api, "b", room)
    assert send(api, room, **extra).status_code == 422


def test_invalid_workspace_rejected(api):
    assert create(api, workspace="  ").status_code == 422
    assert create(api, workspace="a" * 2049).status_code == 422


def test_browser_and_rebinding_requests_rejected(api):
    assert api.get("/health").status_code == 200
    assert api.get("/health", headers={"Origin": "https://example.com"}).status_code == 403
    assert api.get("/health", headers={"Host": "example.com"}).status_code == 400
