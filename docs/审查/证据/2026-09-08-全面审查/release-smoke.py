"""Smoke-test an unchanged EXE copy with isolated configuration and data."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

evidence = Path(__file__).resolve().parent
repo = evidence.parents[3]
source = repo / "release/v0.1.1/PixivTracker_v0.1.1.exe"
fixture = Path(tempfile.mkdtemp(prefix="pixiv-review-release-"))
exe = fixture / source.name
shutil.copy2(source, exe)
environment = dict(os.environ, DATA_ROOT=str(fixture / "runtime"), PIXIV_REFRESH_TOKEN="", PYTHONIOENCODING="utf-8")
environment.pop("IMAGES_DIR", None)
environment.pop("DATA_DIR", None)
result = subprocess.run([str(exe), "--smoke-test"], cwd=fixture, env=environment,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
                        creationflags=subprocess.CREATE_NO_WINDOW)
database = fixture / "runtime/data/pixiv_tracker.db"
tables = []
if database.exists():
    with sqlite3.connect(database) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
output = {"source_exe":str(source), "fixture":str(fixture), "exit_code":result.returncode,
          "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
          "copy_sha256":hashlib.sha256(exe.read_bytes()).hexdigest(),
          "synthetic_database_tables":tables,
          "stdout":result.stdout.decode("gb18030", errors="replace"),
          "stderr":result.stderr.decode("gb18030", errors="replace"),
          "scope":"Packaged smoke entry only; no tray, real login, downloads or GUI acceptance."}
(evidence / "release-smoke-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(output, ensure_ascii=False, indent=2))
raise SystemExit(0 if result.returncode == 0 and "illustration" in tables else 1)
