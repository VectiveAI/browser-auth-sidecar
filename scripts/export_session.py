#!/usr/bin/env python3
"""Export browser session to Playwright storage_state format via CDP."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from audit import audit

SHARED_DIR = os.environ.get("BAS_SHARED_DIR", "/shared/browser-auth")


async def export_session(service_name: str, cdp_url: str, shared_dir: str):
    from playwright.async_api import async_playwright

    output_path = os.path.join(shared_dir, "playwright", f"{service_name}.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            print(f"ERROR: No browser contexts found. Is Chrome open with a page?")
            audit("session_export", service_name, "failure", "export_session.py")
            return False

        context = contexts[0]
        await context.storage_state(path=output_path)
        os.chmod(output_path, 0o600)

        print(f"Session exported to {output_path}")
        audit("session_export", service_name, "success", "export_session.py")
        return True


def main():
    parser = argparse.ArgumentParser(description="Export browser session to Playwright format")
    parser.add_argument("service_name", help="Service name (e.g., my-web-app)")
    parser.add_argument("--cdp-url", default="http://localhost:9222", help="Chrome CDP endpoint")
    parser.add_argument("--shared-dir", default=None, help="Shared directory path (overrides BAS_SHARED_DIR)")
    args = parser.parse_args()

    shared_dir = args.shared_dir if args.shared_dir else SHARED_DIR
    success = asyncio.run(export_session(args.service_name, args.cdp_url, shared_dir))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
