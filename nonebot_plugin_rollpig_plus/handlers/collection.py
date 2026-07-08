import datetime
import time

from nonebot import get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from ..config import Config
from ..roll_flow import build_pigsty_growth_summary
from ..catalog_renderer import render_catalog_image
from ..card_renderer import render_weekly_pig_image
from ..runtime import rollpig_today
from ..resource_manager import PIG_LIST, find_image_file, get_pig_by_id
from ..store import store
from ..helpers import guard_group_enabled, guard_store_errors
from ..helpers import get_event_user_name
from ..helpers import log_perf


# 6. 我的猪圈
cmd_sty = on_command("我的猪圈", aliases={"我的小猪"}, block=True)

@cmd_sty.handle()
@guard_group_enabled(cmd_sty)
@guard_store_errors(cmd_sty)
async def _(event: Event):
    user_id = str(event.user_id)
    draw_state = await store.get_draw_state(user_id)
    total_pigs = len(PIG_LIST)
    user_count = len(draw_state.pig_ids)

    if total_pigs <= 0:
        await cmd_sty.finish(MessageSegment.reply(event.message_id) + "猪图鉴为空，请先检查资源文件。")
        return

    if user_count == 0:
        await cmd_sty.finish(MessageSegment.reply(event.message_id) + "你的猪圈空空如也！")
        return

    msg = build_pigsty_growth_summary(
        event.sender.card or event.sender.nickname,
        draw_state,
        total_pigs,
    )
    await cmd_sty.finish(MessageSegment.reply(event.message_id) + msg)


# 6.5 图片版小猪图鉴
cmd_catalog = on_command("小猪图鉴", aliases={"猪猪图鉴", "完整图鉴"}, block=True)


@cmd_catalog.handle()
@guard_group_enabled(cmd_catalog)
@guard_store_errors(cmd_catalog)
async def _(event: Event, args: Message = CommandArg()):
    plugin_config = get_plugin_config(Config)
    if not plugin_config.rollpig_catalog_enabled:
        await cmd_catalog.finish(MessageSegment.reply(event.message_id) + "图片版小猪图鉴当前未启用。")
        return

    raw_arg = args.extract_plain_text().strip()
    page = 1
    if raw_arg:
        try:
            page = max(1, int(raw_arg.split()[0]))
        except ValueError:
            await cmd_catalog.finish(MessageSegment.reply(event.message_id) + "页码需要是数字，例如：小猪图鉴 2")
            return

    user_id = str(event.user_id)
    if not PIG_LIST:
        await cmd_catalog.finish(MessageSegment.reply(event.message_id) + "猪图鉴为空，请先检查资源文件。")
        return

    command_started_at = time.perf_counter()
    snapshot_started_at = time.perf_counter()
    snapshot = await store.get_catalog_snapshot(user_id, days=14)
    snapshot_ready_at = time.perf_counter()
    if not snapshot.draw_state.pig_ids:
        await cmd_catalog.finish(MessageSegment.reply(event.message_id) + "你的猪圈空空如也！发送「今日小猪」开始收集。")
        return

    try:
        pic = await render_catalog_image(
            user_name=get_event_user_name(event),
            snapshot=snapshot,
            page=page,
        )
        render_ready_at = time.perf_counter()
    except Exception as error:
        logger.error(f"小猪图鉴渲染失败: user={user_id} page={page} error={error}")
        await cmd_catalog.finish(MessageSegment.reply(event.message_id) + "小猪图鉴生成失败，请稍后再试。")
        return

    log_perf(
        f"rollpig catalog command ready: user={user_id} page={page} "
        f"snapshot={snapshot_ready_at - snapshot_started_at:.2f}s "
        f"render={render_ready_at - snapshot_ready_at:.2f}s "
        f"total_before_send={render_ready_at - command_started_at:.2f}s "
        f"bytes={len(pic)}"
    )
    await cmd_catalog.finish(MessageSegment.reply(event.message_id) + MessageSegment.image(pic))


# 7. 本周小猪
cmd_week = on_command("本周小猪", block=True)

@cmd_week.handle()
@guard_group_enabled(cmd_week)
@guard_store_errors(cmd_week)
async def _(event: Event):
    user_id = str(event.user_id)
    today = rollpig_today()

    images_to_merge = []
    for i in range(7):
        d = today - datetime.timedelta(days=(6 - i))
        pig = get_pig_by_id(await store.get_pig_by_date(user_id, d.isoformat()))
        if pig:
            img_file = find_image_file(pig["id"])
            if img_file:
                images_to_merge.append(img_file)

    if not images_to_merge:
        await cmd_week.finish(MessageSegment.reply(event.message_id) + "你这周还没抽过猪呢！")
        return

    try:
        image_data = await render_weekly_pig_image(images_to_merge)
        msg = (
            MessageSegment.reply(event.message_id)
            + f"你这周变了 {len(images_to_merge)} 次猪！"
            + MessageSegment.image(image_data)
        )
    except Exception as e:
        logger.error(f"本周小猪长图生成失败: user={user_id}, error={e}")
        await cmd_week.finish("生成图片失败。")
        return

    await cmd_week.finish(msg)
