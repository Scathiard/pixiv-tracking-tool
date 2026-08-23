import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import config, tombstones, tracker as tracker_module
from src.file_index import encode_file_paths
from src.models import Base, Illustration, TrackedArtist
from src.tracker import Tracker, _artist_dir_name


def test_tombstones_are_atomic_and_survive_reload(monkeypatch, tmp_path):
    store = tmp_path / "data" / "deleted_illusts.json"
    monkeypatch.setattr(tombstones, "_store_path", lambda: store)

    assert tombstones.add_deleted_illust("100")
    assert not tombstones.add_deleted_illust("100")
    assert tombstones.load_deleted_illust_ids() == {"100"}
    assert json.loads(store.read_text(encoding="utf-8")) == ["100"]
    assert list(store.parent.glob("*.tmp")) == []

    tombstones.discard_deleted_illust("100")
    assert tombstones.load_deleted_illust_ids() == set()


def test_corrupt_tombstone_file_fails_closed(monkeypatch, tmp_path):
    store = tmp_path / "deleted_illusts.json"
    store.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(tombstones, "_store_path", lambda: store)

    with pytest.raises(RuntimeError, match="无法读取"):
        tombstones.load_deleted_illust_ids()


def test_purge_deleted_files_matches_exact_numeric_prefix(monkeypatch, tmp_path):
    store = tmp_path / "deleted_illusts.json"
    store.write_text('["100"]', encoding="utf-8")
    monkeypatch.setattr(tombstones, "_store_path", lambda: store)
    artist = tmp_path / "images" / "Artist 1"
    artist.mkdir(parents=True)
    for name in ("100_p0.jpg", "100.caption.html", "1000_p0.jpg", "note.txt"):
        (artist / name).write_text("x", encoding="utf-8")

    assert tombstones.purge_deleted_files(artist) == 2
    assert not (artist / "100_p0.jpg").exists()
    assert not (artist / "100.caption.html").exists()
    assert (artist / "1000_p0.jpg").exists()


def test_permanent_delete_keeps_outside_files_and_creates_tombstone(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    images = tmp_path / "images"
    data = tmp_path / "data"
    images.mkdir()
    data.mkdir()
    monkeypatch.setattr(tracker_module, "Session", test_session)
    monkeypatch.setattr(config, "IMAGES_DIR", images)
    monkeypatch.setattr(config, "DATA_DIR", data)

    with test_session() as session:
        artist = TrackedArtist(pixiv_user_id="1", name="Artist", is_active=True)
        session.add(artist)
        session.flush()
        artist_dir = images / _artist_dir_name(artist)
        artist_dir.mkdir()
        artwork = artist_dir / "100_p0.jpg"
        caption = artist_dir / "100.caption.html"
        artwork.write_bytes(b"image")
        caption.write_text("caption", encoding="utf-8")
        outside = tmp_path / "secret.txt"
        outside.write_text("keep", encoding="utf-8")
        illust = Illustration(
            pixiv_illust_id="100",
            artist_id=artist.id,
            title="Work",
            file_paths=encode_file_paths([
                f"/images/{artist_dir.name}/100_p0.jpg",
                "/images/../secret.txt",
            ]),
        )
        session.add(illust)
        session.commit()
        local_id = illust.id

    tracker = Tracker.__new__(Tracker)
    tracker.permanently_delete_illust(local_id)

    with test_session() as session:
        assert session.get(Illustration, local_id) is None
    assert not artwork.exists()
    assert not caption.exists()
    assert outside.read_text(encoding="utf-8") == "keep"
    assert tombstones.load_deleted_illust_ids() == {"100"}
