"""BAT protocol tests use a local TLS peer, never a running BAT/model session."""

import hashlib
import importlib.util
import json
import ssl
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.sync.client import connect
from websockets.sync.server import serve

spec = importlib.util.spec_from_file_location(
    "bat_poc", Path(__file__).resolve().parents[1] / "scripts" / "bat_poc.py"
)
poc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poc)


class Peer:
    def __init__(self):
        self.sent = []
        self.replies = deque()
        self.socket = self
        self.version = "3.2.10"
        self.preset = "claude-code"
        self.profile_type = "local"
        self.state = {"isStreaming": False, "pendingAskUser": None, "pendingPermission": None}
        self.meta = {"cwd": "/workspace"}
        self.result = {"ok": True, "accepted": True, "queued": False}
        self.fail_send = False
        self.resting = False
        self.targets = {}  # Optional multi-session fixture for dynamic registration tests.

    def getpeercert(self, binary_form):
        return b"test-certificate"

    def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] == "auth":
            self.replies.append(
                {
                    "type": "auth-result",
                    "id": frame["id"],
                    "result": True,
                    "serverVersion": self.version,
                    "protocol": poc.PROTOCOL,
                    "compression": "none",
                    "capabilities": {"profileContext": 1},
                }
            )
            return
        channel = frame["channel"]
        if channel == "profile:list":
            result = {
                "profiles": [
                    {
                        "id": "default",
                        "name": "Default",
                        "type": self.profile_type,
                        "remoteToken": "hidden",
                    }
                ]
            }
        elif channel == "profile:open":
            result = {"contextId": "pc-one", "profileId": "default", "status": "ready"}
        elif channel == "workspace:load":
            result = json.dumps(
                {
                    "terminals": [t["terminal"] for t in self.targets.values()]
                    if self.targets
                    else [
                        {
                            "id": "bat-id",
                            "sdkSessionId": "sdk-id",
                            "cwd": "/workspace",
                            "agentPreset": self.preset,
                        }
                    ]
                }
            )
        elif channel == "claude:get-session-meta":
            result = (
                self.targets[frame["params"]["sessionId"]]["meta"] if self.targets else self.meta
            )
        elif channel == "claude:get-session-state":
            result = self.state
        elif channel == "claude:is-resting":
            result = self.resting
        elif channel == "claude:send-message":
            if self.fail_send:
                return
            result = (
                self.targets[frame["params"]["sessionId"]]["result"]
                if self.targets
                else self.result
            )
        else:
            raise AssertionError(f"Unexpected operation {channel}")
        self.replies.append({"type": "invoke-result", "id": frame["id"], "result": result})

    def recv(self, timeout):
        if not self.replies:
            raise TimeoutError
        return json.dumps(self.replies.popleft())


@pytest.fixture
def client():
    peer = Peer()
    observations = []
    probe = poc.BatProbe(peer, output=lambda kind, **data: observations.append((kind, data)))
    return peer, probe, observations


def test_fingerprint_mismatch_never_sends_token(client):
    peer, probe, _ = client
    with pytest.raises(poc.ProbeError, match="token was not sent"):
        probe.authenticate("secret", "0" * 64)
    assert peer.sent == []


@pytest.mark.parametrize("version", ["3.2.9", "3.2.11-pre.2"])
def test_version_skew_stops_before_profile_or_send(client, version):
    peer, probe, _ = client
    peer.version = version
    with pytest.raises(poc.ProbeError, match="requires BAT 3.2.10"):
        probe.authenticate("secret", hashlib.sha256(b"test-certificate").hexdigest())
    assert [f["type"] for f in peer.sent] == ["auth"]


def test_profile_output_filters_credentials_and_remote_alias_is_refused(client):
    peer, probe, _ = client
    assert "hidden" not in json.dumps(probe.profiles())
    peer.profile_type = "remote"
    with pytest.raises(poc.ProbeError, match="local BAT profile"):
        probe.open_profile("default")
    assert all(f["channel"] == "profile:list" for f in peer.sent)


@pytest.mark.parametrize(
    "preset",
    [
        "codex-agent",
        "codex-agent-worktree",
        "codex-fugu",
        "claude-channel",
        "claude-cli-agent",
        None,
    ],
)
def test_non_claude_targets_never_receive_send(client, preset):
    peer, probe, _ = client
    peer.preset = preset
    probe.open_profile("default")
    with pytest.raises(poc.ProbeError, match="selected claude runtime"):
        probe.send("bat-id", "test", "request-1")
    assert not any(f.get("channel") == "claude:send-message" for f in peer.sent)


@pytest.mark.parametrize("change", ["missing", "cwd", "codex", "unknown-streaming", "pending"])
def test_absent_mismatched_or_blocked_session_never_receives_send(client, change):
    peer, probe, _ = client
    if change == "missing":
        peer.state = None
    elif change == "cwd":
        peer.meta["cwd"] = "/other"
    elif change == "codex":
        peer.state["codexSandboxMode"] = "workspace-write"
    elif change == "unknown-streaming":
        peer.state.pop("isStreaming")
    else:
        peer.state["pendingAskUser"] = {"question": "approval needed"}
    probe.open_profile("default")
    with pytest.raises(poc.ProbeError):
        probe.send("bat-id", "test", "request-1")
    assert not any(f.get("channel") == "claude:send-message" for f in peer.sent)


