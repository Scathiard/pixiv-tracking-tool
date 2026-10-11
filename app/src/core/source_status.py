"""Versioned source observations, independent of local archive completeness.

No Pixiv response currently provides verified deletion evidence. Such a fact
requires an explicit, reviewed evidence record; HTTP errors never create it.
"""
from __future__ import annotations

from .read_control import LocalReadError

from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import select

from .catalogue_revision import bump_catalogue_revision
from .models import ApplicationState, ArchiveIntent, Artist, Work, utcnow
from .tasks import ItemResult, ItemSpec, safe_failure

ARTIST_PREFIX = "artist_source:"
SOURCE_STATES = ("available", "unavailable", "private", "deleted")


class SourceIdentityMismatch(ValueError):
    user_message = "主站返回的编号与请求不符，已有资料保留"
    failure_code = "source_identity_mismatch"
    retryable = False


@dataclass(frozen=True)
class DeletionEvidence:
    """Reviewed explicit source evidence, never inferred from a response code."""
    pixiv_id: str
    reference: str
    explicit: bool = False
    verified: bool = False


def availability_snapshot(value, account_id=None):
    if not isinstance(value, dict) or value.get("state") not in SOURCE_STATES:
        return None
    state = value["state"]
    confirmed = value.get("confirmed") is True
    # Older observations include first-time transient failures. Retain those
    # records for diagnostics without promoting them to a source fact.
    effective = confirmed and (state == "deleted" and value.get("deletion_evidence") is True
                              or value.get("policy") == "source-v2")
    if state == "deleted" and not (confirmed and value.get("deletion_evidence") is True):
        state, confirmed = "unavailable", False
    reason = value.get("reason") if isinstance(value.get("reason"), str) else None
    checked = value.get("checked_at") if isinstance(value.get("checked_at"), str) else None
    scoped = value.get("account_id")
    if scoped is not None and account_id is not None and str(scoped) != str(account_id):
        state, confirmed, reason = "unavailable", False, "当前账号与上次核对不同，来源状态待重新确认"
        effective = False
    return {"state": state, "reason": reason, "checked_at": checked,
            "confirmed": confirmed, "confirmation": "confirmed" if confirmed else "unconfirmed",
            "cached": scoped is not None and account_id is None, "effective": effective}


def _timestamp():
    return utcnow().replace(tzinfo=timezone.utc).isoformat()


def classify_failure(exc):
    code, status = getattr(exc, "failure_code", None), getattr(exc, "status", None)
    if status in (401, 403, 429, 503) or code in ("api_unauthorized", "api_forbidden", "api_rate_limit", "api_unavailable"):
        return None, "本次网络或登录核对未完成，最近来源事实保留"
    if code in ("work_private", "artist_private", "api_private"):
        return "private", "主站已无法访问：来源不可公开获取；已有存档保留"
    if code in ("work_unavailable", "artist_unavailable") or status == 404 and code != "ugoira_metadata_unavailable":
        return "unavailable", "主站已无法访问：对应来源未能取得；已有存档保留"
    if code == "source_identity_mismatch" or code == "api_invalid_response":
        return None, "主站未返回对应记录的有效资料，已有资料保留"
    if status == 429 or code == "api_rate_limit":
        return None, "主站请求受限，请稍后重新核对"
    if status in (401, 403) or code in ("api_unauthorized", "api_forbidden"):
        return None, "当前登录或访问未通过，最近来源事实保留"
    return None, "本次核对未完成，最近来源事实保留"


