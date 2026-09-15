"""Import board/idac session cookies from local browser stores (macOS)."""

from __future__ import annotations

from browser_auth.chrome_profiles import chrome_cookie_files

PREFERRED_COOKIE_NAMES = (
    "idac-dashboard-session",
    "idac-auth-session",
    "JSESSIONID",
    "session",
    "token_key",
)


def _cookies_from_jar(jar) -> dict[str, str]:
    cookies_by_name: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".")
        if "cat-dcloud.com" not in domain:
            continue
        if cookie.name and cookie.value is not None:
            cookies_by_name[cookie.name] = cookie.value
    return cookies_by_name


def _cookie_header(cookies_by_name: dict[str, str]) -> str:
    ordered_names = list(PREFERRED_COOKIE_NAMES) + sorted(
        n for n in cookies_by_name if n not in PREFERRED_COOKIE_NAMES
    )
    parts: list[str] = []
    seen: set[str] = set()
    for cookie_name in ordered_names:
        if cookie_name in cookies_by_name and cookie_name not in seen:
            parts.append(f"{cookie_name}={cookies_by_name[cookie_name]}")
            seen.add(cookie_name)
    return "; ".join(parts)


def _score_cookies(cookies_by_name: dict[str, str]) -> int:
    score = len(cookies_by_name)
    if "idac-dashboard-session" in cookies_by_name:
        score += 20
    if "idac-auth-session" in cookies_by_name:
        score += 10
    if "session" in cookies_by_name:
        score += 5
    return score


def _import_chrome_cookies(browser_cookie3) -> tuple[str | None, str]:
    files = chrome_cookie_files()
    if not files:
        return None, "Chrome: no cookie database found."

    best_cookies: dict[str, str] = {}
    best_profile = ""
    profiles_checked = 0

    for cookie_file in files:
        profile = cookie_file.parent.name
        profiles_checked += 1
        try:
            jar = browser_cookie3.chrome(
                cookie_file=str(cookie_file),
                domain_name="cat-dcloud.com",
            )
        except Exception:
            continue

        cookies = _cookies_from_jar(jar)
        if _score_cookies(cookies) > _score_cookies(best_cookies):
            best_cookies = cookies
            best_profile = profile

    if best_cookies:
        header = _cookie_header(best_cookies)
        if header:
            return header, f"Imported from Chrome {best_profile} ({len(header.split('; '))} cookie(s))"

    return (
        None,
        f"Chrome: scanned {profiles_checked} profile(s) — none had board/idac cookies. "
        "Your login tab may use a profile we couldn't decrypt; paste Cookie from DevTools instead.",
    )


def try_import_idac_cookie() -> tuple[str | None, str]:
    """
    Read cookies from Chrome, Firefox, or Safari cookie databases.

    This does NOT read cookies from an open browser tab directly — it reads the
    on-disk cookie store for browsers you are logged into on this Mac.
    """
    try:
        import browser_cookie3
    except ImportError:
        return None, "Install browser-cookie3 (pip install browser-cookie3) to use this feature."

    header, message = _import_chrome_cookies(browser_cookie3)
    if header:
        return header, message

    loader_map = {
        "Firefox": browser_cookie3.firefox,
        "Edge": browser_cookie3.edge,
        "Safari": browser_cookie3.safari,
    }

    notes: list[str] = [message]
    for name, loader in loader_map.items():
        try:
            jar = loader(domain_name="cat-dcloud.com")
        except Exception as exc:
            err = str(exc).strip()
            if len(err) > 100:
                err = err[:100] + "…"
            notes.append(f"{name}: blocked ({err})")
            continue

        cookies = _cookies_from_jar(jar)
        header = _cookie_header(cookies)
        if header:
            return header, f"Imported from {name} cookie store ({len(header.split('; '))} cookie(s))"
        notes.append(f"{name}: no cat-dcloud.com cookies")

    hint = (
        "Paste Cookie from DevTools → Network → any board.cat-dcloud.com or kitchen.cat-dcloud.com "
        "request → Request Headers, or save as IDAC_COOKIE / KITCHEN_COOKIE in .env."
    )
    return None, f"{notes[0]} {' '.join(notes[1:2])} {hint}"
