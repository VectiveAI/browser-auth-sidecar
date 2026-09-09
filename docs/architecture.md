# Architecture

## Overview

Browser-auth-sidecar provides a persistent, authenticated Chrome instance that Docker-isolated AI agents can control via the Chrome DevTools Protocol (CDP). It solves the problem of accessing web services that rely on cookie/session-based authentication and have no API-level auth alternative.

## Component Diagram

```
                                    Docker Network: browser-auth-net
                    +-----------------------------------------------------------+
                    |                                                           |
+----------+       |  +------------------+      +-------------------+          |
| noVNC    |       |  | browser          |      | cdp-proxy         |          |
| (human)  +--------->| kasmweb/chrome   |      | grants-aware      |          |
| :6901    |       |  |                  |      | (Python, stdlib)  |          |
+----------+       |  | Chrome :9222     |<-----+ :9223 -> :9222    |          |
  localhost        |  | (127.0.0.1 only) |      | network_mode:     |          |
  only             |  |                  |      |   service:browser  |          |
                    |  +------------------+      +-------------------+          |
                    |         |                          ^  |                   |
                    |         v                          |  | outbound poll     |
                    |  /shared/browser-auth/             |  | (~1s, Bearer      |
                    |  +------------------+              |  |  provider token)  |
                    |  | meta/            |      +-------------------+          |
                    |  |   session-health |      | consumer          |          |
                    |  |   audit.log      |      | (your AI agent)   |          |
                    |  | playwright/      |      | connects to       |          |
                    |  |   *.json         |      | browser:9223 with |          |
                    |  +------------------+      | its own grant     |          |
                    |                             | token             |          |
                    |                             +-------------------+          |
                    +-----------------------------------------------------------+
                                                              |
                                                              v
                                              GET /api/v1/providers/{id}/grants
                                                        (Maestro)
```

## Why the CDP Proxy Exists

Chrome 131+ contains a bug where `--remote-debugging-address=0.0.0.0` is silently ignored. Chrome always binds its CDP port to `127.0.0.1`, making it unreachable from other containers on the Docker network.

The workaround is a sidecar using `network_mode: "service:browser"`. This places the proxy container in the same network namespace as the browser container, meaning it can reach Chrome's `127.0.0.1:9222`. It then listens on a configurable port (default `9223`) on all interfaces, making CDP accessible to other containers on the Docker network.

This is the primary reason this pattern exists as infrastructure rather than a set of Chrome flags.

### Why Not Fix It Inside the Browser Container?

KasmVNC's `kasm_post_run_root.sh` hook is unreliable for custom startup scripts. The sidecar-proxy pattern is intentional and avoids depending on KasmVNC internals.

## Grants (per-agent CDP access)

`cdp-proxy` is a grants-aware Python proxy (`docker/cdp_proxy_grants.py`), not a dumb relay. Every connection must present a per-agent bearer token, as a `bas_token` query parameter or an `Authorization: Bearer` header, the same way it's presented to Chrome's own discovery endpoints.

**Maestro is the source of truth, never this proxy's own memory.** There is no grants file on this host. `cdp-proxy` polls an outbound endpoint on its own `BAS_GRANTS_POLL_INTERVAL` (default ~1s):

```
GET {BAS_MAESTRO_API_URL}/api/v1/providers/{BAS_MAESTRO_PROVIDER_ID}/grants
Authorization: Bearer <BAS_MAESTRO_PROVIDER_TOKEN>
```

`BAS_MAESTRO_PROVIDER_TOKEN` is this host's own connection credential from its Maestro provider registration — not a separate secret minted for polling. Maestro uses it both to authenticate the poll and to confirm it's this provider asking for its own grants, never another provider's.

The response carries every grant issued against this provider — including already-revoked and already-expired ones, not a live-only view — and the proxy rebuilds its entire in-memory index from it on every poll. A restart of `cdp-proxy` starts with an empty index and just re-polls; nothing about grant state is ever written to this host, so there is nothing here to lose, and a restart can never "un-revoke" anything.

**Revoke and expiry are enforced in real time, including against already-open connections.** Gating only new connections would leave an already-open CDP WebSocket alive until the consumer disconnected itself. Instead the proxy tracks live sockets per `grant_id` and, on each poll, diffs the newly-fetched grant state against what it saw last poll; a grant that just transitioned to revoked or expired has its live sockets closed immediately.

**Revoke touches only that one grant.** It invalidates one `token_hash` and closes that grant's own live sockets — never the browser container, never noVNC, never `session-health.json`. Every other grant, and the human's own noVNC session, are untouched.

