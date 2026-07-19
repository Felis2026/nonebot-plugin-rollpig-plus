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
    pick_self_roast_block_text,
)
from ..roll_flow import (
    resolve_daily_pig,
)
from ..runtime import resolve_roast_charge_max, resolve_roast_cooldown_seconds
from ..resource_manager import get_pig_by_id
from ..helpers import finish_roast_outcome, send_rendered_pig
from ..store import store
from ..store.models import RoastEvent
from ..texts import (
    AUTO_ROLL_ROAST_TEXTS,
    PROTECTION_BLOCK_TEXTS,
    PROTECTION_BREAK_TEXTS,
    RANDOM_ROAST_INTRO_TEXTS,
    ROAST_BOT_TEXTS,
)
from ..helpers import guard_group_enabled, guard_store_errors
from ..helpers import (
    get_event_group_id,
    get_event_user_name,
    get_group_member_display_name,
    get_group_roll_candidates,
    resolve_roast_target,
)


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
    attacker_pig = get_pig_by_id(await store.get_daily_roll(attacker_id))

    if attacker_pig:
        await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    if force_mode == "super_denied":
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id) + "口令【强行点火】仅 superuser 可用。"
        )
        return

    target = await resolve_roast_target(bot, event)
    target_id = target.target_id
    target_name = target.target_name

    if not target_id:
        await cmd_roast_member.finish("请 At 或回复你要烤的群友！")
        return

    if target_id == attacker_id:
        await cmd_roast_member.finish("对自己好一点，别自焚。请发送「今日烤猪」。")
        return

    # 检测目标是否是 Bot 自身 → 特殊反噬，不消耗 CD，纯文本回复
    if target_id == str(event.self_id):
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
    # 读取目标形态（后门模式也不绕过此检查）
    target_pig = get_pig_by_id(await store.get_daily_roll(target_id))
    if not target_pig:
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id) + f"【{target_name}】今天还没抽猪，没法下嘴！"
        )
        return
    await store.mark_group_roll_seen(target_id, target_pig["id"], group_id)

    # 保护检查：被烤最多的用户次日受保护（后门可突破）
    if await store.is_protected(group_id, target_id):
        if force_mode in {"normal", "super"}:
            break_text = random.choice(PROTECTION_BREAK_TEXTS).format(target=target_name)
            logger.info(f"[烤群友] 保护被突破 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})")
            await cmd_roast_member.send(MessageSegment.reply(event.message_id) + break_text)
        else:
            prot_text = random.choice(PROTECTION_BLOCK_TEXTS).format(target=target_name)
            await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + prot_text)
            return

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


# 5.6 随机烤群友
cmd_random_roast = on_command("随机烤群友", aliases={"随机烤猪", "抽个群友烤了"}, block=True)

@cmd_random_roast.handle()
@guard_group_enabled(cmd_random_roast)
@guard_store_errors(cmd_random_roast)
async def _(bot: Bot, event: GroupMessageEvent):
    attacker_id = str(event.user_id)
    attacker_name = event.sender.card or event.sender.nickname
    group_id = str(event.group_id)
    attacker_pig = get_pig_by_id(await store.get_daily_roll(attacker_id))

    if attacker_pig:
        await store.mark_group_roll_seen(attacker_id, attacker_pig["id"], group_id)

    bot_id = str(event.self_id)
    candidates = await get_group_roll_candidates(bot, event.group_id, {attacker_id, bot_id})

    if not candidates:
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id) + "今天还没有别人抽猪，没有可以烤的目标！"
        )
        return

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
