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
from . import diagnostics
from .read_control import LocalReadBusy, ReadBudget, ReadController, current_read

SCHEMA_REVISION = "0005_read_cards_tasks"


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
            with diagnostics.span("db.open.validate"), self.engine.connect() as connection:
                if "alembic_version" not in inspect(connection).get_table_names():
                    raise UpgradeRequired("此数据库需要先完成显式升级，原库未转换")
                revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                if len(revisions) != 1:
                    raise UpgradeRequired("数据库结构版本记录不是唯一一行，未自动修补")
                revision = revisions[0]
                if revision != SCHEMA_REVISION:
                    raise UpgradeRequired(f"不支持的数据库结构版本：{revision}")
                schema = inspect(connection)
                for table in Base.metadata.sorted_tables:
                    if not schema.has_table(table.name):
                        raise UpgradeRequired("数据库结构不完整，未自动修补")
                    actual = {column["name"] for column in schema.get_columns(table.name)}
                    if not {column.name for column in table.columns}.issubset(actual):
                        raise UpgradeRequired("数据库字段与结构版本不符，未自动改表")
                required_projection_tables = {"read_model_meta", "read_model_dirty", "read_resource_totals",
                    "read_resource_facts", "read_work_order", "read_common_counts", "read_task_candidates",
                    "read_operation_scope", "read_work_state", "read_revision_state", "read_gallery_cards",
                    "read_task_parent", "read_task_item_fact", "read_task_work_parent", "read_task_scope_best"}
                if not required_projection_tables.issubset(schema.get_table_names()):
                    raise UpgradeRequired("读取投影结构不完整，需要显式修复，未自动补建")
                capture_tables = {"works", "work_revisions", "assets", "derivatives", "revision_assets",
                    "revision_derivatives", "local_preferences", "remote_bookmarks", "archive_intents",
                    "operations", "operation_items", "application_state", "work_tags"}
                expected_triggers = {f"read_capture_{table}_{action}" for table in capture_tables
                    for action in ("insert", "update", "delete")}
                actual_triggers = set(connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger'").scalars())
                if not expected_triggers.issubset(actual_triggers):
                    raise UpgradeRequired("读取投影变更捕获不完整，未自动修补")
                from .projection_maintenance import validate_projection_schema
                from .read_projection import ProjectionUnavailable
                try:
                    validate_projection_schema(connection)
                except ProjectionUnavailable as exc:
                    raise UpgradeRequired("读取投影结构与版本不符，需要显式修复，未自动改表") from exc
                self._schema_version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
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

        @event.listens_for(engine, "before_cursor_execute")
        def begin_statement(connection, cursor, statement, parameters, context, executemany):
            if diagnostics.enabled():
                context._diagnostic_sql = diagnostics.sql_start(connection, statement, executemany=executemany)
            if not writer:
                budget = current_read.get()
                if budget is not None:
                    context._read_timing = (budget, monotonic())
                    budget.statement_count += 1

        def finish_statement(context, error=None, rowcount=None):
            if context is None:
                return
            if not writer:
                timing = getattr(context, "_read_timing", None)
                if timing is not None:
                    budget, began = timing
                    budget.sql_seconds += monotonic() - began
                    context._read_timing = None
            token = getattr(context, "_diagnostic_sql", None)
            if token is not None:
                context._diagnostic_sql = None
                diagnostics.sql_finish(token, error=error, rowcount=rowcount)

        @event.listens_for(engine, "after_cursor_execute")
        def end_statement(connection, cursor, statement, parameters, context, executemany):
            finish_statement(context, rowcount=cursor.rowcount)

        @event.listens_for(engine, "handle_error")
        def failed_statement(context):
            finish_statement(context.execution_context, error=context.original_exception)

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
                from .projection_maintenance import install_projections
                from .read_projection import finish_verified_batch
                install_projections(connection)
                finish_verified_batch(connection)
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

    def refresh_schema_contract_after_index_preparation(self):
        """Explicit sanctioned startup DDL completion, never an automatic repair."""
        from .projection_maintenance import validate_projection_schema
        from .read_projection import require_ready
        with self._write_lock, self.engine.connect() as connection:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            validate_projection_schema(connection)
            require_ready(connection)
            self._schema_version = connection.exec_driver_sql("PRAGMA schema_version").scalar()

    def _check_schema(self, connection):
        # A connection can outlive external DDL. Detect a removed/modified
        # capture trigger before admitting a clean-looking stale projection.
        if connection.exec_driver_sql("PRAGMA schema_version").scalar() != self._schema_version:
            raise UpgradeRequired("数据库结构在打开后已变化，需要重新核验，未自动修补")

    @contextmanager
    def read(self):
        with diagnostics.span("db.read.transaction"):
            if self._closed:
                raise RuntimeError("数据库已关闭")
            budget = current_read.get()
            owned = budget is None
            token = None
            if owned:
                budget = ReadBudget()
                with diagnostics.span("db.read.admission", lane=budget.lane):
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
                    with diagnostics.span("db.read.connection"):
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
                    with diagnostics.span("db.read.begin"):
                        session.connection().exec_driver_sql("BEGIN")
                    from .read_projection import require_ready
                    self._check_schema(session.connection())
                    session.info['read_projection_generation'] = require_ready(session.connection())
                    try:
                        with diagnostics.span("db.read.body"):
                            yield session
                            budget.checkpoint()
                    finally:
                        # A Session may be retained/reused by a caller after this
                        # context; never carry readiness to a different snapshot.
                        session.info.pop('read_projection_generation', None)
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
                        with diagnostics.span("db.read.rollback"):
                            connection.rollback()
                    finally:
                        if driver is not None:
                            budget.detach(driver)
                        with diagnostics.span("db.read.close"):
                            connection.close()
                if database_started is not None:
                    budget.database_seconds += monotonic() - database_started
                if owned:
                    current_read.reset(token)
                    self.reads.release(budget.lane)

    @contextmanager
    def _write_gate(self):
        with diagnostics.span("db.write.lock_wait"):
            self._write_lock.acquire()
        try:
            yield
        finally:
            self._write_lock.release()

    @contextmanager
    def write(self):
        if getattr(self._writer_state, "active", False):
            raise RuntimeError("嵌套写事务必须传递已有会话")
        with diagnostics.span("db.write.transaction"), self._write_gate(), self.writer_session() as session:
            if self._closed:
                raise RuntimeError("数据库已关闭")
            try:
                self._writer_state.active = True
                with diagnostics.span("db.write.connection"):
                    connection = session.connection()
                with diagnostics.span("db.write.begin"):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                from .read_projection import require_ready
                from .projection_maintenance import maintain_projections
                self._check_schema(session.connection())
                # Pre-existing dirty records came from an external writer. Do
                # not turn an ordinary transaction into an implicit repair.
                require_ready(session.connection())
                session.info['read_projection_writer'] = True
                with diagnostics.span("db.write.body"):
                    yield session
                with diagnostics.span("db.write.flush"):
                    session.flush()
                with diagnostics.span("db.write.projections"):
                    maintain_projections(session)
                with diagnostics.span("db.write.commit"):
                    session.commit()
                # Even an empty successful transaction may conservatively
                # fence snapshots; detecting DML must not borrow/read SQL here.
                self.reads.advance_generation()
                if self._wal_owner is not None:
                    self._wal_owner.notify_commit()
            except BaseException:
                with diagnostics.span("db.write.rollback"):
                    session.rollback()
                raise
            finally:
                session.info.pop('read_projection_writer', None)
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
        pressure = self.background_pressured()
        allowed = not pressure or (self._wal_owner is not None and self._wal_owner.can_probe())
        if pressure:
            diagnostics.emit("db.background.eligibility", pressure=pressure, allowed=allowed)
        return allowed

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
        with diagnostics.span("db.close.read_engine"):
            self.engine.dispose()
        with diagnostics.span("db.close.write_engine"):
            self.writer_engine.dispose()
