#!/usr/bin/env bash
# Run native mode in foreground: headless Chrome + CDP proxy + login viewer.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "cp .env.sample .env (install.sh does this)"; exit 1; }
set -a; source .env; set +a

CHROME="$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || { echo "Chrome missing — run ./install.sh"; exit 1; }
mkdir -p "$BAS_PROFILE_DIR"
rm -f "$BAS_PROFILE_DIR"/Singleton* 2>/dev/null || true

"$CHROME" --headless=new \
  --remote-debugging-port="${BAS_CHROME_CDP_PORT:-9222}" \
  --remote-allow-origins=* \
  --user-data-dir="$BAS_PROFILE_DIR" \
  --no-first-run --disable-search-engine-choice-screen \
  --disable-blink-features=AutomationControlled \
  --window-size=1440,900 &
CHROME_PID=$!

python3 cdp_proxy.py &
PROXY_PID=$!
python3 viewer/viewer_server.py &
VIEWER_PID=$!

trap 'kill $CHROME_PID $PROXY_PID $VIEWER_PID 2>/dev/null; wait 2>/dev/null' EXIT INT TERM
echo "native mode up: viewer http://${BAS_TAILNET_IP}:${BAS_VIEWER_PORT}/?bas_token=$BAS_CDP_TOKEN"
wait
