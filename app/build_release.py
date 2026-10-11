"""Build one new internal artifact, without deleting or overwriting prior builds."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
from src.core.database import SCHEMA_REVISION

APP = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=APP.parent / "release")
    parser.add_argument("--work-root", type=Path, default=APP.parent / ".devtools/test-runs")
    parser.add_argument("--frontend-dist", type=Path, help="Use a checked external frontend dist with --skip-frontend")
    parser.add_argument("--skip-frontend", action="store_true", help="Use an already checked frontend build")
    args = parser.parse_args()
    identity = runpy.run_path(str(APP / "src/version.py"))
    public, build = identity["__version__"], identity["__build__"]
    destination = args.output_root.resolve() / f"v{public}" / f"build-{build}"
    work = args.work_root.resolve() / f"b{build}-package"
    if destination.exists() or work.exists():
        parser.error("This build already has artifacts; choose a new internal build ID. Prior files are preserved.")
    if args.frontend_dist is not None and not args.skip_frontend:
        parser.error("--frontend-dist requires --skip-frontend")
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
        subprocess.run(command, cwd=APP, stdout=output, stderr=subprocess.STDOUT, check=True, env=build_env)
    executable = destination / (identity["EXE_NAME"] + ".exe")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=APP, text=True, encoding="utf-8").strip()
    manifest = {"version": public, "build": build, "exe_file": executable.name,
                "exe_size_bytes": executable.stat().st_size, "exe_sha256": digest,
                "source_commit": git("rev-parse", "HEAD"), "source_branch": git("branch", "--show-current"),
                "source_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
                "frontend_dist": str(frontend_dist), "database_schema": SCHEMA_REVISION, "database_upgrade_required": False,
                "database_compatible_from": SCHEMA_REVISION,
                "older_database_upgrade_required": True,
                "database_upgrade_entry": "startup-confirmed backup / validate / activate",
                "build_environment": {"os": platform.platform(), "python": platform.python_version(),
                                      "pyinstaller": version("pyinstaller")},
                "published": False, "user_acceptance": False, "validation": "pending packaged validation"}
    (destination / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "SHA256SUMS.txt").write_text(f"{digest}  {executable.name}\n", encoding="utf-8")
    print(str(executable))
    print(f"SHA-256 {digest}")


if __name__ == "__main__":
    main()
