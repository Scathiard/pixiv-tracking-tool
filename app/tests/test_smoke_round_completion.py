from types import SimpleNamespace
import pytest
from src import smoke


def test_smoke_waits_for_deferred_round_without_bypassing_queue(monkeypatch):
    calls=[]; results=iter((False, False, True)); sleeps=[]
    queue=SimpleNamespace(worker_error=None, run_round=lambda: (calls.append('run') or next(results)))
    monkeypatch.setattr(smoke.time,'monotonic',lambda: 0)
    monkeypatch.setattr(smoke.time,'sleep',sleeps.append)
    smoke._run_round_to_completion(queue)
    assert calls==['run']*3 and sleeps==[.05,.05]


def test_smoke_incomplete_round_has_finite_deadline(monkeypatch):
    clock=iter((0,31))
    monkeypatch.setattr(smoke.time,'monotonic',lambda: next(clock))
    monkeypatch.setattr(smoke.time,'sleep',lambda _: pytest.fail('deadline must stop waiting'))
    with pytest.raises(TimeoutError,match='did not finish'):
        smoke._run_round_to_completion(SimpleNamespace(worker_error=None,run_round=lambda:False))


def test_smoke_does_not_hide_worker_failure(monkeypatch):
    monkeypatch.setattr(smoke.time,'monotonic',lambda:0)
    with pytest.raises(RuntimeError,match='worker failed'):
        smoke._run_round_to_completion(SimpleNamespace(worker_error='synthetic',run_round=lambda:False))