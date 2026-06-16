"""统一日志：优先复用 AstrBot 提供的 logger，没有则降级到标准 logging。

verbose 模式：DEBUG 级（每个解析节点都打）。
非 verbose 模式：INFO 级（只在关键节点打）。
"""

from __future__ import annotations

import logging
import sys
import traceback

_logger: logging.Logger | None = None
_verbose: bool = False


def _build_std_logger() -> logging.Logger:
    lg = logging.getLogger("astrbot_plugin_music_parser")
    if not lg.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] [music_parser] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    try:
        from astrbot.api import logger as _astr_logger  # type: ignore
        _logger = _astr_logger
    except Exception:
        _logger = _build_std_logger()
    return _logger


def set_verbose(verbose: bool) -> None:
    """verbose=True 时打开 DEBUG 级。"""
    global _verbose
    _verbose = bool(verbose)
    lg = get_logger()
    if hasattr(lg, "setLevel"):
        try:
            lg.setLevel(logging.DEBUG if _verbose else logging.INFO)
        except Exception:
            pass


def is_verbose() -> bool:
    return _verbose


def log_exc(logger_obj, msg: str) -> None:
    """打日志 + 完整堆栈（verbose 模式才有堆栈，否者只打 msg）。"""
    if _verbose:
        logger_obj.exception(msg)
    else:
        logger_obj.warning(f"{msg}: {traceback.format_exc().splitlines()[-1] if traceback.format_exc() else '?'}")


def d(msg: str) -> None:
    """verbose 时才打的 debug 日志。"""
    if _verbose:
        get_logger().debug(msg)


def i(msg: str) -> None:
    """关键节点 INFO 日志（无论 verbose 与否都打）。"""
    get_logger().info(msg)


def w(msg: str) -> None:
    get_logger().warning(msg)


def e(msg: str) -> None:
    get_logger().error(msg)