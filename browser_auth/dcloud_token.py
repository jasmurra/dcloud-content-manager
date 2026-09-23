"""dCloud OAuth token normalization, env lookup, and validation."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

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


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature."""
    clean = normalize_dcloud_token(token)
    parts = clean.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def jwt_expires_at(token: str) -> float:
    """Unix expiry from a JWT access token payload, or 0 if unknown."""
    exp = jwt_claims(token).get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return 0.0


def _claim_text(claims: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(claims.get(key) or "").strip()
        if text:
            return text
    return ""


def display_name_from_claims(claims: dict[str, Any], user_id: str = "") -> str:
    """Prefer first + last name. Ignore a name claim that is just the CEC id."""
    if not isinstance(claims, dict):
        return ""
    given = _claim_text(claims, "given_name", "first_name", "firstName")
    family = _claim_text(claims, "family_name", "last_name", "lastName")
    joined = " ".join(part for part in (given, family) if part)
    raw = _claim_text(claims, "name", "full_name", "fullName", "displayName")
    uid = (user_id or "").strip().lower()
    if joined and joined.lower() != uid:
        return joined
    if raw and raw.lower() not in {uid, ""} and "@" not in raw:
        return raw
    return joined or raw


def jwt_session_user(token: str) -> dict[str, str]:
    """CEC id / name / email from a dCloud access token, for the header menu.

    dCloud access tokens only carry ccoid and email_address. given_name /
    family_name show up on OpenID userinfo and GET /api/users/{ccoid}.
    """
    claims = jwt_claims(token)
    email = _claim_text(claims, "email", "email_address")
    user_id = _claim_text(claims, "ccoid", "preferred_username")
    if not user_id and "@" in email:
        user_id = email.split("@", 1)[0]
    if not user_id:
        user_id = _claim_text(claims, "uid", "sub")
        if "@" in user_id:
            user_id = user_id.split("@", 1)[0]
    name = display_name_from_claims(claims, user_id) or user_id
    return {"id": user_id, "name": name, "email": email}


def fetch_session_display_name(
    token: str,
    *,
    site: str = "",
    user_id: str = "",
) -> str:
    """Look up first/last name. The access token JWT does not include them."""
    clean = normalize_dcloud_token(token)
    if not clean:
        return ""
    headers = dcloud_auth_header(clean)
    uid = (user_id or jwt_session_user(clean)["id"]).strip()
    try:
        response = requests.get(
            "https://id.cisco.com/oauth2/default/v1/userinfo",
            headers=headers,
            verify=False,
            timeout=8,
        )
        if response.status_code < 400:
            body = response.json()
            if isinstance(body, dict):
                name = display_name_from_claims(body, uid)
                if name and name.lower() != uid.lower():
                    return name
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        pass
    site_code = (site or "rtp").strip().lower() or "rtp"
    if uid:
        try:
            response = requests.get(
                f"https://dcloud2-{site_code}.cisco.com/api/users/{uid}",
                headers=headers,
                verify=False,
                timeout=8,
            )
            if response.status_code < 400:
                body = response.json()
                if isinstance(body, dict):
                    name = display_name_from_claims(body, uid)
                    if name and name.lower() != uid.lower():
                        return name
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            pass
    return ""


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
