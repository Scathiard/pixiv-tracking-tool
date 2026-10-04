"""Explicit database lifetime; no configuration import or implicit legacy migration."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, String, URL, create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from .models import Base
from .read_control import LocalReadBusy, ReadBudget, ReadController, current_read

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
        self._writer_state = local()
        self.reads = ReadController()
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
        self.writer_engine = self._engine(self.path, writer=True)
        self.writer_session = sessionmaker(self.writer_engine, expire_on_commit=False)
        self._closed = False

    @staticmethod
    def _engine(path: Path, *, writer=False):
        engine = create_engine(
            URL.create("sqlite", database=str(path)),
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_size=1 if writer else 7, max_overflow=0, pool_timeout=.1,
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
        budget = current_read.get()
        owned = budget is None
        token = None
        if owned:
            budget = ReadBudget()
            self.reads.acquire_sync(budget)
            token = current_read.set(budget)
        connection = None
        driver = None
        try:
            budget.checkpoint()
            if self._closed:
                raise RuntimeError("数据库已关闭")
            connection = self.engine.connect()
            driver = connection.connection.driver_connection
            budget.attach(driver)
            # SQLite lock waiting does not call the progress handler. Keep each
            # lock wait finite as well; WAL readers normally do not wait at all.
            driver.execute("PRAGMA busy_timeout=100")
            with self.session(bind=connection) as session:
                session.connection().exec_driver_sql("BEGIN")
                yield session
                budget.checkpoint()
        except PoolTimeout as exc:
            budget.checkpoint()
            raise LocalReadBusy("本地读取繁忙，请稍后重试") from exc
        except OperationalError as exc:
            budget.checkpoint()
            if "locked" in str(exc.orig).lower() or "busy" in str(exc.orig).lower():
                raise LocalReadBusy("本地读取繁忙，请稍后重试") from exc
            raise
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    if driver is not None:
                        budget.detach(driver)
                    connection.close()
            if owned:
                current_read.reset(token)
                self.reads.release(budget.lane)

    @contextmanager
    def write(self):
        if getattr(self._writer_state, "active", False):
            raise RuntimeError("嵌套写事务必须传递已有会话")
        with self._write_lock, self.writer_session() as session:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            try:
                self._writer_state.active = True
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                self._writer_state.active = False

    def close(self):
        self._closed = True
        self.engine.dispose()
        self.writer_engine.dispose()
