from __future__ import annotations

from functools import wraps

from nonebot.adapters.onebot.v11 import Event, MessageSegment
from nonebot.log import logger

from .event import get_event_group_id
from ..runtime import is_group_rollpig_enabled
from ..store.cloud import CloudStoreError


# ================================ 命令守卫 ================================ #
# 守卫只负责入口拦截和统一错误兜底，不承载具体业务逻辑。
# matcher 注册仍留在 __init__.py，避免拆命令文件时触发 NoneBot 导入副作用风险。


def _find_event(args: tuple, kwargs: dict) -> Event | None:
    """从 matcher handler 的位置参数和关键字参数中寻找 OneBot 事件对象。"""
    event = kwargs.get("event")
    if isinstance(event, Event):
        return event
    for arg in args:
        if isinstance(arg, Event):
            return arg
    return None


def guard_group_enabled(matcher):
    """统一拦截未启用 RollPig 的群聊指令；私聊默认放行。"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            event = _find_event(args, kwargs)
            group_id = get_event_group_id(event)
            if group_id and not is_group_rollpig_enabled(group_id):
                logger.debug(f"rollpig 群功能未启用，跳过处理: group={group_id}")
                await matcher.finish()

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def guard_store_errors(matcher, message: str = "猪圈云账本暂时离线，请稍后再试。"):
    """把云端账本不可用转换为用户可读提示，避免底层异常刷屏。"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            event = _find_event(args, kwargs)
            try:
                return await func(*args, **kwargs)
            except CloudStoreError as error:
                logger.warning(f"rollpig cloud store unavailable: {error}")
                if event is not None:
                    await matcher.finish(MessageSegment.reply(event.message_id) + message)
                await matcher.finish(message)

        return wrapper

    return decorator
