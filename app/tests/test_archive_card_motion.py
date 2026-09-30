"""Bounded local card animation; synthetic inputs and no account requests."""
from io import BytesIO
import zipfile

from PIL import Image
import pytest
from sqlalchemy import select

from src.core import media
from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.maintenance import MissingMediaMaintenance
from src.core.media import Publisher, build_thumbnail, verify_media
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, OperationItem, Work
from src.core.tasks import TaskQueue


def gif(path, *, count=16, size=(640, 480), duration=50):
    frames = [Image.new("RGB", size, (index * 47 % 256, index * 29 % 256, index * 17 % 256))
              for index in range(count)]
    try:
        frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                       duration=duration, loop=0, optimize=False)
    finally:
        for frame in frames:
            frame.close()


def durations(path):
    with Image.open(path) as image:
        result = []
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            result.append(image.info.get("duration", 0))
        return image.size, result


@pytest.mark.parametrize("count,duration", [(16, 50), (250, 20), (130, 130)])
def test_builder_dimensions_frames_and_total_duration(tmp_path, count, duration):
    source, output = tmp_path / "source.gif", tmp_path / "card.webp"
    gif(source, count=count, duration=duration)
    before = source.read_bytes()
    verified = media.build_card_motion(source, output)
    size, timing = durations(output)
    assert max(size) <= 360 and size == (360, 270)
    assert 1 < len(timing) <= 120
    assert size[0] * size[1] * len(timing) <= media.CARD_MOTION_MAX_PIXELS
    assert sum(timing) == count * duration
    assert verified.sha256 == media.file_digest(output)
    assert source.read_bytes() == before


def test_builder_defaults_absent_duration_and_checks_boundaries(tmp_path):
    source, output = tmp_path / "source.gif", tmp_path / "card.webp"
    gif(source, count=3, size=(10, 10), duration=0)
    media.build_card_motion(source, output)
    assert sum(durations(output)[1]) == 300
    with pytest.raises(media.InvalidMedia):
        media.build_card_motion(source, output, max_input_frames=2)
    with pytest.raises(media.InvalidMedia):
        media.build_card_motion(source, output, max_input_pixels=200)
    with pytest.raises(media.InvalidMedia):
        media.build_card_motion(source, output, max_duration_ms=200)


@pytest.mark.parametrize("timing", [[50, 50], [5, 95], [300, 50]])
def test_short_loop_keeps_two_visible_frames_and_decoded_duration(tmp_path, timing):
    source, output = tmp_path / "source.gif", tmp_path / "card.webp"
    gif(source, count=2, size=(10, 10), duration=timing)
    media.build_card_motion(source, output)
    _, result = durations(output)
    assert len(result) == 2
    # GIF stores hundredths of a second; sub-10 ms becomes an absent delay.
    _, decoded = durations(source)
    assert sum(result) == sum(delay or 100 for delay in decoded)


def test_builder_stop_does_not_touch_existing_output(tmp_path):
    source, output = tmp_path / "source.gif", tmp_path / "card.webp"
    gif(source, count=8, size=(10, 10))
    output.write_bytes(b"old")
    calls = []
    def stop():
        calls.append(True)
        if len(calls) == 3:
            raise RuntimeError("synthetic stop")
    with pytest.raises(RuntimeError, match="synthetic stop"):
        media.build_card_motion(source, output, checkpoint=stop)
    assert output.read_bytes() == b"old"


class NoRemote:
    def __getattr__(self, name):
        raise AssertionError("local cache generation must not access remote: " + name)


@pytest.fixture
def local_archive(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    root = tmp_path / "images"
    root.mkdir()
    gif(root / "101.gif", size=(32, 24))
    verified = verify_media(root / "101.gif", "gif")
    publisher = Publisher(database, root)
    queue = TaskQueue(database, {kind: ArchiveHandler(kind, database, publisher, NoRemote(), NoRemote())
                                for kind in ("check", "repair", "thumbnails")})
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="local GIF", type="ugoira",
                    source_metadata={"original_backfill_requires_selection": True,
                                     "remote_availability": {"state": "unavailable"}})
        work.derivatives = [Derivative(kind="gif", state="verified", relative_path="101.gif",
                                      sha256=verified.sha256, byte_size=verified.byte_size)]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="101")])
    yield database, root, queue, publisher
    database.close()


@pytest.mark.parametrize("kind", ["check", "repair", "thumbnails"])
def test_local_gif_only_jobs_create_card_without_originals(local_archive, kind):
    database, root, queue, _ = local_archive
    before = (root / "101.gif").read_bytes()
    if kind == "check":
        class DiscoveryOnly:
            def get_all_artist_illusts(self, _):
                yield {"illust_id": "101", "title": "local GIF", "type": "ugoira", "page_count": 1}
        queue.handlers[kind].client = DiscoveryOnly()
    queue.submit(kind, {"artist_ids": [1]} if kind == "check" else {"work_ids": [1]}, "synthetic")
    assert queue.run_round()
    with database.read() as session:
        motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
        original = session.scalar(select(Derivative).where(Derivative.kind == "gif"))
        assert motion is not None and motion.state == "verified"
        assert motion.kind == "thumbnail" and motion.source_version == original.sha256
        assert motion.relative_path == ".thumbnails/101_card_motion_v1.webp"
    assert (root / "101.gif").read_bytes() == before
    library = Library(database)
    card = library.query(compact=True)["items"][0]
    assert "card_motion_v1.webp?v=" in card["preview"] and card["thumbnail"]
    assert "101.gif" in library.query()["items"][0]["preview"]
    assert "101.gif" in library.detail(1)["pages"][0]["src"]
    assert "101.gif" in library.feed()["items"][0]["src"]


