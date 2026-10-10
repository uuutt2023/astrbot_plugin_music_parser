# AstrBot 音乐解析插件

解析网易云音乐和 QQ 音乐分享链接，转成可播放的音频发给 QQ 群/私聊。零子进程，Cookie 配好就能用。

## 安装

```bash
# 1. 放插件
cp -r astrbot_plugin_music_parser <AstrBot 数据目录>/plugins/

# 2. 装依赖
pip install aiohttp requests cryptography

# 3. 配 Cookie（不填也能用，但只能解析标准音质）
```

### 配 Cookie

WebUI → 音乐解析插件 → 配置：

| 字段 | 怎么拿 |
|------|--------|
| `cookies.netease_cookie` | 登录 music.163.com → F12 → Network → 任意请求 → 复制 Cookie |
| `cookies.tencent_cookie` | 登录 y.qq.com → F12 → Network → 任意请求 → 复制 Cookie |

填好保存即生效，不需重启。

## 用法

直接发网易云或 QQ 音乐分享链接/卡片，机器人自动解析并发音频。

### 命令

| 命令 | 别名 | 作用 |
|------|------|------|
| `网易云 <链接或ID>` | `/网易云` | 强制网易云解析 |
| `QQ音乐 <链接>` | `/QQ音乐` | 强制 QQ 音乐解析 |
| `搜云 <关键词>` | `/搜云` | 网易云搜索前 10 条 |
| `搜QQ <关键词>` | `/搜QQ` | 提示（QQ 后端无搜索） |
| `音乐解析状态` | `/音乐解析状态` | 查看配置和后端健康 |
| `音乐解析帮助` | `/音乐解析帮助` | 完整帮助 |
| `清理音乐缓存` | `/清理音乐缓存` | 管理员私聊清缓存 |

## 三种输出方式

`message.output_mode` 字段选一种：

| 值 | 发什么 | 稳定性 |
|----|--------|--------|
| `audio`（默认） | 文本+封面+音频文件，超大文件自动压成 mp3 | ★★★ 稳，QQ 兼容最好 |
| `video` | 合成视频气泡，封面+音频合一，**独占一条消息** | ★★ 体验最好，需要 ffmpeg |
| `link` | 文本+封面+直链，不下载音频 | ★★★ 最稳，但用户需自己点链接收听 |

视频气泡的封面比例自适应，帧数和视频上限可在 `video_fps`、`video_max_width`、`video_max_height` 调。

> `video` 模式下视频会作为**独立的一条消息**发出 —— 协议端（SnowLuma 的
> `assertVideoSendPolicy`）明确要求 video 必须是消息里唯一的段，和文本封面混发
> 会直接被拒（`retcode=1400`）。

### 大文件为什么发不出去（v0.3.15 起自动处理）

AstrBot 的 aiocqhttp 适配器对语音（`Record`）组件是**无条件 base64** 的：

```python
if isinstance(segment, Image | Record):
    bs64 = await segment.convert_to_base64()
```

而 `Record.convert_to_base64()` 内部走 `to_base64(target_format="wav")` ——
**它会先用 ffmpeg 把音频解码成 PCM WAV，再 base64**。于是：

```
40MB FLAC（约 4 分钟）→ 42MB WAV → 56MB base64 字符串 → 一个 WebSocket JSON 帧
```

这个膨胀发生在 **AstrBot 内部**，还没走到协议端那一步 —— 换 NapCat、换
SnowLuma 都一样会失败。所谓「QQ 平台限制」其实是这条链路的副作用。

v0.3.15 起，插件在交给 AstrBot **之前**先用 ffmpeg 把超大音频压成 mp3：

```
[audio_fit] 源 song.flac 26MB (时长 240s, 转 WAV 约 40MB → base64 约 54MB)，
            超过 8MB → 转 mp3 @128kbps
[audio_fit] 转码完成: song.flac 26MB → song.qq.mp3 3MB @128kbps
```

| 字段 | 默认 | 说明 |
|------|------|------|
| `message.record_max_mb` | 8 | 语音发送体积上限，超过就转码 |
| `message.record_bitrate_kbps` | 128 | 转码目标码率上限（实际会按时长自动下调） |
| `message.record_transcode` | true | 关掉就只做体积判断，超大文件直接给直链 |
| `message.send_timeout` | 60 | 单次发送超时（秒），网络慢可以调大 |

没装 ffmpeg 时，`record_max_mb` 以上会降级为群文件附件或直链。

## 性能

对 async 机器人插件来说，最要命的性能问题不是"慢"，而是**阻塞 event loop**——
一旦某段同步代码卡住主循环，整个 bot 在那段时间内对**所有**消息都无响应，
而不只是当前这一首歌。