**Grant records never carry a plaintext token.** A grant's `token_hash` is a `sha256:<hex>` digest; the plaintext bearer token is minted once by Maestro's grant-issuance path and handed to the consumer at issuance — it is never stored recoverable anywhere, including on this proxy.

Until `BAS_MAESTRO_PROVIDER_ID` and `BAS_MAESTRO_PROVIDER_TOKEN` are set (i.e. before this host has been registered as a Maestro provider), the proxy runs, polls, gets nothing usable, and refuses every connection. It fails closed, never open.

**`service_name` on a grant is declared/audit scope, not a CDP-enforced sandbox.** CDP itself has no concept of restricting a connection to one origin's cookies — once authenticated, a connection has the same full CDP access any consumer has always had. A grant governs *who* may connect and *for how long*, revocably; it does not restrict *what* a connected agent can touch once inside.

## Shared Volume Structure

The `/shared/browser-auth` directory is mounted into both the browser container and any consumer containers. It serves as the communication channel for session metadata.

```
/shared/browser-auth/
  meta/
    session-health.json    # TTL-based session validity per service
    audit.log              # Append-only structured audit trail (JSONL)
  playwright/
    <service-name>.json    # Exported Playwright storage_state files
```

`session-health.json` and grants are deliberately orthogonal: the former tracks whether the human is still authenticated to the target site, the latter tracks whether a given agent is currently permitted to use the CDP connection at all. Either can be true while the other is false.

### session-health.json

Tracks authentication status per service:

```json
{
  "my-web-app": {
    "last_authenticated": "2026-03-26T10:00:00+00:00",
    "expires_at": "2026-03-27T10:00:00+00:00",
    "status": "active",
    "authenticated_by": "manual_novnc",
    "last_verified": "2026-03-26T10:00:00+00:00",
    "verify_method": "manual_login"
  }
}
```

### audit.log

Append-only JSONL file recording all session operations:

```json
{"timestamp": "2026-03-26T10:00:00+00:00", "event": "session_export", "service": "my-web-app", "result": "success", "source": "export_session.py"}
```

## Session Lifecycle

1. **Start the sidecar**: `docker compose up -d` brings up Chrome with noVNC and the CDP proxy.

2. **Authenticate manually**: A human opens noVNC at `https://127.0.0.1:6901`, navigates to the target web service, and completes the login flow. This establishes cookies and session tokens in the browser profile.

3. **Export session** (optional): Run `export_session.py` to extract cookies and local storage into a Playwright `storage_state` JSON file. Useful for services that use cookie-based auth.

4. **Record health**: Run `update_health.py` to record the authentication timestamp and TTL in `session-health.json`. Consumer agents check this to know whether the session is still valid.

5. **Consumer access**: an AI agent holding a grant connects to Chrome via CDP at `<browser-container>:9223`, presenting its grant's bearer token, and issues commands against the authenticated browser context.

6. **Session expiry**: When the TTL elapses, `check_session_valid()` returns `False`. The consumer should stop or alert. Re-authentication requires repeating steps 2-4. This is independent of grant expiry, which the proxy itself enforces (see Grants above).

## Security Considerations

### noVNC Access

The noVNC port (6901) is bound to `127.0.0.1` only. It is never exposed to the public network. Access it via SSH tunnel or Tailscale. The VNC password provides an additional authentication layer.

### CDP Access

CDP is only accessible within the Docker network, and only to a connection presenting a currently-valid grant token (see Grants above). It is not port-mapped to the host (the proxy runs inside the browser's network namespace). Being joined to `browser-auth-net` is necessary to reach the CDP endpoint at all, but no longer sufficient on its own — a grant is also required.

### Session Data

Exported session files (`playwright/*.json`) contain authentication tokens and are written with `0600` permissions. The shared volume should be treated as sensitive data.

### Chrome Profile

The Chrome profile directory (`/home/kasm-user`) persists browser state across restarts. It contains cookies, local storage, and cached credentials. Protect the host directory accordingly.

## Server-Side Sessions

Some web services use server-side sessions where the authentication state lives entirely on the server, with only an opaque session ID in the browser cookie. For these services:

- Exported Playwright storage_state files may not be useful for other clients
- The only reliable access method is through the live CDP connection to the authenticated browser instance
- Session validity depends on the server's session timeout, not the exported cookie expiry

The architecture supports both patterns: cookie-exportable sessions (via Playwright files) and server-side sessions (via live CDP).
