"""Display-only metadata, kept apart from potentially long source URL manifests."""
import json
from sqlalchemy import select
from .models import Work, WorkRevision
from .read_projection import ProjectionUnavailable


def install(connection):
    connection.exec_driver_sql('CREATE TABLE read_display_metadata (scope TEXT PRIMARY KEY, availability TEXT NOT NULL, translations TEXT NOT NULL) WITHOUT ROWID')
    for table, entity in (("works", "display_work"), ("work_revisions", "display_revision")):
        for event in ("INSERT", "UPDATE", "DELETE"):
            sides = ("NEW",) if event == "INSERT" else ("OLD",) if event == "DELETE" else ("OLD", "NEW")
            body = "".join(f"INSERT OR IGNORE INTO read_model_dirty VALUES('{entity}',CAST({side}.id AS TEXT));" for side in sides)
            clause = "UPDATE OF id,source_metadata" if event == "UPDATE" else event
            condition = " WHEN OLD.id IS NOT NEW.id OR OLD.source_metadata IS NOT NEW.source_metadata" if event == "UPDATE" else ""
            connection.exec_driver_sql(f"CREATE TRIGGER read_display_{table}_{event.lower()} AFTER {clause} ON {table}{condition} BEGIN {body} END")



def _values(metadata):
    metadata = metadata or {}
    return (json.dumps(metadata.get('remote_availability'), ensure_ascii=False),
            json.dumps(metadata.get('tag_translations'), ensure_ascii=False))


def _replace(connection, scope, metadata):
    availability, translations = _values(metadata)
    connection.exec_driver_sql('INSERT INTO read_display_metadata VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET availability=excluded.availability,translations=excluded.translations WHERE availability<>excluded.availability OR translations<>excluded.translations',
        (scope, availability, translations))


def refresh_work(connection, work):
    _replace(connection, f'work:{work.id}', work.source_metadata)


def refresh_revision(connection, revision):
    _replace(connection, 'revision:' + revision.id, revision.source_metadata)


def drop(connection, scope):
    connection.exec_driver_sql('DELETE FROM read_display_metadata WHERE scope=?', (scope,))


def maintain(connection):
    for entity, table, prefix in (("display_work", "works", "work:"),
                                  ("display_revision", "work_revisions", "revision:")):
        ids = list(connection.exec_driver_sql('SELECT identity FROM read_model_dirty WHERE entity=?', (entity,)).scalars())
        for identity in ids:
            row = connection.exec_driver_sql(f'SELECT source_metadata FROM {table} WHERE id=?', (identity,)).first()
            if row is None:
                drop(connection, prefix + identity)
            else:
                _replace(connection, prefix + identity, json.loads(row[0]) if isinstance(row[0], str) else row[0])


def rebuild(session):
    connection = session.connection()
    connection.exec_driver_sql('DELETE FROM read_display_metadata')
    for work in session.scalars(select(Work)):
        refresh_work(connection, work)
    for revision in session.scalars(select(WorkRevision)):
        refresh_revision(connection, revision)


def verify(session):
    connection = session.connection()
    expected = {f'work:{row.id}': _values(row.source_metadata) for row in session.scalars(select(Work))}
    expected.update({'revision:' + row.id: _values(row.source_metadata) for row in session.scalars(select(WorkRevision))})
    actual = {row[0]: tuple(row[1:]) for row in connection.exec_driver_sql('SELECT scope,availability,translations FROM read_display_metadata')}
    if actual != expected:
        raise ProjectionUnavailable('display metadata projection differs from source')


def read(session, identities, *, revisions=False, include_translations=False):
    """Select only display bytes; card reads never decode tag/URL manifests."""
    ids = list(dict.fromkeys(identities))
    prefix = 'revision:' if revisions else 'work:'
    result = {}
    for start in range(0, len(ids), 500):
        selected = ids[start:start + 500]
        scopes = tuple(prefix + str(value) for value in selected)
        columns = 'scope,availability,translations' if include_translations else 'scope,availability'
        rows = session.connection().exec_driver_sql(f'SELECT {columns} FROM read_display_metadata WHERE scope IN (' + ','.join('?' for _ in scopes) + ')', scopes)
        for row in rows:
            result[row[0][len(prefix):]] = {'remote_availability': json.loads(row[1])}
            if include_translations:
                result[row[0][len(prefix):]]['tag_translations'] = json.loads(row[2])
    if set(result) != {str(value) for value in ids}:
        raise ProjectionUnavailable('display metadata is missing; explicit repair is required')
    return result
