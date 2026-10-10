"""构造 AstrBot 消息链。

本插件对 AstrBot 消息组件层做最小依赖：
- 始终用 `Plain` 输出文本节点
- 用 `Image` 输出封面（远程 URL 即可）
- 用 `Record`（优先）或 `File`（降级）输出音频

为什么本地缓存？Record.fromFileSystem / File(file=...) 在缓存可用时优先使用
本地路径，否则回退到远程 URL；这样 QQ / 微信等风控严格的平台也能稳定发出。

v0.3.15 修复（AstrBot → OneBot 的真实链路踩坑）：
- **禁用「音频文件包成 Video 节点」的 fallback。** 旧版在 Record 构造失败时
  会把 .flac 直接塞进 Video(file=...)，结果被协议端拒收：
      SnowLuma: retcode=1400 'message element "video" must be the only
                segment in a message'
                (packages/protocol/src/element-manifest.ts)
  而且从语义上讲，音频文件本来就不该装进 video 段 —— 双重错误。
  现在 Video 只会从显式传入的 synth_video_path 构造。
- **Video 必须独占一条消息。** SnowLuma 明确拒绝 video 与其他段同发
  （QQ 客户端会把同级元素留在链上但不渲染）。新增 split_chain_for_send()，
  由 main.py 把 Video 拆成独立的 event.send()。
- **Record API 按能力探测**，不再直接调 Record.fromFile（AstrBot 4.26.8
  已移除该方法，老日志里 AttributeError 就是这么来的）。
- **超大文件不再直接退化成 File 附件。** 体积治理交给上层
  core/audio_fitter.py 先压成 mp3；这里只保留最后一道保险。
  走 File 的体验是"收到一个附件"，不是"点开就能听"，对音乐插件来说
  基本等于没发成功。
- 视频节点传裸绝对路径而不是 `file://` URI：AstrBot 的 Video.to_dict() 会用
  Path.as_uri() 自行拼 file://，协议端上传阶段可能报 retcode=100 fetch failed。

历史：v0.3.7「下载成功但不发送」、v0.3.13 video 模式 retcode=100，
两者的根因与处理已并入上面的 v0.3.15 段落。
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

# 走 Record 组件的本地文件体积上限（默认 8MB）。
# 依据：AstrBot 的 aiocqhttp 适配器会把 Record 无条件转成 base64 WAV
# （Record.convert_to_base64 → MediaResolver.to_base64(target_format="wav")），
# 8MB 音频转成 WAV 后约 10.7MB base64，稳稳落在 WebSocket 单帧限制内。
# 超过这个值请交给 core/audio_fitter.py 转码，不要硬发。
_RECORD_MAX_BYTES = 8 * 1024 * 1024  # 8MB


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
    """构造 File 组件（降级路径）。AstrBot 各版本 File API 不一致，按顺序尝试。

    v0.3.7 踩坑：File 类没有 .fromFile/.fromURL 静态方法（那是 Record 的）。
    v4.28.2 的签名是 File(name, file="", url="") —— name 是必填位置参数，
    所以第一个候选必须同时给 name，否则整条链都会落到最后一个 raise。
    """
    if name_hint:
        name = name_hint
    else:
        local = Path(path_or_url)
        name = local.name if local.exists() else "audio"

    # 优先尝试现代 API（AstrBot v4+）
    for kwargs in (
        {"file": path_or_url, "name": name},
        {"file": path_or_url, "name": name},
        {"path": path_or_url, "name": name},
        {"url": path_or_url, "name": name},
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


def _make_record(path_or_url: str, *, local: bool):
    """构造 Record 组件，按各版本实际拥有的 API 依次尝试。

    v0.3.15 修复：老版本直接调 `Record.fromFile()`，而 AstrBot 4.26.8 已经
    把它删了，日志里那句
        AttributeError: type object 'Record' has no attribute 'fromFile'
    就是这么来的。这里改成能力探测：

      1. 本地文件 → Record.fromFileSystem(path)（4.26.8+ 的标准入口）
      2. 本地文件 → Record(file=file:// URI)（构造器兜底）
      3. 远程 URL → Record.fromURL(url)
      4. 最后直接 Record(file=...)
    """
    attempts = []
    if local:
        p = Path(path_or_url)
        if hasattr(Record, "fromFileSystem"):
            attempts.append(("fromFileSystem", lambda: Record.fromFileSystem(str(p))))
        attempts.append(
            ("file=uri", lambda: Record(file=p.resolve(strict=False).as_uri()))
        )
        attempts.append(("file=abs", lambda: Record(file=str(p.resolve(strict=False)))))
    else:
        if hasattr(Record, "fromURL"):
            attempts.append(("fromURL", lambda: Record.fromURL(path_or_url)))
        attempts.append(("file=url", lambda: Record(file=path_or_url)))

    last_err: Exception | None = None
    for label, factory in attempts:
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            _log_d(f"[chain.record] {label} 不可用: {type(exc).__name__}: {exc}")
    raise AttributeError(
        f"无法构造 Record（已尝试 {[a[0] for a in attempts]}）: {last_err}"
    )


def _audio_component(
    meta: SongMetadata,
    local_path: Optional[Path],
    as_record: bool,
    synth_video_path: Optional[Path] = None,
):
    """选择 Video / Record / File 作为音频承载组件。

    v0.3.15 的三条硬规则（每条都对应一个真实踩过的坑）：

    1. **Video 只能来自 synth_video_path。** 旧版在 Record 构造失败时会把本地
       音频文件塞进 Video(file=.flac)，被协议端拒收（SnowLuma retcode=1400：
       video 必须是唯一消息段）。音频文件装进 video 段本来就是错的。
    2. **超过 _RECORD_MAX_BYTES 就不硬发。** 走 Record 会被 AstrBot 转成
       base64 WAV，大文件必然撑爆 WebSocket 单帧。正常路径由
       core/audio_fitter.py 先转码；这里只是最后一道保险 —— 压不下来说明
       该降级到 File（群文件）而不是冒险发一条注定失败的消息。
    3. **File 只在明确降级时用。** File 对用户是"收到个附件"，对音乐插件
       来说体验接近失败，所以放在 Record 之后。
    """
    if not _HAVE_ASTRBOT:
        return None

    file_size = 0
    if local_path and Path(local_path).exists():
        try:
            file_size = Path(local_path).stat().st_size
        except OSError:
            pass
    _log_i(
        f"[chain._audio] local_path={local_path} exists={bool(local_path and Path(local_path).exists())} "
        f"size={file_size}MB={file_size // 1024 // 1024} as_record={as_record} "
        f"synth_video={synth_video_path}"
    )

    # ── 规则 1：Video 只认合成视频，且必须是 .mp4 之类的真视频 ──
    if (
        not as_record
        and synth_video_path
        and Path(synth_video_path).exists()
        and Video is not None
    ):
        abs_video_path = str(Path(synth_video_path).resolve())
        for kwargs in ({"file": abs_video_path, "path": abs_video_path}, {"file": abs_video_path}):
            try:
                node = Video(**kwargs)
                _log_i(f"[chain._audio] 使用 Video(合成视频): {abs_video_path}")
                return node
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                _log_w(f"[chain._audio] Video 构造异常: {type(exc).__name__}: {exc}")
                break
        _log_w("[chain._audio] Video 构造失败，降级到 audio 组件")

    local_ok = bool(local_path and Path(local_path).exists())
    # 规则 2 的判定结果。超标就整条 Record 路径都别走 —— 包括 URL 那条，
    # 因为 URL 指向的就是同一个超大文件，走过去只是换个姿势再炸一次。
    oversize = local_ok and file_size > _RECORD_MAX_BYTES

    # ── Record：本地文件优先 ──
    if as_record and local_ok and not oversize:
        try:
            node = _make_record(str(local_path), local=True)
            _log_i(f"[chain._audio] 使用 Record(本地): {local_path}")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] Record 构造失败: {type(exc).__name__}: {exc}")

    # ── Record：只在完全没有本地文件时才走 URL ──
    if as_record and not local_ok and meta.audio_url:
        try:
            node = _make_record(meta.audio_url, local=False)
            _log_i(f"[chain._audio] 使用 Record(URL): {meta.audio_url[:60]}...")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] Record(URL) 构造失败: {type(exc).__name__}: {exc}")

    if oversize:
        # ── 规则 2：体积超标，明确说明为什么不用 Record ──
        _log_i(
            f"[chain._audio] 本地文件 {file_size // 1024 // 1024}MB > "
            f"{_RECORD_MAX_BYTES // 1024 // 1024}MB 上限（AstrBot 会转成 base64 WAV，"
            f"体积约 {int(file_size * 176_400 / 44_100 * 1.34 / 1024 / 1024)}MB，"
            f"会撑爆 WebSocket 单帧），降级到 File 附件"
        )

    # ── 规则 3：File 降级（附件形态，但至少不丢文件）──
    if local_path and Path(local_path).exists():
        try:
            node = _try_make_file(str(local_path), name_hint=Path(local_path).name)
            _log_i(f"[chain._audio] 降级使用 File(本地): {local_path}")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] File 构造失败: {type(exc).__name__}: {exc}")

    if meta.audio_url:
        try:
            fname = (meta.name or "audio") + "." + (meta.audio_format or "mp3")
            node = _try_make_file(meta.audio_url, name_hint=fname)
            _log_i(f"[chain._audio] 降级使用 File(URL): {meta.audio_url[:60]}...")
            return node
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[chain._audio] File(URL) 构造失败: {type(exc).__name__}: {exc}")

    _log_i("[chain._audio] Record/File 都构造失败，回退到 Plain 直链")
    return Plain(f"🔗 音频直链：{meta.audio_url}")


def _is_video_node(node) -> bool:
    """判断组件是否是 Video 类（QQ 视频气泡自带画面，不需要额外 Image 封面）。"""
    if node is None or Video is None:
        return False
    return isinstance(node, Video)


def split_chain_for_send(chain: list) -> list[list]:
    """把消息链拆成"可分别发送"的几组，Video 单独成组。

    为什么必须拆：SnowLuma 在 `assertVideoSendPolicy` 里明确拒绝 video 与
    其他段同发 ——

        'message element "video" must be the only segment in a message'

    原因写在它的注释里：QQ 客户端把视频当独立消息，同级元素会留在链上但不
    渲染。与其让协议端报错，不如我们自己拆开。

    返回的每一组都通过 event.send(event.chain_result(组)) 单独发一次。
    非 video 的段按原顺序合并成第一组，保持"文本+封面+音频"的阅读顺序。
    """
    normal: list = []
    groups: list[list] = []
    for node in chain:
        if _is_video_node(node):
            if normal:
                groups.append(normal)
                normal = []
            groups.append([node])
        else:
            normal.append(node)
    if normal:
        groups.append(normal)
    return groups


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
    output_mode: str = "audio",
) -> list:
    """把 SongMetadata 渲染成 AstrBot 消息组件列表。

    v0.3.9：新增 output_mode 三选一:
      - "link":  文本 + 封面 + 直链 (Plain + Image + Plain URL) — 最稳，文件大也能看
      - "audio": 文本 + 封面 + 音频文件 (Plain + Image + Record/File) — 需要下载发送
      - "video": 只发合成视频气泡 (Video only) — 封面+音频合一，无需额外文本/封面

    v0.3.15：
    - 返回的链**可能含 Video 节点**，调用方必须用 `split_chain_for_send()` 拆开
      分条发送。协议端（SnowLuma assertVideoSendPolicy）要求 video 独占一条消息。
    - `local_audio_path` 传进来的应当是**已经过体积治理**的文件（见
      core/audio_fitter.py）。本模块只做最后一道体积保险，不再自己转码。

    v0.3.7.4：Video 组件自带画面（QQ 视频气泡 = 封面 + 音频二合一），
    与额外发 Image 封面是重复的——会发两次图。
    """
    if not _HAVE_ASTRBOT:
        return [meta]

    chain: list = []

    if not meta.ok:
        return build_error_chain(meta)

    # 模式归一化
    mode = (output_mode or "audio").lower().strip()
    if mode not in ("link", "audio", "video"):
        mode = "audio"

    # === 模式 video: 只发合成视频气泡 ===
    if mode == "video":
        if synth_video_path and Path(synth_video_path).exists() and Video is not None:
            # 传裸绝对路径而不是 Video.fromFileSystem（后者会拼 file:// URI，
            # 协议端上传阶段可能 fetch 失败）。
            # 提醒：返回的 Video 节点必须由 main.py 用 split_chain_for_send
            # 拆成独占一条消息，否则协议端会拒收（video 必须是唯一段）。
            abs_video_path = str(Path(synth_video_path).resolve())
            video_node = None
            for kwargs in ({"file": abs_video_path, "path": abs_video_path}, {"file": abs_video_path}):
                try:
                    video_node = Video(**kwargs)
                    break
                except TypeError:
                    continue
                except Exception as exc:  # noqa: BLE001
                    _log_w(
                        f"[chain.build] Video 构造异常: {type(exc).__name__}: {exc}"
                    )
                    break
            if video_node is not None:
                _log_i(
                    f"[chain.build] mode=video 只发合成视频气泡: {abs_video_path} "
                    f"（将由 main.py 拆成独立消息发送）"
                )
                return [video_node]
        # 合成视频不可用或 Video 构造失败 -> 降级到 audio 模式
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
        # local_audio_path 存在即可发（转码产物也算），不强依赖 meta.audio_url
        has_local = bool(local_audio_path and Path(local_audio_path).exists())
        if show_audio and (meta.audio_url or has_local):
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