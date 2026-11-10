"""Build one new internal artifact, without deleting or overwriting prior builds."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import sqlite3
import subprocess
import sys
from src.core.database import SCHEMA_REVISION
from src.core.upgrade import _SUPPORTED_SOURCES
from src.core.wal_maintenance import WalMaintenance

APP = Path(__file__).resolve().parent


def build_sqlite(dll_path: Path | None) -> tuple[str, Path | None]:
    """Validate the SQLite runtime that PyInstaller will ship, before building."""
    selected = None
    if dll_path is None:
        sqlite_version = sqlite3.sqlite_version
    else:
        if sys.platform != "win32":
            raise ValueError("--sqlite-dll is supported only for Windows builds.")
        selected = dll_path.resolve(strict=True)
        if not selected.is_file():
            raise ValueError("--sqlite-dll must point to a SQLite DLL file.")
        library = ctypes.CDLL(str(selected))
        library.sqlite3_libversion.restype = ctypes.c_char_p
        library.sqlite3_libversion.argtypes = []
        sqlite_version = library.sqlite3_libversion().decode("ascii")
    minimum = WalMaintenance.MIN_SQLITE
    if tuple(int(part) for part in sqlite_version.split(".")) < minimum:
        required = ".".join(str(part) for part in minimum)
        raise ValueError(f"SQLite {sqlite_version} is too old; this application requires >= {required}. "
                         "Use a Python runtime with a compatible SQLite library, or on Windows pass "
                         "--sqlite-dll PATH to a compatible SQLite DLL matching your Python architecture.")
    return sqlite_version, selected


def source_identity() -> dict:
    """A source ZIP may have no Git metadata, even inside another repository."""
    unknown = {"source_commit": None, "source_branch": None, "source_dirty": None}
    root = APP.parent.resolve()
    if not (root / ".git").exists():
        return unknown

    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=root, text=True,
                                       encoding="utf-8", stderr=subprocess.DEVNULL).strip()

    try:
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            return unknown
        return {"source_commit": git("rev-parse", "HEAD"),
                "source_branch": git("branch", "--show-current") or None,
                "source_dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, subprocess.CalledProcessError):
        return unknown


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=APP.parent / "release")
    parser.add_argument("--work-root", type=Path, default=APP.parent / ".devtools/test-runs")
    parser.add_argument("--frontend-dist", type=Path, help="Use a checked external frontend dist with --skip-frontend")
    parser.add_argument("--skip-frontend", action="store_true", help="Use an already checked frontend build")
    parser.add_argument("--sqlite-dll", type=Path,
                        help="Optional Windows SQLite DLL (>= 3.51.3, matching the Python architecture)")
    args = parser.parse_args()
    identity = runpy.run_path(str(APP / "src/version.py"))
    public, build = identity["__version__"], identity["__build__"]
    destination = args.output_root.resolve() / f"v{public}" / f"build-{build}"
    work = args.work_root.resolve() / f"b{build}-package"
    if destination.exists() or work.exists():
        parser.error("This build already has artifacts; choose a new internal build ID. Prior files are preserved.")
    if args.frontend_dist is not None and not args.skip_frontend:
        parser.error("--frontend-dist requires --skip-frontend")
    try:
        sqlite_version, sqlite_dll = build_sqlite(args.sqlite_dll)
    except (OSError, ValueError, AttributeError) as error:
        parser.error(f"SQLite runtime check failed: {error}")
    if not args.skip_frontend:
        subprocess.run(["npm.cmd" if sys.platform == "win32" else "npm", "run", "build"], cwd=APP / "frontend", check=True)
    frontend_dist = (args.frontend_dist or APP / "frontend/dist").resolve()
    if not (frontend_dist / "index.html").is_file():
        parser.error("Build the frontend before packaging.")
    destination.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-m", "PyInstaller", "build.spec", "--distpath", str(destination), "--workpath", str(work)]
    with (work / "build.log").open("x", encoding="utf-8") as output:
        build_env = dict(os.environ, PIXIV_FRONTEND_DIST=str(frontend_dist))
        # Only an explicitly validated command-line override may replace SQLite.
        build_env.pop("PIXIV_SQLITE_DLL", None)
        if sqlite_dll is not None:
            build_env["PIXIV_SQLITE_DLL"] = str(sqlite_dll)
        subprocess.run(command, cwd=APP, stdout=output, stderr=subprocess.STDOUT, check=True, env=build_env)
    executable = destination / (identity["EXE_NAME"] + ".exe")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    helpers = {}
    for name in ("start_release.ps1", "stop_release.ps1"):
        target = destination / name
        shutil.copy2(APP / name, target)
        helpers[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"version": public, "build": build, "exe_file": executable.name,
                "exe_size_bytes": executable.stat().st_size, "exe_sha256": digest,
                "helper_sha256": helpers,
                **source_identity(),
                "frontend_dist": str(frontend_dist), "database_schema": SCHEMA_REVISION, "database_upgrade_required": False,
                "database_compatible_from": SCHEMA_REVISION,
                "explicit_upgrade_supported_from": list(_SUPPORTED_SOURCES),
                "older_database_upgrade_required": True,
                "database_upgrade_entry": "startup-confirmed backup / validate / activate",
                "build_environment": {"os": platform.platform(), "python": platform.python_version(),
                                      "pyinstaller": version("pyinstaller"), "sqlite": sqlite_version,
                                      "sqlite_source": str(sqlite_dll) if sqlite_dll is not None else "python"},
                "published": False, "user_acceptance": False, "validation": "pending packaged validation"}
    (destination / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "SHA256SUMS.txt").write_text(f"{digest}  {executable.name}\n" +
        "".join(f"{value}  {name}\n" for name, value in helpers.items()), encoding="utf-8")
    print(str(executable))
    print(f"SHA-256 {digest}")


if __name__ == "__main__":
    main()
