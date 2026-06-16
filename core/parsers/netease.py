"""网易云解析器：直接 import 内置 vendor/netease_url/music_api 调用。

零子进程，纯函数调用。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

from ..logger import get_logger
from .base import BaseParser, SongMetadata

logger = get_logger()

# ── 一次性把 vendor 目录加进 sys.path ──
_VENDOR_NETEASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor", "netease_url",
)
if _VENDOR_NETEASE_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_NETEASE_DIR)


def _parse_cookie_str(cookie_str: str) -> Dict[str, str]:
    """'k1=v1; k2=v2' -> {'k1': 'v1', 'k2': 'v2'}"""
    out: Dict[str, str] = {}
    if not cookie_str:
        return out
    for chunk in cookie_str.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class NeteaseParser(BaseParser):
    """对接内置 vendor/netease_url/music_api.py。

    BaseParser 的 api_base/quality 字段保留是为了接口一致，但实际不通过 HTTP，
    而是用 asyncio.to_thread 包住 music_api 的同步 requests 调用。
    """

    name = "netease"
    cookie_str: str = ""  # 从 config.cookies 注入

    def __init__(self, cookie_str: str, quality: str = "lossless", **_: Any):
        super().__init__(api_base="inproc://netease", http=None, quality=quality, timeout=30)
        self.cookie_str = cookie_str or ""

    async def health_check(self) -> bool:
        """只要 cookie 配了就视为可用（无网络/凭据错误由具体请求抛出）。"""
        return bool(self.cookie_str)

    def set_cookie(self, cookie_str: str) -> None:
        self.cookie_str = cookie_str or ""

    async def parse(self, identifier: str) -> SongMetadata:
        music_id = self._normalize_id(identifier)
        if not music_id:
            return SongMetadata.from_error(self.name, identifier, "无法提取网易云音乐 ID")

        cookies = _parse_cookie_str(self.cookie_str)
        if not cookies:
            return SongMetadata.from_error(
                self.name, identifier,
                "未配置网易云 Cookie，无法解析（请在 WebUI 填 cookies.netease_cookie）",
            )

        try:
            # vendor music_api 的 url_v1 / name_v1 / lyric_v1 是阻塞 requests 调用
            # 用 to_thread 丢到默认线程池，不阻塞 AstrBot 事件循环
            url_resp, name_resp, lyric_resp = await asyncio.gather(
                asyncio.to_thread(_netease_url_v1, int(music_id), self.quality, cookies),
                asyncio.to_thread(_netease_name_v1, int(music_id)),
                asyncio.to_thread(_netease_lyric_v1, int(music_id), cookies),
            )
        except Exception as e:  # noqa: BLE001
            return SongMetadata.from_error(self.name, identifier, f"调用网易云解析失败：{e}")

        # 1. 拿音频直链
        audio_url, audio_format, audio_size, actual_level = _extract_audio(url_resp)
        if not audio_url:
            return SongMetadata.from_error(
                self.name, identifier,
                f"未能获取音频直链（音质 {self.quality} 可能需要黑胶 Cookie 或当前等级不够）",
            )

        # 2. 拿歌曲元数据
        song = _extract_song(name_resp)
        artists = [a.strip() for a in (song.get("ar_name") or "").split(",") if a.strip()]
        if not artists and song.get("ar"):
            artists = [a.get("name", "") for a in song["ar"] if a.get("name")]

        # 3. 拿歌词
        lyric, tlyric = _extract_lyric(lyric_resp)

        return SongMetadata(
            source=self.name,
            name=song.get("name", ""),
            artists=artists,
            album=song.get("al_name") or song.get("al", {}).get("name", ""),
            pic_url=song.get("pic") or song.get("al", {}).get("picUrl", ""),
            audio_url=audio_url,
            audio_format=audio_format or "mp3",
            audio_size=audio_size,
            bitrate=actual_level or self.quality,
            quality=actual_level or self.quality,
            lyric=lyric,
            tlyric=tlyric,
            song_id=str(music_id),
        )

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        cookies = _parse_cookie_str(self.cookie_str)
        if not cookies:
            return []
        try:
            return await asyncio.to_thread(_netease_search, keyword, cookies, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"网易云搜索失败: {e}")
            return []

    @staticmethod
    def _normalize_id(identifier: str) -> str:
        if not identifier:
            return ""
        s = str(identifier).strip()
        if s.isdigit():
            return s
        import re
        m = re.search(r"id=(\d+)", s)
        if m:
            return m.group(1)
        return s


# ── 把 vendor 函数包一层，延迟 import 避免 AstrBot 启动期就报错 ──
def _get_music_api():
    from music_api import (  # type: ignore
        url_v1,
        name_v1,
        lyric_v1,
        search_music,
    )
    return url_v1, name_v1, lyric_v1, search_music


def _netease_url_v1(song_id, level, cookies):
    url_v1, *_ = _get_music_api()
    return url_v1(song_id, level, cookies)


def _netease_name_v1(song_id):
    _, name_v1, *_ = _get_music_api()
    return name_v1(song_id)


def _netease_lyric_v1(song_id, cookies):
    _, _, lyric_v1, _ = _get_music_api()
    return lyric_v1(song_id, cookies)


def _netease_search(keyword, cookies, limit):
    *_, search_music = _get_music_api()
    return search_music(keyword, cookies, limit)


# ── 响应解析 ──
def _extract_audio(resp: Dict[str, Any]):
    """从 url_v1 的响应中抽 (url, ext, size, level)。"""
    if not isinstance(resp, dict) or resp.get("code") != 200:
        return None, None, 0, None
    data_list = resp.get("data") or []
    if not data_list:
        return None, None, 0, None
    item = data_list[0] or {}
    return (
        item.get("url") or None,
        item.get("type") or None,
        int(item.get("size") or 0),
        item.get("level") or None,
    )


def _extract_song(resp: Dict[str, Any]) -> Dict[str, Any]:
    """从 name_v1 的响应中抽歌曲字典。"""
    if not isinstance(resp, dict):
        return {}
    songs = resp.get("songs") or []
    if songs:
        s = songs[0]
        # 把 ar[] / al{} 标准化成 ar_name / al_name
        ars = s.get("ar") or []
        return {
            "name": s.get("name", ""),
            "ar_name": ", ".join(a.get("name", "") for a in ars if a.get("name")),
            "al_name": (s.get("al") or {}).get("name", ""),
            "pic": (s.get("al") or {}).get("picUrl", ""),
        }
    return {}


def _extract_lyric(resp: Dict[str, Any]):
    if not isinstance(resp, dict):
        return None, None
    lrc = (resp.get("lrc") or {}).get("lyric") or None
    tlyric = (resp.get("tlyric") or {}).get("lyric") or None
    if lrc and not lrc.strip():
        lrc = None
    if tlyric and not tlyric.strip():
        tlyric = None
    return lrc, tlyric