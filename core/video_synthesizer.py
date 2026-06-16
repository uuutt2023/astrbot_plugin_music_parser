"""合成 封面+音频 视频文件。

QQ 视频气泡的画面 = 视频文件的每一帧。如果直接发 FLAC 音频，
QQ 客户端会用默认图标或音频文件的内嵌封面（不一定正确）。

本模块把歌曲封面烧进视频文件，确保视频气泡的每一帧都是用户想要的封面图。
依赖：ffmpeg（系统 PATH 或者 imageio-ffmpeg Python 嵌入版）。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from .logger import i as _log_i, w as _log_w

_FFMPEG_EXE: Optional[str] = None
_FFMPEG_PROBED = False


def find_ffmpeg() -> Optional[str]:
    """探测可用的 ffmpeg。

    优先级：
    1. imageio-ffmpeg Python 嵌入版（pip 装，无需系统依赖）
    2. 系统 PATH 里的 ffmpeg
    """
    global _FFMPEG_EXE, _FFMPEG_PROBED
    if _FFMPEG_PROBED:
        return _FFMPEG_EXE
    _FFMPEG_PROBED = True

    # 1. imageio-ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
        if _FFMPEG_EXE and Path(_FFMPEG_EXE).exists():
            _log_i(f"[video_synth] 使用 imageio-ffmpeg: {_FFMPEG_EXE}")
            return _FFMPEG_EXE
    except Exception:
        pass

    # 2. 系统 ffmpeg
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        _FFMPEG_EXE = sys_ffmpeg
        _log_i(f"[video_synth] 使用系统 ffmpeg: {_FFMPEG_EXE}")
        return _FFMPEG_EXE

    _log_w("[video_synth] 未找到 ffmpeg，将无法合成 封面+音频 视频")
    return None


async def _download_cover(cover_url: str, target: Path) -> bool:
    """下载封面图到本地。失败返回 False。"""
    if not cover_url:
        return False
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            async with session.get(cover_url, timeout=timeout) as resp:
                if resp.status >= 400:
                    _log_w(f"[video_synth] 下载封面失败 HTTP {resp.status}: {cover_url[:60]}")
                    return False
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if chunk:
                            f.write(chunk)
        return target.exists() and target.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        _log_w(f"[video_synth] 下载封面异常: {type(exc).__name__}: {exc}")
        return False


def _extract_embedded_cover(audio_path: Path, target: Path) -> bool:
    """从音频文件内嵌封面提取（FLAC/MP3 通常有）。失败返回 False。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # -an: 不要音频流；-vn: 不要视频流。这里我们要视频流（封面）
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i", str(audio_path),
                "-an",  # 忽略音频
                "-vcodec", "copy",
                str(target),
            ],
            capture_output=True,
            timeout=20,
        )
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        _log_w(f"[video_synth] 提取内嵌封面失败: {type(exc).__name__}: {exc}")
        return False


def _probe_cover_size(cover_path: Path) -> Tuple[int, int]:
    """探测封面图原始尺寸 (w, h)。失败返回 (1920, 1080) 默认值。"""
    try:
        from PIL import Image

        with Image.open(cover_path) as img:
            w, h = img.size
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    try:
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(cover_path),
        ]
        out = subprocess.check_output(
            probe_cmd, stderr=subprocess.STDOUT, timeout=10
        ).decode().strip()
        w, h = out.split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _calc_video_size(
    cover_w: int, cover_h: int, max_w: int = 1920, max_h: int = 1080
) -> Tuple[int, int]:
    """按封面原比例缩放到 max_w x max_h 范围内（不裁切，居中黑边）。
    原图小于上限时不放大（保持原图尺寸，避免画质损失）。

    例子（max 1920x1080）:
      封面 4000x4000 (1:1) -> 视频 1080x1080
      封面 1920x1080 (16:9) -> 视频 1920x1080
      封面 1080x1920 (9:16) -> 视频 608x1080（等比缩放，高度受限）
      封面 4000x2250 (16:9) -> 视频 1920x1080
      封面 1000x1000 -> 视频 1000x1000（小于上限，不放大）
    """
    if cover_w <= 0 or cover_h <= 0:
        return max_w, max_h
    # 计算等比缩放系数，但不放大（scale <= 1）
    scale = min(max_w / cover_w, max_h / cover_h, 1.0)
    new_w = int(cover_w * scale)
    new_h = int(cover_h * scale)
    # h264 要求宽高偶数
    if new_w % 2 != 0:
        new_w += 1
    if new_h % 2 != 0:
        new_h += 1
    if new_w < 16:
        new_w = 16
    if new_h < 16:
        new_h = 16
    return new_w, new_h


