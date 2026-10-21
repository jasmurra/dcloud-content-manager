"""Expo production API helpers (list expos/sessions, reset, end engagement).

Used by dCloud Content Manager's Expo page. The same personal dCloud bearer
that signs in at the top of the app is sent to Expo; bot OAuth is usually 403.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from browser_auth.dcloud_token import normalize_dcloud_token

DEFAULT_EXPO_API_BASE = "https://expo-production.ciscodcloud.com/api"
TIMEOUT_SECS = 30
MAX_RETRIES = 3


def expo_api_base() -> str:
    return (os.getenv("EXPO_API_BASE") or DEFAULT_EXPO_API_BASE).rstrip("/")


def expo_headers(token: str) -> dict[str, str]:
    clean = normalize_dcloud_token(token)
    if not clean:
        raise ValueError("dCloud token is empty.")
    return {
        "Authorization": f"Bearer {clean}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.ciscodcloud.com",
        "Referer": "https://www.ciscodcloud.com/",
    }


def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = TIMEOUT_SECS,
) -> requests.Response:
    last_exc: Exception | None = None
    headers = expo_headers(token)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(attempt)
                continue
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Expo request failed unexpectedly.")


def _response_detail(response: requests.Response) -> str:
    text = (response.text or "").strip()
    if not text:
        return response.reason or ""
    lowered = text[:80].lower()
    if "<html" in lowered or "<head" in lowered or "authorization required" in lowered:
        return "token expired or rejected"
    if len(text) > 180:
        return text[:180] + "..."
    return text


def _raise_for_status(response: requests.Response, action: str) -> None:
    if response.ok:
        return
    detail = _response_detail(response)
    hint = ""
    if response.status_code == 401:
        hint = " Sign in to dCloud again, then retry."
    elif response.status_code == 403:
        hint = (
            " This login does not have Expo access "
            "(www.ciscodcloud.com/apps/expos)."
        )
    raise RuntimeError(
        f"{action} failed ({response.status_code}): {detail}.{hint}"
    )


def _embedded_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    embedded = payload.get("_embedded") or {}
    items = embedded.get(key, payload.get(key, []))
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"Unexpected Expo response: {key} is not a list.")
    return [item for item in items if isinstance(item, dict)]


def list_expos(token: str) -> list[dict[str, Any]]:
    response = _request("GET", f"{expo_api_base()}/expos", token)
    _raise_for_status(response, "List expos")
    expos = _embedded_list(response.json(), "expos")
    state_rank = {"IN_PROGRESS": 0, "SCHEDULED": 1, "COMPLETE": 2}
    expos.sort(
        key=lambda item: (
            state_rank.get(str(item.get("state") or ""), 9),
            str(item.get("name") or "").lower(),
        )
    )
    return expos


def get_expo(token: str, expo_uid: str) -> dict[str, Any]:
    uid = expo_uid.strip()
    response = _request("GET", f"{expo_api_base()}/expos/{uid}", token)
    _raise_for_status(response, "Get expo")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Expo details response was not JSON object.")
    return payload


def list_demos(token: str, expo_uid: str) -> list[dict[str, Any]]:
    uid = expo_uid.strip()
    response = _request("GET", f"{expo_api_base()}/expos/{uid}/demos", token)
    _raise_for_status(response, "List demos")
    return _embedded_list(response.json(), "demos")


def list_sessions(token: str, expo_uid: str) -> list[dict[str, Any]]:
    uid = expo_uid.strip()
    response = _request("GET", f"{expo_api_base()}/expos/{uid}/sessions", token)
    _raise_for_status(response, "List sessions")
    return _embedded_list(response.json(), "sessions")


def reset_session(token: str, session_uid: str) -> dict[str, Any]:
    uid = session_uid.strip()
    if not uid:
        raise ValueError("Session uid is required to reset.")
    response = _request(
        "POST",
        f"{expo_api_base()}/session-resets",
        token,
        json_body={"session": {"uid": uid}},
    )
    _raise_for_status(response, f"Reset session {uid}")
    payload: dict[str, Any] = {"session": {"uid": uid}}
    if response.content:
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
        except ValueError:
            payload["raw"] = response.text[:200]
    payload["httpStatus"] = response.status_code
    return payload


def end_engagement(token: str, engagement_uid: str) -> dict[str, Any]:
    uid = engagement_uid.strip()
    if not uid:
        raise ValueError("Engagement uid is required to end engagement.")
    response = _request("DELETE", f"{expo_api_base()}/public/engagements/{uid}", token)
    if response.status_code not in (200, 202, 204):
        _raise_for_status(response, f"End engagement {uid}")
    return {"uid": uid, "status": response.status_code}


def whoami(token: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            "https://api.ciscodcloud.com/user/me",
            headers=expo_headers(token),
            timeout=TIMEOUT_SECS,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def expo_roles(me: dict[str, Any] | None) -> list[str]:
    if not isinstance(me, dict):
        return []
    raw = (me.get("rolesByScope") or {}).get("EXPO") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(role).strip() for role in raw if str(role).strip()]


def check_expo_access(token: str) -> dict[str, Any]:
    """True only when this login can list Expo expos (same gate as /apps/expos)."""
    empty = {
        "allowed": False,
        "roles": [],
        "expoCount": 0,
        "user": {"email": "", "fullname": "", "username": ""},
        "error": "",
    }
    if not normalize_dcloud_token(token):
        return empty
    me = whoami(token) or {}
    roles = expo_roles(me)
    allowed = False
    error = ""
    count = 0
    try:
        expos = list_expos(token)
        allowed = True
        count = len(expos)
    except Exception as exc:
        error = str(exc)
        allowed = False
    return {
        "allowed": allowed,
        "roles": roles,
        "expoCount": count,
        "user": {
            "email": str(me.get("email") or ""),
            "fullname": str(me.get("fullname") or ""),
            "username": str(me.get("username") or ""),
        },
        "error": error if not allowed else "",
    }


def summarize_expo(expo: dict[str, Any]) -> dict[str, Any]:
    audit = expo.get("_audit") if isinstance(expo.get("_audit"), dict) else {}
    return {
        "uid": expo.get("uid"),
        "name": expo.get("name"),
        "state": expo.get("state"),
        "type": expo.get("type"),
        "location": expo.get("location"),
        "start": expo.get("start"),
        "end": expo.get("end"),
        "noOfSeats": expo.get("noOfSeats"),
        "description": expo.get("description"),
        "createdBy": audit.get("createdBy") or "",
        "createdOn": audit.get("createdOn") or "",
        "modifiedBy": audit.get("modifiedBy") or "",
    }


def _content_pool_name(demo: dict[str, Any]) -> str:
    pool = demo.get("contentPool")
    if isinstance(pool, dict):
        return str(pool.get("name") or pool.get("id") or "")
    return str(pool or "")


def normalize_session(
    session: dict[str, Any],
    demo_by_uid: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    engagement = session.get("engagement") if isinstance(session.get("engagement"), dict) else {}
    demo_ref = session.get("demo") if isinstance(session.get("demo"), dict) else {}
    demo_uid = str(demo_ref.get("uid") or "")
    demo = demo_by_uid.get(demo_uid, {})
    user = str(engagement.get("user") or "").strip()
    running = engagement.get("runningSeconds")
    try:
        running_seconds = int(running) if running is not None else None
    except (TypeError, ValueError):
        running_seconds = None
    return {
        "uid": session.get("uid"),
        "sessionId": session.get("sessionId"),
        "status": session.get("status") or "",
        "dataCenter": session.get("dataCenter") or "",
        "dCloudDemoId": session.get("dCloudDemoId"),
        "demoUid": demo_uid,
        "demoName": engagement.get("demoName") or demo.get("name") or "",
        "user": user,
        "assigned": bool(user),
        "runningSeconds": running_seconds,
        "engagementUid": engagement.get("uid") or "",
        "engagementStart": engagement.get("start") or "",
        "runningUnassigned": (session.get("status") or "").upper() == "RUNNING" and not user,
    }


def session_stats(rows: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "total": len(rows),
        "running": 0,
        "starting": 0,
        "stopping": 0,
        "error": 0,
        "assigned": 0,
        "unassigned": 0,
        "runningUnassigned": 0,
        "runningAssigned": 0,
        "withEngagement": 0,
    }
    for row in rows:
        status = str(row.get("status") or "").upper()
        assigned = bool(row.get("assigned"))
        if status == "RUNNING":
            stats["running"] += 1
            if assigned:
                stats["runningAssigned"] += 1
            else:
                stats["runningUnassigned"] += 1
        elif status == "STARTING":
            stats["starting"] += 1
        elif status == "STOPPING":
            stats["stopping"] += 1
        elif status == "ERROR":
            stats["error"] += 1
        if assigned:
            stats["assigned"] += 1
        else:
            stats["unassigned"] += 1
        if row.get("engagementUid"):
            stats["withEngagement"] += 1
    return stats


def normalize_demo(demo: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    stats = session_stats(sessions)
    return {
        "uid": demo.get("uid"),
        "dCloudId": demo.get("dCloudId"),
        "dataCenter": demo.get("dataCenter") or "",
        "name": demo.get("name") or "",
        "title": demo.get("description") or "",
        "type": demo.get("type") or "",
        "contentPool": _content_pool_name(demo),
        "target": demo.get("targetSessions") or 0,
        "running": demo.get("runningSessions") or 0,
        "inUse": demo.get("activeEngagements") or stats["assigned"],
        "scheduled": demo.get("scheduledSessions") or 0,
        "starting": demo.get("startingSessions") or 0,
        "stopping": demo.get("stoppingSessions") or 0,
        "stopped": demo.get("stoppedSessions") or 0,
        "error": demo.get("errorSessions") or 0,
        "runningUnassigned": stats["runningUnassigned"],
        "sessions": sessions,
        "stats": stats,
    }


def get_expo_safe(token: str, expo_uid: str) -> dict[str, Any] | None:
    try:
        return get_expo(token, expo_uid)
    except Exception:
        return None


def load_expo_sessions(token: str, expo_uid: str) -> dict[str, Any]:
    expo = get_expo_safe(token, expo_uid)
    demos = list_demos(token, expo_uid)
    demo_by_uid = {str(demo.get("uid") or ""): demo for demo in demos if demo.get("uid")}
    sessions = list_sessions(token, expo_uid)
    rows = [normalize_session(session, demo_by_uid) for session in sessions]
    rows.sort(
        key=lambda row: (
            0 if row.get("runningUnassigned") else 1,
            str(row.get("status") or ""),
            str(row.get("user") or ""),
            row.get("sessionId") or 0,
        )
    )
    by_demo: dict[str, list[dict[str, Any]]] = {str(demo.get("uid") or ""): [] for demo in demos}
    unmatched: list[dict[str, Any]] = []
    for row in rows:
        demo_uid = str(row.get("demoUid") or "")
        if demo_uid in by_demo:
            by_demo[demo_uid].append(row)
        else:
            unmatched.append(row)
    demo_rows = [normalize_demo(demo, by_demo.get(str(demo.get("uid") or ""), [])) for demo in demos]
    if unmatched:
        demo_rows.append(
            normalize_demo(
                {
                    "uid": "unmatched",
                    "name": "Other sessions",
                    "dataCenter": "",
                    "dCloudId": "",
                },
                unmatched,
            )
        )
    return {
        "expo": summarize_expo(expo) if expo else {"uid": expo_uid},
        "demos": demo_rows,
        "sessions": rows,
        "stats": session_stats(rows),
    }
