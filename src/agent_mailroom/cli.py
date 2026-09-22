"""Separate entry points for the HTTP service and per-agent stdio bridge."""

import argparse
import os
from pathlib import Path


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local mailboxes for Claude, Codex, and other agents."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the shared HTTP service on 127.0.0.1")
    serve.add_argument("--port", type=port_number, default=8765)
    serve.add_argument(
        "--database",
        type=Path,
        default=Path.home() / ".local/share/agent-mailroom/mailroom.sqlite3",
    )
    serve.add_argument("--bat-url", help="BAT endpoint (default: wss://127.0.0.1:9876)")
    serve.add_argument("--bat-fingerprint", help="SHA-256 shown by BAT Remote Access")
    bridge = commands.add_parser(
        "mcp", help="Run a stdio MCP bridge; agents register their own sessions"
    )
    bridge.add_argument("--url", default="http://127.0.0.1:8765")
    bridge.add_argument(
        "--identity-file", type=Path, help="Import a v0.1 identity for legacy recovery only"
    )
    bridge.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/share/agent-mailroom/sessions",
        help="Shared directory for automatically saved session credentials",
    )
    args = parser.parse_args()
    os.umask(0o077)

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        bat_settings = None
        if args.bat_url is not None or args.bat_fingerprint is not None:
            if not args.bat_fingerprint:
                parser.error("BAT notifications require --bat-fingerprint.")
            import getpass
            import sys

            try:
                from .bat import ProbeError
                from .notifications import BatConnectionSettings
            except ImportError:
                parser.exit(1, "Install BAT support first: uv sync --extra bat\n")
            try:
                url = args.bat_url or "wss://127.0.0.1:9876"
                # Validate public settings before asking for the secret.
                BatConnectionSettings(url, args.bat_fingerprint, "validation-only")
                if not sys.stdin.isatty():
                    parser.exit(
                        1, "Start BAT-enabled serve in your own terminal to enter the token.\n"
                    )
                bat_settings = BatConnectionSettings(
                    url,
                    args.bat_fingerprint,
                    getpass.getpass("BAT Remote Access token (memory only): "),
                )
            except (ValueError, EOFError, ProbeError) as exc:
                parser.exit(1, f"agent-mailroom: invalid BAT settings ({type(exc).__name__})\n")
        uvicorn.run(
            create_app(args.database.expanduser().resolve(), bat_settings),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
    else:
        from .bridge import create_mcp, validate_url
        from .identity import IdentityStore, open_identity

        try:
            url = validate_url(args.url)
            identities = IdentityStore(args.state_dir)
            if args.identity_file is not None:
                with open_identity(args.identity_file) as token:
                    create_mcp(url, identities, legacy_token=token).run()
            else:
                create_mcp(url, identities).run()
        except (ValueError, OSError) as exc:
            parser.exit(1, f"agent-mailroom: {exc}\n")


if __name__ == "__main__":
    main()
