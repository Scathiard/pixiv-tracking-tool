"""Verify this EXE's synthetic tag counts and the exact browser-tested UI."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from urllib.request import urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--dist", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
os.environ.update(TEMP=str(out), TMP=str(out))
exe = args.exe.resolve(strict=True)
dist = args.dist.resolve(strict=True)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
origin = f"http://127.0.0.1:{port}"
stop = out / "stop.signal"
result = {"checks": [], "errors": [], "real_account": False, "real_library": False,
          "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(), "ui_sha256": {}}

def read(path):
    with urlopen(origin + path, timeout=5) as response:
        return response.read()

with (out / "output.txt").open("wb") as output:
    process = subprocess.Popen(
        [str(exe), "--smoke-test", "--smoke-output", str(out / "smoke.json"),
         "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)],
        cwd=out, stdout=output, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"EXE exited before ready: {process.returncode}")
            try:
                read("/api/status")
                break
            except OSError:
                time.sleep(.3)
        else:
            raise TimeoutError("Synthetic EXE did not become ready")
        tags = json.loads(read("/api/library/tags?show_hidden=1"))
        assert tags["tag_counts"]["本地单图"] == 1
        assert set(tags["tag_counts"]) == set(tags["tags"])
        assert any(tag["translated_name"] == "测试单图" for tag in tags["tag_details"])
        result["checks"].append("packaged-local-work-counts-and-official-translation")
        for file in [dist / "index.html", *sorted((dist / "assets").iterdir())]:
            path = "/" if file.name == "index.html" else "/reader/" + file.relative_to(dist).as_posix()
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            assert hashlib.sha256(read(path)).hexdigest() == digest, path
            result["ui_sha256"][path] = digest
        result["checks"].append("exact-browser-verified-compiled-ui-served-by-exe")
    except Exception as error:
        result["errors"].append(str(error))
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            result["errors"].append("Synthetic EXE did not exit within 30 seconds")
        result["exit_code"] = process.returncode
        if (out / "smoke.json").is_file():
            result["smoke"] = json.loads((out / "smoke.json").read_text(encoding="utf-8")).get("status")
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
assert result["exit_code"] == 0 and result.get("smoke") == "SMOKE_OK" and not result["errors"], result
