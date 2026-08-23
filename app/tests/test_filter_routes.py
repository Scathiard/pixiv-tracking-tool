from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import config
from src.models import Base, Illustration, IllustrationTag, TrackedArtist
from src.routes import works


@pytest.fixture
def route_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine)
    monkeypatch.setattr(works, "Session", test_session)
    monkeypatch.setattr(config, "PAGE_SIZE", 50)
    monkeypatch.setattr(config, "PAGINATION_MODE", "paginate")
    monkeypatch.setattr(config, "MAX_VISIBLE_ITEMS", 0)

    with test_session() as session:
        artist = TrackedArtist(
            pixiv_user_id="10",
            name="Artist",
            pixiv_account="artist",
            is_active=True,
        )
        session.add(artist)
        session.flush()
        now = datetime.utcnow()
        for index in range(55):
            illust = Illustration(
                pixiv_illust_id=str(1000 + index),
                artist_id=artist.id,
                title=f"Work {index}",
                type="manga" if index == 1 else "illust",
                posted_at=now - timedelta(minutes=index),
                is_hidden=index == 3,
                tags='["tag&x"]' if index != 2 else '["other"]',
            )
            session.add(illust)
            session.flush()
            session.add(IllustrationTag(
                illustration_id=illust.id,
                tag="other" if index == 2 else "tag&x",
            ))
        session.commit()
        artist_id = artist.id

    app = FastAPI()
    app.include_router(works.router)
    with TestClient(app) as client:
        yield client, artist_id


def test_explicit_empty_artist_filter_means_no_results(route_client):
    client, _ = route_client

    response = client.get("/?artist_ids=")

    assert response.status_code == 200
    assert "画师:0位" in response.text
    assert "Work 0" not in response.text


def test_detail_and_pagination_links_keep_encoded_filter_context(route_client):
    client, artist_id = route_client

    response = client.get(
        f"/?artist_ids={artist_id}&types=illust&tags=tag%26x"
        "&period=year&show_hidden=1&page=1"
    )

    assert response.status_code == 200
    assert "tags=tag%26x" in response.text
    assert "period=year" in response.text
    assert "show_hidden=1" in response.text
    assert "page=2" in response.text


def test_detail_navigation_applies_type_tag_and_hidden_filters(route_client):
    client, artist_id = route_client

    visible = client.get(
        f"/illust/1?artist_id={artist_id}&types=illust&tags=tag%26x"
    )
    with_hidden = client.get(
        f"/illust/1?artist_id={artist_id}&types=illust&tags=tag%26x&show_hidden=1"
    )

    assert visible.status_code == 200
    assert "1 / 52" in visible.text
    assert f"/artist/{artist_id}?type=illust" in visible.text
    assert "tags=tag%26x" in visible.text
    assert with_hidden.status_code == 200
    assert "1 / 53" in with_hidden.text
