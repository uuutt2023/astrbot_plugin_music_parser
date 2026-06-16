"""astrbot_plugin_music_parser 主入口（v0.3.8 — 精准检测音乐卡片版）。

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
    "Mavis",
    "开箱即用的网易云 / QQ 音乐解析插件（进程内调用，零子进程）",
    "0.3.3",
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

    async def _send_one(self, event: AstrMessageEvent, meta):
        cfg = self.config_manager
        d(
            f"[send_one] source={meta.source} ok={meta.ok} "
            f"audio_url={(meta.audio_url or '')[:60]!r} "
            f"format={meta.audio_format} size={meta.audio_size}"
        )

        if meta.error or not meta.ok:
            if cfg.parsers.has_text(meta.source):
                chain = self._build_error_chain(meta)
                i(f"[send_one] 发送错误链: {meta.error}")
                await event.send(event.chain_result(chain))
            else:
                d(f"[send_one] 平台 {meta.source} 关闭了文本输出，错误不显示")
            return

        show_text = cfg.parsers.has_text(meta.source)
        show_audio = cfg.parsers.has_audio(meta.source)
        show_cover = show_text and cfg.message.show_cover
        show_lyric = show_text and cfg.message.show_lyric

        # v0.3.9: 用户三选一输出模式 (link / audio / video)
        # - link:  文本+封面+直链 (不下载不发文件)
        # - audio: 文本+封面+音频文件 (下载发送原音频)
        # - video: 只发合成视频气泡 (下载后合成 封面+音频，视频尺寸=封面原尺寸 1fps)
        output_mode = getattr(cfg.message, "output_mode", "video") or "video"
        output_mode = str(output_mode).lower().strip()
        if output_mode not in ("link", "audio", "video"):
            output_mode = "video"
        # send_as_record 是旧的布尔开关，与 output_mode 互动：
        #   output_mode == "video": 不需要 Record（Video 组件自带音视频）
        #   output_mode == "audio": send_as_record=true 用 Record；false 用 Video/File
        #   output_mode == "link":  不需要音频组件
        send_as_record = bool(getattr(cfg.message, "send_as_record", True))
        # video 模式强制不用 Record
        if output_mode == "video":
            send_as_record = False

        d(
            f"[send_one] 输出模式: text={show_text} audio={show_audio} "
            f"cover={show_cover} lyric={show_lyric} output_mode={output_mode} "
            f"send_as_record={send_as_record}"
        )

        # 6) 下载（按 output_mode 决定）
        local_path = None
        need_download = (
            cfg.cache.enable_cache
            and self.download_manager
            and meta.audio_url
            and (
                (output_mode == "audio" and show_audio)
                or (output_mode == "video")
                # link 模式不需要下载
            )
        )
        if need_download:
            res = await self.download_manager.download(
                url=meta.audio_url,
                filename=self._audio_filename(meta),
            )
            if res.success and res.path:
                local_path = res.path
                d(f"[send_one] 音频已下载到本地: {local_path}")
        else:
            d(
                f"[send_one] 跳过本地下载 "
                f"(output_mode={output_mode} show_audio={show_audio} cache={cfg.cache.enable_cache})"
            )

        # 6.5) v0.3.7.5: 合成 封面+音频 视频（video 模式必须，其它模式也可用）
        synth_video_path = None
        if (
            local_path
            and local_path.exists()
            and meta.pic_url
            and output_mode in ("video", "audio")  # audio 模式优先用合成视频
        ):
            try:
                from .core.video_synthesizer import synthesize_to_temp

                synth_video_path = await synthesize_to_temp(
                    audio_path=local_path,
                    cover_url=meta.pic_url,
                    fps=int(getattr(cfg.message, "video_fps", 2) or 2),
                    max_width=int(getattr(cfg.message, "video_max_width", 1920) or 1920),
                    max_height=int(getattr(cfg.message, "video_max_height", 1080) or 1080),
                )
                if synth_video_path:
                    d(
                        f"[send_one] 封面视频合成成功: {synth_video_path} "
                        f"({synth_video_path.stat().st_size // 1024 // 1024}MB)"
                    )
                else:
                    d("[send_one] 封面视频合成失败或跳过，降级走原路径")
            except Exception as exc:  # noqa: BLE001
                w(f"[send_one] 合成封面视频异常: {type(exc).__name__}: {exc}")

        # 7) 组装消息
        from .utils.message_builder import build_song_chain
        chain = build_song_chain(
            meta,
            show_text=show_text,
            show_cover=show_cover,
            show_audio=show_audio,
            show_lyric=show_lyric,
            as_record=send_as_record,
            local_audio_path=local_path,
            synth_video_path=synth_video_path,
            output_mode=output_mode,
        )
        d(f"[send_one] 消息链长度={len(chain)}")
        # 打印每个 component 的类型和关键字段，方便排查
        for idx, node in enumerate(chain):
            cls_name = type(node).__name__
            detail = ""
            try:
                if hasattr(node, "text") and isinstance(getattr(node, "text", None), str):
                    detail = f" text={getattr(node, 'text', '')[:60]!r}"
                if hasattr(node, "url") and getattr(node, "url", None):
                    detail += f" url={getattr(node, 'url')[:80]!r}"
                if hasattr(node, "file") and getattr(node, "file", None):
                    detail += f" file={getattr(node, 'file')[:80]!r}"
            except Exception:
                pass
            d(f"[send_one]   chain[{idx}] = {cls_name}{detail}")

        # 8) 发送（加 30s 超时保护，防止 aiocqhttp 大文件上传静默卡死）
        import asyncio
        send_ok = False
        try:
            await asyncio.wait_for(
                event.send(event.chain_result(chain)),
                timeout=30.0,
            )
            i(f"[send_one] 已发送: {meta.name!r}")
            send_ok = True
        except asyncio.TimeoutError:
            e(
                f"[send_one] 发送超时（30s）: {meta.name!r} "
                f"chain_len={len(chain)} 可能因为音频文件过大导致 aiocqhttp 上传失败"
            )
        except Exception as exc:  # noqa: BLE001
            e(f"[send_one] 发送失败: {exc}\n{traceback.format_exc()}")

        # 9) 补救发送：如果原链没发出去，单独发一条 Plain 文本（文本+直链）让用户至少能看到
        if not send_ok and show_audio and meta.audio_url:
            try:
                size_mb = (local_path.stat().st_size // 1024 // 1024) if local_path else 0
                # 只发一条 Plain，避免与超时后可能已部分发送的 Record 重复
                fallback_text = (
                    f"⚠️ 音频上传失败（文件 {size_mb}MB 超限 / 网络超时）\n"
                    f"🔗 音频直链：{meta.audio_url}"
                )
                await event.send(event.plain_result(fallback_text))
                i(f"[send_one] 补救发送 Plain 链接成功: {meta.name!r}")
            except Exception as exc:  # noqa: BLE001
                e(f"[send_one] 补救发送也失败: {exc}")

        # 10) 清理缓存文件
        if local_path and self.download_manager:
            self.download_manager.schedule_cleanup(local_path)
        # v0.3.7.5: 合成的封面视频同生命周期
        if synth_video_path and self.download_manager:
            self.download_manager.schedule_cleanup(synth_video_path)

    # ─────────────── 强制指定平台的命令 ───────────────

    @filter.command("网易云")
    async def cmd_force_netease(self, event: AstrMessageEvent, content: str = ""):
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
            await event.send(event.plain_result("用法：网易云 <链接或歌曲ID>"))
            return
        await self._handle_links(event, links)

    @filter.command("QQ音乐")
    async def cmd_force_tencent(self, event: AstrMessageEvent, content: str = ""):
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
            await event.send(event.plain_result("用法：QQ音乐 <链接>"))
            return
        await self._handle_links(event, links)

    # ─────────────── 搜索 ───────────────

    @filter.command("搜云")
    async def cmd_search_netease(self, event: AstrMessageEvent, content: str = ""):
        if not self._check_permission(event):
            return
        keyword = (content or "").strip()
        i(f"[cmd:搜云] keyword={keyword!r}")
        if not keyword:
            await event.send(event.plain_result("用法：搜云 <关键词>"))
            return
        if not self.config_manager.cookies.netease_cookie:
            await event.send(event.plain_result("未配置网易云 Cookie，无法搜索"))
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

    @filter.command("搜QQ")
    async def cmd_search_tencent(self, event: AstrMessageEvent, content: str = ""):
        i(f"[cmd:搜QQ] keyword={(content or '').strip()!r} (QQ 音乐后端无搜索接口)")
        await event.send(event.plain_result(
            "QQ 音乐后端（Suxiaoqinx/tencent_url）暂未提供搜索接口，"
            "请直接粘贴歌曲分享链接。"
        ))

    # ─────────────── 帮助 / 状态 / 缓存 ───────────────

    @filter.command("音乐解析帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        await event.send(event.plain_result(self._help_text()))

    @filter.command("音乐解析状态")
    async def cmd_status(self, event: AstrMessageEvent):
        i("[cmd:状态] 执行")
        health = await self.parser_manager.health_check()
        cfg = self.config_manager
        lines = [
            "🎵 音乐解析状态（v0.3.8 精准检测音乐卡片版）",
            f"• 网易云后端：进程内调用  "
            + ("✅ Cookie 已配置" if health.get("netease") else "⚠️ Cookie 未配置"),
            f"• QQ 音乐后端：进程内调用  "
            + ("✅ Cookie 已配置" if health.get("tencent") else "⚠️ Cookie 未配置"),
            f"• 网易云模式：{cfg.parsers.netease}",
            f"• QQ 音乐模式：{cfg.parsers.tencent}",
            f"• 网易云默认音质：{cfg.quality.netease_level}",
            f"• QQ 默认音质：{cfg.quality.tencent_level}",
            f"• 自动解析：{'开' if cfg.trigger.auto_parse else '关'}",
            f"• 本地缓存：{'开' if cfg.cache.enable_cache else '关'}",
            f"• 详细日志：{'开' if cfg.debug.verbose_log else '关'}",
        ]
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("清理音乐缓存")
    async def cmd_clean_cache(self, event: AstrMessageEvent):
        cfg = self.config_manager
        is_private = event.is_private_chat()
        sender_id = str(event.get_sender_id() or "").strip()
        if not is_private or not cfg.permission.admin_id or sender_id != cfg.permission.admin_id:
            await event.send(event.plain_result("该命令仅管理员私聊可用"))
            return
        if not self.download_manager or not self.download_manager.enabled:
            await event.send(event.plain_result("未启用本地缓存"))
            return
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
            "🎵 音乐解析插件 帮助（v0.3.8 精准检测音乐卡片版）\n"
            "\n"
            "✨ 首次使用：\n"
            "  AstrBot WebUI → 音乐解析插件 → 配置 → 填入\n"
            "  • cookies.netease_cookie（网易云黑胶 Cookie）\n"
            "  • cookies.tencent_cookie（QQ 音乐 Cookie）\n"
            "  保存即生效。\n"
            "\n"
            "🐛 出问题排查：\n"
            "  1) 在 WebUI 把 debug.verbose_log 设为 true\n"
            "  2) 复现问题\n"
            "  3) 把 AstrBot 控制台里 [music_parser] 开头的日志贴出来\n"
            "     特别是 [on_message] [parse] [send_one] [extract_*] 这些节点\n"
            "\n"
            "🎮 命令：\n"
            "• 直接发送网易云 / QQ 音乐分享链接（支持文本/小程序卡片）\n"
            "• 网易云 <链接/ID>    强制走网易云解析\n"
            "• QQ音乐 <链接>       强制走 QQ 音乐解析\n"
            "• 搜云 <关键词>       调用网易云后端搜索\n"
            "• 搜QQ <关键词>       提示（QQ 后端无搜索）\n"
            "• 音乐解析状态        当前配置摘要\n"
            "• 清理音乐缓存        管理员私聊删除本地缓存\n"
        )