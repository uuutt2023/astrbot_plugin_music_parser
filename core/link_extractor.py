"""链接识别：网易云音乐 / QQ 音乐 + QQ 卡片 URL 提取。

支持的卡片结构：
1. 标准小程序 (com.tencent.miniapp)        → meta.detail_1.qqdocurl
2. 公众号 / 新闻卡片                       → meta.news.jumpUrl
3. 音乐小程序 (com.tencent.music.*)         → meta.music.jumpUrl / meta.music.musicUrl
   - 网易云用的是 com.tencent.music.lua
   - QQ 音乐用的是 com.tencent.music.mini 或 com.tencent.miniapp+view=music
4. app 内置分享 (分享小程序到 QQ)            → meta.detail_1.qqdocurl 也可能

抽出后做白名单过滤（_MUSIC_DOMAINS），非音乐卡片直接静默丢弃。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

from .constants import PLATFORM_NETEASE, PLATFORM_TENCENT
from .logger import d

# ──────────── 网易云 ────────────
NETEASE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"https?://music\.163\.com/song\?id=(\d+)"),
    re.compile(r"https?://music\.163\.com/#/song\?id=(\d+)"),
    re.compile(r"https?://music\.163\.com/playlist\?id=(\d+)"),
    re.compile(r"https?://music\.163\.com/album\?id=(\d+)"),
    re.compile(r"https?://music\.163\.com/program\?id=(\d+)"),
    re.compile(r"https?://y\.music\.163\.com/m/song\?id=(\d+)"),
    re.compile(r"https?://163cn\.tv/[A-Za-z0-9]+"),
    # 注意：v0.3.12 移除纯数字正则 (?<![A-Za-z0-9])(\d{6,20})(?![A-Za-z0-9])
    # 原因：会把 Pixiv / B站 / 其他 URL 中的 ID 误识别为网易云歌曲 ID
    # 用户需要纯数字网易云 ID 时，请用命令「网易云 <ID>」
)

# ──────────── QQ 音乐 ────────────
TENCENT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"https?://y\.qq\.com/n/ryqq/songDetail/([A-Za-z0-9]+)"),
    re.compile(r"https?://y\.qq\.com/n/ryqq/songDetail/[A-Za-z0-9/\?=&_\-.]+"),
    re.compile(r"https?://c6\.y\.qq\.com/sharp/songDetail\?.*?songmid=([A-Za-z0-9]+)"),
    re.compile(r"https?://c6\.y\.qq\.com/[A-Za-z0-9/?&=%_\-.]+"),
    re.compile(r"https?://i\.y\.qq\.com/v8/playsong\.html\?.*?songmid=([A-Za-z0-9]+)"),
    re.compile(r"https?://i\.y\.qq\.com/[A-Za-z0-9/?&=%_\-.]+"),
)

LINK_REGEX = re.compile(
    r"https?://[^\s\u4e00-\u9fff<>\"']+",
    re.IGNORECASE,
)

# 音乐小程序域白名单
_MUSIC_DOMAINS = (
    "music.163.com",      # 网易云主域
    "163cn.tv",           # 网易云短链
    "y.music.163.com",    # 网易云移动版（QQ 分享常用此域）
    "y.qq.com",           # QQ 音乐
    "c6.y.qq.com",        # QQ 音乐短链
)


def _is_music_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(domain in u for domain in _MUSIC_DOMAINS)


@dataclass
class ExtractedLink:
    platform: str
    raw: str
    identifier: str


# ──────────── 文本链接识别 ────────────

def _match_first(text: str, patterns: tuple[re.Pattern, ...]) -> Optional[re.Match]:
    for p in patterns:
        m = p.search(text)
        if m:
            return m
    return None


def _extract_netease(text: str) -> Optional[ExtractedLink]:
    m = _match_first(text, NETEASE_PATTERNS)
    if not m:
        return None
    groups = m.groups()
    identifier = groups[0] if groups else m.group(0)
    d(f"[_extract_netease] matched: identifier={identifier!r}")
    return ExtractedLink(
        platform=PLATFORM_NETEASE,
        raw=m.group(0),
        identifier=identifier,
    )


def _extract_tencent(text: str) -> Optional[ExtractedLink]:
    m = _match_first(text, TENCENT_PATTERNS)
    if not m:
        return None
    groups = m.groups()
    identifier = groups[0] if groups else m.group(0)
    d(f"[_extract_tencent] matched: identifier={identifier!r}")
    return ExtractedLink(
        platform=PLATFORM_TENCENT,
        raw=m.group(0),
        identifier=identifier,
    )


def identify_link(text: str) -> Optional[ExtractedLink]:
    link = _extract_netease(text)
    if link:
        return link
    link = _extract_tencent(text)
    if link:
        return link
    return None


def extract_links(text: str) -> List[ExtractedLink]:
    if not text:
        return []
    d(f"[extract_links] text 长度={len(text)} 前 100 字={text[:100]!r}")

    seen: set[str] = set()
    results: List[ExtractedLink] = []
    chunks = LINK_REGEX.findall(text)
    d(f"[extract_links] LINK_REGEX 命中 {len(chunks)} 个 URL 片段")
    for chunk in chunks:
        d(f"[extract_links]   试识别: {chunk[:80]!r}")
        link = identify_link(chunk)
        if not link:
            d(f"[extract_links]     → 不匹配任何平台")
            continue
        if link.raw in seen:
            d(f"[extract_links]     → 重复，已跳过")
            continue
        seen.add(link.raw)
        results.append(link)
        d(f"[extract_links]     → 识别为 [{link.platform}] id={link.identifier}")
    d(f"[extract_links] 共返回 {len(results)} 个链接")
    return results


def extract_links_by_force(text: str, force_platform: str) -> List[ExtractedLink]:
    urls = LINK_REGEX.findall(text or "")
    d(f"[extract_links_by_force] 强制平台={force_platform}, 找到 {len(urls)} 个 URL")
    extractor = _extract_netease if force_platform == PLATFORM_NETEASE else _extract_tencent
    out: List[ExtractedLink] = []
    for u in urls:
        link = extractor(u)
        if link:
            out.append(link)
    return out


# ──────────── 卡片 URL 提取 ────────────

def _extract_candidate_urls_from_meta(meta: dict) -> List[str]:
    """从一个 meta dict 里尽可能多地抽 URL 候选。

    字段优先级（按出现概率排序）：
    1. meta.music.jumpUrl       ← 网易云 QQ 分享卡片
    2. meta.music.musicUrl      ← 网易云 QQ 分享卡片（备用）
    3. meta.detail_1.qqdocurl   ← 标准 miniapp 卡片
    4. meta.news.jumpUrl        ← 公众号 / 新闻卡片
    """
    if not isinstance(meta, dict):
        return []
    candidates: List[str] = []

    # 1) 音乐小程序 view=music
    music = meta.get("music") or {}
    if isinstance(music, dict):
        for k in ("jumpUrl", "musicUrl", "shareUrl"):
            v = music.get(k)
            if isinstance(v, str) and v:
                candidates.append(v)

    # 2) 标准 miniapp
    detail_1 = meta.get("detail_1") or {}
    if isinstance(detail_1, dict):
        v = detail_1.get("qqdocurl")
        if isinstance(v, str) and v:
            candidates.append(v)
        # 某些版本的 detail_1 里还有 jumpUrl
        for k in ("jumpUrl", "url"):
            v = detail_1.get(k)
            if isinstance(v, str) and v:
                candidates.append(v)

    # 3) 公众号 / 新闻卡片
    news = meta.get("news") or {}
    if isinstance(news, dict):
        v = news.get("jumpUrl")
        if isinstance(v, str) and v:
            candidates.append(v)

    return candidates


def extract_url_from_card_data(msg_data) -> Optional[str]:
    """从单个消息段的 data 字段中提取 QQ 卡片 URL。

    支持：
    - 标准小程序 (com.tencent.miniapp)         → meta.detail_1.qqdocurl
    - 公众号 / 新闻卡片                        → meta.news.jumpUrl
    - 音乐小程序 (com.tencent.music.lua 等)     → meta.music.jumpUrl / musicUrl
    """
    try:
        # 解析 msg_data 可能是 dict 或含 data 字符串的 dict
        if isinstance(msg_data, dict) and not msg_data.get("data"):
            meta = msg_data.get("meta") or {}
            candidates = _extract_candidate_urls_from_meta(meta)
        else:
            json_str = (
                msg_data.get("data", "")
                if isinstance(msg_data, dict) else msg_data
            )
            if not json_str or not isinstance(json_str, str):
                return None
            try:
                message_data = json.loads(json_str)
            except (json.JSONDecodeError, TypeError) as e:
                d(f"[extract_url_from_card_data] JSON 解析失败: {e}")
                return None
            if not isinstance(message_data, dict):
                return None
            meta = message_data.get("meta") or {}
            candidates = _extract_candidate_urls_from_meta(meta)

        # 尝试每个候选 URL，过白名单后返回第一个
        for url in candidates:
            if not _is_music_url(url):
                d(
                    f"[extract_url_from_card_data] 非音乐小程序域名，静默过滤: "
                    f"{url[:80]!r}"
                )
                continue
            d(f"[extract_url_from_card_data] 抽出音乐 URL: {url[:80]!r}")
            return url
        return None
    except (AttributeError, KeyError, TypeError) as e:
        d(f"[extract_url_from_card_data] 异常: {e}")
        return None


def extract_card_urls_from_event(event) -> List[str]:
    urls: List[str] = []
    try:
        messages = event.get_messages() or []
    except Exception as e:
        d(f"[extract_card_urls_from_event] get_messages 失败: {e}")
        return urls
    d(f"[extract_card_urls_from_event] event 共有 {len(messages)} 个 component")
    for idx, comp in enumerate(messages):
        data = getattr(comp, "data", None)
        if data is None:
            d(
                f"[extract_card_urls_from_event]   component[{idx}]={type(comp).__name__} "
                f"无 data 字段，跳过"
            )
            continue
        d(
            f"[extract_card_urls_from_event]   component[{idx}]={type(comp).__name__} "
            f"data 类型={type(data).__name__}"
        )
        url = extract_url_from_card_data(data)
        if url and url not in urls:
            urls.append(url)
    d(f"[extract_card_urls_from_event] 共抽出 {len(urls)} 个卡片 URL")
    return urls


def extract_links_from_event(event) -> List[ExtractedLink]:
    text = getattr(event, "message_str", "") or ""
    out: List[ExtractedLink] = []
    seen: set[str] = set()

    d("[extract_links_from_event] ── 1) 文本链接 ──")
    for link in extract_links(text):
        if link.raw not in seen:
            seen.add(link.raw)
            out.append(link)

    d("[extract_links_from_event] ── 2) 卡片链接 ──")
    for card_url in extract_card_urls_from_event(event):
        link = identify_link(card_url)
        if link and link.raw not in seen:
            seen.add(link.raw)
            out.append(link)
            d(f"[extract_links_from_event]   卡片→平台: {card_url[:60]} → [{link.platform}]")
        elif link is None:
            d(
                f"[extract_links_from_event]   卡片URL 不匹配音乐平台: "
                f"{card_url[:60]}"
            )

    d(f"[extract_links_from_event] 合并后共 {len(out)} 个链接")
    return out


def extract_card_links_by_force(event, force_platform: str) -> List[ExtractedLink]:
    text = getattr(event, "message_str", "") or ""
    out: List[ExtractedLink] = []
    seen: set[str] = set()

    for link in extract_links_by_force(text, force_platform):
        if link.raw not in seen:
            seen.add(link.raw)
            out.append(link)

    for card_url in extract_card_urls_from_event(event):
        extractor = _extract_netease if force_platform == PLATFORM_NETEASE else _extract_tencent
        link = extractor(card_url)
        if link and link.raw not in seen:
            seen.add(link.raw)
            out.append(link)
    return out