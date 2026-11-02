"""Real Uvicorn lifecycle, without a database or external services."""
from threading import Event

import pytest


@pytest.mark.parametrize("failure", [None, "start", "close"])
def test_web_return_does_not_hide_lifespan_failure(monkeypatch, failure):
    from src import web
    from src.core import lifecycle

    observed = []

    class Runtime:
        client = None

        def start(self):
            if failure == "start":
                raise ValueError("synthetic private startup message")

        def request_stop(self, **kwargs):
            pass

        def close(self, **kwargs):
            if failure == "close":
                raise ValueError("synthetic private close message")

    monkeypatch.setattr(web, "_shutdown_requested", Event())
    monkeypatch.setattr(web, "_server", None)
    monkeypatch.setattr(web, "PORT", 0)
    monkeypatch.setattr(web, "_print_startup_urls", lambda: None)
    monkeypatch.setattr(web, "cancel_auth", lambda: None)
    monkeypatch.setattr(lifecycle, "exception", lambda where, exc: observed.append((where, type(exc))))
    application = web.create_app(Runtime, authenticate=False, schedule=False)
    monkeypatch.setattr(web, "app", application)
    if failure:
        with pytest.raises(RuntimeError, match="startup or shutdown failed"):
            web.main(ready_callback=web.request_shutdown)
        assert ("web_lifespan", ValueError) in observed
    else:
        web.main(ready_callback=web.request_shutdown)
        assert observed == []
    assert web._server is None
