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
| (human)  +--------->| kasmweb/chrome   |      | alpine/socat      |          |
| :6901    |       |  |                  |      |                   |          |
+----------+       |  | Chrome :9222     |<-----+ :9223 -> :9222    |          |
  localhost        |  | (127.0.0.1 only) |      | network_mode:     |          |
  only             |  |                  |      |   service:browser  |          |
                    |  +------------------+      +-------------------+          |
                    |         |                          ^                      |
                    |         v                          |                      |
                    |  /shared/browser-auth/             | CDP (ws://)          |
                    |  +------------------+              |                      |
                    |  | meta/            |      +-------------------+          |
                    |  |   session-health |      | consumer          |          |
                    |  |   audit.log      |      | (your AI agent)   |          |
                    |  | playwright/      |      | connects to       |          |
                    |  |   *.json         |      | browser:9223      |          |
                    |  +------------------+      +-------------------+          |
                    |                                                           |
                    +-----------------------------------------------------------+
```

## Why the CDP Proxy Exists

Chrome 131+ contains a bug where `--remote-debugging-address=0.0.0.0` is silently ignored. Chrome always binds its CDP port to `127.0.0.1`, making it unreachable from other containers on the Docker network.

The workaround is an `alpine/socat` sidecar using `network_mode: "service:browser"`. This places the socat container in the same network namespace as the browser container, meaning socat can reach Chrome's `127.0.0.1:9222`. Socat then listens on a configurable port (default `9223`) on all interfaces, making CDP accessible to other containers on the Docker network.

This is the primary reason this pattern exists as infrastructure rather than a set of Chrome flags.

### Why Not Fix It Inside the Browser Container?

KasmVNC's `kasm_post_run_root.sh` hook is unreliable for custom startup scripts. The socat sidecar pattern is intentional and avoids depending on KasmVNC internals.

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

5. **Consumer access**: AI agent containers on the same Docker network connect to Chrome via CDP at `<browser-container>:9223` and issue commands against the authenticated browser context.

6. **Session expiry**: When the TTL elapses, `check_session_valid()` returns `False`. The consumer should stop or alert. Re-authentication requires repeating steps 2-4.

## Security Considerations

### noVNC Access

The noVNC port (6901) is bound to `127.0.0.1` only. It is never exposed to the public network. Access it via SSH tunnel or Tailscale. The VNC password provides an additional authentication layer.

### CDP Access

CDP is only accessible within the Docker network. It is not port-mapped to the host (the socat proxy runs inside the browser's network namespace). Only containers explicitly joined to the `browser-auth-net` network can reach the CDP endpoint.

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
