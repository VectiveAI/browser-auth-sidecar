#!/usr/bin/env python3
"""Grants-aware CDP forward proxy (docker mode).

Replaces the alpine/socat relay. Same auth-gate + discovery-rewrite shape
as native/cdp_proxy.py, but validates each connection's presented token
against a multi-consumer grants index instead of one shared secret, and
that index is rebuilt purely from an outbound poll against Maestro --
never from a local file.

SOURCE OF TRUTH IS MAESTRO, NOT THIS PROCESS. Every BAS_GRANTS_POLL_INTERVAL
seconds this proxy calls
    GET {BAS_MAESTRO_API_URL}/api/v1/providers/{BAS_MAESTRO_PROVIDER_ID}/grants
(Authorization: Bearer BAS_MAESTRO_PROVIDER_TOKEN -- the provider's own
registration credential, never a token minted for this purpose) and
replaces its in-memory grants index wholesale from the response. A
restart just re-polls and starts with an empty cache until the next poll
lands -- there is nothing durable on this side to lose or to un-revoke,
because grant state was never written here.

The response carries every grant for this provider -- revoked and expired
included, not a live-only view. This proxy diffs each poll against its
previous one to catch a grant that JUST transitioned to revoked/expired
(or disappeared entirely) and, if that grant has open sockets, closes
them immediately. Gate-on-new-connection alone would leave an
already-open CDP WebSocket running until the consumer disconnected
itself, which is not real-time revoke for a mid-session agent.

Revoke touches only the one grant's own token_hash and its own live
sockets. It never reaches the browser container, noVNC, or
session-health.json -- those stay entirely orthogonal to grant state.

Until the first successful poll (or whenever polling is failing), the
proxy has no grants to check against and so authorizes nothing -- fail
closed, never fail open.

Stdlib only. Python 3.9+.
"""
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BIND = os.environ.get("BAS_TAILNET_IP", "0.0.0.0")
PORT = int(os.environ.get("BAS_CDP_PORT", "9223"))
UPSTREAM = ("127.0.0.1", int(os.environ.get("BAS_CHROME_CDP_PORT", "9222")))
HDR_TIMEOUT = float(os.environ.get("BAS_HDR_TIMEOUT", "10"))

MAESTRO_API_URL = os.environ.get("BAS_MAESTRO_API_URL", "https://maestro.vectiveai.com").rstrip("/")
PROVIDER_ID = os.environ.get("BAS_MAESTRO_PROVIDER_ID", "")
PROVIDER_TOKEN = os.environ.get("BAS_MAESTRO_PROVIDER_TOKEN", "")
POLL_INTERVAL = float(os.environ.get("BAS_GRANTS_POLL_INTERVAL", "1"))
POLL_TIMEOUT = float(os.environ.get("BAS_GRANTS_POLL_TIMEOUT", "5"))
UNCONFIGURED_RETRY = float(os.environ.get("BAS_GRANTS_UNCONFIGURED_RETRY", "30"))

_lock = threading.Lock()
_grants_by_hash = {}          # token_hash -> grant record
_grants_by_id = {}            # grant_id -> grant record (current poll's state)
_dead_by_id = {}              # grant_id -> bool, dead-status AS OF the poll it was computed in
_live_sockets = {}            # grant_id -> set of (conn, up, stop) currently open under it
_poll_ok = threading.Event()  # set once at least one poll has completed successfully


def _grants_url(provider_id: str) -> str:
    return f"{MAESTRO_API_URL}/api/v1/providers/{provider_id}/grants"


def _now():
    return datetime.now(timezone.utc)


def _is_dead(grant: dict) -> bool:
    """A grant is dead if Maestro has revoked or expired it -- absence
    from the latest poll (purged) counts the same as dead, since a
    provider must never treat 'grant not mentioned' as 'still fine'."""
    if grant.get("revoked_at"):
        return True
    expires_at = grant.get("expires_at")
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    return expiry <= _now()


def _fetch_grants(provider_id: str, token: str) -> list:
    req = urllib.request.Request(
        _grants_url(provider_id),
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("grants", [])


def _close_live_sockets(grant_id: str):
    with _lock:
        pairs = _live_sockets.pop(grant_id, set())
    for conn, up, stop in pairs:
        stop.set()
        for s in (conn, up):
            try:
                s.close()
            except OSError:
                pass


def _poll_once():
    global _grants_by_hash, _grants_by_id, _dead_by_id
    grants = _fetch_grants(PROVIDER_ID, PROVIDER_TOKEN)
    new_by_hash = {}
    new_by_id = {}
    new_dead_by_id = {}
    for g in grants:
        new_by_id[g["grant_id"]] = g
        new_by_hash[g["token_hash"]] = g
        # Computed once, now, and cached -- NOT re-derived later against a
        # future wall clock. A grant's expires_at doesn't change between
        # polls even after it expires, so re-running _is_dead() on stale
        # cached data at a LATER poll would silently re-evaluate against
        # the newer clock and never observe a transition. Comparing this
        # poll's freshly-computed status against the previous poll's own
        # cached status is what makes expiry (not just an explicit revoke)
        # a detectable transition at all.
        new_dead_by_id[g["grant_id"]] = _is_dead(g)

    with _lock:
        prev_dead_by_id = _dead_by_id
        _grants_by_hash = new_by_hash
        _grants_by_id = new_by_id
        _dead_by_id = new_dead_by_id
    _poll_ok.set()

    for grant_id in set(prev_dead_by_id) | set(new_dead_by_id):
        was_dead = prev_dead_by_id.get(grant_id, False)
        now_dead = new_dead_by_id.get(grant_id, True)  # disappeared (purged) counts as dead
        if now_dead and not was_dead:
            _close_live_sockets(grant_id)


def _poll_loop():
    warned_unconfigured = False
    while True:
        if not PROVIDER_ID or not PROVIDER_TOKEN:
            if not warned_unconfigured:
                print(
                    "cdp_proxy: BAS_MAESTRO_PROVIDER_ID / BAS_MAESTRO_PROVIDER_TOKEN "
                    "not set -- refusing all connections until this provider is "
                    "registered and configured",
                    flush=True,
                )
                warned_unconfigured = True
            time.sleep(max(POLL_INTERVAL, UNCONFIGURED_RETRY))
            continue
        try:
            _poll_once()
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f"cdp_proxy: grants poll failed: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)


def _extract_token(head: bytes):
    m = re.search(rb"bas_token=([^&\s]+)", head)
    if m:
        return m.group(1).decode()
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"authorization:"):
            v = line.split(b":", 1)[1].strip()
            if v.startswith(b"Bearer "):
                return v[7:].decode()
    return None


