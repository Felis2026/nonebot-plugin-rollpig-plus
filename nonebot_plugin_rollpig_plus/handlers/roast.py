import random

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageSegment
from nonebot.log import logger

from ..roast_flow import (
    RoastFoodMissingError,
    build_member_roast_outcome,
    build_self_roast_data,
    detect_force_roast_mode,
    format_roast_outcome_log,
    format_cooldown_message,
    pick_force_limit_text,
    pick_food_pig,
    pick_member_target_block_text,
    pick_random_target_block_text,
    pick_reservation_prepare_text,
    pick_self_roast_block_text,
)
from ..roll_flow import (
    resolve_daily_pig,
)
from ..runtime import resolve_roast_charge_max, resolve_roast_cooldown_seconds
from ..resource_manager import get_pig_by_id
from ..helpers import finish_roast_outcome, send_rendered_pig
from ..reservation_delivery import register_owned_reservation
from ..reservation_flow import deliver_newly_ready_reservations
from ..store import store
from ..store.cloud import CloudReservationUnsupportedError
from ..store.models import RoastEvent
from ..texts import (
    AUTO_ROLL_ROAST_TEXTS,
    PROTECTION_BLOCK_TEXTS,
    PROTECTION_BREAK_TEXTS,
    RANDOM_ROAST_INTRO_TEXTS,
    ROAST_BOT_TEXTS,
    UNROLLED_FORCE_ROAST_BACKFIRE_TEXTS,
    UNROLLED_ROAST_BACKFIRE_TEXTS,
    UNROLLED_ROAST_WARNING_TEXTS,
)
from ..helpers import guard_group_enabled, guard_store_errors
from ..helpers import (
    GroupMemberLookupError,
    get_event_group_id,
    get_event_user_name,
    get_group_member_display_name,
    get_group_roll_candidates,
    resolve_roast_target,
)


# ================================ 未抽猪攻击者限制 ================================ #


def _register_preparation_owner(preparation, current_bot_id: str) -> None:
    """只要返回的已有预约仍归当前 Bot 负责，就恢复本地 owner 轮询状态。"""

    reservation = preparation.reservation
    if reservation and reservation.delivery_bot_id == str(current_bot_id):
        register_owned_reservation(str(current_bot_id))


def _classify_roast_target(attacker_id: str, target_id: str, bot_id: str) -> str:
    """先区分用法错误、自身与 Bot；只有 member 才进入未抽猪处罚流程。"""

    if not target_id:
        return "missing"
    if target_id == attacker_id:
        return "self"
    if target_id == bot_id:
        return "bot"
    return "member"


async def _finish_unrolled_attacker_attempt(matcher, event: GroupMessageEvent, attacker_name: str, force_mode):
    """记录未抽猪先烤次数；首次警告，第二次起临时熟食反噬且不创建今日猪。"""

    try:
        attempt = await store.record_unrolled_roast_attempt(str(event.user_id))
    except CloudReservationUnsupportedError:
        # 新插件连接旧 Cloud 时没有持久化计数能力，保守降级为警告，不允许进入正常烧烤。
        await matcher.finish(MessageSegment.reply(event.message_id) + random.choice(UNROLLED_ROAST_WARNING_TEXTS))
        return

    if attempt.count <= 1:
        await matcher.finish(MessageSegment.reply(event.message_id) + random.choice(UNROLLED_ROAST_WARNING_TEXTS))
        return

    try:
        food_pig = pick_food_pig().copy()
    except RoastFoodMissingError as error:
        await matcher.finish(str(error))
        return
    pool = UNROLLED_FORCE_ROAST_BACKFIRE_TEXTS if force_mode in {"normal", "super"} else UNROLLED_ROAST_BACKFIRE_TEXTS
    food_pig["analysis"] = random.choice(pool).format(attacker=attacker_name, food=food_pig.get("name", "熟食"))
    await send_rendered_pig(matcher, event, food_pig, cache_final_card=False)


async def _load_attacker_pig_or_finish(
    matcher,
    event: GroupMessageEvent,
    attacker_name: str,
    force_mode,
):
    """区分真正未抽猪与本机资源落后，避免把资源故障计成违规。"""

    attacker_pig_id = await store.get_daily_roll(str(event.user_id))
    if not attacker_pig_id:
        await _finish_unrolled_attacker_attempt(matcher, event, attacker_name, force_mode)
        return None
    attacker_pig = get_pig_by_id(attacker_pig_id)
    if not attacker_pig:
        await matcher.finish(
            MessageSegment.reply(event.message_id)
            + "你的今日小猪记录存在，但本机资源暂时缺失，请稍后再试。"
        )
        return None
    return attacker_pig


