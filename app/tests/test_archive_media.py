import io
import json
import zipfile

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.database import ArchiveDatabase
from src.core.media import InvalidMedia, Publisher, build_gif, contained_path, verify_media
from src.core.models import ArchiveIntent, Artist, Asset, PublicationJournal, Work


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(stream, format="PNG")
    return stream.getvalue()


def animation(tmp_path):
    archive, timing = tmp_path / "original.zip", tmp_path / "timing.json"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("z.png", png("red"))
        output.writestr("a.png", png("blue"))
    timing.write_text(json.dumps({"frames": [
        {"file": "z.png", "delay": 200}, {"file": "a.png", "delay": 300}
    ]}), encoding="utf-8")
    return archive, timing


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "archive.db")
    with db.write() as session:
        artist = Artist(pixiv_user_id="1", name="test")
        work = Work(pixiv_illust_id="2", title="test", artist=artist)
        asset = Asset(work_id=1, role="image")
        session.add_all([work, ArchiveIntent(pixiv_illust_id="2")])
        session.flush()
        asset.work_id = work.id
        session.add(asset)
        session.flush()
        asset_id = asset.id
    yield db, Publisher(db, tmp_path / "media"), asset_id
    db.close()


@pytest.mark.parametrize("contents", [b"", b"not an image", png()[:30]])
def test_nonempty_does_not_mean_complete(tmp_path, contents):
    path = tmp_path / "image.png"
    path.write_bytes(contents)
    with pytest.raises(InvalidMedia):
        verify_media(path, "image")


def test_animation_uses_recorded_order_and_delays_and_keeps_inputs(tmp_path):
    original, timing = animation(tmp_path)
    before = original.read_bytes(), timing.read_bytes()
    target = tmp_path / "preview.part"
    build_gif(original, timing, target)
    with Image.open(target) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        assert image.info["duration"] == 200
        image.seek(1)
        assert image.convert("RGB").getpixel((0, 0)) == (0, 0, 255)
        assert image.info["duration"] == 300
    assert (original.read_bytes(), timing.read_bytes()) == before


def test_animation_does_not_guess_missing_frames_or_timing(tmp_path):
    original, timing = animation(tmp_path)
    timing.write_text('{"frames": [{"file":"missing.png","delay":100}]}', encoding="utf-8")
    with pytest.raises(InvalidMedia, match="不匹配"):
        build_gif(original, timing, tmp_path / "preview.gif")
    assert original.exists()
    assert not (tmp_path / "preview.gif").exists()


def test_conversion_resource_limit_keeps_originals(tmp_path):
    original, timing = animation(tmp_path)
    with pytest.raises(InvalidMedia, match="内存上限"):
        build_gif(original, timing, tmp_path / "preview.gif", max_pixels=1)
    assert original.exists() and timing.exists()


@pytest.mark.parametrize("relative", ["../outside", "/outside", "a\\b", "a:stream", "a/./b", "a//b"])
def test_media_path_boundary(tmp_path, relative):
    with pytest.raises(ValueError):
        contained_path(tmp_path, relative)


def prepared(archive):
    db, publisher, asset_id = archive
    relative, path = publisher.staging()
    path.write_bytes(png())
    journal_id = publisher.prepare(asset_id, relative, "artist/2_p0.png",
                                   verify_media(path, "image"), expected_revision=1)
    return journal_id, path


def test_crash_after_file_move_recovers_without_redownload(archive):
    db, publisher, asset_id = archive
    journal_id, path = prepared(archive)

    def crash():
        raise SystemExit("simulated process death")

    with pytest.raises(SystemExit):
        publisher.publish(journal_id, after_move=crash)
    assert not path.exists()
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "unknown"
        assert session.get(PublicationJournal, journal_id).step == "prepared"
    assert publisher.recover() == [{"id": journal_id, "state": "committed"}]
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "verified"
    assert publisher.recover() == []


def test_newer_delete_decision_defeats_old_publication(archive):
    db, publisher, asset_id = archive
    journal_id, path = prepared(archive)
    with db.write() as session:
        intent = session.get(ArchiveIntent, "2")
        intent.mode, intent.revision = "suppressed", 2
    assert publisher.publish(journal_id) == "obsolete"
    assert path.exists()  # retained privately, never exposed as a library image
    assert not (publisher.root / "artist/2_p0.png").exists()
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "unknown"


def test_disk_publish_failure_does_not_claim_success(archive, monkeypatch):
    db, publisher, asset_id = archive
    journal_id, path = prepared(archive)

    def disk_failure(*_):
        raise OSError("simulated disk full")

    with monkeypatch.context() as patch:
        patch.setattr("src.core.media.os.replace", disk_failure)
        with pytest.raises(OSError):
            publisher.publish(journal_id)
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "unknown"
        assert session.get(PublicationJournal, journal_id).step == "prepared"
    assert path.exists()
    assert publisher.publish(journal_id) == "committed"


def test_corrupt_previous_file_is_retained_until_review(archive):
    db, publisher, asset_id = archive
    target = publisher.root / "artist/2_p0.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old damaged bytes")
    journal_id, _ = prepared(archive)
    publisher.publish(journal_id)
    with db.read() as session:
        journal = session.get(PublicationJournal, journal_id)
        backup = contained_path(publisher.root, journal.verification["backup"])
    assert backup.read_bytes() == b"old damaged bytes"
    assert target.read_bytes() == png()