class SourceStatus:
    def __init__(self, database, remote=None):
        self.database, self.remote = database, remote

    def account_context(self):
        method = getattr(self.remote, "get_account_id", None)
        if method is None:
            return None
        try:
            account = method()
            return str(account) if account is not None else None
        except Exception:
            return None

    @staticmethod
    def _record(session, kind, local_id, *, create=False):
        if kind not in ("artist", "work"):
            raise ValueError("不支持的来源类型")
        owner = session.get(Artist if kind == "artist" else Work, local_id)
        if owner is None:
            raise KeyError(local_id)
        remote_id = owner.pixiv_user_id if kind == "artist" else owner.pixiv_illust_id
        if kind == "work":
            intent = session.get(ArchiveIntent, remote_id)
            if intent is not None and intent.mode == "suppressed":
                return owner, remote_id, None, None
            value = (owner.source_metadata or {}).get("remote_availability")
            return owner, remote_id, owner, dict(value) if isinstance(value, dict) else {}
        record = session.get(ApplicationState, ARTIST_PREFIX + remote_id)
        if record is None and create:
            record = ApplicationState(key=ARTIST_PREFIX + remote_id, value={})
            session.add(record)
        value = record.value if record is not None else None
        return owner, remote_id, record, dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _store(kind, record, value):
        if kind == "artist":
            record.value = value
        else:
            record.source_metadata = {**(record.source_metadata or {}), "remote_availability": value}

    def begin(self, kind, local_id, account_id=None):
        with self.database.write() as session:
            return self._begin(session, kind, local_id, account_id)

    def _begin(self, session, kind, local_id, account_id=None):
        _, _, record, value = self._record(session, kind, local_id, create=True)
        if record is None:
            return None
        prior = value.get("version", 0)
        version = (prior if type(prior) is int and prior >= 0 else 0) + 1
        self._store(kind, record, {**value, "version": version,
                    "observation_account_id": account_id})
        return version

    def begin_many(self, kind, local_ids, account_id=None):
        """Reserve a known selection before I/O in one bounded transaction."""
        ids = sorted(set(local_ids))
        if kind not in ("artist", "work"):
            raise ValueError("不支持的来源类型")
        if not ids:
            return {}
        result = {}
        with self.database.write() as session:
            model = Artist if kind == "artist" else Work
            owners = list(session.scalars(select(model).where(model.id.in_(ids))))
            if kind == "work":
                suppressed = set(session.scalars(select(ArchiveIntent.pixiv_illust_id).where(
                    ArchiveIntent.mode == "suppressed", ArchiveIntent.pixiv_illust_id.in_([owner.pixiv_illust_id for owner in owners]))))
                records = {owner.id: owner for owner in owners if owner.pixiv_illust_id not in suppressed}
            else:
                keys = {ARTIST_PREFIX + owner.pixiv_user_id: owner.id for owner in owners}
                stored = {record.key: record for record in session.scalars(select(ApplicationState).where(ApplicationState.key.in_(keys)))}
                records = {}
                for key, identity in keys.items():
                    record = stored.get(key)
                    if record is None:
                        record = ApplicationState(key=key, value={})
                        session.add(record)
                    records[identity] = record
            for identity, record in records.items():
                value = record.value if kind == "artist" else (record.source_metadata or {}).get("remote_availability")
                value = dict(value) if isinstance(value, dict) else {}
                prior = value.get("version", 0)
                version = (prior if type(prior) is int and prior >= 0 else 0) + 1
                self._store(kind, record, {**value, "version": version, "observation_account_id": account_id})
                result[identity] = version
        return result

    def _apply(self, kind, local_id, version, account_id, *, state=None, reason=None, evidence=None):
        if self.remote is not None and self.account_context() != account_id:
            return False
        with self.database.write() as session:
            return self._apply_in_session(session, kind, local_id, version, account_id,
                state=state, reason=reason, evidence=evidence)

    def _apply_in_session(self, session, kind, local_id, version, account_id, *, state=None, reason=None, evidence=None):
        """The caller resolves remote identity before owning this transaction."""
        _, _, record, value = self._record(session, kind, local_id, create=True)
        if record is None or version is None or value.get("version") != version:
            return False
        if value.get("observation_account_id") != account_id:
            return False
        checked = _timestamp()
        result = {**value, "last_attempt": {"state": state or "failed", "reason": reason,
                   "checked_at": checked, "version": version}}
        if state is not None:
            result.update(state=state, reason=reason, checked_at=checked, confirmed=True,
                          account_id=account_id, deletion_evidence=state == "deleted", policy="source-v2")
        self._store(kind, record, result)
        bump_catalogue_revision(session)
        return True

    def _apply_available(self, session, kind, local_id, data, version, account_id=None):
        _, remote_id, _, _ = self._record(session, kind, local_id)
        field = "user_id" if kind == "artist" else "illust_id"
        if not isinstance(data, dict) or str(data.get(field, "")) != str(remote_id):
            raise SourceIdentityMismatch()
        return self._apply_in_session(session, kind, local_id, version, account_id, state="available")

    def apply_available(self, kind, local_id, data, version, account_id=None):
        with self.database.read() as session:
            _, remote_id, _, _ = self._record(session, kind, local_id)
        field = "user_id" if kind == "artist" else "illust_id"
        if not isinstance(data, dict) or str(data.get(field, "")) != str(remote_id):
            raise SourceIdentityMismatch()
        return self._apply(kind, local_id, version, account_id, state="available")

    def apply_failure(self, kind, local_id, exc, version, account_id=None):
        state, reason = classify_failure(exc)
        return self._apply(kind, local_id, version, account_id, state=state, reason=reason)

    def apply_deleted(self, kind, local_id, evidence, version, account_id=None):
        with self.database.read() as session:
            _, remote_id, _, _ = self._record(session, kind, local_id)
        if not (isinstance(evidence, DeletionEvidence) and evidence.explicit and evidence.verified
                and evidence.pixiv_id == remote_id and evidence.reference.strip()):
            raise ValueError("缺少可信且对应编号的明确删除证据")
        return self._apply(kind, local_id, version, account_id, state="deleted",
                           reason="已依据明确来源证据确认不存在；已有本地存档保留")

    def snapshot(self, kind, local_id, account_id=None):
        with self.database.read() as session:
            _, _, _, value = self._record(session, kind, local_id)
            return availability_snapshot(value, account_id)

    def fetch_artist(self, artist_id):
        account = self.account_context()
        version = self.begin("artist", artist_id, account)
        with self.database.read() as session:
            artist = session.get(Artist, artist_id)
            remote_id = artist.pixiv_user_id
        try:
            data = self.remote.get_artist_detail(remote_id)
            if self.account_context() != account:
                return None
            if not self.apply_available("artist", artist_id, data, version, account):
                return None
            return {**data, "_source_observation": {"version": version, "account_id": account}}
        except Exception as exc:
            if isinstance(exc, LocalReadError):
                raise
            if self.account_context() == account:
                self.apply_failure("artist", artist_id, exc, version, account)
            raise

    def check_artist(self, artist_id):
        # A homepage check deliberately does not enumerate or download works,
        # refresh avatar files, or modify cached profile text.
        data = self.fetch_artist(artist_id)
        return {"applied": data is not None,
                "availability": self.snapshot("artist", artist_id, self.account_context())}


