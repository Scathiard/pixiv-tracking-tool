"""Explicit database lifetime; no configuration import or implicit legacy migration."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local
from time import monotonic

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
        self._wal_owner = None
        self._writer_autocheckpoint = 1000
        self.writer_engine = self._engine(self.path, writer=True,
            writer_policy=lambda: self._writer_autocheckpoint)
        self.writer_session = sessionmaker(self.writer_engine, expire_on_commit=False)
        self._closed = False

    @staticmethod
    def _engine(path: Path, *, writer=False, writer_policy=None):
        engine = create_engine(
            URL.create("sqlite", database=str(path)),
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_size=1 if writer else 7, max_overflow=0, pool_timeout=.1,
        )

        @event.listens_for(engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if writer:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(f"PRAGMA wal_autocheckpoint={writer_policy() if writer_policy else 1000}")

        if not writer:
            @event.listens_for(engine, "before_cursor_execute")
            def begin_statement(connection, cursor, statement, parameters, context, executemany):
                budget = current_read.get()
                if budget is not None:
                    context._read_timing = (budget, monotonic())
                    budget.statement_count += 1

            def finish_statement(context):
                timing = getattr(context, "_read_timing", None)
                if timing is not None:
                    budget, began = timing
                    budget.sql_seconds += monotonic() - began
                    context._read_timing = None

            @event.listens_for(engine, "after_cursor_execute")
            def end_statement(connection, cursor, statement, parameters, context, executemany):
                finish_statement(context)

            @event.listens_for(engine, "handle_error")
            def failed_statement(context):
                finish_statement(context.execution_context)

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
        database_started = None
        try:
            budget.checkpoint()
            if self._closed:
                raise RuntimeError("数据库已关闭")
            pool_started = monotonic()
            try:
                connection = self.engine.connect()
            finally:
                budget.pool_seconds += monotonic() - pool_started
            database_started = monotonic()
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
            if database_started is not None:
                budget.database_seconds += monotonic() - database_started
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
                # Even an empty successful transaction may conservatively
                # fence snapshots; detecting DML must not borrow/read SQL here.
                self.reads.advance_generation()
                if self._wal_owner is not None:
                    self._wal_owner.notify_commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                self._writer_state.active = False

    def enable_wal_maintenance(self, owner):
        """Runtime opts in only after the dedicated connection is ready."""
        if not owner.alive() or not owner._ready.is_set() or owner._startup_error is not None:
            raise RuntimeError("数据库整理执行者尚未就绪")
        with self._write_lock:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            if self._wal_owner is not None and self._wal_owner is not owner:
                raise RuntimeError("数据库已有整理执行者")
            try:
                self._writer_autocheckpoint = 0
                with self.writer_engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
                    if connection.scalar(text("PRAGMA wal_autocheckpoint")) != 0:
                        raise RuntimeError("业务连接未关闭自动整理")
                self._wal_owner = owner
            except BaseException:
                self._writer_autocheckpoint = 1000
                with self.writer_engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA wal_autocheckpoint=1000")
                raise

    def background_pressured(self):
        return self._wal_owner is not None and self._wal_owner.pressured()

    def background_can_advance(self):
        return not self.background_pressured() or (self._wal_owner is not None and self._wal_owner.can_probe())

    def begin_background_step(self):
        if not getattr(self._writer_state, "active", False):
            raise RuntimeError("后台推进许可必须在已有写事务中核对")
        return self._wal_owner.begin_step() if self._wal_owner is not None else (not self.background_pressured(), None)

    def finish_background_step(self, token):
        if token is not None and self._wal_owner is not None:
            self._wal_owner.finish_step(token)

    def close(self):
        if self._wal_owner is not None and self._wal_owner.alive():
            raise RuntimeError("数据库整理连接仍在活动，不能关闭数据库")
        self._closed = True
        self.engine.dispose()
        self.writer_engine.dispose()
