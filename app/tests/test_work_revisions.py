"""Synthetic revision archive acceptance: no account, network, or real media."""
import io
import json
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, Work, WorkRevision
from src.core.revisions import asset_entries, history, latest_revision, revision_assets, selected_revision
from src.core.upgrade import upgrade_0003, activate_upgrade, rollback_upgrade


def test_two_thousand_pages_batch_manifest_and_refresh_without_loading_siblings(archive):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from src.core.revisions import refresh_for_record
    db, root, handler, download = archive
    queries, loaded_assets = [], []
    def record_query(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    def record_entity(session, instance):
        if isinstance(instance, Asset):
            loaded_assets.append(instance.id)
    event.listen(db.engine, "before_cursor_execute", record_query)
    try:
        handler._save_remote_work(1, data(2000))
    finally:
        event.remove(db.engine, "before_cursor_execute", record_query)
    assert len(queries) < 80
    with db.read() as session:
        asset_id = session.scalar(select(Asset.id).where(Asset.page_index == 17))
    event.listen(Session, "loaded_as_persistent", record_entity)
    try:
        with db.write() as session:
            asset = session.get(Asset, asset_id)
            asset.state, asset.relative_path = "verified", "isolated-page.png"
            refresh_for_record(session, asset)
    finally:
        event.remove(Session, "loaded_as_persistent", record_entity)
    assert loaded_assets == [asset_id]
    with db.read() as session:
        assert latest_revision(session, 1).state == "partial"


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG", compress_level=0)
    return out.getvalue()


class Download:
    def __init__(self):
        self.calls, self.colors, self.failure = [], {}, None

    def download(self, url, target, report=None):
        self.calls.append(url)
        if self.failure == url:
            raise OSError("synthetic failure")
        if url.endswith(".zip"):
            with zipfile.ZipFile(target, "w") as output:
                output.writestr(zipfile.ZipInfo("0.png", (2026, 1, 1, 0, 0, 0)), png("red"))
                output.writestr(zipfile.ZipInfo("1.png", (2026, 1, 1, 0, 0, 0)), png("blue"))
        else:
            target.write_bytes(png(self.colors.get(url, "red")))


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    root = tmp_path / "media"; root.mkdir()
    download = Download()
    handler = ArchiveHandler("repair", db, Publisher(db, root), SimpleNamespace(), download)
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="Artist"))
    yield db, root, handler, download
    db.close()


def data(pages=1, title="first", *, urls=None, animation=False):
    return {"illust_id": "10", "title": title, "caption": "caption", "tags": ["tag"],
        "type": "ugoira" if animation else "manga", "page_count": pages,
        "urls": urls or [{"original": f"https://i.pximg.net/10_p{page}.png"} for page in range(pages)]}


def save(archive, payload, *, metadata=None, compare=False, execute=True):
    db, root, handler, download = archive
    work_id = handler._save_remote_work(1, payload, metadata=metadata, compare_content=compare)
    with db.write() as session:
        revision = latest_revision(session, work_id)
        specs = handler._discovery_specs(session, session.get(Work, work_id), metadata)
    if execute:
        context = SimpleNamespace(item_id=None, checkpoint=lambda: None, report_bytes=lambda *_: None)
        for spec in specs:
            handler.execute(spec, context)
    return revision.id, specs


def test_growth_and_reduction_keep_whole_old_versions(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data(1))
    with db.read() as session:
        original_path = revision_assets(session, session.get(WorkRevision, first))[0].relative_path
    second, specs = save(archive, data(3, "three"), execute=False)
    assert len([spec for spec in specs if spec.kind == "download"]) == 3
    with db.read() as session:
        assert selected_revision(session, 1).id == first
        assert session.get(Work, 1).title == "first"
    context = SimpleNamespace(item_id=None, checkpoint=lambda: None, report_bytes=lambda *_: None)
    for spec in specs: handler.execute(spec, context)
    third, _ = save(archive, data(2, "two"))
    with db.read() as session:
        assert len(history(session, 1)) == 3
        assert [session.get(WorkRevision, key).page_count for key in (first, second, third)] == [1, 3, 2]
        assert selected_revision(session, 1).id == third
        assert session.get(Work, 1).page_count == 2
    assert (root / original_path).read_bytes() == png("red")


def test_six_to_two_and_reordered_pages_have_fixed_manifest(archive):
    db, root, handler, download = archive
    colors = ["red", "blue", "green", "yellow", "black", "white"]
    for page, color in enumerate(colors): download.colors[f"https://i.pximg.net/10_p{page}.png"] = color
    first, _ = save(archive, data(6))
    reordered = [{"original": "https://i.pximg.net/10_p1.png"}, {"original": "https://i.pximg.net/10_p0.png"}]
    second, _ = save(archive, data(2, urls=reordered))
    with db.read() as session:
        entries = asset_entries(session, session.get(WorkRevision, second))
        assert [(binding.page_index, asset.source_url) for binding, asset in entries] == [(0, reordered[0]["original"]), (1, reordered[1]["original"])]
        paths = [asset.relative_path for binding, asset in entries]
        assert len(revision_assets(session, session.get(WorkRevision, first))) == 6
    assert [(root / path).read_bytes() for path in paths] == [png("blue"), png("red")]


def test_metadata_only_and_query_change_download_nothing(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data())
    before = len(download.calls)
    second, specs = save(archive, data(title="new caption", urls=[{"original": "https://other.pximg.net/10_p0.png?fresh=1"}]))
    assert len(download.calls) == before and specs == []
    with db.read() as session:
        assert history(session, 1)[0]["change_kind"] == "metadata"
        assert revision_assets(session, session.get(WorkRevision, first))[0].id == revision_assets(session, session.get(WorkRevision, second))[0].id


