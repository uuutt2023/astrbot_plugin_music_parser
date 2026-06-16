# 🎵 AstrBot 音乐解析插件（零子进程版）

> **v0.3.0 重大重构**：内置两个开源音乐解析后端源码到 `vendor/` 子目录，**直接 import 在 AstrBot 进程内调用，零子进程、零端口、零 Flask、零 health check**。填个 Cookie 就能用。

---

## ✨ 特性

- 📦 **真正开箱即用**：把 `astrbot_plugin_music_parser/` 整个目录扔进 plugins/，装 3 个 pip 依赖，重启 AstrBot 就完事
- 🔗 **自动识别**：消息中含网易云 / QQ 音乐链接即自动解析（多条链接并发处理）
- 🎼 **音质可选**：网易云 7 档（standard → jymaster），QQ 音乐 7 档（128 → atmos_51）
- 🖼 **三段式输出**：文本元数据 + 专辑封面 + 音频文件
- 💾 **本地缓存**：默认先下载到本地再发送，绕开 QQ / 微信对直链的风控
- 🔍 **网易云搜索**：`搜云 <关键词>`
- 🩺 **零依赖故障点**：没有子进程 → 没有进程挂掉 / 端口被占 / Flask 启动失败这些幺蛾子

---

## 🧱 架构

```
┌──────────────────────────────────────────────────────┐
│ AstrBot 插件（本仓库，单 Python 进程）                │
│                                                      │
│  main.py                                             │
│    └─ ParserManager                                  │
│         │                                            │
│         │ import (进程内直接调用，零 IPC)             │
│         ▼                                            │
│  ┌────────────────────────────────────────────┐      │
│  │ core/parsers/netease.py                    │      │
│  │   → sys.path 注入 vendor/netease_url       │      │
│  │   → import music_api                       │      │
│  │   → url_v1 / name_v1 / lyric_v1 函数调用   │      │
│  │   → 用 asyncio.to_thread 包住 requests     │      │
│  └────────────────────────────────────────────┘      │
│  ┌────────────────────────────────────────────┐      │
│  │ core/parsers/tencent.py                    │      │
│  │   → sys.path 注入 vendor/tencent_url       │      │
│  │   → from app import QQMusic                │      │
│  │   → 调 get_music_song / get_music_url 等   │      │
│  └────────────────────────────────────────────┘      │
└──────────────────────────────────────────────────────┘
```

**对比之前的"子进程方案"**：

| 维度 | 子进程方案 (v0.2.0) | 零子进程 (v0.3.0，本版) |
|------|----------------------|--------------------------|
| 启动成本 | 拉两个 Python 进程 | 0 |
| 失败模式 | 子进程挂掉 / 端口被占 / Flask 启动失败 | 无 |
| 资源占用 | 3 个 Python 解释器 | 1 个 |
| 调试难度 | 日志分散在子进程 | 全部在主进程 |
| 性能 | HTTP 调 localhost | 直接函数调用 |
| 失败时的可见性 | 经常 `进程已退出，请看日志` | 异常直接抛到主日志 |

---

## 🚀 安装

### 1. 放插件

```bash
# 把整个 astrbot_plugin_music_parser/ 目录放到 AstrBot 插件目录
cp -r astrbot_plugin_music_parser <AstrBot 数据目录>/plugins/
```

### 2. 装依赖

```bash
pip install aiohttp requests cryptography
```

或在 AstrBot WebUI → 控制台 → 安装 Pip 库：
- aiohttp
- requests
- cryptography

### 3. 填 Cookie

AstrBot WebUI → 插件 → `astrbot_plugin_music_parser` → 配置：

| 字段 | 怎么填 |
|------|--------|
| `cookies.netease_cookie` | 登录 https://music.163.com → F12 → Network → 任意请求 → 复制 `Cookie` 字段 |
| `cookies.tencent_cookie` | 登录 https://y.qq.com → F12 → Network → 任意请求 → 复制 `Cookie` 字段 |

保存即生效，**不需要重启 AstrBot**。

### 4. 用

```text
https://music.163.com/song?id=185668
https://y.qq.com/n/ryqq/songDetail/004IAMhn0UBfzR
```

---

## ⚙️ 配置项

