import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_mailroom.api import create_app


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
def live_url(database):
    """Real HTTP listener on a reserved port; shut down only this test's server."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
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
