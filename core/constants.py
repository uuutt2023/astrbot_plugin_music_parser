"""常量定义。"""

DEFAULT_TIMEOUT: int = 30          # 单次 HTTP 请求超时（秒）
DEFAULT_RETRY: int = 2             # 失败重试次数
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# 默认端口与目录
NETEASE_DEFAULT_PORT: int = 5000   # Suxiaoqinx/Netease_url
TENCENT_DEFAULT_PORT: int = 5122   # Suxiaoqinx/tencent_url
CACHE_DIR_NAME: str = "music_cache"

# 平台标识
PLATFORM_NETEASE = "netease"
PLATFORM_TENCENT = "tencent"

# 输出模式
MODE_OFF = "关闭"
MODE_ALL = "全部发送"
MODE_TEXT_ONLY = "仅文本"
MODE_AUDIO_ONLY = "仅音频"

# 网易云音质档（与 Suxiaoqinx/Netease_url level 参数对齐）
NETEASE_QUALITIES = (
    "standard", "exhigh", "lossless", "hires",
    "jyeffect", "sky", "jymaster",
)

# QQ 音乐音质档（与 Suxiaoqinx/tencent_url file_type 对齐）
TENCENT_QUALITIES = (
    "128", "320", "flac", "ape",
    "master", "atmos_2", "atmos_51",
)