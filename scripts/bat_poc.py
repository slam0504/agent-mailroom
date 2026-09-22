"""Manual Claude transport probe for BAT v3.2.10; not a mailroom wake worker."""

import argparse
import getpass
import os
import ssl
import sys
import uuid
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from agent_mailroom.bat import PROTOCOL as PROTOCOL  # Compatibility for the PoC protocol tests.
from agent_mailroom.bat import (
    BatProbe,
    ProbeError,
    emit,
    fingerprint,
    validate_url,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="wss://127.0.0.1:9876")
    parser.add_argument("--fingerprint", required=True, help="SHA-256 from BAT Remote Access")
    parser.add_argument("--profile", default="default")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profiles")
    sub.add_parser("sessions")
    status = sub.add_parser("status")
    status.add_argument("--session-id", required=True, help="BAT terminal id, not sdkSessionId")
    send = sub.add_parser("send")
    send.add_argument("--session-id", required=True)
    send.add_argument("--message-file", required=True, type=Path)
    send.add_argument("--observe-seconds", default=60, type=float)
    args = parser.parse_args()
    try:
        validate_url(args.url)
        expected = fingerprint(args.fingerprint)
        prompt = None
        if args.command == "send":
            if not 0 <= args.observe_seconds <= 600:
                raise ProbeError("Observation must be between 0 and 600 seconds.")
            prompt = args.message_file.read_text(encoding="utf-8")
            if not prompt.strip() or len(prompt) > 32768:
                raise ProbeError("Message must contain 1 to 32768 characters.")
        token = os.environ.get("BAT_REMOTE_TOKEN") or getpass.getpass("BAT Remote Access token: ")
        if not token.strip():
            raise ProbeError("A BAT token is required.")
        # BAT's self-signed certificate is authenticated by its exact fingerprint
        # on this same connection BEFORE any token is sent; no insecure mode.
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        with connect(
            args.url,
            ssl=tls,
            proxy=None,
            compression=None,
            close_timeout=2,
            max_size=16 * 1024 * 1024,
        ) as ws:
            probe = BatProbe(ws)
            probe.authenticate(token, expected)
            if args.command == "profiles":
                emit("profiles", profiles=probe.profiles())
                return 0
            probe.open_profile(args.profile)
            if args.command == "sessions":
                emit("sessions", profileId=args.profile, terminals=probe.terminals())
            elif args.command == "status":
                terminal, state = probe.target(args.session_id)
                emit(
                    "status",
                    target=terminal,
                    **{
                        k: state.get(k)
                        for k in (
                            "active",
                            "isStreaming",
                            "isResting",
                            "model",
                        )
                    },
                    waitingForUser=bool(
                        state.get("pendingAskUser") or state.get("pendingPermission")
                    ),
                )
            else:
                probe.send(args.session_id, prompt, "mailroom-poc-" + uuid.uuid4().hex)
                probe.observe(args.observe_seconds)
        return 0
    except (ProbeError, OSError, ValueError, WebSocketException) as error:
        # Suppress library errors that might embed endpoint/frames or credentials.
        detail = str(error) if isinstance(error, ProbeError) else type(error).__name__
        print(f"BAT PoC: {detail}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("BAT PoC stopped; already submitted work was not cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
