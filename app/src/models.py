from datetime import datetime
from sqlalchemy import create_engine, event, Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from .config import DATABASE_URL, DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL, echo=False,
                       connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """WAL 模式 + 忙等超时，支持多线程并发写入。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_sessionmaker = None


def Session(**kw):
    """复用 sessionmaker，避免每次创建新实例。"""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(bind=engine)
    return _sessionmaker(**kw)


class Base(DeclarativeBase):
    pass


class TrackedArtist(Base):
    __tablename__ = "tracked_artist"

    id = Column(Integer, primary_key=True)
    pixiv_user_id = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    pixiv_account = Column(String)
    avatar_url = Column(String)
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_checked_at = Column(DateTime)

    illustrations = relationship("Illustration", back_populates="artist", cascade="all, delete-orphan")


class Illustration(Base):
    __tablename__ = "illustration"

    id = Column(Integer, primary_key=True)
    pixiv_illust_id = Column(String, unique=True, nullable=False, index=True)
    artist_id = Column(Integer, ForeignKey("tracked_artist.id"), nullable=False)
    title = Column(String, nullable=False)
    type = Column(String, default="illust")
    page_count = Column(Integer, default=1)
    tags = Column(Text)  # JSON array string
    bookmark_count = Column(Integer)
    view_count = Column(Integer)
    posted_at = Column(DateTime)
    downloaded_at = Column(DateTime, default=datetime.utcnow)
    file_paths = Column(Text)  # JSON array string
    is_hidden = Column(Boolean, default=False)
    is_bookmarked = Column(Boolean, default=False)
    rating = Column(Integer, default=0)
    caption = Column(Text, default="")
    x_restrict = Column(Integer, default=0)  # 0=全年龄, 1=R-18, 2=R-18G

    artist = relationship("TrackedArtist", back_populates="illustrations")
    tags_rel = relationship("IllustrationTag", cascade="all, delete-orphan")

    @property
    def file_path_list(self):
        from .file_index import decode_file_paths
        return decode_file_paths(self.file_paths)

    @property
    def primary_file_path(self):
        paths = self.file_path_list
        return paths[0] if paths else None


class IllustrationTag(Base):
    __tablename__ = "illustration_tag"

    illustration_id = Column(Integer, ForeignKey("illustration.id"), primary_key=True)
    tag = Column(String, primary_key=True, index=True)


def _table_exists(dbapi_connection, table):
    cursor = dbapi_connection.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    exists = cursor.fetchone() is not None
    cursor.close()
    return exists


def _column_exists(dbapi_connection, table, column):
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    exists = any(row[1] == column for row in cursor.fetchall())
    cursor.close()
    return exists


def _migrate_existing_db():
    """幂等添加 v0.0.4 之后引入的列和表。"""
    with engine.begin() as conn:
        dbapi_connection = conn.connection.driver_connection
        if not _column_exists(dbapi_connection, "illustration", "is_hidden"):
            conn.exec_driver_sql("ALTER TABLE illustration ADD COLUMN is_hidden BOOLEAN DEFAULT 0")
        if not _column_exists(dbapi_connection, "illustration", "is_bookmarked"):
            conn.exec_driver_sql("ALTER TABLE illustration ADD COLUMN is_bookmarked BOOLEAN DEFAULT 0")
        if not _column_exists(dbapi_connection, "illustration", "caption"):
            conn.exec_driver_sql("ALTER TABLE illustration ADD COLUMN caption TEXT DEFAULT ''")
        if not _column_exists(dbapi_connection, "illustration", "x_restrict"):
            conn.exec_driver_sql("ALTER TABLE illustration ADD COLUMN x_restrict INTEGER DEFAULT 0")

        # v0.0.6: illustration_tag 表
        if not _table_exists(dbapi_connection, "illustration_tag"):
            conn.exec_driver_sql("""
                CREATE TABLE illustration_tag (
                    illustration_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (illustration_id, tag),
                    FOREIGN KEY (illustration_id) REFERENCES illustration(id)
                )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_illustration_tag ON illustration_tag(tag)")

        # 幂等迁移 JSON → illustration_tag（INSERT OR IGNORE 跳过重复）
        import json
        rows = conn.exec_driver_sql(
            "SELECT id, tags FROM illustration WHERE tags IS NOT NULL AND tags != ''"
        ).all()
        inserts = []
        for row in rows:
            try:
                for tag in json.loads(row[1]):
                    inserts.append({"illustration_id": row[0], "tag": tag})
            except (json.JSONDecodeError, TypeError):
                pass
        if inserts:
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO illustration_tag (illustration_id, tag) VALUES (:illustration_id, :tag)",
                inserts
            )
        print(f"  [migrate] illustration_tag: {len(inserts)} 个 tag（来自 {len(rows)} 件作品）")
        from .routes.works import reset_tag_cache
        reset_tag_cache()


def init_db():
    Base.metadata.create_all(engine)
    _migrate_existing_db()


def reinit_db():
    """重新初始化数据库连接（数据目录迁移后调用）。"""
    global engine, _sessionmaker
    engine.dispose()
    _sessionmaker = None
    from .config import DATABASE_URL, DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DATABASE_URL, echo=False,
                           connect_args={"check_same_thread": False})
    event.listens_for(engine, "connect")(_set_sqlite_pragma)
    Base.metadata.create_all(engine)
    _migrate_existing_db()
