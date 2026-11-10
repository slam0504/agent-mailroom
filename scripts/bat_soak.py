"""Manual soak for the BAT client transport; not part of pytest, not run automatically.

Starts a local asyncio TLS echo server (same self-signed cert approach as the tls_bat_peer pytest
fixture, but TLS 1.3 with default session tickets) and hammers a client connection: open,
interleave send/recv, and close from a second thread at a randomized moment, repeatedly for
--seconds. --impl sync uses raw websockets.sync.client.connect to try to reproduce the old
SSL_read (background recv_events thread) vs. SSL_write/SSL_free (foreground thread) race.
--impl asyncio exercises the facade in agent_mailroom.bat that replaced it.
open_failures counts only failed connection opens; send/recv/close errors are tolerated.
"""

import argparse
import asyncio
import json
import random
import ssl
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, WebSocketException

from agent_mailroom.bat import open_connection


def start_server(tmp_dir):
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
    cert_file, key_file = tmp_dir / "cert.pem", tmp_dir / "key.pem"
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
    tls.minimum_version = ssl.TLSVersion.TLSv1_3
    # Default session tickets on purpose (unlike tls_bat_peer's num_tickets=0 workaround):
    # the soak wants ordinary TLS 1.3 traffic, not a deterministic unit-test shortcut.

    async def handler(ws):
        try:
            async for raw in ws:
                await ws.send(raw)  # Echo; enough to give the client something to recv.
        except ConnectionClosed:
            pass  # The client closes mid-echo on purpose.

    # asyncio server on its own loop, so the only blocking-SSL code in the process is the
    # client under test.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start():
        return await serve(handler, "127.0.0.1", 0, ssl=tls)

    server = asyncio.run_coroutine_threadsafe(start(), loop).result()
    return server, loop, thread, f"wss://127.0.0.1:{server.sockets[0].getsockname()[1]}"


def stop_server(server, loop, thread):
    async def close():
        server.close()
        await server.wait_closed()

    asyncio.run_coroutine_threadsafe(close(), loop).result(timeout=10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def client_tls():
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    return tls


def hammer(ws):
    def closer():
        time.sleep(random.uniform(0, 0.05))
        try:
            ws.close()
        except Exception:
            pass

    closer_thread = threading.Thread(target=closer)
    closer_thread.start()
    try:
        for n in range(5):
            try:
                ws.send(json.dumps({"n": n}))
                ws.recv(timeout=1)
            except (OSError, TimeoutError, WebSocketException):
                break
    finally:
        closer_thread.join(timeout=5)
        try:
            ws.close()
        except Exception:
            pass


def run(connect_one, url, deadline):
    iterations, open_failures = 0, 0
    while time.monotonic() < deadline:
        try:
            ws = connect_one(url)
        except (OSError, TimeoutError, WebSocketException):
            # Connection storms can hit transient handshake timeouts; keep soaking.
            open_failures += 1
            continue
        hammer(ws)
        iterations += 1
    return iterations, open_failures


def run_asyncio(url, deadline):
    return run(
        lambda url: open_connection(
            url, client_tls(), proxy=None, compression=None, close_timeout=2
        ),
        url,
        deadline,
    )


def run_sync(url, deadline):
    from websockets.sync.client import connect  # Old implementation, kept only for this repro.

    return run(
        lambda url: connect(
            url,
            ssl=client_tls(),
            proxy=None,
            compression=None,
            close_timeout=2,
            open_timeout=2,
            legacy=True,
        ),
        url,
        deadline,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--impl", choices=["sync", "asyncio"], required=True)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        server, loop, thread, url = start_server(Path(tmp))
        try:
            deadline = time.monotonic() + args.seconds
            run_impl = run_asyncio if args.impl == "asyncio" else run_sync
            iterations, open_failures = run_impl(url, deadline)
        finally:
            stop_server(server, loop, thread)
    print(f"impl={args.impl} iterations={iterations} open_failures={open_failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