### 实测数据

`tests/profile_pipeline.py`，240s / 26.9MB 无损 + 真实封面图，`_send_one` 全流程：

| 场景 | v0.3.15 | v0.3.16 | 变化 |
|------|--------|--------|------|
| `audio`（默认）总耗时 | 26.70s | **3.39s** | −87% |
| `audio` event loop 最长卡顿 | 0.181s | **0.067s** | −63% |
| `video` 总耗时 | 0.07s | 0.07s | — |
| `link` 总耗时 | 0.07s | 0.07s | — |

逐函数卡顿（`tests/profile_loop_blocking.py`）：

| 调用 | 耗时 | event loop 卡顿 |
|------|------|-----------------|
| `probe_duration`（同步，故意） | 0.12s | 0.116s |
| `probe_duration_async`（executor） | 0.10s | **0.001s** |
| `synthesize_cover_audio_video` | 3.49s | 0.040s |
| `fit_audio_for_record`（26MB → mp3） | 4.10s | 0.018s |

### v0.3.16 修了什么

**① audio 模式白合成封面视频（最大的一处）**

`_maybe_synth_video` 以前只要 `output_mode != 'link'` 就合成，于是**默认的 audio
模式也在跑 ffmpeg**。但 audio 模式下 `send_as_record=True`，而 `_audio_component`
的 Video 分支要求 `not as_record` —— 合成出来的 mp4 从头到尾没人用。

实测：合成耗时 **23.20s**、产出 6MB mp4，最终音频组件用的却是 Record。
每首歌白等二十多秒。现在只有 `output_mode == 'video'` 才合成。

**② 子进程调用挪出 event loop**

`_extract_embedded_cover` 和 `probe_duration` 都是 `subprocess.run` 同步调用，
直接卡住 event loop。现在都通过 `run_in_executor` 丢进线程池。
（`_run_ffmpeg_synth` 原本就已经在 executor 里。）

**③ 封面下载复用 HTTP 连接**

`_download_cover` 以前每次都 `async with aiohttp.ClientSession()`，等于每首歌
重做一次 TCP + TLS 握手、用完立刻关掉。现在进程内共享一个 session，并绑定到
创建它的 event loop（换 loop 时自动重建，避免 aiohttp 报错）。插件 `terminate()`
时释放。

**④ 顺带修了个静默 bug**

`_probe_cover_size` 写死调用 `"ffprobe"` 走 PATH 查找，但 `find_ffmpeg()` 可能返回
imageio-ffmpeg 自带的 ffmpeg——**那个包里没有 ffprobe**。于是每次都白 spawn 一次
进程再吃 `FileNotFoundError`，静默退回 1920x1080。现在从 ffmpeg 实际路径推导
ffprobe，与 `audio_fitter.probe_duration` 保持一致。

### 怎么复现这些数字

```bash
python3 tests/profile_loop_blocking.py   # 逐函数卡顿
python3 tests/profile_pipeline.py        # 端到端 A/B
```

两个脚本都需要系统装有 ffmpeg。

> ⚠️ **关于卡顿探针**：`tests/_fake_env.py` 里的 `LagMonitor` 在测量前会先自检
> （已知阻塞必须被测出、纯异步等待必须不算卡顿），自检不过直接拒绝输出结论。
> 这一点是被现实教育过的——最初版本的 `__aexit__` 在 monitor 记录到阻塞之前就
> `cancel()` 了任务，导致**所有卡顿数字都报成 0.000s**。测性能的工具本身没被
> 验证过之前，它的输出没有意义。

## 故障排查

### 大文件不发送

先确认日志里有没有 `[audio_fit]` 段：

- 有 `[audio_fit] 转码完成` 但仍失败 → 看紧跟其后的 `[send_one] 发送失败`，可能是网络或协议端问题
- 有 `[audio_fit] 转码失败` → 没装 ffmpeg，或 `record_transcode` 被关了。装 ffmpeg，或把 `record_max_mb` 调小，或改用 `link` 模式

### `retcode=1400 ... "video" must be the only segment in a message`

协议端拒绝把视频和别的段放同一条消息。v0.3.15 已用 `split_chain_for_send()`
自动拆开发送，日志里会出现：

```
[send_one] 消息链含 Video，拆成 N 条分别发送
```

如果还报这个错，说明跑的还是旧版本插件，检查一下 WebUI 里插件版本是不是 0.3.15。

### `AttributeError: type object 'Record' has no attribute 'fromFile'`