def test_compact_pending_then_source_update_rebuild(local_archive):
    database, root, queue, _ = local_archive
    library = Library(database)
    card = library.query(compact=True)["items"][0]
    assert card["preview"] is None and card["preview_status"] == "pending"
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    old_url = library.query(compact=True)["items"][0]["preview"]
    gif(root / "101.gif", count=6, size=(32, 24), duration=120)
    verified = verify_media(root / "101.gif", "gif")
    with database.write() as session:
        original = session.scalar(select(Derivative).where(Derivative.kind == "gif"))
        original.sha256 = verified.sha256
    assert library.query(compact=True)["items"][0]["preview"] is None
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    assert library.query(compact=True)["items"][0]["preview"] != old_url


def test_maintenance_fills_motion_when_static_exists_offline_and_dedupes(local_archive):
    database, root, queue, publisher = local_archive
    static = root / "static.webp"
    verified = build_thumbnail(root / "101.gif", static)
    with database.write() as session:
        session.add(Derivative(work_id=1, kind="thumbnail", state="verified", relative_path="static.webp",
                               sha256=verified.sha256))
    maintenance = MissingMediaMaintenance(database, queue, remote_available=lambda: False, monotonic=lambda: 0)
    assert len(maintenance.tick(force=True)) == 1
    assert maintenance.tick(force=True) == []
    queue.run_round()
    assert maintenance.tick(force=True) == []
    assert Library(database).query(compact=True)["items"][0]["preview"]
    publisher.suppress(1)
    with database.write() as session:
        session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1")).state = "invalid"
    assert maintenance.tick(force=True) == []


def test_failed_rebuild_preserves_old_motion(local_archive, monkeypatch):
    database, root, queue, _ = local_archive
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    output = root / ".thumbnails/101_card_motion_v1.webp"
    before = output.read_bytes()
    with database.write() as session:
        session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1")).source_version = "old-source"
    def fail(*args, **kwargs):
        raise media.InvalidMedia("synthetic failure")
    monkeypatch.setattr("src.core.archive.build_card_motion", fail)
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    assert output.read_bytes() == before


class SyntheticRemote:
    def __init__(self):
        self.calls = []
    def get_all_artist_illusts(self, _):
        yield {"illust_id": "201", "title": "new animation", "type": "ugoira", "page_count": 1}
    def get_ugoira_metadata(self, _):
        self.calls.append("metadata")
        return {"zip_urls": {"original": "https://i.pximg.net/synthetic.zip"},
                "frames": [{"file": "1.png", "delay": 100}, {"file": "2.png", "delay": 200}]}
    def download(self, url, destination, report=None):
        self.calls.append("download")
        with zipfile.ZipFile(destination, "w") as archive:
            for index, color in enumerate(("red", "blue"), 1):
                output = BytesIO()
                with Image.new("RGB", (12, 8), color) as frame:
                    frame.save(output, format="PNG")
                archive.writestr(f"{index}.png", output.getvalue())


@pytest.mark.parametrize("kind", ["check", "repair"])
def test_download_pair_gif_both_caches_dependency_chain(tmp_path, kind):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    root = tmp_path / "images"
    root.mkdir()
    remote = SyntheticRemote()
    queue = TaskQueue(database, {name: ArchiveHandler(name, database, Publisher(database, root), remote, remote)
                                for name in ("check", "repair")})
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            if kind == "repair":
                work = Work(id=1, pixiv_illust_id="201", artist_id=1, title="new animation", type="ugoira")
                work.assets = [Asset(role="ugoira_zip", state="missing"), Asset(role="ugoira_timing", state="missing")]
                session.add_all([work, ArchiveIntent(pixiv_illust_id="201")])
        queue.submit(kind, {"artist_ids": [1]}, "synthetic")
        queue.run_round()
        with database.read() as session:
            items = list(session.scalars(select(OperationItem)))
            gifs = [item for item in items if item.kind == "gif"]
            cache_items = [item for item in items if item.kind == "thumbnail"]
            assert len(gifs) == 1 and len(cache_items) == 2
            assert all(item.state == "succeeded" for item in cache_items)
            assert all(item.depends_on == [gifs[0].item_key] for item in cache_items)
            motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
            original = session.scalar(select(Derivative).where(Derivative.kind == "gif"))
            assert motion.source_version == original.sha256
        assert remote.calls == ["metadata", "download"]
    finally:
        database.close()


