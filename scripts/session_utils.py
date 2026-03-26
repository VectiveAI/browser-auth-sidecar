"""Shared session validation utilities for browser-auth-sidecar consumers."""
import json
import os
from datetime import datetime, timezone

from audit import audit

SHARED_DIR = os.environ.get("BAS_SHARED_DIR", "/shared/browser-auth")
HEALTH_PATH = os.path.join(SHARED_DIR, "meta", "session-health.json")


def check_session_valid(service_name: str) -> bool:
    """Check if a service session is valid based on session-health.json TTL."""
    try:
        with open(HEALTH_PATH, "r") as f:
            health = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        audit("session_check", service_name, "failure_no_health_file")
        return False

    entry = health.get(service_name)
    if not entry:
        audit("session_check", service_name, "failure_no_entry")
        return False

    expires_at = entry.get("expires_at")
    if not expires_at:
        audit("session_check", service_name, "failure_no_expiry")
        return False

    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now >= expiry:
            audit("session_check", service_name, "failure_expired")
            return False
    except (ValueError, TypeError):
        audit("session_check", service_name, "failure_parse_error")
        return False

    audit("session_check", service_name, "success")
    return True


def get_session_path(service_name: str) -> str:
    """Get the Playwright storage_state file path for a service."""
    return os.path.join(SHARED_DIR, "playwright", f"{service_name}.json")


def get_session_status(service_name: str) -> dict:
    """Get full session status for a service."""
    try:
        with open(HEALTH_PATH, "r") as f:
            health = json.load(f)
        return health.get(service_name, {"status": "unknown"})
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "unknown"}
