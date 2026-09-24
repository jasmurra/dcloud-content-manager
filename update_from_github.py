#!/usr/bin/env python3
"""Check the private GitHub repo and copy a newer VERSION onto this install.

Works for a git clone and for the zip installs (including the full Mac zip
with bundled Python). Local jobs, logins, .venv, and runtime/ are left alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "github-update.txt"
CACHE = ROOT / ".github-update-cache"
VERSION_FILE = ROOT / "VERSION"
INSTALL_FILE = ROOT / ".dcloud-install.json"
USAGE_PING_SECONDS = 12 * 60 * 60

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "runtime",
    ".github-update-cache",
    ".python-runtime-cache",
    ".dcloud-camgr-chrome",
    ".dcloud-tool-chrome",
    ".playwright-browsers",
    ".cursor",
    ".github",
    "__pycache__",
    # Maintainer regression checks — they stay on GitHub, not in installs.
    "tests",
    "node_modules",
}
SKIP_FILE_NAMES = {
    ".env",
    "last-job.json",
    "last-saved-ids.json",
    "managed-saved-ids.json",
    ".dcloud-session.json",
    ".dcloud-cai-session.json",
    ".dcloud-camgr-session.json",
    ".dcloud-install.json",
    "usage.json",
    "collect_usage.py",
    "show_usage.py",
    # Maintainer zip-builders — stay on GitHub / the author's Mac only.
    "pack_for_mac.py",
    "share-for-mac.command",
    "share-for-mac-with-python.command",
}

GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/usr/bin/true",
    "GCM_INTERACTIVE": "never",
}


def log(message: str) -> None:
    print(message, flush=True)


def run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or ROOT),
        env=GIT_ENV,
        text=True,
        capture_output=True,
        check=False,
    )


def git_available() -> bool:
    try:
        result = run_git("--version")
    except FileNotFoundError:
        return False
    return result.returncode == 0


def read_version(path: Path) -> str:
    try:
        line = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return ""
    return (line[0].strip() if line else "") or ""


def version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(raw or "0").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def is_newer(remote: str, local: str) -> bool:
    if not remote:
        return False
    if not local:
        return True
    return version_tuple(remote) > version_tuple(local)


def load_config() -> dict[str, str]:
    values = {"repo": "", "branch": "main"}
    if not CONFIG.is_file():
        return values
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def anonymous_install_hash() -> str:
    """Random install id, hashed. Never includes a name, email, or hostname."""
    data: dict[str, object] = {}
    if INSTALL_FILE.is_file():
        try:
            loaded = json.loads(INSTALL_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
    raw = str(data.get("id") or "").strip()
    if len(raw) < 16:
        raw = uuid.uuid4().hex
        data["id"] = raw
        try:
            INSTALL_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return hashlib.sha256(f"dcloud-content-manager:{raw}".encode("utf-8")).hexdigest()[:16]


def record_anonymous_usage(
    *,
    version: str = "",
    config: dict[str, str] | None = None,
    force: bool = False,
    now: float | None = None,
    post=None,
) -> bool:
    """Count distinct installs that check GitHub. No names — hashed id + version only."""
    cfg = config if config is not None else load_config()
    topic = str(cfg.get("usage_topic") or "").strip()
    if not topic or "/" in topic or " " in topic:
        return False
    stamp = time.time() if now is None else float(now)
    data: dict[str, object] = {}
    if INSTALL_FILE.is_file():
        try:
            loaded = json.loads(INSTALL_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
    last = float(data.get("last_ping") or 0)
    if not force and last and stamp - last < USAGE_PING_SECONDS:
        return False
    payload = {
        "id": anonymous_install_hash(),
        "version": str(version or read_version(VERSION_FILE) or "").strip(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    url = f"https://ntfy.sh/{urllib.parse.quote(topic, safe='')}"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Title": "dcloud-cm",
            "User-Agent": "dcloud-content-manager-usage",
        },
    )
    try:
        if post:
            post(request)
        else:
            with urllib.request.urlopen(request, timeout=8):
                pass
    except (OSError, urllib.error.URLError):
        return False
    try:
        loaded = json.loads(INSTALL_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update(loaded)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    data["last_ping"] = stamp
    try:
        INSTALL_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return True


def origin_url() -> str:
    if not (ROOT / ".git").exists():
        return ""
    result = run_git("remote", "get-url", "origin")
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def to_https_github(url: str) -> str:
    """Coworker updates use HTTPS so nobody needs an SSH key."""
    text = str(url or "").strip()
    if text.startswith("git@github.com:"):
        path = text.split(":", 1)[1]
        if not path.endswith(".git"):
            path += ".git"
        return f"https://github.com/{path}"
    if text.startswith("ssh://git@github.com/"):
        path = text.split("github.com/", 1)[1]
        if not path.endswith(".git"):
            path += ".git"
        return f"https://github.com/{path}"
    return text


def repo_https_url(repo: str) -> str:
    text = str(repo or "").strip()
    if not text:
        return ""
    if "github.com" in text or text.startswith("git@"):
        return to_https_github(text)
    return f"https://github.com/{text}.git"


def repo_slug(repo: str) -> str:
    """Return owner/name from a configured GitHub slug or URL."""
    text = to_https_github(str(repo or "").strip())
    if "github.com/" in text:
        text = text.split("github.com/", 1)[1]
    text = text.removesuffix(".git").strip("/")
    parts = [part for part in text.split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _github_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Cache-Control": "no-cache",
            "User-Agent": "dcloud-content-manager-updater",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
            return body if isinstance(body, dict) else {}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    if last_exc:
        raise last_exc
    return {}


def _fetch_version_via_raw(slug: str, branch: str) -> str:
    """Fallback when api.github.com is blocked or briefly down."""
    url = (
        f"https://raw.githubusercontent.com/{slug}/"
        f"{urllib.parse.quote(branch, safe='')}/VERSION?_={time.time_ns()}"
    )
    request = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "User-Agent": "dcloud-content-manager-updater"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return ""
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line


def fetch_public_release(repo: str, branch: str) -> tuple[str, str]:
    """Return (VERSION, immutable commit SHA) from GitHub's public API."""
    slug = repo_slug(repo)
    if not slug:
        return "", ""
    try:
        branch_name = urllib.parse.quote(branch, safe="")
        branch_data = _github_json(
            f"https://api.github.com/repos/{slug}/branches/{branch_name}?_={time.time_ns()}"
        )
        commit = branch_data.get("commit")
        sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
        if not sha:
            return _fetch_version_via_raw(slug, branch), ""
        version_data = _github_json(
            f"https://api.github.com/repos/{slug}/contents/VERSION"
            f"?ref={urllib.parse.quote(sha, safe='')}&_={time.time_ns()}"
        )
        encoded = str(version_data.get("content") or "").replace("\n", "")
        version = base64.b64decode(encoded).decode("utf-8", errors="replace").strip().splitlines()[0].strip()
        return version, sha
    except (OSError, ValueError, urllib.error.URLError, IndexError):
        return _fetch_version_via_raw(slug, branch), ""


