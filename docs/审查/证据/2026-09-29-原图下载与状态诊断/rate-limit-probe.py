"""Isolated PixivClient request-path probe; never imports project configuration."""

import ast
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
SOURCE = next(parent / "app" / "src" / "client.py" for parent in HERE.parents if (parent / "app" / "src" / "client.py").is_file())
DECLARATIONS = {"PixivRequestError", "_checked_response", "_token_expired", "PixivClient"}


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


def load_client(clock):
    source_bytes = SOURCE.read_bytes()
    source = source_bytes.decode("utf-8")
    parsed = ast.parse(source, filename=str(SOURCE))
    nodes = [node for node in parsed.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in DECLARATIONS]
    assert {node.name for node in nodes} == DECLARATIONS
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {
        "threading": threading,
        "time": SimpleNamespace(time=clock.now, sleep=clock.sleep, monotonic=clock.now),
        "_MIN_INTERVAL": 1.0,
        "_get_auth_header": lambda: {},
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace, hashlib.sha256(source_bytes).hexdigest()


def probe(status, body, response_count):
    clock = Clock()
    namespace, source_hash = load_client(clock)
    client_type = namespace["PixivClient"]
    client_type._last_request = 0.0
    client = client_type()
    responses = iter(Response(status, body) for _ in range(response_count))
    calls = []

    def fake_get(endpoint, headers, params):
        calls.append(clock.now())
        return next(responses)

    client._do_get = fake_get
    try:
        client._request("/probe", None, retry_token=False)
        error_status = None
    except namespace["PixivRequestError"] as exc:
        error_status = exc.status

    result = {
        "response_status": status,
        "get_calls": len(calls),
        "get_call_times": calls[:],
        "fake_sleeps": clock.sleeps[:],
        "error_status": error_status,
        "class_last_request": client_type._last_request,
        "instance_last_request": client.__dict__.get("_last_request"),
    }
    return result, client, clock, calls, source_hash


def main():
    limited, client, clock, calls, source_hash = probe(
        200, {"error": "Rate Limit"}, 4)
    assert limited["get_calls"] == 4
    assert limited["fake_sleeps"] == [60, 120, 180]
    assert limited["error_status"] == 200
    assert limited["class_last_request"] == 1000.0
    assert limited["instance_last_request"] == 1360.0

    # A success immediately after the final rate retry exposes the class/instance mismatch.
    client._do_get = lambda endpoint, headers, params: calls.append(clock.now()) or Response(200, {"ok": True})
    prior_sleeps = len(clock.sleeps)
    follow_up = client._request("/probe", None, retry_token=False)
    after_rate = {
        "ok": follow_up == {"ok": True},
        "get_call_time": calls[-1],
        "extra_fake_sleeps": clock.sleeps[prior_sleeps:],
    }
    assert after_rate == {"ok": True, "get_call_time": 1360.0, "extra_fake_sleeps": []}

    http_429, _, _, _, _ = probe(429, {"error": "Rate Limit"}, 1)
    http_403, _, _, _, _ = probe(403, {"error": "Forbidden"}, 1)
    assert http_429["get_calls"] == 1 and http_429["fake_sleeps"] == [] and http_429["error_status"] == 429
    assert http_403["get_calls"] == 1 and http_403["fake_sleeps"] == [] and http_403["error_status"] == 403

    result = {
        "source": "app/src/client.py",
        "source_sha256": source_hash,
        "network_calls": 0,
        "configuration_imports": 0,
        "http_429": http_429,
        "http_200_rate_body": limited,
        "immediate_follow_up_after_rate_body": after_rate,
        "http_403": http_403,
        "assertions_passed": True,
    }
    (HERE / "rate-limit-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"assertions_passed": True, "result": str(HERE / "rate-limit-result.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

