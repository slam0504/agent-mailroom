"""Automatically persisted per-registration credentials and legacy identity import."""

import json
import os
import re
import secrets
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


class IdentityStore:
    """Immutable credential files, addressed by private random session keys, never member names."""

    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()

    def create(self, token: str | None = None) -> tuple[str, str]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = f"session_{uuid4().hex}"
        token = token or secrets.token_urlsafe(32)
        fd = os.open(self.directory / f"{key}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"version": 1, "token": token}, file)
            file.flush()
            os.fsync(file.fileno())
        return key, token

    def token(self, session_key: str) -> str:
        if not re.fullmatch(r"session_[a-f0-9]{32}", session_key):
            raise ValueError("Invalid session_key.")
        path = self.directory / f"{session_key}.json"
        if not path.is_file():
            raise ValueError(
                "Unknown session_key. Restore the session state directory; "
                "do not rejoin under another identity."
            )
        return load_token(path)


def load_token(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Invalid identity file; restore it instead of replacing its token."
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("token"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data["token"])
    ):
        raise ValueError("Invalid identity file; restore it instead of replacing its token.")
    return data["token"]


@contextmanager
def open_identity(path: Path):
    path = path.expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError("Identity is already in use by another MCP bridge.") from exc
    try:
        if not path.exists():
            token = secrets.token_urlsafe(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump({"version": 1, "token": token}, file)
                file.flush()
                os.fsync(file.fileno())
        token = load_token(path)
        os.chmod(path, 0o600)
        yield token
    finally:
        lock.release()
