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
