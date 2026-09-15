"""dCloud OAuth token normalization, env lookup, and validation."""

from __future__ import annotations

import base64
import json
import os
import re

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_TIMEOUT = 25
JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")

# dCloud regional sites (Local Storage origin is https://dcloud2-{site}.cisco.com).
DCLOUD_SITES = ("rtp", "sjc", "lon", "sng", "syd")
VALIDATION_SITES = DCLOUD_SITES
VALIDATION_PATHS = ("/api/banners", "/api/sessions?limit=1")

# DCLOUD_BASIC_TOKEN is the OAuth client credential for id.cisco.com — not a bearer token.
DCLOUD_TOKEN_ENV_KEYS = (
    "DCLOUD_TOKEN",
    "dtoken",
)


def normalize_dcloud_token(raw: str) -> str:
    """Strip Bearer/Basic prefix and surrounding quotes."""
    token = (raw or "").strip().strip('"').strip("'")
    for prefix in ("Bearer ", "bearer ", "Basic ", "basic "):
        if token.startswith(prefix):
            token = token[len(prefix) :].strip()
    return token


def dcloud_auth_header(token: str) -> dict[str, str]:
    """dCloud API calls use OAuth Bearer access tokens (see dcloud_oauth.get_header)."""
    clean = normalize_dcloud_token(token)
    scheme = "Basic" if clean and not JWT_RE.match(clean) and "." not in clean else "Bearer"
    return {
        "Authorization": f"{scheme} {clean}",
        "Content-Type": "application/json",
    }


def jwt_expires_at(token: str) -> float:
    """Unix expiry from a JWT access token payload, or 0 if unknown."""
    clean = normalize_dcloud_token(token)
    parts = clean.split(".")
    if len(parts) < 2:
        return 0.0
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return 0.0
    exp = data.get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return 0.0


def effective_dcloud_token(override: str | None = None) -> str | None:
    if override and override.strip():
        return normalize_dcloud_token(override)
    for key in DCLOUD_TOKEN_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return normalize_dcloud_token(value)
    return None


def validate_dcloud_token(
    token: str,
    site: str | None = None,
    *,
    sites: tuple[str, ...] | None = None,
    paths: tuple[str, ...] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Lightweight check that a token is accepted by dCloud (not necessarily admin)."""
    clean = normalize_dcloud_token(token)
    if not clean:
        return False, "Token is empty."

    if site and site.strip():
        check_sites = (site.strip().lower(),)
    elif sites:
        check_sites = sites
    else:
        check_sites = VALIDATION_SITES
    check_paths = paths or VALIDATION_PATHS

    last_detail = "Token rejected on all dCloud sites checked."

    for site_code in check_sites:
        if not site_code:
            continue
        for path in check_paths:
            url = f"https://dcloud2-{site_code}.cisco.com{path}"
            try:
                response = requests.get(
                    url,
                    headers=dcloud_auth_header(clean),
                    verify=False,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_detail = f"Could not reach dCloud ({site_code.upper()}): {exc}"
                continue

            if response.status_code == 401:
                last_detail = f"Token rejected (401) on dcloud2-{site_code}.cisco.com."
                continue
            if response.status_code >= 500:
                last_detail = f"dcloud2-{site_code}.cisco.com returned {response.status_code}."
                continue

            return True, (
                f"Token accepted by dcloud2-{site_code}.cisco.com "
                f"({response.status_code} on {path})."
            )

    return False, last_detail
