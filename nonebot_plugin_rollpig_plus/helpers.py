from __future__ import annotations

import time
from dataclasses import dataclass
from functools import wraps
from typing import Any

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.log import logger as nonebot_logger

from .card_renderer import render_pig_card_image
from .resource_manager import find_image_file
from .runtime import is_group_rollpig_enabled, rollpig_date_str
from .store import store
from .store.cloud import CloudStoreError
from .store.models import RoastEvent


# ================================ 事件身份与群成员工具 ================================ #
# 本板块只做事件对象解析和群成员候选筛选，不注册 matcher，也不直接承载业务判定。


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


@dataclass(frozen=True)
class RoastTarget:
    """烤群友目标解析结果；target_id 为空表示用户没有指定目标。"""

    target_id: str | None
    target_name: str


async def resolve_roast_target(bot: Bot, event: GroupMessageEvent) -> RoastTarget:
    """从回复、@ 和 to_me 中解析烤群友目标，并尽量补齐群名片。"""

    target_id: str | None = None
    target_name = "群友"

    if event.reply:
        target_id = str(event.reply.sender.user_id)
        target_name = event.reply.sender.card or event.reply.sender.nickname
    else:
        for seg in event.message:
            if seg.type == "at":
                target_id = str(seg.data["qq"])
                target_name = "对方"
                break

    # @Bot 时框架可能已经把 at 消费成 to_me，这里补一个 Bot 自身目标。
    if not target_id and event.to_me:
        target_id = str(event.self_id)

    if target_id:
        try:
            member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(target_id))
            target_name = member_info.get("card") or member_info.get("nickname") or target_name
        except Exception as error:
            logger.debug(f"获取群成员信息失败: group={event.group_id} user={target_id} error={error}")

    return RoastTarget(target_id=target_id, target_name=target_name)


async def get_group_member_display_name(bot: Bot, group_id: int, user_id: str, default: str = "群友") -> str:
    """按群名片优先获取成员展示名；接口失败时返回默认值，避免随机烤猪中断。"""

    try:
        member_info = await bot.get_group_member_info(group_id=group_id, user_id=int(user_id))
        return member_info.get("card") or member_info.get("nickname") or default
    except Exception:
        return default


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
    except Exception as error:
        logger.debug(f"获取群成员列表失败: group={group_id} error={error}")
        group_rolls = await store.get_group_rolls(str(group_id), today)
        return [uid for uid in group_rolls if uid not in exclude_ids]


# ================================ 命令守卫 ================================ #
# 守卫只负责入口拦截和统一错误兜底，不承载具体业务逻辑。


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


# ================================ 性能日志 ================================ #


def log_perf(message: str) -> None:
    """输出性能埋点；logger 本身异常时退回 warning，避免诊断日志影响业务。"""

    try:
        nonebot_logger.info(message)
    except Exception:
        nonebot_logger.warning(message)


# ================================ 小猪卡片发送 ================================ #
# 命令层只决定“要发哪只猪”；渲染、性能埋点和烤群友结果落库统一在这里。


async def send_rendered_pig(
    matcher,
    event: Event,
    pig_data: dict,
    extra_text: str = "",
    *,
    cache_final_card: bool = True,
) -> None:
    """渲染并发送卡片；固定卡缓存成品，烤猪动态文案只复用 GIF 源帧。"""

    started_at = time.perf_counter()
    pig_id = str(pig_data.get("id", ""))
    avatar_file = find_image_file(pig_id)
    name = pig_data.get("name", "未知小猪")
    payload_ready_at = time.perf_counter()

    try:
        render_started_at = time.perf_counter()
        render_result = await render_pig_card_image(
            pig_data,
            avatar_file,
            cache_final_card=cache_final_card,
        )
        render_finished_at = time.perf_counter()
    except Exception as error:
        logger.error(f"图片渲染失败: pig_id={pig_id}, renderer=pillow, error={error}")
        await matcher.finish("图片生成失败。")
        return

    msg = MessageSegment.reply(event.message_id)
    if extra_text:
        msg += extra_text + "\n"
    msg += MessageSegment.image(render_result.data)
    ready_to_send_at = time.perf_counter()

    log_perf(
        f"rollpig card rendered: renderer={render_result.renderer} "
        f"format={render_result.image_format} pig_id={pig_id} name={name} "
        f"image_found={avatar_file is not None} "
        f"payload={payload_ready_at - started_at:.2f}s "
        f"render={render_finished_at - render_started_at:.2f}s "
        f"message={ready_to_send_at - render_finished_at:.2f}s "
        f"total_before_send={ready_to_send_at - started_at:.2f}s "
        f"bytes={len(render_result.data)} "
        f"analysis_font={render_result.analysis_font_size} "
        f"analysis_lines={render_result.analysis_lines} "
        f"emoji={render_result.emoji_enabled} extra={bool(extra_text)} "
        f"card_cache={'final-disk' if cache_final_card else 'dynamic'}"
    )
    await matcher.finish(msg)


async def finish_roast_outcome(
    matcher,
    event: Event,
    outcome: Any,
    *,
    attacker_id: str,
    attacker_name: str,
    target_id: str,
    target_name: str,
    group_id: str,
) -> None:
    """落库并发送烤群友结果；outcome 使用 Any 避免与 roast_flow 形成循环 import。"""

    await store.append_roast_event(
        RoastEvent(
            event_type=outcome.event_type,
            attacker_id=attacker_id,
            target_id=target_id,
            attacker_name=attacker_name,
            target_name=target_name,
            food=outcome.food_name,
            group_id=group_id,
        )
    )
    if outcome.render_data:
        # 烤猪分析文案会随对象和结果变化；缓存最终成品会迅速挤满，故只复用 GIF 源帧。
        await send_rendered_pig(
            matcher,
            event,
            outcome.render_data,
            extra_text=outcome.extra_text,
            cache_final_card=False,
        )
        return
    await matcher.finish(MessageSegment.reply(event.message_id) + outcome.plain_text)
