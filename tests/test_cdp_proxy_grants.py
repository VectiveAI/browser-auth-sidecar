#!/usr/bin/env python3
"""Verifies the four load-bearing properties of cdp_proxy_grants.py against a
fake Maestro grants endpoint and a fake upstream Chrome -- no docker-host,
no network, no live Maestro required. Stdlib only.

Run: python3 test_cdp_proxy_grants.py
"""
import contextlib
import hashlib
import http.server
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker"))


def _read_audit_log(shared_dir):
    path = os.path.join(shared_dir, "meta", "audit.log")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeMaestro:
    """Serves GET /api/v1/providers/{id}/grants from a mutable in-memory list."""

    def __init__(self):
        self.port = _free_port()
        self.grants = []
        self.lock = threading.Lock()
        self.seen_auth_headers = []
        handler = self._make_handler()
        self.server = http.server.HTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _make_handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen_auth_headers.append(self.headers.get("Authorization"))
                with outer.lock:
                    body = json.dumps({"grants": list(outer.grants)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        return Handler

    def set_grants(self, grants):
        with self.lock:
            self.grants = grants

    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()


class FakeUpstream:
    """Stands in for Chrome's CDP port: echoes whatever it receives."""

    def __init__(self):
        self.port = _free_port()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(16)
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.srv.settimeout(0.5)
        while not self.stop_flag.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        try:
            conn.settimeout(5)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self.stop_flag.set()
        self.srv.close()


class FakeDiscoveryUpstream:
    """Stands in for Chrome's /json/version endpoint: always answers with a
    canned body that self-reports its own loopback address, the way real
    Chrome does -- lets the test verify the proxy rewrites it."""

    def __init__(self):
        self.port = _free_port()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(16)
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.srv.settimeout(0.5)
        while not self.stop_flag.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._respond, args=(conn,), daemon=True).start()

    def _respond(self, conn):
        try:
            conn.settimeout(5)
            conn.recv(65536)  # drain the request
            body = json.dumps({
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/abc",
            }).encode()
            resp = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            conn.sendall(resp)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self.stop_flag.set()
        self.srv.close()


def make_grant(service_name="tradingview", ttl_seconds=3600, revoked=False, token=None):
    token = token or uuid.uuid4().hex
    token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    record = {
        "grant_id": str(uuid.uuid4()),
        "service_name": service_name,
        "consumer_identity": "test-consumer",
        "token_hash": token_hash,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "revoked_at": now.isoformat() if revoked else None,
    }
    return record, token


class ProxyHarness:
    """Boots cdp_proxy_grants against a fake Maestro + fake upstream on
    fresh module state each time (module-level globals reset via reload)."""

    def __init__(self, maestro, upstream, poll_interval="0.1", shared_dir=None):
        os.environ["BAS_TAILNET_IP"] = "127.0.0.1"
        os.environ["BAS_CDP_PORT"] = str(_free_port())
        os.environ["BAS_CHROME_CDP_PORT"] = str(upstream.port)
        os.environ["BAS_MAESTRO_API_URL"] = maestro.base_url()
        os.environ["BAS_MAESTRO_PROVIDER_ID"] = "test-provider"
        os.environ["BAS_MAESTRO_PROVIDER_TOKEN"] = "provider-registration-token"
        os.environ["BAS_GRANTS_POLL_INTERVAL"] = poll_interval
        os.environ["BAS_GRANTS_POLL_TIMEOUT"] = "5"
        self.shared_dir = shared_dir or tempfile.mkdtemp(prefix="bas-test-shared-")
        os.environ["BAS_SHARED_DIR"] = self.shared_dir

        global cdp_proxy_grants
        if "cdp_proxy_grants" in sys.modules:
            cdp_proxy_grants = importlib.reload(sys.modules["cdp_proxy_grants"])
        else:
            import cdp_proxy_grants  # noqa: F401
        self.mod = cdp_proxy_grants
        self.port = self.mod.PORT

        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((self.mod.BIND, self.port))
        self.srv.listen(64)
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()
        self.poll_thread = threading.Thread(target=self.mod._poll_loop, daemon=True)
        self.poll_thread.start()

    def _accept_loop(self):
        self.srv.settimeout(0.5)
        while True:
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.mod.handle, args=(conn, addr), daemon=True).start()

    def wait_polled(self, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.mod._poll_ok.is_set():
                return
            time.sleep(0.02)
        raise TimeoutError("proxy never completed a poll")

    def connect(self, token=None, timeout=3):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", self.port))
        path = f"/?bas_token={token}" if token else "/"
        req = f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n"
        s.sendall(req.encode())
        return s

    def connect_authorized(self, token, timeout=3):
        """Connect with a token expected to pass auth, and drain the bytes
        the fake upstream echoes back for the initial forwarded (secret
        stripped) request line before handing the socket to the caller --
        otherwise that echo pollutes the first real payload read, since
        the fake upstream (unlike real Chrome) echoes everything it's
        sent, including the connection-setup request itself."""
        req = f"GET /?bas_token={token} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        expected = self.mod._strip_secret(req)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", self.port))
        s.sendall(req)
        buf = b""
        while len(buf) < len(expected):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        assert buf == expected, f"unexpected echo prefix: {buf!r} != {expected!r}"
        return s

    def close(self):
        try:
            self.srv.close()
        except OSError:
            pass