def test_sdk_id_cannot_be_used_as_bat_id(client):
    peer, probe, _ = client
    probe.open_profile("default")
    with pytest.raises(poc.ProbeError, match="selected claude runtime"):
        probe.send("sdk-id", "test", "request-1")
    assert not any(f.get("channel") == "claude:send-message" for f in peer.sent)


@pytest.mark.parametrize("busy", [False, True])
def test_send_uses_profile_and_reports_acceptance_separately(client, busy):
    peer, probe, observations = client
    peer.state["isStreaming"] = busy
    peer.result["queued"] = busy
    probe.open_profile("default")
    probe.send("bat-id", "test", "request-1")
    frame = peer.sent[-1]
    assert frame["contextId"] == "pc-one"
    assert frame["params"] == {
        "sessionId": "bat-id",
        "prompt": "test",
        "clientMessageId": "request-1",
    }
    assert observations[-1] == (
        "send_result",
        {"result": peer.result, "clientMessageId": "request-1"},
    )
    assert observations[0][1]["isStreaming"] is busy


def test_send_timeout_does_not_retry_or_cancel(client):
    peer, probe, _ = client
    probe.open_profile("default")
    peer.fail_send = True
    with pytest.raises(poc.ProbeError, match="outcome unknown"):
        probe.send("bat-id", "test", "request-1")
    assert sum(f.get("channel") == "claude:send-message" for f in peer.sent) == 1
    assert peer.sent[-1]["channel"] == "claude:send-message"


def test_resting_session_is_not_woken(client):
    peer, probe, _ = client
    peer.resting = True
    probe.open_profile("default")
    with pytest.raises(poc.ProbeError, match="resting"):
        probe.send("bat-id", "test", "request-1")
    assert peer.sent[-1]["channel"] == "claude:is-resting"


@pytest.mark.parametrize("result", [{"ok": False, "cancelled": True}, None])
def test_cancelled_or_unknown_result_is_not_reported_as_success(client, result):
    peer, probe, _ = client
    peer.result = result
    probe.open_profile("default")
    with pytest.raises(poc.ProbeError, match="did not report a successful send"):
        probe.send("bat-id", "test", "request-1")


def test_events_are_filtered_by_context_and_target_even_before_rpc_reply(client):
    peer, probe, observations = client
    probe.open_profile("default")
    for context, session in [("pc-other", "bat-id"), ("pc-one", "other"), ("pc-one", "bat-id")]:
        peer.replies.append(
            {
                "type": "event",
                "contextId": context,
                "channel": "agent:message",
                "params": {"sessionId": session, "message": "observed"},
            }
        )
    # Set the observation target as send() would, then wait for a matching RPC.
    probe.session_id = "bat-id"
    probe.invoke("claude:get-session-state", {"sessionId": "bat-id"})
    events = [data for kind, data in observations if kind == "event"]
    assert len(events) == 1
    assert events[0]["params"]["sessionId"] == "bat-id"
    probe.observe(0)
    assert observations[-1][1]["completion"] == "not_automatically_verified"


@pytest.fixture
def tls_bat_peer(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    # Keep this blocking OpenSSL fixture free of TLS 1.3 session-ticket traffic.
    # Same fixture workaround as https://github.com/python/cpython/issues/137583.
    # BAT's TLS server and certificate pinning aren't changed by this test setting.
    tls.num_tickets = 0
    peer = Peer()
    frame_lock = threading.Lock()

    def handler(ws):
        for raw in ws:
            with frame_lock:
                peer.send(raw)
                while peer.replies:
                    ws.send(json.dumps(peer.replies.popleft()))

    with serve(handler, "127.0.0.1", 0, ssl=tls) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield (
                peer,
                f"wss://127.0.0.1:{server.socket.getsockname()[1]}",
                cert.fingerprint(hashes.SHA256()).hex(),
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()


def test_local_tls_websocket_auth_profile_and_send(tls_bat_peer):
    peer, url, fingerprint = tls_bat_peer
    client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_tls.check_hostname = False
    client_tls.verify_mode = ssl.CERT_NONE
    with connect(url, ssl=client_tls, proxy=None, compression=None) as ws:
        probe = poc.BatProbe(ws, output=lambda *args, **kwargs: None)
        probe.authenticate("test-token", fingerprint)
        probe.open_profile("default")
        probe.send("bat-id", "PoC only", "request-1")
    assert peer.sent[0]["token"] == "test-token"
    assert peer.sent[-1]["channel"] == "claude:send-message"


@pytest.mark.parametrize(
    "preset,metadata,allowed",
    [
        (
            "codex-agent",
            {"codexSandboxMode": "workspace-write", "codexApprovalPolicy": "on-request"},
            True,
        ),
        ("codex-agent", {}, False),
        (
            "claude-code",
            {"codexSandboxMode": "workspace-write", "codexApprovalPolicy": "on-request"},
            False,
        ),
        ("claude-channel", {}, False),
    ],
)
def test_explicit_codex_target_requires_matching_preset_and_live_metadata(
    client, preset, metadata, allowed
):
    peer, probe, _ = client
    peer.preset = preset
    peer.meta.update(metadata)
    probe.open_profile("default")
    if allowed:
        terminal, state = probe.target("bat-id", runtime="codex")
        assert terminal["agentPreset"] == "codex-agent" and state["isResting"] is False
    else:
        with pytest.raises(poc.ProbeError):
            probe.target("bat-id", runtime="codex")
    assert not any(f.get("channel") == "claude:send-message" for f in peer.sent)
