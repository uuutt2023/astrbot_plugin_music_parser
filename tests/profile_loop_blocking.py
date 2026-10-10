"""逐个函数定位 event loop 卡顿。

跑：python3 tests/profile_loop_blocking.py

注意：卡顿探针（_fake_env.LagMonitor）本身必须可信才有意义，
所以本脚本第一步先做仪器校准；校准不过直接退出，不输出任何结论。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fake_env  # noqa: E402

_fake_env.install()

from _fake_env import LagMonitor  # noqa: E402


def make_audio(path: Path, seconds: int = 60) -> Path:
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=color=pink:duration={seconds}:amplitude=0.9",
         "-ac", "2", "-ar", "44100", "-c:a", "flac", str(path)],
        capture_output=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-300:])
    return path


def calibrate() -> bool:
    """仪器自检：已知阻塞必须被测出来，纯异步等待必须不被算成卡顿。"""
    print("=" * 62)
    print("仪器校准（不可信就中止）")
    print("=" * 62)
    ok = True

    async def run():
        nonlocal ok
        for block in (0.20, 0.50):
            async with LagMonitor() as m:
                time.sleep(block)
            good = abs(m.max_lag - block) < 0.05
            ok = ok and good
            print(f"  阻塞 {block:.2f}s → 测得 {m.max_lag:.3f}s  {'✓' if good else '✗'}")

        async with LagMonitor() as m:
            await asyncio.sleep(0.30)
        good = m.max_lag < 0.05
        ok = ok and good
        print(f"  纯异步 0.30s → 测得 {m.max_lag:.3f}s  {'✓' if good else '✗'}")

    asyncio.run(run())
    print(f"\n仪器可信: {'是' if ok else '否'}")
    return ok


async def measure(name: str, coro_factory):
    async with LagMonitor() as m:
        t0 = time.perf_counter()
        await coro_factory()
        elapsed = time.perf_counter() - t0
    print(f"  {name:<46} 耗时 {elapsed:6.2f}s  卡顿 {m.max_lag:.3f}s")
    return m.max_lag


async def main():
    from astrbot_plugin_music_parser.core.audio_fitter import (
        fit_audio_for_record,
        probe_duration,
        probe_duration_async,
    )
    from astrbot_plugin_music_parser.core.video_synthesizer import (
        synthesize_cover_audio_video,
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio60 = make_audio(tmp / "a60.flac", 60)
        audio240 = make_audio(tmp / "a240.flac", 240)

        print("\n" + "=" * 62)
        print("逐函数 event loop 卡顿")
        print("=" * 62)

        async def synth():
            await synthesize_cover_audio_video(
                audio_path=audio60, cover_url=None,
                output_path=tmp / "o.mp4", fps=2, max_width=640, max_height=640,
            )

        await measure("synthesize_cover_audio_video（无 URL 封面）", synth)

        async def fit240():
            await fit_audio_for_record(audio240, max_mb=1, cap_bitrate_kbps=128)

        await measure("fit_audio_for_record（26MB → mp3）", fit240)

        async def probe_sync():
            probe_duration(audio240)

        await measure("probe_duration（同步调用，故意阻塞）", probe_sync)

        async def probe_async():
            await probe_duration_async(audio240)

        await measure("probe_duration_async（executor 版）", probe_async)

        print("\n  对照：上一行应显著小于上一行 —— 这就是 v0.3.16 的修复点")


if __name__ == "__main__":
    if not calibrate():
        sys.exit("仪器不可信，拒绝输出性能结论")
    print()
    asyncio.run(main())
