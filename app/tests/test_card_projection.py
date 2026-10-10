"""Compact cards preserve pinned metadata without loading discarded detail fields."""
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.library import Library
from src.core.media import CARD_MOTION_SPEC
from src.core.models import (
    Artist, Base, Derivative, RevisionDerivative, Work, WorkRevision, WorkTag,
)


@pytest.fixture
def library():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic artist"))
        for ident in (1, 2, 3):
            session.add(Work(id=ident, pixiv_illust_id=str(ident), artist_id=1,
                title="latest pending", type="manga", page_count=5,
                caption="<script>bad()</script><b>Legacy detail</b>",
                posted_at=datetime(2026, 10, ident),
                default_revision_id=f"saved-{ident}" if ident != 3 else None,
                latest_revision_id=f"pending-{ident}" if ident != 3 else None,
                source_metadata={"large_unused": "x" * 4096,
                    "source_updates": {"metadata": {"reason": "synthetic update"}},
                    "remote_availability": {"state": "private", "confirmed": True,
                        "policy": "source-v2", "account_id": "synthetic-account"}}))
            session.add(WorkTag(work_id=ident, tag="legacy tag"))
            if ident == 3:
                continue
            for ordinal, state in ((1, "saved"), (2, "pending")):
                session.add(WorkRevision(id=f"{state}-{ident}", work_id=ident,
                    ordinal=ordinal, title=f"{state} {ident}",
                    type="ugoira" if ident == 2 and state == "saved" else "illust",
                    page_count=1 if state == "saved" else 5,
                    caption="<script>bad()</script><b>Saved detail</b>" + "x" * 4096,
                    caption_state="available", tags=["saved tag"],
                    source_metadata={"large_unused": "x" * 4096,
                        "tag_translations": {"saved tag": {"language": "zh-cn",
                            "source": "pixiv", "translated_name": "保存标签"}}},
                    source_signature=f"signature-{state}-{ident}"))
        for kind, spec, digest, source in (("gif", "v1", "a" * 64, None),
                ("thumbnail", CARD_MOTION_SPEC, "b" * 64, "a" * 64)):
            ident = kind + spec
            session.add(Derivative(id=ident, work_id=2, revision_id="saved-2",
                kind=kind, spec_key=spec, page_index=0, state="verified",
                relative_path=f"synthetic/{ident}.webp", sha256=digest, source_version=source))
            session.add(RevisionDerivative(revision_id="saved-2", derivative_id=ident,
                kind=kind, spec_key=spec, page_index=0))
    class MemoryDatabase:
        @contextmanager
        def read(self):
            with Session(engine) as session:
                session.connection().exec_driver_sql("BEGIN")
                yield session
    value = Library(MemoryDatabase())
    value.account_id = "synthetic-account"
    value.test_engine = engine
    try:
        yield value
    finally:
        engine.dispose()


def test_compact_cards_select_only_pinned_card_fields_without_deferred_queries(library):
    statements = []
    def capture(conn, cursor, sql, params, context, many):
        statements.append(sql)
    event.listen(library.test_engine, "before_cursor_execute", capture)
    try:
        cards = {item["id"]: item for item in library.query(ceiling=3, compact=True)["items"]}
    finally:
        event.remove(library.test_engine, "before_cursor_execute", capture)
    revision_queries = [sql for sql in statements if "\nFROM work_revisions " in sql]
    assert len(revision_queries) == 1, revision_queries
    columns = revision_queries[0].split("\nFROM ", 1)[0]
    assert all(f"work_revisions.{name}" not in columns
        for name in ("caption", "caption_state", "tags", "source_metadata")), columns
    assert not any("\nFROM work_tags " in sql for sql in statements)
    assert not any(sql.startswith("SELECT works.caption") for sql in statements)
    assert (cards[1]["title"], cards[1]["type"], cards[1]["page_count"]) == ("saved 1", "illust", 1)
    assert (cards[2]["title"], cards[2]["type"], cards[2]["page_count"]) == ("saved 2", "ugoira", 1)
    assert cards[2]["preview"].endswith("?v=" + "b" * 16)
    assert cards[3]["title"] == "latest pending"
    assert cards[1]["availability"]["effective"] is True
    assert cards[1]["source_update"] == {"pending": True, "reason": "synthetic update"}
    assert cards[1]["revision_id"] == "saved-1"
    assert cards[1]["latest_revision_id"] == "pending-1"
    assert all(name not in item for item in cards.values()
        for name in ("caption", "caption_state", "tags", "tag_details"))


def test_compact_skips_detail_processing_and_detail_still_sanitizes(library, monkeypatch):
    import src.security as security
    import src.core.tag_translations as translations
    def discarded(*args, **kwargs):
        raise AssertionError("compact card processed discarded detail data")
    with monkeypatch.context() as scoped:
        scoped.setattr(security, "sanitize_caption", discarded)
        scoped.setattr(translations, "tag_details", discarded)
        assert len(library.query(ceiling=3, compact=True)["items"]) == 3
    detail = library.detail(1)
    assert "<script>" not in detail["caption"]
    assert "Saved detail" in detail["caption"]
    assert detail["tags"] == ["saved tag"]
    assert detail["tag_details"][0]["translated_name"] == "保存标签"
