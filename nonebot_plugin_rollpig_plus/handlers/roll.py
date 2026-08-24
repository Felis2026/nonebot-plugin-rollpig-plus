import random

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from ..roll_flow import (
    resolve_daily_pig,
)
from ..resource_manager import PIG_LIST, sync_rollpig_resources
from ..helpers import send_rendered_pig
from ..reservation_flow import deliver_newly_ready_reservations
from ..pighub_service import build_pighub_image_url, pighub_service
from ..texts import TOMORROW_TEXTS
from ..yesterday_card_renderer import render_yesterday_recap_card
from ..yesterday_recap import (
    YesterdayPigResourceMissingError,
    build_yesterday_recap,
)
from ..helpers import command_has_no_argument, guard_group_enabled, guard_store_errors
from ..helpers import get_event_group_id, is_superuser_user


def _build_pighub_image_message(pig: dict, image_url: str, fallback_title: str) -> Message:
    """把外部 PigHub 标题封装成纯文本消息，并追加已解析的图片段。"""

    title = str(pig.get("title") or fallback_title)
    return MessageSegment.text(title) + MessageSegment.image(image_url)


# 0. 小猪资源同步（管理员）
cmd_sync_resources = on_command(
    "同步小猪资源",
    aliases={"刷新小猪图鉴"},
    rule=command_has_no_argument,
    block=True,
)


@cmd_sync_resources.handle()
async def _(event: Event):
    user_id = str(event.user_id)
    if not is_superuser_user(user_id):
        await cmd_sync_resources.finish(MessageSegment.reply(event.message_id) + "只有超级用户可以同步小猪资源。")
        return

    try:
        message = await sync_rollpig_resources(force=True)
    except Exception as error:
        logger.error(f"rollpig 小猪资源手动同步失败: {error}")
        await cmd_sync_resources.finish(MessageSegment.reply(event.message_id) + f"小猪资源同步失败：{error}")
        return

    await cmd_sync_resources.finish(
        MessageSegment.reply(event.message_id)
        + (
            "🐷 小猪资源同步结果\n"
            f"{message}\n\n"
            f"🐽 小猪数量：{len(PIG_LIST)}"
        )
    )


# 1. 今日小猪
cmd_today = on_command(
    "今天是什么小猪",
    aliases={"今日小猪"},
    rule=command_has_no_argument,
    block=True,
)

@cmd_today.handle()
@guard_group_enabled(cmd_today)
@guard_store_errors(cmd_today)
async def _(event: Event):
    user_id = str(event.user_id)
    group_id = get_event_group_id(event)
    resolution = await resolve_daily_pig(user_id, group_id, include_progress=True)
    if resolution.missing_resources or not resolution.pig:
        await cmd_today.finish("猪圈塌房了（数据缺失）")
        return

    await send_rendered_pig(
        cmd_today,
        event,
        resolution.pig,
        extra_text=resolution.growth_text,
        ex_level=resolution.ex_level or 0,
        after_send=(
            (lambda: deliver_newly_ready_reservations(str(event.self_id)))
            if resolution.was_auto_created
            else None
        ),
    )


# 2. 随机小猪
cmd_roll = on_command("随机小猪", block=True)

@cmd_roll.handle()
@guard_group_enabled(cmd_roll)
async def _(bot: Bot, event: Event, args: Message = CommandArg()):
    if not await pighub_service.ensure_ready():
        await cmd_roll.finish("连不上 PigHub，请稍后再试。")
        return

    text = args.extract_plain_text().strip()
    try:
        count = int(text) if text else 1
    except ValueError:
        count = 1
    count = max(1, min(count, 10))

    selected = pighub_service.sample(count)
    if not selected:
        await cmd_roll.finish("PigHub 图片索引为空，请稍后再试。")
        return

    pig = selected[0]
    image_url = build_pighub_image_url(pig)
    if not image_url:
        await cmd_roll.finish("PigHub 返回了异常图片数据，请稍后再试。")
        return

    if count == 1:
        await cmd_roll.finish(MessageSegment.reply(event.message_id) + MessageSegment.image(image_url))
        return

    # 私聊不支持合并转发，降级为单张
    if not isinstance(event, GroupMessageEvent):
        await cmd_roll.finish(
            MessageSegment.reply(event.message_id)
            + "私聊暂不支持多张连发，先给你一张：\n"
            + MessageSegment.image(image_url)
        )
        return

    # 多图去重：用 sample 避免重复（若图库数量不足则取全部）
    messages = []
    for pig in selected:
        url = build_pighub_image_url(pig)
        if not url:
            continue
        messages.append({
            "type": "node",
            "data": {
                "name": "随机小猪Bot",
                "uin": event.self_id,
                # PigHub 标题属于外部数据，必须作为纯文本，不能让其中的 CQ 码变成消息段。
                "content": _build_pighub_image_message(pig, url, "随机小猪"),
            },
        })

    if not messages:
        await cmd_roll.finish("PigHub 图片数据异常，请稍后再试。")
        return

    try:
        await bot.send_group_forward_msg(group_id=event.group_id, messages=messages)
    except Exception as error:
        # OneBot 合并转发会让接入端预取远程图片；PigHub 抖动或图片过慢时可能超时。
        # 显式告诉用户当前外部图源或转发链路超时。
        logger.warning(f"随机小猪合并转发超时: {error}")
        await cmd_roll.finish(
            MessageSegment.reply(event.message_id)
            + "PigHub 图片加载或合并转发超时了，请稍后再试。"
        )