# 5. 今日烤猪
cmd_roast = on_command("今日烤猪", block=True)

@cmd_roast.handle()
@guard_group_enabled(cmd_roast)
@guard_store_errors(cmd_roast)
async def _(event: Event):
    user_id = str(event.user_id)
    group_id = get_event_group_id(event)
    attacker_name = get_event_user_name(event)
    resolution = await resolve_daily_pig(user_id, group_id)
    original_pig = resolution.pig
    if resolution.missing_resources or not original_pig:
        await cmd_roast.finish(MessageSegment.reply(event.message_id) + "猪圈埋房了（数据缺失）")
        return

    auto_roll_hint = ""
    if resolution.was_auto_created:
        auto_roll_hint = random.choice(AUTO_ROLL_ROAST_TEXTS).format(name=original_pig["name"]) + "\n"

    block_text = pick_self_roast_block_text(original_pig)
    if block_text:
        if resolution.was_auto_created:
            await cmd_roast.send(MessageSegment.reply(event.message_id) + block_text)
            await deliver_newly_ready_reservations(str(event.self_id))
            await cmd_roast.finish()
            return
        await cmd_roast.finish(MessageSegment.reply(event.message_id) + block_text)
        return

    try:
        roasted_pig_data, food_name = await build_self_roast_data(original_pig)
    except RoastFoodMissingError as e:
        await cmd_roast.finish(str(e))
        return

    if group_id:
        await store.append_roast_event(
            RoastEvent(
                event_type="self_roast",
                attacker_id=user_id,
                target_id=user_id,
                attacker_name=attacker_name,
                target_name=attacker_name,
                food=food_name,
                group_id=group_id,
            )
        )
    await send_rendered_pig(
        cmd_roast,
        event,
        roasted_pig_data,
        extra_text=auto_roll_hint,
        cache_final_card=False,
        after_send=(
            (lambda: deliver_newly_ready_reservations(str(event.self_id)))
            if resolution.was_auto_created
            else None
        ),
    )


# 5.5 烤群友
# `加急生火` 是日常使用频率最高的后门口令，因此额外开放为直达触发命令。
# 旧写法 `烤群友 加急生火 @某人` 保持兼容；这里只是让高频输入更顺手。
cmd_roast_member = on_command("烤群友", aliases={"加急生火"}, block=True)