def test_same_size_replacement_compare_and_unchanged_content(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data())
    assert len(png("red")) == len(png("blue"))
    download.colors["https://i.pximg.net/10_p0.png"] = "blue"
    second, _ = save(archive, data(), compare=True)
    with db.read() as session:
        a = revision_assets(session, session.get(WorkRevision, first))[0]
        b = revision_assets(session, session.get(WorkRevision, second))[0]
        assert a.byte_size == b.byte_size and a.sha256 != b.sha256 and a.relative_path != b.relative_path
    third, _ = save(archive, data(), compare=True)
    with db.read() as session:
        assert session.get(WorkRevision, third).state == "redundant"
        assert selected_revision(session, 1).id == second
        assert len(history(session, 1)) == 2


def test_changed_path_identical_bytes_reuses_storage(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data())
    second, _ = save(archive, data(urls=[{"original": "https://i.pximg.net/replaced.png"}]))
    with db.read() as session:
        assert revision_assets(session, session.get(WorkRevision, first))[0].relative_path == revision_assets(session, session.get(WorkRevision, second))[0].relative_path
        assert session.get(WorkRevision, second).change_kind == "metadata"


def test_reused_physical_file_changed_before_publish_is_never_overwritten(archive, monkeypatch):
    from src.core.media import InvalidMedia
    db, root, handler, download = archive
    first, _ = save(archive, data())
    with db.read() as session:
        original_path = revision_assets(session, session.get(WorkRevision, first))[0].relative_path
    second, specs = save(archive, data(urls=[{"original": "https://i.pximg.net/moved.png"}]), execute=False)
    publish = handler.publisher.publish
    def changed_before_publish(journal_id):
        (root / original_path).write_bytes(png("blue"))
        return publish(journal_id)
    monkeypatch.setattr(handler.publisher, "publish", changed_before_publish)
    context = SimpleNamespace(item_id=None, checkpoint=lambda: None, report_bytes=lambda *_: None)
    with pytest.raises(InvalidMedia):
        handler.execute(next(spec for spec in specs if spec.kind == "download"), context)
    assert (root / original_path).read_bytes() == png("blue")
    with db.read() as session:
        assert revision_assets(session, session.get(WorkRevision, second))[0].state == "failed"


def test_missing_caption_preserves_known_empty_and_available_snapshot(archive):
    db, root, handler, download = archive
    payload = data(); payload.pop("caption")
    unknown, _ = save(archive, payload)
    payload["caption"] = ""
    empty, _ = save(archive, payload)
    payload.pop("caption")
    unchanged, _ = save(archive, payload)
    assert unchanged == empty
    payload["caption"] = "saved caption"
    available, _ = save(archive, payload)
    payload["caption"] = None
    preserved, _ = save(archive, payload)
    assert preserved == available
    with db.read() as session:
        assert session.get(WorkRevision, unknown).caption_state == "unknown"
        assert session.get(WorkRevision, empty).caption_state == "empty"
        assert session.get(WorkRevision, available).caption_state == "available"


def test_source_changes_again_before_old_downloads_finish_keep_target_manifest(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data())
    second, older_specs = save(archive, data(2, "second"), execute=False)
    third, latest_specs = save(archive, data(3, "third"), execute=False)
    context = SimpleNamespace(item_id=None, checkpoint=lambda: None, report_bytes=lambda *_: None)
    for spec in older_specs:
        handler.execute(spec, context)
    with db.read() as session:
        assert latest_revision(session, 1).id == third
        assert selected_revision(session, 1).id == second
        assert session.get(Work, 1).page_count == 2
        assert all(asset.state == "verified" for asset in revision_assets(session, session.get(WorkRevision, second)))
        assert all(asset.state == "missing" for asset in revision_assets(session, session.get(WorkRevision, third)))
    for spec in latest_specs:
        handler.execute(spec, context)
    with db.read() as session:
        assert selected_revision(session, 1).id == third
        assert len(history(session, 1)) == 3


def test_incomplete_restart_and_deletion_block_late_results(archive):
    db, root, handler, download = archive
    first, _ = save(archive, data())
    download.failure = "https://i.pximg.net/10_p1.png"
    second, specs = save(archive, data(3), execute=False)
    context = SimpleNamespace(item_id=None, checkpoint=lambda: None, report_bytes=lambda *_: None)
    handler.execute(specs[0], context)
    with pytest.raises(OSError): handler.execute(specs[1], context)
    with db.read() as session:
        assert selected_revision(session, 1).id == first
    reopened = ArchiveDatabase(db.path)
    with reopened.read() as session:
        assert selected_revision(session, 1).id == first
        assert history(session, 1)[0]["complete"] is False
    reopened.close()
    handler.publisher.suppress(1)
    assert handler.execute(specs[2], context).state == "skipped"
    assert handler._save_remote_work(1, data(4)) is None


def test_ugoira_timing_revision_keeps_original_gif(archive):
    db, root, handler, download = archive
    meta = {"zip_urls": {"original": "https://i.pximg.net/10.zip"},
        "frames": [{"file": "0.png", "delay": 80}, {"file": "1.png", "delay": 120}]}
    first, _ = save(archive, data(animation=True), metadata=meta)
    changed = {**meta, "frames": [{"file": "0.png", "delay": 200}, {"file": "1.png", "delay": 100}]}
    second, _ = save(archive, data(animation=True), metadata=changed)
    with db.read() as session:
        a = {asset.role: asset for asset in revision_assets(session, session.get(WorkRevision, first))}
        b = {asset.role: asset for asset in revision_assets(session, session.get(WorkRevision, second))}
        assert a["ugoira_zip"].relative_path == b["ugoira_zip"].relative_path
        assert a["ugoira_timing"].sha256 != b["ugoira_timing"].sha256
        assert len(history(session, 1)) == 2
