from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import config, tracker as tracker_module
from src.file_index import decode_file_paths, encode_file_paths, scan_artist_files
from src.models import Base, Illustration, TrackedArtist
from src.tracker import _artist_dir_name, _merge_and_cleanup, rebuild_all_file_paths


def test_file_paths_json_round_trip_preserves_commas_and_unicode():
    paths = [
        "/images/Alpha, Beta 123/100_p0.jpg",
        "/images/画师 名字 123/100_p1.png",
    ]

    encoded = encode_file_paths(paths)

    assert encoded.startswith("[")
    assert decode_file_paths(encoded) == paths


def test_decode_file_paths_accepts_legacy_comma_format_temporarily():
    assert decode_file_paths("/images/a/1.jpg,/images/a/2.jpg") == [
        "/images/a/1.jpg",
        "/images/a/2.jpg",
    ]


def test_scan_artist_files_reads_directory_once_and_groups_by_illustration(tmp_path: Path):
    artist_dir = tmp_path / "Alpha, Beta 123"
    artist_dir.mkdir()
    for name in (
        "100_p0.jpg",
        "100_p1.png",
        "200.gif",
        "200.caption.html",
        "300_p0.zip",
        "400_p0.jpg.part",
    ):
        (artist_dir / name).write_bytes(b"x")

    index = scan_artist_files(artist_dir, "/images/Alpha, Beta 123")

    assert index == {
        "100": [
            "/images/Alpha, Beta 123/100_p0.jpg",
            "/images/Alpha, Beta 123/100_p1.png",
        ],
        "200": ["/images/Alpha, Beta 123/200.gif"],
    }


def test_startup_rebuild_repairs_legacy_comma_paths(monkeypatch, tmp_path: Path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr(tracker_module, "Session", test_session)
    monkeypatch.setattr(config, "IMAGES_DIR", images)

    with test_session() as session:
        artist = TrackedArtist(pixiv_user_id="1", name="Alpha, Beta", is_active=True)
        session.add(artist)
        session.flush()
        artist_dir = images / _artist_dir_name(artist)
        artist_dir.mkdir()
        (artist_dir / "100_p0.jpg").write_bytes(b"image")
        illust = Illustration(
            pixiv_illust_id="100",
            artist_id=artist.id,
            title="Work",
            file_paths="/images/Alpha, Beta 1/100_p0.jpg",
        )
        session.add(illust)
        session.commit()
        illust_id = illust.id

    assert rebuild_all_file_paths() == 1

    with test_session() as session:
        repaired = session.get(Illustration, illust_id)
        assert decode_file_paths(repaired.file_paths) == [
            "/images/Alpha, Beta 1/100_p0.jpg"
        ]
        assert repaired.file_paths.startswith("[")


def test_merge_never_deletes_when_source_and_target_are_same_directory(tmp_path: Path):
    folder = tmp_path / "Artist 1"
    folder.mkdir()
    artwork = folder / "100_p0.jpg"
    artwork.write_bytes(b"image")

    _merge_and_cleanup(folder, folder)

    assert artwork.read_bytes() == b"image"