@cmd_roast_member.handle()
@guard_group_enabled(cmd_roast_member)
@guard_store_errors(cmd_roast_member)
async def _(bot: Bot, event: GroupMessageEvent):
    attacker_id = str(event.user_id)
    attacker_name = event.sender.card or event.sender.nickname
    group_id = str(event.group_id)
    force_mode = detect_force_roast_mode(event.get_plaintext(), attacker_id)
    if force_mode == "super_denied":
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id) + "口令【强行点火】仅 superuser 可用。"
        )
        return

    target = await resolve_roast_target(bot, event)
    target_id = target.target_id
    target_name = target.target_name
    target_kind = _classify_roast_target(attacker_id, target_id, str(event.self_id))

    if target_kind == "missing":
        await cmd_roast_member.finish("请 At 或回复你要烤的群友！")
        return

    if target_kind == "self":
        await cmd_roast_member.finish("对自己好一点，别自焚。请发送「今日烤猪」。")
        return

    # 检测目标是否是 Bot 自身 → 特殊反噬，不消耗 CD，纯文本回复
    if target_kind == "bot":
        try:
            food_name = pick_food_pig()["name"]
        except RoastFoodMissingError:
            food_name = "美食"
        bot_text = random.choice(ROAST_BOT_TEXTS).format(attacker=attacker_name, food=food_name)
        logger.info(f"[烤群友→Bot] 特殊反噬 | 凶手={attacker_name}({attacker_id}) 变成={food_name}")
        await store.append_roast_event(
            RoastEvent(
                event_type="bot_backfire",
                attacker_id=attacker_id,
                target_id=target_id,
                attacker_name=attacker_name,
                target_name=target_name,
                food=food_name,
                group_id=group_id,
            )
        )
        await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + bot_text)
        return

    if not target.is_group_member:
        # 被 @ 的 QQ 号可能从未在群内，回复对象也可能已经退群。未确认当前成员
        # 身份前不能把对方登记为本群日活，更不能让其影响补货门槛。
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id) + "暂时无法确认对方仍在本群，请核对成员后再试。"
        )
        return

    # 只有解析出真实可烤的群友后才记录“未抽猪先烤”。用法错误、自烤和 Bot
    # 特殊互动都不应消耗违规次数，更不能触发第二次反噬。
    attacker_pig = await _load_attacker_pig_or_finish(
        cmd_roast_member,
        event,
        attacker_name,
        force_mode,
    )
    if not attacker_pig:
        return
    await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    # ================================ 目标状态与预约准备 ================================ #
    # Cloud 由 prepare 在同一事务中消费资源并创建预约；目标已抽猪时只返回 pig_id，
    # 后续继续沿用现有即时烧烤的扣次与判定逻辑。
    preparation = None
    try:
        preparation = await store.prepare_roast_reservation(
            attacker_id=attacker_id,
            attacker_name=attacker_name,
            attacker_pig_id=attacker_pig["id"],
            target_id=target_id,
            target_name=target_name,
            group_id=group_id,
            delivery_bot_id=str(event.self_id),
            force_mode=force_mode,
            cooldown_seconds=resolve_roast_cooldown_seconds(),
            max_charges=resolve_roast_charge_max(),
        )
    except CloudReservationUnsupportedError:
        target_pig_id = await store.get_daily_roll(target_id)
        if not target_pig_id:
            await cmd_roast_member.finish(
                MessageSegment.reply(event.message_id) + f"【{target_name}】今天还没抽猪，没法下嘴！"
            )
            return
        target_pig = get_pig_by_id(target_pig_id)
        if not target_pig:
            await cmd_roast_member.finish(
                MessageSegment.reply(event.message_id) + "目标的小猪记录存在，但本机资源暂时缺失，请稍后再试。"
            )
            return
        if await store.is_protected(group_id, target_id):
            if force_mode in {"normal", "super"}:
                break_text = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name)
                await cmd_roast_member.send(MessageSegment.reply(event.message_id) + break_text)
            else:
                await cmd_roast_member.finish(
                    MessageSegment.reply(event.message_id)
                    + random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name)
                )
                return
    else:
        # Cloud 已创建预约但响应丢失时，重试会返回 already_joined。只按
        # reservation_created 注册会漏掉这个当前 Bot 仍然负责的预约。
        _register_preparation_owner(preparation, str(event.self_id))
        if preparation.status != "target_ready":
            if preparation.status == "protected":
                await cmd_roast_member.finish(
                    MessageSegment.reply(event.message_id)
                    + random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name)
                )
                return
            if preparation.status == "cooldown_denied" and preparation.cooldown:
                await cmd_roast_member.finish(
                    MessageSegment.reply(event.message_id)
                    + format_cooldown_message(preparation.cooldown.remaining_seconds)
                )
                return
            if preparation.status == "force_denied":
                await cmd_roast_member.finish(
                    MessageSegment.reply(event.message_id) + pick_force_limit_text(attacker_name, target_name)
                )
                return
            prefix = ""
            if preparation.protection_broken:
                prefix = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name) + "\n"
            await cmd_roast_member.finish(
                MessageSegment.reply(event.message_id)
                + prefix
                + pick_reservation_prepare_text(
                    preparation,
                    attacker_name=attacker_name,
                    target_name=target_name,
                )
            )
            return
        target_pig = get_pig_by_id(preparation.target_pig_id)

    if preparation is not None and preparation.protection_broken:
        break_text = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name)
        logger.info(f"[烤群友] 保护被突破 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})")
        await cmd_roast_member.send(MessageSegment.reply(event.message_id) + break_text)

    if not target_pig:
        await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + "目标的小猪资源缺失，暂时无法开火。")
        return
    await store.mark_group_roll_seen(target_id, target_pig["id"], group_id)

    block_text = pick_member_target_block_text(target_name, target_pig)
    if block_text:
        await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + block_text)
        return

    # 模式化限制/计数
    if force_mode == "normal":
        if not await store.consume_force_usage(attacker_id):
            reject_text = pick_force_limit_text(attacker_name, target_name)
            await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + reject_text)
            return
    elif force_mode is None:
        cooldown_result = await store.consume_roast_cooldown(
            attacker_id,
            cooldown_seconds=resolve_roast_cooldown_seconds(),
            max_charges=resolve_roast_charge_max(),
        )
        if not cooldown_result.allowed:
            await cmd_roast_member.finish(
                MessageSegment.reply(event.message_id) + format_cooldown_message(cooldown_result.remaining_seconds)
            )
            return
    # super 模式：无限制，不消耗后门次数，不走 CD

    try:
        outcome = await build_member_roast_outcome(
            attacker_pig=attacker_pig,
            target_pig=target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            force_mode=force_mode,
        )
    except RoastFoodMissingError as e:
        await cmd_roast_member.finish(str(e))
        return

    logger.info(
        format_roast_outcome_log(
            "烤群友",
            attacker_name=attacker_name,
            attacker_id=attacker_id,
            target_name=target_name,
            target_id=target_id,
            outcome=outcome,
            force_mode=force_mode,
        )
    )

    await finish_roast_outcome(
        cmd_roast_member,
        event,
        outcome,
        attacker_id=attacker_id,
        attacker_name=attacker_name,
        target_id=target_id,
        target_name=target_name,
        group_id=group_id,
    )


