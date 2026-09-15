"""Import dCloud OAuth access token from local Chrome profiles (Local Storage + cookies)."""

from __future__ import annotations

import re
import shutil
import sqlite3
import string
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from browser_auth.chrome_profiles import chrome_cookie_files, chrome_leveldb_dirs
from browser_auth.dcloud_token import (
    DCLOUD_SITES,
    normalize_dcloud_token,
    validate_dcloud_token,
)

JWT_BYTES_RE = re.compile(
    rb"eyJ[A-Za-z0-9_-]{10,300}\.[A-Za-z0-9_-]{10,6000}\.[A-Za-z0-9_-]{10,700}"
)
# Okta / dCloud often nest the bearer under accessToken keys in Local Storage JSON.
OKTA_ACCESS_TOKEN_RE = re.compile(
    rb'(?:accessToken|access_token)["\']?\s*:\s*["\'](eyJ[A-Za-z0-9_-]{10,300}\.[A-Za-z0-9_-]{10,6000}\.[A-Za-z0-9_-]{10,700})["\']',
    re.IGNORECASE,
)
OKTA_NESTED_ACCESS_TOKEN_RE = re.compile(
    rb'accessToken["\']?\s*:\s*\{[^}]{0,400}?accessToken["\']?\s*:\s*["\'](eyJ[A-Za-z0-9_-]{10,300}\.[A-Za-z0-9_-]{10,6000}\.[A-Za-z0-9_-]{10,700})["\']',
    re.IGNORECASE | re.DOTALL,
)
STORAGE_KEY_HINTS = (
    b"dc_p_a",  # dCloud partner access token (Local Storage on dcloud2-*.cisco.com)
    b"access_token",
    b"accessToken",
    b"TB_AUTH_TOKEN",
    b"authToken",
    b"dcloud_token",
    b"token",
)
# dCloud stores the bearer JWT under Local Storage key dc_p_a (refresh is dc_p_r).
DCLOUD_ACCESS_KEY_RE = re.compile(
    rb"dc_p_a[\x00\"':\s]*?(eyJ[A-Za-z0-9_-]{10,300}\.[A-Za-z0-9_-]{10,6000}\.[A-Za-z0-9_-]{10,700})",
)
DCLOUD_REFRESH_KEY_RE = re.compile(
    rb"dc_p_r[\x00\"':=\s]+(?:[\"']([A-Za-z0-9_\-+=/.]{20,8000})[\"']|([A-Za-z0-9_\-+=/.]{20,8000}))",
    re.IGNORECASE,
)
# Chromium leveldb Local Storage: dc_p_r,\x01<token> or dc_p_r\x01<len-bytes>\x01<token>
DCLOUD_REFRESH_LEVELDB_RE = re.compile(
    rb"dc_p_r(?:,\x01|\x01)[\x00-\xff]{0,32}?([A-Za-z0-9_\-+=/.]{20,8000})",
)
OKTA_REFRESH_TOKEN_RE = re.compile(
    rb'refreshToken["\']?\s*:\s*["\']([A-Za-z0-9_\-+=/.]{20,8000})["\']',
    re.IGNORECASE,
)
OKTA_NESTED_REFRESH_RE = re.compile(
    rb'refreshToken["\']?\s*:\s*\{[^}]{0,800}?refreshToken["\']?\s*:\s*["\']([A-Za-z0-9_\-+=/.]{20,8000})["\']',
    re.IGNORECASE | re.DOTALL,
)
DCLOUD_ORIGIN_SITE_RE = re.compile(
    rb"dcloud2-(rtp|sjc|lon|sng|syd)\.cisco\.com",
    re.IGNORECASE,
)
ORIGIN_HINTS = tuple(f"dcloud2-{site}.cisco.com".encode() for site in DCLOUD_SITES) + (
    b"dcloud.cisco.com",
    b"ciscodcloud.com",
    b"cat-dcloud.com",
)
IMPORT_VALIDATE_TIMEOUT = 4
IMPORT_VALIDATE_PATHS = ("/api/banners",)
MAX_DCLOUD_PROFILES = 5
MAX_DCPA_TOKENS_PER_PROFILE = 8
MAX_GENERIC_CANDIDATES = 8
PREFERRED_COOKIE_NAMES = (
    "access_token",
    "accessToken",
    "dcloud_token",
    "authToken",
    "okta-token-storage",
)


