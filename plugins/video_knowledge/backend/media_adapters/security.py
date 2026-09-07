"""Network admission guard for messaging-originated Bilibili collection."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from plugins.video_knowledge.backend.app.domain.messaging_url import (
    is_b23_url,
    validate_bilibili_messaging_url,
)
from plugins.video_knowledge.backend.media_adapters.errors import (
    MediaUnavailableError,
    NetworkTimeoutError,
    RateLimitedError,
    UnsafeUrlError,
)
from plugins.video_knowledge.backend.media_adapters.models import MediaProbe

AddressResolver = Callable[[str, int], Sequence[str]]


def _system_addresses(host: str, port: int) -> Sequence[str]:
    return tuple(
        str(item[4][0])
        for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )


class MessagingUrlGuard:
    """Validate allowlisted URLs, every DNS answer, and short-link redirects."""

    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_redirects: int = 5,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.resolver = resolver or _system_addresses
        self.transport = transport
        self.max_redirects = max(1, max_redirects)
        self.timeout_seconds = timeout_seconds

    async def validate_input(self, value: str) -> str:
        current = self._validate_shape(value)
        await self._validate_dns(current)
        if not is_b23_url(current):
            return current
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(self.timeout_seconds),
            transport=self.transport,
        ) as client:
            for _hop in range(self.max_redirects):
                try:
                    response = await client.head(current)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    raise NetworkTimeoutError("连接视频平台超时") from exc
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location:
                        raise UnsafeUrlError("短链重定向缺少目标地址")
                    current = self._validate_shape(urljoin(current, location))
                    await self._validate_dns(current)
                    if not is_b23_url(current):
                        return current
                    continue
                if response.status_code in {412, 429}:
                    raise RateLimitedError(
                        "平台拒绝或限制了当前请求，请稍后重试或配置 Cookies"
                    )
                if response.status_code in {404, 410}:
                    raise MediaUnavailableError("该视频目前不可用")
                if 200 <= response.status_code < 300 and not is_b23_url(current):
                    return current
                raise UnsafeUrlError("短链没有解析到允许的视频地址")
        raise UnsafeUrlError("短链重定向次数超过安全限制")

    async def validate_probe(self, probe: MediaProbe) -> str:
        platform = str(probe.platform or "").casefold()
        if "bili" not in platform:
            raise UnsafeUrlError("媒体探测结果不是允许的 Bilibili 视频")
        resolved = self._validate_shape(probe.webpage_url)
        if is_b23_url(resolved):
            raise UnsafeUrlError("媒体探测结果仍是未解析短链")
        await self._validate_dns(resolved)
        return resolved

    @staticmethod
    def _validate_shape(value: str) -> str:
        try:
            return validate_bilibili_messaging_url(value)
        except ValueError as exc:
            raise UnsafeUrlError("视频地址不在允许范围内") from exc

    async def _validate_dns(self, value: str) -> None:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = await asyncio.to_thread(self.resolver, host, port)
        except OSError as exc:
            raise NetworkTimeoutError("无法解析视频平台地址") from exc
        if not addresses:
            raise NetworkTimeoutError("无法解析视频平台地址")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError as exc:
                raise UnsafeUrlError("DNS 返回了无效地址") from exc
            if not address.is_global:
                raise UnsafeUrlError("视频地址解析到了私有或本机网络")
