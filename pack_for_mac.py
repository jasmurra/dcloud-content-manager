#!/usr/bin/env python3
"""Build coworker-safe Mac zips on the Desktop. Excludes secrets, venv, git, and job state."""

from __future__ import annotations

import os
import stat
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESKTOP = Path.home() / "Desktop"
ZIP_NAME = "dCloud-Content-Manager-Mac.zip"
ZIP_NAME_WITH_PYTHON = "dCloud-Content-Manager-Mac-with-Python.zip"
FOLDER = "dCloud Content Manager"
VERSION_FILE = ROOT / "VERSION"
CACHE_DIR = ROOT / ".python-runtime-cache"

# Portable CPython for Apple Silicon and Intel. Downloaded once onto this machine
# when packing the larger zip — coworkers do not download Python themselves.
PBS_TAG = "20260610"
PBS_PY = "3.12.13"
STANDALONE = {
    "darwin-arm64": (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_TAG}/cpython-{PBS_PY}+{PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
    ),
    "darwin-x86_64": (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_TAG}/cpython-{PBS_PY}+{PBS_TAG}-x86_64-apple-darwin-install_only_stripped.tar.gz"
    ),
}

FILES = (
    "START HERE.txt",
    "start.command",
    "requirements.txt",
    ".env.example",
    "VERSION",
    "github-update.txt",
    "update_from_github.py",
    "app.py",
    "dcloud_client.py",
    "cai_client.py",
    "camgr_client.py",
    "camgr_tab.py",
    "camgr_browser.py",
    "net_errors.py",
    "static/index.html",
)
AUTH_DIR = ROOT / "browser_auth"
PARENT_AUTH = ROOT.parent / "browser_auth"

# Never pack these — they can hold tokens, cookies, or Webex secrets.
BLOCKED_NAMES = {
    ".env",
    ".dcloud-session.json",
    ".dcloud-cai-session.json",
    ".dcloud-camgr-session.json",
    "managed-saved-ids.json",
    "last-job.json",
    "last-saved-ids.json",
}


def add_file(zf: zipfile.ZipFile, src: Path, rel: str) -> None:
    if src.name in BLOCKED_NAMES:
        raise RuntimeError(f"Refusing to pack secret/state file: {src}")
    info = zipfile.ZipInfo(f"{FOLDER}/{rel}")
    info.date_time = time.localtime(src.stat().st_mtime)[:6]
    mode = src.stat().st_mode
    if src.is_file() and (mode & stat.S_IXUSR):
        info.external_attr = (stat.S_IFREG | 0o755) << 16
    else:
        info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, src.read_bytes())


def add_tree(zf: zipfile.ZipFile, src_dir: Path, dest_prefix: str) -> int:
    count = 0
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src_dir).as_posix()
        add_file(zf, path, f"{dest_prefix}/{rel}")
        count += 1
    return count


def add_auth(zf: zipfile.ZipFile) -> int:
    src_dir = AUTH_DIR if AUTH_DIR.is_dir() else PARENT_AUTH
    if not src_dir.is_dir():
        raise SystemExit(f"Missing browser_auth at {src_dir}")
    return add_tree(zf, src_dir, "browser_auth")


def check_local_imports() -> None:
    """Fail before packing if a packed module imports a module we do not pack.

    A missing module here only shows up as ModuleNotFoundError on the
    coworker's Mac, so catch it while building the zip instead.
    """
    import ast

    project_modules = {path.stem for path in ROOT.glob("*.py")}
    packed = {name for name in FILES if name.endswith(".py")}
    packed_modules = {Path(name).stem for name in packed}
    auth_dir = AUTH_DIR if AUTH_DIR.is_dir() else PARENT_AUTH
    available = packed_modules | {"browser_auth"} if auth_dir.is_dir() else packed_modules

    missing: dict[str, set[str]] = {}
    for name in sorted(packed):
        source = (ROOT / name).read_text(encoding="utf-8")
        needed: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                needed.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                needed.add(node.module.split(".")[0])
        gap = (needed & (project_modules | {"browser_auth"})) - available
        if gap:
            missing[name] = gap
    if missing:
        detail = "; ".join(f"{name} needs {', '.join(sorted(mods))}" for name, mods in missing.items())
        raise SystemExit(
            "Refusing to pack an incomplete zip — add these to FILES in pack_for_mac.py: " + detail
        )


