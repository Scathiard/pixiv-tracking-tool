"""Web 边界：局域网来源、同源校验、图片路径和 caption HTML 清洗。"""
from __future__ import annotations

import html
import ipaddress
import re
import secrets
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse


_BLOCKED_CONTENT_TAGS = {
    "script", "style", "iframe", "object", "embed", "svg", "math", "form", "template"
}
_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li",
    "blockquote", "code", "pre", "a",
}
_VOID_TAGS = {"br"}
_CONTROL_CHARS = re.compile(r"[\x00-\x20\x7f]+")
# Windows exposes a Tailscale node as /32; its peers use the shared overlay
# range. Only enable this range when an active Tailscale adapter owns an IP
# inside it, not for an arbitrary ISP/CGNAT adapter.
_TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")


def resolve_image_path(images_root: Path, requested_path: str) -> Path | None:
    """将 Web 图片路径限制在 ``images_root`` 内，并仅返回现有普通文件。"""
    root = Path(images_root).resolve()
    decoded = str(requested_path)
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded = decoded.replace("\\", "/")
    candidate_path = Path(decoded)
    if candidate_path.is_absolute() or candidate_path.drive:
        return None
    try:
        candidate = (root / candidate_path).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return candidate


class _CaptionSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if self._suppressed_depth:
            self._suppressed_depth += 1
            return
        if tag in _BLOCKED_CONTENT_TAGS:
            self._suppressed_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return

        rendered_attrs: list[str] = []
        if tag == "a":
            href = ""
            title = ""
            for key, value in attrs:
                key = key.lower()
                value = value or ""
                if key == "href":
                    compact = _CONTROL_CHARS.sub("", value.strip())
                    parsed = urlsplit(compact)
                    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                        href = compact
                elif key == "title":
                    title = value
            if href:
                rendered_attrs.append(f'href="{html.escape(href, quote=True)}"')
                rendered_attrs.append('target="_blank"')
                rendered_attrs.append('rel="noopener noreferrer"')
            if title:
                rendered_attrs.append(f'title="{html.escape(title, quote=True)}"')

        suffix = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""
        self.output.append(f"<{tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str):
        if not self._suppressed_depth:
            self.output.append(html.escape(data, quote=False))


def sanitize_caption(value: str | None) -> str:
    """保留 Pixiv caption 的基础排版，同时丢弃可执行标签、属性和 URL。"""
    if not value:
        return ""
    parser = _CaptionSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.output)


def _strip_scope(value: str) -> str:
    return value.split("%", 1)[0]


def _prefix_length(netmask: str, version: int) -> int:
    mask = ipaddress.ip_address(_strip_scope(netmask))
    if mask.version != version:
        raise ValueError("地址与掩码版本不一致")
    bits = f"{int(mask):0{mask.max_prefixlen}b}"
    if "01" in bits:
        raise ValueError("非连续子网掩码")
    return bits.count("1")


@dataclass(frozen=True)
class NetworkPolicy:
    """允许回环地址与本机活动网卡所在子网，不把全局 IPv6 等同于外网放行。"""

    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]

    @classmethod
    def from_interfaces(cls, interfaces: list[tuple[str, str | None]]) -> "NetworkPolicy":
        networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = {
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
        }
        for raw_address, raw_netmask in interfaces:
            try:
                address = ipaddress.ip_address(_strip_scope(raw_address))
                if address.is_unspecified:
                    continue
                if raw_netmask:
                    prefix = _prefix_length(raw_netmask, address.version)
                elif address.version == 6 and not address.is_loopback:
                    # psutil 在 Windows 上通常不返回 IPv6 netmask。IPv6 LAN
                    # 的标准子网大小是 /64；退化为 /128 会让所有手机端 IPv6
                    # 请求都被误判为外部来源。
                    prefix = 64
                else:
                    prefix = address.max_prefixlen
                networks.add(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
            except ValueError:
                continue
        ordered = sorted(
            networks,
            key=lambda item: (item.version, int(item.network_address), item.prefixlen),
        )
        return cls(tuple(ordered))

    @classmethod
    def from_system(cls) -> "NetworkPolicy":
        try:
            import psutil

            stats = psutil.net_if_stats()
            interfaces: list[tuple[str, str | None]] = []
            tailscale_active = False
            for name, entries in psutil.net_if_addrs().items():
                if name in stats and not stats[name].isup:
                    continue
                for entry in entries:
                    if entry.family in (socket.AF_INET, socket.AF_INET6):
                        interfaces.append((entry.address, entry.netmask))
                    if (name.casefold().startswith("tailscale")
                            and name in stats and stats[name].isup
                            and entry.family == socket.AF_INET):
                        try:
                            tailscale_active |= ipaddress.ip_address(entry.address) in _TAILSCALE_IPV4
                        except ValueError:
                            continue
            if tailscale_active:
                interfaces.append((str(_TAILSCALE_IPV4.network_address), str(_TAILSCALE_IPV4.netmask)))
            return cls.from_interfaces(interfaces)
        except Exception:
            return cls.from_interfaces([])

    def allows(self, raw_address: str) -> bool:
        try:
            address = ipaddress.ip_address(_strip_scope(raw_address))
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address.version == network.version and address in network for network in self.networks)