AstrBot 4.26.8 移除了 `Record.fromFile`。v0.3.15 已改成能力探测
（`fromFileSystem` → 构造器 → `fromURL`），看到这条就是旧版本插件。

### 视频模式 `retcode=100, fetch failed`

video 模式上传本地路径时协议端 fetch 失败。v0.3.15 已改用裸绝对路径
（不再拼 `file://` URI），并在发送失败时自动降级到 `audio` 模式重发。
仍然失败就切到 `audio` / `link` 模式。

### 日志里没 `[music_parser]` 字样

权限被拒或者插件没加载。检查 AstrBot 控制台是否有 `astrbot_plugin_music_parser` 启动日志。

### 发链接不识别

WebUI → 配置 → `debug.verbose_log` 设为 `true` → 再发一次链接 → 把 `[on_message] [extract_*] [parse] [send_one]` 节点的日志复制出来排查。

### 只解析到 128k / 320k

Cookie 不是会员账号。网易云换黑胶会员重新拿 Cookie，QQ 音乐换绿钻。

## 测试

```bash
python3 tests/test_send_pipeline.py       # 52 项断言：发送链路
python3 tests/profile_loop_blocking.py    # 逐函数 event loop 卡顿
python3 tests/profile_pipeline.py         # 端到端 A/B 性能
```

用假的 astrbot 组件 + 真实 ffmpeg 跑完整发送链路，覆盖配置读取、Record API
能力探测、Video 拆分、超大文件转码等场景（需要系统装有 ffmpeg）。详见[性能](#性能)一节。

## 架构

```
AstrBot 进程
├── main.py                  消息入口和命令
├── core/
│   ├── link_extractor.py    文本/卡片 URL 提取 + 音乐域名白名单
│   ├── parser_manager.py    平台分发
│   ├── downloader.py        音频下载 + 缓存清理
│   ├── audio_fitter.py      超大音频转 mp3（Record 体积治理）
│   ├── video_synthesizer.py 封面+音频合成 mp4
│   └── parsers/
│       ├── netease.py       sys.path 注入 → import music_api
│       └── tencent.py       sys.path 注入 → from app import QQMusic
├── utils/
│   └── message_builder.py   SongMetadata → AstrBot 消息链（含 Video 拆条）
├── tests/
│   └── test_send_pipeline.py 发送链路回归测试
└── vendor/                  内置的开源后端源码，不修改
    ├── netease_url/         Suxiaoqinx/Netease_url
    └── tencent_url/         Suxiaoqinx/tencent_url
```

`vendor/` 里的代码原样保留，插件只通过 `sys.path` 注入调用，不修改任何文件。

## 配置项

WebUI 可视化配置，schema 在 `_conf_schema.json`。常用字段：

| 分组 | 字段 | 默认 | 说明 |
|------|------|------|------|
| cookies | netease_cookie | 空 | 网易云 Cookie |
| cookies | tencent_cookie | 空 | QQ 音乐 Cookie |
| parsers | netease | 全部发送 | 关闭 / 全部发送 / 仅文本 / 仅音频 |
| parsers | tencent | 全部发送 | 关闭 / 全部发送 / 仅文本 / 仅音频 |
| quality | netease_level | lossless | 网易云音质档 |
| quality | tencent_level | flac | QQ 音质档 |
| trigger | auto_parse | true | 链接自动解析 |
| trigger | keywords | 解析音乐等 | 手动触发关键词 |
| message | output_mode | audio | link / audio / video |
| message | show_cover | true | 发封面 |
| message | show_lyric | false | 发歌词 |
| message | record_max_mb | 8 | 语音体积上限，超过自动转 mp3 |
| message | record_bitrate_kbps | 128 | 转码目标码率上限 |
| message | record_transcode | true | 是否自动转码 |
| message | send_timeout | 60 | 单次发送超时（秒） |
| message | video_fps | 2 | 视频帧数 |
| message | video_max_width | 1920 | 视频最大宽度 |
| message | video_max_height | 1080 | 视频最大高度 |
| permission | enabled_groups | 空 | 白名单群号 |
| permission | admin_id | 空 | 管理员 ID |
| cache | enable_cache | true | 本地缓存 |
| debug | verbose_log | false | 详细日志 |

## 致谢

解析能力来自这些开源项目：

- [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url) — 网易云无损解析
- [Suxiaoqinx/tencent_url](https://github.com/Suxiaoqinx/tencent_url) — QQ 音乐无损解析
- [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser) — 架构参考

## 协议

本仓库新增代码 MIT。`vendor/` 子目录代码归原作者所有，遵循各自项目的许可证。请勿用于商业转售或大规模爬取，遵守各平台服务条款。