"""astrbot_plugin_music_parser 主入口（v0.3.10 — 输出三选一 + 比例裁剪）。

进程内直接 import vendor 模块，零子进程。
"""

from __future__ import annotations

import asyncio
import re
import traceback
from pathlib import Path
from typing import List, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.config_manager import ConfigManager
from .core.constants import CACHE_DIR_NAME, PLATFORM_NETEASE, PLATFORM_TENCENT
from .core.downloader import DownloadManager, default_cache_dir
from .core.link_extractor import (
    ExtractedLink,
    extract_card_links_by_force,
    extract_links_from_event,
)
from .core.logger import d, e, get_logger, i, set_verbose, w
from .core.parser_manager import ParserManager

logger = get_logger()


@register(
    "astrbot_plugin_music_parser",
    "uuutt2023",
    "开箱即用的网易云 / QQ 音乐解析插件（进程内调用，零子进程）",
    "0.3.10",
)
class MusicParserPlugin(Star):

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config_manager = ConfigManager(config or {})
        cfg = self.config_manager
        set_verbose(cfg.debug.verbose_log)

        self._http: Optional[aiohttp.ClientSession] = None
        self.parser_manager: Optional[ParserManager] = None
        self.download_manager: Optional[DownloadManager] = None

        i(
            f"[init] === 音乐解析插件 v0.3.8 启动 === "
            f"verbose={cfg.debug.verbose_log}, "
            f"netease_cookie={'YES' if cfg.cookies.netease_cookie else 'NO'}, "
            f"tencent_cookie={'YES' if cfg.cookies.tencent_cookie else 'NO'}, "
            f"netease_mode={cfg.parsers.netease}, tencent_mode={cfg.parsers.tencent}, "
            f"auto_parse={cfg.trigger.auto_parse}, reply_trigger={cfg.trigger.reply_trigger}"
        )
        self._init_runtime()

    def _init_runtime(self) -> None:
        cfg = self.config_manager
        data_dir = self._resolve_plugin_data_dir()
        cache_dir = self._resolve_cache_dir(cfg.cache.cache_dir, data_dir)
        ne_cookie_pairs = self._parse_cookie_pairs(cfg.cookies.netease_cookie)
        self.download_manager = DownloadManager(
            cache_dir=cache_dir,
            enabled=cfg.cache.enable_cache,
            cleanup_after=cfg.cache.cleanup_after,
            cookie_pairs=ne_cookie_pairs,
        )
        self.parser_manager = ParserManager(cfg)
        i(f"[init] data_dir={data_dir}, cache_dir={cache_dir}")
        i(f"[init] netease cookie 解析出 {len(ne_cookie_pairs)} 个 key-value")
        i(f"[init] === 启动完成 ===")

    @staticmethod
    def _parse_cookie_pairs(cookie_str: str) -> dict:
        """'k1=v1; k2=v2' → {'k1': 'v1', 'k2': 'v2'}"""
        out: dict = {}
        if not cookie_str:
            return out
        for chunk in cookie_str.replace("\n", ";").split(";"):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                out[k] = v
        return out

    def _resolve_plugin_data_dir(self) -> Path:
        try:
            from astrbot.api.star import StarTools  # type: ignore
            return Path(StarTools.get_data_dir())  # type: ignore[attr-defined]
        except Exception:
            return Path.cwd() / "data" / "plugins_data" / CACHE_DIR_NAME

    def _resolve_cache_dir(self, raw: str, default_dir: Path) -> Path:
        if raw:
            p = Path(raw).expanduser().resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        return default_cache_dir(default_dir)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            if self.download_manager:
                self.download_manager.set_session(self._http)
            d("[session] aiohttp.ClientSession 已创建")
        return self._http

    async def terminate(self) -> None:
        i("[terminate] 插件终止中...")
        if self.download_manager:
            await self.download_manager.shutdown()
        if self._http and not self._http.closed:
            await self._http.close()
        i("[terminate] 已清理资源")

    # ─────────────── 消息分发核心 ───────────────

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 收到任何消息
        try:
            sender_id = event.get_sender_id() or "?"
        except Exception:
            sender_id = "?"
        try:
            is_private = event.is_private_chat()
        except Exception:
            is_private = True
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None

        cfg = self.config_manager
        text = (event.message_str or "").strip()
        d(
            f"[on_message] in sender={sender_id} private={is_private} group={group_id} "
            f"text_len={len(text)} text={text[:80]!r}"
        )

        # 0) 触发条件
        if not cfg.trigger.auto_parse and not cfg.trigger.reply_trigger:
            d("[on_message] skip: auto_parse + reply_trigger 都关闭")
            return

        # 1) 权限
        if not self._check_permission(event):
            d(f"[on_message] skip: 权限拒绝 (sender={sender_id} group={group_id})")
            return

        # 2) 抽链接（文本 + 卡片）
        links = extract_links_from_event(event)
        d(f"[on_message] 文本+卡片共抽出 {len(links)} 个音乐链接")
        for l in links:
            d(f"[on_message]   - [{l.platform}] {l.raw[:60]!r} id={l.identifier}")

        # 3) 回复触发
        if not links and cfg.trigger.reply_trigger and cfg.trigger.has_keyword(text):
            i(f"[on_message] 文本含触发关键词 {cfg.trigger.keywords}，尝试从引用消息抽链接")
            links = self._extract_reply_links(event)
            d(f"[on_message] 引用消息抽出 {len(links)} 个链接")

        # 4) 没有任何链接
        if not links:
            d("[on_message] exit: 未识别到任何音乐链接")
            return

        # 5) 处理
        await self._handle_links(event, links)

    async def _handle_links(
        self,
        event: AstrMessageEvent,
        links: List[ExtractedLink],
    ):
        cfg = self.config_manager
        before = len(links)
        links = [l for l in links if self.parser_manager.enabled(l.platform)]
        if len(links) < before:
            d(
                f"[handle_links] 过滤平台开关: {before} → {len(links)} "
                f"({[l.platform for l in links]})"
            )
        if not links:
            w("[handle_links] 所有链接的平台开关都关闭，跳过")
            return

        await self._ensure_session()

        # 5.1 开场语
        if cfg.message.opening_enabled:
            try:
                await event.send(event.plain_result(cfg.message.opening_content))
                d(f"[handle_links] 开场语已发送")
            except Exception as exc:  # noqa: BLE001
                w(f"[handle_links] 开场语发送失败: {exc}")

        # 5.2 并发解析
        i(f"[handle_links] 开始解析 {len(links)} 个链接...")
        results = await self.parser_manager.parse_many(links)
        for idx, meta in enumerate(results):
            i(f"[handle_links] 准备发送第 {idx + 1}/{len(results)} 条: {meta.name!r} ok={meta.ok}")
            await self._send_one(event, meta)

    # ─────────────── _send_one 拆分为子方法 (v0.3.11) ───────────────

    def _compute_output_config(self, meta) -> dict:
        """计算当前歌曲的输出配置 (output_mode / send_as_record / show_*)。

        v0.3.11: 从 _send_one 拆出，便于独立测试和复用。
        """
        cfg = self.config_manager
        show_text = cfg.parsers.has_text(meta.source)
        show_audio = cfg.parsers.has_audio(meta.source)
        show_cover = show_text and bool(getattr(cfg.message, "show_cover", True))
        show_lyric = show_text and bool(getattr(cfg.message, "show_lyric", False))

        # 归一化 output_mode (link / audio / video)
        mode = str(getattr(cfg.message, "output_mode", "video") or "video").lower().strip()
        if mode not in ("link", "audio", "video"):
            mode = "video"
        send_as_record = bool(getattr(cfg.message, "send_as_record", True))
        # video 模式下强制不用 Record
        if mode == "video":
            send_as_record = False

        d(
            f"[send_one] 输出模式: text={show_text} audio={show_audio} "
            f"cover={show_cover} lyric={show_lyric} output_mode={mode} "
            f"send_as_record={send_as_record}"
        )
        return {
            "show_text": show_text,
            "show_audio": show_audio,
            "show_cover": show_cover,
            "show_lyric": show_lyric,
            "output_mode": mode,
            "send_as_record": send_as_record,
        }

    def _need_download(self, meta, cfg_out: dict) -> bool:
        """是否需要预下载音频文件。"""
        cfg = self.config_manager
        if not (cfg.cache.enable_cache and self.download_manager and meta.audio_url):
            return False
        mode = cfg_out["output_mode"]
        # link 模式不下载；audio 需要且平台开关允许；video 必须
        if mode == "link":
            return False
        if mode == "audio":
            return cfg_out["show_audio"]
        return True  # video

    async def _download_audio(self, meta) -> Optional[Path]:
        """下载音频文件，返回本地路径。失败返回 None。"""
        if not self.download_manager:
            return None
        res = await self.download_manager.download(
            url=meta.audio_url,
            filename=self._audio_filename(meta),
        )
        if res.success and res.path:
            d(f"[send_one] 音频已下载到本地: {res.path}")
            return res.path
        return None

    async def _maybe_synth_video(
        self, meta, local_path: Optional[Path], cfg_out: dict
    ) -> Optional[Path]:
        """合成 封面+音频 视频气泡 (output_mode in audio/video)。

        失败返回 None (调用方降级处理)。
        """
        cfg = self.config_manager
        if not (local_path and local_path.exists() and meta.pic_url):
            return None
        if cfg_out["output_mode"] == "link":
            return None
        try:
            from .core.video_synthesizer import synthesize_to_temp

            result = await synthesize_to_temp(
                audio_path=local_path,
                cover_url=meta.pic_url,
                fps=int(getattr(cfg.message, "video_fps", 2) or 2),
                max_width=int(getattr(cfg.message, "video_max_width", 1920) or 1920),
                max_height=int(getattr(cfg.message, "video_max_height", 1080) or 1080),
            )
            if result:
                d(
                    f"[send_one] 封面视频合成成功: {result} "
                    f"({result.stat().st_size // 1024 // 1024}MB)"
                )
            else:
                d("[send_one] 封面视频合成失败或跳过，降级走原路径")
            return result
        except Exception as exc:  # noqa: BLE001
            w(f"[send_one] 合成封面视频异常: {type(exc).__name__}: {exc}")
            return None

    def _build_chain(self, meta, local_path, synth_video, cfg_out: dict) -> list:
        """构造 AstrBot 消息链。"""
        from .utils.message_builder import build_song_chain, chain_node_summary

        chain = build_song_chain(
            meta,
            show_text=cfg_out["show_text"],
            show_cover=cfg_out["show_cover"],
            show_audio=cfg_out["show_audio"],
            show_lyric=cfg_out["show_lyric"],
            as_record=cfg_out["send_as_record"],
            local_audio_path=local_path,
            synth_video_path=synth_video,
            output_mode=cfg_out["output_mode"],
        )
        d(f"[send_one] 消息链长度={len(chain)}")
        for idx, node in enumerate(chain):
            d(f"[send_one]   chain[{idx}] = {chain_node_summary(node)}")
        return chain

    async def _send_chain(self, event, chain: list, meta) -> bool:
        """发送消息链，加 30s 超时保护。返回是否发送成功。"""
        try:
            await asyncio.wait_for(
                event.send(event.chain_result(chain)),
                timeout=30.0,
            )
            i(f"[send_one] 已发送: {meta.name!r}")
            return True
        except asyncio.TimeoutError:
            e(
                f"[send_one] 发送超时（30s）: {meta.name!r} "
                f"chain_len={len(chain)} 可能因为音频文件过大导致 aiocqhttp 上传失败"
            )
        except Exception as exc:  # noqa: BLE001
            e(f"[send_one] 发送失败: {exc}\n{traceback.format_exc()}")
        return False

    async def _send_fallback_plain(self, event, meta, local_path) -> None:
        """发送失败后补救：发一条 Plain 文本（直链 + 错误说明）。"""
        if not meta.audio_url:
            return
        try:
            size_mb = (local_path.stat().st_size // 1024 // 1024) if local_path else 0
            fallback_text = (
                f"⚠️ 音频上传失败（文件 {size_mb}MB 超限 / 网络超时）\n"
                f"🔗 音频直链：{meta.audio_url}"
            )
            await event.send(event.plain_result(fallback_text))
            i(f"[send_one] 补救发送 Plain 链接成功: {meta.name!r}")
        except Exception as exc:  # noqa: BLE001
            e(f"[send_one] 补救发送也失败: {exc}")

    def _schedule_cleanup(self, local_path, synth_video_path) -> None:
        """清理音频缓存和合成视频。"""
        if local_path and self.download_manager:
            self.download_manager.schedule_cleanup(local_path)
        if synth_video_path and self.download_manager:
            self.download_manager.schedule_cleanup(synth_video_path)

    async def _send_one(self, event: AstrMessageEvent, meta):
        """编排发送流程：输出配置 → 下载 → 合成 → 组装 → 发送 → 补救 → 清理。"""
        cfg = self.config_manager
        d(
            f"[send_one] source={meta.source} ok={meta.ok} "
            f"audio_url={(meta.audio_url or '')[:60]!r} "
            f"format={meta.audio_format} size={meta.audio_size}"
        )

        # 错误链
        if meta.error or not meta.ok:
            if cfg.parsers.has_text(meta.source):
                chain = self._build_error_chain(meta)
                i(f"[send_one] 发送错误链: {meta.error}")
                await event.send(event.chain_result(chain))
            else:
                d(f"[send_one] 平台 {meta.source} 关闭了文本输出，错误不显示")
            return

        # 1) 输出配置
        cfg_out = self._compute_output_config(meta)

        # 2) 下载音频
        local_path = None
        if self._need_download(meta, cfg_out):
            local_path = await self._download_audio(meta)
        else:
            d(
                f"[send_one] 跳过本地下载 "
                f"(output_mode={cfg_out['output_mode']} "
                f"show_audio={cfg_out['show_audio']} "
                f"cache={cfg.cache.enable_cache})"
            )

        # 3) 合成封面视频
        synth_video_path = await self._maybe_synth_video(meta, local_path, cfg_out)

        # 4) 组装消息
        chain = self._build_chain(meta, local_path, synth_video_path, cfg_out)

        # 5) 发送
        send_ok = await self._send_chain(event, chain, meta)

        # 6) 补救（发送失败时）
        if not send_ok and cfg_out["show_audio"]:
            await self._send_fallback_plain(event, meta, local_path)

        # 7) 清理缓存
        self._schedule_cleanup(local_path, synth_video_path)

    # ─────────────── 强制指定平台的命令 (v0.3.11: 加 / 前缀别名) ───────────────

    async def _cmd_netease_impl(self, event: AstrMessageEvent, content: str):
        """网易云解析 - 实现体，被两个命令别名复用。"""
        if not self._check_permission(event):
            d("[cmd:网易云] 权限拒绝")
            return
        text = (content or "").strip()
        i(f"[cmd:网易云] content={text!r}")
        from .core.link_extractor import extract_links_by_force
        links = extract_card_links_by_force(event, PLATFORM_NETEASE)
        if not links:
            links = extract_links_by_force(text, PLATFORM_NETEASE)
        if not links and text.isdigit():
            links = [ExtractedLink(PLATFORM_NETEASE, text, text)]
        d(f"[cmd:网易云] 找到 {len(links)} 个链接")
        if not links:
            await event.send(event.plain_result(
                "用法：网易云 <链接或歌曲ID>\n也可以直接发网易云分享链接/卡片让我自动解析"
            ))
            return
        await self._handle_links(event, links)

    @filter.command("网易云")
    async def cmd_force_netease(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_netease_impl(event, content)

    @filter.command("/网易云")
    async def cmd_force_netease_slash(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_netease_impl(event, content)

    async def _cmd_tencent_impl(self, event: AstrMessageEvent, content: str):
        """QQ 音乐解析 - 实现体。"""
        if not self._check_permission(event):
            d("[cmd:QQ音乐] 权限拒绝")
            return
        text = (content or "").strip()
        i(f"[cmd:QQ音乐] content={text!r}")
        links = extract_card_links_by_force(event, PLATFORM_TENCENT)
        if not links:
            from .core.link_extractor import extract_links_by_force
            links = extract_links_by_force(text, PLATFORM_TENCENT)
        d(f"[cmd:QQ音乐] 找到 {len(links)} 个链接")
        if not links:
            await event.send(event.plain_result(
                "用法：QQ音乐 <链接>\n也可以直接发 QQ 音乐分享链接/卡片"
            ))
            return
        await self._handle_links(event, links)

    @filter.command("QQ音乐")
    async def cmd_force_tencent(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_tencent_impl(event, content)

    @filter.command("/QQ音乐")
    async def cmd_force_tencent_slash(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_tencent_impl(event, content)

    # ─────────────── 搜索 (v0.3.11: 加 / 前缀别名) ───────────────

    async def _cmd_search_netease_impl(self, event: AstrMessageEvent, content: str):
        if not self._check_permission(event):
            return
        keyword = (content or "").strip()
        i(f"[cmd:搜云] keyword={keyword!r}")
        if not keyword:
            await event.send(event.plain_result(
                "用法：搜云 <关键词>\n例如：搜云 暑假 转校生"
            ))
            return
        if not self.config_manager.cookies.netease_cookie:
            await event.send(event.plain_result(
                "未配置网易云 Cookie，无法搜索。\n"
                "去 AstrBot WebUI → 音乐解析插件 → cookies.netease_cookie 填入黑胶 Cookie"
            ))
            return
        try:
            songs = await self.parser_manager.get("netease").search(keyword, limit=10)
            i(f"[cmd:搜云] 返回 {len(songs)} 条结果")
        except Exception as exc:  # noqa: BLE001
            e(f"[cmd:搜云] 失败: {exc}\n{traceback.format_exc()}")
            await event.send(event.plain_result(f"搜索失败：{exc}"))
            return
        if not songs:
            await event.send(event.plain_result("没搜到结果，换个关键词试试？"))
            return

        lines = [f"🔍 网易云搜索「{keyword}」前 {len(songs)} 条："]
        for idx, s in enumerate(songs[:10], 1):
            name = s.get("name") or "未知"
            artists = s.get("artists") or s.get("ar_name") or "未知"
            sid = s.get("id") or ""
            album = s.get("album") or s.get("al_name") or ""
            line = f"{idx}. {name} - {artists}"
            if album:
                line += f" 〔{album}〕"
            if sid:
                line += f"\n   https://music.163.com/song?id={sid}"
            lines.append(line)
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("搜云")
    async def cmd_search_netease(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_search_netease_impl(event, content)

    @filter.command("/搜云")
    async def cmd_search_netease_slash(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_search_netease_impl(event, content)

    async def _cmd_search_tencent_impl(self, event: AstrMessageEvent, content: str):
        i(f"[cmd:搜QQ] keyword={(content or '').strip()!r} (QQ 音乐后端无搜索接口)")
        await event.send(event.plain_result(
            "QQ 音乐后端（Suxiaoqinx/tencent_url）暂未提供搜索接口，\n"
            "请直接粘贴歌曲分享链接。"
        ))

    @filter.command("搜QQ")
    async def cmd_search_tencent(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_search_tencent_impl(event, content)

    @filter.command("/搜QQ")
    async def cmd_search_tencent_slash(self, event: AstrMessageEvent, content: str = ""):
        await self._cmd_search_tencent_impl(event, content)

    # ─────────────── 帮助 / 状态 / 缓存 (v0.3.11: 加 / 前缀别名) ───────────────

    @filter.command("音乐解析帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        await event.send(event.plain_result(self._help_text()))

    @filter.command("/音乐解析帮助")
    async def cmd_help_slash(self, event: AstrMessageEvent):
        await self.cmd_help(event)

    @filter.command("音乐解析状态")
    async def cmd_status(self, event: AstrMessageEvent):
        await self._send_status(event)

    @filter.command("/音乐解析状态")
    async def cmd_status_slash(self, event: AstrMessageEvent):
        await self._send_status(event)

    async def _send_status(self, event: AstrMessageEvent):
        i("[cmd:状态] 执行")
        health = await self.parser_manager.health_check()
        cfg = self.config_manager
        ne_cookie = bool(cfg.cookies.netease_cookie)
        te_cookie = bool(cfg.cookies.tencent_cookie)
        ne_mode_emoji = {
            "关闭": "⛔",
            "全部发送": "📦",
            "仅文本": "📝",
            "仅音频": "🎵",
        }.get(cfg.parsers.netease, cfg.parsers.netease)
        te_mode_emoji = {
            "关闭": "⛔",
            "全部发送": "📦",
            "仅文本": "📝",
            "仅音频": "🎵",
        }.get(cfg.parsers.tencent, cfg.parsers.tencent)
        lines = [
            "🎵 音乐解析状态 (v0.3.11)",
            "",
            "📦 后端健康：",
            f"  网易云：{'✅' if health.get('netease') else '⚠️'} "
            + ("Cookie 已配置" if ne_cookie else "Cookie 未配置（标准音质可用）"),
            f"  QQ 音乐：{'✅' if health.get('tencent') else '⚠️'} "
            + ("Cookie 已配置" if te_cookie else "Cookie 未配置（标准音质可用）"),
            "",
            "⚙️ 当前配置：",
            f"  网易云模式：{ne_mode_emoji} {cfg.parsers.netease}",
            f"  QQ 音乐模式：{te_mode_emoji} {cfg.parsers.tencent}",
            f"  网易云音质：{cfg.quality.netease_level}",
            f"  QQ 音质：{cfg.quality.tencent_level}",
            f"  输出模式：{getattr(cfg.message, 'output_mode', 'video')}",
            f"  自动解析：{'开' if cfg.trigger.auto_parse else '关'}",
            f"  本地缓存：{'开' if cfg.cache.enable_cache else '关'}",
            f"  详细日志：{'开' if cfg.debug.verbose_log else '关'}",
        ]
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("清理音乐缓存")
    async def cmd_clean_cache(self, event: AstrMessageEvent):
        await self._clean_cache_impl(event)

    @filter.command("/清理音乐缓存")
    async def cmd_clean_cache_slash(self, event: AstrMessageEvent):
        await self._clean_cache_impl(event)

    async def _clean_cache_impl(self, event: AstrMessageEvent):
        cfg = self.config_manager
        is_private = event.is_private_chat()
        sender_id = str(event.get_sender_id() or "").strip()
        if not is_private or not cfg.permission.admin_id or sender_id != cfg.permission.admin_id:
            await event.send(event.plain_result(
                "该命令仅管理员私聊可用。\n"
                f"当前 admin_id={cfg.permission.admin_id or '（未配置）'}"
            ))
            return
        if not self.download_manager or not self.download_manager.enabled:
            await event.send(event.plain_result("未启用本地缓存"))
            return
        # v0.3.11: 只清理过期文件，不清未过期的合成视频
        removed = 0
        for f in self.download_manager.cache_dir.iterdir():
            try:
                if f.is_file():
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        i(f"[cmd:清理音乐缓存] 清理 {removed} 个文件")
        await event.send(event.plain_result(f"已清理 {removed} 个缓存文件"))

    # ─────────────── 内部工具 ───────────────

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        cfg = self.config_manager
        is_private = event.is_private_chat()
        sender_id = event.get_sender_id()
        group_id = None if is_private else event.get_group_id()
        ok = cfg.permission.check(is_private, sender_id, group_id)
        d(
            f"[check_permission] private={is_private} sender={sender_id} "
            f"group={group_id} → {'OK' if ok else 'DENY'}"
        )
        return ok

    def _extract_reply_links(self, event: AstrMessageEvent) -> List[ExtractedLink]:
        try:
            messages = event.get_messages() or []
        except Exception as exc:
            d(f"[_extract_reply_links] get_messages 失败: {exc}")
            return []
        d(f"[_extract_reply_links] 引用消息共 {len(messages)} 个 component")
        for idx, comp in enumerate(messages):
            cls_name = type(comp).__name__
            d(f"[_extract_reply_links]   [{idx}] {cls_name}")
            if cls_name != "Reply":
                continue
            text = getattr(comp, "message_str", "") or ""
            d(f"[_extract_reply_links]     reply.message_str={text[:80]!r}")
            if text:
                from .core.link_extractor import extract_links
                links = extract_links(text)
                if links:
                    i(f"[_extract_reply_links] 引用消息文本抽出 {len(links)} 个链接")
                    return links
            chain = getattr(comp, "chain", None)
            if chain:
                for sub in chain:
                    sub_text = getattr(sub, "text", "") or ""
                    if sub_text:
                        from .core.link_extractor import extract_links
                        links = extract_links(sub_text)
                        if links:
                            i(f"[_extract_reply_links] 引用消息 chain 抽出 {len(links)} 个链接")
                            return links
        return []

    def _audio_filename(self, meta) -> str:
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", meta.name or "audio")[:60]
        artists = "-".join(meta.artists) if meta.artists else "未知"
        ext = meta.audio_format or "mp3"
        return f"{safe_name}-{artists}.{ext}"

    def _build_error_chain(self, meta):
        from .utils.message_builder import build_error_chain
        return build_error_chain(meta)

    @staticmethod
    def _help_text() -> str:
        return (
            "🎵 音乐解析插件 帮助（v0.3.11）\n"
            "\n"
            "✨ 首次使用：\n"
            "  AstrBot WebUI → 音乐解析插件 → 配置 → 填入\n"
            "  • cookies.netease_cookie（网易云黑胶 Cookie）\n"
            "  • cookies.tencent_cookie（QQ 音乐 Cookie）\n"
            "  保存即生效。\n"
            "\n"
            "🎬 三种输出方式（message.output_mode）：\n"
            "  • link  - 文本 + 封面 + 音频直链（不下载，最稳）\n"
            "  • audio - 文本 + 封面 + 音频文件（QQ 收件）\n"
            "  • video - 只发合成视频气泡（封面循环 + 音频，默认）\n"
            "\n"
            "🎮 命令（都支持加 / 前缀，如 /网易云）：\n"
            "  • 网易云 <链接/ID>    强制走网易云解析\n"
            "  • QQ音乐 <链接>        强制走 QQ 音乐解析\n"
            "  • 搜云 <关键词>         调用网易云后端搜索（前10条）\n"
            "  • 搜QQ <关键词>         提示（QQ 后端无搜索接口）\n"
            "  • 音乐解析状态           当前配置 + 后端健康检查\n"
            "  • 音乐解析帮助           本帮助\n"
            "  • 清理音乐缓存          管理员私聊清空本地缓存\n"
            "\n"
            "🐛 出问题排查：\n"
            "  1) WebUI 把 debug.verbose_log 设为 true\n"
            "  2) 复现问题\n"
            "  3) 把 AstrBot 控制台里 [music_parser] 开头的日志贴出来\n"
            "     特别关注 [on_message] [parse] [send_one] [extract_*]\n"
        )