| 分组 | 字段 | 默认 | 说明 |
|------|------|------|------|
| `cookies` | `netease_cookie` | `""` | 网易云黑胶 Cookie |
| `cookies` | `tencent_cookie` | `""` | QQ 音乐 Cookie |
| `parsers` | `netease` | `全部发送` | `关闭 / 全部发送 / 仅文本 / 仅音频` |
| `parsers` | `tencent` | `全部发送` | `关闭 / 全部发送 / 仅文本 / 仅音频` |
| `quality` | `netease_level` | `lossless` | 网易云音质档 |
| `quality` | `tencent_level` | `flac` | QQ 音乐音质档 |
| `trigger` | `auto_parse` | `true` | 消息含链接自动解析 |
| `trigger` | `reply_trigger` | `true` | 引用消息 + 关键词触发 |
| `trigger` | `keywords` | `["解析音乐", …]` | 手动触发关键词 |
| `message` | `opening_enabled` | `true` | 发送开场语 |
| `message` | `send_as_record` | `true` | 音频用语音组件 |
| `message` | `show_cover` | `true` | 附带专辑封面 |
| `message` | `show_lyric` | `false` | 附带歌词 |
| `permission` | `private_enabled` | `true` | 允许私聊 |
| `permission` | `group_enabled` | `true` | 允许群聊 |
| `permission` | `enabled_groups` | `[]` | 白名单群号 |
| `permission` | `admin_id` | `""` | 管理员 ID |
| `cache` | `enable_cache` | `true` | 启用本地缓存 |
| `cache` | `cleanup_after` | `600` | 发送后 N 秒清理 |
| `debug` | `verbose_log` | `false` | 详细日志 |

---

## 📖 命令列表

| 命令 | 行为 | 权限 |
|------|------|------|
| 直接发链接 | 自动识别 + 解析 | 所有人 |
| `网易云 <链接>` / `QQ音乐 <链接>` | 强制指定平台 | 所有人 |
| `搜云 <关键词>` | 网易云搜索 | 所有人 |
| `搜QQ <关键词>` | 提示（QQ 后端无搜索） | 所有人 |
| `音乐解析状态` | 配置 + Cookie 状态 | 所有人 |
| `音乐解析帮助` | 输出帮助 | 所有人 |
| `清理音乐缓存` | 删除本地缓存 | 仅管理员私聊 |

---

## ❓ 常见问题

### 1. 网易云只解析到 128k / 极高

`cookies.netease_cookie` 不是黑胶会员 Cookie。重新登录 https://music.163.com 用黑胶账号拿 Cookie。

### 2. QQ 音乐只拿到 128k / 320

`cookies.tencent_cookie` 不是会员 Cookie，或 Cookie 过期。

### 3. `ImportError: No module named 'music_api'`

vendor 目录被破坏。重新解压 zip 即可。

### 4. `cryptography` 缺失

```bash
pip install cryptography
```

### 5. 网易云扫码登录

vendor/netease_url 自带 `/qrlogin` 端点，但 v0.3.x 走的是 Cookie 模式，未启用此端点。如需扫码登录，把 vendor 的 `qr_login.py` 暴露成插件命令即可（自行实现）。

### 6. 之前报过 `'type'` 加载错误

v0.2.0 已修复。后续版本重新整理了 schema，确保所有容器节点都有 `type: object`。

### 7. 发链接不识别 / 卡片没反应 → 开详细日志

在 WebUI → 音乐解析插件 → 配置 → 把 **`debug.verbose_log`** 设为 `true`，保存。然后再发一次链接，**把 AstrBot 控制台里所有 `[music_parser]` 开头的日志复制出来**，特别留意这些节点：

| 日志节点 | 含义 |
|---------|------|
| `[on_message] in sender=... text=...` | 消息是否进了插件 |
| `[check_permission] → OK / DENY` | 权限是否放行 |
| `[extract_links_from_event] ── 1) 文本链接 ──` | 文本里的 URL 提取 |
| `[extract_links_from_event] ── 2) 卡片链接 ──` | QQ 卡片里的 URL 提取 |
| `[extract_card_urls_from_event] event 共有 N 个 component` | **关键**：N=0 说明 AstrBot 没给你 component |
| `[extract_url_from_card_data] 抽出音乐 URL: ...` | 成功抽出音乐卡片 URL |
| `[extract_url_from_card_data] 非音乐小程序域名，静默过滤: ...` | 抽到了非音乐卡片 → 已过滤 |
| `[parse] 开始解析 [netease] ...` | 进入解析 |
| `[parse] 解析成功 / 失败: <原因>` | 结果 |
| `[send_one] 音频已下载到本地` | 音频缓存成功 |
| `[send_one] 发送失败: ...` | 真实失败原因（堆栈也会打） |

### 8. 发 B 站 / 抖音 / 淘宝 / 知乎 等其他小程序会被插件处理吗？

**不会**。v0.3.4 起，本插件**只处理网易云 / QQ 音乐小程序**：

