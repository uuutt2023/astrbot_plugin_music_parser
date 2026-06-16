"""统一的 aiohttp 客户端：超时、重试、JSON/二进制下载。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import aiohttp

from .constants import DEFAULT_RETRY, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT
from .logger import get_logger

logger = get_logger()


class HttpClient:
    """轻量级 aiohttp 封装。

    用法::

        async with HttpClient(timeout=30, retry=2) as client:
            data = await client.get_json("http://x/health")
            buf = await client.download("http://x/song.mp3")
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        retry: int = DEFAULT_RETRY,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retry = max(0, int(retry))
        self.headers = {"User-Agent": user_agent}
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "HttpClient":
        self._session = aiohttp.ClientSession(
            timeout=self.timeout, headers=self.headers
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, headers=self.headers
            )
        return self._session

    async def _do(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        session = self._ensure()
        last_exc: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                return await session.request(method, url, **kwargs)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                wait = 0.6 * (attempt + 1)
                logger.warning(
                    f"HTTP {method} {url} 第 {attempt + 1} 次失败: {e}, "
                    f"等待 {wait:.1f}s 后重试"
                )
                await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc

    async def get_json(self, url: str, **kwargs) -> Any:
        """GET 并解析 JSON。失败抛 aiohttp 异常。"""
        async with await self._do("GET", url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def post_json(self, url: str, **kwargs) -> Any:
        """POST 并解析 JSON。"""
        async with await self._do("POST", url, **kwargs) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def download(self, url: str, max_bytes: int = 200 * 1024 * 1024) -> bytes:
        """下载二进制（默认上限 200MB）。"""
        async with await self._do("GET", url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError(
                    f"响应体过大: {len(data)} > {max_bytes} bytes"
                )
            return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None