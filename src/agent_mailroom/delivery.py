"""Delivery credential files: issuance for `serve --delivery-host` and rotation.

The registry (table delivery_credentials) holds only SHA-256 hashes. The token itself lives in
one 0600 file next to nothing else, named by the operator. Ordering rules:

* first issue: the registry row and the file are made together; any failure before commit
  removes the file this call created, so no row exists without its file;
* rotation: private temp file first, then the DB transaction, then an atomic rename. A committed
  DB with a failed rename is a known, recoverable state: the old token is already revoked and
  re-running rotate (which does not need the old token to be valid) repairs it.
"""

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .store import Store

TOKEN_FORMAT = re.compile(r"delivery_[a-f0-9]{64}")
HOST_FORMAT = re.compile(r"host_[a-f0-9]{32}")
MAX_FILE_BYTES = 4096
ROTATE_HINT = "run rotate-delivery-credential for this host"


class CredentialFileError(Exception):
    """Never carries a token: messages name the problem, not the content."""


class CredentialFileMissing(CredentialFileError):
    pass


@dataclass(frozen=True)
class DeliverySettings:
    credential_file: Path


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_credential_file(path: Path) -> tuple[str, str]:
    """Return (host_id, token) from a safe credential file or raise CredentialFileError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise CredentialFileMissing("Credential file does not exist.") from exc
    except OSError as exc:
        raise CredentialFileError(
            "Credential file can not be opened (symlink or no access)."
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialFileError("Credential path is not a regular file.")
        if info.st_uid != os.geteuid():
            raise CredentialFileError("Credential file owner is not the current user.")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise CredentialFileError("Credential file permissions must be 0600 (no group/other).")
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise CredentialFileError("Credential file is too large.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CredentialFileError("Credential file is not valid JSON.") from exc
    if not isinstance(data, dict) or set(data) != {"host_id", "token"}:
        raise CredentialFileError("Credential file must contain exactly host_id and token.")
    host_id, token = data["host_id"], data["token"]
    if not (isinstance(host_id, str) and HOST_FORMAT.fullmatch(host_id)):
        raise CredentialFileError("Credential file host_id has an invalid format.")
    if not (isinstance(token, str) and TOKEN_FORMAT.fullmatch(token)):
        raise CredentialFileError("Credential file token has an invalid format.")
    return host_id, token


def write_private_temp(directory: Path, host_id: str, token: str) -> Path:
    """Create a 0600 temp file in the target directory (same filesystem as the final name)."""
    temp = directory / f".delivery-credential.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump({"host_id": host_id, "token": token}, file)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def new_token() -> str:
    return "delivery_" + secrets.token_hex(32)


def ensure_credential(store: Store, path: Path) -> str:
    """serve --delivery-host: keep the existing credential or issue the very first one."""
    try:
        host_id, token = read_credential_file(path)
    except CredentialFileMissing:
        return issue_first_credential(store, path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM delivery_credentials "
            "WHERE credential_hash = ? AND host_id = ? AND state = 'current'",
            (digest(token), host_id),
        ).fetchone()
    if row is None:
        raise CredentialFileError(
            f"Credential file does not hold the current credential; {ROTATE_HINT}."
        )
    return host_id


def issue_first_credential(store: Store, path: Path) -> str:
    host_id, token = "host_" + secrets.token_hex(16), new_token()
    with store.delivery_lock, store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM delivery_credentials LIMIT 1").fetchone():
            raise CredentialFileError(
                "Credential file is missing but this database already has a delivery host; "
                "its identity is not recreated automatically."
            )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = write_private_temp(path.parent, host_id, token)
        linked = False
        try:
            conn.execute(
                "INSERT INTO delivery_credentials VALUES (?, ?, 'current', ?, NULL)",
                (digest(token), host_id, store.stamp()),
            )
            try:
                os.link(temp, path)  # fails instead of replacing a file that appeared meanwhile
            except FileExistsError as exc:
                raise CredentialFileError("Credential file appeared while issuing.") from exc
            linked = True
            sync_directory(path.parent)
            conn.commit()
        except BaseException:
            if linked:
                path.unlink(missing_ok=True)
            raise
        finally:
            temp.unlink(missing_ok=True)
    return host_id


def rotate_credential(store: Store, path: Path) -> str:
    """Replace the host's credential: temp file, then DB commit, then atomic rename."""
    try:
        host_id, _stale_token = read_credential_file(path)
    except CredentialFileMissing as exc:
        raise CredentialFileError(
            "Credential file is missing; the host identity is not inferred or recreated."
        ) from exc
    token = new_token()
    temp = write_private_temp(path.parent, host_id, token)
    try:
        with store.delivery_lock, store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM delivery_credentials WHERE host_id = ? AND state = 'current'",
                (host_id,),
            ).fetchone():
                raise CredentialFileError(
                    "Credential file names a host this database does not know."
                )
            stamp = store.stamp()
            conn.execute(
                "UPDATE delivery_credentials SET state = 'revoked', revoked_at = ? "
                "WHERE host_id = ? AND state = 'current'",
                (stamp, host_id),
            )
            conn.execute(
                "INSERT INTO delivery_credentials VALUES (?, ?, 'current', ?, NULL)",
                (digest(token), host_id, stamp),
            )
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)
    return host_id
