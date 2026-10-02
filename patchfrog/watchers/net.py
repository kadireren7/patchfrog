"""Outbound-URL safety for hosted watchers (M11.2).

Watcher sources include user-configured URLs (OpenAPI specs, changelog
feeds). A hosted service that fetches arbitrary URLs is an SSRF primitive
unless it refuses internal targets. :func:`validate_public_url` is the one
gate every fetch passes through.

Known limitation (documented in ``docs/watchers.md``): a hostname is
resolved here and again by the HTTP client; a hostile DNS server could answer
differently the second time. Deployments should *also* restrict the watcher
process's egress at the network layer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

#: ports a watcher may fetch from.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(ValueError):
    pass


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def validate_public_url(url: str, *, allow_http: bool = False, resolver: Resolver | None = None) -> str:
    """Returns the URL unchanged if it is safe to fetch; raises
    :class:`UnsafeUrlError` otherwise."""

    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("malformed URL") from exc
    if parts.scheme not in (("https", "http") if allow_http else ("https",)):
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in a URL are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    effective_port = port or (443 if parts.scheme == "https" else 80)
    if effective_port not in ALLOWED_PORTS:
        raise UnsafeUrlError(f"port {effective_port} is not allowed")
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
        raise UnsafeUrlError("internal hostnames are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = await (resolver or _system_resolver)(host, effective_port)
    else:
        addresses = [host]
    if not addresses:
        raise UnsafeUrlError("host did not resolve")
    for address in addresses:
        if not _is_public(address):
            raise UnsafeUrlError("host resolves to a non-public address")
    return url


__all__ = ["ALLOWED_PORTS", "Resolver", "UnsafeUrlError", "validate_public_url"]
