#!/usr/bin/python3
"""Show how many anonymous installs have checked GitHub for updates.

Maintainer-only. Prints a count and versions, never names.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / "usage.json"
REMOTE = (
    "https://raw.githubusercontent.com/jasmurra/dcloud-content-manager/"
    "usage-data/usage.json"
)


def _load() -> dict:
    if LOCAL.is_file():
        try:
            data = json.loads(LOCAL.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("installs"), dict):
                return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    request = urllib.request.Request(
        REMOTE,
        headers={"User-Agent": "dcloud-content-manager-usage", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TypeError, ValueError):
        return {"installs": {}}
    return data if isinstance(data, dict) else {"installs": {}}


def main() -> int:
    data = _load()
    installs = data.get("installs") if isinstance(data.get("installs"), dict) else {}
    print(f"Unique installs that checked for updates: {len(installs)}")
    if not installs:
        print("No pings stored yet. After people run start.command or Check for updates,")
        print("wait for the collector (every few hours) or run: .venv/bin/python collect_usage.py")
        return 0
    versions = Counter(
        str(row.get("version") or "unknown") for row in installs.values() if isinstance(row, dict)
    )
    print("By version:")
    for version, count in versions.most_common():
        print(f"  {version}: {count}")
    latest = max(
        (str(row.get("seen") or "") for row in installs.values() if isinstance(row, dict)),
        default="",
    )
    if latest:
        print(f"Most recent ping: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
