from __future__ import annotations

import random

from nonebot import get_bot, on_command, on_notice
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageSegment, NoticeEvent
from nonebot.log import logger

from ..helpers import (
    get_event_user_name,
    guard_group_enabled,
    guard_store_errors,
    is_superuser_user,
)
from ..roast_refill import (
    RoastRefillReactionError,
    add_refill_reaction,
    build_refill_created_message,
    extract_message_id,
    fetch_refill_group_members,
    fetch_refill_reactors,
    format_existing_refill,
    pick_refill_reaction_hint,
    pick_refill_unsupported_text,
    process_refill_notice,
)
from ..runtime import resolve_roast_charge_max
from ..store import store
from ..store.cloud import CloudRoastRefillUnsupportedError
from ..texts import ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS, ROAST_REFILL_PERMISSION_DENIED_TEXTS


cmd_roast_refill = on_command(
    "烤箱补货",
    aliases={"重置烤猪次数", "恢复烧烤配额", "申请烤箱补给", "重置烧烤次数"},
    block=True,
)
roast_refill_notice = on_notice(block=False, priority=5)


def _can_start_refill(event: GroupMessageEvent) -> bool:
    """只有本群群主、管理员或 NoneBot superuser 可以主持补货投票。"""

    if is_superuser_user(str(event.user_id)):
        return True
    return getattr(event.sender, "role", "") in {"owner", "admin"}


async def _fail_unusable_request(request_id: str, message_id: str, reason: str) -> None:
    try:
        await store.fail_group_roast_refill(request_id, message_id, reason)
    except Exception as error:
        logger.warning(f"rollpig 烤箱补货失败状态写入异常: request={request_id} error={error}")


@cmd_roast_refill.handle()
@guard_group_enabled(cmd_roast_refill)
@guard_store_errors(cmd_roast_refill)
async def _(bot: Bot, event: Event):
    if not isinstance(event, GroupMessageEvent):
        await cmd_roast_refill.finish("烤箱补货只能在群聊中发起。")
        return
    if not _can_start_refill(event):
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id) + random.choice(ROAST_REFILL_PERMISSION_DENIED_TEXTS)
        )
        return

    try:
        preparation = await store.prepare_group_roast_refill(
            group_id=str(event.group_id),
            initiator_id=str(event.user_id),
            initiator_name=get_event_user_name(event),
            delivery_bot_id=str(bot.self_id),
        )
    except CloudRoastRefillUnsupportedError:
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id)
            + "当前 Cloud 版本尚未支持烤箱补货，请先升级 RollPig Cloud。其他功能不受影响。"
        )
        return

    if preparation.status == "insufficient_active":
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id) + random.choice(ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS)
        )
        return
    if preparation.request is None:
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + "补货申请没有成功建立，请稍后再试。")
        return

    request = preparation.request
    if preparation.status == "existing":
        delivery_bot = bot
        if request.delivery_bot_id != str(bot.self_id):
            try:
                delivery_bot = get_bot(request.delivery_bot_id)
            except KeyError:
                await cmd_roast_refill.finish(
                    MessageSegment.reply(event.message_id)
                    + "现有补货申请仍在有效期内，但负责投递的 Bot 暂时离线，票数稍后再查。"
                )
                return
        try:
            text = await format_existing_refill(delivery_bot, request)
        except RoastRefillReactionError as error:
            if error.message_missing:
                await _fail_unusable_request(request.request_id, request.message_id, "message_missing")
            text = pick_refill_unsupported_text()
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + text)
        return

    try:
        send_result = await bot.send_group_msg(
            group_id=event.group_id,
            message=build_refill_created_message(request, resolve_roast_charge_max()),
        )
    except Exception as error:
        await _fail_unusable_request(request.request_id, "", "send_failed")
        logger.warning(f"rollpig 烤箱补货投票消息发送失败: request={request.request_id} error={error}")
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + "补货投票消息发送失败，本轮没有扣改任何次数。")
        return

    message_id = extract_message_id(send_result)
    if not message_id:
        await _fail_unusable_request(request.request_id, "", "message_id_missing")
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + pick_refill_unsupported_text())
        return
    bound = await store.bind_group_roast_refill_message(request.request_id, message_id)
    if bound is None:
        await _fail_unusable_request(request.request_id, message_id, "bind_failed")
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + "投票消息状态绑定失败，本轮申请已停止。")
        return

    reaction_added = await add_refill_reaction(bot, message_id)
    try:
        # 发起时主动探测 fetch 能力；无法核验名单时绝不退化为管理员直接重置。
        await fetch_refill_reactors(bot, message_id)
        await fetch_refill_group_members(bot, str(event.group_id))
    except RoastRefillReactionError as error:
        await _fail_unusable_request(request.request_id, message_id, "reaction_unsupported")
        logger.warning(f"rollpig 烤箱补货 reaction 能力不可用: request={request.request_id} error={error}")
        await bot.send_group_msg(group_id=event.group_id, message=pick_refill_unsupported_text())
        return
    if not reaction_added:
        await bot.send_group_msg(group_id=event.group_id, message=pick_refill_reaction_hint())


@roast_refill_notice.handle()
async def _(bot: Bot, event: NoticeEvent):
    try:
        await process_refill_notice(bot, event)
    except CloudRoastRefillUnsupportedError:
        return
    except Exception as error:
        # Notice 属于旁路事件，任何异常都不能影响其他插件的事件分发。
        logger.warning(f"rollpig 烤箱补货 Notice 处理失败: error={error}")