- 在 `extract_url_from_card_data` 里加了**音乐域名白名单**早期过滤（`music.163.com` / `163cn.tv` / `y.qq.com` / `c6.y.qq.com` 等）
- B 站、抖音、淘宝、知乎、公众号等任何**非音乐**卡片，**根本不会**抽出 URL，更不会触发解析
- verbose 模式下会打 `[extract_url_from_card_data] 非音乐小程序域名，静默过滤: ...` 日志

如果以后插件要支持更多音乐平台（汽水音乐 / 咪咕 / Apple Music 等），往 `_MUSIC_DOMAINS` 元组里加域名即可。

---

## 🗂 项目结构

```
astrbot_plugin_music_parser/
├── metadata.yaml                   # 插件元信息
├── main.py                         # 入口（事件 / 命令 / 配置注入）
├── _conf_schema.json               # WebUI schema
├── requirements.txt                # 依赖：aiohttp / requests / cryptography
├── README.md                       # 你正在看的
├── core/
│   ├── constants.py
│   ├── logger.py
│   ├── config_manager.py           # schema → dataclass
│   ├── link_extractor.py           # 网易云 / QQ 音乐 URL 正则
│   ├── parser_manager.py           # 平台分发（进程内调用）
│   ├── downloader.py               # 音频本地缓存 + 延迟清理
│   └── parsers/
│       ├── base.py
│       ├── netease.py              # → 直接 import vendor/netease_url/music_api
│       └── tencent.py              # → 直接 import vendor/tencent_url/app
├── utils/
│   └── message_builder.py          # SongMetadata → AstrBot 消息链
└── vendor/                         # ⭐ 内置的开源后端源码（不修改）
    ├── netease_url/                # 来自 Suxiaoqinx/Netease_url
    │   ├── main.py                 # 原样（未使用）
    │   ├── music_api.py            # 原样 ← 插件实际 import 这个
    │   ├── cookie_manager.py       # 原样
    │   ├── music_downloader.py     # 原样
    │   ├── qr_login.py             # 原样
    │   ├── requirements.txt        # 原样
    │   └── cookie.txt.example      # 原样
    └── tencent_url/                # 来自 Suxiaoqinx/tencent_url
        ├── app.py                  # 原样 ← 插件实际 import 这个
        └── requirements.txt        # 原样
```

> **重要**：`vendor/` 里的代码**完全保持原项目原样**，插件不修改它们的任何文件。

---

## 🙏 鸣谢

本插件的核心解析能力完全来自以下开源项目，特此致谢：

### 内置的开源后端（无它们就没有这个插件）

- 🎵 **[Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)** — 网易云音乐无损解析 API
  - 本插件 `vendor/netease_url/` 完整嵌入该项目全部 Python 源码
  - 插件通过 `sys.path` 注入 → `import music_api` → 直接调用 `url_v1 / name_v1 / lyric_v1 / search_music / playlist_detail / album_detail`
  - 同步 `requests` 调用由 `asyncio.to_thread` 包住，不阻塞 AstrBot 事件循环
  - 原项目许可证：详见上游仓库

- 🎶 **[Suxiaoqinx/tencent_url](https://github.com/Suxiaoqinx/tencent_url)** — QQ 音乐无损解析 API
  - 本插件 `vendor/tencent_url/` 完整嵌入该项目 `app.py`
  - 插件通过 `sys.path` 注入 → `from app import QQMusic` → 实例化后调 `get_music_song / get_music_url / get_music_lyric_new`
  - 同步 `requests` 调用同样用 `asyncio.to_thread` 包住
  - 原项目许可证：详见上游仓库

### 架构参考

- 🧩 **[drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)** — 流媒体聚合解析器（AstrBot 插件的模块化范本）
  - 参考了其 `core/parsers/` 拆分模式、`aiohttp` 客户端封装、消息组装思路
  - 本插件的 `core/` 目录结构受其启发

### 间接依赖

- [Ravizhan](https://github.com/ravizhan) — `Suxiaoqinx/Netease_url` 项目致谢
- AstrBot 框架及作者 — 提供插件运行时

### 致谢方式

> 如果本插件对你有用，记得给上面这些仓库点个 Star ⭐。
> 本插件作者只是把这些好用的工具拼装成了一个 AstrBot 插件，真正的功劳属于上面这些项目的作者。

---

## 📄 协议

本仓库新增代码采用 **MIT License**。

`vendor/` 子目录中的源码分别属于其各自的原作者所有，许可证以原项目为准：

- `vendor/netease_url/` — 来自 [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)，遵循其原始许可证
- `vendor/tencent_url/` — 来自 [Suxiaoqinx/tencent_url](https://github.com/Suxiaoqinx/tencent_url)，遵循其原始许可证

> ⚠️ **请勿将本插件用于商业转售或大规模爬取，遵守各平台的服务条款。**