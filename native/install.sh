#!/usr/bin/env bash
# Native mode install — Apple Silicon (or Intel) macOS, no sudo required.
# Installs Google Chrome to ~/Applications and seeds .env with a generated token.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p "$HOME/Applications" "$HOME/browser-auth/chrome-profile"

if [ ! -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "==> downloading Chrome (universal)"
  curl -fSL --retry 2 -o /tmp/chrome.dmg \
    "https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg"
  mnt="/tmp/chrome-mnt"
  rm -rf "$mnt"
  hdiutil attach -nobrowse -mountpoint "$mnt" /tmp/chrome.dmg >/dev/null || { echo "dmg mount failed"; exit 1; }
  cp -R "$mnt/Google Chrome.app" "$HOME/Applications/"
  hdiutil detach "$mnt" >/dev/null
  rm -f /tmp/chrome.dmg
  echo "==> installed: $HOME/Applications/Google Chrome.app"
else
  echo "==> Chrome already in ~/Applications"
fi

if [ ! -f .env ]; then
  cp .env.sample .env
fi
if grep -q '^BAS_CDP_TOKEN=$' .env; then
  tok=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/^BAS_CDP_TOKEN=$/BAS_CDP_TOKEN=$tok/" .env
  else
    sed -i "s/^BAS_CDP_TOKEN=$/BAS_CDP_TOKEN=$tok/" .env
  fi
  echo "==> generated BAS_CDP_TOKEN in .env"
fi

echo "done. next: ./run.sh (foreground) or ./install-launchd.sh (persistent)"