def _run_ffmpeg_synth(
    audio_path: Path,
    cover_path: Path,
    output_path: Path,
    *,
    fps: int = 2,
    max_width: int = 1920,
    max_height: int = 1080,
) -> bool:
    """同步 ffmpeg 合成。封面作为画面（循环），音频保留原始质量。

    v0.3.10: 视频尺寸 = max(1920)xmax(1080) 上限，按封面比例缩放，居中黑边。
              帧数 fps 可配（默认 2）。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 探测音频时长
        try:
            probe_cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ]
            duration_str = subprocess.check_output(
                probe_cmd, stderr=subprocess.STDOUT, timeout=10
            ).decode().strip()
            duration_sec = float(duration_str)
        except Exception:
            duration_sec = 0

        # 按封面比例计算视频尺寸（不超出 max_width x max_height）
        cover_w, cover_h = _probe_cover_size(cover_path)
        target_w, target_h = _calc_video_size(
            cover_w, cover_h, max_width, max_height
        )
        # fps 安全值
        fps = max(1, min(int(fps), 30))
        _log_i(
            f"[video_synth] 封面原尺寸 {cover_w}x{cover_h} -> "
            f"视频尺寸 {target_w}x{target_h} (max {max_width}x{max_height}, fps={fps})"
        )

        # 命令行参数：
        # -r <fps> -loop 1: 循环图片作为视频帧
        # -i cover: 输入图
        # -i audio: 输入音频
        # -c:v libx264 -tune stillimage: 针对静态图优化的编码
        # -c:a aac -b:a 256k: 音频转 AAC 256k（QQ 视频气泡兼容性最好）
        # -t <duration>: 用音频时长
        # -pix_fmt yuv420p: 兼容性
        # -vf scale=W:H:force_original_aspect_ratio=decrease: 等比缩放，不裁切，居中黑边
        cmd = [
            ffmpeg,
            "-y",
            "-r", str(fps),
            "-loop", "1",
            "-i", str(cover_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "256k",
            "-pix_fmt", "yuv420p",
            "-vf",
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black",
        ]
        if duration_sec > 0:
            cmd.extend(["-t", str(duration_sec)])
        cmd.append(str(output_path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="ignore")[-300:]
            _log_w(f"[video_synth] ffmpeg 合成失败 rc={result.returncode}: {stderr}")
            return False
        if not output_path.exists() or output_path.stat().st_size < 1024:
            _log_w(f"[video_synth] ffmpeg 输出文件无效: {output_path}")
            return False
        return True
    except subprocess.TimeoutExpired:
        _log_w(f"[video_synth] ffmpeg 合成超时（120s）")
        return False
    except Exception as exc:  # noqa: BLE001
        _log_w(f"[video_synth] ffmpeg 合成异常: {type(exc).__name__}: {exc}")
        return False


async def synthesize_cover_audio_video(
    *,
    audio_path: Path,
    cover_url: Optional[str],
    output_path: Path,
    fps: int = 2,
    max_width: int = 1920,
    max_height: int = 1080,
) -> Tuple[bool, str]:
    """合成 封面+音频 的 mp4 视频。返回 (成功, 备注)。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "no ffmpeg"

    audio_path = Path(audio_path)
    output_path = Path(output_path)
    if not audio_path.exists():
        return False, f"audio not found: {audio_path}"

    # 1) 准备封面图
    cover_path = output_path.parent / f".{output_path.name}.cover.jpg"
    cover_ok = False
    cover_source = ""

    # 1a) 优先下载 URL 封面
    if cover_url:
        if await _download_cover(cover_url, cover_path):
            cover_ok = True
            cover_source = "url"

    # 1b) 退回：从音频文件内嵌封面提取
    if not cover_ok:
        if _extract_embedded_cover(audio_path, cover_path):
            cover_ok = True
            cover_source = "embedded"

    if not cover_ok:
        # 兜底：生成一张纯黑占位图（ffmpeg 可以接受，但视频气泡会黑屏）
        try:
            from PIL import Image

            img = Image.new("RGB", (1920, 1080), color=(30, 30, 30))
            img.save(cover_path, "JPEG", quality=85)
            cover_ok = True
            cover_source = "placeholder"
        except Exception:
            return False, "no cover image available"

    _log_i(
        f"[video_synth] 准备合成: audio={audio_path.name} "
        f"cover={cover_path.name} ({cover_source}) -> {output_path.name}"
    )

    # 2) ffmpeg 合成（丢到线程池避免阻塞 event loop）
    loop = asyncio.get_event_loop()

    def _synth_call():
        return _run_ffmpeg_synth(
            audio_path,
            cover_path,
            output_path,
            fps=fps,
            max_width=max_width,
            max_height=max_height,
        )

    synth_ok = await loop.run_in_executor(None, _synth_call)

    # 清理临时封面
    try:
        cover_path.unlink(missing_ok=True)
    except Exception:
        pass

    if synth_ok:
        size = output_path.stat().st_size
        _log_i(
            f"[video_synth] 合成成功: {output_path.name} "
            f"({size // 1024 // 1024}MB, cover={cover_source})"
        )
        return True, cover_source

    # 失败清理
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False, "ffmpeg failed"


async def synthesize_to_temp(
    *,
    audio_path: Path,
    cover_url: Optional[str],
    fps: int = 2,
    max_width: int = 1920,
    max_height: int = 1080,
) -> Optional[Path]:
    """合成视频到 audio 同目录的临时文件，返回合成后的路径。失败返回 None。"""
    output_path = audio_path.with_suffix(".cover.mp4")
    if output_path.exists():
        # 缓存命中（同一首歌），直接复用
        try:
            # 检查时效：mtime 比 audio 新就行
            if output_path.stat().st_mtime >= audio_path.stat().st_mtime:
                _log_i(f"[video_synth] 命中缓存: {output_path.name}")
                return output_path
        except OSError:
            pass
    ok, info = await synthesize_cover_audio_video(
        audio_path=audio_path,
        cover_url=cover_url,
        output_path=output_path,
        fps=fps,
        max_width=max_width,
        max_height=max_height,
    )
    return output_path if ok else None