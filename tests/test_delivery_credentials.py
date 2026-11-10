"""Checkpoint A: delivery credential registry, issuance, rotation fault branches and CLI wiring."""

import errno
import json
import os
import sqlite3
import stat
import sys

import pytest

import agent_mailroom.delivery as delivery
from agent_mailroom.cli import main
from agent_mailroom.delivery import (
    CredentialFileError,
    digest,
    ensure_credential,
    read_credential_file,
    rotate_credential,
)
from agent_mailroom.models import MailroomError
from agent_mailroom.store import Store


@pytest.fixture
def store(database):
    store = Store(database)
    store.initialize()
    return store


@pytest.fixture
def cred(tmp_path):
    return tmp_path / "creds" / "delivery-credential.json"


def registry(store):
    with store.connection() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT credential_hash, host_id, state, revoked_at FROM delivery_credentials "
                "ORDER BY created_at, credential_hash"
            )
        ]


def current(store):
    rows = [row for row in registry(store) if row["state"] == "current"]
    assert len(rows) == 1
    return rows[0]


def file_state(path):
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def leftovers(path):
    return sorted(p.name for p in path.parent.iterdir() if p.name != path.name)


def db_bytes(store):
    """Raw database plus WAL content: any plaintext token would show up here."""
    return b"".join(
        p.read_bytes()
        for p in (store.path, store.path.with_name(store.path.name + "-wal"))
        if p.exists()
    )


def host_epochs(store):
    with store.connection() as conn:
        return [tuple(r) for r in conn.execute("SELECT host_id, current_epoch FROM delivery_hosts")]


# --- schema ----------------------------------------------------------------------------------


def test_registry_schema_allows_one_current_credential_per_host_and_consistent_revocation(store):
    h = "h" * 64
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO delivery_credentials VALUES (?, 'host_a', 'current', 't', NULL)", (h,)
        )
        # a second current credential for the same host is impossible
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delivery_credentials VALUES (?, 'host_a', 'current', 't', NULL)",
                ("i" * 64,),
            )
        # revoked rows need a revocation time and current rows must not have one
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delivery_credentials VALUES (?, 'host_a', 'revoked', 't', NULL)",
                ("j" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delivery_credentials VALUES (?, 'host_b', 'current', 't', 'x')",
                ("k" * 64,),
            )


# --- issuance (serve --delivery-host) ----------------------------------------------------------


def test_first_issue_creates_0600_file_in_private_dir_and_stores_only_the_hash(store, cred):
    host_id = ensure_credential(store, cred)
    data = json.loads(cred.read_text())
    assert set(data) == {"host_id", "token"}
    assert data["host_id"] == host_id
    assert delivery.HOST_FORMAT.fullmatch(host_id) and delivery.TOKEN_FORMAT.fullmatch(
        data["token"]
    )
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    assert stat.S_IMODE(cred.parent.stat().st_mode) == 0o700
    assert registry(store) == [
        {
            "credential_hash": digest(data["token"]),
            "host_id": host_id,
            "state": "current",
            "revoked_at": None,
        }
    ]
    assert data["token"].encode() not in db_bytes(store)
    assert leftovers(cred) == []


def test_restart_keeps_the_same_host_and_file_without_new_registry_rows(store, cred):
    host_id = ensure_credential(store, cred)
    before = (file_state(cred), registry(store))
    assert ensure_credential(store, cred) == host_id
    assert (file_state(cred), registry(store)) == before


