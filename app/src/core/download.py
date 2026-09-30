"""A bounded Requests download of one already identified asset, never an artist crawl."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from ..request_policy import retry_after_seconds


class DownloadFailure(RuntimeError):
    def __init__(self, code, message, *, status=None, retry_after=None,
                 retryable=False, failure_code=None):
        super().__init__(message)
        self.code, self.user_message = code, message
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable
        self.failure_code = failure_code or "image_" + code


class AssetDownloader:
    _lock = threading.Lock()
    _last_request = 0.0
    _cooldown_until = 0.0

    def __init__(self, session=None, *, allowed_hosts=("pximg.net",), allow_http=False,
                 max_bytes=2 * 1024**3):
        self._session = session
        self._sessions = threading.local()
        self.allowed_hosts, self.allow_http, self.max_bytes = allowed_hosts, allow_http, max_bytes

    @property
    def session(self):
        if self._session is not None:
            return self._session
        if not hasattr(self._sessions, "session"):
            self._sessions.session = requests.Session()
        return self._sessions.session

    def _check_url(self, url):
        parts = urlsplit(url)
        schemes = ("https", "http") if self.allow_http else ("https",)
        hostname = parts.hostname or ""
        if (parts.scheme not in schemes or parts.username or parts.password
                or not any(hostname == host or hostname.endswith("." + host) for host in self.allowed_hosts)):
            raise DownloadFailure("invalid_source", "下载地址不属于允许的图片来源")

    @classmethod
    def _before_request(cls):
        """Image-host timing is independent of the API and shared by downloaders."""
        with cls._lock:
            now = time.monotonic()
            if now < cls._cooldown_until:
                raise DownloadFailure("rate_limit", "图片来源请求受限，请稍后重试",
                    retry_after=max(0, int(cls._cooldown_until - now + 0.999)),
                    retryable=True, failure_code="image_rate_limit")
            wait = cls._last_request + 1.0 - now
            if wait > 0:
                time.sleep(wait)
            cls._last_request = time.monotonic()

    @classmethod
    def _note_limit(cls, response):
        if response.status_code != 429:
            return
        seconds = max(60, retry_after_seconds(getattr(response, "headers", None)) or 0)
        with cls._lock:
            cls._cooldown_until = max(cls._cooldown_until,
                                      time.monotonic() + seconds)

    def download(self, url: str, destination: Path, report=None) -> dict:
        response = None
        try:
            for _ in range(4):
                self._check_url(url)
                self._before_request()
                # The CDN rejects Requests' default UA. Keep the Pixiv client
                # identity without sharing API credentials with image requests.
                response = self.session.get(url, stream=True, allow_redirects=False,
                    headers={"User-Agent": "PixivIOSApp/7.19.1 (iOS 16.6; iPhone14,5)",
                             "Referer": "https://www.pixiv.net/", "Accept-Encoding": "identity"},
                    timeout=(10, 60))
                self._note_limit(response)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise DownloadFailure("redirect", "图片来源返回了无效跳转")
                url = urljoin(url, location)
            else:
                raise DownloadFailure("redirect", "图片来源跳转次数过多")
            if response.status_code == 401:
                raise DownloadFailure("authentication", "图片来源未授权访问（401）",
                    status=401, failure_code="image_unauthorized")
            if response.status_code == 403:
                raise DownloadFailure("authentication", "图片来源拒绝访问（403），原因待核查",
                    status=403, failure_code="image_forbidden")
            if response.status_code == 429:
                raise DownloadFailure("rate_limit", "图片来源请求受限，请稍后重试",
                    status=429,
                    retry_after=max(60, retry_after_seconds(response.headers) or 0),
                    retryable=True, failure_code="image_rate_limit")
            if response.status_code != 200:
                status = response.status_code
                raise DownloadFailure("http_status", f"下载未成功，服务器状态码 {status}",
                    status=status,
                    retry_after=retry_after_seconds(response.headers),
                    retryable=status == 408 or status >= 500,
                    failure_code="image_http_status")
            try:
                total = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
            except ValueError as exc:
                raise DownloadFailure("length", "服务器返回了无效的文件长度",
                                      retryable=True) from exc
            if total is not None and (total <= 0 or total > self.max_bytes):
                raise DownloadFailure("length", "文件为空或超过单文件下载上限")
            done, last_report = 0, 0.0
            # The caller allocates a new private stage for each attempt.
            with Path(destination).open("xb") as output:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > self.max_bytes or total is not None and done > total:
                        raise DownloadFailure("length", "实际文件长度超过预期")
                    output.write(chunk)
                    if report and time.monotonic() - last_report >= 0.25:
                        report(done, total)
                        last_report = time.monotonic()
                output.flush()
                os.fsync(output.fileno())
            if not done or total is not None and done != total:
                raise DownloadFailure("incomplete", "下载中断，文件尚未完整取得",
                                      retryable=True)
            if report:
                report(done, total)
            return {"bytes": done, "expected_bytes": total}
        except requests.RequestException as exc:
            raise DownloadFailure("network", "图片连接失败或中断，请稍后重试",
                                  retryable=True) from exc
        finally:
            if response is not None:
                response.close()
