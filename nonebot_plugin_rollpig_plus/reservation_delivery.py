from __future__ import annotations

import asyncio
import time

from nonebot import get_driver
from nonebot.log import logger

from .store import store


DELIVERY_CHECK_INTERVAL_SECONDS = 90.0
_owned_bot_ids: set[str] = set()
_last_check_by_bot: dict[str, float] = {}
_tasks_by_bot: dict[str, asyncio.Task] = {}


def register_owned_reservation(delivery_bot_id: str) -> None:
    """创建预约后记住负责 Bot；后续只在现有用户请求上节流检查，不启定时轮询。"""

    if delivery_bot_id:
        _owned_bot_ids.add(str(delivery_bot_id))


async def restore_owned_reservations(delivery_bot_id: str) -> None:
    """Bot 连接时只恢复查询一次，避免重启丢失轻量 owner 标记。"""

    bot_id = str(delivery_bot_id)
    try:
        if await store.has_owned_roast_reservations(bot_id):
            _owned_bot_ids.add(bot_id)
    except Exception as error:
        logger.warning(f"rollpig 恢复预约 owner 状态失败: bot={bot_id} error={error}")


async def _deliver_if_due(bot_id: str) -> None:
    # 延迟导入避免 helpers -> reservation_delivery -> reservation_flow -> roast_flow
    # -> helpers 的模块初始化环；真正检查时插件已完成全部模块加载。
    from .reservation_flow import deliver_ready_reservations

    now = time.monotonic()
    if bot_id not in _owned_bot_ids:
        return
    if now - _last_check_by_bot.get(bot_id, 0.0) < DELIVERY_CHECK_INTERVAL_SECONDS:
        return
    _last_check_by_bot[bot_id] = now
    completed = await deliver_ready_reservations(bot_id)
    if completed == 0 and not await store.has_owned_roast_reservations(bot_id):
        _owned_bot_ids.discard(bot_id)


def schedule_opportunistic_delivery(delivery_bot_id: str) -> None:
    """在已有 RollPig 指令请求上附带一次节流检查；无人使用时不会产生请求。"""

    bot_id = str(delivery_bot_id or "")
    if not bot_id or bot_id not in _owned_bot_ids:
        return
    task = _tasks_by_bot.get(bot_id)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_deliver_if_due(bot_id))
    _tasks_by_bot[bot_id] = task


@get_driver().on_bot_connect
async def _restore_on_bot_connect(bot) -> None:
    await restore_owned_reservations(str(bot.self_id))
