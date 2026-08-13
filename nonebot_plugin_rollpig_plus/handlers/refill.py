from __future__ import annotations

import asyncio
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
    find_group_roast_refill,
    format_existing_refill,
    get_refill_eligible_users,
    pick_refill_reaction_hint,
    pick_refill_unsupported_text,
    process_refill_notice,
    reconcile_refill_request,
)
from ..runtime import resolve_roast_charge_max, rollpig_date_str
from ..store import store
from ..store.cloud import CloudRoastRefillUnsupportedError
from ..store.models import (
    GroupRoastRefillPrepareResult,
    GroupRoastRefillRequest,
    legacy_roast_refill_threshold,
    roast_refill_threshold,
)
from ..texts import ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS, ROAST_REFILL_PERMISSION_DENIED_TEXTS


cmd_roast_refill = on_command(
    "烤箱补货",
    aliases={"重置烤猪次数", "恢复烧烤配额", "申请烤箱补给", "重置烧烤次数"},
    block=True,
)
roast_refill_notice = on_notice(block=False, priority=5)

# 同一 request_id + message_id 在 Local 与 Cloud 均可幂等重放。
# 两次短等待覆盖瞬时网络抖动，同时避免把命令处理拖成长任务。
ROAST_REFILL_BIND_RETRY_DELAYS = (0.25, 0.75)


def _can_start_refill(event: GroupMessageEvent) -> bool:
    """只有本群群主、管理员或 NoneBot superuser 可以主持补货投票。"""

    if is_superuser_user(str(event.user_id)):
        return True
    return getattr(event.sender, "role", "") in {"owner", "admin"}


# ================================ 申请恢复与成员校验 ================================ #

async def _fail_unusable_request(request_id: str, message_id: str, reason: str) -> None:
    try:
        await store.fail_group_roast_refill(request_id, message_id, reason)
    except Exception as error:
        logger.warning(f"rollpig 烤箱补货失败状态写入异常: request={request_id} error={error}")


async def _bind_refill_message_with_retry(
    request_id: str,
    message_id: str,
) -> GroupRoastRefillRequest | None:
    """有限重试消息绑定，处理 Cloud 已提交但响应丢失及短暂断网。"""

    total_attempts = len(ROAST_REFILL_BIND_RETRY_DELAYS) + 1
    for attempt in range(total_attempts):
        try:
            return await store.bind_group_roast_refill_message(request_id, message_id)
        except Exception as error:
            if attempt >= len(ROAST_REFILL_BIND_RETRY_DELAYS):
                logger.error(
                    f"rollpig 烤箱补货消息绑定重试耗尽: "
                    f"request={request_id} message={message_id} error={error}"
                )
                return None
            delay = ROAST_REFILL_BIND_RETRY_DELAYS[attempt]
            logger.warning(
                f"rollpig 烤箱补货消息绑定临时失败，准备重试: "
                f"request={request_id} message={message_id} "
                f"attempt={attempt + 1}/{total_attempts} error={error}"
            )
            await asyncio.sleep(delay)
    return None


async def _handle_post_send_probe_error(
    bot: Bot,
    request: GroupRoastRefillRequest,
    error: RoastRefillReactionError,
) -> bool:
    """处理发消息后的能力探测异常；返回是否必须终止当前投票。"""

    if not error.message_missing and not error.capability_unsupported:
        # 网络抖动和临时 OneBot 异常不能证明接口不可用。保留 voting，后续 Notice
        # 或再次执行命令会重新验票，避免一场已经发出的投票被永久作废。
        logger.warning(
            f"rollpig 烤箱补货发起后验票临时失败，申请保持有效: "
            f"request={request.request_id} error={error}"
        )
        return False

    reason = "message_missing" if error.message_missing else "reaction_unsupported"
    await _fail_unusable_request(request.request_id, request.message_id, reason)
    if error.message_missing:
        text = "补货投票消息已经失效，本轮申请已停止，请重新发起。"
    else:
        text = pick_refill_unsupported_text()
    logger.warning(
        f"rollpig 烤箱补货投票无法继续: "
        f"request={request.request_id} reason={reason} error={error}"
    )
    await bot.send_group_msg(group_id=int(request.group_id), message=text)
    return True


async def _reconcile_after_bind(bot: Bot, request: GroupRoastRefillRequest) -> bool:
    """绑定消息后立即补验票；返回是否应停止后续 reaction 引导。"""

    try:
        result = await reconcile_refill_request(bot, request)
    except RoastRefillReactionError as error:
        return await _handle_post_send_probe_error(bot, request, error)
    # 绑定重试期间可能已经收齐票；成功文案由统一结算函数发送。只有仍在
    # pending 的申请需要继续 reaction 引导，其他状态均应在此停止。
    return result.status != "pending"


def _preparation_matches_members(
    preparation: GroupRoastRefillPrepareResult,
    eligible_user_ids: set[str],
) -> bool:
    """确认 Store 确实按当前群成员冻结门槛；旧 Cloud 忽略新字段时安全停场。"""

    request = preparation.request
    if request is None:
        return False
    active_count = len(eligible_user_ids)
    expected_thresholds = {
        roast_refill_threshold(active_count, request.success_count_before),
        # 新 Plus 连接旧 Cloud 时仍接受旧门槛；升级 Cloud 后，新申请会自动
        # 使用 capped-v1。两种情况都继续以 Cloud 创建时保存的票数为准。
        legacy_roast_refill_threshold(active_count, request.success_count_before),
    }
    if (
        request.active_count_snapshot != active_count
        or (request.required_ratio, request.required_votes) not in expected_thresholds
    ):
        return False
    returned_active = {str(user_id) for user_id in preparation.active_user_ids if user_id}
    return not returned_active or returned_active == eligible_user_ids


