"""Full local filter catalogue and explicit empty selections; synthetic data only."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import ArchiveIntent, Artist, LocalPreference, RemoteBookmark, TrackingPolicy, Work, WorkTag, WorkRevision, utcnow
from src.routes.archive_api import create_router


def test_full_catalogue_and_empty_selection(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    with database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"画师 {i:03}") for i in range(1, 138)])
        session.add(TrackingPolicy(artist_id=137, mode="stopped"))
        for i in range(1, 302):
            session.add(Work(id=i, pixiv_illust_id=str(1000+i), artist_id=1 if i < 301 else 137, title=str(i)))
            session.add(WorkTag(work_id=i, tag=f"中文标签 {i:03}"))
            session.add(WorkTag(work_id=i, tag="重复标签"))
        session.add(LocalPreference(work_id=301, is_hidden=True))
        session.add(ArchiveIntent(pixiv_illust_id="1300", mode="suppressed"))
    app = FastAPI()
    app.include_router(create_router(lambda: SimpleNamespace(database=database, library=Library(database))))
    with TestClient(app) as client:
        artists = []
        for offset in (0, 100):
            artists.extend(client.get(f"/api/artists?offset={offset}&limit=100").json()["items"])
        assert len(artists) == 137
        assert any(a["id"] == 137 for a in artists)
        tags = client.get("/api/library/tags?show_hidden=1").json()["tags"]
        assert len(tags) == 301 and tags.count("重复标签") == 1
        assert "中文标签 301" in tags and "中文标签 300" not in tags
        assert client.get("/api/library?tags=").json()["total"] == 0
        assert client.get("/api/library?artist_ids=").json()["total"] == 0
        assert client.get("/api/library?types=").json()["total"] == 0
        assert client.get("/api/library?tags=中文标签 001,中文标签 002").json()["total"] == 2
        assert client.get("/api/library").json()["total"] == 299
    database.close()


def test_literal_comma_tag_repeated_queries_and_legacy_csv(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "literal-tags.db")
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="artist"))
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1, title=str(i)) for i in range(1, 5)])
        session.add_all([WorkTag(work_id=1, tag="逗号,标签"), WorkTag(work_id=2, tag="逗号"), WorkTag(work_id=3, tag="标签")])
    app = FastAPI()
    app.include_router(create_router(lambda: SimpleNamespace(database=database, library=Library(database))))
    with TestClient(app) as client:
        literal = [("tags", "逗号,标签"), ("tags", "逗号,标签")]
        page = client.get("/api/library", params=literal).json()
        assert [item["id"] for item in page["items"]] == [1]
        assert client.get("/api/library", params={"tags": "逗号,标签"}).json()["total"] == 2
        inverse = [("tags_exclude", "逗号,标签"), ("tags_exclude", "逗号,标签")]
        assert client.get("/api/library", params=inverse).json()["total"] == 2
        reading = literal + [("ceiling", str(page["ceiling"]))]
        assert client.get("/api/library/feed", params=reading).json()["total"] == 1
        assert client.get("/api/library/locate", params=reading + [("work_id", "1")]).json()["work_index"] == 0
        assert client.get("/api/works/1/neighbors", params=reading).json()["next"] is None
    database.close()


def test_inverse_any_tag_and_shared_reading_scope(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic-inverse.db")
    reference = utcnow()
    with database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"artist{i}") for i in (1, 2)])
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1 if i < 3 else 2,
            title=str(i), type="illust", posted_at=reference, downloaded_at=reference) for i in range(1, 5)])
        session.add_all([WorkTag(work_id=1, tag="A"), WorkTag(work_id=2, tag="A"), WorkTag(work_id=2, tag="B"), WorkTag(work_id=3, tag="B")])
        session.add(LocalPreference(work_id=3, is_hidden=True))
        session.add(RemoteBookmark(work_id=2, value=True, source="readback", confirmed_at=reference, version=1))
    app = FastAPI()
    app.include_router(create_router(lambda: SimpleNamespace(database=database, library=Library(database))))
    with TestClient(app) as client:
        assert client.get("/api/library").json()["total"] == 3  # includes work 4 without tags
        assert client.get("/api/library?tags=A,B").json()["total"] == 2  # explicit all-known tags still excludes untagged work 4
        assert client.get("/api/library?tags=old").json()["total"] == 0
        assert client.get("/api/library?tags_exclude=A").json()["total"] == 1
        inverse = client.get("/api/library?tags_exclude=A&show_hidden=1").json()
        assert {item["id"] for item in inverse["items"]} == {2, 3}
        assert client.get("/api/library?artist_ids_exclude=1&show_hidden=1").json()["total"] == 2
        for query in ("tags=A&tags_exclude=B", "artist_ids=1&artist_ids_exclude=2"):
            assert client.get("/api/library?" + query).status_code == 422
        query = "tags_exclude=A&favorites=1&recent=1&types=illust&show_hidden=1&sort=oldest"
        selected = client.get("/api/library?" + query).json()
        assert [item["id"] for item in selected["items"]] == [2]
        ceiling = selected["ceiling"]
        assert client.get(f"/api/library/feed?{query}&ceiling={ceiling}").json()["total"] == 1
        assert client.get(f"/api/library/locate?{query}&ceiling={ceiling}&work_id=2").json()["work_index"] == 0
        assert client.get(f"/api/works/2/neighbors?{query}&ceiling={ceiling}").json()["next"] is None
        assert client.get(f"/api/library/locate?{query}&ceiling={ceiling}&work_id=1").status_code == 404
    database.close()


def test_compact_artist_choices_skip_profiles_previews_and_keep_mode_updates(tmp_path, monkeypatch):
    from sqlalchemy import event
    database = ArchiveDatabase.create(tmp_path / "compact-artists.db")
    library = Library(database)
    with database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"artist {i:03}",
                               biography="large unused profile" * 100) for i in range(1, 138)])
        session.add(TrackingPolicy(artist_id=137, mode="paused"))
    monkeypatch.setattr(library, "_artist_views", lambda *_: (_ for _ in ()).throw(AssertionError("previews read")))
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())
    event.listen(database.engine, "before_cursor_execute", capture)
    app = FastAPI()
    app.include_router(create_router(lambda: SimpleNamespace(database=database, library=library)))
    with TestClient(app) as client:
        first = client.get("/api/artists?compact=1&limit=100").json()
        last = client.get("/api/artists?compact=1&limit=100&offset=100").json()
        assert first["total"] == 137 and len(first["items"]) == 100 and len(last["items"]) == 37
        assert last["items"][-1] == {"id": 137, "pixiv_id": "137", "name": "artist 137", "mode": "paused"}
        assert not any("biography" in sql or "work_revisions" in sql or "read_gallery_cards" in sql for sql in statements)
        library.set_tracking(137, "stopped")
        assert client.get("/api/artists?compact=1&offset=136").json()["items"][0]["mode"] == "stopped"
    database.close()


def test_filter_translations_are_current_local_narrow_and_keep_original_tag_identity(tmp_path):
    from sqlalchemy import event
    database = ArchiveDatabase.create(tmp_path / "filter-translations.db")
    def metadata(name, checked, **extra):
        return {"urls": ["unused-large-manifest" * 1000], "tag_translations": {
            "tag": {"translated_name": name, "source": "pixiv", "language": "zh-cn",
                    "checked_at": checked, **extra}}}
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="artist"))
        for identity, data in enumerate([
                metadata("旧译名", "2026-10-20T10:00:00+08:00"),
                metadata("较新译名", "2026-10-20T03:00:00+00:00"),
                metadata("被删除作品译名", "2026-10-21T00:00:00+00:00"),
                metadata("非主站译名", "2026-10-22T00:00:00+00:00", source="other")], 1):
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=1,
                             title=str(identity), source_metadata=data))
            session.add(WorkTag(work_id=identity, tag="tag"))
        session.add(WorkTag(work_id=1, tag="no-translation"))
        session.add(WorkTag(work_id=3, tag="only-suppressed"))
        session.add(LocalPreference(work_id=2, is_hidden=True))
        session.add(ArchiveIntent(pixiv_illust_id="3", mode="suppressed"))
        session.add(WorkRevision(id="history", work_id=1, ordinal=1, title="history",
            type="illust", page_count=1, tags=["history-only"], source_signature="synthetic",
            source_metadata=metadata("历史译名", "2026-10-23T00:00:00+00:00")))
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())
    event.listen(database.engine, "before_cursor_execute", capture)
    app = FastAPI()
    library = Library(database)
    app.include_router(create_router(lambda: SimpleNamespace(database=database, library=library)))
    with TestClient(app) as client:
        result = client.get("/api/library/tags?show_hidden=1").json()
        assert result["tags"] == ["no-translation", "tag"]
        details = {value["name"]: value for value in result["tag_details"]}
        assert details["tag"]["translated_name"] == "较新译名"
        assert "no-translation" not in details
        assert not any("source_metadata" in sql or "work_revisions" in sql for sql in statements)
        assert not any("max(works.id)" in sql for sql in statements)
        assert client.get("/api/library/tags?tags=absent&show_hidden=1").json()["tags"] == []
        assert client.get("/api/library/tags?show_hidden=1&ceiling=1").json()["tags"] == ["no-translation", "tag"]
        with database.write() as session:
            session.get(Work, 2).source_metadata = metadata("更新后译名", "2026-10-24T00:00:00+00:00")
        updated = client.get("/api/library/tags?show_hidden=1").json()
        assert next(tag for tag in updated["tag_details"] if tag["name"] == "tag")["translated_name"] == "更新后译名"
    database.close()
