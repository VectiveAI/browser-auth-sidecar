#!/usr/bin/env bash
# Persistent native mode: per-user launchd agent (no sudo).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.vective.browser-auth.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/browser-auth/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.vective.browser-auth</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/browser-auth/logs/out.log</string>
  <key>StandardErrorPath</key><string>$HOME/browser-auth/logs/err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $PLIST"
echo "logs: $HOME/browser-auth/logs/{out,err}.log"
echo "stop: launchctl unload $PLIST"