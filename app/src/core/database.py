"""Explicit database lifetime; no configuration import or implicit legacy migration."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, String, URL, create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base

SCHEMA_REVISION = "0003_revisions"


class UpgradeRequired(RuntimeError):
    pass


class ArchiveDatabase:
    """One database owner with short serialized write transactions.

    Creating a fresh database is explicit. Opening an existing file only accepts
    our known schema, and never edits a legacy database to make it fit.
    """

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self._write_lock = RLock()
        self.engine = self._engine(self.path)
        try:
            with self.engine.connect() as connection:
                if "alembic_version" not in inspect(connection).get_table_names():
                    raise UpgradeRequired("此数据库需要先完成显式升级，原库未转换")
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != SCHEMA_REVISION:
                    raise UpgradeRequired(f"不支持的数据库结构版本：{revision}")
                schema = inspect(connection)
                for table in Base.metadata.sorted_tables:
                    if not schema.has_table(table.name):
                        raise UpgradeRequired("数据库结构不完整，未自动修补")
                    actual = {column["name"] for column in schema.get_columns(table.name)}
                    if not {column.name for column in table.columns}.issubset(actual):
                        raise UpgradeRequired("数据库字段与结构版本不符，未自动改表")
        except Exception:
            self.engine.dispose()
            raise
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        self._closed = False

    @staticmethod
    def _engine(path: Path):
        engine = create_engine(
            URL.create("sqlite", database=str(path)),
            connect_args={"check_same_thread": False, "timeout": 5},
        )

        @event.listens_for(engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")

        return engine

    @classmethod
    def create(cls, path: Path) -> ArchiveDatabase:
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Claim the candidate path exclusively, never replace a user's database.
        with path.open("xb"):
            pass
        engine = cls._engine(path)
        try:
            with engine.begin() as connection:
                # sqlite's legacy transaction mode does not begin on DDL. A
                # failed initializer must roll back every table and index too.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                operations = Operations(MigrationContext.configure(connection))
                Base.metadata.create_all(connection)
                from .task_indexes import create_task_indexes
                create_task_indexes(connection)
                operations.create_table(
                    "alembic_version",
                    Column("version_num", String(32), primary_key=True),
                )
                connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
                    {"revision": SCHEMA_REVISION},
                )
            # WAL is enabled only for databases we explicitly created/activated.
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        finally:
            engine.dispose()
        return cls(path)

    @contextmanager
    def read(self):
        if self._closed:
            raise RuntimeError("数据库已关闭")
        with self.session() as session:
            # sqlite's legacy transaction mode does not begin a snapshot on SELECT.
            session.connection().exec_driver_sql("BEGIN")
            yield session

    @contextmanager
    def write(self):
        with self._write_lock, self.session() as session:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def close(self):
        self._closed = True
        self.engine.dispose()
