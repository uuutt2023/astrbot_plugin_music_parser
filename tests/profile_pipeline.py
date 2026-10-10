"""端到端性能剖析：真实跑 _send_one，测「用户等多久」+「bot 被阻塞多久」。

这是对 async 机器人插件最有意义的两个指标：
  - 总耗时：用户从发链接到收到语音等了多久
  - event loop 最长卡顿：期间整个 bot（对**所有**消息）无响应多久

跑：python3 tests/profile_pipeline.py
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fake_env  # noqa: E402

_fake_env.install()

from _fake_env import FakeEvent, LagMonitor, make_meta  # noqa: E402

import astrbot_plugin_music_parser.main as main_mod  # noqa: E402


def make_audio(path: Path, seconds: int = 240) -> Path:
    """造一段不可压缩的无损音频（模拟线上 40MB FLAC）。"""
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=color=pink:duration={seconds}:amplitude=0.9",
         "-ac", "2", "-ar", "44100", "-c:a", "flac", str(path)],
        capture_output=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-300:])
    return path


_COVER_SRV: list = []


def start_cover_server(tmp: Path) -> str:
    """起一个本地静态服务提供封面图，模拟真实场景（有封面 URL）。

    真实线上每首歌都有 pic_url，audio 模式以前正是在这个分支上白合成视频。
    """
    import http.server
    import os
    import threading

    img = tmp / "c.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x640:d=1",
         "-frames:v", "1", str(img)], capture_output=True, timeout=60,
    )
    cwd = Path.cwd()
    os.chdir(tmp)
    srv = http.server.HTTPServer(("127.0.0.1", 0),
                                 http.server.SimpleHTTPRequestHandler)
    srv.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _COVER_SRV.append(srv)
    os.chdir(cwd)
    return f"http://127.0.0.1:{srv.server_address[1]}/c.jpg"


def stop_cover_server():
    for srv in _COVER_SRV:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _COVER_SRV.clear()


async def run_case(name: str, cfg: dict, seconds: int = 240, cover: bool = False):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio = make_audio(tmp / "song.flac", seconds=seconds)
        src_mb = audio.stat().st_size / 1024 / 1024

        plugin = main_mod.MusicParserPlugin(None, cfg)
        plugin.download_manager.schedule_cleanup = lambda p: None

        async def fake_download(_meta):
            return audio

        plugin._download_audio = fake_download

        pic_url = start_cover_server(tmp) if cover else ""
        meta = make_meta(audio_url="https://example.com/a.flac",
                         audio_format="flac", pic_url=pic_url)

        ev = FakeEvent()
        mon = LagMonitor()
        t0 = time.perf_counter()
        async with mon:
            await plugin._send_one(ev, meta)
        elapsed = time.perf_counter() - t0

        chain = ev.sent[0][1] if ev.sent and ev.sent[0][0] == "chain" else []
        kinds = [type(n).__name__ for n in chain]
        print(f"\n[{name}]")
        print(f"  源文件        {src_mb:.1f}MB / {seconds}s"
              f"{' + 真实封面 URL' if cover else '（无封面）'}")
        print(f"  输出消息      {len(ev.sent)} 条, 链构成 {kinds}")
        print(f"  总耗时        {elapsed:.2f}s")
        print(f"  loop 最长卡顿 {mon.max_lag:.3f}s")
        return {"elapsed": elapsed, "lag": mon.max_lag, "sent": len(ev.sent),
                "kinds": kinds, "src_mb": src_mb}


async def probe_wasted_synthesis():
    """量化一个具体问题：audio 模式下合成出来的视频有没有被用上。"""
    print("\n" + "=" * 62)
    print("专项：audio 模式下封面视频合成是否被浪费")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        audio = make_audio(tmp / "song.flac", seconds=240)

        plugin = main_mod.MusicParserPlugin(None, {"message": {"output_mode": "audio"}})
        meta = make_meta(audio_url="x", pic_url="")  # pic_url 空 → 合成被跳过

        # pic_url 有值时才会真合成；这里给一张真图
        img = tmp / "c.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x640:d=1",
             "-frames:v", "1", str(img)], capture_output=True, timeout=60,
        )
        import threading

        import http.server

        os_cwd = Path.cwd()
        import os

        os.chdir(tmp)
        srv = http.server.HTTPServer(("127.0.0.1", 0),
                                     http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/c.jpg"
        srv.shutdown()
        os.chdir(os_cwd)

        meta = make_meta(audio_url="x", pic_url=url)

        cfg_out = plugin._compute_output_config(meta)
        print(f"  output_mode={cfg_out['output_mode']} send_as_record={cfg_out['send_as_record']}")

        t0 = time.perf_counter()
        mon = LagMonitor()
        async with mon:
            video = await plugin._maybe_synth_video(meta, audio, cfg_out)
        synth_time = time.perf_counter() - t0

        used = None
        if video:
            from astrbot_plugin_music_parser.utils.message_builder import _audio_component

            node = _audio_component(meta, audio, cfg_out["send_as_record"], video)
            used = type(node).__name__
        print(f"  合成耗时      {synth_time:.2f}s  → {video.name if video else None}")
        print(f"  最终音频组件  {used}")
        print(f"  loop 卡顿     {mon.max_lag:.3f}s")
        if video and used != "Video":
            print(f"  ⚠ 浪费：合成出来的视频（{video.stat().st_size // 1024 // 1024}MB）"
                  f"在 audio 模式下从未被使用")


async def main():
    if not shutil.which("ffmpeg"):
        print("需要 ffmpeg 才能跑本剖析脚本")
        return

    print("=" * 62)
    print("端到端性能剖析 _send_one")
    print("=" * 62)

    results = {}
    results["audio"] = await run_case(
        "A. audio 模式 + 真实封面（默认，26MB 无损 / 240s）",
        {"message": {"output_mode": "audio", "record_max_mb": 8}},
        cover=True,
    )
    results["video"] = await run_case(
        "B. video 模式",
        {"message": {"output_mode": "video"}},
    )
    results["link"] = await run_case(
        "C. link 模式",
        {"message": {"output_mode": "link"}},
    )

    stop_cover_server()

    print("\n" + "=" * 62)
    print("汇总")
    print("=" * 62)
    print(f"{'场景':<8}{'总耗时':>10}{'loop 卡顿':>12}")
    for k in ("audio", "video", "link"):
        r = results[k]
        print(f"{k:<8}{r['elapsed']:>9.2f}s{r['lag']:>11.3f}s")
    stop_cover_server()


if __name__ == "__main__":
    asyncio.run(main())
