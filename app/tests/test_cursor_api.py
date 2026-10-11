"""Synthetic HTTP cursor navigation; no real media or remote calls."""
from datetime import datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.models import Artist, Work, WorkRevision
from src.core.runtime import Runtime
from src.routes.archive_api import create_router

@pytest.fixture
def cursor_api(tmp_path):
    runtime = Runtime(tmp_path / 'cursor.db', tmp_path / 'images', create=True)
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='synthetic-artist', name='Synthetic'))
        session.flush()
        for work_id, pages in ((1, 2), (2, 2000), (3, 3)):
            session.add(Work(id=work_id, pixiv_illust_id=str(work_id), artist_id=1,
                title=f'Synthetic {work_id}', page_count=pages, posted_at=datetime(2026, 1, work_id)))
    app = FastAPI(); app.include_router(create_router(lambda: runtime))
    with TestClient(app) as client:
        yield client, runtime
    assert runtime.close()

def read(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()

def test_cursor_card_and_numeric_jump_contract(cursor_api):
    client, _ = cursor_api
    first = read(client, '/api/library/cursor', limit=2, compact=1)
    assert [i['id'] for i in first['items']] == [3, 2]
    assert first['total'] == 3 and first['performance'] == 'indexed_seek'
    assert first['boundaries'] == [i['cursor'] for i in first['items']]
    tail = read(client, '/api/library/cursor', cursor=first['next_cursor'], limit=2)
    assert [i['id'] for i in tail['items']] == [1]
    back = read(client, '/api/library/cursor', cursor=tail['boundaries'][0], direction='previous', limit=2)
    assert [i['id'] for i in back['items']] == [3, 2]
    jumped = read(client, '/api/library', offset=2, limit=1)
    assert jumped['total'] == 3 and jumped['items'][0]['id'] == 1
    assert jumped['items'][0]['cursor'] and jumped['performance'] == 'offset_scan'

def test_sequential_feed_and_unvisited_long_middle(cursor_api):
    client, _ = cursor_api
    batch = read(client, '/api/library/cursor/feed', limit=4)
    assert [(p['work_id'], p['page'], p['index']) for p in batch['items']] == [(3,0,0),(3,1,1),(3,2,2),(2,0,3)]
    anchor = batch['items'][-1]
    assert anchor['next_work_index'] == 2003 and anchor['previous_work_index'] == 0
    tail = read(client, '/api/library/cursor/feed', cursor=anchor['work_cursor'], anchor_work_id=2,
        page=2000, index=2003, work_index=1, limit=2, ceiling=batch['ceiling'], as_of=batch['as_of'])
    assert [(p['work_id'],p['page'],p['index'],p['work_index']) for p in tail['items']] == [(1,0,2003,2),(1,1,2004,2)]
    assert tail['total'] == 2005 and not tail['has_more']
    assert all(not page['has_next_work'] for page in tail['items'])
    back = read(client, '/api/library/cursor/feed', cursor=tail['items'][0]['work_cursor'], anchor_work_id=1,
        page=-1, index=2002, work_index=2, direction='previous', limit=2, ceiling=batch['ceiling'], as_of=batch['as_of'])
    assert [(p['work_id'],p['page'],p['index'],p['work_index']) for p in back['items']] == [(2,1998,2001,1),(2,1999,2002,1)]
    assert back['has_previous']

def test_expired_generation_and_explicit_recovery(cursor_api):
    client, runtime = cursor_api
    first = read(client, '/api/library/cursor/feed', limit=1)
    token = first['items'][0]['work_cursor']
    with runtime.database.write() as session:
        session.get(Work, 3).title = 'Changed synthetic title'
    expired = client.get('/api/library/cursor/feed', params={'cursor':token,'anchor_work_id':3,'page':1,'index':1})
    assert expired.status_code == 409 and expired.json()['detail']['code'] == 'cursor_expired'
    recovered = read(client, '/api/library/cursor/feed', anchor_work_id=3, page=1, index=1, limit=1)
    assert recovered['generation'] > first['generation'] and recovered['items'][0]['page'] == 1
    assert client.get('/api/library/cursor', params={'cursor':'bad-token'}).status_code == 422
    cards = read(client, '/api/library/cursor', limit=1)
    assert client.get('/api/library/cursor', params={'cursor':cards['boundaries'][0],'anchor_work_id':2}).status_code == 422

def test_neighbors_use_identity_without_full_rank(cursor_api):
    client, _ = cursor_api
    middle = read(client, '/api/works/2/cursor-neighbors')
    assert middle['index'] is None and middle['anchor_cursor']
    assert middle['previous']['work_id'] == 3 and middle['next']['work_id'] == 1
    assert middle['next']['page_count'] == 2

def test_arbitrary_exact_counts_and_frozen_random_remain(cursor_api):
    client, _ = cursor_api
    searched = read(client, '/api/library/cursor', q='Synthetic', limit=1)
    assert searched['total'] is None and searched['count_mode'] == 'on_demand'
    assert read(client, '/api/library/counts', q='Synthetic')['total'] == 3
    response = client.post('/api/reading-sessions'); assert response.status_code == 200
    visit = response.json(); assert visit['total'] == 3 and visit['page_total'] == 2005
    assert len(read(client, f"/api/reading-sessions/{visit['session_id']}/feed", mode='random', limit=3)['items']) == 3
    assert client.delete(f"/api/reading-sessions/{visit['session_id']}").status_code == 200

def test_history_remains_accessible_in_bounded_pages(cursor_api):
    client, runtime = cursor_api
    with runtime.database.write() as session:
        for number in range(1, 106):
            session.add(WorkRevision(id=f'revision-{number}', work_id=1, ordinal=number,
                type='illust', page_count=2, title=f'Revision {number}', state='complete', source_signature=f'synthetic-{number}'))
    detail = read(client, '/api/works/1')
    assert len(detail['revisions']) <= 100 and detail['revisions_has_more']
    older = read(client, '/api/works/1/revisions', before_ordinal=detail['revisions_before'], limit=100)
    assert [i['ordinal'] for i in older['items']] == [5,4,3,2,1] and not older['has_more']


def test_recent_cursor_reuses_reference_without_explicit_as_of(cursor_api):
    client, runtime = cursor_api
    from src.core.models import utcnow
    with runtime.database.write() as session:
        for work_id in (1, 2, 3):
            session.get(Work, work_id).downloaded_at = utcnow()
    first = read(client, '/api/library/cursor', recent=1, limit=1)
    second = read(client, '/api/library/cursor', recent=1, cursor=first['next_cursor'], limit=1)
    assert second['as_of'] == first['as_of'] and second['items'][0]['id'] == 2
    near = read(client, '/api/works/3/cursor-neighbors', recent=1, cursor=first['boundaries'][0])
    assert near['next']['work_id'] == 2
    pages = read(client, '/api/library/cursor/feed', recent=1, cursor=first['boundaries'][0], anchor_work_id=3, limit=1)
    assert pages['as_of'] == first['as_of']
