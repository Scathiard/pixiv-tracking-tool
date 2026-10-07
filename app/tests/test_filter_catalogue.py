"""Full local filter catalogue and explicit empty selections; synthetic data only."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import ArchiveIntent, Artist, LocalPreference, RemoteBookmark, TrackingPolicy, Work, WorkTag, utcnow
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
