"""配置管理：解析 schema、提供按平台开关 / 权限 / 缓存等访问方法。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .constants import (
    MODE_ALL,
    MODE_AUDIO_ONLY,
    MODE_OFF,
    MODE_TEXT_ONLY,
)


@dataclass
class CookiesConfig:
    netease_cookie: str = ""
    tencent_cookie: str = ""


@dataclass
class ParsersConfig:
    netease: str = MODE_ALL
    tencent: str = MODE_ALL

    def mode(self, platform: str) -> str:
        return getattr(self, platform, MODE_OFF)

    def is_enabled(self, platform: str) -> bool:
        return self.mode(platform) != MODE_OFF

    def has_audio(self, platform: str) -> bool:
        m = self.mode(platform)
        return m in (MODE_ALL, MODE_AUDIO_ONLY)

    def has_text(self, platform: str) -> bool:
        m = self.mode(platform)
        return m in (MODE_ALL, MODE_TEXT_ONLY)


@dataclass
class QualityConfig:
    netease_level: str = "lossless"
    tencent_level: str = "flac"


@dataclass
class TriggerConfig:
    auto_parse: bool = True
    reply_trigger: bool = True
    keywords: List[str] = field(
        default_factory=lambda: ["解析音乐", "音乐解析", "解析", "来一份"]
    )

    def has_keyword(self, text: str) -> bool:
        return any(kw and kw in (text or "") for kw in self.keywords)


@dataclass
class MessageConfig:
    opening_enabled: bool = True
    opening_content: str = "🎵 音乐解析小助手为您服务～"
    show_lyric: bool = False
    send_as_record: bool = True
    show_cover: bool = True


@dataclass
class PermissionConfig:
    private_enabled: bool = True
    group_enabled: bool = True
    enabled_groups: List[str] = field(default_factory=list)
    admin_id: str = ""

    def check(self, is_private: bool, sender_id: str, group_id: Optional[str]) -> bool:
        if is_private and not self.private_enabled:
            return False
        if not is_private and not self.group_enabled:
            return False
        if not is_private and self.enabled_groups and group_id not in self.enabled_groups:
            return False
        return True


@dataclass
class CacheConfig:
    enable_cache: bool = True
    cache_dir: str = ""
    cleanup_after: int = 600


@dataclass
class DebugConfig:
    verbose_log: bool = False


class ConfigManager:
    def __init__(self, raw: dict | None):
        raw = raw or {}

        ck = raw.get("cookies") or {}
        self.cookies = CookiesConfig(
            netease_cookie=str(ck.get("netease_cookie", "") or "").strip(),
            tencent_cookie=str(ck.get("tencent_cookie", "") or "").strip(),
        )

        p = raw.get("parsers") or {}
        self.parsers = ParsersConfig(
            netease=p.get("netease", MODE_ALL),
            tencent=p.get("tencent", MODE_ALL),
        )

        q = raw.get("quality") or {}
        self.quality = QualityConfig(
            netease_level=q.get("netease_level", "lossless"),
            tencent_level=q.get("tencent_level", "flac"),
        )

        t = raw.get("trigger") or {}
        kw = t.get("keywords") or ["解析音乐", "音乐解析", "解析", "来一份"]
        if isinstance(kw, str):
            kw = [kw]
        self.trigger = TriggerConfig(
            auto_parse=bool(t.get("auto_parse", True)),
            reply_trigger=bool(t.get("reply_trigger", True)),
            keywords=list(kw),
        )

        m = raw.get("message") or {}
        self.message = MessageConfig(
            opening_enabled=bool(m.get("opening_enabled", True)),
            opening_content=m.get("opening_content", "🎵 音乐解析小助手为您服务～"),
            show_lyric=bool(m.get("show_lyric", False)),
            send_as_record=bool(m.get("send_as_record", True)),
            show_cover=bool(m.get("show_cover", True)),
        )

        p = raw.get("permission") or {}
        eg = p.get("enabled_groups") or []
        if isinstance(eg, str):
            eg = [eg]
        self.permission = PermissionConfig(
            private_enabled=bool(p.get("private_enabled", True)),
            group_enabled=bool(p.get("group_enabled", True)),
            enabled_groups=[str(x) for x in eg if str(x).strip()],
            admin_id=str(p.get("admin_id", "") or ""),
        )

        c = raw.get("cache") or {}
        self.cache = CacheConfig(
            enable_cache=bool(c.get("enable_cache", True)),
            cache_dir=str(c.get("cache_dir", "") or ""),
            cleanup_after=int(c.get("cleanup_after", 600)),
        )

        self.debug = DebugConfig(verbose_log=bool((raw.get("debug") or {}).get("verbose_log", False)))