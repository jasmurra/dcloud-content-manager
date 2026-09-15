"""Open a tool-owned Chrome window for CAMGR and read the session cookies from it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from camgr_client import (
    CAMGR_BASE,
    CAMGR_HOME,
    build_cookie_header,
    probe_camgr_login,
)


def _header_from_playwright_cookies(raw: list[dict[str, Any]]) -> str:
    cookies: dict[str, str] = {}
    for item in raw:
        domain = str(item.get("domain") or "").lstrip(".").lower()
        if "dcloud-camgr.cisco.com" not in domain:
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name and value:
            cookies[name] = value
    return build_cookie_header(cookies)


def capture_camgr_session(profile_dir: Path, timeout_s: float = 180) -> tuple[str | None, str]:
    """Open Chrome under a private profile, wait until CAMGR is signed in, return Cookie header."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Playwright is intentionally not installed; Connect uses your own Chrome tab.
        return None, "This build connects through your open CAMGR tab. Click Connect to CAMGR."

    profile_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(30.0, timeout_s)
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chrome",
                headless=False,
                viewport={"width": 1200, "height": 860},
                ignore_default_args=["--enable-automation"],
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            return None, f"Could not open Chrome for CAMGR ({exc})."

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(CAMGR_HOME, wait_until="domcontentloaded", timeout=60_000)
            while time.time() < deadline:
                if not context.pages:
                    return None, "CAMGR window was closed before sign-in finished."
                header = _header_from_playwright_cookies(context.cookies(CAMGR_BASE + "/"))
                if header:
                    probed = probe_camgr_login(header)
                    if probed.get("loggedIn"):
                        return header, str(probed.get("message") or "CAMGR session is active.")
                time.sleep(1.5)
            return None, "Timed out waiting for CAMGR sign-in in the tool window."
        finally:
            try:
                context.close()
            except Exception:
                pass
