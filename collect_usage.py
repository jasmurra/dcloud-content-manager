#!/usr/bin/python3
"""Merge anonymous update-check pings into usage.json. Maintainer / GitHub Actions only."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
USAGE_FILE = ROOT / "usage.json"
CONFIG = ROOT / "github-update.txt"
HASH_RE = re.compile(r"^[0-9a-f]{16}$")


def _load_config() -> dict[str, str]:
    values = {"usage_topic": ""}
    if not CONFIG.is_file():
        return values
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def _empty() -> dict:
    return {"v": 1, "installs": {}}


def load_usage(path: Path = USAGE_FILE) -> dict:
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    installs = data.get("installs")
    if not isinstance(installs, dict):
        installs = {}
    return {"v": 1, "installs": installs}


def merge_ping(data: dict, ping: dict) -> None:
    ident = str(ping.get("id") or "").strip().lower()
    if not HASH_RE.match(ident):
        return
    version = str(ping.get("version") or "").strip()
    seen = str(ping.get("seen") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    row = data.setdefault("installs", {}).get(ident) or {}
    if not isinstance(row, dict):
        row = {}
    if version:
        row["version"] = version
    prev = str(row.get("seen") or "")
    if seen >= prev:
        row["seen"] = seen
    data["installs"][ident] = row


def parse_ntfy_message(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        parts = text.split()
        if parts and HASH_RE.match(parts[0].lower()):
            return {"id": parts[0].lower(), "version": parts[1] if len(parts) > 1 else ""}
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def fetch_ntfy_pings(topic: str, *, since: str = "48h") -> list[dict]:
    if not topic:
        return []
    url = (
        f"https://ntfy.sh/{urllib.parse.quote(topic, safe='')}/json"
        f"?poll=1&since={urllib.parse.quote(since, safe='')}"
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/x-ndjson", "User-Agent": "dcloud-content-manager-usage"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return []
    out: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") not in {None, "message"}:
            continue
        ping = parse_ntfy_message(str(event.get("message") or ""))
        if event.get("time"):
            try:
                ping["seen"] = datetime.fromtimestamp(int(event["time"]), tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except (OSError, TypeError, ValueError):
                pass
        out.append(ping)
    return out


def collect(*, topic: str = "", existing: Path = USAGE_FILE, since: str = "48h") -> dict:
    data = load_usage(existing)
    topic = topic or _load_config().get("usage_topic") or ""
    for ping in fetch_ntfy_pings(topic, since=since):
        merge_ping(data, ping)
    return data


def main() -> int:
    data = collect()
    USAGE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(data.get('installs') or {})} unique install(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
