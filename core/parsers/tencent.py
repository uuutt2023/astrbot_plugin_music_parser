"""QQ 音乐解析器：直接 import 内置 vendor/tencent_url/app 调用 QQMusic 类。

零子进程，纯函数调用。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any, Dict, List

from ..logger import get_logger
from .base import BaseParser, SongMetadata

logger = get_logger()

# ── 把 vendor 目录加进 sys.path ──
_VENDOR_TENCENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "vendor", "tencent_url",
)
if _VENDOR_TENCENT_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_TENCENT_DIR)

_QQ_MUSIC_CLS = None  # 延迟 import

# 文件类型优先级（回退用，从高到低）
_FALLBACK_ORDER = [
    "master", "atmos_51", "atmos_2", "ape", "flac",
    "320", "128", "m4a",
]


def _get_qq_music_class():
    global _QQ_MUSIC_CLS
    if _QQ_MUSIC_CLS is None:
        from app import QQMusic  # type: ignore
        _QQ_MUSIC_CLS = QQMusic
    return _QQ_MUSIC_CLS


class TencentParser(BaseParser):
    """对接内置 vendor/tencent_url/app.py 里的 QQMusic 类。"""

    name = "tencent"
    cookie_str: str = ""

    def __init__(self, cookie_str: str, quality: str = "flac", **_: Any):
        super().__init__(api_base="inproc://tencent", http=None, quality=quality, timeout=30)
        self.cookie_str = cookie_str or ""

    async def health_check(self) -> bool:
        return bool(self.cookie_str)

    def set_cookie(self, cookie_str: str) -> None:
        self.cookie_str = cookie_str or ""

    async def parse(self, identifier: str) -> SongMetadata:
        qq_url = self._build_url(identifier)
        if not qq_url:
            return SongMetadata.from_error(self.name, identifier, "无法识别 QQ 音乐链接")

        if not self.cookie_str:
            return SongMetadata.from_error(
                self.name, identifier,
                "未配置 QQ 音乐 Cookie，无法解析（请在 WebUI 填 cookies.tencent_cookie）",
            )

        try:
            song, music_urls, lyric = await asyncio.gather(
                asyncio.to_thread(_get_song, self.cookie_str, qq_url),
                asyncio.to_thread(_get_music_urls, self.cookie_str, qq_url),
                asyncio.to_thread(_get_lyric, self.cookie_str, qq_url),
            )
        except Exception as e:  # noqa: BLE001
            return SongMetadata.from_error(self.name, identifier, f"调用 QQ 音乐解析失败：{e}")

        if isinstance(song, dict) and song.get("msg"):
            return SongMetadata.from_error(self.name, identifier, f"QQ 音乐：{song['msg']}")
        if not song or not song.get("name"):
            return SongMetadata.from_error(self.name, identifier, "QQ 音乐返回为空")

        chosen = self._pick_audio(music_urls)
        if not chosen:
            return SongMetadata.from_error(
                self.name, identifier,
                f"未能获取音频直链（音质 {self.quality} 可能需要 VIP 会员 Cookie）",
            )

        return SongMetadata(
            source=self.name,
            name=song.get("name", ""),
            artists=[song.get("singer", "")] if song.get("singer") else [],
            album=song.get("album", ""),
            pic_url=song.get("pic", ""),
            audio_url=chosen.get("url", ""),
            audio_format=self._guess_ext(chosen.get("url", "")),
            bitrate=chosen.get("bitrate", "") or self.quality,
            quality=self.quality,
            lyric=(lyric or {}).get("lyric") or None,
            tlyric=(lyric or {}).get("tylyric") or None,
            song_id=str(song.get("id") or song.get("mid") or ""),
        )

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        # vendor tencent_url 没有搜索接口
        return []

    def _pick_audio(self, music_urls: Dict[str, Any]) -> Dict[str, Any] | None:
        if not music_urls:
            return None
        if self.quality in music_urls and music_urls[self.quality]:
            return music_urls[self.quality]
        for key in _FALLBACK_ORDER:
            if key in music_urls and music_urls[key]:
                return music_urls[key]
        for v in music_urls.values():
            if v and isinstance(v, dict) and v.get("url"):
                return v
        return None

    @staticmethod
    def _build_url(identifier: str) -> str:
        s = (identifier or "").strip()
        if not s:
            return ""
        if s.startswith("http://") or s.startswith("https://"):
            return s
        if s.isalnum():
            return f"https://y.qq.com/n/ryqq/songDetail/{s}"
        return s

    @staticmethod
    def _guess_ext(url: str) -> str:
        u = url.lower().split("?")[0]
        for ext in ("flac", "ape", "m4a", "mp3", "ogg"):
            if u.endswith(f".{ext}"):
                return ext
        return "mp3"


# ── vendor 调用封装 ──
def _get_song(cookie_str: str, qq_url: str) -> Dict[str, Any]:
    QQMusic = _get_qq_music_class()
    qq = QQMusic()
    qq.set_cookies(cookie_str)
    song_mid = qq.ids(qq_url)
    if not song_mid:
        return {"msg": "信息获取错误/无法解析链接"}
    try:
        sid = int(song_mid)
        mid = 0
    except ValueError:
        sid = 0
        mid = song_mid
    info = qq.get_music_song(mid, sid)
    # 拿到 mid 后再回填到返回值里（parse 时需要）
    info["mid"] = info.get("mid") or mid
    return info


def _get_music_urls(cookie_str: str, qq_url: str) -> Dict[str, Any]:
    """对每个 file_type 调一次 get_music_url，组装 music_urls。"""
    QQMusic = _get_qq_music_class()
    qq = QQMusic()
    qq.set_cookies(cookie_str)
    song_mid = qq.ids(qq_url)
    if not song_mid:
        return {}
    # mid 走 ids 拿到的是 mid；优先用 mid
    try:
        int(song_mid)
        return {}  # sid 模式无法直接拿音乐 url
    except ValueError:
        mid = song_mid
    file_types = [
        "aac_48", "aac_96", "aac_192", "ogg_96", "ogg_192", "ogg_320", "ogg_640",
        "atmos_51", "atmos_2", "master", "flac", "320", "128",
    ]
    results: Dict[str, Any] = {}
    for ft in file_types:
        try:
            r = qq.get_music_url(mid, ft)
            if r:
                results[ft] = r
        except Exception:
            pass
    return results


def _get_lyric(cookie_str: str, qq_url: str) -> Dict[str, Any]:
    QQMusic = _get_qq_music_class()
    qq = QQMusic()
    qq.set_cookies(cookie_str)
    song_mid = qq.ids(qq_url)
    if not song_mid:
        return {}
    try:
        sid = int(song_mid)
    except ValueError:
        # mid 模式：先 get_song 拿 sid
        try:
            info = qq.get_music_song(song_mid, 0)
            sid = int(info.get("id") or 0)
        except Exception:
            sid = 0
    if not sid:
        return {}
    try:
        return qq.get_music_lyric_new(sid) or {}
    except Exception:
        return {}