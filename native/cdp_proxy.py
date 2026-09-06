#!/usr/bin/env python3
"""Token-gated CDP forward proxy (native mode).

Listens on BAS_TAILNET_IP:BAS_CDP_PORT and forwards to 127.0.0.1:9222
(local headless Chrome). Every connection must present the token either
as a `bas_token` query parameter or an `Authorization: Bearer` header.
The query param is stripped before forwarding so upstream Chrome never
sees it.

Why: Chrome 131+ binds CDP to 127.0.0.1 and Chrome has no auth on CDP.
Binding to the Tailscale IP + a per-connection token gate keeps the
browser controllable only from the tailnet with the secret.

Discovery requests (GET /json/version, /json/list, /json) go through the
same auth gate as everything else, but their JSON body is rewritten:
Chrome self-reports its own loopback address in fields like
webSocketDebuggerUrl, which is unreachable from anywhere but this host.
Those occurrences are rewritten to this proxy's own externally-reachable
BAS_TAILNET_IP:BAS_CDP_PORT before the response is returned.

Stdlib only. Python 3.9+.
"""
import os
import re
import socket
import threading

TOKEN = os.environ.get("BAS_CDP_TOKEN", "")
BIND = os.environ.get("BAS_TAILNET_IP", "0.0.0.0")
PORT = int(os.environ.get("BAS_CDP_PORT", "9223"))
UPSTREAM = ("127.0.0.1", int(os.environ.get("BAS_CHROME_CDP_PORT", "9222")))
HDR_TIMEOUT = float(os.environ.get("BAS_HDR_TIMEOUT", "10"))

if not TOKEN:
    raise SystemExit("BAS_CDP_TOKEN is required (see .env.sample)")


def _auth_ok(head: bytes) -> bool:
    m = re.search(rb"bas_token=([^&\s]+)", head)
    if m and m.group(1).decode() == TOKEN:
        return True
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"authorization:"):
            v = line.split(b":", 1)[1].strip()
            if v.startswith(b"Bearer ") and v[7:].decode() == TOKEN:
                return True
    return False


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

    if not _auth_ok(first):
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

    up = socket.create_connection(UPSTREAM, timeout=10)
    up.sendall(_strip_secret(first))
    stop = threading.Event()
    t1 = threading.Thread(target=_pipe, args=(conn, up, stop), daemon=True)
    t2 = threading.Thread(target=_pipe, args=(up, conn, stop), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join(timeout=5)
    for s in (conn, up):
        try:
            s.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND, PORT))
    srv.listen(64)
    print(f"cdp_proxy: {BIND}:{PORT} -> {UPSTREAM[0]}:{UPSTREAM[1]} (token-gated)", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
