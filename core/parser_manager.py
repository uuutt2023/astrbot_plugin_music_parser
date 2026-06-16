"""解析器管理器：按平台分发。

直接 import 内置 vendor 模块，零子进程。
"""

from __future__ import annotations

import asyncio
import traceback
from typing import List, Optional

from .config_manager import ConfigManager
from .link_extractor import ExtractedLink
from .logger import d, e, i, w
from .parsers.base import SongMetadata
from .parsers.netease import NeteaseParser
from .parsers.tencent import TencentParser


class ParserManager:
    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg
        self._parsers = {
            "netease": NeteaseParser(
                cookie_str=cfg.cookies.netease_cookie,
                quality=cfg.quality.netease_level,
            ),
            "tencent": TencentParser(
                cookie_str=cfg.cookies.tencent_cookie,
                quality=cfg.quality.tencent_level,
            ),
        }
        i(
            f"[ParserManager] 初始化: netease(quality={cfg.quality.netease_level}, "
            f"cookie={'YES' if cfg.cookies.netease_cookie else 'NO'}), "
            f"tencent(quality={cfg.quality.tencent_level}, "
            f"cookie={'YES' if cfg.cookies.tencent_cookie else 'NO'})"
        )

    def reload_cookies(self) -> None:
        self._parsers["netease"].set_cookie(self.cfg.cookies.netease_cookie)
        self._parsers["tencent"].set_cookie(self.cfg.cookies.tencent_cookie)
        self._parsers["netease"].quality = self.cfg.quality.netease_level
        self._parsers["tencent"].quality = self.cfg.quality.tencent_level
        i("[ParserManager] 已重载 cookie / quality 配置")

    def get(self, platform: str) -> Optional[object]:
        return self._parsers.get(platform)

    def enabled(self, platform: str) -> bool:
        is_supported = platform in self._parsers
        is_on = self.cfg.parsers.is_enabled(platform)
        d(f"[ParserManager.enabled] {platform}: supported={is_supported}, on={is_on}")
        return is_supported and is_on

    async def parse(self, link: ExtractedLink) -> SongMetadata:
        i(f"[parse] 开始解析 [{link.platform}] raw={link.raw[:60]!r} id={link.identifier}")
        parser = self._parsers.get(link.platform)
        if parser is None:
            e(f"[parse] 暂不支持的平台: {link.platform}")
            return SongMetadata.from_error(
                source=link.platform, raw=link.raw,
                error=f"暂不支持的平台: {link.platform}",
            )
        try:
            meta = await parser.parse(link.identifier)
            meta.raw = link.raw
            if meta.error:
                w(f"[parse] 解析失败 [{link.platform}]: {meta.error}")
            else:
                i(
                    f"[parse] 解析成功 [{link.platform}]: "
                    f"{meta.name} - {'/'.join(meta.artists) if meta.artists else '?'} "
                    f"({meta.audio_format} {meta.bitrate})"
                )
            return meta
        except Exception as exc:  # noqa: BLE001
            e(f"[parse] 异常 [{link.platform}]: {exc}\n{traceback.format_exc()}")
            return SongMetadata.from_error(
                source=link.platform, raw=link.raw, error=str(exc)
            )

    async def parse_many(self, links: List[ExtractedLink]) -> List[SongMetadata]:
        if not links:
            return []
        i(f"[parse_many] 并发解析 {len(links)} 个链接: {[l.platform for l in links]}")
        results = await asyncio.gather(*(self.parse(link) for link in links))
        ok = sum(1 for r in results if r.ok)
        i(f"[parse_many] 完成: {ok}/{len(results)} 成功")
        return results

    async def health_check(self) -> dict:
        d("[ParserManager.health_check] 检查两个后端 cookie 状态")
        out = {}
        for name, parser in self._parsers.items():
            try:
                ok = await parser.health_check()
                out[name] = ok
                d(f"  {name}: {'OK' if ok else 'NO'}")
            except Exception as exc:
                w(f"  {name}: 异常 {exc}")
                out[name] = False
        return out