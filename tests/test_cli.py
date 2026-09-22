import os
import sys

import pytest

from agent_mailroom.cli import main


@pytest.fixture
def launch(monkeypatch, tmp_path):
    calls, prompts = [], []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(app))
    monkeypatch.setattr("agent_mailroom.api.create_app", lambda database, settings: settings)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "test-token")
    original_umask = os.umask(0o077)

    def run(*flags):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "agent-mailroom",
                "serve",
                "--database",
                str(tmp_path / "mail.sqlite3"),
                *flags,
            ],
        )
        main()
        return calls, prompts

    yield run
    os.umask(original_umask)


def test_bat_starts_without_any_room_or_member_and_prompts_once(launch):
    calls, prompts = launch("--bat-fingerprint", "0" * 64)
    assert len(calls) == len(prompts) == 1
    assert calls[0].url == "wss://127.0.0.1:9876"
    assert calls[0].token == "test-token"
    assert not hasattr(calls[0], "room_id")
    assert "test-token" not in repr(calls)


@pytest.mark.parametrize(
    "flags",
    [
        ("--bat-url", "wss://127.0.0.1:9876"),
        ("--bat-fingerprint", "invalid"),
        ("--bat-url", "wss://100.1.2.3:9876", "--bat-fingerprint", "0" * 64),
        ("--bat-room", "room_old"),
        ("--bat-bindings", "old.json"),
    ],
)
def test_invalid_or_retired_flags_fail_before_prompt_or_server(launch, monkeypatch, flags):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid config must not prompt for a token or start the server")

    monkeypatch.setattr("getpass.getpass", forbidden)
    monkeypatch.setattr("uvicorn.run", forbidden)
    with pytest.raises(SystemExit) as error:
        launch(*flags)
    assert error.value.code != 0


def test_basic_server_does_not_request_bat_token(launch):
    calls, prompts = launch()
    assert calls == [None] and not prompts
