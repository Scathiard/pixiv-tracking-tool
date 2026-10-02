"""Synthetic tag metadata only; no Pixiv authentication or real catalogue."""
import pytest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.client import PixivClient, PixivRequestError
from src.core.archive import ArchiveHandler
from src.core.catalogue_revision import catalogue_revision
from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.media import Publisher
from src.core.models import Artist, Work
from src.core.tag_translations import parse_tag_details
from src.core.tasks import TaskQueue
from src.routes.archive_api import create_router


def raw_work(tags, **extra):
    return {"id": 123, "title": "Synthetic tag work", "type": "illust", "page_count": 1,
            "image_urls": {}, "meta_single_page": {}, "tags": tags, **extra}


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    media = tmp_path / "media"
    media.mkdir()
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="Synthetic artist"))
    handler = ArchiveHandler("metadata", db, Publisher(db, media), PixivClient())
    yield db, handler, Library(db)
    db.close()


def revision(db):
    with db.read() as session:
        return catalogue_revision(session)


@pytest.mark.parametrize("tags,extra,expected,source_field", [
    ([{"name": "オリジナル", "translated_name": "原创"}], {}, "原创", "tags[].translated_name"),
    ([{"name": "オリジナル", "translated_name": "original", "translation": {"zh": "原创", "en": "original"}}],
     {}, "原创", "tags[].translation.zh"),
    ([{"name": "オリジナル", "translated_name": "original"}],
     {"tagTranslation": {"オリジナル": {"zh": "原创", "en": "original"}}}, "原创", "tagTranslation[tag].zh"),
    ({"tags": [{"tag": "オリジナル", "translation": {"zh": "原创"}}]},
     {}, "原创", "tags.tags[].translation.zh"),
])
def test_parse_save_detail_preserves_field_provenance_and_original_filter(archive, tags, extra, expected, source_field):
    db, handler, library = archive
    parsed = PixivClient._parse_illust(raw_work(tags, **extra))
    assert parsed["tags"] == ["オリジナル"]
    work_id = handler._save_remote_work(1, parsed)
    view = library.detail(work_id)
    assert view["tags"] == ["オリジナル"]
    detail = view["tag_details"][0]
    assert detail["name"] == "オリジナル" and detail["translated_name"] == expected
    assert detail["language"] == "zh-cn" and detail["requested_locale"] == "zh-cn"
    assert detail["source"] == "pixiv" and detail["source_field"] == source_field
    assert detail["language_basis"] == ("requested_locale" if source_field.endswith("translated_name") else "explicit_language_key")
    assert detail["checked_at"]
    with db.read() as session:
        assert session.get(Work, work_id).source_metadata["tag_translations"]["オリジナル"]["source_field"] == source_field
    assert library.query(LibraryFilter(tags=("オリジナル",)))["total"] == 1
    assert library.query(LibraryFilter(tags=("原创",)))["total"] == 0
    assert library.tags() == ["オリジナル"]


@pytest.mark.parametrize("tags,extra", [
    ([{"name": "original", "translated_name": None}], {}),
    ([{"name": "original"}], {}),
    ([{"name": "original", "translated_name": " "}], {}),
    ([{"name": "original", "translated_name": {"unexpected": "value"}}], {}),
    ([{"name": "original", "translated_name": "original", "translation": {"en": "original"}}], {}),
    ([{"name": "original", "translated_name": "original"}], {"tagTranslation": {"original": {"en": "original"}}}),
    ({"tags": [{"tag": "original", "translation": {"en": "original", "zh_tw": "原創"}}]}, {}),
])
def test_no_chinese_evidence_means_no_translation(archive, tags, extra):
    _, handler, library = archive
    work_id = handler._save_remote_work(1, PixivClient._parse_illust(raw_work(tags, **extra)))
    detail = library.detail(work_id)["tag_details"][0]
    assert detail["translated_name"] is None
    assert detail["language"] is None and detail["source"] is None


def test_never_guesses_language_from_characters_or_translates_itself():
    details = parse_tag_details(raw_work([{"name": "Brand", "translated_name": "Brand"}]))
    assert details[0]["translated_name"] == "Brand"
    assert details[0]["language_basis"] == "requested_locale"
    assert parse_tag_details(raw_work([{"name": "tag", "translated_name": "English"}]),
                             requested_locale="en-us")[0]["translated_name"] is None


@pytest.mark.parametrize("replacement", [
    {"name": "オリジナル"},
    {"name": "オリジナル", "translated_name": None},
    {"name": "オリジナル", "translated_name": "original", "translation": {"en": "original"}},
])
def test_missing_translation_preserves_previous_chinese_and_revision(archive, replacement):
    db, handler, library = archive
    work_id = handler._save_remote_work(1, PixivClient._parse_illust(raw_work([
        {"name": "オリジナル", "translated_name": "原创"}])))
    previous = library.detail(work_id)["tag_details"]
    before = revision(db)
    handler._save_remote_work(1, PixivClient._parse_illust(raw_work([replacement])))
    assert library.detail(work_id)["tag_details"] == previous
    assert revision(db) == before


