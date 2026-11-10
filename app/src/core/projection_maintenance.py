"""Transactional, explicitly installed archive read-model maintenance.

SQLite source triggers retain both sides of identity changes, including raw SQL.
An ordinary writer may consume only the changes it made after a clean starting
snapshot. Already dirty databases require an explicit candidate repair/rebuild.
Work refreshes inspect their current resources, never all their revision/task
history. Parent lifecycle changes update one shared narrow fact; ordinary progress
reports leave parent ordering unchanged. A single item update refreshes its
item-only fact and indexed best-item identities. Reads compose task state with
the current source and resource snapshots for the requested works.
"""
from __future__ import annotations

from . import diagnostics

import json
import math
import re
import time
from functools import lru_cache
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import set_committed_value

from . import read_projection as store
from . import normalized_task_projection as tasks
from .models import Work, WorkRevision, ArchiveIntent, LocalPreference, RemoteBookmark

ACTIVE = ("queued", "planning", "ready", "running", "interrupted")
LOCAL_KINDS = ("check", "metadata", "repair", "verify", "backfill", "thumbnails", "reconcile", "compare")
BAD = ("missing", "failed", "invalid", "mismatch", "corrupt")
STATE_TABLES = ("read_work_state", "read_revision_state")


def install_state_cache(connection):
    connection.exec_driver_sql('CREATE TABLE read_work_state (work_id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
    connection.exec_driver_sql('CREATE TABLE read_revision_state (revision_id TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID')


def _queue(entity, expression, *, selection=None):
    if selection is not None:
        return f"INSERT OR IGNORE INTO read_model_dirty SELECT '{entity}',CAST({expression} AS TEXT) {selection};"
    return f"INSERT OR IGNORE INTO read_model_dirty SELECT '{entity}',CAST({expression} AS TEXT) WHERE {expression} IS NOT NULL;"


def _trigger(connection, name, table, event, body, *, when=None):
    connection.exec_driver_sql(f"CREATE TRIGGER {name} AFTER {event} ON {table} "
        + (f"WHEN {when} " if when else "") + f"BEGIN {body} END")


def _source_triggers(connection):
    """Capture identities in SQL, so ORM, bulk statements and external writes agree."""
    for table in ("works", "work_revisions", "assets", "derivatives",
                  "revision_assets", "revision_derivatives", "local_preferences",
                  "remote_bookmarks", "archive_intents", "operations", "operation_items",
                  "application_state", "work_tags"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            sides = ("NEW",) if action == "INSERT" else ("OLD",) if action == "DELETE" else ("OLD", "NEW")
            body = ""
            for side in sides:
                if table == "works":
                    body += _queue("work", f"{side}.id")
                    if action == "UPDATE":
                        body += _queue("resource_work", f"{side}.id", selection="WHERE OLD.id IS NOT NEW.id OR OLD.type IS NOT NEW.type OR OLD.page_count IS NOT NEW.page_count OR OLD.default_revision_id IS NOT NEW.default_revision_id")
                    else:
                        body += _queue("resource_work", f"{side}.id")
                elif table == "work_revisions":
                    body += _queue("revision", f"{side}.id")
                    body += _queue("lifecycle_revision", f"{side}.id")
                    body += _queue("work", "id", selection=f"FROM works WHERE default_revision_id={side}.id")
                elif table in ("local_preferences", "remote_bookmarks", "work_tags"):
                    body += _queue("work", f"{side}.work_id")
                elif table == "archive_intents":
                    body += _queue("work", "id", selection=f"FROM works WHERE pixiv_illust_id={side}.pixiv_illust_id")
                elif table == "operations":
                    body += _queue("operation", f"{side}.id")
                elif table == "operation_items":
                    body += _queue("item", f"{side}.id")
                    body += _queue("scope", f"{side}.operation_id")
                elif table == "application_state":
                    body += _queue("work", f"CAST(substr({side}.key,18) AS INTEGER)",
                        selection=f"WHERE {side}.key LIKE 'maintenance_work:%'")
                elif table in ("revision_assets", "revision_derivatives"):
                    if table == "revision_assets":
                        body += _queue("revision", f"{side}.revision_id")
                    body += _queue("lifecycle_revision", f"{side}.revision_id")
                    body += _queue("work", "id", selection=f"FROM works WHERE default_revision_id={side}.revision_id")
                    body += _queue("resource_work", "id", selection=f"FROM works WHERE default_revision_id={side}.revision_id")
                else:
                    binding, physical = ("revision_assets", "asset_id") if table == "assets" else ("revision_derivatives", "derivative_id")
                    target = "asset_id" if table == "assets" else "record_id"
                    # Physical readiness changes update existing indexed slots
                    # by delta. Only identity/shape changes rebuild a scope.
                    structural_fields = ("id", "work_id", "revision_id", "role", "page_index") if table == "assets" else ("id", "work_id", "revision_id", "kind", "page_index", "spec_key")
                    structural = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in structural_fields)
                    structural_where = f"WHERE {structural}" if action == "UPDATE" else None
                    body += _queue("work", f"{side}.work_id")
                    body += _queue("resource_work", f"{side}.work_id", selection=structural_where)
                    if table == "assets":
                        # Every bound explicit archive really changes with its
                        # original file; derivative changes do not affect this
                        # archive-only pinned revision state.
                        body += _queue("revision", f"{side}.revision_id", selection=structural_where)
                        body += _queue("revision", "revision_id", selection=f"FROM {binding} WHERE {physical}={side}.id" + (f" AND ({structural})" if action == "UPDATE" else ""))
                    body += _queue("lifecycle_revision", f"{side}.revision_id", selection=structural_where)
                    body += _queue("lifecycle_revision", "revision_id", selection=f"FROM {binding} WHERE {physical}={side}.id" + (f" AND ({structural})" if action == "UPDATE" else ""))
                    current_users = f"FROM read_resource_facts INDEXED BY ix_read_resource_current_physical WHERE physical_id={side}.id AND substr(scope,1,5)='work:'"
                    body += _queue("work", "CAST(substr(scope,6) AS INTEGER)", selection=current_users)
                    body += _queue("resource_work", "CAST(substr(scope,6) AS INTEGER)", selection=current_users + (f" AND ({structural})" if action == "UPDATE" else ""))
                    if action == "UPDATE" and side == "NEW":
                        entity = "physical_asset" if table == "assets" else "physical_derivative"
                        body += _queue(entity, "NEW.id", selection="WHERE OLD.state IS NOT NEW.state OR OLD.relative_path IS NOT NEW.relative_path")
                    task_selection = "FROM operation_items WHERE json_extract(target,'$.work_id') IS NULL AND " + f"CAST(json_extract(target,'$.{target}') AS TEXT)={side}.id"
                    if action == "UPDATE":
                        task_selection += " AND (OLD.id IS NOT NEW.id OR OLD.work_id IS NOT NEW.work_id)"
                    body += _queue("item", "id", selection=task_selection)
            when = None
            if action == "UPDATE":
                fields = {
                    "operations": ("id", "kind", "state", "created_at", "updated_at"),
                    "operation_items": ("id", "operation_id", "state", "position", "kind"),
                    "assets": ("id", "work_id", "revision_id", "role", "page_index", "state", "relative_path"),
                    "derivatives": ("id", "work_id", "revision_id", "kind", "page_index", "spec_key", "state", "relative_path"),
                    "work_revisions": ("id", "work_id", "type", "page_count"),
                }.get(table)
                if fields:
                    expressions = [f"OLD.{name} IS NOT NEW.{name}" for name in fields]
                    if table == "operations":
                        expressions.append("json_extract(OLD.parameters,'$.work_ids') IS NOT json_extract(NEW.parameters,'$.work_ids')")
                    elif table == "operation_items":
                        expressions.extend(f"json_extract(OLD.target,'$.{name}') IS NOT json_extract(NEW.target,'$.{name}')" for name in ('work_id','asset_id','record_id'))
                        expressions.append("json_extract(OLD.result,'$.message') IS NOT json_extract(NEW.result,'$.message')")
                    when = " OR ".join(expressions)
            _trigger(connection, f"read_capture_{table}_{action.lower()}", table, action, body, when=when)

    # Cursor/catalogue generation fences membership/order/filter changes only.
    # Download and task progress updates still refresh states atomically but do
    # not invalidate an otherwise stable browsing window.
    generation = _queue("generation", "'catalogue'")
    for table in ("works", "local_preferences", "remote_bookmarks", "archive_intents", "work_tags"):
        for action in ("INSERT", "DELETE"):
            _trigger(connection, f"read_generation_{table}_{action.lower()}", table, action, generation)
    shape = ("id", "pixiv_illust_id", "artist_id", "type", "page_count", "title", "posted_at", "downloaded_at", "time_basis", "default_revision_id")
    _trigger(connection, "read_generation_works_update", "works", "UPDATE", generation,
        when=" OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in shape))
    for table, columns in (("local_preferences", ("work_id", "is_hidden")),
            ("remote_bookmarks", ("work_id", "value")),
            ("archive_intents", ("pixiv_illust_id", "mode")),
            ("work_tags", ("work_id", "tag")), ("artists", ("name",))):
        _trigger(connection, f"read_generation_{table}_update", table, "UPDATE", generation,
            when=" OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in columns))


def install_projections(connection):
    """Install empty projections/triggers in an explicit new/candidate transaction."""
    from .task_indexes import create_task_indexes
    store.install(connection)
    install_state_cache(connection)
    tasks.install(connection)
    create_task_indexes(connection)
    connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_bookmark_work_latest ON bookmark_operations(work_id,version DESC,created_at DESC,id DESC)')
    connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_revision_visible_history ON work_revisions(work_id,state,ordinal DESC)')
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_revision_visible_order ON work_revisions(work_id,ordinal DESC) WHERE state != 'redundant'")
    connection.exec_driver_sql("CREATE INDEX ix_revision_selection ON work_revisions(work_id,(CASE WHEN state='complete' AND viewable=1 THEN 2 WHEN viewable=1 THEN 1 ELSE 0 END) DESC,ordinal DESC) WHERE state != 'redundant'")
    _source_triggers(connection)
    from .task_read_projection import install_task_read_projection
    install_task_read_projection(connection)
    from .source_read_projection import install as install_source_projection
    install_source_projection(connection)
    from .display_metadata import install as install_display_metadata
    install_display_metadata(connection)
    from .gallery_card_projection import install as install_gallery_cards
    install_gallery_cards(connection)


def _normalized_ddl(statement):
    # Preserve SQL string literals/JSON paths while ignoring harmless token
    # spacing, keyword case, quoted identifiers and IF NOT EXISTS spelling.
    from .index_preparation import _normalized_sql
    return tuple(_normalized_sql(statement))

@lru_cache(maxsize=1)
def _expected_projection_schema():
    """Generate the fixed contract with a recorder, never against a database."""
    class Recorder:
        connection = SimpleNamespace(driver_connection=SimpleNamespace(in_transaction=True))
        def __init__(self):
            self.statements = []
        def in_transaction(self):
            return True
        def exec_driver_sql(self, statement, *args):
            self.statements.append(statement)
            return self
        def scalars(self):
            return ()
    recorder = Recorder()
    install_projections(recorder)
    expected = {}
    for statement in recorder.statements:
        match = re.match(r'CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', statement, re.IGNORECASE)
        if match:
            expected[match.group(2).casefold()] = (match.group(1).casefold(), _normalized_ddl(statement))
    # Retain the old explicit startup preparation route for source task indexes.
    from .task_indexes import TASK_INDEX_SQL
    for name, statement in TASK_INDEX_SQL.items():
        expected[name.casefold()] = ('index', _normalized_ddl(statement))
    return expected


def validate_projection_schema(connection):
    """Read-only schema-sized validation; reject same-name malformed objects."""
    expected = _expected_projection_schema()
    actual = {name.casefold(): (kind.casefold(), _normalized_ddl(statement))
        for name, kind, statement in connection.exec_driver_sql(
            "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','index','trigger') AND sql IS NOT NULL")}
    from .task_indexes import TASK_INDEX_SQL
    optional_missing = {name.casefold() for name in TASK_INDEX_SQL}
    for name, definition in expected.items():
        if name not in actual and name in optional_missing:
            continue
        if actual.get(name) != definition:
            raise store.ProjectionUnavailable(f'projection schema object is missing or incompatible: {name}')
    return {"objects": len(expected)}


def _candidate_values(operation, item, *, concern):
    active = operation["state"] in ACTIVE and item["state"] in ("pending", "running")
    priority = 0 if active and item["state"] == "running" else 1 if active else 2
    payload = {"operation_id": operation["id"], "operation_state": operation["state"],
        "item_state": item["state"], "reason": item.get("reason"), "concern": concern}
    return dict(priority=priority, created_at=operation["created_at"] or "",
        updated_at=operation["updated_at"] or "",
        failure_priority=0 if item["state"] in ("failed", "not_executed") else 1,
        position=item["position"], operation_id=operation["id"], item_id=item.get("id") or "", payload=payload)




_DIRECT_SQL = """
SELECT i.id,i.operation_id,i.state,i.position,i.kind,
       json_extract(i.target,'$.work_id') AS explicit_work,
       CAST(json_extract(i.target,'$.asset_id') AS TEXT) AS asset_id,
       CAST(json_extract(i.target,'$.record_id') AS TEXT) AS record_id,
       coalesce(json_extract(i.target,'$.work_id'),a.work_id,d.work_id) AS resolved_work,
       typeof(coalesce(json_extract(i.target,'$.work_id'),a.work_id,d.work_id)) AS resolved_type,
       json_extract(i.result,'$.message') AS reason
FROM operation_items i
LEFT JOIN assets a ON json_extract(i.target,'$.work_id') IS NULL
 AND a.id=CAST(json_extract(i.target,'$.asset_id') AS TEXT)
LEFT JOIN derivatives d ON json_extract(i.target,'$.work_id') IS NULL AND a.work_id IS NULL
 AND d.id=CAST(json_extract(i.target,'$.record_id') AS TEXT)
WHERE {predicate} ORDER BY i.id
"""


def _operation(connection, operation_id):
    return connection.exec_driver_sql("SELECT id,kind,state,created_at,updated_at,parameters,json_type(parameters,'$.work_ids') AS scope_type FROM operations WHERE id=?", (operation_id,)).mappings().first()


def _direct_candidate(connection, row, operation, *, dry_run=False):
    # The old direct query compared JSON values to INTEGER bind values without
    # casting. JSON strings (even '1') must not become explicit work identities.
    identity = row["resolved_work"]
    if (row["resolved_type"] not in ("integer", "real") or identity is None
            or not math.isfinite(identity) or int(identity) != identity
            or not -(2**63) <= identity < 2**63):
        return None
    identity = int(identity)
    concern = ("original" if row["asset_id"] is not None or row["kind"] == "ugoira_pair"
        else "local" if row["record_id"] is not None or operation["kind"] == "thumbnails"
        or row["kind"] in ("gif", "thumbnail", "verify_derivative") else "work")
    if dry_run:
        return identity, "item:" + row["id"], _candidate_values(operation, row, concern=concern)
    raise RuntimeError("direct task candidates must use normalized transactional maintenance")


def reconcile_item(connection, item_id):
    return tasks.reconcile_item(connection, item_id)


def _scope_values(connection, operation_id, *, members=None):
    operation = _operation(connection, operation_id)
    if operation is None or operation["kind"] not in LOCAL_KINDS or operation["scope_type"] is None:
        return set(), None
    # Keep CAST semantics of json_each, including scalar/object legacy values.
    if members is None:
        members = set(connection.exec_driver_sql("SELECT DISTINCT CAST(value AS INTEGER) FROM operations,json_each(operations.parameters,'$.work_ids') WHERE operations.id=? AND CAST(value AS INTEGER) IS NOT NULL", (operation_id,)).scalars())
    if not members:
        return members, None
    item = connection.exec_driver_sql("""SELECT id,state,position,json_extract(result,'$.message') AS reason
        FROM operation_items INDEXED BY ix_items_target_scope WHERE operation_id=?
        AND json_extract(target,'$.work_id') IS NULL
        AND CAST(json_extract(target,'$.asset_id') AS TEXT) IS NULL
        AND CAST(json_extract(target,'$.record_id') AS TEXT) IS NULL
        ORDER BY CASE WHEN ? AND state='running' THEN 0 WHEN ? AND state='pending' THEN 1 ELSE 2 END,
            CASE WHEN state IN ('failed','not_executed') THEN 0 ELSE 1 END,position,id LIMIT 1""",
        (operation_id, int(operation["state"] in ACTIVE), int(operation["state"] in ACTIVE))).mappings().first()
    if item is None:
        any_item = connection.exec_driver_sql('SELECT 1 FROM operation_items WHERE operation_id=? LIMIT 1', (operation_id,)).first()
        if operation["state"] not in ACTIVE or any_item:
            return members, None
        item = {"id": None, "state": "pending", "position": 0, "reason": None}
    return members, _candidate_values(operation, item,
        concern="local" if operation["kind"] == "thumbnails" else "work")


def reconcile_scope(connection, operation_id, *, membership_unchanged=False):
    return tasks.reconcile_scope(connection, operation_id, membership_unchanged=membership_unchanged)


def reconcile_operation(connection, operation_id):
    return tasks.reconcile_operation(connection, operation_id)


def _histories(connection, work_ids):
    result = {}
    for work_id in work_ids:
        winner = store.task_winner(connection, work_id, check_ready=False)
        if winner is not None:
            result[work_id] = SimpleNamespace(**winner["payload"])
    return result


def _replace_resources(connection, scope, facts):
    """Resource fanout is bounded by the changed current/revision scope, not H."""
    existing = {row[0]: (row[1], tuple(row[2:])) for row in connection.exec_driver_sql(
        'SELECT slot,physical_id,recorded,verified,missing,thumbnail,gif FROM read_resource_facts WHERE scope=?', (scope,))}
    for slot in existing.keys() - facts.keys():
        store.resource_fact(connection, scope, slot)
    for slot, (physical, values) in facts.items():
        normalized = tuple(values.get(key, 0) for key in store._FIELDS)
        if existing.get(slot) != (physical, normalized):
            store.resource_fact(connection, scope, slot, physical, **values)

def _asset_facts(rows):
    result = {}
    for row in rows:
        bad = row["state"] in BAD or not row["relative_path"]
        result[f"asset:{row['role']}:{row['page_index']}"] = (row["id"], {
            "recorded": 1, "verified": int(row["state"] == "verified" and bool(row["relative_path"])), "missing": int(bad)})
    return result


def _derivative_values(row):
    """Retain eligible zero-readiness dependencies for future physical deltas."""
    thumbnail_slot = row["kind"] == "thumbnail" and row["page_index"] == 0 and row["spec_key"] == "v1"
    gif_slot = row["kind"] == "gif" and row["spec_key"] == "v1"
    if not thumbnail_slot and not gif_slot:
        return None
    return {"thumbnail": int(thumbnail_slot and row["state"] == "verified" and bool(row["relative_path"])),
        "gif": int(gif_slot and row["state"] in ("verified", "unknown") and bool(row["relative_path"]))}


def reconcile_physical_resources(connection, asset_ids=(), derivative_ids=()):
    """Update only affected physical slots; readiness writes never enumerate P.

    Reused files intentionally fan out to their actual dependent logical slots.
    Structural edits are separately captured for explicit scope replacement.
    """
    works, revisions = set(), set()
    for table, identities, prefix in (("assets", asset_ids, "asset:"), ("derivatives", derivative_ids, "derivative:")):
        columns = "state,relative_path" if table == "assets" else "kind,page_index,spec_key,state,relative_path"
        for identity in sorted(set(identities)):
            physical = connection.exec_driver_sql(f"SELECT {columns} FROM {table} WHERE id=?", (identity,)).mappings().first()
            if physical is None:
                continue  # Deletions and identity edits rebuild captured scopes.
            _lifecycle_physical(connection, table, identity, physical)
            if table == "assets":
                values = {"recorded": 1, "verified": int(physical["state"] == "verified" and bool(physical["relative_path"])),
                    "missing": int(physical["state"] in BAD or not physical["relative_path"])}
            else:
                values = _derivative_values(physical)
                if values is None:
                    continue
            expected = tuple(values.get(key, 0) for key in store._FIELDS)
            slots = connection.exec_driver_sql(
                'SELECT scope,slot,recorded,verified,missing,thumbnail,gif FROM read_resource_facts WHERE physical_id=? AND substr(slot,1,?)=?',
                (identity, len(prefix), prefix)).all()
            for scope, slot, *old in slots:
                if not connection.exec_driver_sql('SELECT 1 FROM read_resource_totals WHERE scope=?', (scope,)).first():
                    raise store.ProjectionUnavailable('resource aggregate is missing; explicit repair is required')
                if tuple(old) != expected:
                    store.resource_fact(connection, scope, slot, identity, **values)
                if scope.startswith('work:'):
                    works.add(int(scope[5:]))
                elif scope.startswith('revision:'):
                    revisions.add(scope[9:])
    return works, revisions


def _lifecycle_fact(connection, revision_id, slot, physical_id=None, values=(0, 0, 0)):
    old = connection.exec_driver_sql('SELECT verified,readable,gif FROM read_revision_lifecycle_facts WHERE revision_id=? AND slot=?', (revision_id, slot)).first()
    previous = tuple(old) if old else (0, 0, 0)
    expected = values if physical_id is not None else (0, 0, 0)
    connection.exec_driver_sql('INSERT OR IGNORE INTO read_revision_lifecycle_totals(revision_id) VALUES(?)', (revision_id,))
    connection.exec_driver_sql('UPDATE read_revision_lifecycle_totals SET verified=verified+?,readable=readable+?,gif=gif+? WHERE revision_id=?', (*[after - before for before, after in zip(previous, expected)], revision_id))
    if physical_id is None:
        connection.exec_driver_sql('DELETE FROM read_revision_lifecycle_facts WHERE revision_id=? AND slot=?', (revision_id, slot))
    else:
        connection.exec_driver_sql('INSERT INTO read_revision_lifecycle_facts VALUES(?,?,?,?,?,?) ON CONFLICT(revision_id,slot) DO UPDATE SET physical_id=excluded.physical_id,verified=excluded.verified,readable=excluded.readable,gif=excluded.gif', (revision_id, slot, physical_id, *expected))


def _lifecycle_values(physical, *, derivative=False):
    present = bool(physical['relative_path'])
    verified = int(present and physical['state'] == 'verified')
    readable = int(present and physical['state'] in ('verified', 'unknown'))
    return (0, 0, readable) if derivative else (verified, readable, 0)


def _lifecycle_physical(connection, table, identity, physical):
    prefix = 'asset:' if table == 'assets' else 'derivative:'
    expected = _lifecycle_values(physical, derivative=table == 'derivatives')
    for revision_id, slot, verified, readable, gif in connection.exec_driver_sql(
            'SELECT revision_id,slot,verified,readable,gif FROM read_revision_lifecycle_facts WHERE physical_id=? AND substr(slot,1,?)=?',
            (identity, len(prefix), prefix)).all():
        if not connection.exec_driver_sql('SELECT 1 FROM read_revision_lifecycle_totals WHERE revision_id=?', (revision_id,)).first():
            raise store.ProjectionUnavailable('revision lifecycle fact is missing; explicit repair is required')
        if (verified, readable, gif) != expected:
            _lifecycle_fact(connection, revision_id, slot, identity, expected)


def _lifecycle_resources(connection, revision, *, dry_run=False):
    required = "b.role IN ('ugoira_zip','ugoira_timing')" if revision.type == 'ugoira' else "b.role='image' AND b.page_index<?"
    params = (revision.id,) if revision.type == 'ugoira' else (revision.id, revision.page_count)
    facts = {f"asset:{row['role']}:{row['page_index']}": (row['id'], _lifecycle_values(row))
        for row in connection.exec_driver_sql(f'SELECT a.id,b.role,b.page_index,a.state,a.relative_path FROM revision_assets b JOIN assets a ON a.id=b.asset_id WHERE b.revision_id=? AND ({required})', params).mappings()}
    if revision.type == 'ugoira':
        for row in connection.exec_driver_sql("SELECT d.id,b.kind,b.page_index,b.spec_key,d.state,d.relative_path FROM revision_derivatives b JOIN derivatives d ON d.id=b.derivative_id WHERE b.revision_id=? AND b.kind='gif'", (revision.id,)).mappings():
            facts[f"derivative:{row['kind']}:{row['page_index']}:{row['spec_key']}"] = (row['id'], _lifecycle_values(row, derivative=True))
    if dry_run:
        return facts
    existing = {row[0]: (row[1], tuple(row[2:])) for row in connection.exec_driver_sql('SELECT slot,physical_id,verified,readable,gif FROM read_revision_lifecycle_facts WHERE revision_id=?', (revision.id,))}
    for slot in existing.keys() - facts.keys():
        _lifecycle_fact(connection, revision.id, slot)
    for slot, (physical, values) in facts.items():
        if existing.get(slot) != (physical, values):
            _lifecycle_fact(connection, revision.id, slot, physical, values)
    connection.exec_driver_sql('INSERT OR IGNORE INTO read_revision_lifecycle_totals(revision_id) VALUES(?)', (revision.id,))
    totals = tuple(sum(fact[1][index] for fact in facts.values()) for index in range(3))
    connection.exec_driver_sql('UPDATE read_revision_lifecycle_totals SET verified=?,readable=?,gif=? WHERE revision_id=?', (*totals, revision.id))
    connection.exec_driver_sql("DELETE FROM read_model_dirty WHERE entity='lifecycle_revision' AND identity=?", (revision.id,))


def cached_revision_facts(session, revision):
    """Same-writer lifecycle facts; structural changes explicitly refresh P."""
    connection = session.connection()
    pending = connection.exec_driver_sql("SELECT 1 FROM read_model_dirty WHERE entity='lifecycle_revision' AND identity=?", (revision.id,)).first()
    totals = connection.exec_driver_sql('SELECT verified,readable,gif FROM read_revision_lifecycle_totals WHERE revision_id=?', (revision.id,)).first()
    if pending:
        _lifecycle_resources(connection, revision)
        totals = connection.exec_driver_sql('SELECT verified,readable,gif FROM read_revision_lifecycle_totals WHERE revision_id=?', (revision.id,)).first()
    elif totals is None:
        raise store.ProjectionUnavailable('revision lifecycle fact is missing; explicit repair is required')
    expected = 2 if revision.type == 'ugoira' else revision.page_count
    complete = totals[0] == expected
    viewable = totals[2] > 0 if revision.type == 'ugoira' else totals[1] > 0
    return {'complete': complete, 'viewable': viewable, 'state': 'complete' if complete else 'partial' if viewable else 'pending'}


def _work_resources(connection, work, *, dry_run=False):
    required = "slot.role IN ('ugoira_zip','ugoira_timing') AND slot.page_index=0" if work.type == "ugoira" else "slot.role='image' AND slot.page_index<?"
    params = (work.id,) if work.type == "ugoira" else (work.id, work.page_count)
    if work.default_revision_id is None:
        rows = connection.exec_driver_sql(f"SELECT slot.id,slot.role,slot.page_index,slot.state,slot.relative_path FROM assets slot WHERE slot.work_id=? AND slot.revision_id IS NULL AND ({required})", params).mappings()
        derivatives = connection.exec_driver_sql('SELECT id,kind,page_index,spec_key,state,relative_path FROM derivatives WHERE work_id=? AND revision_id IS NULL', (work.id,)).mappings()
    else:
        params = (work.default_revision_id,) if work.type == "ugoira" else (work.default_revision_id, work.page_count)
        rows = connection.exec_driver_sql(f"SELECT a.id,slot.role,slot.page_index,a.state,a.relative_path FROM revision_assets slot JOIN assets a ON a.id=slot.asset_id WHERE slot.revision_id=? AND ({required})", params).mappings()
        # Derivative readiness uses the physical file's shape and owner, while
        # original counts use logical binding shape, as in the legacy query.
        derivatives = connection.exec_driver_sql('SELECT DISTINCT d.id,d.kind,d.page_index,d.spec_key,d.state,d.relative_path FROM revision_derivatives b JOIN derivatives d ON d.id=b.derivative_id WHERE b.revision_id=? AND d.work_id=?', (work.default_revision_id, work.id)).mappings()
    facts = _asset_facts(rows)
    for row in derivatives:
        values = _derivative_values(row)
        if values is not None:
            facts["derivative:" + row["id"]] = (row["id"], values)
    if dry_run:
        return facts
    _replace_resources(connection, f"work:{work.id}", facts)


def _revision_resources(connection, revision, *, dry_run=False):
    required = "slot.role IN ('ugoira_zip','ugoira_timing')" if revision.type == "ugoira" else "slot.role='image' AND slot.page_index<?"
    params = (revision.id,) if revision.type == "ugoira" else (revision.id, revision.page_count)
    rows = connection.exec_driver_sql(f"SELECT a.id,slot.role,slot.page_index,a.state,a.relative_path FROM revision_assets slot JOIN assets a ON a.id=slot.asset_id WHERE slot.revision_id=? AND ({required})", params).mappings()
    facts = _asset_facts(rows)
    if dry_run:
        return facts
    _replace_resources(connection, "revision:" + revision.id, facts)
    if connection.exec_driver_sql("SELECT 1 FROM read_model_dirty WHERE entity='lifecycle_revision' AND identity=?", (revision.id,)).first() or not connection.exec_driver_sql('SELECT 1 FROM read_revision_lifecycle_totals WHERE revision_id=?', (revision.id,)).first():
        _lifecycle_resources(connection, revision)


def _drop_scope(connection, scope):
    connection.exec_driver_sql('DELETE FROM read_resource_facts WHERE scope=?', (scope,))
    connection.exec_driver_sql('DELETE FROM read_resource_totals WHERE scope=?', (scope,))
    if scope.startswith('revision:'):
        connection.exec_driver_sql('DELETE FROM read_revision_lifecycle_facts WHERE revision_id=?', (scope[9:],))
        connection.exec_driver_sql('DELETE FROM read_revision_lifecycle_totals WHERE revision_id=?', (scope[9:],))


def _current_states(session, works, *, source_ids=None):
    from .archive_state import _source_update
    connection = session.connection()
    result = {}
    for work in works.values():
        row = connection.exec_driver_sql('SELECT recorded,verified,missing,thumbnail,gif FROM read_resource_totals WHERE scope=?', (f'work:{work.id}',)).first()
        if row is None and connection.exec_driver_sql('SELECT 1 FROM read_resource_facts WHERE scope=? LIMIT 1', (f'work:{work.id}',)).first():
            raise store.ProjectionUnavailable('resource aggregate is missing; explicit repair is required')
        recorded, verified, bad, thumbnail, gif = tuple(row) if row else (0, 0, 0, 0, 0)
        total = 2 if work.type == 'ugoira' else work.page_count
        missing = bad + max(0, total - recorded)
        unknown = total - verified - missing
        archive = {'state': 'partial' if missing else 'unknown' if unknown else 'complete',
            'total': total, 'verified': verified, 'missing': missing, 'unknown': unknown}
        value = connection.exec_driver_sql('SELECT value FROM application_state WHERE key=?', (f'maintenance_work:{work.id}',)).scalar()
        value = json.loads(value) if value is not None else {}
        ledger = value if isinstance(value, dict) else {}
        if source_ids is None or work.id in source_ids:
            source_update = _source_update(work)
        else:
            previous = connection.exec_driver_sql('SELECT value FROM read_work_state WHERE work_id=?', (work.id,)).scalar()
            if previous is None:
                raise store.ProjectionUnavailable('current source state is missing; explicit repair is required')
            source_update = json.loads(previous)['source_update']
        result[work.id] = {'archive': archive, 'source_update': source_update}
    return result


def reconcile_task_states(connection, work_ids):
    """Task facts are combined at read time; resource/source caches stay separate."""
    return None


def reconcile_works(session, work_ids, *, resource_ids=None, display_ids=None, raw_state_reader=None, raw_revision_reader=None):
    """Refresh K states; resource fanout only for captured resource/shape changes.

    Task and ledger changes use persistent totals and indexed winners. Explicit
    rebuild callers leave resource_ids unspecified to refresh all current scopes.
    """
    with diagnostics.span("projection.work.batch") as detail:
        ids = sorted(set(work_ids))
        detail['count'] = len(ids)
        if not ids:
            return
        if len(ids) > 200:
            for start in range(0, len(ids), 200):
                reconcile_works(session, ids[start:start + 200], resource_ids=resource_ids, display_ids=display_ids, raw_state_reader=raw_state_reader)
            return
        resources = set(ids) if resource_ids is None else resource_ids
        displays = set(ids) if display_ids is None else set(display_ids)
        detail.update(resource_count=len(resources), display_count=len(displays))
        connection = session.connection()
        fields = (Work.id, Work.pixiv_illust_id, Work.artist_id, Work.title, Work.type, Work.page_count,
            Work.posted_at, Work.downloaded_at, Work.default_revision_id, Work.latest_revision_id)
        works = {work.id: work for work in session.scalars(select(Work).where(Work.id.in_(ids))
            .options(load_only(*fields, raiseload=True)).execution_options(populate_existing=True))}
        selected_source_ids = sorted(set(works) & displays)
        if selected_source_ids:
            with diagnostics.span("projection.work.display", count=len(selected_source_ids)):
                for work_id, metadata in session.execute(select(Work.id, Work.source_metadata).where(Work.id.in_(selected_source_ids))):
                    set_committed_value(works[work_id], 'source_metadata', metadata)
        hidden = set(session.scalars(select(LocalPreference.work_id).where(LocalPreference.work_id.in_(ids), LocalPreference.is_hidden.is_(True))))
        favorites = set(session.scalars(select(RemoteBookmark.work_id).where(RemoteBookmark.work_id.in_(ids), RemoteBookmark.value.is_(True))))
        suppressed = set(session.scalars(select(Work.id).join(ArchiveIntent, ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(Work.id.in_(ids), ArchiveIntent.mode == 'suppressed')))
        for work in works.values():
            if work.id in resources:
                with diagnostics.span("projection.work.resources", work=diagnostics.ref(work.id)):
                    _work_resources(connection, work)
        with diagnostics.span("projection.work.states", count=len(works)):
            states = _current_states(session, works, source_ids=displays)
        for work_id in ids:
            work = works.get(work_id)
            if work is None:
                store.work_fact(connection, work_id)
                connection.exec_driver_sql('DELETE FROM read_work_state WHERE work_id=?', (work_id,))
                _drop_scope(connection, f"work:{work_id}")
                continue
            store.work_fact(connection, work.id, order_key=work.posted_at.isoformat() if work.posted_at else '',
                visible=work.id not in hidden and work.id not in suppressed, artist_id=work.artist_id,
                work_type=work.type, favorite=work.id in favorites, title=work.title,
                added_key=work.downloaded_at.isoformat())
            connection.exec_driver_sql('INSERT INTO read_work_state VALUES(?,?) ON CONFLICT(work_id) DO UPDATE SET value=excluded.value',
                (work.id, json.dumps(states[work.id], ensure_ascii=False)))

def reconcile_revisions(session, revision_ids, *, resource_ids=None, raw_revision_reader=None):
    with diagnostics.span("projection.revision.batch") as detail:
        connection = session.connection()
        resources = None if resource_ids is None else set(resource_ids)
        ids = sorted(set(revision_ids))
        detail.update(count=len(ids), resource_count=None if resources is None else len(resources))
        for revision_id in ids:
            statement = select(WorkRevision).where(WorkRevision.id == revision_id)
            if raw_revision_reader is None:
                statement = statement.options(load_only(WorkRevision.id, WorkRevision.work_id,
                    WorkRevision.type, WorkRevision.page_count, raiseload=True))
            revision = session.scalar(statement.execution_options(populate_existing=True))
            if revision is None:
                connection.exec_driver_sql('DELETE FROM read_revision_state WHERE revision_id=?', (revision_id,))
                _drop_scope(connection, "revision:" + revision_id)
                continue
            if resources is None or revision.id in resources:
                with diagnostics.span("projection.revision.resources", revision=diagnostics.ref(revision.id)):
                    _revision_resources(connection, revision)
            row = connection.exec_driver_sql('SELECT recorded,verified,missing FROM read_resource_totals WHERE scope=?', ('revision:' + revision.id,)).first()
            recorded, verified, bad = tuple(row) if row else (0, 0, 0)
            total = 2 if revision.type == 'ugoira' else revision.page_count
            missing = bad + max(0, total - recorded)
            unknown = max(0, total - verified - missing)
            value = {'state': 'partial' if missing else 'unknown' if unknown else 'complete',
                'total': total, 'verified': verified, 'missing': missing, 'unknown': unknown}
            if raw_revision_reader is not None:
                value = raw_revision_reader(session, revision)
            connection.exec_driver_sql('INSERT INTO read_revision_state VALUES(?,?) ON CONFLICT(revision_id) DO UPDATE SET value=excluded.value',
                (revision.id, json.dumps(value, ensure_ascii=False)))



def maintain_projections(session):
    """Consume this writer's trigger queue before commit; never commits itself."""
    with diagnostics.span("projection.flush"):
        session.flush()
    connection = session.connection()
    with diagnostics.span("projection.dirty") as detail:
        dirty = connection.exec_driver_sql('SELECT entity,identity FROM read_model_dirty').all()
        detail['count'] = len(dirty)
    if not dirty:
        return {"works": 0, "revisions": 0, "operations": 0, "items": 0}
    entities = {}
    for entity, identity in dirty:
        entities.setdefault(entity, set()).add(identity)
    from .source_read_projection import DIRTY_ENTITIES
    if set(entities) - ({"work", "resource_work", "revision", "lifecycle_revision", "physical_asset", "physical_derivative", "operation", "item", "scope", "scope_definition", "generation", "task_status", "display_work", "display_revision", "card_work", "card_artist"} | DIRTY_ENTITIES):
        raise store.ProjectionUnavailable('unrecognized dirty source identity; explicit repair is required')
    works = {int(value) for value in entities.get("work", ())}
    task_works = set()
    operations = entities.get("operation", set())
    with diagnostics.span("projection.operations", count=len(operations)) as detail:
        for operation_id in sorted(operations):
            task_works.update(reconcile_operation(connection, operation_id))
        detail['affected_count'] = len(task_works)
    with diagnostics.span("projection.items", count=len(entities.get("item", ()))):
        for item_id in sorted(entities.get("item", ())):
            task_works.update(reconcile_item(connection, item_id))
    with diagnostics.span("projection.scopes", count=len(entities.get("scope", set()))):
        for operation_id in sorted(entities.get("scope", set())):
            task_works.update(reconcile_scope(connection, operation_id, membership_unchanged=True))
    resources = {int(value) for value in entities.get("resource_work", ())}
    for operation_id in sorted(entities.get('scope_definition',set())):
        tasks.reconcile_scope(connection,operation_id,membership_unchanged=False)
    with diagnostics.span("projection.physical", asset_count=len(entities.get('physical_asset', ())),
            derivative_count=len(entities.get('physical_derivative', ()))) as detail:
        physical_works, physical_revisions = reconcile_physical_resources(connection,
            entities.get('physical_asset', ()), entities.get('physical_derivative', ()))
        detail.update(work_count=len(physical_works), revision_count=len(physical_revisions))
    works.update(physical_works)
    displays = resources | {int(value) for value in entities.get('source_work', ())} | {int(value) for value in entities.get('display_work', ())}
    works.update(resources)
    works.update(displays)
    with diagnostics.span("projection.works", count=len(works), resource_count=len(resources), display_count=len(displays)):
        reconcile_works(session, works, resource_ids=resources, display_ids=displays)
    reconcile_task_states(connection, task_works - works)
    revision_resources = entities.get("revision", set())
    revisions = revision_resources | physical_revisions
    with diagnostics.span("projection.revisions", count=len(revisions), resource_count=len(revision_resources)):
        reconcile_revisions(session, revisions, resource_ids=revision_resources)
    with diagnostics.span("projection.lifecycle", count=len(entities.get('lifecycle_revision', set()) - revision_resources)):
        for revision_id in entities.get('lifecycle_revision', set()) - revision_resources:
            revision = session.scalar(select(WorkRevision).where(WorkRevision.id == revision_id).options(load_only(WorkRevision.id, WorkRevision.type, WorkRevision.page_count, raiseload=True)))
            if revision is not None:
                _lifecycle_resources(connection, revision)
    from .source_read_projection import maintain as maintain_source_projection
    with diagnostics.span("projection.source", resource_count=len(resources)):
        maintain_source_projection(connection, work_ids=resources)
    from .display_metadata import maintain as maintain_display_metadata
    with diagnostics.span("projection.display"):
        maintain_display_metadata(connection)
    from .gallery_card_projection import reconcile_cards
    card_ids = works | {int(value) for value in entities.get("card_work", ())}
    for artist_id in entities.get("card_artist", ()):
        card_ids.update(connection.exec_driver_sql(
            "SELECT work_id FROM read_gallery_cards WHERE artist_id=?", (int(artist_id),)).scalars())
    with diagnostics.span("projection.cards", count=len(card_ids)):
        reconcile_cards(connection, card_ids)
    # Delete precisely the captured identities after every reconciler succeeds.
    with diagnostics.span("projection.finalize", count=len(dirty)):
        connection.exec_driver_sql('DELETE FROM read_model_dirty')
        store.finish_verified_batch(connection, advance_generation="generation" in entities)
    if set(entities) & {"generation", "work", "resource_work", "source_work", "source_local_work",
                        "source_artist", "display_work", "display_revision", "card_work", "card_artist"}:
        session.info['catalogue_data_changed'] = True
    if "generation" in entities and 'catalogue_change_generation' in session.info:
        # Keep the original captured identities before task/artist expansion.
        # An artist-name-only change intentionally sends an empty work set:
        # complex filters still need to discard the previous generation.
        changed_ids = session.info.setdefault('catalogue_change_work_ids', set())
        for entity in ("work", "resource_work", "source_work", "display_work", "card_work"):
            changed_ids.update(int(value) for value in entities.get(entity, ()))
        session.info['catalogue_change_generation'] += 1
    return {"works": len(works | task_works), "revisions": len(revisions),
        "operations": len(operations), "items": len(entities.get("item", ()))}


def rebuild_projections(session):
    """Explicit candidate backfill/repair; source records remain untouched."""
    session.flush()
    connection = session.connection()
    connection.exec_driver_sql('UPDATE read_model_meta SET ready=0 WHERE id=1')
    for table in ("read_resource_facts", "read_resource_totals", "read_work_order", "read_common_counts",
                  "read_task_candidates", "read_operation_scope", 'read_revision_lifecycle_facts', 'read_revision_lifecycle_totals', *STATE_TABLES):
        connection.exec_driver_sql(f'DELETE FROM {table}')
    from .task_read_projection import rebuild_task_read_projection
    rebuild_task_read_projection(connection)
    tasks.backfill(connection)
    ids = list(session.scalars(select(Work.id).order_by(Work.id)))
    # Bounded batches avoid SQLite variable limits and large ORM retention.
    for start in range(0, len(ids), 200):
        reconcile_works(session, ids[start:start + 200])
    reconcile_revisions(session, session.scalars(select(WorkRevision.id)).all())
    from .task_read_projection import rebuild_task_read_projection
    rebuild_task_read_projection(connection)
    from .source_read_projection import rebuild as rebuild_source_projection
    rebuild_source_projection(connection)
    from .display_metadata import rebuild as rebuild_display_metadata
    rebuild_display_metadata(session)
    from .gallery_card_projection import backfill as backfill_gallery_cards
    backfill_gallery_cards(connection)
    connection.exec_driver_sql('DELETE FROM read_model_dirty')
    store.finish_verified_batch(connection)
    return {"works": len(ids), "revisions": connection.exec_driver_sql('SELECT count(*) FROM read_revision_state').scalar()}


def _verify_resource_scope(connection, scope, expected):
    actual = {}
    for row in connection.exec_driver_sql('SELECT slot,physical_id,recorded,verified,missing,thumbnail,gif FROM read_resource_facts WHERE scope=?', (scope,)):
        actual[row[0]] = (row[1], dict(zip(store._FIELDS, row[2:])))
    normalized = {slot: (physical, {key: values.get(key, 0) for key in store._FIELDS})
        for slot, (physical, values) in expected.items()}
    if actual != normalized:
        raise store.ProjectionUnavailable('resource source fact mismatch')


def _verify_tasks(connection):
    return tasks.verify(connection)


def verify_projections(session):
    """Explicit full candidate validation; this is never an ordinary read path."""
    from .archive_state import raw_work_states
    from .library import Library
    connection = session.connection()
    validate_projection_schema(connection)
    generation = store.require_ready(connection)
    # Empty source scopes may legitimately omit a totals row, but every
    # nonempty factual scope requires its aggregate. Check both directions:
    # beginning only from totals would overlook a deleted aggregate entirely.
    if connection.exec_driver_sql('''SELECT 1 FROM read_resource_facts f
        LEFT JOIN read_resource_totals t ON t.scope=f.scope
        WHERE t.scope IS NULL LIMIT 1''').first():
        raise store.ProjectionUnavailable('resource aggregate is missing')
    if connection.exec_driver_sql('''SELECT scope FROM
        (SELECT scope FROM read_resource_facts UNION SELECT scope FROM read_resource_totals)
        WHERE scope NOT IN
        (SELECT 'work:'||id FROM works UNION ALL SELECT 'revision:'||id FROM work_revisions)
        LIMIT 1''').first():
        raise store.ProjectionUnavailable('orphan resource scope')
    source_ids = set(session.scalars(select(Work.id)))
    projected_ids = set(connection.exec_driver_sql('SELECT work_id FROM read_work_state').scalars())
    ordered_ids = set(connection.exec_driver_sql('SELECT work_id FROM read_work_order').scalars())
    if source_ids != projected_ids or source_ids != ordered_ids:
        raise store.ProjectionUnavailable('work projection membership mismatch')
    revision_ids = set(session.scalars(select(WorkRevision.id)))
    if revision_ids != set(connection.exec_driver_sql('SELECT revision_id FROM read_revision_state').scalars()):
        raise store.ProjectionUnavailable('revision projection membership mismatch')
    if revision_ids != set(connection.exec_driver_sql('SELECT revision_id FROM read_revision_lifecycle_totals').scalars()):
        raise store.ProjectionUnavailable('revision lifecycle membership mismatch')
    if connection.exec_driver_sql('SELECT 1 FROM read_revision_lifecycle_facts WHERE revision_id NOT IN (SELECT id FROM work_revisions) LIMIT 1').first():
        raise store.ProjectionUnavailable('orphan revision lifecycle fact')
    # Compare archive/processing values to the independent source reader, which
    # retains the original JSON identity selection and winner semantics.
    hidden = set(session.scalars(select(LocalPreference.work_id).where(LocalPreference.is_hidden.is_(True))))
    favorites = set(session.scalars(select(RemoteBookmark.work_id).where(RemoteBookmark.value.is_(True))))
    suppressed = set(session.scalars(select(Work.id).join(ArchiveIntent, ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(ArchiveIntent.mode == 'suppressed')))
    ids = sorted(source_ids)
    for start in range(0, len(ids), 200):
        works = list(session.scalars(select(Work).where(Work.id.in_(ids[start:start + 200]))))
        expected, _ = raw_work_states(session, works)
        if expected != read_work_states(session, works):
            raise store.ProjectionUnavailable('current work state mismatch')
        for work in works:
            expected_order = (work.posted_at.isoformat() if work.posted_at else '',
                int(work.id not in hidden and work.id not in suppressed), work.artist_id, work.type, int(work.id in favorites),
                connection.exec_driver_sql('SELECT lower(?)', (work.title,)).scalar(), work.downloaded_at.isoformat())
            actual_order = connection.exec_driver_sql('SELECT order_key,visible,artist_id,work_type,favorite,title_key,added_key FROM read_work_order WHERE work_id=?', (work.id,)).first()
            if tuple(actual_order) != expected_order:
                raise store.ProjectionUnavailable('work ordering/filter fact mismatch')
            _verify_resource_scope(connection, f'work:{work.id}', _work_resources(connection, work, dry_run=True))
    for revision in session.scalars(select(WorkRevision)):
        if Library._raw_revision_archive(session, revision) != read_revision_state(session, revision.id):
            raise store.ProjectionUnavailable('revision archive state mismatch')
        _verify_resource_scope(connection, "revision:" + revision.id, _revision_resources(connection, revision, dry_run=True))
        lifecycle = _lifecycle_resources(connection, revision, dry_run=True)
        actual_lifecycle = {row[0]: (row[1], tuple(row[2:])) for row in connection.exec_driver_sql('SELECT slot,physical_id,verified,readable,gif FROM read_revision_lifecycle_facts WHERE revision_id=?', (revision.id,))}
        if lifecycle != actual_lifecycle:
            raise store.ProjectionUnavailable('revision lifecycle source fact mismatch')
        expected_lifecycle_totals = tuple(sum(fact[1][index] for fact in lifecycle.values()) for index in range(3))
        actual_lifecycle_totals = tuple(connection.exec_driver_sql('SELECT verified,readable,gif FROM read_revision_lifecycle_totals WHERE revision_id=?', (revision.id,)).one())
        if expected_lifecycle_totals != actual_lifecycle_totals:
            raise store.ProjectionUnavailable('revision lifecycle aggregate mismatch')
        from .revisions import revision_facts
        total = 2 if revision.type == 'ugoira' else revision.page_count
        complete = actual_lifecycle_totals[0] == total
        viewable = actual_lifecycle_totals[2 if revision.type == 'ugoira' else 1] > 0
        if revision_facts(session, revision) != {'complete': complete, 'viewable': viewable, 'state': 'complete' if complete else 'partial' if viewable else 'pending'}:
            raise store.ProjectionUnavailable('revision lifecycle oracle mismatch')
    _verify_tasks(connection)
    from .task_read_projection import verify_task_read_projection
    verify_task_read_projection(connection)
    from .source_read_projection import verify as verify_source_projection
    verify_source_projection(connection)
    from .display_metadata import verify as verify_display_metadata
    verify_display_metadata(session)
    mismatched = connection.exec_driver_sql('''SELECT t.scope FROM read_resource_totals t LEFT JOIN
        (SELECT scope,sum(recorded) recorded,sum(verified) verified,sum(missing) missing,sum(thumbnail) thumbnail,sum(gif) gif
         FROM read_resource_facts GROUP BY scope) f ON f.scope=t.scope
        WHERE t.recorded<>coalesce(f.recorded,0) OR t.verified<>coalesce(f.verified,0)
           OR t.missing<>coalesce(f.missing,0) OR t.thumbnail<>coalesce(f.thumbnail,0) OR t.gif<>coalesce(f.gif,0) LIMIT 1''').first()
    if mismatched:
        raise store.ProjectionUnavailable('resource aggregate mismatch')
    expected_counts = {"artists": connection.exec_driver_sql('SELECT count(*) FROM artists').scalar(), "visible": connection.exec_driver_sql('SELECT count(*) FROM read_work_order WHERE visible=1').scalar(),
        "favorite": connection.exec_driver_sql('SELECT count(*) FROM read_work_order WHERE visible=1 AND favorite=1').scalar()}
    for column, prefix in (("artist_id", "artist"), ("work_type", "type")):
        for key, count in connection.exec_driver_sql(f'SELECT {column},count(*) FROM read_work_order WHERE visible=1 GROUP BY {column}'):
            expected_counts[f'{prefix}:{key}'] = count
    actual = dict(connection.exec_driver_sql('SELECT scope,value FROM read_common_counts').all())
    if any(actual.get(key, 0) != value for key, value in expected_counts.items()) or any(value != expected_counts.get(key, 0) for key, value in actual.items()):
        raise store.ProjectionUnavailable('common count mismatch')
    from .gallery_card_projection import verify as verify_gallery_cards
    verify_gallery_cards(connection)
    return {"works": len(source_ids), "revisions": len(revision_ids), "generation": generation}


_HISTORY_WINNER_LOCK = __import__('threading').Lock()
_HISTORY_WINNER_KEY = None
_HISTORY_WINNER_MAP = {}


def _cached_task_winners(session, connection, selected, history_ids):
    global _HISTORY_WINNER_KEY, _HISTORY_WINNER_MAP
    active = tasks._read_winners(connection, selected, active_only=True) if tasks.has_active_local(connection) else {}
    pending = [work_id for work_id in selected if work_id in history_ids and work_id not in active]
    if not pending:
        return active
    key = (id(session.bind), session.info.get('read_projection_generation'), tasks._TASK_PROJECTION_GENERATION)
    with _HISTORY_WINNER_LOCK:
        if _HISTORY_WINNER_KEY != key:
            _HISTORY_WINNER_KEY = key
            _HISTORY_WINNER_MAP = {}
        cached = {work_id: _HISTORY_WINNER_MAP[work_id] for work_id in pending if work_id in _HISTORY_WINNER_MAP}
    uncached = [work_id for work_id in pending if work_id not in cached]
    if uncached:
        fetched = tasks._read_winners(connection, uncached, active_only=False)
        with _HISTORY_WINNER_LOCK:
            if _HISTORY_WINNER_KEY == key:
                for work_id in uncached:
                    _HISTORY_WINNER_MAP[work_id] = fetched.get(work_id)
        for work_id in uncached:
            cached[work_id] = fetched.get(work_id)
    result = dict(active)
    for work_id, winner in cached.items():
        if winner is not None:
            result[work_id] = winner
    return result


def read_work_states(session, works):
    """Compose current parent state for only the requested Work identities."""
    from .archive_state import _processing, _recovery
    works = list(works)
    connection = session.connection()
    store.session_generation(session)
    ids = sorted({work.id for work in works})
    result = {}
    idle = SimpleNamespace(item_state=None, operation_state=None, operation_id=None, reason=None, concern=None)
    for start in range(0, len(ids), 240):
        selected = ids[start:start + 240]
        placeholders = ','.join('?' for _ in selected)
        with diagnostics.span('read.work_states.execute', selected_count=len(selected)):
            rows = connection.exec_driver_sql(f'''SELECT s.work_id,s.value,o.work_type,r.thumbnail,r.gif,l.value
            FROM read_work_state s JOIN read_work_order o ON o.work_id=s.work_id
            LEFT JOIN read_resource_totals r ON r.scope='work:'||s.work_id
            LEFT JOIN application_state l ON l.key='maintenance_work:'||s.work_id
            WHERE s.work_id IN ({placeholders})''', tuple(selected))
        observing = diagnostics.enabled()
        consumed, decode_seconds = 0, 0.0
        snapshots, history_ids = {}, set()
        with diagnostics.span('read.work_states.consume', selected_count=len(selected)) as observation:
            try:
                for work_id, encoded, work_type, thumbnail, gif, ledger_value in rows:
                    consumed += 1
                    began = time.perf_counter() if observing else 0
                    try:
                        value = json.loads(encoded)
                        archive, source_update = value['archive'], value['source_update']
                        ledger = json.loads(ledger_value) if ledger_value is not None else {}
                        ledger = ledger if isinstance(ledger, dict) else {}
                        local_gap = not thumbnail or (work_type == 'ugoira' and not gif)
                        snapshots[work_id] = (value, ledger, local_gap)
                        if archive['missing'] or local_gap or source_update['pending']:
                            history_ids.add(work_id)
                    finally:
                        if observing:
                            decode_seconds += time.perf_counter() - began
            finally:
                observation.update(row_count=consumed, decode_ms=decode_seconds * 1000)
        # Historical failures only affect processing while a relevant gap
        # remains. Active work is still composed for every requested identity.
        if 'read_projection_generation' in session.info:
            task_winners = _cached_task_winners(session, connection, selected, history_ids)
        else:
            task_winners = tasks.winners(connection, selected, history_ids=history_ids)
        with diagnostics.span('read.work_states.assemble', selected_count=len(selected)):
            for work_id, (value, ledger, local_gap) in snapshots.items():
                archive, source_update = value['archive'], value['source_update']
                winner = task_winners.get(work_id)
                history = SimpleNamespace(**winner['payload']) if winner is not None else idle
                processing = _processing(history, ledger, original_gap=bool(archive['missing']),
                    local_gap=local_gap, source_pending=source_update['pending'])
                result[work_id] = {**value, 'processing': processing,
                    'recovery': _recovery(ledger, processing, archive)}
    if set(result) != set(ids):
        raise store.ProjectionUnavailable('work state is missing; explicit repair is required')
    return result


def read_revision_state(session, revision_id):
    connection = session.connection()
    store.session_generation(session)
    value = connection.exec_driver_sql('SELECT value FROM read_revision_state WHERE revision_id=?', (revision_id,)).scalar()
    if value is None:
        raise store.ProjectionUnavailable('revision state is missing; explicit repair is required')
    return json.loads(value)
