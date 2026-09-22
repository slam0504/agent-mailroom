import os

import pytest

from agent_mailroom.bridge import validate_url
from agent_mailroom.identity import IdentityStore, open_identity


def test_registration_allocates_distinct_sessions_and_recovers_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = IdentityStore(tmp_path / "sessions")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(pool.map(lambda _: store.create(), range(8)))
    assert len({key for key, _ in sessions}) == 8
    assert len({token for _, token in sessions}) == 8
    resumed = IdentityStore(tmp_path / "sessions")
    for key, token in sessions:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(resumed.token, [key] * 8)) == [token] * 8
        if os.name == "posix":
            assert (tmp_path / "sessions" / f"{key}.json").stat().st_mode & 0o777 == 0o600


def test_missing_session_is_not_recreated_and_cannot_escape_directory(tmp_path):
    store = IdentityStore(tmp_path)
    with pytest.raises(ValueError, match="Unknown session_key"):
        store.token("session_" + "0" * 32)
    with pytest.raises(ValueError, match="Invalid session_key"):
        store.token("../another-agent")
    assert list(tmp_path.iterdir()) == []


def test_identity_is_persistent_private_and_exclusive(tmp_path):
    path = tmp_path / "identity.json"
    with open_identity(path) as first:
        assert len(first) == 43
        with pytest.raises(ValueError, match="already in use"), open_identity(path):
            pytest.fail("Two bridges acquired the same identity")
    with open_identity(path) as second:
        assert first == second
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 1, "token": "bad"}'])
def test_corrupt_identity_is_not_silently_replaced(tmp_path, content):
    path = tmp_path / "identity.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="Invalid identity"), open_identity(path):
        pytest.fail("Invalid identity accepted")
    assert path.read_text() == content


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://example.com",
        "http://127.0.0.1:8765/path",
        "http://user:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765?token=abc",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
    ],
)
def test_bridge_does_not_send_credentials_to_arbitrary_origins(url):
    with pytest.raises(ValueError):
        validate_url(url)