def test_publisher_recovers_interrupted_motion_and_rejects_changed_source(local_archive):
    database, root, queue, publisher = local_archive
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    with database.read() as session:
        motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
    relative, staged = publisher.staging()
    gif(root / "new.gif", count=3, size=(12, 8), duration=100)
    verified = media.build_card_motion(root / "new.gif", staged)
    journal = publisher.prepare(motion.id, relative, motion.relative_path, verified, derivative=True,
                                expected_revision=1, source_version=motion.source_version)
    def interrupt():
        raise RuntimeError("synthetic process interruption")
    with pytest.raises(RuntimeError):
        publisher.publish(journal, after_move=interrupt)
    assert publisher.recover() == [{"id": journal, "state": "committed"}]
    assert (root / motion.relative_path).read_bytes() and media.file_digest(root / motion.relative_path) == verified.sha256
    relative, staged = publisher.staging()
    staged.write_bytes((root / motion.relative_path).read_bytes())
    obsolete = publisher.prepare(motion.id, relative, motion.relative_path, verified, derivative=True,
                                 expected_revision=1, source_version="obsolete-source")
    assert publisher.publish(obsolete) == "obsolete"
    assert staged.exists()


def test_maintenance_detects_source_change_and_missing_recorded_path(local_archive):
    database, root, queue, _ = local_archive
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    gif(root / "101.gif", count=4, size=(32, 24), duration=150)
    verified = verify_media(root / "101.gif", "gif")
    with database.write() as session:
        session.scalar(select(Derivative).where(Derivative.kind == "gif")).sha256 = verified.sha256
    maintenance = MissingMediaMaintenance(database, queue, remote_available=lambda: False, monotonic=lambda: 0)
    assert len(maintenance.tick(force=True)) == 1
    queue.run_round()
    assert maintenance.tick(force=True) == []
    with database.write() as session:
        session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1")).relative_path = None
    # The completed batch's local track waits five minutes before checking again.
    _, candidates, _, _, _, _ = maintenance._candidates()
    assert candidates == [1]


def test_unknown_gif_is_verified_once_then_uses_cached_stat(local_archive, monkeypatch):
    database, root, queue, _ = local_archive
    with database.write() as session:
        source = session.scalar(select(Derivative).where(Derivative.kind == "gif"))
        source.state, source.sha256 = "unknown", None
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    with database.read() as session:
        source = session.scalar(select(Derivative).where(Derivative.kind == "gif"))
        motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
        assert source.state == "verified" and source.sha256 == motion.source_version
    def forbidden(*_):
        raise AssertionError("unchanged known GIF must not be decoded again")
    monkeypatch.setattr("src.core.archive.verify_media", forbidden)
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    assert queue.snapshot()["round"]["outcome"] == "empty"


def test_source_unavailable_and_missing_pair_do_not_block_local_card(local_archive):
    database, root, queue, _ = local_archive
    with database.write() as session:
        work = session.get(Work, 1)
        work.source_metadata = {"remote_availability": {"state": "unavailable", "checked_at": "2026-10-01T00:00:00Z"}}
        work.assets = [Asset(role="ugoira_zip", state="missing"), Asset(role="ugoira_timing", state="missing")]
    queue.submit("repair", {"work_ids": [1]}, "synthetic", source="maintenance")
    queue.run_round()
    card = Library(database).query(compact=True)["items"][0]
    assert card["preview"] and card["availability"]["state"] == "unavailable"
    assert card["archive"]["state"] != "complete"
    assert "101.gif" in Library(database).detail(1)["pages"][0]["src"]


def test_cooperative_stop_keeps_motion_pending_and_resumes(local_archive, monkeypatch):
    database, root, queue, _ = local_archive
    builder = media.build_card_motion
    def stop(source, destination, *, checkpoint):
        queue.request_stop()
        checkpoint()
    monkeypatch.setattr("src.core.archive.build_card_motion", stop)
    request = queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    assert queue.details(request["operation_id"])["state"] == "interrupted"
    with database.read() as session:
        motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
        assert motion.state != "failed"
        item = session.scalar(select(OperationItem).where(OperationItem.target["record_id"].as_string() == motion.id))
        assert item.state == "pending"
    monkeypatch.setattr("src.core.archive.build_card_motion", builder)
    queue._stop.clear()
    queue._accepting = True
    queue.recover()
    queue.run_round()
    assert Library(database).query(compact=True)["items"][0]["preview"]


def test_motion_deletion_recovery_keeps_suppression(local_archive):
    database, root, queue, publisher = local_archive
    queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
    queue.run_round()
    with database.read() as session:
        motion = session.scalar(select(Derivative).where(Derivative.spec_key == "card-motion-v1"))
    journals = publisher.suppress(1)
    from src.core.models import PublicationJournal
    with database.read() as session:
        journal = session.scalar(select(PublicationJournal).where(PublicationJournal.action == "delete",
                                                                 PublicationJournal.staging_path == motion.relative_path))
    def crash():
        raise SystemExit()
    with pytest.raises(SystemExit):
        publisher.finish_deletion(journal.id, after_move=crash)
    results = publisher.recover()
    assert all(item["state"] == "committed" for item in results)
    assert not (root / motion.relative_path).exists()
    assert Library(database).query(compact=True)["items"] == []
    assert publisher.suppress(1) == journals
