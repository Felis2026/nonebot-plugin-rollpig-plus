from __future__ import annotations

from functools import lru_cache
from typing import Any

from nonebot.log import logger as nonebot_logger


@lru_cache(maxsize=1)
def get_perf_logger() -> Any:
    """获取 RollPig 性能日志使用的 logger。

    普通 NoneBot INFO 日志会被过滤，而项目自带 logger 可以正常出现在
    Docker 日志里；用可选导入保持外部插件仓库仍能独立运行。
    """
    try:
        from src.plugins.utils.utils import get_logger as get_v2_logger
    except Exception:
        return nonebot_logger

    try:
        return get_v2_logger("RollPig")
    except Exception:
        return nonebot_logger


def log_perf(message: str) -> None:
    """输出性能埋点；logger 本身异常时退回 warning，避免诊断日志影响业务。"""
    try:
        get_perf_logger().info(message)
    except Exception:
        nonebot_logger.warning(message)
