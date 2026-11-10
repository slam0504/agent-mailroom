import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from delivery_helpers import (
    ALICE,
    BOB,
    BODY,
    HOST,
    WORKSPACE,
    Clock,
    Keys,
    register_host,
    target,
)
from delivery_stack import started
from fastapi.testclient import TestClient

from agent_mailroom.api import create_app
from agent_mailroom.models import CreateRoomRequest, JoinRequest, SendRequest
from agent_mailroom.store import Store

TEST_PORT = 18765


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def database(tmp_path):
    return tmp_path / "mailroom.sqlite3"


@pytest.fixture
def api(database):
    with TestClient(create_app(database), base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture
def dstack(database, tmp_path):
    """Delivery-mode app over a fresh DB with a really issued credential file."""
    with started(database, tmp_path / "creds" / "delivery-credential.json") as stack:
        yield stack


@pytest.fixture
def live_url(database):
    """Real HTTP listener on the one reserved test port; shut down only this test's server.

    Fixed (never 8765, the daily service) and bound before uvicorn starts. A busy port fails the
    test instead of picking another one; tests using it must run serially.
    """
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", TEST_PORT))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(database), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started, "HTTP test server did not start"
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "HTTP test server did not stop"


@pytest.fixture
def env(database):
    """Delivery-mode store: room, confirmed bob binding, two unread mails, counting key factory."""
    store = Store(database)
    store.initialize()
    clock = Clock()
    store.clock = clock
    store.create_room(ALICE, CreateRoomRequest(member_name="alice", workspace=WORKSPACE))
    with store.connection() as conn:
        room = conn.execute("SELECT room_id FROM rooms").fetchone()[0]
    store.join(room, BOB, JoinRequest(member_name="bob"))
    register_host(store)
    pending = store.advance_binding_generation(room, BOB, target("bat-bob"), WORKSPACE)
    binding = store.confirm_binding(pending["binding_id"], pending["binding_version"], HOST, 1)
    first = store.send(room, ALICE, SendRequest(to="bob", text=BODY, request_id="q1"))
    second = store.send(room, ALICE, SendRequest(to="bob", text=BODY, request_id="q2"))
    return SimpleNamespace(
        store=store, room=room, binding=binding, m1=first, m2=second, keys=Keys(), clock=clock
    )
