"""v0.3.15 回归测试：Record/video 发送链路。

用假的 astrbot.message_components 顶替真实组件，覆盖三个真实踩过的坑：
  1. 40MB FLAC → base64 WAV 膨胀（audio_fitter 转码）
  2. Record.fromFile 已不存在（_make_record 能力探测）
  3. video 必须是唯一消息段（split_chain_for_send）

跑：python3 tests/test_send_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# ── 先把假的 astrbot 包塞进 sys.modules，再 import 被测模块 ──


class Plain:
    def __init__(self, text: str, **_):
        self.text = text


class Image:
    def __init__(self, file: str | None = None, **_):
        self.file = file
        self.url = file

    @staticmethod
    def fromURL(url, **_):
        return Image(file=url)


class Record:
    """模拟 AstrBot 4.28.2：只有 fromFileSystem/fromURL/fromBase64，没有 fromFile。"""

    def __init__(self, file: str | None = None, **kw):
        self.file = file
        self.url = kw.get("url")
        self.path = kw.get("path")

    @staticmethod
    def fromFileSystem(path, **_):
        p = Path(path).resolve(strict=False)
        return Record(file=p.as_uri(), path=str(p))

    @staticmethod
    def fromURL(url, **_):
        return Record(file=url)


class File:
    def __init__(self, name: str, file: str = "", url: str = ""):
        self.name = name
        self.file_ = file
        self.url = url


class Video:
    def __init__(self, file: str, **_):
        self.file = file
        self.path = _.get("path")


def _install_fake_astrbot():
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = Plain
    components.Image = Image
    components.Record = Record
    components.File = File
    components.Video = Video

    api = types.ModuleType("astrbot.api")
    api.message_components = components
    core = types.ModuleType("astrbot.core")
    star_pkg = types.ModuleType("astrbot.core.star")
    filter_pkg = types.ModuleType("astrbot.core.star.filter")
    evt_mod = types.ModuleType("astrbot.core.star.filter.event_message_type")

    class EventMessageType:
        ALL = "all"

    evt_mod.EventMessageType = EventMessageType
    filter_pkg.event_message_type = evt_mod
    star_pkg.filter = filter_pkg

    class FakeStar:
        """Star 基类。真实环境里 __init__(context) 会被子类调用。"""

        def __init__(self, context=None, *a, **k):
            self.context = context

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    star_mod.Star = FakeStar
    star_mod.Context = object

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = types.SimpleNamespace(
        event_message_type=lambda *a, **k: (lambda fn: fn),
        command=lambda *a, **k: (lambda fn: fn),
    )
    event_mod.AstrMessageEvent = object

    # aiohttp 真实环境才有，这里给个最小桩 —— 测试不走网络
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, **kw):
            self.kw = kw

    class ClientSession:
        def __init__(self, *a, **kw):
            self.closed = False

        async def close(self):
            self.closed = True

    class ClientError(Exception):
        pass

    aiohttp_stub.ClientTimeout = ClientTimeout
    aiohttp_stub.ClientSession = ClientSession
    aiohttp_stub.ClientError = ClientError

    for name, mod in {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": components,
        "astrbot.core": core,
        "astrbot.core.star": star_pkg,
        "astrbot.core.star.filter": filter_pkg,
        "astrbot.core.star.filter.event_message_type": evt_mod,
        "aiohttp": aiohttp_stub,
    }.items():
        sys.modules[name] = mod


_install_fake_astrbot()

# 插件内部用相对导入（from ..core.parsers.base import ...），必须以包的形式导入。
# 插件目录本身叫 repo，所以在临时目录里建个别名软链，避免污染工作区。
import os  # noqa: E402
import tempfile as _tf  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALIAS_PARENT = _tf.mkdtemp(prefix="music_parser_pkg_")
os.symlink(str(_REPO_ROOT), os.path.join(_ALIAS_PARENT, "astrbot_plugin_music_parser"))
sys.path.insert(0, _ALIAS_PARENT)

from astrbot_plugin_music_parser.core.audio_fitter import (  # noqa: E402
    fit_audio_for_record,
    plan_target_bitrate_kbps,
    probe_duration,
)
from astrbot_plugin_music_parser.core.config_manager import ConfigManager  # noqa: E402
from astrbot_plugin_music_parser.core.parsers.base import SongMetadata  # noqa: E402
from astrbot_plugin_music_parser.utils.message_builder import (  # noqa: E402
    _audio_component,
    _make_record,
    build_song_chain,
    split_chain_for_send,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail and not cond else ""))


def make_meta(**kw) -> SongMetadata:
    # SongMetadata.ok 是派生属性（无 error 且有 audio_url），不是构造参数
    base = dict(
        source="netease",
        name="水の星へ愛をこめて",
        artists=["森口博子"],
        album="",
        audio_url="https://example.com/a.flac",
        audio_format="flac",
        pic_url="https://example.com/cover.jpg",
        error=None,
    )
    base.update(kw)
    return SongMetadata(**base)


# ─────────────────────────────────────────────────────────────
def test_config():
    print("\n[1] ConfigManager：output_mode 等字段不再被静默丢弃")
    cfg = ConfigManager({"message": {"output_mode": "video", "video_fps": 7,
                                     "record_max_mb": 6, "record_bitrate_kbps": 96}})
    check("output_mode 被读到", cfg.message.output_mode == "video", cfg.message.output_mode)
    check("video_fps 被读到", cfg.message.video_fps == 7, str(cfg.message.video_fps))
    check("record_max_mb 被读到", cfg.message.record_max_mb == 6, str(cfg.message.record_max_mb))
    check("record_bitrate_kbps 被读到", cfg.message.record_bitrate_kbps == 96)

    cfg2 = ConfigManager({"message": {"output_mode": "胡说八道", "video_fps": "abc",
                                      "record_max_mb": -5}})
    check("非法 output_mode 回退 audio", cfg2.message.output_mode == "audio")
    check("非法 video_fps 回退 2", cfg2.message.video_fps == 2, str(cfg2.message.video_fps))
    check("越界 record_max_mb 夹到 1", cfg2.message.record_max_mb == 1, str(cfg2.message.record_max_mb))

    cfg3 = ConfigManager({})
    check("空配置默认 audio", cfg3.message.output_mode == "audio")
    check("空配置默认 8MB", cfg3.message.record_max_mb == 8)


def test_record_api():
    print("\n[2] Record API 能力探测（AstrBot 4.26.8+ 无 fromFile）")
    check("Record 确实没有 fromFile", not hasattr(Record, "fromFile"))
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.mp3"
        f.write_bytes(b"fake")
        node = _make_record(str(f), local=True)
        check("本地文件走 fromFileSystem", node.file.startswith("file://"), node.file)

    class BareRecord:
        """连 fromFileSystem 都没有的老版本。"""

        def __init__(self, file=None, **kw):
            self.file = file

    import astrbot_plugin_music_parser.utils.message_builder as mb

    real = mb.Record
    try:
        mb.Record = BareRecord
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "y.mp3"
            f.write_bytes(b"fake")
            node = _make_record(str(f), local=True)
            check("老版本回退到构造器", node.file.startswith("file://"), str(node.file))
    finally:
        mb.Record = real


def test_no_audio_as_video():
    print("\n[3] 音频文件绝不被包成 Video 节点（retcode=1400 的根源）")
    meta = make_meta()
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "song.flac"
        audio.write_bytes(b"fLaC" * 1024)
        synth = Path(td) / "song.cover.mp4"
        synth.write_bytes(b"mp4" * 512)

        # as_record=False 且**没有** synth video → 绝不能产出 Video
        node = _audio_component(meta, audio, as_record=False, synth_video_path=None)
        check("无合成视频时不产 Video", not isinstance(node, Video), type(node).__name__)

        # 显式给了 synth video → 才允许 Video
        node = _audio_component(meta, audio, as_record=False, synth_video_path=synth)
        check("有合成视频时用 Video", isinstance(node, Video), type(node).__name__)
        check("Video 指向 .mp4 而非 .flac", str(node.file).endswith(".cover.mp4"), str(node.file))


def test_oversize_no_record():
    print("\n[4] 超大文件不走 Record（base64 WAV 会撑爆 WS 单帧）")
    meta = make_meta()
    with tempfile.TemporaryDirectory() as td:
        big = Path(td) / "big.mp3"
        with big.open("wb") as f:
            f.write(b"\0" * (12 * 1024 * 1024))  # 12MB > 8MB 上限
        node = _audio_component(meta, big, as_record=True)
        check("12MB 文件不是 Record", not isinstance(node, Record), type(node).__name__)
        check("12MB 文件降级为 File", isinstance(node, File), type(node).__name__)

        small = Path(td) / "small.mp3"
        small.write_bytes(b"\0" * (2 * 1024 * 1024))
        node = _audio_component(meta, small, as_record=True)
        check("2MB 文件正常走 Record", isinstance(node, Record), type(node).__name__)


def test_split_chain():
    print("\n[5] Video 拆成独占消息（SnowLuma assertVideoSendPolicy）")
    meta = make_meta()
    with tempfile.TemporaryDirectory() as td:
        synth = Path(td) / "c.mp4"
        synth.write_bytes(b"mp4")

        # audio 模式 + 给了 synth video → build_song_chain 不会用 Video（as_record=True）
        chain = build_song_chain(meta, as_record=True, local_audio_path=None,
                                 synth_video_path=synth, output_mode="audio")
        groups = split_chain_for_send(chain)
        check("audio 模式不被 video 干扰", all(not isinstance(n, Video) for n in chain))

        # video 模式 → 单独一条
        vchain = build_song_chain(meta, as_record=False, local_audio_path=None,
                                  synth_video_path=synth, output_mode="video")
        vgroups = split_chain_for_send(vchain)
        check("video 模式产出 Video", isinstance(vchain[0], Video), type(vchain[0]).__name__)
        check("video 独占一条", len(vgroups) == 1 and len(vgroups[0]) == 1, str(len(vgroups)))

        # 混合链 → Video 被拆出去
        mixed = [Plain("文本"), Image.fromURL("http://x/y.jpg"), Video(file="a.mp4"), Plain("尾巴")]
        mgroups = split_chain_for_send(mixed)
        check("混合链拆成 3 组", len(mgroups) == 3, str([len(g) for g in mgroups]))
        check("中间那组只有 Video", len(mgroups[1]) == 1 and isinstance(mgroups[1][0], Video))
        check("顺序保留", isinstance(mgroups[0][0], Plain) and isinstance(mgroups[2][0], Plain))

        check("空链返回空", split_chain_for_send([]) == [])


def test_transcode():
    print("\n[6] audio_fitter 真实转码（40MB FLAC 的实际场景）")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        flac = td / "song.flac"
        # 造一段 240 秒的立体声无损音频。
        # 用粉红噪声而不是正弦：正弦波在 FLAC 里压得极狠（240s 才 2.8MB），
        # 模拟不出真实音乐那种 ~40MB 的体积。噪声不可压缩，才贴近线上场景。
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "anoisesrc=color=pink:duration=240:amplitude=0.9", "-ac", "2",
             "-ar", "44100", "-c:a", "flac", str(flac)],
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            check("生成测试 FLAC", False, r.stderr.decode()[-200:])
            return
        size_mb = flac.stat().st_size / 1024 / 1024
        dur = probe_duration(flac)
        check(f"测试 FLAC 生成 ({size_mb:.1f}MB, {dur:.0f}s)", dur > 230)
        check(f"测试 FLAC 超过 8MB 阈值（模拟线上 40MB 场景）", size_mb > 8, f"{size_mb:.1f}MB")

        # 8MB 阈值下，这文件（无损，~14MB+）应该被转码
        fitted, note = asyncio.run(fit_audio_for_record(flac, max_mb=8, cap_bitrate_kbps=128))
        check("超阈值触发转码", fitted is not None and fitted != flac, f"{note}")
        if fitted:
            out_mb = fitted.stat().st_size / 1024 / 1024
            check(f"转码后 ≤ 8MB ({out_mb:.2f}MB)", out_mb <= 8.01, f"{out_mb}")
            check("产物是 mp3", fitted.suffix == ".mp3", fitted.suffix)
            check("产物真实可解码",
                  subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", str(fitted)],
                                 capture_output=True).returncode == 0)

        # 二次调用命中缓存（不重跑 ffmpeg）
        mtime = fitted.stat().st_mtime_ns
        fitted2, note2 = asyncio.run(fit_audio_for_record(flac, max_mb=8, cap_bitrate_kbps=128))
        check("二次调用命中转码缓存", fitted2 == fitted and "缓存" in note2, note2)
        check("缓存命中未重写文件", fitted.stat().st_mtime_ns == mtime)

        # 小文件不动
        small = td / "small.flac"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=3",
                        str(small)], capture_output=True, timeout=60)
        s1, n1 = asyncio.run(fit_audio_for_record(small, max_mb=8))
        check("小文件原样返回", s1 == small and n1 == "原样", n1)

        # 转码关闭 → 明确拒绝
        s2, n2 = asyncio.run(fit_audio_for_record(flac, max_mb=1, enabled=False))
        check("关闭转码时拒绝并给原因", s2 is None and "关闭" in n2, n2)


def test_bitrate_plan():
    print("\n[7] 码率规划")
    check("240s/8MB → 128k（取上限）",
          plan_target_bitrate_kbps(240, 8 * 1024 * 1024, 128) == 128)
    check("45min → 48k（取下限）",
          plan_target_bitrate_kbps(2700, 8 * 1024 * 1024, 128) == 48)
    check("时长未知 → 用上限", plan_target_bitrate_kbps(0, 8 * 1024 * 1024, 192) == 192)
    # 目标体积自洽性：按规划码率估出来的体积应当塞得下 8MB
    br = plan_target_bitrate_kbps(600, 8 * 1024 * 1024, 320)
    est_bytes = br * 1000 / 8 * 600
    check(f"600s 规划码率 {br}k 估体积 {est_bytes/1024/1024:.1f}MB ≤ 8MB",
          est_bytes <= 8 * 1024 * 1024)


def test_end_to_end():
    """把 _send_one 整条流水线真跑一遍：fit → build → split → send → cleanup。"""
    print("\n[8] 端到端：_send_one 全流程")
    import astrbot_plugin_music_parser.main as main_mod

    class FakeEvent:
        def __init__(self):
            self.sent = []          # [(kind, chain), ...]

        def message_str(self):
            return ""

        def is_private_chat(self):
            return False

        def get_sender_id(self):
            return "2333"

        def get_group_id(self):
            return "584193570"

        def chain_result(self, chain):
            return ("chain", list(chain))

        def plain_result(self, text):
            return ("plain", text)

        async def send(self, result):
            self.sent.append(result)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 造一个 26MB 的无损源文件（线上 40MB 的等价场景）
        flac = td / "水の星へ愛をこめて-森口博子.flac"
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "anoisesrc=color=pink:duration=240:amplitude=0.9", "-ac", "2",
             "-ar", "44100", "-c:a", "flac", str(flac)],
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            check("生成源文件", False, r.stderr.decode()[-200:])
            return
        check(f"源文件 {flac.stat().st_size // 1024 // 1024}MB 超过阈值",
              flac.stat().st_size > 8 * 1024 * 1024)

        plugin = main_mod.MusicParserPlugin(
            None, {"message": {"output_mode": "audio", "record_max_mb": 8}}
        )
        meta = make_meta(audio_url="https://example.com/a.flac", audio_format="flac")

        async def fake_download(_meta):
            return flac

        async def fake_synth(_meta, _p, _c):
            return None

        plugin._download_audio = fake_download
        plugin._maybe_synth_video = fake_synth

        scheduled = []
        plugin.download_manager.schedule_cleanup = lambda p: scheduled.append(Path(p))

        ev = FakeEvent()
        asyncio.run(plugin._send_one(ev, meta))

        check("发出了消息", len(ev.sent) >= 1, f"{len(ev.sent)}")
        check("只有一条（无 Video，无需拆分）", len(ev.sent) == 1, f"{len(ev.sent)}")

        chain = ev.sent[0][1]
        kinds = [type(n).__name__ for n in chain]
        check(f"消息链构成 {kinds}",
              kinds == ["Plain", "Image", "Record"], str(kinds))

        rec = chain[-1]
        check("语音指向转码后的 mp3", rec.file.endswith(".qq.mp3"), str(rec.file))
        sent_path = Path(rec.path)
        check("转码文件真实存在", sent_path.exists())
        check("转码文件 ≤ 8MB",
              sent_path.stat().st_size <= 8 * 1024 * 1024,
              f"{sent_path.stat().st_size // 1024 // 1024}MB")
        check("**没有把 FLAC 当 Video 发出去**",
              not any(isinstance(n, Video) for n in chain))

        # 清理：源文件 + 转码产物都要被登记
        check("源文件登记清理", flac in scheduled, str([p.name for p in scheduled]))
        check("转码产物登记清理", sent_path in scheduled, str([p.name for p in scheduled]))

        # ── video 模式：验证拆条 ──
        ev2 = FakeEvent()
        synth_mp4 = td / "synth.mp4"
        synth_mp4.write_bytes(b"\0" * (2 * 1024 * 1024))
        plugin2 = main_mod.MusicParserPlugin(
            None, {"message": {"output_mode": "video"}}
        )
        plugin2._download_audio = fake_download
        plugin2._maybe_synth_video = lambda *a, **k: _async_return(synth_mp4)
        plugin2.download_manager.schedule_cleanup = lambda p: scheduled.append(Path(p))
        asyncio.run(plugin2._send_one(ev2, meta))
        check("video 模式发了独立的一条视频消息", len(ev2.sent) == 1, str(len(ev2.sent)))
        check("该条只含 Video",
              len(ev2.sent[0][1]) == 1 and isinstance(ev2.sent[0][1][0], Video),
              str([type(n).__name__ for n in ev2.sent[0][1]]))

        # ── 没装 ffmpeg / 转码关闭 → 必须给直链兜底，不能假装成功 ──
        ev3 = FakeEvent()
        plugin3 = main_mod.MusicParserPlugin(
            None, {"message": {"output_mode": "audio", "record_transcode": False,
                               "record_max_mb": 1}}
        )
        plugin3._download_audio = fake_download
        plugin3._maybe_synth_video = fake_synth
        plugin3.download_manager.schedule_cleanup = lambda p: None
        asyncio.run(plugin3._send_one(ev3, meta))
        kinds3 = [r[0] for r in ev3.sent]
        check("转码关闭时降级为 File 而非 Record",
              any(isinstance(n, File) for n in ev3.sent[0][1]),
              str([type(n).__name__ for n in ev3.sent[0][1]]))


async def _async_return(value):
    return value


def main():
    print("=" * 62)
    print("astrbot_plugin_music_parser v0.3.15 发送链路回归测试")
    print("=" * 62)
    test_config()
    test_record_api()
    test_no_audio_as_video()
    test_oversize_no_record()
    test_split_chain()
    test_bitrate_plan()
    test_transcode()
    test_end_to_end()
    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败：")
        for n in FAIL:
            print(f"  - {n}")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
