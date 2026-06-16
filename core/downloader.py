"""音频下载与缓存清理。

直链发送对 QQ/微信等平台往往 403，所以默认会先下到本地再发。
网易云 / QQ 音乐音源直链是带签名的临时 URL，必须带 Referer 才能下。
"""

from __future__ import annotations

import asyncio
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp

from .logger import d, e, i, w

_SAFE_NAME = re.compile(r"[^A-Za-z0-9一-龥_\-\.\(\)\[\] ]+")


# 下载音频时用的请求头
# - 网易云/QQ 音乐音源直链是带临时签名的，服务器检查 Referer
# - 不带 Referer 会被 403 / 410
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}


def safe_filename(name: str, ext: str = "") -> str:
    cleaned = _SAFE_NAME.sub("_", (name or "audio").strip())[:80]
    if not cleaned:
        cleaned = "audio"
    if ext and not cleaned.lower().endswith("." + ext.lower()):
        cleaned = f"{cleaned}.{ext.lstrip('.')}"
    return cleaned


@dataclass
class DownloadResult:
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    size: int = 0


class DownloadManager:
    def __init__(
        self,
        cache_dir: Path,
        enabled: bool = True,
        cleanup_after: int = 600,
        session: Optional[aiohttp.ClientSession] = None,
        cookie_pairs: Optional[dict] = None,
    ):
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.cleanup_after = max(0, int(cleanup_after))
        self._session = session
        # 网易云 VIP 资源下载需要带 Cookie
        self.cookie_pairs = cookie_pairs or {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            i(
                f"[DownloadManager] 缓存启用: dir={self.cache_dir}, "
                f"cleanup={self.cleanup_after}s, cookies={len(self.cookie_pairs)} 个"
            )
        else:
            d("[DownloadManager] 缓存未启用")

    def set_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        d("[DownloadManager] HTTP session 已绑定")

    def set_cookies(self, cookie_pairs: dict) -> None:
        """热更新 Cookie（用于无损/会员资源下载）。"""
        self.cookie_pairs = dict(cookie_pairs or {})
        d(f"[DownloadManager] Cookie 已更新: {len(self.cookie_pairs)} 个")

    async def download(self, url: str, filename: str) -> DownloadResult:
        if not self.enabled:
            return DownloadResult(success=False, error="缓存未启用")
        if not self._session or self._session.closed:
            return DownloadResult(success=False, error="HTTP 会话未就绪")

        target = self.cache_dir / safe_filename(filename)
        if target.exists() and target.stat().st_size > 0:
            d(f"[download] 命中缓存，跳过下载: {target.name} ({target.stat().st_size}B)")
            return DownloadResult(success=True, path=target, size=target.stat().st_size)

        d(f"[download] 开始下载: {url[:80]} -> {target.name}")
        try:
            # 拼接请求头 + cookie（VIP 资源必须带）
            headers = dict(_DOWNLOAD_HEADERS)
            cookies = self.cookie_pairs or None
            # 下载超时：单首 60s 顶到这了。Connection/Read 分别给 10s/60s
            req_timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=60)
            async with self._session.get(
                url, headers=headers, cookies=cookies, timeout=req_timeout
            ) as resp:
                if resp.status >= 400:
                    body_preview = b""
                    try:
                        body_preview = (await resp.read())[:200]
                    except Exception:
                        pass
                    w(
                        f"[download] 失败: HTTP {resp.status} {url[:60]} "
                        f"body[:200]={body_preview!r}"
                    )
                    resp.raise_for_status()
                with target.open("wb") as f:
                    total = 0
                    max_bytes = 200 * 1024 * 1024  # 200MB 上限
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if chunk:
                            total += len(chunk)
                            if total > max_bytes:
                                w(
                                    f"[download] 超出大小上限 {max_bytes} 字节，终止: {url[:60]}"
                                )
                                f.close()
                                target.unlink(missing_ok=True)
                                return DownloadResult(
                                    success=False,
                                    error=f"音频文件超过 {max_bytes // 1024 // 1024}MB 上限",
                                )
                            f.write(chunk)
            size = target.stat().st_size
            i(f"[download] 下载完成: {target.name} ({size} bytes)")
            return DownloadResult(success=True, path=target, size=size)
        except Exception as exc:  # noqa: BLE001
            w(f"[download] 失败: {url} -> {type(exc).__name__}: {exc}")
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
            return DownloadResult(success=False, error=f"{type(exc).__name__}: {exc}")

    def schedule_cleanup(self, path: Path | None) -> None:
        if not self.enabled or not path or not path.exists():
            return
        if self.cleanup_after <= 0:
            return
        d(f"[schedule_cleanup] {path.name} 将于 {self.cleanup_after}s 后清理")
        task = asyncio.create_task(self._delayed_cleanup(path, self.cleanup_after))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _delayed_cleanup(self, path: Path, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            if path.exists():
                path.unlink(missing_ok=True)
                d(f"[cleanup] 已清理: {path}")
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            w(f"[cleanup] 失败 {path}: {exc}")

    async def shutdown(self) -> None:
        for task in list(self._cleanup_tasks):
            if not task.done():
                task.cancel()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        self._cleanup_tasks.clear()
        d("[DownloadManager] 已关闭")


def default_cache_dir(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / "music_cache"