@pytest.mark.parametrize("failure_code,status", [("work_unavailable", 404), ("api_unavailable", 503)])
def test_failed_refresh_preserves_translation(archive, monkeypatch, failure_code, status):
    _, handler, library = archive
    work_id = handler._save_remote_work(1, PixivClient._parse_illust(raw_work([
        {"name": "オリジナル", "translated_name": "原创"}])))
    previous = library.detail(work_id)["tag_details"]

    def failed(_):
        raise PixivRequestError(status, "Synthetic failure", failure_code=failure_code)

    monkeypatch.setattr(handler.client, "get_illust_detail", failed)
    with pytest.raises(PixivRequestError):
        handler._refresh_work(1, "123", work_id)
    assert library.detail(work_id)["tag_details"] == previous


def test_translation_change_updates_revision_once(archive):
    db, handler, library = archive
    work_id = handler._save_remote_work(1, PixivClient._parse_illust(raw_work([
        {"name": "オリジナル", "translated_name": "原创"}])))
    before = revision(db)
    updated = PixivClient._parse_illust(raw_work([
        {"name": "オリジナル", "translation": {"zh": "原创作品"}}]))
    handler._save_remote_work(1, updated)
    assert revision(db) == before + 1
    handler._save_remote_work(1, updated)
    assert revision(db) == before + 1
    assert Library(db).detail(work_id)["tag_details"][0]["translated_name"] == "原创作品"
    reopened = ArchiveDatabase(db.path)
    try:
        assert Library(reopened).detail(work_id)["tag_details"][0]["translated_name"] == "原创作品"
    finally:
        reopened.close()


def test_legacy_without_translation_remains_readable_until_explicit_refresh(archive, monkeypatch):
    db, handler, library = archive
    parsed = PixivClient._parse_illust(raw_work([{"name": "オリジナル"}]))
    parsed.pop("tag_details")
    work_id = handler._save_remote_work(1, parsed)
    assert library.detail(work_id)["tags"] == ["オリジナル"]
    assert library.detail(work_id)["tag_details"][0]["translated_name"] is None
    calls = []

    def refreshed(remote_id):
        calls.append(remote_id)
        return PixivClient._parse_illust(raw_work([{"name": "オリジナル", "translated_name": "原创"}]))

    monkeypatch.setattr(handler.client, "get_illust_detail", refreshed)
    assert calls == []
    handler._refresh_work(1, "123", work_id)
    assert calls == ["123"]
    assert library.detail(work_id)["tag_details"][0]["translated_name"] == "原创"


def test_selected_metadata_task_refreshes_only_selected_work_without_downloads(archive, monkeypatch):
    db, handler, library = archive
    first = handler._save_remote_work(1, PixivClient._parse_illust(raw_work([{"name": "オリジナル"}])))
    other = raw_work([{"name": "オリジナル"}])
    other["id"] = 124
    second = handler._save_remote_work(1, PixivClient._parse_illust(other))
    calls = []

    def refreshed(remote_id):
        calls.append(remote_id)
        return PixivClient._parse_illust(raw_work([{"name": "オリジナル", "translated_name": "原创"}]))

    def unexpected(*_args, **_kwargs):
        pytest.fail("Selected metadata must not download assets or refresh the artist")

    monkeypatch.setattr(handler.client, "get_illust_detail", refreshed)
    monkeypatch.setattr(handler.client, "get_artist_detail", unexpected)
    monkeypatch.setattr(handler.downloader, "download", unexpected)
    queue = TaskQueue(db, {"metadata": handler})
    app = FastAPI()
    app.include_router(create_router(lambda: SimpleNamespace(queue=queue)))
    with TestClient(app) as http:
        response = http.post("/api/tasks", json={"kind": "metadata", "work_ids": [first]})
        assert response.status_code == 200
        operation_id = response.json()["operation_id"]
    assert queue.run_round()
    details = queue.details(operation_id)
    assert details["state"] == "finished" and details["result"]["outcome"] == "succeeded"
    assert len(details["items"]) == 1 and details["items"][0]["key"] == f"metadata-work-{first}"
    assert calls == ["123"]
    assert library.detail(first)["tag_details"][0]["translated_name"] == "原创"
    assert library.detail(second)["tag_details"][0]["translated_name"] is None
    assert queue.snapshot()["next_round_count"] == 0


def test_empty_selected_metadata_does_not_fall_back_to_all_works(archive):
    _, handler, _ = archive
    handler._save_remote_work(1, PixivClient._parse_illust(raw_work([{"name": "tag"}])))
    assert handler.plan({"work_ids": []}, None) == []
