"""Read-only real query plans plus a small, independent in-memory comparison.

No real aggregation, statistics collection, database copy, account or network.
Keep this directory for the main agent's evidence selection; do not auto-clean.
"""
import argparse
import json
import sqlite3
from pathlib import Path


SUFFIX = """WHERE t.work_id NOT IN (
    SELECT w.id FROM archive_intents i
    JOIN works w ON w.pixiv_illust_id=i.pixiv_illust_id
    WHERE i.mode='suppressed') ORDER BY t.tag"""
QUERIES = {
    "original_tags": "SELECT DISTINCT t.tag FROM work_tags t " + SUFFIX,
    "sequential_tags": "SELECT DISTINCT t.tag FROM work_tags t NOT INDEXED " + SUFFIX,
    "translations": """SELECT d.scope,d.translations
        FROM read_display_metadata d JOIN read_source_work_local l
        ON l.work_id=CAST(substr(d.scope,6) AS INTEGER) AND l.included=1
        WHERE d.scope>='work:' AND d.scope<'work;'
        AND d.translations NOT IN ('null','{}') ORDER BY d.scope""",
}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert args.real_db.is_file()
    assert args.output.is_dir()
    with sqlite3.connect(args.real_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as real:
        real.execute("PRAGMA query_only=ON")
        schemas = {}
        for table in ("work_tags", "works", "archive_intents", "read_display_metadata", "read_source_work_local"):
            indexes = list(real.execute("PRAGMA index_list(" + table + ")"))
            schemas[table] = [{"index": row[1], "columns": list(real.execute(
                "PRAGMA index_info(" + row[1] + ")"))} for row in indexes]
        plans = {name: {"sql": sql, "plan": list(real.execute("EXPLAIN QUERY PLAN " + sql))}
                 for name, sql in QUERIES.items()}
        write(args.output / "real-query-plans.json", {"sqlite_version": sqlite3.sqlite_version,
            "scope": "read-only schema metadata and EXPLAIN QUERY PLAN; no real aggregation executed",
            "indexes": schemas, "queries": plans})

    with sqlite3.connect(":memory:") as synthetic:
        synthetic.executescript("""CREATE TABLE work_tags(work_id INTEGER NOT NULL,tag TEXT NOT NULL,
            PRIMARY KEY(work_id,tag)); CREATE INDEX ix_work_tags_tag ON work_tags(tag);
            CREATE TABLE works(id INTEGER PRIMARY KEY,pixiv_illust_id TEXT UNIQUE);
            CREATE TABLE archive_intents(pixiv_illust_id TEXT PRIMARY KEY,mode TEXT);""")
        synthetic.executemany("INSERT INTO works VALUES(?,?)", [(i, str(i)) for i in range(1, 601)])
        synthetic.executemany("INSERT INTO work_tags VALUES(?,?)", [
            (i, "tag-%04d" % ((i * 31 + j * 17) % 1800)) for i in range(1, 601) for j in range(12)])
        synthetic.executemany("INSERT INTO archive_intents VALUES(?,?)", [
            (str(i), "suppressed") for i in range(1, 13)])
        records = {}
        baseline = None
        for name in ("original_tags", "sequential_tags"):
            sql = QUERIES[name]
            bytecode = list(synthetic.execute("EXPLAIN " + sql))
            rows = list(synthetic.execute(sql))
            if baseline is None:
                baseline = rows
            records[name] = {"plan": list(synthetic.execute("EXPLAIN QUERY PLAN " + sql)),
                "deferred_seek_opcodes": sum(row[1] == "DeferredSeek" for row in bytecode),
                "result_count": len(rows), "equal_to_original": rows == baseline}
        assert records["original_tags"]["deferred_seek_opcodes"] == 1
        assert records["sequential_tags"]["deferred_seek_opcodes"] == 0
        assert records["sequential_tags"]["equal_to_original"]
        write(args.output / "synthetic-comparison.json", {"works": 600, "tag_relations": 7200,
            "suppressed_works": 12, "queries": records,
            "boundary": "In-memory comparison proves equivalent results and no per-tag table back-lookup; it does not measure real disk latency."})
    print("Real plans recorded without executing real aggregations; synthetic results equivalent (1800 tags), DeferredSeek 1 -> 0.")


if __name__ == "__main__":
    main()
