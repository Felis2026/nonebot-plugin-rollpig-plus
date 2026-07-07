from __future__ import annotations

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent
from nonebot.log import logger

from .runtime import rollpig_date_str
from .store import store


# ================================ 事件身份与群成员工具 ================================ #
# 本模块只做事件对象解析和群成员候选筛选，不注册 matcher，也不直接发送消息。
# 这样命令层可以继续留在 __init__.py，同时把容易复用的事件解析逻辑先移出大文件。


def get_event_group_id(event: Event | None) -> str:
    """从事件中提取群号；私聊或未知事件返回空字符串。"""
    return str(event.group_id) if isinstance(event, GroupMessageEvent) else ""


def get_event_user_name(event: Event) -> str:
    """按群名片、昵称、用户 ID 的优先级取用户展示名。"""
    sender = getattr(event, "sender", None)
    if sender:
        return getattr(sender, "card", "") or getattr(sender, "nickname", "") or str(getattr(event, "user_id", ""))
    return str(getattr(event, "user_id", ""))


def is_superuser_user(user_id: str) -> bool:
    """兼容 NoneBot superusers 里可能出现的 adapter:user_id 写法。"""
    superusers = {str(x) for x in getattr(get_driver().config, "superusers", set())}
    if user_id in superusers:
        return True
    return any(s.endswith(f":{user_id}") for s in superusers)


async def get_group_roll_candidates(bot: Bot, group_id: int, exclude_ids: set[str]) -> list[str]:
    """优先按当前群成员范围筛候选；接口异常时回退到群内已登记过的今日形态。"""
    today = rollpig_date_str()
    today_rolls = await store.get_daily_rolls(today)

    try:
        members = await bot.call_api("get_group_member_list", group_id=group_id)
        member_ids = {
            str(member.get("user_id"))
            for member in members
            if member.get("user_id") is not None
        }
        return [uid for uid in today_rolls if uid in member_ids and uid not in exclude_ids]
    except Exception as e:
        logger.debug(f"获取群成员列表失败: group={group_id} error={e}")
        group_rolls = await store.get_group_rolls(str(group_id), today)
        return [uid for uid in group_rolls if uid not in exclude_ids]
