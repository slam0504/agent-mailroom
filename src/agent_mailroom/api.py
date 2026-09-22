"""Loopback HTTP service shared by all MCP bridges."""

import asyncio
import hashlib
import re
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated

from anyio import to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from filelock import FileLock
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import (
    CollaborationRequest,
    CreateRoomRequest,
    Inbox,
    JoinRequest,
    MailroomError,
    Member,
    Membership,
    Message,
    Name,
    NotificationCredential,
    NotificationStatus,
    ReconnectRequest,
    SendRequest,
)
from .store import Store

bearer = HTTPBearer(auto_error=False)


def identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str | NotificationCredential:
    if credentials is None or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credentials.credentials):
        raise HTTPException(401, "A valid identity bearer token is required.")
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    if re.fullmatch(r"notice_[a-f0-9]{64}", credentials.credentials):
        if request.scope["route"].name not in {
            "notification_status",
            "get_message",
            "ack_message",
            "send_message",
        }:
            raise HTTPException(403, "Notification credentials cannot perform this operation.")
        return NotificationCredential(digest=digest)
    return digest


Identity = Annotated[str | NotificationCredential, Depends(identity)]
Room = Annotated[Name, PathParam()]
MessageId = Annotated[int, PathParam(gt=0)]


def create_app(database: Path, bat_settings=None) -> FastAPI:
    store = Store(database)
    notifications = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal notifications
        database.parent.mkdir(parents=True, exist_ok=True)
        # Single process ownership is necessary for pause/send serialization.
        with FileLock(str(database) + ".server.lock", timeout=0):
            await to_thread.run_sync(store.initialize)
            await to_thread.run_sync(store.recover_notifications)
            async with AsyncExitStack() as cleanup:
                if bat_settings is not None:
                    from .notifications import NotificationService

                    notifications = NotificationService(store, bat_settings)
                    cleanup.push_async_callback(to_thread.run_sync, notifications.stop)
                    await to_thread.run_sync(notifications.refresh)
                try:
                    yield
                finally:
                    notifications = None

    app = FastAPI(title="Agent Mailroom", version="0.7.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def reject_browser_origin(request: Request, call_next):
        if "origin" in request.headers:
            return JSONResponse(
                status_code=403, content={"detail": "Browser origins are disabled."}
            )
        return await call_next(request)

    @app.exception_handler(MailroomError)
    async def mailroom_error(request: Request, exc: MailroomError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def validate_registration(body, member_id, room_id=None):
        if body.bat is None:
            return
        if notifications is None:
            raise MailroomError(
                503, "BAT notifications are disabled; start serve with --bat-fingerprint."
            )
        workspace = body.workspace
        if room_id is not None:
            with store.connection() as conn:
                room = store.room(conn, room_id)
                existing = conn.execute(
                    "SELECT workspace FROM members WHERE room_id = ? AND member_id = ?",
                    (room_id, member_id),
                ).fetchone()
                if workspace is None:
                    workspace = existing["workspace"] if existing else room.workspace
        notifications.validate_target(body.bat, workspace)

    def refresh_notifications():
        if notifications is not None:
            notifications.refresh()

    @app.post("/rooms", response_model=Membership)
    def create_room(body: CreateRoomRequest, member_id: Identity):
        validate_registration(body, member_id)
        result = store.create_room(member_id, body)
        refresh_notifications()
        return result

    @app.get("/rooms/{room_id}/membership", response_model=Membership)
    def resume_session(room_id: Room, member_id: Identity):
        return store.resume(room_id, member_id)

    @app.post("/rooms/{room_id}/members", response_model=Member)
    def join_room(room_id: Room, body: JoinRequest, member_id: Identity):
        validate_registration(body, member_id, room_id)
        result = store.join(room_id, member_id, body)
        refresh_notifications()
        return result

    @app.post("/rooms/{room_id}/members/reconnect", response_model=Membership)
    def reconnect_member(room_id: Room, body: ReconnectRequest, member_id: Identity):
        if notifications is None:
            raise MailroomError(
                503, "BAT notifications are disabled; reconnect proof is unavailable."
            )
        result = store.reconnect(room_id, member_id, body, notifications.validate_replacement)
        refresh_notifications()
        return result

    @app.get("/rooms/{room_id}/members", response_model=list[Member])
    def list_members(room_id: Room, member_id: Identity):
        return store.list_members(room_id, member_id)

    @app.put("/rooms/{room_id}/membership/state", response_model=Member)
    def set_collaboration_state(room_id: Room, body: CollaborationRequest, member_id: Identity):
        result = store.set_collaboration(room_id, member_id, body.state)
        refresh_notifications()
        return result

    @app.get("/rooms/{room_id}/membership/notifications", response_model=NotificationStatus)
    def notification_status(room_id: Room, member_id: Identity):
        return store.notification_status(room_id, member_id)

    @app.post("/rooms/{room_id}/messages", response_model=Message)
    def send_message(room_id: Room, body: SendRequest, member_id: Identity):
        return store.send(room_id, member_id, body)

    @app.get("/rooms/{room_id}/messages", response_model=Inbox)
    async def receive_messages(
        room_id: Room,
        member_id: Identity,
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        wait_ms: Annotated[int, Query(ge=0, le=30000)] = 0,
    ):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_ms / 1000
        while True:
            inbox = await to_thread.run_sync(
                partial(store.receive, room_id, member_id, after, limit)
            )
            if inbox.messages or loop.time() >= deadline:
                return inbox
            if await request.is_disconnected():
                return inbox
            await asyncio.sleep(min(0.2, max(0, deadline - loop.time())))

    @app.get("/rooms/{room_id}/messages/{message_id}", response_model=Message)
    def get_message(room_id: Room, message_id: MessageId, member_id: Identity):
        return store.get_message(room_id, member_id, message_id)

    @app.post("/rooms/{room_id}/messages/{message_id}/ack", response_model=Message)
    def ack_message(room_id: Room, message_id: MessageId, member_id: Identity):
        return store.get_message(room_id, member_id, message_id, ack=True)

    return app
