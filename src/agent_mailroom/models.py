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
CollaborationState = Literal["active", "paused", "left"]


class NotificationCredential(BaseModel):
    """Internal request principal; never a reusable member identity."""

    digest: str


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
    client_message_id: str
    state: Literal["submitting", "accepted", "unknown", "not_accepted"]
    queued: bool | None
    updated_at: str


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
