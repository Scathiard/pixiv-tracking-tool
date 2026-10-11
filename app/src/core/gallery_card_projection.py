"""Small, transactional default-version gallery cards.

Source records remain authoritative. Only explicit installation/backfill and
writer reconciliation read source tables; card reads never repair the cache.
Explicit detail/stream revision selection stays in revision_views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from urllib.parse import quote

from .media import CARD_MOTION_SPEC
from .read_projection import ProjectionUnavailable
from .source_status import availability_snapshot


TABLE = "read_gallery_cards"
_DDL = """CREATE TABLE read_gallery_cards (
    work_id INTEGER PRIMARY KEY,
    pixiv_id TEXT NOT NULL,
    artist_id INTEGER NOT NULL,
    artist_name TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK(page_count>=1),
    x_restrict INTEGER,
    posted_at TEXT,
    default_revision_id TEXT,
    revision_id TEXT,
    latest_revision_id TEXT,
    availability TEXT NOT NULL,
    first_page TEXT NOT NULL DEFAULT 'null',
    thumbnail TEXT NOT NULL DEFAULT 'null',
    motion TEXT NOT NULL DEFAULT 'null',
    gif TEXT NOT NULL DEFAULT 'null'
)"""
_RESOURCE_FIELDS = ("id", "work_id", "relative_path", "state", "sha256", "source_version")
_BASE_FIELDS = ("work_id", "pixiv_id", "artist_id", "artist_name", "title", "type",
                "page_count", "x_restrict", "posted_at", "default_revision_id",
                "revision_id", "latest_revision_id", "availability")
_SLOTS = ("first_page", "thumbnail", "motion", "gif")
_ARTIST_INDEX = "CREATE INDEX ix_read_gallery_card_artist ON read_gallery_cards(artist_id,work_id)"
TRIGGERS = {
    "read_card_artist_update": """CREATE TRIGGER read_card_artist_update
        AFTER UPDATE OF id,name ON artists WHEN OLD.id IS NOT NEW.id OR OLD.name IS NOT NEW.name
        BEGIN
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_artist',CAST(OLD.id AS TEXT));
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_artist',CAST(NEW.id AS TEXT));
        END""",
    "read_card_revision_title_update": """CREATE TRIGGER read_card_revision_title_update
        AFTER UPDATE OF title ON work_revisions WHEN OLD.title IS NOT NEW.title
        BEGIN
          INSERT OR IGNORE INTO read_model_dirty SELECT 'card_work',CAST(id AS TEXT)
          FROM works WHERE default_revision_id=OLD.id OR default_revision_id=NEW.id;
        END""",
    "read_card_asset_sha_update": """CREATE TRIGGER read_card_asset_sha_update
        AFTER UPDATE OF sha256 ON assets WHEN OLD.sha256 IS NOT NEW.sha256
        BEGIN
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_work',CAST(OLD.work_id AS TEXT));
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_work',CAST(NEW.work_id AS TEXT));
        END""",
    "read_card_derivative_sha_update": """CREATE TRIGGER read_card_derivative_sha_update
        AFTER UPDATE OF sha256,source_version ON derivatives
        WHEN OLD.sha256 IS NOT NEW.sha256 OR OLD.source_version IS NOT NEW.source_version
        BEGIN
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_work',CAST(OLD.work_id AS TEXT));
          INSERT OR IGNORE INTO read_model_dirty VALUES('card_work',CAST(NEW.work_id AS TEXT));
        END""",
}


@dataclass(frozen=True)
class CardResource:
    id: str
    work_id: int
    relative_path: str | None
    state: str
    sha256: str | None
    source_version: str | None


@dataclass
class GalleryCard:
    """Work-like narrow value; caller attaches current preference/bookmark."""
    id: int
    pixiv_illust_id: str
    artist_id: int
    artist_name: str
    title: str
    type: str
    page_count: int
    x_restrict: int | None
    posted_at: datetime | None
    default_revision_id: str | None
    revision_id: str | None
    latest_revision_id: str | None
    availability: dict | None
    first_page: CardResource | None = None
    thumbnail: CardResource | None = None
    motion: CardResource | None = None
    gif: CardResource | None = None
    preference: object | None = field(default=None, repr=False)
    bookmark: object | None = field(default=None, repr=False)

    @property
    def _read_metadata(self):
        return {"remote_availability": self.availability}


def _connection(owner):
    return owner if hasattr(owner, "exec_driver_sql") else owner.connection()


def install(connection):
    """Install with additive captures; caller owns candidate transaction."""
    connection = _connection(connection)
    connection.exec_driver_sql(_DDL)
    connection.exec_driver_sql(_ARTIST_INDEX)
    for statement in TRIGGERS.values():
        connection.exec_driver_sql(statement)


def _chunks(identities, size=480):
    ids = sorted(set(identities))
    for begin in range(0, len(ids), size):
        yield tuple(ids[begin:begin + size])


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _resources(connection, ids):
    """Resolve indexed logical page-zero slots, retaining physical ownership."""
    placeholders = ",".join("?" for _ in ids)
    assets = f"""SELECT c.work_id,a.id,a.work_id,a.relative_path,a.state,a.sha256,NULL
        FROM read_gallery_cards c JOIN assets a ON a.work_id=c.work_id
        AND a.revision_id IS NULL AND a.role='image' AND a.page_index=0
        WHERE c.work_id IN ({placeholders}) AND c.revision_id IS NULL
        UNION ALL
        SELECT c.work_id,a.id,a.work_id,a.relative_path,a.state,a.sha256,NULL
        FROM read_gallery_cards c JOIN revision_assets b ON b.revision_id=c.revision_id
        AND b.role='image' AND b.page_index=0
        JOIN assets a ON a.id=b.asset_id AND a.work_id=c.work_id
        WHERE c.work_id IN ({placeholders})"""
    result = {identity: {slot: None for slot in _SLOTS} for identity in ids}
    for row in connection.exec_driver_sql(assets, ids + ids):
        result[row[0]]["first_page"] = dict(zip(_RESOURCE_FIELDS, row[1:]))
    derivatives = f"""SELECT c.work_id,d.id,d.work_id,d.relative_path,d.state,d.sha256,d.source_version,
        d.kind,d.spec_key FROM read_gallery_cards c JOIN derivatives d ON d.work_id=c.work_id
        AND d.revision_id IS NULL AND d.page_index=0
        WHERE c.work_id IN ({placeholders}) AND c.revision_id IS NULL
        AND ((d.kind='thumbnail' AND d.spec_key IN ('v1','{CARD_MOTION_SPEC}'))
          OR (d.kind='gif' AND d.spec_key='v1'))
        UNION ALL
        SELECT c.work_id,d.id,d.work_id,d.relative_path,d.state,d.sha256,d.source_version,
        b.kind,b.spec_key FROM read_gallery_cards c JOIN revision_derivatives b
        ON b.revision_id=c.revision_id AND b.page_index=0
        JOIN derivatives d ON d.id=b.derivative_id AND d.work_id=c.work_id
        WHERE c.work_id IN ({placeholders})
        AND ((b.kind='thumbnail' AND b.spec_key IN ('v1','{CARD_MOTION_SPEC}'))
          OR (b.kind='gif' AND b.spec_key='v1'))"""
    for row in connection.exec_driver_sql(derivatives, ids + ids):
        slot = "gif" if row[7] == "gif" else "motion" if row[8] == CARD_MOTION_SPEC else "thumbnail"
        result[row[0]][slot] = dict(zip(_RESOURCE_FIELDS, row[1:7]))
    return result


def reconcile_cards(owner, work_ids):
    """Refresh only supplied Works in the caller's source-writing transaction.

    Run after display metadata maintenance. Resources are resolved from binding
    slot indexes, since resource totals intentionally omit card-motion details.
    Deletion removes only the corresponding cache row; no commit is performed.
    """
    connection = _connection(owner)
    for ids in _chunks(work_ids):
        placeholders = ",".join("?" for _ in ids)
        rows = list(connection.exec_driver_sql(f"""SELECT w.id,w.pixiv_illust_id,w.artist_id,a.name,
            CASE WHEN r.id IS NULL THEN w.title ELSE r.title END,
            CASE WHEN r.id IS NULL THEN w.type ELSE r.type END,
            CASE WHEN r.id IS NULL THEN w.page_count ELSE r.page_count END,
            w.x_restrict,w.posted_at,w.default_revision_id,r.id,w.latest_revision_id,m.availability
            FROM works w JOIN artists a ON a.id=w.artist_id
            LEFT JOIN work_revisions r ON r.id=w.default_revision_id AND r.work_id=w.id
            LEFT JOIN read_display_metadata m ON m.scope='work:'||w.id
            WHERE w.id IN ({placeholders})""", ids))
        if any(row[-1] is None for row in rows):
            raise ProjectionUnavailable("gallery card display metadata is missing")
        present = {row[0] for row in rows}
        absent = tuple(identity for identity in ids if identity not in present)
        if absent:
            connection.exec_driver_sql("DELETE FROM read_gallery_cards WHERE work_id IN ("
                + ",".join("?" for _ in absent) + ")", absent)
        if not rows:
            continue
        fields = ",".join(_BASE_FIELDS)
        updates = ",".join(f"{key}=excluded.{key}" for key in _BASE_FIELDS[1:])
        changed = " OR ".join(f"{key} IS NOT excluded.{key}" for key in _BASE_FIELDS[1:])
        connection.exec_driver_sql(f"INSERT INTO read_gallery_cards({fields}) VALUES("
            + ",".join("?" for _ in _BASE_FIELDS) + f") ON CONFLICT(work_id) DO UPDATE SET {updates} WHERE {changed}",
            [tuple(row) for row in rows])
        slots = _resources(connection, tuple(sorted(present)))
        values = [tuple(_json(slots[identity][slot]) for slot in _SLOTS) + (identity,)
                  for identity in sorted(present)]
        # SQLite suppresses unchanged card writes to avoid dirtying extra pages.
        connection.exec_driver_sql("UPDATE read_gallery_cards SET first_page=?,thumbnail=?,motion=?,gif=? "
            "WHERE work_id=? AND (first_page IS NOT ? OR thumbnail IS NOT ? OR motion IS NOT ? OR gif IS NOT ?)",
            [value + value[:4] for value in values])


def backfill(owner, *, batch_size=480):
    """Explicit keyset backfill, with bounded source rows and no whole-library ORM."""
    if not 1 <= batch_size <= 480:
        raise ValueError("gallery backfill batch size must be between 1 and 480")
    connection = _connection(owner)
    after, count = None, 0
    while True:
        statement = "SELECT id FROM works ORDER BY id LIMIT ?" if after is None else "SELECT id FROM works WHERE id>? ORDER BY id LIMIT ?"
        parameters = (batch_size,) if after is None else (after, batch_size)
        ids = list(connection.exec_driver_sql(statement, parameters).scalars())
        if not ids:
            break
        reconcile_cards(connection, ids)
        count += len(ids)
        after = ids[-1]
    return {"cards": count}


def read_cards(owner, work_ids):
    """Return Work-like dataclasses; missing rows fail without source fallback."""
    connection = _connection(owner)
    result = {}
    for ids in _chunks(work_ids):
        rows = connection.exec_driver_sql("SELECT * FROM read_gallery_cards WHERE work_id IN ("
            + ",".join("?" for _ in ids) + ")", ids).mappings()
        for row in rows:
            resources = {key: CardResource(**value) if (value := json.loads(row[key])) is not None else None
                         for key in _SLOTS}
            result[row["work_id"]] = GalleryCard(id=row["work_id"], pixiv_illust_id=row["pixiv_id"],
                **{key: row[key] for key in _BASE_FIELDS if key not in
                   ("work_id", "pixiv_id", "posted_at", "availability")},
                posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
                availability=json.loads(row["availability"]), **resources)
        if any(identity not in result for identity in ids):
            raise ProjectionUnavailable("gallery card is missing; explicit repair is required")
    return result


def _url(resource, *, animation=False):
    if resource is None or not resource.relative_path or resource.state == "invalid":
        return None
    if resource.state == "missing" and not animation:
        return None
    return "/images/" + quote(resource.relative_path, safe="/")


def card_view(card, account_id=None):
    """Static compact fields and the exact legacy thumbnail/motion SHA gates."""
    animation = card.type == "ugoira"
    preview = card.first_page
    if animation:
        source, preview = card.gif, card.motion
        if (preview is None or preview.state != "verified" or not preview.sha256 or source is None
                or source.state != "verified" or not source.sha256 or preview.source_version != source.sha256):
            preview = None
    preview_url = _url(preview, animation=animation)
    if animation and preview_url:
        preview_url += "?v=" + preview.sha256[:16]
    status = ("ready" if preview_url else "failed" if preview and preview.state == "failed"
              else "unavailable" if preview and preview.state in ("invalid", "mismatch") else "pending")
    thumbnail = card.thumbnail if card.thumbnail and card.thumbnail.state == "verified" else None
    thumbnail_url = "/images/" + quote(thumbnail.relative_path, safe="/") if thumbnail and thumbnail.relative_path else None
    if thumbnail_url and thumbnail.sha256:
        thumbnail_url += "?v=" + thumbnail.sha256[:16]
    return {"id": card.id, "pixiv_id": card.pixiv_illust_id, "artist_id": card.artist_id,
        "artist_name": card.artist_name, "title": card.title, "type": card.type,
        "page_count": card.page_count, "x_restrict": card.x_restrict,
        "posted_at": card.posted_at.isoformat() if card.posted_at else None,
        "revision_id": card.revision_id, "latest_revision_id": card.latest_revision_id,
        "availability": availability_snapshot(card.availability, account_id),
        "preview": preview_url, "preview_status": status, "thumbnail": thumbnail_url}


def verify(owner):
    """Schema/count/mapping checks over narrow cards; no physical file scans."""
    connection = _connection(owner)
    columns = tuple(row[1] for row in connection.exec_driver_sql("PRAGMA table_info(read_gallery_cards)"))
    if columns != _BASE_FIELDS + _SLOTS:
        raise ProjectionUnavailable("gallery card schema is missing or incompatible")
    from .index_preparation import _normalized_sql
    expected = {TABLE: _DDL, **TRIGGERS}
    actual_schema = {name: sql for name, sql in connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE name IN ("
        + ",".join("?" for _ in expected) + ")", tuple(expected))}
    if any(name not in actual_schema or tuple(_normalized_sql(actual_schema[name])) != tuple(_normalized_sql(sql))
           for name, sql in expected.items()):
        raise ProjectionUnavailable("gallery card schema objects are missing or incompatible")
    source = connection.exec_driver_sql("SELECT count(*) FROM works").scalar_one()
    actual = connection.exec_driver_sql("SELECT count(*) FROM read_gallery_cards").scalar_one()
    if source != actual or connection.exec_driver_sql("""SELECT 1 FROM works w
        LEFT JOIN read_gallery_cards c ON c.work_id=w.id WHERE c.work_id IS NULL LIMIT 1""").first():
        raise ProjectionUnavailable("gallery card identities differ from source")
    invalid = connection.exec_driver_sql("""SELECT 1 FROM read_gallery_cards WHERE page_count<1
        OR NOT json_valid(availability) OR NOT json_valid(first_page) OR NOT json_valid(thumbnail)
        OR NOT json_valid(motion) OR NOT json_valid(gif) LIMIT 1""").first()
    if invalid:
        raise ProjectionUnavailable("gallery card payload is invalid")
    owner_check = " OR ".join(f"json_extract({slot},'$.work_id') IS NOT NULL AND json_extract({slot},'$.work_id')!=work_id"
                              for slot in _SLOTS)
    if connection.exec_driver_sql("SELECT 1 FROM read_gallery_cards WHERE " + owner_check + " LIMIT 1").first():
        raise ProjectionUnavailable("gallery card resource ownership is invalid")
    return {"cards": actual}
