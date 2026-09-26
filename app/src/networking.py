"""双栈监听对外展示用的本机地址枚举。"""
from __future__ import annotations

import ipaddress
import socket
import asyncio
import sys


def http_loop_factory() -> asyncio.AbstractEventLoop:
    """Use a socket-only loop for the local HTTP server, without changing policy.

    Python 3.12's Windows Proactor shutdown can lose server detach after a peer
    resets a cancelled media request. The HTTP thread needs only sockets; login
    and Playwright run in their own threads with the default subprocess loop.
    Windows SelectSelector has a 512-socket limit; this is a personal LAN server.
    """
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def active_ip_addresses() -> list[str]:
    try:
        import psutil

        stats = psutil.net_if_stats()
        values = []
        for name, entries in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for entry in entries:
                if entry.family in (socket.AF_INET, socket.AF_INET6):
                    values.append(entry.address)
        return values
    except Exception:
        try:
            return [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None)]
        except OSError:
            return []


def access_urls(port: int, addresses: list[str] | None = None) -> list[tuple[str, str]]:
    """返回可复制的非回环 IPv4/IPv6 URL；链路本地 IPv6 不对其他设备展示。"""
    result = []
    seen = set()
    for raw in addresses if addresses is not None else active_ip_addresses():
        clean = raw.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(clean)
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            continue
        if address.version == 6 and address.is_link_local:
            continue
        key = (address.version, str(address))
        if key in seen:
            continue
        seen.add(key)
        if address.version == 4:
            result.append(("局域网 IPv4", f"http://{address}:{port}"))
        else:
            result.append(("IPv6", f"http://[{address}]:{port}"))
    return result
