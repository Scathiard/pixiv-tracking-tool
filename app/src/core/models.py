"""Versioned archive records, independent of application configuration and login."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artists"
    id: Mapped[int] = mapped_column(primary_key=True)
    pixiv_user_id: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    account: Mapped[str | None] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Historical locations are mappings, not names to rename when the artist changes name.
    directory: Mapped[str | None] = mapped_column(Text)
    policy: Mapped[TrackingPolicy | None] = relationship(cascade="all, delete-orphan")
    works: Mapped[list[Work]] = relationship(back_populates="artist")


class TrackingPolicy(Base):
    __tablename__ = "tracking_policies"
    __table_args__ = (CheckConstraint("mode IN ('active','paused','stopped')"),)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String, default="active")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (
        CheckConstraint("page_count >= 1"),
        Index("ix_works_order", "posted_at", "id"),
        Index("ix_works_artist_order", "artist_id", "posted_at", "id"),
        Index("ix_works_type_order", "type", "posted_at", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    pixiv_illust_id: Mapped[str] = mapped_column(String, unique=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    title: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String, default="illust")
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    caption: Mapped[str] = mapped_column(Text, default="")
    caption_state: Mapped[str] = mapped_column(String, default="unknown")
    bookmark_count: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(Integer)
    x_restrict: Mapped[int | None] = mapped_column(Integer)
    restriction_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    time_basis: Mapped[str] = mapped_column(String, default="utc")
    legacy_posted_at: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    artist: Mapped[Artist] = relationship(back_populates="works")
    tags: Mapped[list[WorkTag]] = relationship(cascade="all, delete-orphan")
    preference: Mapped[LocalPreference | None] = relationship(cascade="all, delete-orphan")
    bookmark: Mapped[RemoteBookmark | None] = relationship(cascade="all, delete-orphan")
    assets: Mapped[list[Asset]] = relationship(cascade="all, delete-orphan")
    derivatives: Mapped[list[Derivative]] = relationship(cascade="all, delete-orphan")


class WorkTag(Base):
    __tablename__ = "work_tags"
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True, index=True)


class LocalPreference(Base):
    __tablename__ = "local_preferences"
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), primary_key=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[int] = mapped_column(Integer, default=0)


class RemoteBookmark(Base):
    __tablename__ = "remote_bookmarks"
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class ArchiveIntent(Base):
    __tablename__ = "archive_intents"
    __table_args__ = (CheckConstraint("mode IN ('keep','suppressed')"),)
    pixiv_illust_id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, default="keep")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("work_id", "role", "page_index"),
        CheckConstraint("page_index >= 0"),
        CheckConstraint("role IN ('image','ugoira_zip','ugoira_timing')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    page_index: Mapped[int] = mapped_column(Integer, default=0)
    relative_path: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="unknown")
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    file_mtime_ns: Mapped[int | None] = mapped_column(Integer)
    file_identity: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Derivative(Base):
    __tablename__ = "derivatives"
    __table_args__ = (UniqueConstraint("work_id", "kind", "page_index", "spec_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    page_index: Mapped[int] = mapped_column(Integer, default=0)
    spec_key: Mapped[str] = mapped_column(String, default="v1")
    source_version: Mapped[str | None] = mapped_column(String)
    generator_version: Mapped[str] = mapped_column(String, default="v1")
    relative_path: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="missing")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String)
    file_mtime_ns: Mapped[int | None] = mapped_column(Integer)
    file_identity: Mapped[int | None] = mapped_column(Integer)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class TaskRound(Base):
    __tablename__ = "task_rounds"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    state: Mapped[str] = mapped_column(String, default="planning", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    round_id: Mapped[str | None] = mapped_column(ForeignKey("task_rounds.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String, default="manual")
    dedupe_key: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    phase: Mapped[str] = mapped_column(String, default="waiting")
    phase_unit: Mapped[str] = mapped_column(String, default="items")
    phase_total: Mapped[int | None] = mapped_column(Integer)
    phase_done: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    items: Mapped[list[OperationItem]] = relationship(cascade="all, delete-orphan")


class OperationItem(Base):
    __tablename__ = "operation_items"
    __table_args__ = (UniqueConstraint("operation_id", "item_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), index=True)
    item_key: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String)
    target: Mapped[dict] = mapped_column(JSON)
    phase: Mapped[str] = mapped_column(String)
    unit: Mapped[str] = mapped_column(String, default="items")
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class PublicationJournal(Base):
    __tablename__ = "publication_journal"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    operation_item_id: Mapped[str | None] = mapped_column(ForeignKey("operation_items.id", ondelete="SET NULL"))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    pixiv_illust_id: Mapped[str] = mapped_column(String, index=True)
    intent_revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String)
    staging_path: Mapped[str | None] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(String, default="prepared", index=True)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
