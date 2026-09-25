"""A bounded Requests download of one already identified asset, never an artist crawl."""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests


class DownloadFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.user_message = code, message


class AssetDownloader:
    def __init__(self, session=None, *, allowed_hosts=("pximg.net",), allow_http=False,
                 max_bytes=2 * 1024**3):
        self.session = session or requests.Session()
        self.allowed_hosts, self.allow_http, self.max_bytes = allowed_hosts, allow_http, max_bytes

    def _check_url(self, url):
        parts = urlsplit(url)
        schemes = ("https", "http") if self.allow_http else ("https",)
        hostname = parts.hostname or ""
        if (parts.scheme not in schemes or parts.username or parts.password
                or not any(hostname == host or hostname.endswith("." + host) for host in self.allowed_hosts)):
            raise DownloadFailure("invalid_source", "下载地址不属于允许的图片来源")

    def download(self, url: str, destination: Path, report=None) -> dict:
        response = None
        try:
            for _ in range(4):
                self._check_url(url)
                response = self.session.get(url, stream=True, allow_redirects=False,
                    headers={"Referer": "https://www.pixiv.net/", "Accept-Encoding": "identity"},
                    timeout=(10, 60))
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise DownloadFailure("redirect", "图片来源返回了无效跳转")
                url = urljoin(url, location)
            else:
                raise DownloadFailure("redirect", "图片来源跳转次数过多")
            if response.status_code in (401, 403):
                raise DownloadFailure("authentication", "图片来源拒绝访问，请检查登录状态后重试")
            if response.status_code != 200:
                raise DownloadFailure("http_status", f"下载未成功，服务器状态码 {response.status_code}")
            try:
                total = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
            except ValueError as exc:
                raise DownloadFailure("length", "服务器返回了无效的文件长度") from exc
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
                raise DownloadFailure("incomplete", "下载中断，文件尚未完整取得")
            if report:
                report(done, total)
            return {"bytes": done, "expected_bytes": total}
        except requests.RequestException as exc:
            raise DownloadFailure("network", "网络连接失败或中断，可稍后重试") from exc
        finally:
            if response is not None:
                response.close()
