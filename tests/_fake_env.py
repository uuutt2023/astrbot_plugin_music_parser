"""测试/剖析用的假环境：假的 astrbot 组件 + 假 aiohttp + 包别名软链。

被 tests/test_send_pipeline.py 和 tests/profile_pipeline.py 共用。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 假的 message_components ──
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


class FakeStar:
    def __init__(self, context=None, *a, **k):
        self.context = context


def install() -> None:
    """把假模块装进 sys.modules，并把仓库以插件包名挂到 sys.path 上。"""
    if getattr(install, "_done", False):
        return

    components = types.ModuleType("astrbot.api.message_components")
    for cls in (Plain, Image, Record, File, Video):
        setattr(components, cls.__name__, cls)

    api = types.ModuleType("astrbot.api")
    api.message_components = components

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    star_mod.Star = FakeStar
    star_mod.Context = object

    evt_mod = types.ModuleType("astrbot.core.star.filter.event_message_type")
    evt_mod.EventMessageType = type("EventMessageType", (), {"ALL": "all"})
    filter_pkg = types.ModuleType("astrbot.core.star.filter")
    filter_pkg.event_message_type = evt_mod
    star_pkg = types.ModuleType("astrbot.core.star")
    star_pkg.filter = filter_pkg

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = types.SimpleNamespace(
        event_message_type=lambda *a, **k: (lambda fn: fn),
        command=lambda *a, **k: (lambda fn: fn),
    )
    event_mod.AstrMessageEvent = object

    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, **kw):
            self.kw = kw

    class ClientSession:
        def __init__(self, *a, **kw):
            self.closed = False

        async def close(self):
            self.closed = True

    aiohttp_stub.ClientTimeout = ClientTimeout
    aiohttp_stub.ClientSession = ClientSession
    aiohttp_stub.ClientError = type("ClientError", (Exception,), {})

    for name, mod in {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.api.message_components": components,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.star": star_pkg,
        "astrbot.core.star.filter": filter_pkg,
        "astrbot.core.star.filter.event_message_type": evt_mod,
        "aiohttp": aiohttp_stub,
    }.items():
        sys.modules[name] = mod

    # 插件内部用相对导入（from ..core... import），必须以包的形式导入
    alias_parent = tempfile.mkdtemp(prefix="music_parser_pkg_")
    os.symlink(str(REPO_ROOT), os.path.join(alias_parent, "astrbot_plugin_music_parser"))
    sys.path.insert(0, alias_parent)

    install._done = True


class FakeEvent:
    """记录所有发出的消息，用来断言发送结果。"""

    def __init__(self):
        self.sent = []

    def message_str(self):
        return ""

    def is_private_chat(self):
        return False

    def get_sender_id(self):
        return "2333"

    def get_group_id(self):
        return "584193570"

    def get_messages(self):
        return []

    def chain_result(self, chain):
        return ("chain", list(chain))

    def plain_result(self, text):
        return ("plain", text)

    async def send(self, result):
        self.sent.append(result)


def make_meta(**kw):
    from astrbot_plugin_music_parser.core.parsers.base import SongMetadata

    base = dict(
        source="netease",
        name="水の星へ愛をこめて",
        artists=["森口博子"],
        album="",
        audio_url="https://example.com/a.flac",
        audio_format="flac",
        pic_url="",
        error=None,
    )
    base.update(kw)
    return SongMetadata(**base)


class LagMonitor:
    """测 event loop 最长卡顿：相邻 tick 的最大间隔。"""

    def __init__(self, interval: float = 0.005):
        self.interval = interval
        self.max_lag = 0.0
        self.samples = 0
        self._task = None
        self._stop = False

    async def _loop(self):
        last = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(self.interval)
            now = time.perf_counter()
            self.max_lag = max(self.max_lag, now - last - self.interval)
            self.samples += 1
            last = now

    async def __aenter__(self):
        self._stop = False
        self.max_lag = 0.0
        self._task = asyncio.create_task(self._loop())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        self._stop = True
        # 关键：必须先让出控制权，等 monitor 把"被阻塞期间"累积的那次 lag 记下来，
        # 再 cancel。否则 __aexit__（无 await 点的协程）会同步执行到 cancel，
        # 待触发的 timer 回调永远等不到运行 → 报出 0.000s 的假数据。
        try:
            await asyncio.sleep(self.interval * 4)
        except asyncio.CancelledError:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return False


import asyncio  # noqa: E402
import time  # noqa: E402
