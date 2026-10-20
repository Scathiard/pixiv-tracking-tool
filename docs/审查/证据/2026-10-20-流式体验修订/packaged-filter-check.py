"""Check the tag-read-only follow-up package and unchanged tested UI bytes."""
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
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
exe = args.exe.resolve()
os.environ.update(TEMP=str(out), TMP=str(out))
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
origin = f"http://127.0.0.1:{port}"
stop = out / "stop.signal"
result = {"checks": [], "errors": [], "real_account_used": False, "real_library_used": False,
          "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
with (out / "output.txt").open("wb") as output:
    process = subprocess.Popen(
        [str(exe), "--smoke-test", "--smoke-output", str(out / "smoke.json"),
         "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)],
        cwd=out, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        for _ in range(180):
            if process.poll() is not None:
                raise RuntimeError("EXE exited before ready")
            try:
                with urlopen(origin + "/api/status", timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(.5)
        else:
            raise TimeoutError("EXE not ready")
        with urlopen(origin + "/api/library/tags?show_hidden=1", timeout=10) as response:
            tags = json.load(response)
            assert "本地单图" in tags["tags"]
            assert any(tag["translated_name"] == "测试单图" for tag in tags["tag_details"])
        result["checks"].append("packaged-local-tags-and-official-translation")
        with urlopen(origin + "/api/artists?compact=1&limit=100", timeout=10) as response:
            artists = json.load(response)
            assert artists["items"] and "recent_works" not in artists["items"][0]
        result["checks"].append("packaged-compact-artists")
        for asset, digest in {
            "index-C8LCmJsv.js": "c18f3a769d72a0a7efd7d958e0fa11c734b2e9ad1cba1304aefc0f48e7777770",
            "index-DWxsaIwY.css": "ba39ea0d29f3bc722f72c63f6315003a6f0753a3f839ed79bc770c44d2a4766d",
        }.items():
            with urlopen(origin + "/reader/assets/" + asset, timeout=10) as response:
                assert hashlib.sha256(response.read()).hexdigest() == digest
        result["checks"].append("same-served-ui-bytes-as-browser-verified-20261020.4")
    except Exception as error:
        result["errors"].append(str(error))
        raise
    finally:
        stop.write_text("stop", encoding="utf-8")
        process.wait(timeout=25)
        result["exit_code"] = process.returncode
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
assert process.returncode == 0 and not result["errors"], result
assert json.loads((out / "smoke.json").read_text(encoding="utf-8"))["status"] == "SMOKE_OK"
print(json.dumps(result, ensure_ascii=False))
