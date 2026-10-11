"""Bounded source/artist read projections: synthetic SQL/legacy-oracle contracts."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import and_, case, func, or_, select

from src.core.database import ArchiveDatabase
from src.core.library import Library, remote_availability
from src.core.models import (ApplicationState, ArchiveIntent, Artist, Asset, Derivative,
    LocalPreference, RevisionAsset, RevisionDerivative, Work, WorkRevision)
from src.core.read_projection import ProjectionUnavailable
from src.core.revision_views import media_maps, selected_snapshots
from src.core.source_read_projection import verify
from src.core.source_status import ARTIST_PREFIX


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / 'source-synthetic.db')
    yield db
    db.close()


def observation(**overrides):
    return {'state': 'unavailable', 'confirmed': True, 'policy': 'source-v2', **overrides}


def legacy_selection(library, kind, group):
    with library.database.read() as session:
        base, deleted, unavailable = library._source_statement(kind)
        count = session.execute(base.with_only_columns(func.sum(case((deleted, 1), else_=0)),
            func.sum(case((unavailable, 1), else_=0)))).one()
        selected = base.where(deleted if group == 'deleted' else unavailable if group == 'unavailable' else or_(deleted, unavailable))
        model = Work if kind == 'works' else Artist
        return [row.id for row in session.scalars(selected.order_by(model.id))], {'deleted': count[0] or 0, 'unavailable': count[1] or 0}


def legacy_local(session, work):
    # The original single-work detail oracle remains independent of the cache.
    snapshots = selected_snapshots(session, [work], {work.id: work.latest_revision_id} if work.latest_revision_id else {})
    assets, derivatives = media_maps(session, [work], snapshots)
    shape = snapshots.get(work.id, work)
    return {**Library._local_work(SimpleNamespace(type=shape.type, page_count=shape.page_count,
        caption_state=shape.caption_state), list(assets.values()), list(derivatives.values())),
        'default_revision_id': work.default_revision_id, 'latest_revision_id': work.latest_revision_id,
        'has_saved_version': work.default_revision_id is not None}


def test_source_sql_scalar_policy_evidence_and_account_buckets_match_legacy(archive):
    values = [None, {}, [], {'state': 'available'},
        observation(), observation(state='private'), observation(state='deleted'),
        observation(state='deleted', deletion_evidence=True, policy=None),
        observation(state='deleted', deletion_evidence=False),
        observation(confirmed=False), observation(confirmed='true'), observation(confirmed=1),
        observation(confirmed=1.0), observation(confirmed=None), observation(policy='source-v1'),
        observation(account_id='42'), observation(account_id=42), observation(account_id=None),
        observation(account_id=True), observation(account_id=['42']), observation(account_id={'x': 42}),
        observation(state='deleted', deletion_evidence=1, account_id='43'),
        observation(state='deleted', deletion_evidence='true'), observation(account_id='null')]
    with archive.write() as session:
        for identity, value in enumerate(values, 1):
            session.add(Artist(id=identity, pixiv_user_id=str(identity), name=str(identity)))
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=identity,
                title=str(identity), source_metadata={'remote_availability': value}))
            session.add(ApplicationState(key=ARTIST_PREFIX + str(identity), value=value))
        session.add(ArchiveIntent(pixiv_illust_id='5', mode='suppressed'))
    library = Library(archive)
    with archive.read() as session:
        baseline = tuple(session.connection().exec_driver_sql('SELECT * FROM read_source_counts ORDER BY kind,bucket,category').all())
    for account in (None, '42', 42, '43', 'null', '["42"]', '{"x":42}'):
        library.account_id = account
        for kind in ('works', 'artists'):
            for group in ('all', 'deleted', 'unavailable'):
                expected, counts = legacy_selection(library, kind, group)
                actual = library.source_overview(kind=kind, group=group, limit=100)
                assert [row['id'] for row in actual['items']] == expected
                assert actual['counts'][kind] == counts
                assert actual['total'] == len(expected)
                assert [row['id'] for row in library.source_overview(kind=kind, group=group, offset=2, limit=3)['items']] == expected[2:5]
                if kind == 'works':
                    with archive.read() as session:
                        for row in actual['items']:
                            assert row['availability'] == remote_availability(session.get(Work, row['id']), account)
    with archive.read() as session:
        assert tuple(session.connection().exec_driver_sql('SELECT * FROM read_source_counts ORDER BY kind,bucket,category').all()) == baseline
        assert verify(session.connection())['source_local_works'] == len(values)


def test_latest_detail_and_artist_summary_keep_distinct_caption_and_shape_semantics(archive):
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A', profile_refreshed_at=datetime(2026, 10, 1)))
        session.add(ApplicationState(key=ARTIST_PREFIX + 'a', value=observation()))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='live', type='manga', page_count=4,
            caption_state='empty', source_metadata={'remote_availability': observation()}))
        session.flush()
        session.add_all([WorkRevision(id='saved', work_id=1, ordinal=1, title='saved', type='manga', page_count=2,
            caption_state='empty', source_signature='s'), WorkRevision(id='latest', work_id=1, ordinal=2,
            title='latest', type='illust', page_count=1, caption_state='unavailable', source_signature='l')])
        session.flush()
        session.add_all([Asset(id='image', work_id=1, revision_id='saved', role='image', page_index=0,
            state='verified', relative_path='image.png'), Derivative(id='thumb', work_id=1, revision_id='saved',
            kind='thumbnail', state='verified', relative_path='thumb.webp')])
        session.flush()
        session.add_all([RevisionAsset(revision_id='saved', role='image', page_index=0, asset_id='image'),
            RevisionAsset(revision_id='latest', role='image', page_index=0, asset_id='image'),
            RevisionDerivative(revision_id='saved', kind='thumbnail', page_index=0, spec_key='v1', derivative_id='thumb'),
            RevisionDerivative(revision_id='latest', kind='thumbnail', page_index=0, spec_key='v1', derivative_id='thumb')])
        work = session.get(Work, 1)
        work.default_revision_id, work.latest_revision_id = 'saved', 'latest'
    library = Library(archive)
    row = library.source_overview()['items'][0]
    assert (row['type'], row['page_count']) == ('manga', 4)
    assert row['local']['missing'] == [{'role': 'caption', 'page': None, 'label': '作品简介资料', 'kind': 'metadata'}]
    artist = library.source_overview(kind='artists')['items'][0]
    assert artist['incomplete_work_count'] == 0 and artist['local']['state'] == 'complete'
    with archive.read() as session:
        assert row['local'] == legacy_local(session, session.get(Work, 1))
    # A reused older physical record changes latest facts immediately.
    with archive.write() as session:
        session.get(Asset, 'image').state = 'missing'
    assert library.source_overview()['items'][0]['local']['missing'][0]['role'] == 'image'
    assert library.source_overview(kind='artists')['items'][0]['incomplete_work_count'] == 1
    with archive.write() as session:
        session.get(Asset, 'image').state = 'verified'
        binding = session.get(RevisionAsset, ('latest', 'image', 0))
        session.delete(binding)
        session.add(RevisionAsset(revision_id='latest', role='image', page_index=1, asset_id='image'))
    with archive.read() as session:
        assert library.source_overview()['items'][0]['local'] == legacy_local(session, session.get(Work, 1))
        verify(session.connection())
    with archive.write() as session:
        session.get(Work, 1).latest_revision_id = None
    # Legacy source fallback selects default, while artist latest-NULL uses Work.
    assert len(library.source_overview()['items'][0]['local']['missing']) == 1
    assert library.source_overview(kind='artists')['items'][0]['incomplete_work_count'] == 1


def test_artist_totals_top12_hidden_suppressed_reassigned_and_null_order(archive):
    with archive.write() as session:
        session.add_all([Artist(id=1, pixiv_user_id='a', name='A'), Artist(id=2, pixiv_user_id='b', name='B')])
        for identity in range(1, 31):
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=1, title=str(identity),
                posted_at=datetime(2026, 10, 1) if identity < 26 else None))
        session.add(LocalPreference(work_id=25, is_hidden=True))
        session.add(ArchiveIntent(pixiv_illust_id='24', mode='suppressed'))
    library = Library(archive)
    row = library.artist_detail(1)
    assert row['work_count'] == 29
    assert [work['id'] for work in row['recent_works']] == [25, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13]
    with archive.write() as session:
        session.get(Work, 25).artist_id = 2
        session.get(ArchiveIntent, '24').mode = 'keep'
        session.delete(session.get(Work, 23))
    assert library.artist_detail(1)['work_count'] == 28
    assert library.artist_detail(2)['work_count'] == 1
    assert library.artist_detail(2)['recent_works'][0]['id'] == 25
    with archive.read() as session:
        verify(session.connection())


def test_application_state_key_identity_artist_remote_identity_and_delete(archive):
    with archive.write() as session:
        session.add_all([Artist(id=1, pixiv_user_id='a', name='A'), Artist(id=2, pixiv_user_id='b', name='B')])
        session.add(ApplicationState(key=ARTIST_PREFIX + 'a', value=observation()))
    library = Library(archive)
    assert [row['id'] for row in library.source_overview(kind='artists')['items']] == [1]
    with archive.write() as session:
        session.get(ApplicationState, ARTIST_PREFIX + 'a').key = ARTIST_PREFIX + 'b'
    assert [row['id'] for row in library.source_overview(kind='artists')['items']] == [2]
    with archive.write() as session:
        session.get(Artist, 2).pixiv_user_id = 'c'
    assert library.source_overview(kind='artists')['total'] == 0
    with archive.write() as session:
        session.get(ApplicationState, ARTIST_PREFIX + 'b').key = ARTIST_PREFIX + 'c'
    assert [row['id'] for row in library.source_overview(kind='artists')['items']] == [2]
    with archive.write() as session:
        session.delete(session.get(Artist, 2))
    assert library.source_overview(kind='artists')['total'] == 0


def test_source_failure_rolls_back_source_projection_and_counts(archive, monkeypatch):
    from src.core import source_read_projection
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='one'))
    original = source_read_projection._replace_entry
    def failing(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic source projection failure')
    monkeypatch.setattr(source_read_projection, '_replace_entry', failing)
    with pytest.raises(RuntimeError, match='synthetic source projection failure'):
        with archive.write() as session:
            session.get(Work, 1).source_metadata = {'remote_availability': observation()}
    monkeypatch.setattr(source_read_projection, '_replace_entry', original)
    assert Library(archive).source_overview()['total'] == 0
    with archive.read() as session:
        assert session.get(Work, 1).source_metadata == {}
        verify(session.connection())


def test_external_sql_leaves_source_reads_dirty_until_explicit_rebuild(archive):
    from src.core.projection_maintenance import rebuild_projections
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
        session.add(ApplicationState(key=ARTIST_PREFIX + 'a', value=observation()))
    with archive.writer_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE application_state SET value='{}' WHERE key='artist_source:a'")
    with pytest.raises(ProjectionUnavailable):
        Library(archive).source_overview(kind='artists')
    with archive.writer_session() as session:
        with session.begin():
            rebuild_projections(session)
    assert Library(archive).source_overview(kind='artists')['total'] == 0
    with archive.read() as session:
        verify(session.connection())


# Frozen SQL oracle from the transferred 0003 source, kept for equivalence tests.
def legacy_artist_summaries(session, artists):
    ids = [artist.id for artist in artists]
    if not ids:
        return {}
    # Aggregate local work facts in SQL. No per-artist/per-work queries or
    # loading every image-page record just to obtain two summary counts.
    current_type, current_pages = func.coalesce(WorkRevision.type, Work.type), func.coalesce(WorkRevision.page_count, Work.page_count)
    asset_bound = or_(and_(Work.latest_revision_id.is_(None), Asset.revision_id.is_(None)),
        select(RevisionAsset.asset_id).where(RevisionAsset.revision_id == Work.latest_revision_id,
            RevisionAsset.asset_id == Asset.id).exists())
    required = and_(asset_bound, or_(and_(current_type != "ugoira", Asset.role == "image", Asset.page_index < current_pages),
        and_(current_type == "ugoira", Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.page_index == 0)))
    counts = select(Asset.work_id.label("work_id"), func.count().label("recorded"),
        func.sum(case((and_(Asset.state == "verified", func.length(Asset.relative_path) > 0), 1), else_=0)).label("verified"),
        func.sum(case((or_(Asset.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")),
                              Asset.relative_path.is_(None), Asset.relative_path == ""), 1), else_=0)).label("missing")
        ).join(Work, Work.id == Asset.work_id).outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.artist_id.in_(ids), required).group_by(Asset.work_id).subquery()
    total = case((current_type == "ugoira", 2), else_=current_pages)
    derivative_bound = or_(and_(Work.latest_revision_id.is_(None), Derivative.revision_id.is_(None)),
        select(RevisionDerivative.derivative_id).where(RevisionDerivative.revision_id == Work.latest_revision_id,
            RevisionDerivative.derivative_id == Derivative.id).exists())
    thumbnail_base = select(Derivative.id).where(Derivative.work_id == Work.id, Derivative.kind == "thumbnail",
        Derivative.page_index == 0, Derivative.spec_key == "v1", func.length(Derivative.relative_path) > 0, derivative_bound)
    gif_base = select(Derivative.id).where(Derivative.work_id == Work.id, Derivative.kind == "gif", Derivative.page_index == 0,
        Derivative.spec_key == "v1", func.length(Derivative.relative_path) > 0, derivative_bound)
    bad = ("missing", "failed", "invalid", "mismatch", "corrupt")
    thumbnail = thumbnail_base.where(~Derivative.state.in_(bad)).exists()
    gif = gif_base.where(~Derivative.state.in_(bad)).exists()
    thumbnail_verified = thumbnail_base.where(Derivative.state == "verified").exists()
    gif_verified = gif_base.where(Derivative.state == "verified").exists()
    incomplete = or_(func.coalesce(counts.c.recorded, 0) < total, func.coalesce(counts.c.missing, 0) > 0,
                     ~thumbnail, and_(current_type == "ugoira", ~gif), Work.caption_state.in_(("unavailable", "missing", "failed")))
    unknown = or_(func.coalesce(counts.c.verified, 0) < total, ~Work.caption_state.in_(("available", "empty")),
                  ~thumbnail_verified, and_(current_type == "ugoira", ~gif_verified))
    rows = session.execute(select(Work.artist_id, func.count().label("total"),
        func.sum(case((incomplete, 1), else_=0)).label("incomplete"),
        func.sum(case((and_(~incomplete, unknown), 1), else_=0)).label("unknown")
        ).outerjoin(counts, counts.c.work_id == Work.id).outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.artist_id.in_(ids),
        ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
        .group_by(Work.artist_id)).all()
    counts_by_artist = {row.artist_id: row for row in rows}
    result = {}
    for artist in artists:
        row = counts_by_artist.get(artist.id)
        incomplete_count, unknown_count = (row.incomplete or 0, row.unknown or 0) if row else (0, 0)
        missing = []
        if incomplete_count:
            missing.append({"role": "saved_works", "page": None,
                            "label": f"{incomplete_count} 件已保存作品有本地缺项", "kind": "original"})
        if artist.avatar_url and not artist.avatar_path:
            missing.append({"role": "avatar", "page": None, "label": "画师头像缓存", "kind": "derivative"})
        unknown = bool(unknown_count or artist.profile_refreshed_at is None)
        result[artist.id] = {"work_count": row.total if row else 0, "incomplete_work_count": incomplete_count,
            "local": {"state": "incomplete" if missing else "unknown" if unknown else "complete", "missing": missing}}
    return result


def test_artist_aggregate_matches_frozen_legacy_sql_across_latest_and_slot_edges(archive):
    with archive.write() as session:
        session.add_all([Artist(id=1, pixiv_user_id='a', name='A', avatar_url='remote', profile_refreshed_at=datetime(2026, 10, 1)),
            Artist(id=2, pixiv_user_id='b', name='B')])
        for identity in range(1, 9):
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=1 if identity < 7 else 2,
                title=str(identity), page_count=2, caption_state='empty', source_metadata={'remote_availability': observation()}))
        session.flush()
        session.add_all([WorkRevision(id='r3', work_id=3, ordinal=1, title='three', type='illust', page_count=1,
            caption_state='available', source_signature='r3'),
            WorkRevision(id='r4', work_id=4, ordinal=1, title='four', type='ugoira', page_count=1,
            caption_state='missing', source_signature='r4'),
            WorkRevision(id='r5', work_id=5, ordinal=1, title='five', type='manga', page_count=3,
            caption_state='empty', source_signature='r5'),
            WorkRevision(id='r6', work_id=6, ordinal=1, title='six', type='illust', page_count=1,
            caption_state='unavailable', source_signature='r6')])
        session.flush()
        for identity in range(1, 9):
            for page in range(3):
                session.add(Asset(id=f'a{identity}_{page}', work_id=identity, revision_id=f'r{identity}' if 3 <= identity <= 6 else None,
                    role='image', page_index=page, state='unknown' if identity == 1 and page == 1 else 'verified',
                    relative_path=None if identity == 2 and page == 1 else f'{identity}_{page}.png'))
            session.add(Derivative(id=f'd{identity}', work_id=identity, revision_id=f'r{identity}' if 3 <= identity <= 6 else None,
                kind='thumbnail', state='unknown' if identity == 3 else 'verified', relative_path=f'{identity}.webp'))
        session.flush()
        for identity in range(3, 7):
            for page in range(3):
                session.add(RevisionAsset(revision_id=f'r{identity}', role='image', page_index=page,
                    asset_id=f'a{identity}_{page}'))
            session.add(RevisionDerivative(revision_id=f'r{identity}', kind='thumbnail', page_index=0,
                spec_key='v1', derivative_id=f'd{identity}'))
            work = session.get(Work, identity)
            work.latest_revision_id, work.default_revision_id = f'r{identity}', f'r{identity}'
        session.get(Work, 3).caption_state = 'unknown'
        session.get(Work, 5).latest_revision_id = 'r3'
        session.get(Work, 6).latest_revision_id = None
        session.add(LocalPreference(work_id=7, is_hidden=True))
        session.add(ArchiveIntent(pixiv_illust_id='8', mode='suppressed'))
    library = Library(archive)
    with archive.read() as session:
        artists = list(session.scalars(select(Artist).order_by(Artist.id)))
        assert library._artist_local_summaries(session, artists) == legacy_artist_summaries(session, artists)
        actual = {item['id']: item for item in library.source_overview()['items']}
        for work in session.scalars(select(Work).where(Work.id != 8)):
            assert actual[work.id]['local'] == legacy_local(session, work)
        verify(session.connection())


def test_artist_catalogue_count_and_name_order_are_indexed_and_transactional(archive):
    from src.core.source_read_projection import artist_count
    with archive.write() as session:
        session.add_all([Artist(id=1, pixiv_user_id='a', name='A'), Artist(id=2, pixiv_user_id='b', name='A'),
            Artist(id=3, pixiv_user_id='c', name='B')])
    assert Library(archive).artist_page(limit=2)['total'] == 3
    assert [row['id'] for row in Library(archive).artist_page(limit=2)['items']] == [1, 2]
    with pytest.raises(RuntimeError):
        with archive.write() as session:
            session.add(Artist(id=4, pixiv_user_id='d', name='D'))
            session.flush()
            raise RuntimeError('synthetic rollback')
    with archive.read() as session:
        assert artist_count(session.connection()) == 3
        plans = session.connection().exec_driver_sql('EXPLAIN QUERY PLAN SELECT id FROM artists ORDER BY name,id LIMIT 20').all()
        assert any('ix_artists_name_order' in row[3] for row in plans)
        assert not any('TEMP B-TREE' in row[3] for row in plans)
    with archive.write() as session:
        session.delete(session.get(Artist, 2))
    assert Library(archive).artist_page(limit=2)['total'] == 2


def test_source_and_artist_preview_reads_do_not_select_url_manifests(archive):
    from sqlalchemy import event
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='W',
            source_metadata={'remote_availability': observation(), 'page_urls': ['unused'] * 1000}))
        session.flush()
        session.add(WorkRevision(id='r', work_id=1, ordinal=1, title='R', type='illust', page_count=1,
            caption_state='empty', source_signature='r', source_metadata={'page_urls': ['unused'] * 1000}))
        session.flush()
        work = session.get(Work, 1)
        work.default_revision_id, work.latest_revision_id = 'r', 'r'
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(archive.engine, 'before_cursor_execute', record)
    try:
        Library(archive).source_overview()
        Library(archive).artist_detail(1)
    finally:
        event.remove(archive.engine, 'before_cursor_execute', record)
    assert not any('works.source_metadata' in statement for statement in statements)
    assert not any('work_revisions.source_metadata' in statement for statement in statements)


def test_state_path_changes_use_slot_deltas_without_scope_rebuild(archive, monkeypatch):
    from src.core import source_read_projection as projection
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A', profile_refreshed_at=datetime(2026, 10, 1)))
        session.add(ApplicationState(key=ARTIST_PREFIX + 'a', value=observation()))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='W', page_count=2000,
            caption_state='empty', source_metadata={'remote_availability': observation()}))
        session.flush()
        session.add_all([Asset(id=f'p{page}', work_id=1, role='image', page_index=page,
            state='verified', relative_path=f'{page}.png') for page in range(2000)])
        session.add(Derivative(id='thumb', work_id=1, kind='thumbnail', state='verified', relative_path='thumb.webp'))
    library = Library(archive)
    assert library.source_overview()['items'][0]['local']['state'] == 'complete'
    def no_scope_rebuild(*args, **kwargs):
        raise AssertionError('ordinary resource state/path change rebuilt the P scope')
    monkeypatch.setattr(projection, '_normalized_local', no_scope_rebuild)
    # Every edit touches at most its detail slot and artist physical fact.
    for state, path, expected, missing in [
        ('missing', '7.png', 'incomplete', ['image']),
        ('unknown', '7.png', 'unknown', []),
        ('verified', '7.png', 'complete', []),
        ('verified', None, 'incomplete', ['image']),
        ('verified', '7.png', 'complete', []),
    ]:
        with archive.write() as session:
            record = session.get(Asset, 'p7')
            record.state, record.relative_path = state, path
        local = library.source_overview()['items'][0]['local']
        assert local['state'] == expected
        assert [item['role'] for item in local['missing']] == missing
        if missing:
            assert local['missing'][0]['page'] == 7
        artist = library.source_overview(kind='artists')['items'][0]
        assert artist['incomplete_work_count'] == int(expected == 'incomplete')
        assert artist['local']['state'] == expected
    for state, path, expected in [('unknown', 'thumb.webp', 'unknown'), ('failed', None, 'incomplete'), ('verified', 'thumb.webp', 'complete')]:
        with archive.write() as session:
            record = session.get(Derivative, 'thumb')
            record.state, record.relative_path = state, path
        assert library.source_overview()['items'][0]['local']['state'] == expected
        assert library.source_overview(kind='artists')['items'][0]['local']['state'] == expected


def test_missing_rows_are_normalized_ordered_and_output_sensitive(archive):
    from src.core.source_read_projection import work_details
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='W', page_count=1000,
            caption_state='unavailable', source_metadata={'remote_availability': observation()}))
    with archive.read() as session:
        connection = session.connection()
        cached = connection.exec_driver_sql('SELECT local FROM read_source_work_local WHERE work_id=1').scalar()
        assert 'missing' not in cached and len(cached) < 256
        local = work_details(connection, [1])[1]
        assert len(local['missing']) == 1002
        assert [item['page'] for item in local['missing'][:1000]] == list(range(1000))
        assert [item['role'] for item in local['missing'][-2:]] == ['thumbnail', 'caption']
        plans = connection.exec_driver_sql("EXPLAIN QUERY PLAN SELECT role,page,kind FROM read_source_slot_facts WHERE work_id=1 AND scope='detail' AND missing=1 ORDER BY position").all()
        assert any('ix_read_source_missing_slots' in row[3] for row in plans)
        assert not any('TEMP B-TREE' in row[3] for row in plans)
        verify(connection)


def test_physical_source_delta_failure_rolls_back_slots_counts_and_source(archive, monkeypatch):
    from src.core import source_read_projection as projection
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
        session.add(Work(id=1, pixiv_illust_id='w', artist_id=1, title='W', caption_state='empty',
            source_metadata={'remote_availability': observation()}))
        session.flush()
        session.add(Asset(id='image', work_id=1, role='image', state='verified', relative_path='image.png'))
        session.add(Derivative(id='thumb', work_id=1, kind='thumbnail', state='verified', relative_path='thumb.webp'))
    original = projection._refresh_summary
    def fail_after_refresh(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic physical source delta failure')
    monkeypatch.setattr(projection, '_refresh_summary', fail_after_refresh)
    with pytest.raises(RuntimeError, match='synthetic physical source delta failure'):
        with archive.write() as session:
            session.get(Asset, 'image').state = 'missing'
    monkeypatch.setattr(projection, '_refresh_summary', original)
    assert Library(archive).source_overview()['items'][0]['local'] == {
        'state': 'complete', 'missing': [], 'default_revision_id': None, 'latest_revision_id': None, 'has_saved_version': False}
    with archive.read() as session:
        assert session.get(Asset, 'image').state == 'verified'
        verify(session.connection())


def test_application_state_capture_is_indexed_and_n_independent(tmp_path):
    measurements = {'unrelated': [], 'matching': []}
    for size in (100, 1000, 10000):
        db = ArchiveDatabase.create(tmp_path / f'artist-state-{size}.db')
        try:
            matching = ARTIST_PREFIX + str(size)
            with db.write() as session:
                session.add_all([Artist(id=identity, pixiv_user_id=str(identity), name=str(identity)) for identity in range(1, size + 1)])
                session.add(ApplicationState(key='unrelated-counter', value=0))
                session.add(ApplicationState(key=matching, value=observation()))
            with db.read() as session:
                for key in ('unrelated-counter', matching):
                    plans = session.connection().exec_driver_sql("EXPLAIN QUERY PLAN SELECT id FROM artists WHERE substr(?,1,14)='artist_source:' AND pixiv_user_id=substr(?,15)", (key, key)).all()
                    assert any('SEARCH artists USING COVERING INDEX sqlite_autoindex_artists_1' in row[3] for row in plans)
                    assert not any('SCAN artists' in row[3] for row in plans)
                trigger = session.connection().exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='read_source_capture_application_state_update'").scalar()
                assert "pixiv_user_id=substr(OLD.key,15)" in trigger and "pixiv_user_id=substr(NEW.key,15)" in trigger
            for name, key in (('unrelated', 'unrelated-counter'), ('matching', matching)):
                steps = [0]
                def progress():
                    steps[0] += 1
                    return 0
                with db.writer_engine.connect() as connection:
                    connection.connection.driver_connection.set_progress_handler(progress, 1)
                try:
                    with db.write() as session:
                        record = session.get(ApplicationState, key)
                        record.value = 1 if name == 'unrelated' else observation(checked_at='synthetic-new')
                finally:
                    with db.writer_engine.connect() as connection:
                        connection.connection.driver_connection.set_progress_handler(None, 0)
                measurements[name].append(steps[0])
            assert Library(db).source_overview(kind='artists')['total'] == 1
            assert Library(db).source_overview(kind='artists')['items'][0]['id'] == size
        finally:
            db.close()
    print('application-state writer VM steps:', measurements)
    for values in measurements.values():
        assert values[-1] <= values[0] * 1.15 + 30


def test_missing_mandatory_zero_artist_counter_fails_closed_and_repairs_explicitly(archive):
    from sqlalchemy.exc import IntegrityError
    from src.core.projection_maintenance import rebuild_projections, verify_projections
    from src.core.source_read_projection import artist_count
    with archive.read() as session:
        assert artist_count(session.connection()) == 0
        verify_projections(session)
    with archive.writer_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM read_common_counts WHERE scope='artists'")
    with archive.read() as session:
        with pytest.raises(ProjectionUnavailable, match='mandatory artist counter'):
            artist_count(session.connection())
        with pytest.raises(ProjectionUnavailable, match='mandatory artist counter'):
            verify_projections(session)
    with pytest.raises(IntegrityError, match='mandatory artist counter'):
        with archive.write() as session:
            session.add(Artist(id=1, pixiv_user_id='a', name='A'))
    with archive.read() as session:
        assert session.get(Artist, 1) is None
    # Only an explicit candidate repair may recreate the mandatory zero seed.
    with archive.writer_session() as session:
        with session.begin():
            rebuild_projections(session)
    with archive.write() as session:
        session.add(Artist(id=1, pixiv_user_id='a', name='A'))
    with archive.read() as session:
        assert artist_count(session.connection()) == 1
        verify_projections(session)