class ArtistSourceHandler:
    def __init__(self, source_status):
        self.source_status = source_status
        self.client = source_status.remote

    def plan(self, parameters, context):
        ids = parameters.get("artist_ids")
        if not isinstance(ids, list) or len(ids) != 1 or type(ids[0]) is not int or ids[0] < 1:
            raise ValueError("主页核对须指定一位画师")
        with self.source_status.database.read() as session:
            if session.get(Artist, ids[0]) is None:
                raise KeyError(ids[0])
        return [ItemSpec(f"artist-source-{ids[0]}", "artist_source", {"artist_id": ids[0]}, "checking", "artists")]

    def execute(self, item, context):
        context.checkpoint()
        try:
            result = self.source_status.check_artist(item.target["artist_id"])
            return ItemResult("succeeded" if result["applied"] else "skipped", result)
        except Exception as exc:
            if isinstance(exc, LocalReadError):
                raise
            from ..request_policy import ShutdownRequested
            if isinstance(exc, ShutdownRequested):
                raise
            _, reason = classify_failure(exc)
            failure = safe_failure(exc)
            code, status = failure.get("failure_code"), failure.get("status")
            if code == "api_network":
                reason = "网络连接失败，本次主页核对未完成；最近来源事实保留"
            elif status == 401 or code == "api_unauthorized":
                reason = "Pixiv 登录未通过，本次主页核对未完成；最近来源事实保留"
            elif status == 403 or code == "api_forbidden":
                reason = "Pixiv 拒绝访问，本次主页核对未完成；最近来源事实保留"
            elif status == 429 or code == "api_rate_limit":
                reason = "Pixiv 请求受限，本次主页核对未完成；请稍后重试，最近来源事实保留"
            elif (status is not None and status >= 500) or code == "api_unavailable":
                reason = "Pixiv 服务暂时不可用，本次主页核对未完成；最近来源事实保留"
            return ItemResult("failed", {**failure, "message": reason,
                "availability": self.source_status.snapshot("artist", item.target["artist_id"],
                                                             self.source_status.account_context())})
