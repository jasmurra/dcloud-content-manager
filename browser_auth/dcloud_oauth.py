"""Fetch dCloud OAuth access tokens via id.cisco.com (idac get-tokens-dcloud-bot flow)."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import requests

from browser_auth.dcloud_token import (
    DCLOUD_SITES,
    effective_dcloud_token,
    jwt_expires_at,
    normalize_dcloud_token,
)

DCLOUD_TOKEN_URL = "https://id.cisco.com/oauth2/default/v1/token"
DCLOUD_OAUTH_SCOPE = "openid profile email offline_access"
DCLOUD_SPA_CLIENT_ID = "2fc326b2-f31c-415f-b7e1-bb76e908a79e"
DCLOUD_LOGIN_SCOPE = "openid profile email offline_access cci_admemberOf groups"
TIMEOUT_SECS = 30

USERNAME_KEYS = ("DCLOUD_USERNAME", "dc_uname", "DC_UNAME")
PASSWORD_KEYS = ("DCLOUD_PASSWORD", "dc_passwd", "DC_PASSWD")
BASIC_TOKEN_KEYS = ("DCLOUD_BASIC_TOKEN", "dc_basic_token", "DC_BASIC_TOKEN")


def _load_env_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _first_env_value(env: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def get_dcloud_oauth_credentials(
    env_file: Path | None = None,
) -> tuple[str, str, str]:
    """Read username, password, and OAuth client Basic token from .env."""
    env = _load_env_file(env_file)
    username = _first_env_value(env, USERNAME_KEYS)
    password = _first_env_value(env, PASSWORD_KEYS)
    basic_token = _first_env_value(env, BASIC_TOKEN_KEYS)
    return username, password, basic_token


def dcloud_auth_status(
    env_file: Path,
    env_example_file: Path | None = None,
) -> dict[str, Any]:
    username, password, basic_token = get_dcloud_oauth_credentials(env_file)
    static_token = effective_dcloud_token()
    return {
        "env_path": str(env_file),
        "env_example_path": str(env_example_file or env_file.parent / ".env.example"),
        "env_exists": env_file.is_file(),
        "oauth_configured": bool(username and password and basic_token),
        "has_username": bool(username),
        "has_password": bool(password),
        "has_basic_token": bool(basic_token),
        "has_static_token": bool(static_token),
    }


def fetch_dcloud_access_token(env_file: Path | None = None) -> tuple[str, str | None]:
    """
    Exchange dCloud bot credentials for a short-lived bearer token.
    Same POST as idac WebExIdentityBroker.get_dcloud_token().
    Returns (access_token, error_message).
    """
    username, password, basic_token = get_dcloud_oauth_credentials(env_file)
    env_label = str(env_file) if env_file else ".env"
    if not username or not password or not basic_token:
        return "", (
            f"dCloud auto-auth is not configured. Add DCLOUD_USERNAME, "
            f"DCLOUD_PASSWORD, and DCLOUD_BASIC_TOKEN to {env_label} "
            f"(same values as idac get-tokens-dcloud-bot: dc_uname, dc_passwd, dc_basic_token)."
        )

    payload = (
        "grant_type=password"
        f"&username={quote(username, safe='')}"
        f"&password={quote(password, safe='')}"
        f"&scope={quote(DCLOUD_OAUTH_SCOPE, safe='')}"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic_token.strip()}",
    }

    try:
        response = requests.post(
            DCLOUD_TOKEN_URL,
            headers=headers,
            data=payload,
            timeout=TIMEOUT_SECS,
        )
    except requests.RequestException as exc:
        return "", f"dCloud token request failed: {exc}"

    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        return "", f"dCloud token request failed ({response.status_code}): {detail}"

    try:
        body = response.json()
    except ValueError:
        return "", "dCloud token response was not JSON."

    access_token = (body.get("access_token") or "").strip()
    if not access_token:
        return "", "dCloud token response did not include access_token."

    return access_token, None


def build_dcloud_login_url(site: str = "rtp", state: str | None = None) -> tuple[str, str]:
    """
    Cisco SSO authorize URL used by dCloud (opens in a popup; callback lands on dcloud2-{site}).
    Returns (url, state).
    """
    site_code = (site or "rtp").strip().lower()
    if site_code not in DCLOUD_SITES:
        site_code = "rtp"
    oauth_state = state or secrets.token_urlsafe(24)
    redirect_uri = f"https://dcloud2-{site_code}.cisco.com/authenticate"
    params = {
        "response_type": "code",
        "scope": DCLOUD_LOGIN_SCOPE,
        "client_id": DCLOUD_SPA_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": oauth_state,
    }
    url = f"https://id.cisco.com/oauth2/default/v1/authorize?{urlencode(params)}"
    return url, oauth_state


def exchange_dcloud_access_code(
    site: str,
    access_code: str,
) -> tuple[str, str, int, str | None]:
    """
    Exchange the SSO authorization code for dCloud tokens (same as dCloud app after /authenticate).

    POST /api/public/ui-tokens with {accessCode, redirectUri} — returns access + refresh tokens.
    """
    site_code = (site or "rtp").strip().lower()
    if site_code not in DCLOUD_SITES:
        site_code = "rtp"
    code = (access_code or "").strip()
    if not code:
        return "", "", 0, "Authorization code is empty."

    redirect_uri = f"https://dcloud2-{site_code}.cisco.com/authenticate"
    url = f"https://dcloud2-{site_code}.cisco.com/api/public/ui-tokens"
    try:
        response = requests.post(
            url,
            json={"accessCode": code, "redirectUri": redirect_uri},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            verify=False,
            timeout=TIMEOUT_SECS,
        )
    except requests.RequestException as exc:
        return "", "", 0, f"dCloud login exchange failed: {exc}"

    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        return "", "", 0, f"dCloud login exchange failed ({response.status_code}): {detail}"

    try:
        body = response.json()
    except ValueError:
        return "", "", 0, "dCloud login exchange response was not JSON."

    access = normalize_dcloud_token(body.get("accessToken") or body.get("access_token") or "")
    refresh = (body.get("refreshToken") or body.get("refresh_token") or "").strip()
    expires_in = int(body.get("expiresIn") or body.get("expires_in") or 3600)
    if not access:
        return "", "", 0, "dCloud login exchange did not include accessToken."
    return access, refresh, expires_in, None


def refresh_dcloud_user_token(
    site: str,
    refresh_token: str,
) -> tuple[str, str, int, str | None]:
    """
    Exchange a dCloud refresh token (dc_p_r) for a new access token via ui-tokens.
    Same POST body as dCloud app.bundle.js loginWithCurrentSession().
    Returns (access_token, refresh_token, expires_in, error_message).
    """
    site_code = (site or "rtp").strip().lower()
    if site_code not in DCLOUD_SITES:
        site_code = "rtp"
    refresh = (refresh_token or "").strip()
    if not refresh:
        return "", "", 0, "Refresh token is empty."

    url = f"https://dcloud2-{site_code}.cisco.com/api/public/ui-tokens"
    try:
        response = requests.post(
            url,
            json={"refreshToken": refresh},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            verify=False,
            timeout=TIMEOUT_SECS,
        )
    except requests.RequestException as exc:
        return "", "", 0, f"dCloud refresh request failed: {exc}"

    if response.status_code >= 400:
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        return "", "", 0, f"dCloud refresh failed ({response.status_code}): {detail}"

    try:
        body = response.json()
    except ValueError:
        return "", "", 0, "dCloud refresh response was not JSON."

    access = normalize_dcloud_token(body.get("accessToken") or body.get("access_token") or "")
    new_refresh = (body.get("refreshToken") or body.get("refresh_token") or refresh).strip()
    expires_in = int(body.get("expiresIn") or body.get("expires_in") or 3600)
    if not access:
        return "", "", 0, "dCloud refresh response did not include accessToken."
    return access, new_refresh, expires_in, None


def resolve_dcloud_token(
    source: str,
    override: str | None = None,
    *,
    env_file: Path | None = None,
    progress: Callable[[str], None] | None = None,
    allow_env_fallback: bool = True,
) -> tuple[str, str | None]:
    """
    Resolve a dCloud bearer token by source.
    source: oauth | paste | env | browser
    Returns (token, error_message).
    """
    mode = (source or "browser").strip().lower()

    if mode == "oauth":
        if not allow_env_fallback:
            return "", (
                "Env-based auto login is disabled. Import from Chrome or paste your own token."
            )
        if progress:
            progress("Fetching dCloud access token (OAuth password grant)...")
        token, err = fetch_dcloud_access_token(env_file)
        if err:
            return "", err
        if progress:
            progress("dCloud access token retrieved.")
        return token, None

    if mode == "browser":
        from browser_auth.browser_dcloud_auth import try_import_dcloud_token

        if progress:
            progress("Importing dCloud token from Chrome Local Storage...")
        token, message = try_import_dcloud_token()
        if not token:
            return "", message or "Could not import dCloud token from browser."
        if progress:
            progress(message or "dCloud token imported from browser.")
        return normalize_dcloud_token(token), None

    if mode == "login":
        return "", "Use Log in to dCloud or Import from browser — login mode resolves on the server."

    # paste — override first; optional DCLOUD_TOKEN in environment when allow_env_fallback
    token = normalize_dcloud_token(override or "") if override and override.strip() else ""
    if not token and allow_env_fallback:
        token = effective_dcloud_token(override) or ""
    if not token:
        return "", "dCloud token is required — paste below or click Import from browser."
    return token, None