MAX_SCAN_FILE_BYTES = 16 * 1024 * 1024
MAX_FILES_PER_PROFILE = 30

# SSO authorization code captured during /authenticate redirect (same as dCloud ui-tokens login body).
ACCESS_CODE_RE = re.compile(
    rb"(?:accessCode[\"']?\s*:\s*[\"']|authenticate\?code=|pageQueryString[^?]*\?code=)([A-Za-z0-9_\-]{20,256})",
    re.IGNORECASE,
)

_used_access_codes: set[str] = set()
_REFRESH_VALUE_BYTES = bytes(string.ascii_letters + string.digits + "_-+=/.", "ascii")
# Chrome/WebKit history timestamps: microseconds since 1601-01-01 UTC.
_WEBKIT_EPOCH_OFFSET_SEC = 11644473600
_login_exchange_started_at: float = 0.0
FAST_LOGIN_MAX_PROFILES = 2


def _iter_leveldb_files(leveldb_dir):
    files = []
    for pattern in ("*.log", "*.ldb"):
        files.extend(leveldb_dir.glob(pattern))
    files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return files[:MAX_FILES_PER_PROFILE]


def _read_file_bytes(path) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= MAX_SCAN_FILE_BYTES:
                return handle.read()
            # Large leveldb files: read both ends (keys can be split across tail-only reads).
            half = MAX_SCAN_FILE_BYTES // 2
            handle.seek(0)
            head = handle.read(half)
            handle.seek(max(0, size - half))
            tail = handle.read()
            return head + tail
    except OSError:
        return b""


def _is_plausible_refresh_token(token: str) -> bool:
    val = (token or "").strip()
    if len(val) < 20 or len(val) > 8000:
        return False
    if val.startswith("eyJ") and val.count(".") == 2:
        return False
    return all(ch in string.ascii_letters + string.digits + "_-+=/." for ch in val)


def _extract_value_after_storage_key(data: bytes, key: bytes) -> list[str]:
    """Parse Chrome leveldb local-storage values that follow a key like dc_p_r."""
    if not data or not key:
        return []
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        val = (raw or "").strip()
        if _is_plausible_refresh_token(val) and val not in seen:
            seen.add(val)
            values.append(val)

    # UTF-16-LE key (some Chromium builds interleave null bytes).
    utf16_key = key.decode("ascii", errors="ignore").encode("utf-16-le")
    search_keys = (key, utf16_key)

    for search_key in search_keys:
        start = 0
        key_len = len(search_key)
        while True:
            pos = data.find(search_key, start)
            if pos == -1:
                break
            cursor = pos + key_len
            while cursor < len(data) and data[cursor] in b'\x00,"\':= \t\n\r\x01':
                cursor += 1
            # Leveldb may embed length/metadata bytes before the opaque refresh value.
            if cursor < len(data) and data[cursor] not in _REFRESH_VALUE_BYTES:
                scan_end = min(len(data), cursor + 32)
                while cursor < scan_end and data[cursor] not in _REFRESH_VALUE_BYTES:
                    cursor += 1
            if cursor >= len(data):
                start = pos + key_len
                continue
            raw = ""
            if data[cursor:cursor + 1] == b'"':
                cursor += 1
                end = cursor
                while end < len(data) and data[end:end + 1] != b'"':
                    end += 1
                raw = data[cursor:end].decode("ascii", errors="ignore")
            else:
                end = cursor
                while end < len(data) and data[end:end + 1] in _REFRESH_VALUE_BYTES:
                    end += 1
                raw = data[cursor:end].decode("ascii", errors="ignore")
            if not raw:
                leveldb_match = DCLOUD_REFRESH_LEVELDB_RE.search(data, pos)
                if leveldb_match:
                    raw = leveldb_match.group(1).decode("ascii", errors="ignore")
            add(raw)
            start = pos + key_len

    return values


