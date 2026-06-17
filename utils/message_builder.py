"""构造 AstrBot 消息链。

本插件对 AstrBot 消息组件层做最小依赖：
- 始终用 `Plain` 输出文本节点
- 用 `Image` 输出封面（远程 URL 即可）
- 用 `Record` 或 `File` 输出音频（远程 URL）

为什么本地缓存？我们让 `File.fromFile` / `Record.fromFile` 在缓存可用时优先使用本地路径，
否则回退到远程 URL；这样 QQ / 微信等风控严格的平台也能稳定发出。

v0.3.7 新增（修复"下载成功但不发送"）：
- 大文件降级：超过 _LARGE_AUDIO_BYTES（默认 30MB）强制走 Plain 链接而不是
  Record/File.fromFile，避免 aiocqhttp 上传 55MB FLAC 静默卡死
- 每个 component 构造都打印详细日志，方便定位哪一步崩了
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 兜底：让编辑器 / 类型检查时不依赖 astrbot 实际安装
try:
    from astrbot.api.message_components import (  # type: ignore
        Plain,
        Image,
        Record,
        File,
        Video,
    )
    _HAVE_ASTRBOT = True
except Exception:  # noqa: BLE001
    Plain = Image = Record = File = Video = None  # type: ignore
    _HAVE_ASTRBOT = False

# 统一从 core 拿 SongMetadata，避免重复定义
from ..core.parsers.base import SongMetadata  # noqa: E402,F401
# 用项目统一的日志 wrapper（已绑定 AstrBot logger，不会有 plugin_tag KeyError）
from ..core.logger import d as _log_d, w as _log_w, i as _log_i  # noqa: E402

# 超过这个大小的音频文件直接走 Plain 链接，避免 aiocqhttp 上传大文件静默卡死
_LARGE_AUDIO_BYTES = 30 * 1024 * 1024  # 30MB


def _truncate_lyric(lyric: str | None, max_lines: int = 30) -> str:
    if not lyric:
        return ""
    lines = [ln for ln in lyric.splitlines() if ln.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + "\n…（歌词已截断）"


def chain_node_summary(node) -> str:
    """提取 message component 的关键字段为摘要字符串。

    避免 main.py 里用 hasattr + getattr 反射获取字段（重复多次调用且容易出错）。
    """
    if node is None:
        return "<None>"
    cls = type(node).__name__
    fields = []
    text = getattr(node, "text", None)
    if isinstance(text, str) and text:
        fields.append(f"text={text[:60]!r}")
    url = getattr(node, "url", None)
    if url:
        fields.append(f"url={str(url)[:80]!r}")
    file_attr = getattr(node, "file", None)
    if file_attr:
        fields.append(f"file={str(file_attr)[:80]!r}")
    path_or_url = getattr(node, "path_or_url", None)
    if path_or_url and not file_attr:
        fields.append(f"file/path={str(path_or_url)[:80]!r}")
    detail = " " + " ".join(fields) if fields else ""
    return f"{cls}{detail}"


def _audio_filename(meta: SongMetadata) -> str:
    safe_name = (meta.name or "audio").strip().replace("/", "_").replace("\\", "_")[:60]
    artists = "-".join(meta.artists) if meta.artists else "未知"
    return f"{safe_name}-{artists}.{meta.audio_format or 'mp3'}"


def _try_make_file(path_or_url: str, name_hint: str | None = None):
    """构造 File 组件（透营录最后回退）。AstrBot 各版本 File API 不一致，按顺序尝试。

    v0.3.7 踩坑：File 类没有 .fromFile/.fromURL 静态方法（那是 Record 的）。
    正确用法是 File(file=path, name=...) 或 File(path=...)。
    """
    name = name_hint or Path(path_or_url).name if Path(path_or_url).exists() else "audio"

    # 优先尝试现代 API（AstrBot v4+）
    for kwargs in (
        {"file": path_or_url, "name": name},
        {"file": path_or_url},
        {"path": path_or_url},
        {"url": path_or_url},
    ):
        try:
            return File(**kwargs)
        except TypeError:
            continue
    # 退化：老版本有 fromFile / fromURL
    if hasattr(File, "fromFile"):
        return File.fromFile(path_or_url)
    if hasattr(File, "fromURL"):
        return File.fromURL(path_or_url)
    raise AttributeError("Cannot construct File (no matching constructor)")


def _audio_component(
    meta: SongMetadata,
    local_path: Optional[Path],
    as_record: bool,
    synth_video_path: Optional[Path] = None,
):
    """选择 Record / Video / File。

    v0.3.7.5：as_record=false 且传入 synth_video_path（提前合成的封面视频）时，
              优先使用合成的视频文件发送。这样 QQ 视频气泡的每一帧画面
              都是用户指定的封面。

    v0.3.7.1：不再预判大文件降级。直接构造组件，让 event.send 走实际路径。
    v0.3.7.2：File 类用 File(file=...) 构造，不用 .fromFile/.fromURL。
    v0.3.7.3：as_record=false 时优先用 Video 组件（参考 astrbot_plugin_media_parser）。
    """
    if not _HAVE_ASTRBOT:
        return None

    file_size = 0
    if local_path and local_path.exists():
        try:
            file_size = local_path.stat().st_size
        except OSError:
            pass
    _log_i(
        f"[chain._audio] local_path={local_path} exists={local_path and local_path.exists()} "
        f"size={file_size}MB={file_size // 1024 // 1024} as_record={as_record} "
        f"synth_video={synth_video_path}"
    )

    # 本地缓存路径优先
    if local_path and local_path.exists():
        # v0.3.7.5: 优先使用合成的封面视频
        if not as_record and synth_video_path and synth_video_path.exists():
            if Video is not None:
                try:
                    if hasattr(Video, "fromFileSystem"):
                        node = Video.fromFileSystem(str(synth_video_path))
                        _log_i(
                            f"[chain._audio] 使用 Video.fromFileSystem (合成视频): "
                            f"{synth_video_path}"
                        )
                        return node
                    if hasattr(Video, "fromFile"):
                        node = Video.fromFile(str(synth_video_path))
                        _log_i(
                            f"[chain._audio] 使用 Video.fromFile (合成视频): "
                            f"{synth_video_path}"
                        )
                        return node
                except Exception as exc:  # noqa: BLE001
                    _log_w(
                        f"[chain._audio] Video 合成视频构造失败: "
                        f"{type(exc).__name__}: {exc}"
                    )

        if as_record:
            try:
                node = Record.fromFile(str(local_path))
                _log_i(f"[chain._audio] 使用 Record.fromFile: {local_path}")
                return node
            except Exception as exc:  # noqa: BLE001
                _log_w(f"[chain._audio] Record.fromFile 失败: {type(exc).__name__}: {exc}")
        if Video is not None:
            try:
                if hasattr(Video, "fromFileSystem"):
                    node = Video.fromFileSystem(str(local_path))
                    _log_i(f"[chain._audio] 使用 Video.fromFileSystem: {local_path}")
                    return node
                if hasattr(Video, "fromFile"):
                    node = Video.fromFile(str(local_path))
                    _log_i(f"[chain._audio] 使用 Video.fromFile: {local_path}")
                    return node
            except Exception as exc:  # noqa: BLE001
                _log_w(f"[chain._audio] Video 本地构造失败: {type(exc).__name__}: {exc}")
        try:
            node = _try_make_file(str(local_path), name_hint=local_path.name)
            _log_i(f"[chain._audio] 使用 File(file=...): {local_path}")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] File 构造失败: {type(exc).__name__}: {exc}")

    # 回退到远程 URL
    if as_record:
        try:
            node = Record.fromURL(meta.audio_url)
            _log_i(f"[chain._audio] 使用 Record.fromURL: {meta.audio_url[:60]}...")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] Record.fromURL 失败: {type(exc).__name__}: {exc}")
    if Video is not None and hasattr(Video, "fromURL"):
        try:
            node = Video.fromURL(meta.audio_url)
            _log_i(f"[chain._audio] 使用 Video.fromURL: {meta.audio_url[:60]}...")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] Video.fromURL 失败: {type(exc).__name__}: {exc}")
    try:
        fname = (meta.name or "audio") + "." + (meta.audio_format or "mp3")
        node = _try_make_file(meta.audio_url, name_hint=fname)
        _log_i(f"[chain._audio] 使用 File(url=...): {meta.audio_url[:60]}...")
        return node
    except Exception as exc:  # noqa: BLE001
        _log_w(f"[chain._audio] File(url=...) 构造失败: {type(exc).__name__}: {exc}")

    _log_i("[chain._audio] 所有 Record/Video/File 构造都失败，回退到 Plain 链接")
    return Plain(f"🔗 音频直链：{meta.audio_url}")


def _is_video_node(node) -> bool:
    """判断组件是否是 Video 类（QQ 视频气泡自带画面，不需要额外 Image 封面）。"""
    if node is None or Video is None:
        return False
    return isinstance(node, Video)


def build_song_chain(
    meta: SongMetadata,
    *,
    show_text: bool = True,
    show_cover: bool = True,
    show_audio: bool = True,
    show_lyric: bool = False,
    as_record: bool = True,
    local_audio_path: Optional[Path] = None,
    synth_video_path: Optional[Path] = None,
    output_mode: str = "video",
) -> list:
    """把 SongMetadata 渲染成 AstrBot 消息组件列表。

    v0.3.9：新增 output_mode 三选一:
      - "link":  文本 + 封面 + 直链 (Plain + Image + Plain URL) — 最稳，文件大也能看
      - "audio": 文本 + 封面 + 音频文件 (Plain + Image + Record/File/Video) — 需要下载发送
      - "video": 只发合成视频气泡 (Video only) — 封面+音频合一，无需额外文本/封面

    v0.3.7.5：接受 synth_video_path 参数（提前合成的封面+音频 视频文件）。
              传入后会优先使用合成视频，QQ 视频气泡每一帧都是封面。
    v0.3.7.4：Video 组件自带画面（QQ 视频气泡 = 封面 + 音频二合一），
    与额外发 Image 封面是重复的——会发两次图。
    """
    if not _HAVE_ASTRBOT:
        return [meta]

    chain: list = []

    if not meta.ok:
        return build_error_chain(meta)

    # 模式归一化
    mode = (output_mode or "video").lower().strip()
    if mode not in ("link", "audio", "video"):
        mode = "video"

    # === 模式 video: 只发合成视频气泡 ===
    if mode == "video":
        if synth_video_path and synth_video_path.exists() and Video is not None:
            try:
                if hasattr(Video, "fromFileSystem"):
                    chain.append(Video.fromFileSystem(str(synth_video_path)))
                elif hasattr(Video, "fromFile"):
                    chain.append(Video.fromFile(str(synth_video_path)))
                else:
                    chain.append(File(file=str(synth_video_path), name=synth_video_path.name))
                _log_i(f"[chain.build] mode=video 只发合成视频气泡: {synth_video_path}")
                return chain
            except Exception as exc:  # noqa: BLE001
                _log_w(f"[chain.build] 合成视频构造失败: {type(exc).__name__}: {exc}")
        # 合成视频不可用 -> 降级到 audio 模式
        _log_w("[chain.build] mode=video 但合成视频不可用，降级到 audio")
        mode = "audio"

    # === 模式 link / audio 共用前置：Plain 文本 + Image 封面 ===
    if mode in ("link", "audio"):
        if show_text:
            artists = " / ".join(meta.artists) if meta.artists else "未知艺术家"
            text = f"🎵 {meta.name} - {artists}"
            if meta.album:
                text += f"\n💿 {meta.album}"
            if meta.bitrate:
                text += f"\n🎚 音质：{meta.bitrate}"
            chain.append(Plain(text))

        if show_cover and meta.pic_url:
            try:
                chain.append(Image.fromURL(meta.pic_url))
            except Exception:
                chain.append(Plain(f"🖼 封面：{meta.pic_url}"))

        if mode == "link":
            # 只发文本+封面+直链，不下载不发音频
            if meta.audio_url:
                chain.append(Plain(f"🔗 音频直链：{meta.audio_url}"))
            if show_lyric and meta.lyric:
                chain.append(Plain(_truncate_lyric(meta.lyric)))
            return chain

        # mode == "audio": 发音频文件
        if show_audio and meta.audio_url:
            node = _audio_component(meta, local_audio_path, as_record, synth_video_path)
            if node is not None:
                chain.append(node)

        if show_lyric and meta.lyric:
            chain.append(Plain(_truncate_lyric(meta.lyric)))

    return chain


def build_error_chain(meta: SongMetadata) -> list:
    """统一错误展示。"""
    if not _HAVE_ASTRBOT:
        return [meta]
    platform_cn = "网易云" if meta.source == "netease" else "QQ 音乐"
    text = f"❌ {platform_cn} 解析失败：{meta.error or '未知错误'}"
    if meta.raw:
        text += f"\n🔗 {meta.raw}"
    return [Plain(text)]