#!/usr/bin/env python3
"""Update session-health.json with authentication metadata."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit import audit

SHARED_DIR = os.environ.get("BAS_SHARED_DIR", "/shared/browser-auth")
HEALTH_PATH = os.path.join(SHARED_DIR, "meta", "session-health.json")


def update_health(service_name: str, ttl_hours: int, auth_method: str, shared_dir: str):
    health_path = os.path.join(shared_dir, "meta", "session-health.json")
    Path(health_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(health_path, "r") as f:
            health = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        health = {}

    now = datetime.now(timezone.utc)
    health[service_name] = {
        "last_authenticated": now.isoformat(),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        "status": "active",
        "authenticated_by": auth_method,
        "last_verified": now.isoformat(),
        "verify_method": "manual_login",
    }

    with open(health_path, "w") as f:
        json.dump(health, f, indent=2)

    print(f"Updated health for {service_name}: active, TTL={ttl_hours}h")
    audit("health_update", service_name, "success", "update_health.py")


def main():
    parser = argparse.ArgumentParser(description="Update session health metadata")
    parser.add_argument("service_name", help="Service name (e.g., my-web-app)")
    parser.add_argument("--ttl", type=int, default=24, help="Session TTL in hours (default: 24)")
    parser.add_argument("--method", default="manual_novnc", help="Auth method (default: manual_novnc)")
    parser.add_argument("--shared-dir", default=None, help="Shared directory path (overrides BAS_SHARED_DIR)")
    args = parser.parse_args()

    shared_dir = args.shared_dir if args.shared_dir else SHARED_DIR
    update_health(args.service_name, args.ttl, args.method, shared_dir)


if __name__ == "__main__":
    main()