def test_issue_failure_before_commit_leaves_no_registry_row_and_no_file(store, cred, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(delivery.os, "link", fail)
    with pytest.raises(OSError):
        ensure_credential(store, cred)
    assert registry(store) == []
    assert not cred.exists() and leftovers(cred) == []


def test_issue_insert_failure_leaves_no_registry_row_and_no_file(store, cred):
    with store.connection() as conn:
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON delivery_credentials "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        ensure_credential(store, cred)
    assert registry(store) == []
    assert not cred.exists() and leftovers(cred) == []


def test_issue_failure_after_the_file_is_linked_removes_that_file_and_the_row(
    store, cred, monkeypatch
):
    def fail(path):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(delivery, "sync_directory", fail)
    with pytest.raises(OSError):
        ensure_credential(store, cred)
    assert registry(store) == []
    assert not cred.exists() and leftovers(cred) == []


# --- rejecting unsafe or inconsistent existing files -------------------------------------------


def make_file(path, host_id, token, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w") as file:
        file.write(token if host_id is None else json.dumps({"host_id": host_id, "token": token}))


GOOD_TOKEN = "delivery_" + "c" * 64
GOOD_HOST = "host_" + "d" * 32


def bad_files():
    return {
        "group_readable": lambda p: make_file(p, GOOD_HOST, GOOD_TOKEN, 0o640),
        "world_readable": lambda p: make_file(p, GOOD_HOST, GOOD_TOKEN, 0o604),
        "not_json": lambda p: make_file(p, None, "garbage"),
        "json_list": lambda p: make_file(p, None, "[]"),
        "extra_key": lambda p: make_file(
            p, None, json.dumps({"host_id": GOOD_HOST, "token": GOOD_TOKEN, "x": 1})
        ),
        "bad_host_id": lambda p: make_file(p, "host_../../x", GOOD_TOKEN),
        "bad_token": lambda p: make_file(p, GOOD_HOST, "delivery_" + "C" * 64),
        "symlink": lambda p: (
            make_file(p.with_name("real.json"), GOOD_HOST, GOOD_TOKEN),
            p.symlink_to(p.with_name("real.json")),
        ),
        "directory": lambda p: p.mkdir(parents=True),
        "oversized": lambda p: make_file(p, None, "x" * 100_000),
    }


@pytest.mark.parametrize("kind", list(bad_files()))
def test_unsafe_or_malformed_existing_file_is_rejected_by_issue_rotate_and_read(store, cred, kind):
    bad_files()[kind](cred)
    before = registry(store)
    operations = (
        lambda: read_credential_file(cred),
        lambda: ensure_credential(store, cred),
        lambda: rotate_credential(store, cred),
    )
    for operation in operations:
        with pytest.raises(CredentialFileError) as caught:
            operation()
        assert GOOD_TOKEN not in str(caught.value)
    assert registry(store) == before
    assert leftovers(cred) in ([], ["real.json"])


def test_file_owned_by_someone_else_is_rejected(store, cred, monkeypatch):
    ensure_credential(store, cred)
    before = (file_state(cred), registry(store))
    uid = os.geteuid()
    monkeypatch.setattr(delivery.os, "geteuid", lambda: uid + 1)
    for operation in (ensure_credential, rotate_credential):
        with pytest.raises(CredentialFileError, match="owner"):
            operation(store, cred)
    assert (file_state(cred), registry(store)) == before


def test_file_whose_token_is_not_the_current_credential_is_rejected_at_issue(store, cred):
    make_file(cred, GOOD_HOST, GOOD_TOKEN)
    with pytest.raises(CredentialFileError, match="current credential"):
        ensure_credential(store, cred)
    assert registry(store) == []


def test_missing_file_with_an_existing_host_never_invents_a_new_host(store, cred):
    host_id = ensure_credential(store, cred)
    cred.unlink()
    before = registry(store)
    for operation in (ensure_credential, rotate_credential):
        with pytest.raises(CredentialFileError):
            operation(store, cred)
    assert registry(store) == before
    assert {row["host_id"] for row in registry(store)} == {host_id}
    assert not cred.exists()


def test_rotate_refuses_a_file_naming_a_host_the_registry_does_not_know(store, cred):
    ensure_credential(store, cred)
    make_file(cred, GOOD_HOST, GOOD_TOKEN)
    before = registry(store)
    with pytest.raises(CredentialFileError, match="host"):
        rotate_credential(store, cred)
    assert registry(store) == before and leftovers(cred) == []


# --- rotation: the four fault branches of the plan ------------------------------------------


def register_epoch(store, token):
    return store.register_delivery_host(digest(token))


def test_rotation_success_replaces_hash_and_file_keeps_host_and_epoch(store, cred):
    host_id = ensure_credential(store, cred)
    old_token = json.loads(cred.read_text())["token"]
    assert register_epoch(store, old_token) == 1
    assert register_epoch(store, old_token) == 2

    assert rotate_credential(store, cred) == host_id
    new_token = json.loads(cred.read_text())["token"]
    assert new_token != old_token
    assert json.loads(cred.read_text())["host_id"] == host_id
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600
    assert leftovers(cred) == []
    states = {row["credential_hash"]: row for row in registry(store)}
    assert states[digest(old_token)]["state"] == "revoked"
    assert states[digest(old_token)]["revoked_at"]
    assert states[digest(new_token)]["state"] == "current"
    assert host_epochs(store) == [(host_id, 2)]  # rotation does not reset or advance the epoch
    assert db_bytes(store).count(new_token.encode()) == 0
    assert register_epoch(store, new_token) == 3


def test_branch1a_temp_file_preparation_failure_changes_nothing(store, cred):
    ensure_credential(store, cred)
    token = json.loads(cred.read_text())["token"]
    before = (file_state(cred), registry(store))
    cred.parent.chmod(0o500)  # the private temp file can not be created next to the target
    try:
        with pytest.raises(OSError):
            rotate_credential(store, cred)
    finally:
        cred.parent.chmod(0o700)
    assert (file_state(cred), registry(store)) == before
    assert leftovers(cred) == []
    assert register_epoch(store, token) == 1  # old credential is still accepted


def test_branch1b_db_update_not_committed_keeps_old_hash_file_and_cleans_temp(store, cred):
    ensure_credential(store, cred)
    token = json.loads(cred.read_text())["token"]
    before = (file_state(cred), registry(store))
    with store.connection() as conn:
        # fires on the INSERT of the new hash, after the old one was already marked revoked
        conn.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON delivery_credentials "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        rotate_credential(store, cred)
    assert (file_state(cred), registry(store)) == before
    assert leftovers(cred) == []
    assert register_epoch(store, token) == 1


def test_branch2_committed_db_with_failed_rename_revokes_old_token_and_rerun_repairs(
    store, cred, monkeypatch
):
    host_id = ensure_credential(store, cred)
    old_token = json.loads(cred.read_text())["token"]
    old_bytes = cred.read_bytes()
    register_epoch(store, old_token)

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "injected rename failure")

    with monkeypatch.context() as patch:
        patch.setattr(delivery.os, "replace", fail)
        with pytest.raises(OSError):
            rotate_credential(store, cred)

    # the file is the old one, the old token is already dead, the temp file is cleaned
    assert cred.read_bytes() == old_bytes and leftovers(cred) == []
    with pytest.raises(MailroomError) as caught:
        register_epoch(store, old_token)
    assert (caught.value.status_code, caught.value.detail) == (401, "delivery_credential_rejected")
    assert len([r for r in registry(store) if r["state"] == "current"]) == 1
    # serve would refuse the stale file instead of reviving the old token or inventing a host
    with pytest.raises(CredentialFileError, match="rotate-delivery-credential"):
        ensure_credential(store, cred)

    # the repair is a plain rerun of rotate: it does not need the old token to be valid
    assert rotate_credential(store, cred) == host_id
    new_token = json.loads(cred.read_text())["token"]
    assert new_token != old_token
    assert {row["host_id"] for row in registry(store)} == {host_id}
    assert host_epochs(store) == [(host_id, 1)]
    assert register_epoch(store, new_token) == 2
    states = [r["state"] for r in registry(store)]
    assert states.count("current") == 1 and states.count("revoked") == 2


def test_branch3_file_and_db_both_new_but_a_holder_of_the_old_token_is_rejected_until_reread(
    store, cred
):
    ensure_credential(store, cred)
    stale_token = json.loads(cred.read_text())["token"]
    rotate_credential(store, cred)
    with pytest.raises(MailroomError) as caught:
        register_epoch(store, stale_token)
    assert caught.value.status_code == 401
    # re-reading the file recovers; nothing about the host changed
    fresh_token = read_credential_file(cred)[1]
    assert register_epoch(store, fresh_token) == 1


def test_rotate_twice_keeps_exactly_one_current_credential(store, cred):
    host_id = ensure_credential(store, cred)
    rotate_credential(store, cred)
    rotate_credential(store, cred)
    assert [r["state"] for r in registry(store)].count("current") == 1
    assert {r["host_id"] for r in registry(store)} == {host_id}


# --- CLI ------------------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    created = []

    def fake_create_app(database, bat_settings=None, delivery=None):
        created.append((database, bat_settings, delivery))
        return "app"

    monkeypatch.setattr("agent_mailroom.api.create_app", fake_create_app)
    original = os.umask(0o077)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["agent-mailroom", *argv])
        main()

    yield run, created, calls
    os.umask(original)


