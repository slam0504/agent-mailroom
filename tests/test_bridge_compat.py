"""B08: the bridge DTO contract between the old bridge and the new server.

OLD_* below is a fixed copy of the response models of d1c2a51 (src/agent_mailroom/models.py), the
last bridge before delivery mode. Nothing here reads git history.
"""

from typing import Literal

import pytest
from delivery_stack import BOB_KEY, started
from pydantic import BaseModel, ValidationError
from test_delivery_lifecycle import cred_path

from agent_mailroom.models import NotificationStatus


class OldBatBinding(BaseModel):
    runtime: Literal["claude", "codex"] = "claude"
    binding_id: str
    room_id: str
    member_id: str
    profile_id: str
    session_id: str
    workspace: str
    last_error: str | None


class OldNotificationAttempt(BaseModel):
    message_id: int
    room_id: str
    member_id: str
    binding_id: str
    client_message_id: str
    state: Literal["submitting", "accepted", "unknown", "not_accepted"]
    queued: bool | None
    updated_at: str


class OldNotificationStatus(BaseModel):
    state: Literal["active", "paused", "left"]
    binding: OldBatBinding | None
    worker_enabled: bool
    pending_notification: OldNotificationAttempt | None


ATTEMPT = {
    "message_id": 1,
    "room_id": "r",
    "member_id": "m",
    "binding_id": "b",
    "client_message_id": "c-1",
    "state": "accepted",
    "queued": None,
    "updated_at": "t",
}


def status_with(attempt):
    return {
        "state": "active",
        "binding": None,
        "worker_enabled": False,
        "pending_notification": attempt,
    }


def test_old_dto_ignores_the_three_extra_null_fields():
    extra = {**ATTEMPT, "binding_version": None, "unknown_reason": None}
    extra["outcome_reported_at"] = None
    shown = OldNotificationStatus.model_validate(status_with(extra)).pending_notification
    assert shown.state == "accepted"


def test_old_dto_rejects_released_and_a_null_client_message_id():
    released = {**ATTEMPT, "state": "released"}
    with pytest.raises(ValidationError):
        OldNotificationStatus.model_validate(status_with(released))
    claim_shaped = {**ATTEMPT, "client_message_id": None}
    with pytest.raises(ValidationError):
        OldNotificationStatus.model_validate(status_with(claim_shaped))


def test_new_dto_reads_the_old_format_and_the_new_format():
    old = NotificationStatus.model_validate(status_with(ATTEMPT)).pending_notification
    assert (old.client_message_id, old.binding_version, old.unknown_reason) == ("c-1", None, None)
    new = {
        **ATTEMPT,
        "state": "released",
        "client_message_id": None,
        "binding_version": 2,
        "unknown_reason": None,
        "outcome_reported_at": "t",
    }
    read = NotificationStatus.model_validate(status_with(new)).pending_notification
    assert (read.state, read.client_message_id, read.binding_version) == ("released", None, 2)


def test_a_real_delivery_server_response_splits_old_and_new_readers(database, tmp_path):
    with started(database, cred_path(tmp_path)) as stack:
        room, epoch, binding, message_id, claim = stack.full_claim()
        body = stack.status(room)
        assert NotificationStatus.model_validate(body).pending_notification.state == "submitting"
        # a claim is shaped without client_message_id, so the old bridge cannot read it
        with pytest.raises(ValidationError):
            OldNotificationStatus.model_validate(body)
        released = stack.outcome(claim, "released", evidence="not_invoked")
        assert released.status_code == 200
        with pytest.raises(ValidationError):
            OldNotificationStatus.model_validate(stack.status(room))
        assert stack.member("GET", f"/rooms/{room}/membership", BOB_KEY).status_code == 200
