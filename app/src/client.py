import threading
import time
import requests
from contextlib import contextmanager
from functools import wraps
from . import config as _cfg
from .request_policy import AccountChanged, RequestDeferred, ShutdownRequested, is_rate_limit, retry_after_seconds
from .core.tag_translations import REQUESTED_LOCALE, parse_tag_details
from .core import diagnostics


_REQUEST_ACTIONS = {
    "/v1/search/user": "artist_search", "/v1/user/detail": "artist_detail",
    "/v1/user/illusts": "artist_inventory", "/v1/illust/detail": "work_detail",
    "/v1/ugoira/metadata": "ugoira_metadata", "/v2/illust/bookmark/add": "bookmark_add",
    "/v1/illust/bookmark/delete": "bookmark_delete",
}


def _remote_fields(context):
    operation_id, item_id = getattr(context, "operation_id", None), getattr(context, "item_id", None)
    return {"operation_ref": diagnostics.ref(operation_id) if operation_id is not None else None,
        "item_ref": diagnostics.ref(item_id) if item_id is not None else None}


def _remote_event(event, context=None, **fields):
    if diagnostics.enabled():
        diagnostics.emit(event, **_remote_fields(context), **fields)


@contextmanager
def _remote_span(event, context=None, **fields):
    if not diagnostics.enabled():
        yield {}
        return
    with diagnostics.span(event, **_remote_fields(context), **fields) as state:
        yield state


@contextmanager
def _remote_lock(lock, context=None, *, reason):
    if not diagnostics.enabled():
        with lock:
            yield
        return
    with _remote_span("remote.client_lock_wait", context, reason=reason):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _trace_request(method):
    def decorate(function):
        @wraps(function)
        def wrapped(self, endpoint, *args, **kwargs):
            if not diagnostics.enabled():
                return function(self, endpoint, *args, **kwargs)
            context = getattr(getattr(self, "_task_context", None), "context", None)
            with _remote_span("remote.request", context, method=method,
                    action=_REQUEST_ACTIONS.get(endpoint, "other")):
                return function(self, endpoint, *args, **kwargs)
        return wrapped
    return decorate


def _token_file():
    return _cfg.DATA_DIR / "pixiv_token.json"

API_HOST = "https://app-api.pixiv.net"
_MIN_INTERVAL = 1.0  # 请求最小间隔（秒）


class PixivRequestError(RuntimeError):
    def __init__(self, status, message, *, retry_after=None, retryable=None,
                 failure_code=None):
        super().__init__(message)
        self.status, self.user_message = status, message
        self.retry_after = retry_after
        self.retryable = (status in (408, 429) or status is not None and status >= 500
                          if retryable is None else retryable)
        self.failure_code = failure_code or (
            "api_rate_limit" if status == 429 else
            "api_unauthorized" if status == 401 else
            "api_forbidden" if status == 403 else
            "api_unavailable" if status is not None and status >= 500 else
            "api_error")


def _explicit_private(body):
    """Only a witnessed, unambiguous API message counts as private evidence.

    A 404 / 'Page not found', including repeated responses, is not deletion
    evidence. Unknown messages remain unknown rather than guessed by keywords.
    """
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("user_message") if isinstance(error, dict) else None
    return isinstance(message, str) and message.strip().casefold() == "artist has made their work private."


def _checked_response(response):
    # A parseable error body (including {}) is never evidence of HTTP success.
    if not 200 <= response.status_code < 300:
        status = response.status_code
        if status in (400, 404):
            try:
                private = _explicit_private(response.json())
            except ValueError:
                private = False
            if private:
                raise PixivRequestError(status, "主站明确提示作品已设为非公开",
                    retryable=False, failure_code="api_private")
        message = ("Pixiv 请求受限，请稍后重试" if status == 429 else
                   "Pixiv 登录未通过（401）" if status == 401 else
                   "Pixiv 拒绝访问（403），原因待核查" if status == 403 else
                   "Pixiv 服务暂时不可用" if status >= 500 else
                   f"Pixiv 请求未成功（{status}）")
        raise PixivRequestError(status, message,
            retry_after=retry_after_seconds(getattr(response, "headers", None)))
    try:
        value = response.json()
    except ValueError as exc:
        raise PixivRequestError(response.status_code, "Pixiv 返回了无法识别的内容") from exc
    if not isinstance(value, dict):
        raise PixivRequestError(response.status_code, "Pixiv 返回了无效的结果")
    return value