def fetch_public_version(repo: str, branch: str) -> str:
    return fetch_public_release(repo, branch)[0]


def download_public_source(repo: str, branch: str, dest: Path, *, ref: str = "") -> Path | None:
    """Download a public repo archive without Git, GitHub login, or SSH keys."""
    slug = repo_slug(repo)
    if not slug:
        return None
    wanted = ref or branch
    url = f"https://api.github.com/repos/{slug}/zipball/{urllib.parse.quote(wanted, safe='')}?_={time.time_ns()}"
    archive = dest / "source.zip"
    try:
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(dest / "source")
    except (OSError, urllib.error.URLError, zipfile.BadZipFile):
        return None
    roots = [path for path in (dest / "source").iterdir() if path.is_dir()]
    return roots[0] if len(roots) == 1 else None


def current_branch(config_branch: str) -> str:
    if (ROOT / ".git").exists():
        result = run_git("rev-parse", "--abbrev-ref", "HEAD")
        name = (result.stdout or "").strip()
        if result.returncode == 0 and name and name != "HEAD":
            return name
    return config_branch or "main"


def working_tree_dirty() -> bool:
    if not (ROOT / ".git").exists():
        return False
    result = run_git("status", "--porcelain")
    if result.returncode != 0:
        return True
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if path.startswith(".github-update-cache/") or path in SKIP_FILE_NAMES:
            continue
        return True
    return False


