"""工具模块。"""

from .message_builder import build_song_chain, build_error_chain
from ..core.parsers.base import SongMetadata

__all__ = ["SongMetadata", "build_song_chain", "build_error_chain"]