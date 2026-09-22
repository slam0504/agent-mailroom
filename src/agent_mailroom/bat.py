"""BAT v3.2.10 transport shared by the manual PoC and optional notification worker."""

import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException

PROTOCOL = "bat-remote/v2"
BAT_VERSION = "3.2.10"
CLAUDE_PRESETS = {"claude-code", "claude-code-worktree"}


class ProbeError(Exception):
    pass


def emit(kind, **values):
    print(json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)


def fingerprint(value):
    value = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ProbeError("Fingerprint must be the SHA-256 value shown by BAT.")
    return value


def validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "wss"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ProbeError("Use wss://HOST:PORT without credentials, query, or path.")


class BatProbe:
    def __init__(self, ws, timeout=330, output=emit):
        self.ws = ws
        self.timeout = timeout
        self.output = output
        self.context_id = None
        self.profile_id = None
        self.session_id = None

    def event(self, frame):
        params = frame.get("params", {})
        if (
            self.session_id
            and frame.get("contextId") == self.context_id
            and isinstance(params, dict)
            and params.get("sessionId") == self.session_id
            and frame.get("channel")
            in {
                "agent:message",
                "agent:result",
                "agent:turn-end",
                "agent:error",
                "agent:ask-user",
                "agent:permission-request",
            }
        ):
            # Events are observations, not correlated proof of our prompt's completion.
            self.output("event", channel=frame["channel"], params=params)

    def receive(self, timeout):
        raw = self.ws.recv(timeout=max(0, timeout))
        if not isinstance(raw, str):
            raise ProbeError("Unexpected binary frame; compression must be none.")
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise ProbeError("Invalid BAT frame.")
        return frame

    def request(self, frame, response_type, timeout=None, submit=None):
        request_id = uuid.uuid4().hex
        frame = {**frame, "id": request_id}
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        if submit is None:
            self.ws.send(json.dumps(frame))
        else:
            submit(lambda: self.ws.send(json.dumps(frame)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("BAT request timed out")
            reply = self.receive(remaining)
            if reply.get("type") == "event":
                self.event(reply)
            elif reply.get("id") == request_id:
                if reply.get("error") or reply.get("type") == "invoke-error":
                    # Don't echo arbitrary server payloads (or auth credentials).
                    raise ProbeError(f"BAT rejected {frame.get('channel', frame['type'])}.")
                if reply.get("type") != response_type:
                    raise ProbeError("Unexpected BAT response type.")
                return reply

    def authenticate(self, token, expected_fingerprint):
        cert = self.ws.socket.getpeercert(binary_form=True)
        if not cert or not hmac.compare_digest(
            hashlib.sha256(cert).hexdigest(), fingerprint(expected_fingerprint)
        ):
            raise ProbeError("BAT certificate fingerprint mismatch; token was not sent.")
        result = self.request(
            {
                "type": "auth",
                "token": token,
                "protocols": [PROTOCOL],
                "compression": ["none"],
                "args": ["agent-mailroom PoC"],
            },
            "auth-result",
            timeout=15,
        )
        if (
            result.get("result") is not True
            or result.get("protocol") != PROTOCOL
            or result.get("compression") != "none"
            or result.get("serverVersion") != BAT_VERSION
            or result.get("capabilities", {}).get("profileContext") != 1
        ):
            raise ProbeError("This probe requires BAT 3.2.10, protocol v2 and profileContext 1.")
        self.output("connected", serverVersion=BAT_VERSION, protocol=PROTOCOL)

    def invoke(self, channel, params=None, scoped=True, submit=None):
        frame = {"type": "invoke", "channel": channel, "params": params or {}}
        if scoped:
            if not self.context_id:
                raise ProbeError("A profile context is required.")
            frame["contextId"] = self.context_id
        return self.request(frame, "invoke-result", submit=submit).get("result")

    def profiles(self):
        data = self.invoke("profile:list", scoped=False)
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            raise ProbeError("Unexpected profile list.")
        return [{k: p.get(k) for k in ("id", "name", "type")} for p in data["profiles"]]

    def open_profile(self, profile_id):
        # Remote profile aliases can forward to a different BAT version/runtime.
        if not any(p["id"] == profile_id and p["type"] == "local" for p in self.profiles()):
            raise ProbeError(
                "Select an existing local BAT profile; remote aliases are unsupported."
            )
        info = self.invoke("profile:open", {"profileId": profile_id}, scoped=False)
        if (
            not isinstance(info, dict)
            or info.get("profileId") != profile_id
            or not info.get("contextId")
            or info.get("status") != "ready"
        ):
            raise ProbeError("BAT profile context is not ready.")
        self.profile_id = profile_id
        self.context_id = info["contextId"]

    def terminals(self):
        data = self.invoke("workspace:load", {"profileId": self.profile_id})
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict) or not isinstance(data.get("terminals"), list):
            raise ProbeError("No workspace snapshot with terminals is available.")
        keys = ("id", "workspaceId", "agentPreset", "title", "alias", "cwd", "sdkSessionId")
        return [{k: t.get(k) for k in keys} for t in data["terminals"]]

    def target(self, session_id, runtime="claude"):
        matches = [t for t in self.terminals() if t["id"] == session_id]
        presets = {"claude": CLAUDE_PRESETS, "codex": {"codex-agent"}}.get(runtime)
        if presets is None or len(matches) != 1 or matches[0]["agentPreset"] not in presets:
            raise ProbeError(f"Target must match the selected {runtime} runtime in this profile.")
        terminal = matches[0]
        meta = self.invoke("claude:get-session-meta", {"sessionId": session_id})
        state = self.invoke("claude:get-session-state", {"sessionId": session_id})
        if (
            not isinstance(meta, dict)
            or not isinstance(state, dict)
            or not terminal["cwd"]
            or meta.get("cwd") != terminal["cwd"]
            or type(state.get("isStreaming")) is not bool
            or (
                runtime == "claude"
                and any(
                    obj.get(key) is not None
                    for obj in (meta, state)
                    for key in ("codexSandboxMode", "codexApprovalPolicy")
                )
            )
            or (
                runtime == "codex"
                and not all(
                    isinstance(meta.get(key), str) and meta[key]
                    for key in ("codexSandboxMode", "codexApprovalPolicy")
                )
            )
        ):
            raise ProbeError(
                "Live session is absent or differs from the runtime/workspace binding."
            )
        resting = self.invoke("claude:is-resting", {"sessionId": session_id})
        if type(resting) is not bool:
            raise ProbeError("BAT did not return a resting state.")
        return terminal, {**state, "isResting": resting}

    def send(self, session_id, prompt, client_message_id):
        if not prompt.strip():
            raise ProbeError("The message file is empty.")
        terminal, state = self.target(session_id)
        if state["isResting"]:
            raise ProbeError("Claude is resting; wake the test session in BAT first.")
        if state.get("pendingAskUser") or state.get("pendingPermission"):
            raise ProbeError("Claude is waiting for user input; resolve it in BAT first.")
        self.session_id = session_id
        self.output(
            "before_send",
            target=terminal,
            isStreaming=state["isStreaming"],
            clientMessageId=client_message_id,
        )
        try:
            result = self.invoke(
                "claude:send-message",
                {
                    "sessionId": session_id,
                    "prompt": prompt,
                    "clientMessageId": client_message_id,
                },
            )
        except (TimeoutError, OSError, ValueError, WebSocketException, ProbeError):
            raise ProbeError(
                "Send outcome unknown; it may still be queued or accepted. "
                "No retry or cancellation was sent. Inspect the target in BAT."
            ) from None
        self.output("send_result", result=result, clientMessageId=client_message_id)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ProbeError("BAT did not report a successful send; inspect send_result.")

    def observe(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                frame = self.receive(deadline - time.monotonic())
            except TimeoutError:
                break
            if frame.get("type") == "event":
                self.event(frame)
        self.output("observation_finished", completion="not_automatically_verified")