def is_same_origin(origin: str | None, host_header: str | None) -> bool:
    """验证浏览器 Origin 与实际 Host 一致，支持 IPv4、IPv6 字面量。"""
    if not origin or not host_header or origin == "null":
        return False
    try:
        origin_parts = urlsplit(origin)
        host_parts = urlsplit("//" + host_header)
        if origin_parts.scheme not in {"http", "https"}:
            return False
        origin_port = origin_parts.port or (443 if origin_parts.scheme == "https" else 80)
        host_port = host_parts.port or origin_port
        return (
            (origin_parts.hostname or "").lower() == (host_parts.hostname or "").lower()
            and origin_port == host_port
        )
    except ValueError:
        return False


class RequestBoundaryMiddleware:
    """无登录局域网模式的来源、Host 与透明 CSRF 边界。"""

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app, policy: NetworkPolicy | None = None, cookie_name: str = "pixiv_csrf"):
        self.app = app
        self.policy = policy or NetworkPolicy.from_system()
        self.cookie_name = cookie_name
        self.allowed_hostnames = {
            "localhost",
            socket.gethostname().lower(),
            socket.getfqdn().lower(),
        }

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

    def _host_allowed(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        try:
            hostname = urlsplit("//" + host_header).hostname
        except ValueError:
            return False
        if not hostname:
            return False
        if hostname.lower() in self.allowed_hostnames:
            return True
        return self.policy.allows(hostname)

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str | None:
        for part in cookie_header.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                return value
        return None

    async def _reject(self, scope, receive, send, status: int, message: str):
        response = PlainTextResponse(message, status_code=status)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        client_host = client[0] if client else ""
        if not self.policy.allows(client_host):
            await self._reject(scope, receive, send, 403, "仅允许当前局域网设备访问")
            return

        headers = self._headers(scope)
        host_header = headers.get("host")
        if not self._host_allowed(host_header):
            await self._reject(scope, receive, send, 400, "无效的访问地址")
            return

        cookie_token = self._cookie_value(headers.get("cookie", ""), self.cookie_name)
        new_cookie = not cookie_token
        csrf_token = cookie_token or secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = csrf_token

        method = scope.get("method", "GET").upper()
        if method not in self._SAFE_METHODS:
            origin = headers.get("origin")
            header_token = headers.get("x-csrf-token", "")
            if origin:
                allowed = is_same_origin(origin, host_header)
            else:
                allowed = bool(
                    cookie_token
                    and header_token
                    and secrets.compare_digest(cookie_token, header_token)
                )
            if not allowed:
                await self._reject(scope, receive, send, 403, "请求来源校验失败")
                return

        mutation_lease = False
        if method not in self._SAFE_METHODS and scope.get("path") not in {"/settings/change-path", "/api/diagnostics/events"}:
            from . import progress

            mutation_lease = progress.begin_mutation()
            if not mutation_lease:
                await self._reject(scope, receive, send, 409, "数据迁移中，请稍后再操作")
                return

        async def send_with_security_headers(message):
            if message.get("type") == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "same-origin"
                response_headers["X-Frame-Options"] = "DENY"
                if new_cookie:
                    response_headers.append(
                        "Set-Cookie",
                        f"{self.cookie_name}={csrf_token}; Path=/; Max-Age=31536000; "
                        "HttpOnly; SameSite=strict",
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_security_headers)
        finally:
            if mutation_lease:
                from . import progress
                progress.end_mutation()
