#!/usr/bin/env python3
"""Minimal CDP connection test — verifies the browser sidecar is reachable."""
import json
import os
import urllib.request


def main():
    host = os.environ.get("BAS_CDP_HOST", "browser-auth-browser")
    port = os.environ.get("BAS_CDP_PORT", "9223")
    url = f"http://{host}:{port}/json/version"

    print(f"Connecting to CDP endpoint: {url}")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            print(f"Browser:  {data.get('Browser', 'unknown')}")
            print(f"Protocol: {data.get('Protocol-Version', 'unknown')}")
            print(f"User-Agent: {data.get('User-Agent', 'unknown')}")
            print("Connection successful.")
    except Exception as e:
        print(f"ERROR: Could not connect to CDP endpoint: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
