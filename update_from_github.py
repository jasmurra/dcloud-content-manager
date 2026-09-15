#!/usr/bin/env python3
"""Check the private GitHub repo and copy a newer VERSION onto this install.

Works for a git clone and for the zip installs (including the full Mac zip
with bundled Python). Local jobs, logins, .venv, and runtime/ are left alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "github-update.txt"
CACHE = ROOT / ".github-update-cache"
VERSION_FILE = ROOT / "VERSION"

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "runtime",
    ".github-update-cache",
    ".python-runtime-cache",
    ".dcloud-camgr-chrome",
    "__pycache__",
}
SKIP_FILE_NAMES = {
    ".env",
    "last-job.json",
    "last-saved-ids.json",
    "managed-saved-ids.json",
    ".dcloud-session.json",
    ".dcloud-cai-session.json",
    ".dcloud-camgr-session.json",
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
    if not git_available():
        log("Git is not installed, so this copy cannot check GitHub for updates.")
        log("Install the Xcode Command Line Tools, then start again.")
        return 0

    config = load_config()
    url = repo_https_url(config.get("repo") or "") or to_https_github(origin_url())
    branch = current_branch(config.get("branch") or "main")
    if not url:
        log("GitHub updates are not configured yet (set repo=owner/name in github-update.txt).")
        return 0

    local = read_version(VERSION_FILE) or "0.0"
    log(f"Checking GitHub for a newer version than {local}…")

    if (ROOT / ".git").exists() and origin_url():
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
            return 0
        log(f"Updating {local} → {remote_version}…")
        if pull_existing_clone(branch):
            log(f"Updated to {read_version(VERSION_FILE) or remote_version}.")
        return 0

    source = fetch_cache(url, branch)
    if source is None:
        log("Could not reach GitHub. Starting the installed copy instead.")
        log("If the repo is still private, set it to Public so installs can update without a GitHub login.")
        return 0
    remote_version = read_version(source / "VERSION")
    if not is_newer(remote_version, local):
        log(f"Already on {local}.")
        return 0
    log(f"Updating {local} → {remote_version}…")
    copied = copy_tree(source)
    log(f"Updated {copied} file(s) to {read_version(VERSION_FILE) or remote_version}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — launcher must still start the app
        log(f"Update check failed ({exc}). Starting the installed copy instead.")
        sys.exit(0)
