"""Tailscale's Windows /32 describes one node, not the reachable tailnet."""
import socket
from types import SimpleNamespace

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.security import NetworkPolicy, RequestBoundaryMiddleware


def _interfaces(monkeypatch, name="Tailscale", *, up=True, address="100.94.235.37"):
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {name: SimpleNamespace(isup=up)})
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {name: [
        SimpleNamespace(family=socket.AF_INET, address=address, netmask="255.255.255.255"),
    ]})


def test_active_tailscale_host_route_allows_other_tailnet_nodes(monkeypatch):
    _interfaces(monkeypatch)
    policy = NetworkPolicy.from_system()
    assert policy.allows("100.95.173.87")
    assert policy.allows("::ffff:100.95.173.87")
    assert policy.allows("100.64.0.1")
    assert policy.allows("100.127.255.254")
    assert not policy.allows("100.63.255.254")
    assert not policy.allows("100.128.0.1")
    assert not policy.allows("203.0.113.1")


@pytest.mark.parametrize("name,up,address", [
    ("Ethernet", True, "100.94.235.37"),
    ("Tailscale", False, "100.94.235.37"),
    ("Tailscale", True, "192.168.1.2"),
])
def test_cgnat_is_not_unconditionally_trusted(monkeypatch, name, up, address):
    _interfaces(monkeypatch, name, up=up, address=address)
    assert not NetworkPolicy.from_system().allows("100.95.173.87")


def test_tailnet_http_keeps_host_and_same_origin_checks(monkeypatch):
    _interfaces(monkeypatch)
    app = FastAPI()
    app.add_middleware(RequestBoundaryMiddleware)

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    with TestClient(app, base_url="http://100.94.235.37:1919",
                    client=("100.95.173.87", 50000)) as client:
        assert client.get("/").status_code == 200
        assert client.post("/mutate", headers={"Origin": "http://100.94.235.37:1919"}).status_code == 200
        assert client.post("/mutate", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
