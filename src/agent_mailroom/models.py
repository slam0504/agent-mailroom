"""Shared validation for HTTP requests and MCP tools."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32768)]
RequestId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
SessionId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Workspace = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
SessionKey = Annotated[str, StringConstraints(pattern=r"^session_[a-f0-9]{32}$")]
NotificationKey = Annotated[str, StringConstraints(pattern=r"^notice_[a-f0-9]{64}$")]
HostId = Annotated[str, StringConstraints(pattern=r"^host_[a-f0-9]{32}$")]
CollaborationState = Literal["active", "paused", "left"]


class NotificationCredential(BaseModel):
    """Internal request principal; never a reusable member identity."""

    digest: str


class DeliveryPrincipal(BaseModel):
    """Internal request principal: host_id comes from the credential registry, never the body."""

    host_id: str
    credential_hash: str


class RegisterHostRequest(BaseModel):
    """host_id is optional and only cross-checked against the authenticated host."""

    model_config = ConfigDict(extra="forbid")
    host_id: HostId | None = None


class HostRegistration(BaseModel):
    host_id: str
    host_epoch: int


class Observation(BaseModel):
    """What the host actually saw at the terminal; the server only compares, never infers."""

    model_config = ConfigDict(extra="forbid")
    profile_id: SessionId
    session_id: SessionId
    runtime: Literal["claude", "codex"]
    preset: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    cwd: Workspace


class BindingDecision(BaseModel):
    """confirm and reject carry the same proof: epoch, exact generation and the observation."""

    model_config = ConfigDict(extra="forbid")
    host_epoch: Annotated[int, Field(ge=1)]
    binding_version: Annotated[int, Field(ge=0)]
    observed: Observation


class PendingBinding(BaseModel):
    binding_id: str
    binding_version: int
    profile_id: str
    session_id: str
    runtime: Literal["claude", "codex"]
    workspace: str


class BindingResult(BaseModel):
    binding_id: str
    binding_version: int
    binding_state: Literal["pending", "confirmed", "rejected"]
    host_id: str | None
    reason: str | None


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_epoch: Annotated[int, Field(ge=1)]
    binding_id: str
    binding_version: Annotated[int, Field(ge=0)]
    message_id: Annotated[int, Field(gt=0)]


class DeliveryCandidate(BaseModel):
    binding_id: str
    binding_version: int
    room_id: str
    message_id: int


class ClaimGrant(DeliveryCandidate):
    claim_id: str
    send_deadline: str
    report_deadline: str
    prompt: str


class OutcomeRequest(BaseModel):
    """No host field: the reporter is the authenticated principal, never the body."""

    model_config = ConfigDict(extra="forbid")
    binding_id: str
    binding_version: Annotated[int, Field(ge=0)]
    result: str
    evidence: Annotated[str, StringConstraints(max_length=128)] | None = None


class ClaimRecord(BaseModel):
    claim_id: str
    binding_id: str
    binding_version: int
    room_id: str
    message_id: int
    host_id: str | None
    host_epoch: int | None
    state: Literal["submitting", "accepted", "unknown", "not_accepted", "released"]
    unknown_reason: str | None
    evidence: str | None
    outcome_reported_at: str | None
    updated_at: str


class CollaborationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: CollaborationState


class BatRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["claude", "codex"]
    profile_id: SessionId = "default"
    session_id: SessionId


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_name: Name
    session_id: SessionId | None = None
    workspace: Workspace | None = None
    bat: BatRegistration | None = None


class ReconnectRequest(BaseModel):
    """Replace a disconnected member session with a newly authenticated identity."""

    model_config = ConfigDict(extra="forbid")

    member_name: Name
    session_id: SessionId | None = None
    workspace: Workspace | None = None
    bat: BatRegistration


class CreateRoomRequest(JoinRequest):
    workspace: Workspace


class RoomInfo(BaseModel):
    room_id: str
    workspace: str | None
    created_at: str


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Name
    text: Text
    request_id: RequestId
    reply_to: Annotated[int, Field(gt=0)] | None = None


class Member(BaseModel):
    room_id: str
    member_id: str
    member_name: str
    session_id: str | None
    joined_at: str
    workspace: str | None
    state: CollaborationState = "active"


class BatBinding(BaseModel):
    runtime: Literal["claude", "codex"] = "claude"
    binding_id: str
    room_id: str
    member_id: str
    profile_id: str
    session_id: str
    workspace: str
    last_error: str | None
    # Delivery mode only; old-mode bindings carry neither.
    binding_state: Literal["pending", "confirmed", "rejected"] | None = None
    binding_version: int | None = None


class Membership(BaseModel):
    room: RoomInfo
    member: Member
    bat_binding: BatBinding | None = None


class Registration(Membership):
    # Private capability: never include this in peer-visible membership lists.
    session_key: str


class NotificationAttempt(BaseModel):
    message_id: int
    room_id: str
    member_id: str
    binding_id: str
    # Old-mode attempts carry the BAT client id and queue flag; delivery claims have neither.
    client_message_id: str | None = None
    state: Literal["submitting", "accepted", "unknown", "not_accepted", "released"]
    queued: bool | None = None
    updated_at: str
    # Delivery claims only. unknown_reason is history: a late accepted keeps report_timeout.
    binding_version: int | None = None
    unknown_reason: (
        Literal["report_timeout", "startup_recovery", "runtime_unknown", "legacy"] | None
    ) = None
    outcome_reported_at: str | None = None


class NotificationStatus(BaseModel):
    state: CollaborationState
    binding: BatBinding | None
    worker_enabled: bool
    pending_notification: NotificationAttempt | None


class Message(BaseModel):
    message_id: int
    room_id: str
    sender: str
    recipient: str
    text: str
    request_id: str
    reply_to: int | None
    created_at: str
    acknowledged_at: str | None


class Inbox(BaseModel):
    messages: list[Message]
    next_cursor: int
    has_more: bool


class MailroomError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
