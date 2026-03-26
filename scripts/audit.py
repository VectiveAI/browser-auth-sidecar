"""Centralized audit logging for browser-auth-sidecar session operations."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SHARED_DIR = os.environ.get("BAS_SHARED_DIR", "/shared/browser-auth")
AUDIT_LOG = os.path.join(SHARED_DIR, "meta", "audit.log")


def audit(event_type: str, service: str, result: str, source: str = "script"):
    """Append a structured audit entry to the shared audit log.

    Args:
        event_type: Category of event (e.g., session_export, health_update, session_check).
        service: Consumer-defined service name being audited.
        result: Outcome string (e.g., success, failure, failure_expired).
        source: Identifier for the script or agent producing this entry.
    """
    Path(AUDIT_LOG).parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "service": service,
        "result": result,
        "source": source,
    })
    with open(AUDIT_LOG, "a") as f:
        f.write(entry + "\n")
