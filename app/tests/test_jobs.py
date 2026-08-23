import threading
import time

from src import progress
from src.progress import JobManager


def test_job_manager_serializes_jobs_and_retains_terminal_results():
    manager = JobManager(history_limit=10)
    order = []

    try:
        first_id, first_created = manager.submit("first", lambda: order.append("first"))
        second_id, second_created = manager.submit("second", lambda: order.append("second"))

        assert first_created and second_created
        assert manager.wait_for_idle(timeout=2)
        assert order == ["first", "second"]
        assert manager.get_job(first_id)["status"] == "succeeded"
        assert manager.get_job(second_id)["status"] == "succeeded"
    finally:
        manager.shutdown()


def test_job_manager_deduplicates_queued_or_running_work():
    manager = JobManager(history_limit=10)
    started = threading.Event()
    release = threading.Event()

    def blocking_job():
        started.set()
        assert release.wait(2)

    try:
        first_id, created = manager.submit("refresh", blocking_job, dedupe_key="refresh-all")
        assert created
        assert started.wait(1)

        duplicate_id, duplicate_created = manager.submit(
            "refresh", lambda: None, dedupe_key="refresh-all"
        )
        assert not duplicate_created
        assert duplicate_id == first_id

        release.set()
        assert manager.wait_for_idle(timeout=2)
    finally:
        release.set()
        manager.shutdown()


def test_running_job_is_never_pruned_by_age():
    manager = JobManager(history_limit=10, terminal_ttl=0.01)
    started = threading.Event()
    release = threading.Event()

    def blocking_job():
        started.set()
        assert release.wait(2)

    try:
        job_id, _ = manager.submit("long", blocking_job)
        assert started.wait(1)
        time.sleep(0.03)
        manager.prune()
        assert manager.get_job(job_id)["status"] == "running"
    finally:
        release.set()
        manager.wait_for_idle(timeout=2)
        manager.shutdown()


def test_job_failure_is_visible_after_completion():
    manager = JobManager(history_limit=10)

    def fail():
        raise RuntimeError("download exploded")

    try:
        job_id, _ = manager.submit("download", fail)
        assert manager.wait_for_idle(timeout=2)
        job = manager.get_job(job_id)
        assert job["status"] == "failed"
        assert any("download exploded" in item for item in job["errors"])
    finally:
        manager.shutdown()


def test_exclusive_job_is_rejected_while_any_job_is_active():
    manager = JobManager(history_limit=10)
    started = threading.Event()
    release = threading.Event()

    def blocking_job():
        started.set()
        assert release.wait(2)

    try:
        manager.submit("download", blocking_job)
        assert started.wait(1)
        job_id, created = manager.submit_exclusive("migration", lambda: None)
        assert job_id is None
        assert not created
    finally:
        release.set()
        manager.wait_for_idle(timeout=2)
        manager.shutdown()


def test_wait_for_job_returns_terminal_snapshot():
    manager = JobManager(history_limit=10)
    try:
        job_id, _ = manager.submit("quick", lambda: "done")
        job = manager.wait_for_job(job_id, timeout=2)
        assert job["status"] == "succeeded"
        assert job["result"] == "done"
    finally:
        manager.shutdown()


def test_maintenance_gate_excludes_short_mutations():
    assert progress.begin_mutation()
    try:
        assert not progress.begin_maintenance()
    finally:
        progress.end_mutation()

    assert progress.begin_maintenance()
    try:
        assert progress.maintenance_active()
        assert not progress.begin_mutation()
    finally:
        progress.end_maintenance()
    assert not progress.maintenance_active()
