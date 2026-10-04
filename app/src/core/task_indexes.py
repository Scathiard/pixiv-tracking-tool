"""Additive SQLite task lookup indexes; callers own backup and transactions."""
from sqlalchemy import Text, cast, func, literal_column

TASK_TARGET_PATH_SQL = {
    "work_id": "'$.work_id'",
    "asset_id": "'$.asset_id'",
    "record_id": "'$.record_id'",
}
TASK_TARGET_SQL = {
    "work_id": "json_extract(target, '$.work_id')",
    "asset_id": "CAST(json_extract(target, '$.asset_id') AS TEXT)",
    "record_id": "CAST(json_extract(target, '$.record_id') AS TEXT)",
}

TASK_INDEX_SQL = {
    "ix_items_target_work_asset": "CREATE INDEX ix_items_target_work_asset ON operation_items ("
        + TASK_TARGET_SQL["work_id"] + ", " + TASK_TARGET_SQL["asset_id"] + ")",
    "ix_items_target_work_record": "CREATE INDEX ix_items_target_work_record ON operation_items ("
        + TASK_TARGET_SQL["work_id"] + ", " + TASK_TARGET_SQL["record_id"] + ")",
    "ix_items_target_scope": "CREATE INDEX ix_items_target_scope ON operation_items (operation_id, "
        + ", ".join(TASK_TARGET_SQL.values()) + ", state, position, id) WHERE "
        + " AND ".join(expression + " IS NULL" for expression in TASK_TARGET_SQL.values()),
}


def task_target(target_column, key):
    """Fixed SQL paths match index expressions, preserving identity affinity."""
    value = func.json_extract(target_column, literal_column(TASK_TARGET_PATH_SQL[key]))
    # Asset/derivative IDs have TEXT affinity in their referenced tables. Raw
    # JSON scalars cannot use an expression index with a TEXT-affinity IN RHS.
    return value if key == "work_id" else cast(value, Text)


def create_task_indexes(connection):
    """Create only missing indexes; never commit or change schema revision."""
    existing = {name.casefold() for name in connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='operation_items'").scalars()}
    created = []
    for name, statement in TASK_INDEX_SQL.items():
        if name.casefold() not in existing:
            connection.exec_driver_sql(statement)
            created.append(name)
    return tuple(created)