@pytest.mark.parametrize("conflicting", [["--bat-fingerprint", "0" * 64], ["--bat-url", "wss://x"]])
def test_delivery_host_and_bat_flags_are_rejected_before_any_side_effect(
    cli, tmp_path, conflicting, capsys
):
    run, created, calls = cli
    database = tmp_path / "state" / "mail.sqlite3"
    file = tmp_path / "state" / "delivery-credential.json"
    with pytest.raises(SystemExit) as caught:
        run(
            "serve",
            "--database",
            str(database),
            "--delivery-host",
            "--delivery-credential-file",
            str(file),
            *conflicting,
        )
    assert caught.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err
    assert created == [] and calls == []
    assert not (tmp_path / "state").exists()


def test_serve_delivery_host_builds_the_app_with_delivery_settings_only(cli, tmp_path):
    run, created, calls = cli
    file = tmp_path / "creds" / "delivery-credential.json"
    run(
        "serve",
        "--database",
        str(tmp_path / "mail.sqlite3"),
        "--delivery-host",
        "--delivery-credential-file",
        str(file),
    )
    database, bat_settings, settings = created[0]
    assert bat_settings is None and settings.credential_file == file
    assert not file.exists() and not (tmp_path / "creds").exists()  # lifespan issues, not parsing


def test_delivery_credential_file_option_requires_delivery_host(cli, tmp_path, capsys):
    run, created, calls = cli
    with pytest.raises(SystemExit) as caught:
        run(
            "serve",
            "--database",
            str(tmp_path / "m.sqlite3"),
            "--delivery-credential-file",
            str(tmp_path / "x.json"),
        )
    assert caught.value.code == 2 and created == []


