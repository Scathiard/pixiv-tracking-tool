import threading
import time
import requests
from . import config as _cfg
from .request_policy import is_rate_limit, retry_after_seconds


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


def _checked_response(response):
    # A parseable error body (including {}) is never evidence of HTTP success.
    if not 200 <= response.status_code < 300:
        status = response.status_code
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
        pass

    def _get(self, endpoint, params=None):
        return self._request(endpoint, params, retry_token=True)

    def _do_get(self, endpoint, headers, params):
        """带连接重试的 GET。"""
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{API_HOST}{endpoint}",
                    headers=headers,
                    params=params or {},
                    timeout=30,
                )
                return r
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def _do_post(self, endpoint, headers, data):
        """A remote write may commit before a transport error, so send it once."""
        return requests.post(
            f"{API_HOST}{endpoint}", headers=headers, data=data or {}, timeout=30,
        )

    def _request(self, endpoint, params, retry_token):
        self._throttle()

        headers = _get_auth_header()
        headers["Accept-Language"] = "zh-cn"
        headers["App-OS"] = "ios"
        headers["App-OS-Version"] = "16.7.2"
        headers["App-Version"] = "7.19.1"
        headers["User-Agent"] = "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)"

        try:
            r = self._do_get(endpoint, headers, params)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 网络连接失败，请稍后重试",
                retryable=True, failure_code="api_network") from exc
        self._note_api_limit(r)
        if retry_token and _token_expired(r):
            from .auth import _try_refresh_token
            if _try_refresh_token():
                return self._request(endpoint, params, retry_token=False)
        data = _checked_response(r)
        self._note_api_limit(r, data)
        if "error" not in data:
            return data

        if is_rate_limit(data):
            raise PixivRequestError(r.status_code, "Pixiv 请求受限，请稍后重试",
                retry_after=max(60, retry_after_seconds(getattr(r, "headers", None)) or 0),
                retryable=True, failure_code="api_rate_limit")
        raise PixivRequestError(r.status_code, "Pixiv 拒绝了此次请求，原因待核查",
            retryable=False, failure_code="api_response_error")

    def _post(self, endpoint, data=None, retry_token=True):
        """Send a remote write once; its outcome may be uncertain on failure."""
        self._throttle()

        headers = _get_auth_header()
        headers["Accept-Language"] = "zh-cn"
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
            "Accept-Language": "zh-cn", "App-OS": "ios",
            "App-OS-Version": "16.7.2", "App-Version": "7.19.1",
            "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)",
        }

    def write_bookmark(self, illust_id, desired, account_id):
        """Capture identity and bearer together, then issue exactly one POST."""
        headers = self._bound_headers(account_id)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        endpoint = "/v2/illust/bookmark/add" if desired else "/v1/illust/bookmark/delete"
        data = {"illust_id": illust_id}
        if desired:
            data["restrict"] = "public"
        self._bookmark_throttle()
        try:
            response = self._do_post(endpoint, headers, data)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 收藏结果未确认，请稍后核对",
                retryable=False, failure_code="api_write_unconfirmed") from exc
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
        headers = self._bound_headers(account_id)
        self._bookmark_throttle()
        try:
            response = requests.get(f"{API_HOST}/v1/illust/detail", headers=headers,
                                    params={"illust_id": illust_id}, timeout=30)
        except requests.RequestException as exc:
            raise PixivRequestError(None, "Pixiv 收藏核对连接失败，请稍后重试",
                retryable=True, failure_code="api_network") from exc
        self._note_bookmark_limit(response)
        self._note_api_limit(response)
        if _token_expired(response):
            from .auth import _try_refresh_token
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
        with type(self)._lock:
            if time.monotonic() < type(self)._bookmark_cooldown_until:
                raise PixivRequestError(None, "Pixiv 收藏请求受限，请稍后手动核对",
                    retry_after=max(0, int(type(self)._bookmark_cooldown_until - time.monotonic() + 0.999)),
                    retryable=True, failure_code="api_rate_limit")
        self._throttle()

    @classmethod
    def _note_bookmark_limit(cls, response):
        if response.status_code != 429:
            return
        seconds = max(60, retry_after_seconds(getattr(response, "headers", None)) or 0)
        with cls._lock:
            cls._bookmark_cooldown_until = max(
                cls._bookmark_cooldown_until, time.monotonic() + seconds)

    @classmethod
    def _note_api_limit(cls, response, body=None):
        if response.status_code != 429 and not is_rate_limit(body):
            return
        seconds = max(60, retry_after_seconds(getattr(response, "headers", None)) or 0)
        # Local recovery timing is an engineering default, not a Pixiv quota.
        with cls._lock:
            cls._api_cooldown_until = max(cls._api_cooldown_until,
                                          time.monotonic() + seconds)

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
        with cls._lock:
            now = time.monotonic()
            if now < cls._api_cooldown_until:
                raise PixivRequestError(None, "Pixiv 请求受限，请稍后重试",
                    retry_after=max(0, int(cls._api_cooldown_until - now + 0.999)),
                    retryable=True, failure_code="api_rate_limit")
            wait = cls._last_request + _MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            cls._last_request = time.monotonic()

    def search_artist(self, keyword):
        """搜索画师。"""
        result = self._get("/v1/search/user", {"word": keyword, "filter": "for_ios"})
        users = result.get("user_previews", [])
        return [
            {
                "user_id": str(u["user"]["id"]),
                "name": u["user"]["name"],
                "account": u["user"]["account"],
                "avatar_url": u["user"]["profile_image_urls"]["medium"],
            }
            for u in users
        ]

    def get_artist_detail(self, user_id):
        """获取画师详细信息。"""
        result = self._get("/v1/user/detail", {"user_id": user_id})
        user = result["user"]
        return {
            "user_id": str(user["id"]),
            "name": user["name"],
            "account": user["account"],
            "avatar_url": user["profile_image_urls"]["medium"],
            "biography": user.get("comment") if isinstance(user.get("comment"), str) else None,
            "total_illusts": user.get("total_illusts", 0),
        }

    def get_all_artist_illusts(self, user_id):
        """迭代获取画师的全部作品（插画 + 漫画）。"""
        for work_type in ("illust", "manga"):
            params = {"user_id": user_id, "type": work_type}
            url = "/v1/user/illusts"

            while True:
                data = self._get(url, params)
                for illust in data.get("illusts", []):
                    yield self._parse_illust(illust)

                next_url = data.get("next_url")
                if not next_url:
                    break
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(next_url).query)
                params = {k: v[0] for k, v in qs.items()}

    def get_illust_detail(self, illust_id):
        """获取单个作品详情。"""
        data = self._get("/v1/illust/detail", {"illust_id": illust_id})
        return self._parse_illust(data["illust"])

    def get_ugoira_metadata(self, illust_id):
        return self._get("/v1/ugoira/metadata", {"illust_id": illust_id})["ugoira_metadata"]

    @staticmethod
    def _parse_illust(illust):
        tags = [t["name"] for t in illust.get("tags", [])]

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
            "bookmark_count": illust.get("total_bookmarks"),
            "view_count": illust.get("total_view"),
            "posted_at": illust.get("create_date"),
            "is_bookmarked": illust.get("is_bookmarked"),
            "caption": illust.get("caption"),
            "x_restrict": illust.get("x_restrict"),
            "urls": urls,
        }
