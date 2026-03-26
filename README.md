# Browser Auth Sidecar

A Docker Compose pattern that gives container-isolated AI agents authenticated access to web services that lack API-level authentication. A persistent Chrome instance runs inside a KasmVNC container with noVNC for human login, and a socat CDP proxy makes the authenticated browser controllable by any container on the shared Docker network.

## Architecture

```
+-------------+     +--------------------------+     +------------------+
|   Human     |     |  browser container       |     |  consumer        |
|   (noVNC)   +---->|  kasmweb/chrome:1.16.1   |<----+  (your AI agent) |
| :6901       |     |  Chrome + CDP :9222      |     |  CDP :9223       |
| localhost   |     +-----------+--------------+     +------------------+
|  only       |                 |
+-------------+     +-----------+--------------+
                    |  cdp-proxy container     |
                    |  alpine/socat            |
                    |  :9223 -> 127.0.0.1:9222 |
                    |  network_mode:           |
                    |    service:browser       |
                    +--------------------------+

        Shared volume: /shared/browser-auth/
        +-- meta/session-health.json
        +-- meta/audit.log
        +-- playwright/<service>.json
```

The CDP proxy exists because Chrome 131+ silently ignores `--remote-debugging-address=0.0.0.0` and always binds to `127.0.0.1`. The socat sidecar shares the browser's network namespace and forwards CDP traffic to other containers.

## Quickstart

```bash
git clone https://github.com/VectiveAI/browser-auth-sidecar.git
cd browser-auth-sidecar

cp .env.sample .env
# Edit .env — set BAS_VNC_PASSWORD to a secure value

docker compose up -d
```

Open noVNC to log into your target web service:

```
https://127.0.0.1:6901
```

Export the session and record health (from a container with Playwright installed, or locally):

```bash
python scripts/export_session.py my-service --cdp-url http://localhost:9223
python scripts/update_health.py my-service --ttl 24
```

Your AI agent container joins the `browser-auth-net` network and connects via CDP:

```python
# From inside a consumer container
from playwright.async_api import async_playwright

async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp("ws://browser-auth-browser:9223")
    page = browser.contexts[0].pages[0]
    # The page is already authenticated
```

## Configuration

All configuration uses environment variables with the `BAS_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `BAS_VNC_PASSWORD` | *(required)* | Password for noVNC web access |
| `BAS_CONTAINER_PREFIX` | `browser-auth` | Prefix for container names |
| `BAS_NOVNC_PORT` | `6901` | Host port for noVNC (localhost only) |
| `BAS_CDP_PORT` | `9223` | CDP proxy listen port |
| `BAS_PROFILE_DIR` | `./data/kasm-profile` | Host path for Chrome profile persistence |
| `BAS_SHARED_DIR` | `./data/shared` | Host path for shared session data |
| `BAS_NETWORK` | `browser-auth-net` | Docker network name |

## Session Management

### Workflow

1. **Start** the sidecar with `docker compose up -d`
2. **Log in** via noVNC at `https://127.0.0.1:6901`
3. **Export** cookies with `scripts/export_session.py <service> --cdp-url http://localhost:9223`
4. **Record** TTL with `scripts/update_health.py <service> --ttl 24`
5. **Consume** from your agent via CDP at `ws://<browser-container>:9223`
6. **Check validity** using `session_utils.check_session_valid("<service>")`

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/export_session.py` | Extract cookies/storage to Playwright format via CDP |
| `scripts/update_health.py` | Record authentication timestamp and TTL |
| `scripts/session_utils.py` | Check session validity, get session paths |
| `scripts/audit.py` | Centralized audit logging |

All scripts accept `--shared-dir` to override the `BAS_SHARED_DIR` environment variable.

### Shared Volume Layout

```
/shared/browser-auth/
  meta/
    session-health.json    # Per-service TTL tracking
    audit.log              # Append-only JSONL audit trail
  playwright/
    <service>.json         # Exported Playwright storage_state
```

## Security Model

- **noVNC**: Bound to `127.0.0.1` only. Access via SSH tunnel or VPN. Never exposed publicly.
- **CDP**: Internal to the Docker network. Not port-mapped to the host. Only containers on `browser-auth-net` can reach it.
- **Session files**: Written with `0600` permissions. Treat the shared volume as sensitive data.
- **Chrome profile**: Contains cached credentials. Protect the host directory.

## Consumer Integration

### Joining the Network

Your consumer's `docker-compose.yml` references the sidecar network as external:

```yaml
services:
  my-agent:
    image: my-agent:latest
    volumes:
      - /path/to/shared:/shared/browser-auth
    networks:
      - browser-auth

networks:
  browser-auth:
    name: browser-auth-net
    external: true
```

### CDP Connection

From inside the consumer container, CDP is available at:

```
ws://<BAS_CONTAINER_PREFIX>-browser:<BAS_CDP_PORT>
```

With defaults: `ws://browser-auth-browser:9223`

See `examples/` for complete consumer setups.

## Examples

- **[basic-consumer](examples/basic-consumer/)** — Minimal Python container that connects via CDP and prints the browser version.
- **[openclaw-consumer](examples/openclaw-consumer/)** — Integration pattern for [OpenClaw](https://github.com/nichochar/openclaw) AI agents.

## Known Limitations

- **Chrome CDP binding bug**: Chrome 131+ ignores `--remote-debugging-address=0.0.0.0`. The socat sidecar is the workaround. This is a [known Chromium issue](https://issues.chromium.org/issues/issues).
- **Server-side sessions**: Services that store sessions entirely server-side (opaque session IDs) cannot be meaningfully exported via Playwright storage_state. These require live CDP access to the browser instance.
- **Manual login required**: Initial authentication must be performed by a human via noVNC. Automated login is not included (and is fragile for services with CAPTCHAs, 2FA, etc.).
- **Single browser instance**: The pattern runs one Chrome instance. Multiple concurrent authenticated services share the same browser profile.
- **KasmVNC startup hooks**: `kasm_post_run_root.sh` is unreliable for custom scripts. The socat sidecar pattern is intentional.

## Contributing

Contributions are welcome. Please open an issue to discuss proposed changes before submitting a pull request.

## License

[MIT](LICENSE) - Copyright (c) 2026 [Vective AI](https://vectiveai.com)
