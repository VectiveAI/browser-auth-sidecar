# OpenClaw Consumer Example

This example shows how to connect an [OpenClaw](https://github.com/nichochar/openclaw) AI agent to browser-auth-sidecar for authenticated web access via Chrome DevTools Protocol (CDP).

## Setup

1. Start the browser sidecar from the repository root:

   ```bash
   cp .env.sample .env
   # Edit .env — set BAS_VNC_PASSWORD
   docker compose up -d
   ```

2. Open noVNC in your browser and log into the target web service:

   ```
   https://127.0.0.1:6901
   ```

3. Export the session (from a machine with Playwright installed):

   ```bash
   python scripts/export_session.py my-service --cdp-url http://localhost:9223
   ```

4. Update `browser-profile.json` with the correct CDP endpoint and place it in OpenClaw's config directory.

5. Start the OpenClaw consumer:

   ```bash
   cd examples/openclaw-consumer
   cp ../../.env.sample .env
   # Set OPENCLAW_GATEWAY_TOKEN and adjust BAS_* vars if needed
   docker compose up -d
   ```

## CDP Browser Profile

The `browser-profile.json` template configures OpenClaw to use CDP for browser access instead of launching its own browser. Key fields:

- `cdpUrl`: Points to the socat CDP proxy running inside the sidecar network.
- `sharedDir`: Path where exported session data and health metadata are stored.

Copy `browser-profile.json` into your OpenClaw config directory (`./config/` by default) and adjust the values for your deployment.

## How It Works

```
OpenClaw Agent
    |
    |  CDP (ws://browser-auth-browser:9223)
    v
CDP Proxy (socat) -- network_mode: service:browser
    |
    |  TCP:127.0.0.1:9222
    v
Chrome (KasmVNC) -- authenticated session
```

The agent sends CDP commands through the socat proxy to the authenticated Chrome instance. Session cookies and local storage are available through the live browser context.

## Shared Volume

The `/shared/browser-auth` mount gives the agent read access to:

- `meta/session-health.json` — TTL-based session validity
- `playwright/*.json` — Exported Playwright storage_state files
- `meta/audit.log` — Audit trail of session operations