def test_rotate_command_rotates_and_prints_no_token(cli, store, cred, capsys):
    run, _, _ = cli
    host_id = ensure_credential(store, cred)
    old = json.loads(cred.read_text())["token"]
    run(
        "rotate-delivery-credential",
        "--database",
        str(store.path),
        "--delivery-credential-file",
        str(cred),
    )
    out = capsys.readouterr()
    new = json.loads(cred.read_text())["token"]
    assert new != old and host_id in out.out
    assert old not in out.out + out.err and new not in out.out + out.err


def test_rotate_command_failure_exits_1_with_no_token_and_no_change(cli, store, cred, capsys):
    run, _, _ = cli
    ensure_credential(store, cred)
    cred.chmod(0o644)
    before = registry(store)
    token = json.loads(cred.read_text())["token"]
    with pytest.raises(SystemExit) as caught:
        run(
            "rotate-delivery-credential",
            "--database",
            str(store.path),
            "--delivery-credential-file",
            str(cred),
        )
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "permission" in err.lower() and token not in err
    assert registry(store) == before


def test_rotate_command_does_not_create_a_database_that_does_not_exist(cli, tmp_path, capsys):
    run, _, _ = cli
    database = tmp_path / "absent" / "mail.sqlite3"
    with pytest.raises(SystemExit) as caught:
        run(
            "rotate-delivery-credential",
            "--database",
            str(database),
            "--delivery-credential-file",
            str(tmp_path / "c.json"),
        )
    assert caught.value.code == 1
    assert not database.parent.exists()
    sibling = tmp_path / "mail.sqlite3"
    with pytest.raises(SystemExit) as caught:
        run(
            "rotate-delivery-credential",
            "--database",
            str(sibling),
            "--delivery-credential-file",
            str(tmp_path / "c.json"),
        )
    assert caught.value.code == 1 and not sibling.exists()


# --- CLI must not resolve away a symlinked credential path --------------------------------------


@pytest.fixture
def real_serve(monkeypatch):
    """Run the real app's lifespan (issuance included) instead of a listener."""
    from fastapi.testclient import TestClient

    def run_lifespan(app, **kwargs):
        with TestClient(app, base_url="http://127.0.0.1"):
            pass

    monkeypatch.setattr("uvicorn.run", run_lifespan)


@pytest.fixture
def alias(store, cred, tmp_path):
    ensure_credential(store, cred)
    link = tmp_path / "alias.json"
    link.symlink_to(cred)
    return link


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["agent-mailroom", *argv])
    main()


def test_rotate_cli_rejects_a_symlinked_credential_file_without_touching_the_target(
    monkeypatch, store, cred, alias
):
    before = (file_state(cred), registry(store))
    with pytest.raises(SystemExit) as caught:
        run_main(
            monkeypatch,
            "rotate-delivery-credential",
            "--database",
            str(store.path),
            "--delivery-credential-file",
            str(alias),
        )
    assert caught.value.code == 1
    assert (file_state(cred), registry(store)) == before
    assert alias.is_symlink() and leftovers(cred) == []


def test_serve_cli_rejects_a_symlinked_credential_file_and_issues_nothing(
    monkeypatch, real_serve, store, cred, alias
):
    before = (file_state(cred), registry(store))
    with pytest.raises(CredentialFileError):
        run_main(
            monkeypatch,
            "serve",
            "--database",
            str(store.path),
            "--delivery-host",
            "--delivery-credential-file",
            str(alias),
        )
    assert (file_state(cred), registry(store)) == before
    assert alias.is_symlink()


@pytest.mark.parametrize("command", ["rotate", "serve"])
def test_cli_rejects_a_dangling_symlinked_credential_path(
    monkeypatch, real_serve, store, tmp_path, command
):
    missing = tmp_path / "nowhere.json"
    link = tmp_path / "dangling.json"
    link.symlink_to(missing)
    argv = {
        "rotate": ["rotate-delivery-credential", "--database", str(store.path)],
        "serve": ["serve", "--database", str(store.path), "--delivery-host"],
    }[command]
    expected = SystemExit if command == "rotate" else CredentialFileError
    with pytest.raises(expected):
        run_main(monkeypatch, *argv, "--delivery-credential-file", str(link))
    assert registry(store) == []
    assert link.is_symlink() and not missing.exists()
