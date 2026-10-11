"""Small source-oracle and query-boundary contracts; synthetic data only."""
from datetime import datetime, timedelta

from sqlalchemy import event, select

from src.core.archive_state import raw_work_states, work_states
from src.core.database import ArchiveDatabase
from src.core.models import Artist, Work, Operation, OperationItem
from src.core.normalized_task_projection import winners, verify, winner_sql, backfill

STAMP = datetime(2026, 10, 12, 12)


def seed(database, size=3):
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='synthetic', name='synthetic'))
        session.flush()
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1, title='synthetic',
            type='illust', page_count=1, downloaded_at=STAMP) for i in range(1,size+1)])


def operation(identity, *, state='ready', scope=None, days=0, kind='verify'):
    return Operation(id=identity, kind=kind, state=state, dedupe_key=identity,
        label='synthetic', parameters={} if scope is None else {'work_ids':scope},
        created_at=STAMP+timedelta(days=days), updated_at=STAMP)


def item(identity, parent, state, position=0, target=None, kind="verify_asset"):
    return OperationItem(id=identity, operation_id=parent, item_key=identity,
        kind=kind, phase='verify', lane='local', state=state, position=position,
        target={} if target is None else target, result={'message':identity})


def assert_oracle(database):
    with database.read() as session:
        works=list(session.scalars(select(Work).order_by(Work.id)))
        actual,_=work_states(session,works)
        expected,_=raw_work_states(session,works)
        assert actual==expected
        verify(session.connection())


def test_shared_lifecycle_direct_scope_history_and_rollback(tmp_path):
    database=ArchiveDatabase.create(tmp_path/'synthetic.db')
    try:
        seed(database)
        with database.write() as session:
            session.add_all([operation('history',state='finished',days=-1),
                operation('current',scope=[1,2,3]),operation('empty',scope=3,days=-2)])
            session.flush()
            session.add_all([item('history-failed','history','failed',target={'work_id':1}),
                item('direct-pending','current','pending',target={'work_id':1}),
                item('scope-running','current','running',position=2,kind='gif'),
                item('scope-failed','current','failed',position=1)])
        assert_oracle(database)
        with database.read() as session:
            assert winners(session.connection(),[2])[2]['payload']['concern']=='work'
        with database.write() as session:
            before=winners(session.connection(),[1,2,3])
            backfill(session.connection())
            assert winners(session.connection(),[1,2,3])==before
        assert_oracle(database)
        for state in ('running','interrupted','finished','ready'):
            with database.write() as session:
                session.get(Operation,'current').state=state
                session.get(Operation,'current').updated_at+=timedelta(seconds=1)
            assert_oracle(database)
        try:
            with database.write() as session:
                session.get(Operation,'current').state='finished'
                session.flush()
                raise RuntimeError('synthetic rollback')
        except RuntimeError:
            pass
        assert_oracle(database)
        with database.write() as session:
            session.get(Operation,'current').parameters={'work_ids':[2]}
            session.get(OperationItem,'scope-running').state='finished'
            session.get(OperationItem,'direct-pending').state='failed'
        assert_oracle(database)
        with database.write() as session:
            session.delete(session.get(OperationItem,'direct-pending'))
            session.get(Operation,'current').kind='thumbnails'
        assert_oracle(database)
        with database.write() as session:
            session.delete(session.get(Operation,'current'))
        assert_oracle(database)
    finally:
        database.close()


def test_parent_only_write_does_not_read_items_or_rewrite_members(tmp_path):
    database=ArchiveDatabase.create(tmp_path/'synthetic.db')
    try:
        seed(database,240)
        with database.write() as session:
            session.add(operation('large',scope=list(range(1,241))))
            session.flush()
            session.add_all([item(f'item-{i:04}','large','pending',i,{'work_id':i%240+1}) for i in range(960)])
        statements=[]
        def capture(connection,cursor,statement,parameters,context,executemany):
            statements.append(statement.lower())
        event.listen(database.engine,'before_cursor_execute',capture)
        event.listen(database.writer_engine,'before_cursor_execute',capture)
        try:
            with database.write() as session:
                parent=session.get(Operation,'large')
                parent.state='running'
                parent.updated_at+=timedelta(seconds=1)
            assert not any(('select' in sql and ('operation_items' in sql or 'read_task_candidates' in sql
                or 'read_task_item_fact' in sql or 'read_task_work_parent' in sql or 'read_operation_scope' in sql)) for sql in statements)
            assert not any(('update read_work_state' in sql or 'insert into read_work_state' in sql) for sql in statements)
            statements.clear()
            with database.read() as session:
                result=winners(session.connection(),range(1,241))
                assert len(result)==240
            assert len([sql for sql in statements if sql.lstrip().startswith('with selected')])==1
        finally:
            event.remove(database.engine,'before_cursor_execute',capture)
            event.remove(database.writer_engine,'before_cursor_execute',capture)
        assert_oracle(database)
    finally:
        database.close()


def test_selected_history_skips_unrelated_parent_history(tmp_path):
    database=ArchiveDatabase.create(tmp_path/'synthetic.db')
    try:
        seed(database)
        with database.write() as session:
            session.add(operation('selected',state='finished'))
            session.flush()
            session.add(item('selected-failed','selected','failed',target={'work_id':1}))
        def measure():
            with database.read() as session:
                connection=session.connection()
                driver=connection.connection.driver_connection
                steps=[0]
                def progress():
                    steps[0]+=1
                    return 0
                driver.set_progress_handler(progress,1)
                try:
                    value=winners(connection,[1])
                finally:
                    driver.set_progress_handler(None,0)
                plans=[row[3] for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN '+winner_sql(1),(1,))]
                return value,steps[0],plans
        before,steps_before,_=measure()
        with database.write() as session:
            session.add_all([operation(f'unrelated-{i:04}',state='finished',days=i+1) for i in range(400)])
            session.flush()
            session.add_all([item(f'unrelated-item-{i:04}',f'unrelated-{i:04}','failed',target={'work_id':2}) for i in range(400)])
        after,steps_after,plans=measure()
        assert before==after
        print(f"unrelated_history_vm_steps: before={steps_before}, after={steps_after}")
        assert steps_after<=steps_before+50
        assert any('ix_read_task_parent_active' in plan for plan in plans)
        assert any('read_task_work_parent' in plan and 'work_id=?' in plan for plan in plans)
        assert not any('SCAN read_task_work_parent' in plan for plan in plans)
        assert_oracle(database)
    finally:
        database.close()
