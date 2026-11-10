"""Checkpoint A: delivery principal authorization over real HTTP, the one register route, and the
member/delivery token namespace compatibility decision."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_mailroom.api import create_app
from agent_mailroom.delivery import (
    CredentialFileError,
    DeliverySettings,
    digest,
    ensure_credential,
    rotate_credential,
)
from agent_mailroom.models import (
    BatRegistration,
    CreateRoomRequest,
    MailroomError,
    ReconnectRequest,
)
from agent_mailroom.store import Store

REGISTER = "/delivery/hosts/register"
MEMBER_REGEX_OK = "delivery_" + "x" * 34  # 43 chars, same length as the current member tokens
CANDIDATE_FORMAT = "delivery_" + "ab" * 32  # delivery_ + 64 hex, the candidate full format


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cred_file(tmp_path):
    return tmp_path / "creds" / "delivery-credential.json"


@pytest.fixture
def dapi(database, cred_file):
    app = create_app(database, delivery=DeliverySettings(cred_file))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture
def token(dapi, cred_file):
    return json.loads(cred_file.read_text())["token"]


def table(database, name):
    with sqlite3.connect(database) as conn:
        return conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall()


def create_room(client, bearer, name="m", workspace="/w"):
    return client.post(
        "/rooms", headers=auth(bearer), json={"workspace": workspace, "member_name": name}
    )


# --- mode boundary: the route exists only in delivery mode ------------------------------------


def test_old_mode_app_has_no_delivery_route_at_all(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/delivery")]
    assert api.post(REGISTER, headers=auth("delivery_" + "0" * 64)).status_code == 404


def test_delivery_mode_adds_exactly_the_host_control_routes(dapi):
    paths = dapi.get("/openapi.json").json()["paths"]
    exposed = {(p, method) for p in paths if p.startswith("/delivery") for method in paths[p]}
    assert exposed == {
        (REGISTER, "post"),
        ("/delivery/bindings", "get"),
        ("/delivery/bindings/{binding_id}/confirm", "post"),
        ("/delivery/bindings/{binding_id}/reject", "post"),
        ("/delivery/candidates", "get"),
        ("/delivery/claims", "post"),
        ("/delivery/claims/{claim_id}/outcome", "post"),
    }


def test_delivery_and_bat_modes_are_mutually_exclusive_in_create_app(database, cred_file):
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_app(database, object(), DeliverySettings(cred_file))


def test_startup_issues_a_credential_and_a_restart_keeps_it(database, cred_file):
    with TestClient(create_app(database, delivery=DeliverySettings(cred_file))):
        first = cred_file.read_bytes()
    with TestClient(create_app(database, delivery=DeliverySettings(cred_file))):
        assert cred_file.read_bytes() == first
    assert len(table(database, "delivery_credentials")) == 1


def test_startup_refuses_an_unsafe_credential_file_instead_of_serving(database, cred_file):
    with TestClient(create_app(database, delivery=DeliverySettings(cred_file))):
        pass
    cred_file.chmod(0o644)
    with pytest.raises(CredentialFileError, match="permission"):
        with TestClient(create_app(database, delivery=DeliverySettings(cred_file))):
            pass


# --- register: host_id from the registry, epoch monotonic in one transaction -------------------


def test_register_returns_registry_host_and_monotonic_epoch(dapi, token, cred_file, database):
    host_id = json.loads(cred_file.read_text())["host_id"]
    for expected in (1, 2, 3):
        response = dapi.post(REGISTER, headers=auth(token))
        assert response.status_code == 200
        assert response.json() == {"host_id": host_id, "host_epoch": expected}
    assert [row[:2] for row in table(database, "delivery_hosts")] == [(host_id, 3)]


def test_register_body_host_id_is_never_trusted(dapi, token, cred_file, database):
    host_id = json.loads(cred_file.read_text())["host_id"]
    own = dapi.post(REGISTER, headers=auth(token), json={"host_id": host_id})
    assert own.status_code == 200 and own.json()["host_epoch"] == 1
    other = dapi.post(REGISTER, headers=auth(token), json={"host_id": "host_" + "9" * 32})
    assert (other.status_code, other.json()) == (403, {"detail": "host_id_mismatch"})
    assert [row[:2] for row in table(database, "delivery_hosts")] == [(host_id, 1)]


@pytest.mark.parametrize(
    "body", [{"host_id": "not-a-host"}, {"host_id": "host_" + "A" * 32}, {"extra": 1}]
)
def test_register_rejects_malformed_bodies_without_side_effects(dapi, token, database, body):
    assert dapi.post(REGISTER, headers=auth(token), json=body).status_code == 422
    assert table(database, "delivery_hosts") == []


def test_register_rechecks_the_credential_inside_its_own_transaction(database, cred_file):
    store = Store(database)
    store.initialize()
    ensure_credential(store, cred_file)
    old = json.loads(cred_file.read_text())["token"]
    rotate_credential(store, cred_file)  # revoked between the dependency check and the write
    with pytest.raises(MailroomError) as caught:
        store.register_delivery_host(digest(old))
    assert (caught.value.status_code, caught.value.detail) == (401, "delivery_credential_rejected")
    assert table(database, "delivery_hosts") == []


# --- principal rejection matrix ---------------------------------------------------------------


def revoked_token(database, cred_file):
    old = json.loads(cred_file.read_text())["token"]
    rotate_credential(Store(database), cred_file)
    return old


def case_token(name, dapi, database, cred_file):
    if name == "revoked":
        return revoked_token(database, cred_file)
    if name == "member":
        bearer = "m" * 43
        assert create_room(dapi, bearer).status_code == 200
        return bearer
    return {
        "unknown_delivery": "delivery_" + "e" * 64,
        "uppercase_hex": "delivery_" + "E" * 64,
        "short_hex": "delivery_" + "e" * 63,
        "long_hex": "delivery_" + "e" * 65,
        "member_style_prefix": MEMBER_REGEX_OK,
        "notification": "notice_" + "f" * 64,
        "too_short": "short",
    }[name]


MATRIX = [
    ("no_header", None, 401, "delivery_credential_required"),
    ("too_short", "short", 401, "delivery_credential_required"),
    ("illegal_chars", "x", 401, "delivery_credential_required"),
    ("unknown_delivery", None, 401, "delivery_credential_rejected"),
    ("revoked", None, 401, "delivery_credential_rejected"),
    ("member", None, 401, "delivery_credential_rejected"),
    ("member_style_prefix", None, 401, "delivery_credential_rejected"),
    ("uppercase_hex", None, 401, "delivery_credential_rejected"),
    ("short_hex", None, 401, "delivery_credential_rejected"),
    ("long_hex", None, 401, "delivery_credential_rejected"),
    ("hash_of_a_valid_token", None, 401, "delivery_credential_rejected"),
    ("notification", None, 403, "delivery_credential_required"),
]


@pytest.mark.parametrize(("name", "_unused", "status", "detail"), MATRIX)
def test_register_rejection_matrix_has_exact_status_detail_and_no_side_effects(
    dapi, token, cred_file, database, name, _unused, status, detail
):
    if name == "no_header":
        headers = {}
    elif name == "illegal_chars":
        headers = {"Authorization": "Bearer has spaces and !! symbols"}
    elif name == "hash_of_a_valid_token":
        headers = auth(digest(token))  # the stored hash is not a bearer
    else:
        headers = auth(case_token(name, dapi, database, cred_file))
    before = (table(database, "delivery_hosts"), table(database, "delivery_credentials"))
    response = dapi.post(REGISTER, headers=headers)
    assert (response.status_code, response.json()) == (status, {"detail": detail})
    assert (table(database, "delivery_hosts"), table(database, "delivery_credentials")) == before


def test_wrong_scheme_is_treated_as_missing(dapi, token):
    response = dapi.post(REGISTER, headers={"Authorization": f"Basic {token}"})
    assert (response.status_code, response.json()) == (
        401,
        {"detail": "delivery_credential_required"},
    )


def test_the_valid_token_is_accepted_and_the_rotated_one_replaces_it(
    dapi, token, database, cred_file
):
    assert dapi.post(REGISTER, headers=auth(token)).json()["host_epoch"] == 1
    rotate_credential(Store(database), cred_file)
    assert dapi.post(REGISTER, headers=auth(token)).status_code == 401
    fresh = json.loads(cred_file.read_text())["token"]
    assert dapi.post(REGISTER, headers=auth(fresh)).json()["host_epoch"] == 2


# --- prefix / namespace compatibility ---------------------------------------------------------


@pytest.mark.parametrize("bearer", [MEMBER_REGEX_OK, CANDIDATE_FORMAT])
def test_current_contract_lets_a_member_pick_a_delivery_prefixed_bearer(api, bearer):
    """Recorded counter-example: the pre-existing API accepts these, so a blanket prefix guard
    would invalidate legal members. This must stay true: the policy is not a prefix guard."""
    created = create_room(api, bearer, name="legacy-prefix")
    assert created.status_code == 200
    room = created.json()["room"]["room_id"]
    assert api.get(f"/rooms/{room}/membership", headers=auth(bearer)).status_code == 200


def test_existing_prefixed_members_keep_working_after_delivery_mode_starts(database, cred_file):
    with TestClient(create_app(database), base_url="http://127.0.0.1") as old:
        room = create_room(old, CANDIDATE_FORMAT, name="legacy-prefix").json()["room"]["room_id"]
        other = old.post(
            f"/rooms/{room}/members", headers=auth("o" * 43), json={"member_name": "peer"}
        )
        assert other.status_code == 200
    with TestClient(
        create_app(database, delivery=DeliverySettings(cred_file)), base_url="http://127.0.0.1"
    ) as new:
        assert (
            new.get(f"/rooms/{room}/membership", headers=auth(CANDIDATE_FORMAT)).status_code == 200
        )
        sent = new.post(
            f"/rooms/{room}/messages",
            headers=auth(CANDIDATE_FORMAT),
            json={"to": "peer", "text": "hi", "request_id": "r1"},
        )
        assert sent.status_code == 200
        # and that member bearer is not a delivery principal: the registry does not know it
        assert new.post(REGISTER, headers=auth(CANDIDATE_FORMAT)).status_code == 401


def test_new_members_cannot_register_with_a_current_or_revoked_delivery_token(
    dapi, token, database, cred_file
):
    room = create_room(dapi, "m" * 43, name="owner").json()["room"]["room_id"]
    revoked = revoked_token(database, cred_file)
    current = json.loads(cred_file.read_text())["token"]
    for bearer in (current, revoked):
        created = create_room(dapi, bearer, name="intruder")
        assert (created.status_code, created.json()) == (
            403,
            {"detail": "delivery_credential_reserved"},
        )
        joined = dapi.post(
            f"/rooms/{room}/members", headers=auth(bearer), json={"member_name": "intruder"}
        )
        assert (joined.status_code, joined.json()) == (
            403,
            {"detail": "delivery_credential_reserved"},
        )
    assert [row[0] for row in table(database, "rooms")] == [room]
    assert [row[1] for row in table(database, "members")] == [digest("m" * 43)]


def test_the_policy_is_hash_based_not_prefix_based_for_new_members(dapi, token):
    # a different delivery_-prefixed bearer that was never issued is an ordinary new member
    assert create_room(dapi, CANDIDATE_FORMAT, name="free").status_code == 200
    assert create_room(dapi, MEMBER_REGEX_OK, name="free2").status_code == 200


def test_a_failed_reserved_registration_leaves_no_half_created_room_or_member(
    dapi, token, database
):
    assert create_room(dapi, token).status_code == 403
    assert table(database, "rooms") == [] and table(database, "members") == []


def test_delivery_tokens_cannot_use_member_routes(dapi, token, database, cred_file):
    room = create_room(dapi, "m" * 43, name="owner").json()["room"]["room_id"]
    revoked = revoked_token(database, cred_file)
    current = json.loads(cred_file.read_text())["token"]
    for bearer in (current, revoked):
        for method, path, body in (
            ("get", f"/rooms/{room}/membership", None),
            ("get", f"/rooms/{room}/members", None),
            ("get", f"/rooms/{room}/messages", None),
            ("get", f"/rooms/{room}/membership/notifications", None),
            ("post", f"/rooms/{room}/messages", {"to": "owner", "text": "x", "request_id": "r"}),
        ):
            response = dapi.request(method, path, headers=auth(bearer), json=body)
            assert response.status_code == 403, (path, response.text)
            assert response.json() == {"detail": "Join this room with this identity first."}
    assert table(database, "messages") == []


def test_reconnect_cannot_move_a_member_onto_a_delivery_credential(database, cred_file):
    store = Store(database)
    store.initialize()
    ensure_credential(store, cred_file)
    reserved = digest(json.loads(cred_file.read_text())["token"])
    store.create_room(
        "a" * 64,
        CreateRoomRequest(
            member_name="alice",
            workspace="/w",
            bat=BatRegistration(runtime="claude", session_id="old"),
        ),
    )
    room = table(database, "rooms")[0][0]
    request = ReconnectRequest(
        member_name="alice", bat=BatRegistration(runtime="claude", session_id="new")
    )
    with pytest.raises(MailroomError) as caught:
        store.reconnect(room, reserved, request, lambda *args: None)
    assert (caught.value.status_code, caught.value.detail) == (403, "delivery_credential_reserved")
    assert [row[1] for row in table(database, "members")] == ["a" * 64]
    # control: the same call with an ordinary new identity succeeds, so the guard is what refused
    store.reconnect(room, "c" * 64, request, lambda *args: None)
    assert [row[1] for row in table(database, "members")] == ["c" * 64]