def _token_expired(response):
    if response.status_code == 401:
        return True
    if response.status_code == 429:
        return False
    try:
        body = response.json()
        error = str(body.get("error", "")).lower() if isinstance(body, dict) else ""
    except ValueError:
        return False
    return "invalid_grant" in error or "oauth" in error


def _get_auth_header():
    """获取当前有效的 access_token 用于 API 请求。"""
    from .auth import _load_token

    access_token = _load_token().get("access_token")
    return {"Authorization": f"Bearer {access_token}"} if access_token else {}


class PixivClient:
    _last_request = 0.0
    _lock = threading.Lock()
    _api_cooldown_until = 0.0
    _bookmark_cooldown_until = 0.0

    def __init__(self):
        self._stopping = threading.Event()
        self._task_context = threading.local()

    @contextmanager
    def task_scope(self, context):
        previous = getattr(self._task_context, "context", None)
        self._task_context.context = context
        try:
            yield
        finally:
            self._task_context.context = previous

    def request_stop(self):
        self._stopping.set()
        _remote_event("remote.stop_requested")

    def _checkpoint(self):
        if self._stopping.is_set():
            raise ShutdownRequested()
        context = getattr(getattr(self, "_task_context", None), "context", None)
        if context is not None:
            context.checkpoint()

    @staticmethod
    def identity_generation():
        from .auth import credential_snapshot
        token, _, login = credential_snapshot()
        return (login, str(token.get("user_id", "")))

    @staticmethod
    def identity_scope():
        from .auth import identity_scope
        return identity_scope()

    @staticmethod
    def _check_identity(identity):
        if PixivClient.identity_generation() != identity:
            raise AccountChanged()

    def _get(self, endpoint, params=None):
        return self._request(endpoint, params, retry_token=True)

    def _do_get(self, endpoint, headers, params):
        """带连接重试的 GET。"""
        # Archive tasks persist retries and yield their worker after one attempt.
        attempts = 1 if getattr(getattr(self, "_task_context", None), "context", None) is not None else 3
        for attempt in range(attempts):
            self._checkpoint()
            context = getattr(self._task_context, "context", None)
            if context is not None:
                context.mark_remote_attempt()
            try:
                with _remote_span("remote.transport", context, method="GET",
                        action=_REQUEST_ACTIONS.get(endpoint, "other"), attempt=attempt + 1) as trace:
                    r = requests.get(
                        f"{API_HOST}{endpoint}",
                        headers=headers,
                        params=params or {},
                        timeout=30,
                    )
                    if diagnostics.enabled():
                        trace["status_code"] = r.status_code
                return r
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
                self._checkpoint()
                if attempt == attempts - 1:
                    raise
                with _remote_span("remote.transport_retry_wait", context, expected_delay_seconds=2 * (attempt + 1)):
                    if self._stopping.wait(2 * (attempt + 1)):
                        raise ShutdownRequested()

    def _do_post(self, endpoint, headers, data):
        """A remote write may commit before a transport error, so send it once."""
        self._checkpoint()
        context = getattr(self._task_context, "context", None)
        with _remote_span("remote.transport", context, method="POST",
                action=_REQUEST_ACTIONS.get(endpoint, "other"), attempt=1) as trace:
            response = requests.post(
                f"{API_HOST}{endpoint}", headers=headers, data=data or {}, timeout=30,
            )
            if diagnostics.enabled():
                trace["status_code"] = response.status_code
            return response

    @_trace_request("GET")
    def _request(self, endpoint, params, retry_token):
        self._checkpoint()
        self._task_throttle()
        self._checkpoint()

        from .auth import credential_snapshot, refresh_scope
        token, generation, login = credential_snapshot()
        identity = (login, str(token.get("user_id", "")))
        headers = _get_auth_header()
        headers["Accept-Language"] = REQUESTED_LOCALE
        headers["App-OS"] = "ios"
        headers["App-OS-Version"] = "16.7.2"
        headers["App-Version"] = "7.19.1"
        headers["User-Agent"] = "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)"

        if credential_snapshot()[1] != generation:
            raise AccountChanged()
        try:
            r = self._do_get(endpoint, headers, params)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 网络连接失败，请稍后重试",
                retryable=True, failure_code="api_network") from exc
        self._check_identity(identity)
        self._note_api_limit(r)
        task = getattr(self._task_context, "context", None)
        if retry_token and _token_expired(r) and (task is None or getattr(task, "allow_token_refresh", True)):
            self._checkpoint()
            from .auth import _try_refresh_token
            with _remote_span("remote.auth_refresh", task), refresh_scope(generation):
                refreshed = _try_refresh_token()
            self._check_identity(identity)
            if refreshed:
                if getattr(self._task_context, "context", None) is not None:
                    task.auth_refreshed = True
                    raise RequestDeferred(1, "登录已刷新，等待重试此项读取")
                return self._request(endpoint, params, retry_token=False)
        data = _checked_response(r)
        self._note_api_limit(r, data)
        if "error" not in data:
            return data

        if is_rate_limit(data):
            raise PixivRequestError(r.status_code, "Pixiv 请求受限，请稍后重试",
                retry_after=max(60, retry_after_seconds(getattr(r, "headers", None)) or 0),
                retryable=True, failure_code="api_rate_limit")
        if _explicit_private(data):
            raise PixivRequestError(r.status_code, "主站明确提示作品已设为非公开",
                retryable=False, failure_code="api_private")
        raise PixivRequestError(r.status_code, "Pixiv 拒绝了此次请求，原因待核查",
            retryable=False, failure_code="api_response_error")

    @_trace_request("POST")
    def _post(self, endpoint, data=None, retry_token=True):
        """Send a remote write once; its outcome may be uncertain on failure."""
        self._checkpoint()
        self._task_throttle()
        self._checkpoint()

        identity = self.identity_generation()
        headers = _get_auth_header()
        headers["Accept-Language"] = REQUESTED_LOCALE
        headers["App-OS"] = "ios"
        headers["App-OS-Version"] = "16.7.2"
        headers["App-Version"] = "7.19.1"
        headers["User-Agent"] = "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)"
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        try:
            r = self._do_post(endpoint, headers, data)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 操作结果未确认，请稍后核对",
                retryable=False, failure_code="api_write_unconfirmed") from exc
        self._check_identity(identity)
        self._note_api_limit(r)
        resp = _checked_response(r)
        self._note_api_limit(r, resp)
        if "error" not in resp:
            return resp
        if is_rate_limit(resp):
            raise PixivRequestError(r.status_code, "Pixiv 请求受限，请稍后核对操作结果",
                retry_after=max(60, retry_after_seconds(getattr(r, "headers", None)) or 0),
                retryable=True, failure_code="api_rate_limit")
        raise PixivRequestError(r.status_code, "Pixiv 拒绝了此次操作，远端结果尚未确认",
            retryable=False, failure_code="api_write_unconfirmed")

    def get_account_id(self):
        """Use the identity returned with OAuth tokens, refreshing old tokens once."""
        from .auth import _load_token, _try_refresh_token
        token = _load_token()
        account_id = token.get("user_id")
        if not account_id:
            if not _try_refresh_token():
                raise PixivRequestError(401, "无法确认当前 Pixiv 账号")
            account_id = _load_token().get("user_id")
        if not isinstance(account_id, (str, int)) or not str(account_id).isdigit():
            raise PixivRequestError(401, "无法确认当前 Pixiv 账号")
        return str(account_id)

    def read_bookmark(self, illust_id):
        """A missing or non-boolean field is unknown, never false."""
        result = self._get("/v1/illust/detail", {"illust_id": illust_id})
        illust = result.get("illust")
        if not isinstance(illust, dict) or str(illust.get("id", "")) != str(illust_id):
            return None
        value = illust.get("is_bookmarked") if isinstance(illust, dict) else None
        return value if type(value) is bool else None

    @staticmethod
    def _bound_headers(account_id):
        from .auth import _load_token
        token = _load_token()
        if str(token.get("user_id", "")) != str(account_id) or not token.get("access_token"):
            raise PixivRequestError(401, "当前 Pixiv 账号已变化")
        return {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept-Language": REQUESTED_LOCALE, "App-OS": "ios",
            "App-OS-Version": "16.7.2", "App-Version": "7.19.1",
            "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)",
        }

    def write_bookmark(self, illust_id, desired, account_id):
        """Capture identity and bearer together, then issue exactly one POST."""
        identity = self.identity_generation()
        headers = self._bound_headers(account_id)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        endpoint = "/v2/illust/bookmark/add" if desired else "/v1/illust/bookmark/delete"
        data = {"illust_id": illust_id}
        if desired:
            data["restrict"] = "public"
        self._bookmark_throttle()
        self._check_identity(identity)
        try:
            response = self._do_post(endpoint, headers, data)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 收藏结果未确认，请稍后核对",
                retryable=False, failure_code="api_write_unconfirmed") from exc
        self._check_identity(identity)
        self._note_bookmark_limit(response)
        self._note_api_limit(response)
        result = _checked_response(response)
        self._note_api_limit(response, result)
        if "error" in result:
            if is_rate_limit(result):
                raise PixivRequestError(response.status_code, "Pixiv 收藏请求受限，请稍后手动核对",
                    retry_after=max(60, retry_after_seconds(getattr(response, "headers", None)) or 0),
                    retryable=True, failure_code="api_rate_limit")
            raise PixivRequestError(response.status_code, "Pixiv 拒绝了此次收藏操作")
        return result

    def read_bookmark_bound(self, illust_id, account_id):
        """One throttled read using the captured account's token."""
        from .auth import credential_snapshot, refresh_scope
        token, generation, login = credential_snapshot()
        identity = (login, str(token.get("user_id", "")))
        headers = self._bound_headers(account_id)
        self._bookmark_throttle()
        self._check_identity(identity)
        self._checkpoint()
        try:
            with _remote_span("remote.transport", getattr(self._task_context, "context", None),
                    method="GET", action="bookmark_readback", attempt=1) as trace:
                response = requests.get(f"{API_HOST}/v1/illust/detail", headers=headers,
                                        params={"illust_id": illust_id}, timeout=30)
                if diagnostics.enabled():
                    trace["status_code"] = response.status_code
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 收藏核对连接失败，请稍后重试",
                retryable=True, failure_code="api_network") from exc
        self._check_identity(identity)
        self._note_bookmark_limit(response)
        self._note_api_limit(response)
        if _token_expired(response):
            self._checkpoint()
            from .auth import _try_refresh_token
            with refresh_scope(generation):
                _try_refresh_token()
            raise PixivRequestError(response.status_code, "收藏核对登录已更新，请重试回读")
        result = _checked_response(response)
        self._note_api_limit(response, result)
        if "error" in result:
            if is_rate_limit(result):
                raise PixivRequestError(response.status_code, "Pixiv 收藏核对受限，请稍后重试",
                    retry_after=max(60, retry_after_seconds(getattr(response, "headers", None)) or 0),
                    retryable=True, failure_code="api_rate_limit")
            raise PixivRequestError(response.status_code, "Pixiv 拒绝了此次收藏核对")
        illust = result.get("illust")
        if not isinstance(illust, dict) or str(illust.get("id", "")) != str(illust_id):
            return None
        value = illust.get("is_bookmarked") if isinstance(illust, dict) else None
        return value if type(value) is bool else None

    def _bookmark_throttle(self):
        context = getattr(self._task_context, "context", None)
        with _remote_lock(type(self)._lock, context, reason="bookmark_admission"):
            if time.monotonic() < type(self)._bookmark_cooldown_until:
                _remote_event("remote.admission_deferred", context, reason="bookmark_cooldown",
                    wait_seconds=max(0, type(self)._bookmark_cooldown_until - time.monotonic()))
                raise PixivRequestError(None, "Pixiv 收藏请求受限，请稍后手动核对",
                    retry_after=max(0, int(type(self)._bookmark_cooldown_until - time.monotonic() + 0.999)),
                    retryable=True, failure_code="api_rate_limit")
        self._task_throttle()

    @classmethod
    def _note_bookmark_limit(cls, response):
        if response.status_code != 429:
            return
        seconds = max(60, retry_after_seconds(getattr(response, "headers", None)) or 0)
        with _remote_lock(cls._lock, reason="bookmark_cooldown_update"):
            cls._bookmark_cooldown_until = max(
                cls._bookmark_cooldown_until, time.monotonic() + seconds)
            _remote_event("remote.cooldown_updated", reason="bookmark_rate_limit", expected_delay_seconds=seconds)

    @classmethod
    def _note_api_limit(cls, response, body=None):
        if response.status_code != 429 and not is_rate_limit(body):
            return
        seconds = max(60, retry_after_seconds(getattr(response, "headers", None)) or 0)
        # Local recovery timing is an engineering default, not a Pixiv quota.
        with _remote_lock(cls._lock, reason="api_cooldown_update"):
            cls._api_cooldown_until = max(cls._api_cooldown_until,
                                          time.monotonic() + seconds)
            _remote_event("remote.cooldown_updated", reason="api_rate_limit", expected_delay_seconds=seconds)

    def add_bookmark(self, illust_id, restrict="public"):
        """收藏作品。restrict: 'public'=公开, 'private'=私有。"""
        return self._post("/v2/illust/bookmark/add",
                          {"illust_id": illust_id, "restrict": restrict}, retry_token=False)

    def delete_bookmark(self, illust_id):
        """取消收藏。"""
        return self._post("/v1/illust/bookmark/delete",
                          {"illust_id": illust_id}, retry_token=False)

    @classmethod
    def _throttle(cls):
        """线程安全的请求节流——确保两次请求至少间隔 _MIN_INTERVAL 秒。"""
        with _remote_lock(cls._lock, reason="ordinary_admission"):
            now = time.monotonic()
            if now < cls._api_cooldown_until:
                _remote_event("remote.admission_deferred", reason="api_cooldown",
                    wait_seconds=max(0, cls._api_cooldown_until - now))
                raise PixivRequestError(None, "Pixiv 请求受限，请稍后重试",
                    retry_after=max(0, int(cls._api_cooldown_until - now + 0.999)),
                    retryable=True, failure_code="api_rate_limit")
            wait = cls._last_request + _MIN_INTERVAL - now
            # Reserve a slot atomically; the ordinary caller waits outside the
            # shared lock, so cooldown observations and other lanes can proceed.
            cls._last_request = max(now, cls._last_request + _MIN_INTERVAL)
        if wait > 0:
            with _remote_span("remote.interval_wait", expected_delay_seconds=wait):
                time.sleep(wait)

    def _task_throttle(self):
        if getattr(self._task_context, "context", None) is None:
            return self._throttle()
        cls = type(self)
        context = getattr(self._task_context, "context", None)
        with _remote_lock(cls._lock, context, reason="task_admission"):
            now = time.monotonic()
            wait = max(cls._api_cooldown_until, cls._last_request + _MIN_INTERVAL) - now
            if wait > 0:
                _remote_event("remote.admission_deferred", context,
                    reason="api_cooldown" if cls._api_cooldown_until >= cls._last_request + _MIN_INTERVAL else "request_interval",
                    wait_seconds=wait, cooldown_seconds=max(0, cls._api_cooldown_until - now),
                    interval_seconds=max(0, cls._last_request + _MIN_INTERVAL - now))
                raise RequestDeferred(wait, "等待远端冷却或请求间隔")
            cls._last_request = now
            _remote_event("remote.admission_granted", context, reason="task_admission")

    def search_artist(self, keyword):
        """搜索画师。"""
        result = self._get("/v1/search/user", {"word": keyword, "filter": "for_ios"})
        users = result.get("user_previews", [])[:20]
        return [
            {
                "user_id": str(u["user"]["id"]),
                "name": u["user"]["name"],
                "account": u["user"]["account"],
                "avatar_url": u["user"]["profile_image_urls"]["medium"],
                "biography": u["user"].get("comment"),
            }
            for u in users
        ]

    def get_artist_detail(self, user_id):
        """获取画师详细信息。"""
        try:
            result = self._get("/v1/user/detail", {"user_id": user_id})
        except PixivRequestError as exc:
            if exc.status == 404:
                raise PixivRequestError(404, "主站暂时无法获取画师主页，尚不能确认删除",
                    retryable=False, failure_code="artist_unavailable") from exc
            raise
        user = result.get("user")
        if not isinstance(user, dict) or str(user.get("id", "")) != str(user_id):
            raise PixivRequestError(200, "主站未返回对应画师的有效资料，已有资料保留",
                retryable=False, failure_code="api_invalid_response")
        if not isinstance(user.get("name"), str):
            raise PixivRequestError(200, "主站画师资料不完整，已有资料保留",
                retryable=False, failure_code="api_invalid_response")
        images = user.get("profile_image_urls")
        return {
            "user_id": str(user["id"]),
            "name": user["name"],
            "account": user.get("account") if isinstance(user.get("account"), str) else None,
            "avatar_url": images.get("medium") if isinstance(images, dict) and isinstance(images.get("medium"), str) else None,
            "biography": user.get("comment") if isinstance(user.get("comment"), str) else None,
            "total_illusts": user.get("total_illusts", 0),
        }

    def get_artist_illusts_page(self, user_id, work_type="illust", offset=0):
        """Read one page; persist only its numeric continuation, never a remote URL."""
        from urllib.parse import parse_qs, urlparse
        if work_type not in ("illust", "manga") or type(offset) is not int or offset < 0:
            raise ValueError("无效分页范围")
        data = self._get("/v1/user/illusts", {"user_id": user_id, "type": work_type, "offset": offset})
        items = data.get("illusts", [])
        if not isinstance(items, list) or len(items) > 100:
            raise PixivRequestError(200, "主站分页清单无效", retryable=False, failure_code="api_invalid_response")
        next_offset = None
        if data.get("next_url"):
            query = parse_qs(urlparse(data["next_url"]).query)
            try:
                next_offset = int(query["offset"][0])
            except (KeyError, TypeError, ValueError) as exc:
                raise PixivRequestError(200, "主站分页进度无效", retryable=False, failure_code="api_invalid_response") from exc
            if next_offset <= offset:
                raise PixivRequestError(200, "主站分页未前进", retryable=False, failure_code="api_invalid_response")
        return {"items": [self._parse_illust(item) for item in items], "next_offset": next_offset}

    def get_all_artist_illusts(self, user_id):
        """迭代获取画师的全部作品（插画 + 漫画）。"""
        for work_type in ("illust", "manga"):
            params = {"user_id": user_id, "type": work_type}
            url = "/v1/user/illusts"

            while True:
                data = self._get(url, params)
                for illust in data.get("illusts", []):
                    self._checkpoint()
                    yield self._parse_illust(illust)

                next_url = data.get("next_url")
                if not next_url:
                    break
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(next_url).query)
                params = {k: v[0] for k, v in qs.items()}

    def get_illust_detail(self, illust_id):
        """获取单个作品详情。"""
        try:
            data = self._get("/v1/illust/detail", {"illust_id": illust_id})
        except PixivRequestError as exc:
            if exc.failure_code == "api_private":
                raise PixivRequestError(exc.status, "主站明确提示作品已设为非公开；已有本地内容保留",
                    retryable=False, failure_code="work_private") from exc
            if exc.status == 404:
                raise PixivRequestError(404, "主站暂时无法获取此作品，未确认删除；已有本地内容保留",
                    retryable=False, failure_code="work_unavailable") from exc
            raise
        illust = data.get("illust")
        if not isinstance(illust, dict) or str(illust.get("id", "")) != str(illust_id):
            raise PixivRequestError(200, "主站未返回对应作品的有效资料，已有信息保留",
                retryable=False, failure_code="api_invalid_response")
        try:
            return self._parse_illust(illust)
        except (KeyError, TypeError, ValueError) as exc:
            raise PixivRequestError(200, "主站作品资料不完整，已有信息保留",
                retryable=False, failure_code="api_invalid_response") from exc

    def get_ugoira_metadata(self, illust_id):
        try:
            data = self._get("/v1/ugoira/metadata", {"illust_id": illust_id})
        except PixivRequestError as exc:
            if exc.status == 404 or exc.failure_code == "api_private":
                raise PixivRequestError(exc.status, "主站暂时无法提供动图原包或播放时序；已有本地内容保留",
                    retryable=False, failure_code="ugoira_metadata_unavailable") from exc
            raise
        metadata = data.get("ugoira_metadata")
        frames = metadata.get("frames") if isinstance(metadata, dict) else None
        zip_urls = metadata.get("zip_urls") if isinstance(metadata, dict) else None
        zip_url = (zip_urls.get("original") or zip_urls.get("medium")) if isinstance(zip_urls, dict) else None
        # A partial 200 response is not proof that the stopped input is usable.
        # File safety and archive/frame matching are checked again before publish.
        if (not isinstance(zip_url, str) or not zip_url.strip()
                or not isinstance(frames, list) or not frames
                or any(not isinstance(frame, dict) or not isinstance(frame.get("file"), str)
                       or not frame["file"] or type(frame.get("delay")) is not int
                       or frame["delay"] <= 0 for frame in frames)):
            raise PixivRequestError(200, "主站未返回有效的动图资料，已有内容保留",
                retryable=False, failure_code="api_invalid_response")
        return metadata

    @staticmethod
    def _parse_illust(illust):
        tag_details = parse_tag_details(illust)
        tags = [tag["name"] for tag in tag_details]

        if illust.get("meta_pages"):
            urls = [
                {
                    "thumb": p["image_urls"].get("square_medium"),
                    "medium": p["image_urls"].get("medium"),
                    "original": p["image_urls"].get("original"),
                }
                for p in illust["meta_pages"]
            ]
        else:
            urls = [
                {
                    "thumb": illust["image_urls"].get("square_medium"),
                    "medium": illust["image_urls"].get("medium"),
                    "original": illust.get("meta_single_page", {}).get("original_image_url"),
                }
            ]

        return {
            "illust_id": str(illust["id"]),
            "title": illust["title"],
            "type": illust.get("type", "illust"),
            "page_count": illust.get("page_count", 1),
            "tags": tags,
            "tag_details": tag_details,
            "bookmark_count": illust.get("total_bookmarks"),
            "view_count": illust.get("total_view"),
            "posted_at": illust.get("create_date"),
            "is_bookmarked": illust.get("is_bookmarked"),
            "caption": illust.get("caption"),
            "x_restrict": illust.get("x_restrict"),
            "urls": urls,
        }