def _grant_for_presented_token(token: str):
    token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        grant = _grants_by_hash.get(token_hash)
        return dict(grant) if grant is not None else None


def _auth_grant(head: bytes):
    if not _poll_ok.is_set():
        return None
    token = _extract_token(head)
    if not token:
        return None
    grant = _grant_for_presented_token(token)
    if grant is None or _is_dead(grant):
        return None
    return grant


def _strip_secret(buf: bytes) -> bytes:
    head, sep, rest = buf.partition(b"\r\n\r\n")
    first, _, other = head.partition(b"\r\n")
    first = re.sub(rb"[?&]bas_token=[^&\s]*", b"", first)
    lines = [first]
    if other:
        lines += [l for l in other.split(b"\r\n")
                  if not l.lower().startswith(b"authorization:")]
    return b"\r\n".join(lines) + sep + rest


def _is_discovery_request(first: bytes) -> bool:
    line = first.split(b"\r\n", 1)[0]
    parts = line.split(b" ")
    if len(parts) < 2 or parts[0] != b"GET":
        return False
    path = parts[1].split(b"?", 1)[0]
    return path in (b"/json/version", b"/json/list", b"/json")


def _read_http_response(sock: socket.socket, timeout: float = 10) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            return buf
        buf += chunk
        if len(buf) > 1 << 20:
            return buf
    head, sep, body = buf.partition(b"\r\n\r\n")
    m = re.search(rb"(?i)content-length:\s*(\d+)", head)
    if m:
        need = int(m.group(1))
        while len(body) < need:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    return head + sep + body


def _rewrite_discovery_response(raw: bytes) -> bytes:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return raw
    old_loopback = f"127.0.0.1:{UPSTREAM[1]}".encode()
    old_localhost = f"localhost:{UPSTREAM[1]}".encode()
    new_target = f"{BIND}:{PORT}".encode()
    new_body = body.replace(old_loopback, new_target).replace(old_localhost, new_target)
    if new_body != body:
        head = re.sub(
            rb"(?i)content-length:\s*\d+",
            b"Content-Length: " + str(len(new_body)).encode(),
            head,
        )
    return head + sep + new_body


def _pipe(a: socket.socket, b: socket.socket, stop: threading.Event):
    try:
        while not stop.is_set():
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        stop.set()


def handle(conn: socket.socket, addr):
    conn.settimeout(HDR_TIMEOUT)
    first = b""
    try:
        while b"\r\n\r\n" not in first:
            chunk = conn.recv(65536)
            if not chunk:
                conn.close()
                return
            first += chunk
            if len(first) > 1 << 20:  # runaway garbage
                conn.close()
                return
    except socket.timeout:
        conn.close()
        return

    grant = _auth_grant(first)
    if grant is None:
        conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        conn.close()
        return

    conn.settimeout(None)

    if _is_discovery_request(first):
        try:
            up = socket.create_connection(UPSTREAM, timeout=10)
            up.sendall(_strip_secret(first))
            raw = _read_http_response(up)
            up.close()
        except OSError:
            conn.close()
            return
        conn.sendall(_rewrite_discovery_response(raw))
        conn.close()
        return

    try:
        up = socket.create_connection(UPSTREAM, timeout=10)
    except OSError:
        conn.close()
        return
    up.sendall(_strip_secret(first))

    grant_id = grant["grant_id"]
    stop = threading.Event()
    entry = (conn, up, stop)
    with _lock:
        # A grant can be revoked in the gap between the auth check above
        # and taking this lock. Re-check under the lock before publishing
        # the connection as "live" -- otherwise a revoke landing in that
        # exact window is missed by both the check that already passed
        # and the next poll's diff (which only fires on a transition, and
        # would see no prior live-socket entry here to close).
        current = _grants_by_id.get(grant_id)
        already_dead = current is None or _is_dead(current)
        if already_dead:
            stop.set()
        else:
            _live_sockets.setdefault(grant_id, set()).add(entry)

    if stop.is_set():
        for s in (conn, up):
            try:
                s.close()
            except OSError:
                pass
        return

    t1 = threading.Thread(target=_pipe, args=(conn, up, stop), daemon=True)
    t2 = threading.Thread(target=_pipe, args=(up, conn, stop), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join(timeout=5)
    with _lock:
        _live_sockets.get(grant_id, set()).discard(entry)
    for s in (conn, up):
        try:
            s.close()
        except OSError:
            pass


def main():
    threading.Thread(target=_poll_loop, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, PORT))
    srv.listen(64)
    print(
        f"cdp_proxy (grants-aware): {BIND}:{PORT} -> {UPSTREAM[0]}:{UPSTREAM[1]} "
        f"(polling {_grants_url(PROVIDER_ID or '<unconfigured>')} every {POLL_INTERVAL}s)",
        flush=True,
    )
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