class TestGrantsAwareProxy(unittest.TestCase):
    def setUp(self):
        self.maestro = FakeMaestro()
        self.upstream = FakeUpstream()
        self.addCleanup(self.maestro.stop)
        self.addCleanup(self.upstream.stop)

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.harness.close()

    def test_property_1_source_of_truth_is_maestro_not_proxy_memory(self):
        """A grant absent from Maestro's response is never authorized, even
        on the very first poll -- there is no local file this could have
        been seeded from, and restart (module reload) starts empty."""
        grant, token = make_grant()
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream)
        self.harness.wait_polled()

        s = self.harness.connect_authorized(token=token)
        s.sendall(b"ping")
        self.assertEqual(s.recv(4096), b"ping")  # echoed by fake upstream -> authorized
        s.close()

        # "Restart": reload the module (fresh empty globals), same Maestro
        # state (still has the grant). It must re-derive authorization from
        # Maestro's response again, not fail open and not require anything
        # locally persisted.
        self.harness.close()
        self.harness = ProxyHarness(self.maestro, self.upstream)
        self.harness.wait_polled()
        s = self.harness.connect_authorized(token=token)
        s.sendall(b"ping2")
        self.assertEqual(s.recv(4096), b"ping2")
        s.close()

    def test_property_1b_unknown_token_rejected_before_and_after_poll(self):
        self.maestro.set_grants([])
        self.harness = ProxyHarness(self.maestro, self.upstream)
        s = self.harness.connect(token="not-a-real-token")
        resp = s.recv(4096)
        self.assertIn(b"401", resp)
        s.close()

    def test_property_2_revoke_closes_an_already_open_socket(self):
        """Gate-on-new-connection isn't enough -- a live connection must be
        actively closed within one poll cycle of its grant being revoked."""
        grant, token = make_grant()
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1")
        self.harness.wait_polled()

        s = self.harness.connect_authorized(token=token)
        s.sendall(b"alive")
        self.assertEqual(s.recv(4096), b"alive")

        revoked = dict(grant)
        revoked["revoked_at"] = datetime.now(timezone.utc).isoformat()
        self.maestro.set_grants([revoked])

        # give it a few poll cycles
        deadline = time.time() + 2
        closed = False
        while time.time() < deadline:
            try:
                s.sendall(b"still-there")
                data = s.recv(4096)
                if data == b"":
                    closed = True
                    break
            except OSError:
                closed = True
                break
            time.sleep(0.05)
        self.assertTrue(closed, "socket was not closed within the poll window after revoke")
        s.close()

    def test_property_2b_expiry_closes_an_already_open_socket(self):
        grant, token = make_grant(ttl_seconds=1)
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1")
        self.harness.wait_polled()

        s = self.harness.connect_authorized(token=token)
        s.sendall(b"alive")
        self.assertEqual(s.recv(4096), b"alive")

        time.sleep(1.3)  # cross the expiry boundary; next poll (same grant list) should catch it

        deadline = time.time() + 2
        closed = False
        while time.time() < deadline:
            try:
                s.sendall(b"still-there")
                data = s.recv(4096)
                if data == b"":
                    closed = True
                    break
            except OSError:
                closed = True
                break
            time.sleep(0.05)
        self.assertTrue(closed, "socket was not closed after expiring")
        s.close()

    def test_property_2c_new_connection_rejected_once_revoked(self):
        grant, token = make_grant()
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1")
        self.harness.wait_polled()

        revoked = dict(grant)
        revoked["revoked_at"] = datetime.now(timezone.utc).isoformat()
        self.maestro.set_grants([revoked])
        time.sleep(0.4)

        s = self.harness.connect(token=token)
        resp = s.recv(4096)
        self.assertIn(b"401", resp)
        s.close()

    def test_property_3_revoke_never_touches_upstream_or_other_grants(self):
        """Revoking grant A must not affect a live connection under grant B,
        and must never send anything to the fake browser/upstream itself
        outside the one connection being torn down (no upstream-directed
        'kill' message exists in this protocol at all -- verified by the
        other grant's connection staying fully functional throughout)."""
        grant_a, token_a = make_grant("svc-a")
        grant_b, token_b = make_grant("svc-b")
        self.maestro.set_grants([grant_a, grant_b])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1")
        self.harness.wait_polled()

        sa = self.harness.connect_authorized(token=token_a)
        sb = self.harness.connect_authorized(token=token_b)
        sa.sendall(b"a1")
        self.assertEqual(sa.recv(4096), b"a1")
        sb.sendall(b"b1")
        self.assertEqual(sb.recv(4096), b"b1")

        revoked_a = dict(grant_a)
        revoked_a["revoked_at"] = datetime.now(timezone.utc).isoformat()
        self.maestro.set_grants([revoked_a, grant_b])

        deadline = time.time() + 2
        a_closed = False
        while time.time() < deadline:
            try:
                sa.sendall(b"a2")
                if sa.recv(4096) == b"":
                    a_closed = True
                    break
            except OSError:
                a_closed = True
                break
            time.sleep(0.05)
        self.assertTrue(a_closed)

        sb.sendall(b"b2")
        self.assertEqual(sb.recv(4096), b"b2")  # untouched
        sa.close()
        sb.close()

    def test_property_4_credential_ref_is_a_hash_never_plaintext(self):
        """The proxy authorizes by hashing the PRESENTED token and matching
        Maestro's token_hash -- presenting the hash string itself (as if it
        were a bearer secret) must NOT authorize, proving the comparison
        is sha256(presented) == stored_hash, not stored_hash == stored_hash
        or any path that would accept the hash as a credential."""
        grant, token = make_grant()
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1")
        self.harness.wait_polled()

        # presenting the hash itself must fail
        s = self.harness.connect(token=grant["token_hash"])
        resp = s.recv(4096)
        self.assertIn(b"401", resp)
        s.close()

        # presenting the real plaintext token must succeed
        s = self.harness.connect_authorized(token=token)
        s.sendall(b"real")
        self.assertEqual(s.recv(4096), b"real")
        s.close()

        # never sent the plaintext bearer token to Maestro -- only the
        # provider's own registration credential goes in the poll's
        # Authorization header
        for auth_header in self.maestro.seen_auth_headers:
            self.assertNotIn(token, auth_header or "")
        self.assertTrue(
            any((h or "").endswith("provider-registration-token") for h in self.maestro.seen_auth_headers)
        )

    def test_discovery_request_is_auth_gated_and_rewritten(self):
        """The /json/version discovery path is a separate code branch from
        the plain pipe-through path (no live-socket registration, no
        threads) -- it still must (a) require a valid grant and (b) rewrite
        Chrome's self-reported loopback address to the proxy's own."""
        discovery_upstream = FakeDiscoveryUpstream()
        self.addCleanup(discovery_upstream.stop)

        grant, token = make_grant()
        self.maestro.set_grants([grant])
        os.environ["BAS_TAILNET_IP"] = "127.0.0.1"
        os.environ["BAS_CDP_PORT"] = str(_free_port())
        os.environ["BAS_CHROME_CDP_PORT"] = str(discovery_upstream.port)
        os.environ["BAS_MAESTRO_API_URL"] = self.maestro.base_url()
        os.environ["BAS_MAESTRO_PROVIDER_ID"] = "test-provider"
        os.environ["BAS_MAESTRO_PROVIDER_TOKEN"] = "provider-registration-token"
        os.environ["BAS_GRANTS_POLL_INTERVAL"] = "0.1"
        os.environ["BAS_GRANTS_POLL_TIMEOUT"] = "5"

        global cdp_proxy_grants
        import cdp_proxy_grants as mod
        mod = importlib.reload(mod)
        self.harness = ProxyHarness.__new__(ProxyHarness)
        self.harness.mod = mod
        self.harness.port = mod.PORT
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((mod.BIND, mod.PORT))
        srv.listen(64)
        self.harness.srv = srv
        threading.Thread(target=self.harness._accept_loop, daemon=True).start()
        threading.Thread(target=mod._poll_loop, daemon=True).start()

        deadline = time.time() + 3
        while time.time() < deadline and not mod._poll_ok.is_set():
            time.sleep(0.02)
        self.assertTrue(mod._poll_ok.is_set())

        # No token: rejected before ever reaching upstream.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", mod.PORT))
        s.sendall(b"GET /json/version HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"401", s.recv(4096))
        s.close()

        # Valid token: proxied, and the loopback address gets rewritten to
        # this proxy's own BIND:PORT so a consumer can actually reach it.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", mod.PORT))
        s.sendall(f"GET /json/version?bas_token={token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        head, _, body = resp.partition(b"\r\n\r\n")
        s.close()
        self.assertIn(b"200", head)
        self.assertNotIn(str(discovery_upstream.port).encode(), body)
        self.assertIn(f"{mod.BIND}:{mod.PORT}".encode(), body)

    def test_unconfigured_provider_refuses_everything_without_crashing(self):
        os.environ["BAS_TAILNET_IP"] = "127.0.0.1"
        os.environ["BAS_CDP_PORT"] = str(_free_port())
        os.environ["BAS_CHROME_CDP_PORT"] = str(self.upstream.port)
        os.environ["BAS_MAESTRO_API_URL"] = self.maestro.base_url()
        os.environ["BAS_MAESTRO_PROVIDER_ID"] = ""
        os.environ["BAS_MAESTRO_PROVIDER_TOKEN"] = ""
        os.environ["BAS_GRANTS_POLL_INTERVAL"] = "0.1"
        os.environ["BAS_GRANTS_UNCONFIGURED_RETRY"] = "0.1"

        global cdp_proxy_grants
        import cdp_proxy_grants as mod
        mod = importlib.reload(mod)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((mod.BIND, mod.PORT))
        srv.listen(16)
        threading.Thread(target=mod._poll_loop, daemon=True).start()

        def accept_loop():
            srv.settimeout(0.5)
            while True:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=mod.handle, args=(conn, addr), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        time.sleep(0.3)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("127.0.0.1", mod.PORT))
        s.sendall(b"GET /?bas_token=whatever HTTP/1.1\r\nHost: x\r\n\r\n")
        resp = s.recv(4096)
        self.assertIn(b"401", resp)
        s.close()
        srv.close()


class TestAuditLogging(unittest.TestCase):
    """The design (decision c453ccc8 on task 5fc24712) promises audit.log
    entries for grant_issued, grant_revoked, connection_accepted and
    connection_rejected. None of the four existed in the deployed proxy
    (vault's third-reader finding, entry c542c6a1 on task 5fc24712).

    Scope narrowed per task 0522d178 entry 4245b980 (independently
    verified against PR #199 on VectiveAI/maestro, which writes
    grant_issued/grant_revoked server-side via Maestro's own issuance/
    revocation endpoints, tested there): the proxy never issues or
    revokes a grant, only enforces one, so only connection_accepted and
    connection_rejected -- the two events only the proxy can observe --
    belong here."""

    def setUp(self):
        self.maestro = FakeMaestro()
        self.upstream = FakeUpstream()
        self.addCleanup(self.maestro.stop)
        self.addCleanup(self.upstream.stop)
        self.shared_dir = tempfile.mkdtemp(prefix="bas-test-shared-")
        self.addCleanup(shutil.rmtree, self.shared_dir, ignore_errors=True)

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.harness.close()

    def test_connection_accepted_is_audited(self):
        grant, token = make_grant(service_name="tradingview")
        self.maestro.set_grants([grant])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1",
                                     shared_dir=self.shared_dir)
        self.harness.wait_polled()

        s = self.harness.connect_authorized(token=token)
        s.sendall(b"x")
        self.assertEqual(s.recv(4096), b"x")
        s.close()

        entries = [e for e in _read_audit_log(self.shared_dir) if e["event"] == "connection_accepted"]
        self.assertTrue(entries, "no connection_accepted audit entry was written")
        entry = entries[0]
        self.assertEqual(entry["service"], "tradingview")
        self.assertEqual(entry["grant_id"], grant["grant_id"])
        self.assertEqual(entry["consumer_identity"], "test-consumer")

    def test_connection_rejected_is_audited(self):
        self.maestro.set_grants([])
        self.harness = ProxyHarness(self.maestro, self.upstream, poll_interval="0.1",
                                     shared_dir=self.shared_dir)
        self.harness.wait_polled()

        s = self.harness.connect(token="not-a-real-token")
        resp = s.recv(4096)
        self.assertIn(b"401", resp)
        s.close()

        entries = [e for e in _read_audit_log(self.shared_dir) if e["event"] == "connection_rejected"]
        self.assertTrue(entries, "no connection_rejected audit entry was written")


if __name__ == "__main__":
    unittest.main(verbosity=2)
