"""解析器基类 + 统一元数据对象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..logger import get_logger

logger = get_logger()


@dataclass
class SongMetadata:
    """解析结果统一结构，供消息组装层使用。"""

    source: str
    name: str = ""
    artists: List[str] = field(default_factory=list)
    album: str = ""
    pic_url: str = ""
    duration: int = 0
    audio_url: str = ""
    audio_format: str = ""
    audio_size: int = 0
    bitrate: str = ""
    quality: str = ""
    lyric: Optional[str] = None
    tlyric: Optional[str] = None
    song_id: str = ""
    raw: str = ""
    error: Optional[str] = None

    @classmethod
    def from_error(cls, source: str, raw: str, error: str) -> "SongMetadata":
        return cls(source=source, raw=raw, error=error)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.audio_url)


class BaseParser(ABC):
    """所有平台解析器统一接口。"""

    name: str = "base"

    def __init__(self, api_base: str, http, quality: str, timeout: int):
        # api_base / http / timeout 仍保留为基类字段，但本实现走进程内调用，
        # 不真正发 HTTP。这里只为了和旧签名兼容。
        self.api_base = (api_base or "").rstrip("/")
        self.http = http
        self.quality = quality
        self.timeout = timeout

    @abstractmethod
    async def parse(self, identifier: str) -> SongMetadata:
        """给定 ID 或 URL，返回 SongMetadata。"""

    @abstractmethod
    async def health_check(self) -> bool:
        """子进程内调用模式下，这里通常只判断 cookie 是否就绪。"""

    async def search(self, keyword: str, limit: int = 10) -> list:
        """关键字搜索，子类按需重写。"""
        return []