def fetch_cache(url: str, branch: str) -> Path | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    git_dir = CACHE / ".git"
    if git_dir.exists():
        run_git("remote", "set-url", "origin", url, cwd=CACHE)
        fetched = run_git("fetch", "--depth", "1", "origin", branch, cwd=CACHE)
        if fetched.returncode != 0:
            log((fetched.stderr or fetched.stdout or "Could not fetch GitHub updates.").strip())
            return None
        checked = run_git("checkout", "-f", f"origin/{branch}", cwd=CACHE)
        if checked.returncode != 0:
            checked = run_git("checkout", "-f", "FETCH_HEAD", cwd=CACHE)
        if checked.returncode != 0:
            log((checked.stderr or "Could not check out the latest GitHub files.").strip())
            return None
        return CACHE
    cloned = run_git(
        "clone",
        "--depth",
        "1",
        "--branch",
        branch,
        url,
        str(CACHE),
    )
    if cloned.returncode != 0:
        log((cloned.stderr or cloned.stdout or "Could not download the GitHub repo.").strip())
        return None
    return CACHE


def should_skip_copy(rel: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in rel.parts):
        return True
    if rel.name in SKIP_FILE_NAMES:
        return True
    if rel.suffix in {".pyc", ".pyo"} or rel.name == ".DS_Store":
        return True
    return False


def copy_tree(src: Path) -> int:
    copied = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if should_skip_copy(rel):
            continue
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        copied += 1
    start = ROOT / "start.command"
    if start.is_file():
        start.chmod(start.stat().st_mode | 0o111)
    return copied


def pull_existing_clone(branch: str) -> bool:
    fetched = run_git("fetch", "origin", branch)
    if fetched.returncode != 0:
        log((fetched.stderr or "Could not fetch GitHub updates.").strip())
        return False
    pulled = run_git("merge", "--ff-only", f"origin/{branch}")
    if pulled.returncode != 0:
        log((pulled.stderr or "GitHub update was not a fast-forward; leaving this copy as-is.").strip())
        return False
    return True


def main() -> int:
    if os.environ.get("DCLOUD_SKIP_UPDATE") == "1":
        return 0

    config = load_config()
    configured_repo = config.get("repo") or ""
    url = repo_https_url(configured_repo) or to_https_github(origin_url())
    branch = current_branch(config.get("branch") or "main")
    if not url:
        log("GitHub updates are not configured yet (set repo=owner/name in github-update.txt).")
        return 0

    local = read_version(VERSION_FILE) or "0.0"
    log(f"Checking GitHub for a newer version than {local}…")

    if (ROOT / ".git").exists() and origin_url():
        if not git_available():
            log("Git is not available. Starting the installed copy instead.")
            return 0
        if working_tree_dirty():
            log("Local file changes are present — skipping the GitHub update so nothing is overwritten.")
            return 0
        fetched = run_git("fetch", "origin", branch)
        if fetched.returncode != 0:
            log("Could not reach GitHub. Starting the installed copy instead.")
            log("If the repo is still private, set it to Public so installs can update without a GitHub login.")
            detail = (fetched.stderr or "").strip()
            if detail:
                log(detail.splitlines()[-1])
            return 0
        remote_version = ""
        shown = run_git("show", f"origin/{branch}:VERSION")
        if shown.returncode == 0:
            remote_version = (shown.stdout or "").strip().splitlines()[0].strip()
        if not is_newer(remote_version, local):
            log(f"Already on {local}.")
            record_anonymous_usage(version=local, config=config)
            return 0
        log(f"Updating {local} → {remote_version}…")
        if pull_existing_clone(branch):
            log(f"Updated to {read_version(VERSION_FILE) or remote_version}.")
        record_anonymous_usage(version=read_version(VERSION_FILE) or remote_version, config=config)
        return 0

    # Normal coworker install: query and download the public GitHub archive
    # directly. This works in both zips, including the bundled-Python version,
    # without Git, an account, an SSH key, or a personal access token.
    remote_version, remote_ref = fetch_public_release(configured_repo or url, branch)
    if not remote_version:
        log("Could not reach GitHub. Starting the installed copy instead.")
        return 0
    if not is_newer(remote_version, local):
        log(f"Already on {local}.")
        record_anonymous_usage(version=local, config=config)
        return 0
    log(f"Updating {local} → {remote_version}…")
    with tempfile.TemporaryDirectory(prefix="dcloud-content-update-") as tmp:
        source = download_public_source(
            configured_repo or url,
            branch,
            Path(tmp),
            ref=remote_ref,
        )
        if source is None:
            log("Could not download the GitHub update. Starting the installed copy instead.")
            return 0
        copied = copy_tree(source)
    installed = read_version(VERSION_FILE) or remote_version
    log(f"Updated {copied} file(s) to {installed}.")
    record_anonymous_usage(version=installed, config=config)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — launcher must still start the app
        log(f"Update check failed ({exc}). Starting the installed copy instead.")
        sys.exit(0)