def _chrome_history_files() -> list[Path]:
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.is_dir():
        return []
    patterns = ("Default/History", "Profile */History")
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(base.glob(pattern))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(
        (p for p in paths if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        text = str(path)
        if "Snapshots" in text or "Backup" in text:
            continue
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def mark_login_exchange_started() -> None:
    """Call when opening the SSO popup — limits OAuth code search to this login attempt."""
    global _login_exchange_started_at
    _login_exchange_started_at = time.time()


def _unix_to_chrome_us(unix_ts: float) -> int:
    return int((unix_ts + _WEBKIT_EPOCH_OFFSET_SEC) * 1_000_000)


def _scan_access_codes_from_chrome_history(
    since_unix: float | None = None,
    site_hint: str | None = None,
    limit: int = 8,
) -> list[tuple[str, str | None]]:
    """Recent SSO authorization codes from Chrome history (/authenticate?code=...)."""
    found: list[tuple[str, str | None, int]] = []
    seen: set[str] = set()
    prefer = (site_hint or "").strip().lower()
    cutoff = _unix_to_chrome_us(since_unix) if since_unix else 0

    for hist_path in _chrome_history_files()[:MAX_DCLOUD_PROFILES]:
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(hist_path, tmp_path)
            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            try:
                if cutoff:
                    rows = conn.execute(
                        "SELECT url, last_visit_time FROM urls "
                        "WHERE url LIKE '%authenticate%code=%' AND last_visit_time >= ? "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (cutoff, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT url, last_visit_time FROM urls "
                        "WHERE url LIKE '%authenticate%code=%' "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            continue
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass
        for url, visit_time in rows:
            url_text = str(url or "")
            match = re.search(
                r"dcloud2-(rtp|sjc|lon|sng|syd)\.cisco\.com/authenticate",
                url_text,
                re.IGNORECASE,
            )
            site = match.group(1).lower() if match else None
            code = (parse_qs(urlparse(url_text).query).get("code") or [""])[0].strip()
            if len(code) < 20 or code in seen or code in _used_access_codes:
                continue
            seen.add(code)
            rank = int(visit_time or 0)
            if prefer and site == prefer:
                rank += 10_000_000_000_000
            found.append((code, site, rank))

    found.sort(key=lambda item: item[2], reverse=True)
    return [(code, site) for code, site, _rank in found[:limit]]


def _is_plausible_access_token(token: str) -> bool:
    token = token.strip()
    if len(token) < 100 or len(token) > 7000:
        return False
    return token.count(".") == 2 and token.startswith("eyJ")


def _jwt_candidates_from_blob(data: bytes) -> list[str]:
    if not data:
        return []

    found: list[str] = []

    def add_token(raw: str) -> None:
        token = normalize_dcloud_token(raw)
        if _is_plausible_access_token(token):
            found.append(token)

    def add_from_window(window: bytes) -> None:
        for match in DCLOUD_ACCESS_KEY_RE.finditer(window):
            add_token(match.group(1).decode("ascii", errors="ignore"))
        for match in OKTA_NESTED_ACCESS_TOKEN_RE.finditer(window):
            add_token(match.group(1).decode("ascii", errors="ignore"))
        for match in OKTA_ACCESS_TOKEN_RE.finditer(window):
            add_token(match.group(1).decode("ascii", errors="ignore"))
        for match in JWT_BYTES_RE.finditer(window):
            add_token(match.group(0).decode("ascii", errors="ignore"))

    for match in DCLOUD_ACCESS_KEY_RE.finditer(data):
        add_token(match.group(1).decode("ascii", errors="ignore"))

    for origin_hint in ORIGIN_HINTS:
        start = 0
        while True:
            pos = data.find(origin_hint, start)
            if pos == -1:
                break
            window = data[max(0, pos - 400) : pos + 12000]
            if any(hint in window for hint in STORAGE_KEY_HINTS):
                add_from_window(window)
            start = pos + len(origin_hint)

    for hint in STORAGE_KEY_HINTS:
        start = 0
        while True:
            pos = data.find(hint, start)
            if pos == -1:
                break
            add_from_window(data[pos : pos + 8000])
            start = pos + len(hint)

    deduped: list[str] = []
    seen: set[str] = set()
    for token in found:
        if token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _profile_dcloud_score(data: bytes) -> int:
    score = 0
    for origin_hint in ORIGIN_HINTS:
        score += data.count(origin_hint) * 3
    for hint in STORAGE_KEY_HINTS:
        score += data.count(hint)
    if b"okta-token-storage" in data:
        score += 5
    if b"dc_p_a" in data:
        score += 25
    return score


def _site_hint_near(data: bytes, pos: int) -> str | None:
    """Site code from nearest dcloud2-{site}.cisco.com origin in leveldb."""
    window = data[max(0, pos - 800) : pos + 200]
    matches = DCLOUD_ORIGIN_SITE_RE.findall(window)
    if not matches:
        return None
    return matches[-1].decode("ascii").lower()


def _dc_pa_tokens_from_blob(data: bytes) -> list[str]:
    """Tokens stored under dCloud Local Storage key dc_p_a (highest priority)."""
    return [token for token, _site in _dc_pa_entries_from_blob(data)]


def _refresh_candidates_from_blob(data: bytes) -> list[str]:
    """Collect dc_p_r / refreshToken values from a Chrome Local Storage blob."""
    if not data:
        return []
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        val = (raw or "").strip()
        if not _is_plausible_refresh_token(val) or val in seen:
            return
        seen.add(val)
        candidates.append(val)

    for val in _extract_value_after_storage_key(data, b"dc_p_r"):
        add(val)

    for match in DCLOUD_REFRESH_LEVELDB_RE.finditer(data):
        add(match.group(1).decode("ascii", errors="ignore"))

    for match in DCLOUD_REFRESH_KEY_RE.finditer(data):
        for group in match.groups():
            if group:
                add(group.decode("ascii", errors="ignore"))

    for match in OKTA_REFRESH_TOKEN_RE.finditer(data):
        add(match.group(1).decode("ascii", errors="ignore"))

    for match in OKTA_NESTED_REFRESH_RE.finditer(data):
        add(match.group(1).decode("ascii", errors="ignore"))

    return candidates


def _best_refresh_for_position(data: bytes, pos: int) -> str | None:
    """Pick dc_p_r nearest to a dc_p_a entry, with wider file fallback."""
    window = data[max(0, pos - 4000) : pos + 12000]
    window_candidates = _refresh_candidates_from_blob(window)
    if window_candidates:
        return window_candidates[0]
    file_candidates = _refresh_candidates_from_blob(data)
    if len(file_candidates) == 1:
        return file_candidates[0]
    return None


def _scan_refresh_from_chrome_storage() -> str | None:
    """Last-resort scan for dc_p_r across Chrome profiles."""
    dirs = chrome_leveldb_dirs()
    for leveldb_dir in dirs[:MAX_DCLOUD_PROFILES]:
        for path in _iter_leveldb_files(leveldb_dir):
            data = _read_file_bytes(path)
            if not data or b"dc_p_r" not in data:
                continue
            candidates = _refresh_candidates_from_blob(data)
            if candidates:
                return candidates[0]
    return None


def _refresh_token_near(data: bytes, pos: int) -> str | None:
    """Opaque refresh token stored beside dc_p_a as dc_p_r in Local Storage."""
    return _best_refresh_for_position(data, pos)


def _dc_pa_entries_from_blob(data: bytes) -> list[tuple[str, str | None]]:
    """dc_p_a bearer tokens with optional regional site hint from Local Storage origin."""
    entries: list[tuple[str, int, str | None]] = []
    seen: set[str] = set()
    start = 0
    while True:
        pos = data.find(b"dc_p_a", start)
        if pos == -1:
            break
        window = data[pos : pos + 5000]
        token = None
        key_match = DCLOUD_ACCESS_KEY_RE.search(window)
        if key_match:
            token = normalize_dcloud_token(key_match.group(1).decode("ascii", errors="ignore"))
        else:
            jwt_match = JWT_BYTES_RE.search(window)
            if jwt_match:
                token = normalize_dcloud_token(jwt_match.group(0).decode("ascii", errors="ignore"))
        if token and _is_plausible_access_token(token) and token not in seen:
            seen.add(token)
            entries.append((token, pos, _site_hint_near(data, pos)))
        start = pos + 5

    entries.sort(key=lambda item: item[1], reverse=True)
    return [(token, site_hint) for token, _pos, site_hint in entries]


def _dc_session_entries_from_blob(data: bytes) -> list[tuple[str, str | None, str | None]]:
    """dc_p_a access tokens with optional dc_p_r refresh and regional site hint."""
    sessions: list[tuple[str, str | None, str | None, int]] = []
    seen: set[str] = set()
    start = 0
    while True:
        pos = data.find(b"dc_p_a", start)
        if pos == -1:
            break
        window = data[pos : pos + 5000]
        token = None
        key_match = DCLOUD_ACCESS_KEY_RE.search(window)
        if key_match:
            token = normalize_dcloud_token(key_match.group(1).decode("ascii", errors="ignore"))
        else:
            jwt_match = JWT_BYTES_RE.search(window)
            if jwt_match:
                token = normalize_dcloud_token(jwt_match.group(0).decode("ascii", errors="ignore"))
        if token and _is_plausible_access_token(token) and token not in seen:
            seen.add(token)
            sessions.append(
                (
                    token,
                    _best_refresh_for_position(data, pos),
                    _site_hint_near(data, pos),
                    pos,
                )
            )
        start = pos + 5

    sessions.sort(key=lambda item: item[3], reverse=True)
    return [(token, refresh, site_hint) for token, refresh, site_hint, _pos in sessions]


def _quick_validate_dcloud_token(token: str, site_hint: str | None = None) -> tuple[bool, str]:
    """Validate import candidate; try hinted region first, then remaining sites."""
    if site_hint:
        ok, message = validate_dcloud_token(
            token,
            sites=(site_hint,),
            paths=IMPORT_VALIDATE_PATHS,
            timeout=IMPORT_VALIDATE_TIMEOUT,
        )
        if ok:
            return ok, message
        other_sites = tuple(site for site in DCLOUD_SITES if site != site_hint)
        if other_sites:
            return validate_dcloud_token(
                token,
                sites=other_sites,
                paths=IMPORT_VALIDATE_PATHS,
                timeout=IMPORT_VALIDATE_TIMEOUT,
            )
    return validate_dcloud_token(
        token,
        sites=DCLOUD_SITES,
        paths=IMPORT_VALIDATE_PATHS,
        timeout=IMPORT_VALIDATE_TIMEOUT,
    )


def _cookies_from_jar(jar) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".")
        if not any(part in domain for part in ("cisco.com", "ciscodcloud.com", "cat-dcloud.com")):
            continue
        if cookie.name and cookie.value is not None:
            cookies[cookie.name] = cookie.value
    return cookies


def _token_from_cookies(cookies: dict[str, str]) -> str | None:
    for name in PREFERRED_COOKIE_NAMES:
        value = (cookies.get(name) or "").strip()
        if not value:
            continue
        if value.startswith("{"):
            for match in JWT_BYTES_RE.finditer(value.encode("utf-8", errors="ignore")):
                return match.group(0).decode("ascii", errors="ignore")
        clean = normalize_dcloud_token(value)
        if clean:
            return clean
    for value in cookies.values():
        if value.startswith("eyJ") and "." in value:
            return normalize_dcloud_token(value)
    return None


def _try_renew_from_refresh(
    refresh: str,
    site_hint: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Use dc_p_r from Chrome when dc_p_a access token is expired."""
    if not (refresh or "").strip():
        return None, None, None
    from browser_auth.dcloud_oauth import refresh_dcloud_user_token

    sites = [site_hint] if site_hint else list(DCLOUD_SITES)
    seen: set[str] = set()
    for site in sites:
        site_code = (site or "").strip().lower()
        if not site_code or site_code in seen:
            continue
        seen.add(site_code)
        access, new_refresh, _expires, err = refresh_dcloud_user_token(site_code, refresh)
        if not access:
            continue
        ok, _message = _quick_validate_dcloud_token(access, site_code)
        if ok:
            return access, new_refresh or refresh, site_code
    return None, None, None


def _scan_access_codes_from_chrome_storage(
    since_unix: float | None = None,
    site_hint: str | None = None,
    limit: int = 8,
) -> list[tuple[str, str | None]]:
    """SSO codes from recent Chrome history plus leveldb (slow — use storage backup only)."""
    found: list[tuple[str, int, str | None]] = []
    seen: set[str] = set()

    for code, site in _scan_access_codes_from_chrome_history(since_unix, site_hint, limit):
        if code not in seen:
            seen.add(code)
            found.append((code, 2_000_000_000, site))

    dirs = chrome_leveldb_dirs()
    for leveldb_dir in dirs[:MAX_DCLOUD_PROFILES]:
        for path in _iter_leveldb_files(leveldb_dir):
            data = _read_file_bytes(path)
            if not data:
                continue
            if b"accessCode" not in data and b"authenticate" not in data and b"code=" not in data:
                continue
            mtime = path.stat().st_mtime if path.exists() else 0
            if since_unix and mtime < since_unix - 60:
                continue
            for match in ACCESS_CODE_RE.finditer(data):
                code = match.group(1).decode("ascii", errors="ignore").strip()
                if len(code) < 20 or code in seen or code in _used_access_codes:
                    continue
                seen.add(code)
                found.append((code, int(mtime), _site_hint_near(data, match.start())))

    found.sort(key=lambda item: item[1], reverse=True)
    return [(code, site) for code, _mtime, site in found[:limit]]


def try_exchange_login_from_chrome(
    site_hint: str | None = None,
    *,
    since_unix: float | None = None,
    max_codes: int = 3,
    history_only: bool = True,
) -> tuple[str | None, str | None, str | None, str]:
    """
    Complete SSO login by exchanging the authorization code via dCloud ui-tokens.

    Fast path: read /authenticate?code= from Chrome history and POST ui-tokens (gets refreshToken).
    """
    from browser_auth.dcloud_oauth import exchange_dcloud_access_code

    prefer = (site_hint or "").strip().lower()
    if since_unix is None and _login_exchange_started_at:
        since_unix = _login_exchange_started_at - 30

    if history_only:
        codes = _scan_access_codes_from_chrome_history(since_unix, prefer, max_codes)
    else:
        codes = _scan_access_codes_from_chrome_storage(since_unix, prefer, max_codes)

    if not codes:
        return None, None, None, ""

    sites: list[str] = []
    if prefer:
        sites.append(prefer)
    for code in DCLOUD_SITES:
        if code not in sites:
            sites.append(code)

    last_err = ""
    for code, hint in codes:
        try_sites: list[str] = []
        if hint:
            try_sites.append(hint)
        if prefer and prefer not in try_sites:
            try_sites.insert(0, prefer)
        for site in sites:
            if site not in try_sites:
                try_sites.append(site)
        seen_sites: set[str] = set()
        for site in try_sites:
            site_code = (site or "").strip().lower()
            if not site_code or site_code in seen_sites:
                continue
            seen_sites.add(site_code)
            access, refresh, _expires_in, err = exchange_dcloud_access_code(site_code, code)
            if access:
                _used_access_codes.add(code)
                site_out = hint or prefer or site_code
                refresh_note = " Auto-refresh enabled." if refresh else ""
                return (
                    access,
                    refresh or None,
                    site_out,
                    f"Logged in via SSO token exchange ({site_out.upper()}).{refresh_note}",
                )
            if err:
                last_err = err
                if any(
                    token in err.lower()
                    for token in ("invalid", "expired", "used", "already", "401", "400")
                ):
                    _used_access_codes.add(code)
                    break

    return None, None, None, last_err


def _import_session_from_chrome_storage(
    max_profiles: int | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    dirs = chrome_leveldb_dirs()
    if not dirs:
        return None, None, None, "Chrome: no Local Storage databases found."

    found_dc_pa = False
    profile_limit = max_profiles if max_profiles is not None else MAX_DCLOUD_PROFILES

    for leveldb_dir in dirs[:profile_limit]:
        profile = leveldb_dir.parent.parent.name
        for path in _iter_leveldb_files(leveldb_dir):
            data = _read_file_bytes(path)
            if not data or b"dc_p_a" not in data:
                continue
            found_dc_pa = True
            for token, refresh, site_hint in _dc_session_entries_from_blob(data)[
                :MAX_DCPA_TOKENS_PER_PROFILE
            ]:
                ok, message = _quick_validate_dcloud_token(token, site_hint)
                if ok:
                    if not refresh:
                        file_candidates = _refresh_candidates_from_blob(data)
                        refresh = file_candidates[0] if file_candidates else None
                    if not refresh:
                        refresh = _scan_refresh_from_chrome_storage()
                    if not refresh:
                        try:
                            import browser_cookie3
                        except ImportError:
                            browser_cookie3 = None
                        if browser_cookie3 is not None:
                            refresh = _refresh_from_chrome_cookies(browser_cookie3)
                    site_note = f" ({site_hint.upper()})" if site_hint else ""
                    if refresh:
                        refresh_note = " Auto-refresh enabled."
                    else:
                        refresh_note = (
                            " Auto-refresh unavailable (no refresh token in Chrome) — "
                            "log in again if the access token expires."
                        )
                    return (
                        token,
                        refresh,
                        site_hint,
                        f"Imported login from Chrome{site_note} — {message}{refresh_note}",
                    )
                if refresh:
                    renewed, new_refresh, renewed_site = _try_renew_from_refresh(refresh, site_hint)
                    if renewed:
                        site_note = f" ({renewed_site.upper()})" if renewed_site else ""
                        return (
                            renewed,
                            new_refresh or refresh,
                            renewed_site or site_hint,
                            f"Renewed login from Chrome{site_note}. Auto-refresh enabled.",
                        )

    if found_dc_pa:
        return (
            None,
            None,
            None,
            "Your dCloud session has expired. Click Log in to dCloud in Step 1 to sign in again.",
        )

    return (
        None,
        None,
        None,
        "Not signed into dCloud in Chrome. Click Log in to dCloud in Step 1.",
    )


def _import_from_chrome_storage() -> tuple[str | None, str]:
    token, _refresh, _site, message = _import_session_from_chrome_storage()
    return token, message


def _refresh_from_chrome_cookies(browser_cookie3) -> str | None:
    files = chrome_cookie_files()
    for cookie_file in files:
        try:
            jar = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                domain_name="cisco.com",
            )
        except Exception:
            continue
        cookies = _cookies_from_jar(jar)
        for name in ("dc_p_r", "refreshToken", "refresh_token"):
            val = (cookies.get(name) or "").strip()
            if _is_plausible_refresh_token(val):
                return val
    return None


def _import_from_chrome_cookies(browser_cookie3) -> tuple[str | None, str]:
    files = chrome_cookie_files()
    if not files:
        return None, "Chrome cookies: no cookie database found."

    for cookie_file in files:
        profile = cookie_file.parent.name
        try:
            jar = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                domain_name="cisco.com",
            )
        except Exception:
            continue
        token = _token_from_cookies(_cookies_from_jar(jar))
        if not token:
            continue
        ok, detail = _quick_validate_dcloud_token(token)
        if ok:
            return token, f"Imported from Chrome {profile} cookies — {detail}"
    return None, ""


def try_import_dcloud_session(
    *,
    full_scan: bool = False,
    oauth_only: bool = False,
    storage_only: bool = False,
    site_hint: str | None = None,
) -> tuple[str | None, str | None, str | None, str]:
    """
    Resolve dCloud session tokens.

    storage_only=True: Chrome scan only (use while SSO popup is open — do not steal OAuth code).
    oauth_only=True: SSO code exchange only (after popup closes / fallback).
    full_scan=True: scan more Chrome profiles (Import button).
    """
    prefer = (site_hint or "").strip().lower() or None
    since = _login_exchange_started_at - 30 if _login_exchange_started_at else None

    if not storage_only:
        token, refresh, site, message = try_exchange_login_from_chrome(
            prefer,
            since_unix=since,
            max_codes=5 if full_scan else 3,
            history_only=not full_scan,
        )
        if token:
            return token, refresh, site, message

    if oauth_only:
        return None, None, None, "Waiting for dCloud popup to finish sign-in…"

    profile_limit = MAX_DCLOUD_PROFILES if full_scan else FAST_LOGIN_MAX_PROFILES
    token, refresh, site, message = _import_session_from_chrome_storage(max_profiles=profile_limit)
    if token:
        return token, refresh, site, message

    try:
        import browser_cookie3
    except ImportError:
        return None, None, None, (
            f"{message} Install browser-cookie3 for cookie fallback."
        )

    cookie_token, cookie_note = _import_from_chrome_cookies(browser_cookie3)
    if cookie_token:
        refresh = _scan_refresh_from_chrome_storage() or _refresh_from_chrome_cookies(browser_cookie3)
        note = cookie_note
        if refresh:
            note = f"{cookie_note} Auto-refresh enabled."
        else:
            note = (
                f"{cookie_note} Auto-refresh unavailable (no refresh token in Chrome) — "
                "log in via dCloud in Chrome, then Import again."
            )
        return cookie_token, refresh, None, note

    notes = [message]
    if cookie_note:
        notes.append(cookie_note)
    combined = " ".join(n for n in notes if n).strip()
    if combined and "step 1" not in combined.lower():
        combined = f"{combined} Click Log in to dCloud in Step 1, or paste a token below."
    return None, None, None, combined or "Click Log in to dCloud in Step 1."


def try_import_dcloud_session_legacy() -> tuple[str | None, str | None, str | None, str]:
    """Backward-compatible wrapper."""
    return try_import_dcloud_session(full_scan=True)


def scan_dcloud_refresh_from_chrome() -> str | None:
    """Read dc_p_r from Chrome when access was saved without a refresh token."""
    return _scan_refresh_from_chrome_storage()


def clear_login_scan_state() -> None:
    """Forget consumed SSO authorization codes (e.g. after Clear login session)."""
    global _login_exchange_started_at
    _used_access_codes.clear()
    _login_exchange_started_at = 0.0


def try_import_dcloud_token() -> tuple[str | None, str]:
    """
    Read dCloud OAuth access token from Chrome Local Storage (primary) or cookies (fallback).

    Like IDAC cookie import, this reads on-disk browser data — not an open tab.
    """
    token, _refresh, _site, message = try_import_dcloud_session(full_scan=True)
    return token, message
