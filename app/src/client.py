import threading
import time
import requests
from . import config as _cfg


def _token_file():
    return _cfg.DATA_DIR / "pixiv_token.json"

API_HOST = "https://app-api.pixiv.net"
_MIN_INTERVAL = 1.0  # 请求最小间隔（秒）


def _get_auth_header():
    """获取当前有效的 access_token 用于 API 请求。"""
    from .auth import _load_token

    access_token = _load_token().get("access_token")
    return {"Authorization": f"Bearer {access_token}"} if access_token else {}


class PixivClient:
    _last_request = 0.0
    _lock = threading.Lock()

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
        """带连接重试的 POST。"""
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{API_HOST}{endpoint}",
                    headers=headers,
                    data=data or {},
                    timeout=30,
                )
                return r
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def _request(self, endpoint, params, retry_token):
        self._throttle()

        headers = _get_auth_header()
        headers["Accept-Language"] = "zh-cn"
        headers["App-OS"] = "ios"
        headers["App-OS-Version"] = "16.7.2"
        headers["App-Version"] = "7.19.1"
        headers["User-Agent"] = "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)"

        r = self._do_get(endpoint, headers, params)
        data = r.json()
        if "error" not in data:
            return data

        err_msg = str(data["error"])
        err_lower = err_msg.lower()

        # token 过期——刷新后续期重试一次
        if retry_token and ("invalid_grant" in err_msg or "oauth" in err_lower):
            from .auth import _try_refresh_token
            if _try_refresh_token():
                return self._request(endpoint, params, retry_token=False)

        # 限流——等待后重试（最多 3 次）
        if "rate" in err_lower:
            for attempt in range(3):
                wait = 60 * (attempt + 1)
                time.sleep(wait)
                self._last_request = time.time()
                r = self._do_get(endpoint, headers, params)
                data = r.json()
                if "error" not in data:
                    return data
                err_msg = str(data["error"])

        raise RuntimeError(f"API error: {data['error']}")

    def _post(self, endpoint, data=None, retry_token=True):
        """带节流的 POST 请求（用于收藏等写操作）。"""
        self._throttle()

        headers = _get_auth_header()
        headers["Accept-Language"] = "zh-cn"
        headers["App-OS"] = "ios"
        headers["App-OS-Version"] = "16.7.2"
        headers["App-Version"] = "7.19.1"
        headers["User-Agent"] = "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone14,2)"
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        r = self._do_post(endpoint, headers, data)
        resp = r.json()
        if "error" not in resp:
            return resp

        err_msg = str(resp["error"])
        err_lower = err_msg.lower()

        # token 过期——刷新后续期重试一次
        if retry_token and ("invalid_grant" in err_msg or "oauth" in err_lower):
            from .auth import _try_refresh_token
            if _try_refresh_token():
                return self._post(endpoint, data, retry_token=False)

        # 限流——等待后重试（最多 3 次）
        if "rate" in err_lower:
            for attempt in range(3):
                wait = 60 * (attempt + 1)
                time.sleep(wait)
                self._last_request = time.time()
                r = self._do_post(endpoint, headers, data)
                resp = r.json()
                if "error" not in resp:
                    return resp

        raise RuntimeError(f"API error: {resp['error']}")

    def add_bookmark(self, illust_id, restrict="public"):
        """收藏作品。restrict: 'public'=公开, 'private'=私有。"""
        return self._post("/v2/illust/bookmark/add",
                          {"illust_id": illust_id, "restrict": restrict})

    def delete_bookmark(self, illust_id):
        """取消收藏。"""
        return self._post("/v1/illust/bookmark/delete",
                          {"illust_id": illust_id})

    @classmethod
    def _throttle(cls):
        """线程安全的请求节流——确保两次请求至少间隔 _MIN_INTERVAL 秒。"""
        with cls._lock:
            now = time.time()
            wait = cls._last_request + _MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            cls._last_request = time.time()

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
                    "original": illust.get("meta_single_page", {}).get("original_image_url")
                    or illust["image_urls"].get("large"),
                }
            ]

        return {
            "illust_id": str(illust["id"]),
            "title": illust["title"],
            "type": illust.get("type", "illust"),
            "page_count": illust.get("page_count", 1),
            "tags": tags,
            "bookmark_count": illust.get("total_bookmarks", 0),
            "view_count": illust.get("total_view", 0),
            "posted_at": illust.get("create_date"),
            "is_bookmarked": illust.get("is_bookmarked", False),
            "caption": illust.get("caption", ""),
            "x_restrict": illust.get("x_restrict", 0),
            "urls": urls,
        }
