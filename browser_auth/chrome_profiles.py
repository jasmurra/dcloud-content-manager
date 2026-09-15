"""Chrome profile path helpers (macOS)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def chrome_last_used_profile() -> str:
    state = Path.home() / "Library/Application Support/Google/Chrome/Local State"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str((data.get("profile") or {}).get("last_used") or "").strip()


def _checkpoint_copy(cookies_path: Path) -> None:
    """Fold Chrome's write-ahead log into the copy.

    browser_cookie3 later copies Cookies alone (no -wal), so a fresh CAMGR
    login is invisible until this merge. Never touch Chrome's original files.
    """
    try:
        conn = sqlite3.connect(str(cookies_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        return
    for suffix in ("-wal", "-shm"):
        sidecar = cookies_path.with_name(cookies_path.name + suffix)
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def chrome_cookie_snapshot(cookie_file: Path) -> Iterator[Path]:
    """Copy a cookie DB with its -wal/-shm/-journal sidecars, then checkpoint the copy."""
    source = Path(cookie_file)
    tmpdir = Path(tempfile.mkdtemp(prefix="chrome-cookies-"))
    try:
        target = tmpdir / "Cookies"
        shutil.copyfile(source, target)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = source.with_name(source.name + suffix)
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                try:
                    shutil.copyfile(sidecar, target.with_name(target.name + suffix))
                except OSError:
                    pass
        _checkpoint_copy(target)
        yield target
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def chrome_cookie_files() -> list[Path]:
    """Cookie DBs for the last-used Chrome profile first, then other recent profiles.

    Prefer Network/Cookies when both that and the legacy Cookies file exist.
    """
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.is_dir():
        return []

    patterns = (
        "Default/Cookies",
        "Default/Network/Cookies",
        "Profile */Cookies",
        "Profile */Network/Cookies",
    )
    by_profile: dict[str, tuple[int, Path]] = {}
    for pattern in patterns:
        for path in base.glob(pattern):
            if not path.is_file():
                continue
            text = str(path)
            if "Snapshots" in text or "Backup" in text:
                continue
            if path.parent.name == "Network":
                profile = path.parent.parent.name
                rank = 0
            else:
                profile = path.parent.name
                rank = 1
            prev = by_profile.get(profile)
            if prev is None or rank < prev[0]:
                by_profile[profile] = (rank, path)

    last = chrome_last_used_profile()

    def sort_key(item: tuple[str, tuple[int, Path]]) -> tuple[int, float]:
        profile, (_rank, path) = item
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (0 if profile == last else 1, -mtime)

    ordered = [pair[1] for _profile, pair in sorted(by_profile.items(), key=sort_key)]
    return ordered[:8]


def chrome_leveldb_dirs() -> list[Path]:
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.is_dir():
        return []

    patterns = (
        "Default/Local Storage/leveldb",
        "Default/Session Storage/leveldb",
        "Profile */Local Storage/leveldb",
        "Profile */Session Storage/leveldb",
    )
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(base.glob(pattern))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(
        (p for p in paths if p.is_dir()),
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
