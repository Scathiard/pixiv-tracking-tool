from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import progress
from src.security import NetworkPolicy, RequestBoundaryMiddleware


def _build_app():
    app = FastAPI()
    policy = NetworkPolicy.from_interfaces([("192.168.50.10", "255.255.255.0")])
    app.add_middleware(RequestBoundaryMiddleware, policy=policy)

    @app.get("/")
    async def index(request: Request):
        return {"csrf": request.state.csrf_token}

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    return app


def test_same_lan_device_gets_page_and_transparent_csrf_cookie():
    with TestClient(
        _build_app(),
        base_url="http://192.168.50.10:1919",
        client=("192.168.50.77", 50000),
    ) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json()["csrf"]
    assert response.cookies.get("pixiv_csrf") == response.json()["csrf"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_windows_ipv6_client_is_allowed_when_interface_netmask_is_missing():
    app = FastAPI()
    policy = NetworkPolicy.from_interfaces(
        [("2408:820c:c31b:fe80:437c:c16f:fae9:496b", None)]
    )
    app.add_middleware(RequestBoundaryMiddleware, policy=policy)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(
        app,
        base_url="http://testserver",
        client=("2408:820c:c31b:fe80::77", 50000),
    ) as client:
        response = client.get(
            "/", headers={"Host": "[2408:820c:c31b:fe80::52]:1919"}
        )

    assert response.status_code == 200


def test_external_network_and_dns_rebinding_host_are_rejected():
    with TestClient(
        _build_app(),
        base_url="http://192.168.50.10:1919",
        client=("203.0.113.8", 50000),
    ) as external:
        assert external.get("/").status_code == 403

    with TestClient(
        _build_app(),
        base_url="http://evil.example:1919",
        client=("192.168.50.77", 50000),
    ) as rebound:
        assert rebound.get("/").status_code == 400


def test_same_origin_post_is_seamless_but_cross_site_post_is_rejected():
    with TestClient(
        _build_app(),
        base_url="http://192.168.50.10:1919",
        client=("192.168.50.77", 50000),
    ) as client:
        assert client.post(
            "/mutate", headers={"Origin": "http://192.168.50.10:1919"}
        ).status_code == 200
        assert client.post(
            "/mutate", headers={"Origin": "https://evil.example"}
        ).status_code == 403
        assert client.post("/mutate").status_code == 403


def test_non_browser_client_can_write_with_cookie_bound_csrf_header():
    with TestClient(
        _build_app(),
        base_url="http://192.168.50.10:1919",
        client=("192.168.50.77", 50000),
    ) as client:
        token = client.get("/").json()["csrf"]
        response = client.post("/mutate", headers={"X-CSRF-Token": token})

    assert response.status_code == 200


def test_maintenance_mode_rejects_new_mutations_but_keeps_reads_available():
    assert progress.begin_maintenance()
    try:
        with TestClient(
            _build_app(),
            base_url="http://192.168.50.10:1919",
            client=("192.168.50.77", 50000),
        ) as client:
            assert client.get("/").status_code == 200
            response = client.post(
                "/mutate", headers={"Origin": "http://192.168.50.10:1919"}
            )
    finally:
        progress.end_maintenance()

    assert response.status_code == 409
