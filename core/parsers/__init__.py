"""各平台解析器实现。"""

from .base import BaseParser, SongMetadata
from .netease import NeteaseParser
from .tencent import TencentParser

__all__ = ["BaseParser", "SongMetadata", "NeteaseParser", "TencentParser"]