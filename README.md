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

| 值 | 发什么 |
|----|--------|
| `video`（默认） | 合成视频气泡，封面+音频合一 |
| `audio` | 文本+封面+音频文件 |
| `link` | 文本+封面+直链，不下载音频 |

视频气泡的封面比例自适应，帧数和视频上限可在 `video_fps`、`video_max_width`、`video_max_height` 调。

## 故障排查

### 大文件不发送

QQ 平台对音频文件大小有限制。`message.output_mode` 切到 `link` 看直链，或用 `audio` 模式发小一点的音质。

### 日志里没 `[music_parser]` 字样

权限被拒或者插件没加载。检查 AstrBot 控制台是否有 `astrbot_plugin_music_parser` 启动日志。

### 发链接不识别

WebUI → 配置 → `debug.verbose_log` 设为 `true` → 再发一次链接 → 把 `[on_message] [extract_*] [parse] [send_one]` 节点的日志复制出来排查。

### 只解析到 128k / 320k

Cookie 不是会员账号。网易云换黑胶会员重新拿 Cookie，QQ 音乐换绿钻。

## 架构

```
AstrBot 进程
├── main.py                  消息入口和命令
├── core/
│   ├── link_extractor.py    文本/卡片 URL 提取 + 音乐域名白名单
│   ├── parser_manager.py    平台分发
│   ├── downloader.py        音频下载 + 缓存清理
│   ├── video_synthesizer.py 封面+音频合成 mp4
│   └── parsers/
│       ├── netease.py       sys.path 注入 → import music_api
│       └── tencent.py       sys.path 注入 → from app import QQMusic
├── utils/
│   └── message_builder.py   SongMetadata → AstrBot 消息链
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
| message | output_mode | video | link / audio / video |
| message | show_cover | true | 发封面 |
| message | show_lyric | false | 发歌词 |
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