async def _describe_existing_refill(
    bot: Bot,
    request: GroupRoastRefillRequest,
) -> str | None:
    """重查并尝试补结算已有投票；返回 None 表示成功文案已经发到群里。"""

    if not request.message_id:
        return "补货申请正在生成，请稍后再查看票数。"

    delivery_bot = bot
    if request.delivery_bot_id != str(bot.self_id):
        try:
            delivery_bot = get_bot(request.delivery_bot_id)
        except KeyError:
            return "现有补货申请仍在有效期内，但负责投递的 Bot 暂时离线，票数稍后再查。"

    try:
        result = await reconcile_refill_request(delivery_bot, request)
    except RoastRefillReactionError as error:
        if error.message_missing or error.capability_unsupported:
            reason = "message_missing" if error.message_missing else "reaction_unsupported"
            await _fail_unusable_request(request.request_id, request.message_id, reason)
            if error.message_missing:
                return "原补货投票消息已经失效，本轮申请已停止，请重新发起。"
            return pick_refill_unsupported_text()
        logger.warning(f"rollpig 烤箱补货恢复验票失败: request={request.request_id} error={error}")
        return "暂时没能读取当前票数，申请仍然有效，请稍后再试。"

    if result.completed:
        return None
    if result.status == "pending":
        return format_existing_refill(request, len(result.valid_voter_ids))
    if result.status == "unbound":
        return "补货申请正在生成，请稍后再查看票数。"
    if result.status == "group_disabled":
        return "本群 RollPig 当前已关闭，补货申请暂不结算。"
    if result.status == "succeeded":
        return "本轮烤箱补货已经完成。"
    if result.status in {"expired", "failed"}:
        return "上一轮补货申请已经结束，请重新发起。"
    return "补货申请状态刚刚发生变化，请稍后再试。"


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

    group_id = str(event.group_id)
    date_str = rollpig_date_str()

    # ================================ 跨日旧申请恢复 ================================ #

    try:
        # 投票可能在零点前创建、零点后收到回应。先找跨日旧申请，避免另起一轮。
        existing = await find_group_roast_refill(
            group_id,
            reference_ts=getattr(event, "time", None),
        )
    except CloudRoastRefillUnsupportedError:
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id)
            + "当前 Cloud 版本尚未支持烤箱补货，请先升级 RollPig Cloud。其他功能不受影响。"
        )
        return

    if existing is not None:
        text = await _describe_existing_refill(bot, existing)
        if text is None:
            await cmd_roast_refill.finish()
            return
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + text)
        return

    # ================================ 当前群成员门槛 ================================ #

    try:
        _, eligible_user_ids = await get_refill_eligible_users(bot, group_id, date_str)
    except RoastRefillReactionError as error:
        logger.warning(f"rollpig 烤箱补货群成员核对失败: group={group_id} error={error}")
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id) + "暂时无法核对本群活跃小猪，请稍后再试。"
        )
        return

    if len(eligible_user_ids) < 3:
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id) + random.choice(ROAST_REFILL_INSUFFICIENT_ACTIVE_TEXTS)
        )
        return

    # ================================ 原子创建与发送 ================================ #

    try:
        preparation = await store.prepare_group_roast_refill(
            group_id=str(event.group_id),
            initiator_id=str(event.user_id),
            initiator_name=get_event_user_name(event),
            delivery_bot_id=str(bot.self_id),
            eligible_user_ids=sorted(eligible_user_ids),
            date_str=date_str,
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
        text = await _describe_existing_refill(bot, request)
        if text is None:
            await cmd_roast_refill.finish()
            return
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + text)
        return

    if not _preparation_matches_members(preparation, eligible_user_ids):
        # 当前 Cloud 若尚未理解 eligible_user_ids，会返回未过滤的旧快照。
        # 立即停场可避免错误门槛和给已退群账号补充全局次数。
        await _fail_unusable_request(request.request_id, request.message_id, "member_snapshot_mismatch")
        await cmd_roast_refill.finish(
            MessageSegment.reply(event.message_id)
            + "今日活跃记录与当前群成员不一致，为避免误补次数，本轮未开始；记录会在次日自动刷新。"
        )
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
    bound = await _bind_refill_message_with_retry(request.request_id, message_id)
    if bound is None:
        await _fail_unusable_request(request.request_id, message_id, "bind_failed")
        await cmd_roast_refill.finish(MessageSegment.reply(event.message_id) + "投票消息状态绑定失败，本轮申请已停止。")
        return

    reaction_added = await add_refill_reaction(bot, message_id)
    # 绑定重试期间的 reaction Notice 无法匹配未绑定申请；绑定成功后立即走
    # 完整验票，既完成能力探测，也补结算这个窗口内已经到齐的票。
    if await _reconcile_after_bind(bot, bound):
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