# 2.5 找猪
cmd_find = on_command("找猪", aliases={"搜猪"}, block=True)

@cmd_find.handle()
@guard_group_enabled(cmd_find)
async def _(bot: Bot, event: Event, args: Message = CommandArg()):
    if not await pighub_service.ensure_ready():
        await cmd_find.finish("连不上 PigHub，请稍后再试。")
        return

    keyword = args.extract_plain_text().strip()
    if not keyword:
        await cmd_find.finish("请加上关键词，如：/找猪 玩偶")
        return

    found_pigs = pighub_service.search(keyword)
    if not found_pigs:
        await cmd_find.finish(f"没找到叫「{keyword}」的猪。")
        return

    if isinstance(event, GroupMessageEvent):
        messages = []
        count = min(len(found_pigs), 10)
        for i in range(count):
            pig = found_pigs[i]
            image_url = build_pighub_image_url(pig)
            if not image_url:
                continue
            messages.append({
                "type": "node",
                "data": {
                    "name": "搜猪小助手",
                    "uin": event.self_id,
                    "content": _build_pighub_image_message(pig, image_url, "未命名小猪"),
                },
            })
        if not messages:
            await cmd_find.finish("搜索结果数据异常，请稍后再试。")
            return
        try:
            await bot.send_group_forward_msg(group_id=event.group_id, messages=messages)
        except Exception as error:
            # 群转发失败时必须显式回消息；否则用户只会看到“找猪没反应”。
            logger.warning(f"找猪合并转发超时: keyword={keyword}, error={error}")
            await cmd_find.finish(
                MessageSegment.reply(event.message_id)
                + "PigHub 图片加载或合并转发超时了，请稍后再试。"
            )
        return

    # 私聊降级：展示首条匹配
    pig = found_pigs[0]
    image_url = build_pighub_image_url(pig)
    if not image_url:
        await cmd_find.finish("搜索结果数据异常，请稍后再试。")
        return
    msg = _build_pighub_image_message(pig, image_url, "未命名小猪")
    if len(found_pigs) > 1:
        msg += MessageSegment.text(f"\n共找到 {len(found_pigs)} 张，私聊仅展示第 1 张。")
    await cmd_find.finish(MessageSegment.reply(event.message_id) + msg)


# 3. 明日小猪
cmd_tmr = on_command("明日小猪", rule=command_has_no_argument, block=True)

@cmd_tmr.handle()
@guard_group_enabled(cmd_tmr)
async def _(event: Event):
    await cmd_tmr.finish(MessageSegment.reply(event.message_id) + random.choice(TOMORROW_TEXTS))


# 4. 昨日小猪
cmd_yest = on_command("昨日小猪", rule=command_has_no_argument, block=True)


async def _handle_yesterday_pig(event: Event) -> None:
    """生成昨日回顾卡；失败时只返回错误，不再进入旧版普通卡流程。"""

    user_id = str(event.user_id)
    group_id = get_event_group_id(event)
    try:
        recap = await build_yesterday_recap(user_id, group_id=group_id)
    except YesterdayPigResourceMissingError as error:
        logger.warning(f"rollpig 昨日身份无法解析: user={user_id} pig_id={error}")
        await cmd_yest.finish(
            MessageSegment.reply(event.message_id)
            + "昨天那只猪暂时不在当前资源包里。"
        )
        return

    if recap is None:
        await cmd_yest.finish(MessageSegment.reply(event.message_id) + "你昨天没抽猪。")
        return

    try:
        render_result = await render_yesterday_recap_card(recap)
    except Exception as error:
        logger.error(
            "rollpig 昨日回顾卡片生成失败: "
            f"user={user_id} pig_id={recap.roll.pig_id} error={error}"
        )
        await cmd_yest.finish(
            MessageSegment.reply(event.message_id)
            + "昨日回顾卡生成失败，请稍后再试。"
        )
        return

    logger.info(
        "rollpig yesterday card rendered: "
        f"user={user_id} pig_id={recap.roll.pig_id} "
        f"renderer={render_result.renderer} format={render_result.image_format} "
        f"size={render_result.width}x{render_result.height} "
        f"bytes={len(render_result.data)} image_fallback={render_result.used_fallback_image}"
    )
    await cmd_yest.finish(
        MessageSegment.reply(event.message_id)
        + MessageSegment.image(render_result.data)
    )


@cmd_yest.handle()
@guard_group_enabled(cmd_yest)
@guard_store_errors(cmd_yest)
async def _(event: Event):
    await _handle_yesterday_pig(event)