def read_version() -> str:
    if not VERSION_FILE.is_file():
        return "0.0"
    line = VERSION_FILE.read_text(encoding="utf-8").strip().splitlines()
    return (line[0].strip() if line else "") or "0.0"


def bump_version(current: str) -> str:
    parts = current.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts.append("1")
    return ".".join(parts)


def write_version(version: str) -> None:
    VERSION_FILE.write_text(version.strip() + "\n", encoding="utf-8")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        print(f"Using cached {dest.name}")
        return
    tmp = dest.with_suffix(dest.suffix + ".partial")
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if total:
                pct = int(read * 100 / total)
                print(f"  {pct}% ({read // (1024 * 1024)} MB)", end="\r", flush=True)
        print()
    tmp.replace(dest)


def _extract_standalone(tarball: Path, dest_dir: Path) -> Path:
    if (dest_dir / "bin" / "python3").is_file():
        return dest_dir
    if dest_dir.exists():
        raise SystemExit(f"Incomplete runtime cache at {dest_dir} — delete it and pack again.")
    scratch = dest_dir.parent / (dest_dir.name + "-extract")
    if scratch.exists():
        import shutil

        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    print(f"Extracting {tarball.name}")
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(scratch, filter="data")
    python_root = None
    for candidate in [scratch / "python", *scratch.glob("*/bin/python3")]:
        if candidate.name == "python3" and candidate.is_file():
            python_root = candidate.parent.parent
            break
        if candidate.is_dir() and (candidate / "bin" / "python3").is_file():
            python_root = candidate
            break
    if python_root is None:
        raise SystemExit(f"Could not find python in {tarball.name}")
    python_root.rename(dest_dir)
    import shutil

    shutil.rmtree(scratch, ignore_errors=True)
    return dest_dir


def ensure_runtimes() -> dict[str, Path]:
    runtimes: dict[str, Path] = {}
    for arch, url in STANDALONE.items():
        tarball = CACHE_DIR / Path(url).name
        extracted = CACHE_DIR / arch
        _download(url, tarball)
        runtimes[arch] = _extract_standalone(tarball, extracted)
        python_bin = runtimes[arch] / "bin" / "python3"
        if not python_bin.is_file():
            raise SystemExit(f"Missing {python_bin}")
        print(f"Ready {arch}: {python_bin}")
    return runtimes


def pack_zip(dest: Path, runtimes: dict[str, Path] | None) -> tuple[int, int]:
    if dest.exists():
        dest.unlink()
    runtime_files = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for name in FILES:
            add_file(zf, ROOT / name, name)
        auth_count = add_auth(zf)
        if runtimes:
            for arch, src_dir in runtimes.items():
                runtime_files += add_tree(zf, src_dir, f"runtime/{arch}")
        names = zf.namelist()
        leaked = [n for n in names if Path(n).name in BLOCKED_NAMES]
        if leaked:
            raise RuntimeError("Refusing to leave a zip that contains: " + ", ".join(leaked))
        file_count = len(names)
    return file_count, auth_count + runtime_files


def main() -> None:
    bump = "--bump" in sys.argv
    with_python = "--with-python" in sys.argv
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("Missing files: " + ", ".join(missing))
    check_local_imports()

    version = read_version()
    if bump:
        version = bump_version(version)
        write_version(version)

    DESKTOP.mkdir(parents=True, exist_ok=True)
    dest = DESKTOP / (ZIP_NAME_WITH_PYTHON if with_python else ZIP_NAME)
    runtimes = ensure_runtimes() if with_python else None
    try:
        file_count, extra = pack_zip(dest, runtimes)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    print(f"Version {version}")
    print(f"Packed {file_count} files → {dest}")
    if with_python:
        print("This zip includes Python for Apple Silicon and Intel Macs.")
        print("Your coworker does not install Python. First run still creates .venv in the unzipped folder.")
    else:
        print(f"App files plus browser_auth ({extra} files). No Python bundled.")
    print("Send that zip. Do not zip this project folder yourself — that would include your login files.")
    os.system(f'open -R "{dest}"')


if __name__ == "__main__":
    main()