# ================================ 随机烤目标预检 ================================ #


async def _load_random_roast_context_or_finish(matcher, bot: Bot, event: GroupMessageEvent, attacker_name: str):
    """先确认群内存在可选目标，再检查攻击者并记录未抽猪违规。"""

    attacker_id = str(event.user_id)
    bot_id = str(event.self_id)
    try:
        candidates = await get_group_roll_candidates(bot, event.group_id, {attacker_id, bot_id})
    except GroupMemberLookupError:
        await matcher.finish(
            MessageSegment.reply(event.message_id) + "暂时无法读取当前群成员，随机烤猪没有执行。"
        )
        return None, []
    if not candidates:
        await matcher.finish(
            MessageSegment.reply(event.message_id) + "今天还没有别人抽猪，没有可以烤的目标！"
        )
        return None, []
    attacker_pig = await _load_attacker_pig_or_finish(
        matcher,
        event,
        attacker_name,
        None,
    )
    return attacker_pig, candidates


# 5.6 随机烤群友
cmd_random_roast = on_command("随机烤群友", aliases={"随机烤猪", "抽个群友烤了"}, block=True)

@cmd_random_roast.handle()
@guard_group_enabled(cmd_random_roast)
@guard_store_errors(cmd_random_roast)
async def _(bot: Bot, event: GroupMessageEvent):
    attacker_id = str(event.user_id)
    attacker_name = event.sender.card or event.sender.nickname
    group_id = str(event.group_id)
    attacker_pig, candidates = await _load_random_roast_context_or_finish(
        cmd_random_roast,
        bot,
        event,
        attacker_name,
    )

    if not attacker_pig:
        return
    await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    target_id = random.choice(candidates)

    target_name = await get_group_member_display_name(bot, event.group_id, target_id)

    # 读取目标形态
    target_pig = get_pig_by_id(await store.get_daily_roll(target_id))
    if not target_pig:
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id) + f"系统随机选中了【{target_name}】，但对方的猪数据异常。"
        )
        return
    await store.mark_group_roll_seen(target_id, target_pig["id"], group_id)

    # 保护检查
    if await store.is_protected(group_id, target_id):
        prot_text = random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name)
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id)
            + f"系统随机选中了【{target_name}】——\n{prot_text}"
        )
        return

    block_text = pick_random_target_block_text(target_name, target_pig)
    if block_text:
        await cmd_random_roast.finish(MessageSegment.reply(event.message_id) + block_text)
        return

    # 只有可实际进入烧烤判定的目标才消耗次数；数据异常、保护和特殊形态拦截均不应扣次。
    cooldown_result = await store.consume_roast_cooldown(
        attacker_id,
        cooldown_seconds=resolve_roast_cooldown_seconds(),
        max_charges=resolve_roast_charge_max(),
    )
    if not cooldown_result.allowed:
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id) + format_cooldown_message(cooldown_result.remaining_seconds)
        )
        return

    # 正常概率判定
    intro = random.choice(RANDOM_ROAST_INTRO_TEXTS).format(target=target_name) + "\n\n"
    try:
        outcome = await build_member_roast_outcome(
            attacker_pig=attacker_pig,
            target_pig=target_pig,
            attacker_name=attacker_name,
            target_name=target_name,
            intro_text=intro,
        )
    except RoastFoodMissingError as e:
        await cmd_random_roast.finish(str(e))
        return

    logger.info(
        format_roast_outcome_log(
            "随机烤群友",
            attacker_name=attacker_name,
            attacker_id=attacker_id,
            target_name=target_name,
            target_id=target_id,
            outcome=outcome,
        )
    )

    await finish_roast_outcome(
        cmd_random_roast,
        event,
        outcome,
        attacker_id=attacker_id,
        attacker_name=attacker_name,
        target_id=target_id,
        target_name=target_name,
        group_id=group_id,
    )
