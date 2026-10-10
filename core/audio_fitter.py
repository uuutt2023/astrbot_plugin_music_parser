"""音频体积治理：在 base64 膨胀之前把音频压到能安全发出去的体积。

为什么需要这个模块
------------------
AstrBot 的 aiocqhttp 适配器（`aiocqhttp_message_event.py`）对 Record / Image
是无条件 base64 的：

    if isinstance(segment, Image | Record):
        bs64 = await segment.convert_to_base64()

而 `Record.convert_to_base64()` 走的是 `MediaResolver(...).to_base64(
target_format="wav")` —— **它会先用 ffmpeg 把音频解码成 PCM WAV 再 base64**。

于是一首 4 分钟的无损 FLAC（≈40MB）的实际链路是：

    40MB FLAC → 42MB WAV (44.1kHz/16bit/立体声 = 176KB/s) → 56MB base64 字符串
              → 塞进一个 WebSocket JSON 帧发出去

56MB 的单帧既超过常见的 WS 帧上限，也让插件侧的超时保护先炸掉。这就是群里
那句「音频上传失败（文件 40MB 超限 / 网络超时）」的真正来源。

**注意：这跟用哪个 OneBot 协议端无关。** 膨胀发生在 AstrBot 内部，还没走到
NapCat / SnowLuma 那一步就爆了。SnowLuma 自己那侧的 record 上限是 1GiB。

所以修法是：在交给 AstrBot 之前，自己先把文件压到安全体积（默认 8MB）。
8MB 的 WAV → 约 10.7MB base64，稳稳落在 WS 帧限制内。

转码产物缓存在源文件旁边（`<name>.qq.mp3`），重复发送同一首歌直接复用，
和 `video_synthesizer` 的缓存策略一致。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from .logger import d as _log_d, i as _log_i, w as _log_w
from .video_synthesizer import find_ffmpeg

# WAV 未压缩 PCM 的码率（字节/秒），用于估算"这个源文件转成 wav 后会有多大"。
# 44.1kHz * 16bit * 2ch = 176400 B/s。这里按最常见的 44.1k/16bit/立体声估，
# 高规格源（24bit/96kHz）只会更大，所以这是个偏乐观的估算。
_WAV_BYTES_PER_SEC = 176_400

# 单条语音的时长硬顶。超过这个长度即使压到目标码率，base64 帧也会很肥，
# 且 QQ 语音气泡的体验也不佳 —— 这种直接交给调用方降级到 File / 直链。
_MAX_RECORD_SECONDS = 12 * 60

# 转码目标码率的取值区间
_MIN_BITRATE_KBPS = 48
_MAX_BITRATE_KBPS = 320


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def probe_duration(audio_path: Path) -> float:
    """探测音频时长（秒）。失败返回 0.0。"""
    ffmpeg = find_ffmpeg()
    ffprobe = Path(ffmpeg).with_name("ffprobe") if ffmpeg else None
    if ffprobe is None or not ffprobe.exists():
        # 没有 ffprobe 就退回 ffmpeg 自己的日志解析
        try:
            result = _run(
                [ffmpeg, "-i", str(audio_path), "-f", "null", "-"], timeout=30
            )
            text = (result.stderr or b"").decode("utf-8", errors="ignore")
            for line in text.splitlines():
                if "Duration:" in line:
                    stamp = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = stamp.split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception as exc:  # noqa: BLE001
            _log_w(f"[audio_fit] 探测时长失败: {type(exc).__name__}: {exc}")
        return 0.0
    try:
        out = _run(
            [
                str(ffprobe),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            timeout=20,
        )
        return float(out.stdout.decode().strip())
    except Exception as exc:  # noqa: BLE001
        _log_w(f"[audio_fit] ffprobe 失败: {type(exc).__name__}: {exc}")
        return 0.0


def _transcode_cache_path(src: Path) -> Path:
    """转码产物路径：<源名>.qq.mp3，和源文件同目录。"""
    return src.with_name(f"{src.stem}.qq.mp3")


def _cache_is_fresh(src: Path, out: Path) -> bool:
    try:
        return out.exists() and out.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def plan_target_bitrate_kbps(
    duration_sec: float, max_bytes: int, cap_kbps: int
) -> int:
    """按时长反推一个"刚好塞进 max_bytes"的码率，再夹到合理区间。

    max_bytes=8MB / 240s → 8000*8/240 ≈ 267kbps，但上限是 128 → 取 128。
    45 分钟的现场版 → 23kbps，低于下限 → 取 48（由调用方判超时后降级）。
    """
    if duration_sec <= 0:
        return max(_MIN_BITRATE_KBPS, min(cap_kbps, _MAX_BITRATE_KBPS))
    # 留 3% 余量给容器开销
    budget = max_bytes * 0.97
    needed = (budget * 8) / (duration_sec * 1000)
    return max(_MIN_BITRATE_KBPS, min(int(needed), cap_kbps))


async def fit_audio_for_record(
    src: Path,
    *,
    max_mb: int = 8,
    cap_bitrate_kbps: int = 128,
    enabled: bool = True,
) -> tuple[Optional[Path], str]:
    """把 src 压到能安全走 Record 组件的体积。

    返回 (可发送的路径, 说明)。说明用于日志和兜底提示：

      - ``(src, "原样")``       已经在阈值内，不用动
      - ``(xxx.qq.mp3, "转码")`` 转码后达标
      - ``(None, 原因)``         转码不可用，调用方应降级到 File / 直链
    """
    if not src or not src.exists():
        return None, "源文件不存在"

    try:
        size = src.stat().st_size
    except OSError as exc:  # noqa: BLE001
        return None, f"读取大小失败: {exc}"

    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    if size <= max_bytes:
        _log_d(f"[audio_fit] {src.name} {size // 1024}KB ≤ {max_mb}MB，原样发送")
        return src, "原样"

    if not enabled:
        return None, f"{size // 1024 // 1024}MB 超过 {max_mb}MB，转码已关闭"

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, f"{size // 1024 // 1024}MB 超过 {max_mb}MB，且找不到 ffmpeg"

    duration = probe_duration(src)
    if duration > _MAX_RECORD_SECONDS:
        return None, (
            f"时长 {int(duration // 60)} 分 {int(duration % 60)} 秒 "
            f"超过 {_MAX_RECORD_SECONDS // 60} 分钟上限"
        )

    out = _transcode_cache_path(src)
    if _cache_is_fresh(src, out):
        try:
            out_size = out.stat().st_size
        except OSError:
            out_size = 0
        if 0 < out_size <= max_bytes:
            _log_i(
                f"[audio_fit] 命中转码缓存: {out.name} "
                f"({out_size // 1024 // 1024}MB)"
            )
            return out, "转码(缓存)"

    bitrate = plan_target_bitrate_kbps(duration, max_bytes, cap_bitrate_kbps)
    est_wav = int(duration * _WAV_BYTES_PER_SEC) if duration else 0
    _log_i(
        f"[audio_fit] 源 {src.name} {size // 1024 // 1024}MB "
        f"(时长 {duration:.0f}s, 转 WAV 约 {est_wav // 1024 // 1024}MB "
        f"→ base64 约 {int(est_wav * 1.34) // 1024 // 1024}MB)，"
        f"超过 {max_mb}MB → 转 mp3 @{bitrate}kbps"
    )

    def _encode() -> tuple[bool, str]:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                ffmpeg, "-y",
                "-i", str(src),
                "-vn",                       # 不要视频流（有些 FLAC 内嵌封面）
                "-ac", "2",                   # 双声道（QQ 语音气泡兼容性最好）
                "-ar", "44100",
                "-c:a", "libmp3lame",
                "-b:a", f"{bitrate}k",
                str(out),
            ]
            result = _run(cmd, timeout=180)
            if result.returncode != 0:
                return False, (result.stderr or b"").decode("utf-8", errors="ignore")[-300:]
            if not out.exists() or out.stat().st_size == 0:
                return False, "输出文件为空"
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "ffmpeg 转码超时(180s)"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, _encode)

    if not ok:
        out.unlink(missing_ok=True)
        _log_w(f"[audio_fit] 转码失败: {err}")
        return None, f"转码失败: {err}"

    new_size = out.stat().st_size
    if new_size > max_bytes:
        # 一遍没压下来：清掉产物，交给调用方降级（不要拿一个仍然超标的文件去
        # 触发 base64 膨胀，那等于白转码还多占一份磁盘）
        out.unlink(missing_ok=True)
        _log_w(
            f"[audio_fit] 转码后仍 {new_size // 1024 // 1024}MB > {max_mb}MB，"
            f"放弃转码，交由调用方降级"
        )
        return None, f"转码后仍 {new_size // 1024 // 1024}MB，超出 {max_mb}MB"

    _log_i(
        f"[audio_fit] 转码完成: {src.name} {size // 1024 // 1024}MB "
        f"→ {out.name} {new_size // 1024 // 1024}MB @{bitrate}kbps"
    )
    return out, "转码"


def cleanup_transcode(src: Optional[Path], fitted: Optional[Path]) -> None:
    """只删转码产物，源文件由 DownloadManager 负责。"""
    if not fitted or not src:
        return
    if Path(fitted) == Path(src):
        return
    try:
        Path(fitted).unlink(missing_ok=True)
    except OSError:
        pass
