import asyncio
import random
import datetime
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from nonebot import on_command, require, get_driver, get_bot, get_plugin_config
from nonebot.adapters.onebot.v11 import Event, MessageSegment, Message, GroupMessageEvent, Bot
from nonebot.params import CommandArg
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

# 确保依赖插件先被 NoneBot 注册（必须在本地模块 import 之前）
# data_manager.py 在模块加载时会调用 store.get_plugin_data_file()；
# 定时任务也需要 apscheduler 提前完成插件注册，避免商店/静态审核误判。
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

# 本地模块（在 require() 之后 import）
from .command_guards import guard_group_enabled, guard_store_errors
from .config import Config
from .card_renderer import render_pig_card_image
from .catalog_renderer import render_catalog_image, shutdown_catalog_renderer
from .event_utils import (
    get_event_group_id,
    get_event_user_name,
    get_group_roll_candidates,
    is_superuser_user,
)
from .perf_logging import log_perf
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, build_pighub_image_url, pighub_service
from .resource_manager import pig_resource_manager
from .flows.roll import (
    build_pigsty_growth_summary,
    build_roll_growth_text,
    pick_daily_roll_candidate,
)
from .flows.roast import (
    RoastFoodMissingError,
    build_member_roast_outcome,
    build_self_roast_data,
    detect_force_roast_mode,
    format_cooldown_message,
    is_eaten_pig,
    is_food_pig,
    is_human_pig,
    is_sold_pig,
    pick_force_limit_text,
    pick_food_pig,
)
from .runtime import (
    is_daily_summary_enabled,
    rollpig_date_str,
    rollpig_today,
    resolve_roast_charge_max,
    resolve_roast_cooldown_seconds,
)
from .store import store
from .store.models import RoastEvent
from .summary_service import build_daily_summary
from .texts import (
    TOMORROW_TEXTS,
    TODAY_ROAST_HUMAN_BLOCK_TEXTS, TODAY_ROAST_EATEN_BLOCK_TEXTS, TODAY_ROAST_SOLD_BLOCK_TEXTS, TODAY_ROAST_FOOD_BLOCK_TEXTS,
    TARGET_HUMAN_BLOCK_TEXTS, TARGET_EATEN_BLOCK_TEXTS, TARGET_SOLD_BLOCK_TEXTS, TARGET_FOOD_BLOCK_TEXTS,
    ROAST_BOT_TEXTS,
    AUTO_ROLL_ROAST_TEXTS,
    DAILY_SUMMARY_EMPTY_TEXTS, DAILY_SUMMARY_HEADER, DAILY_SUMMARY_FOOTER,
    PROTECTION_BLOCK_TEXTS, PROTECTION_BREAK_TEXTS,
    RANDOM_ROAST_INTRO_TEXTS,
)

# --- 引入 PIL ---
try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ========================================================

__plugin_meta__ = PluginMetadata(
    name="今天是什么小猪（今日小猪）Plus",
    description="基于原版Rollpig的拓展分支，新增拓展烤群友、图鉴成长等多项玩法",
    usage="""
    🐷 基础指令：
    今日小猪 / 今天是什么小猪 - 抽取今天的命运之猪
    随机小猪 - 随机看一张猪图
    找猪 -  从 PigHub 模糊搜索猪猪图
    
    🔮 趣味指令：
    明日小猪 - 预测明天的猪猪运势
    昨日小猪 - 查看昨天抽到了什么
    今日烤猪 - 把今天的猪做成美食
    烤群友 - 把群友做成烤猪（目标需已抽猪）
    烤群友 + 加急生火 - 每日一次强制成功（目标仍需已抽猪）
    加急生火 + @目标 / 回复目标 - 直达触发一次普通后门烧烤
    烤群友 + 强行点火 - superuser 专属，无限强制成功
    
    📊 统计指令：
    我的猪圈 - 查看解锁进度
    小猪图鉴 - 生成图片版小猪图鉴
    本周小猪 - 生成本周猪猪总结长图
    """,
    type="application",
    homepage="https://github.com/Felis2026/nonebot-plugin-rollpig-plus",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

background_resource_sync_tasks: set[asyncio.Task[None]] = set()


@get_driver().on_shutdown
async def _shutdown_rollpig_runtime() -> None:
    """释放图鉴页面池与存储后端连接，避免长期运行或重载后残留浏览器/HTTP 资源。"""
    # 启动期资源同步是后台任务；退出时必须先收束，避免同步仍在改缓存目录时关闭运行时。
    for task in list(background_resource_sync_tasks):
        task.cancel()
    for task in list(background_resource_sync_tasks):
        with suppress(asyncio.CancelledError):
            await task
    background_resource_sync_tasks.clear()
    await pighub_service.shutdown()
    await shutdown_catalog_renderer()
    await store.close()

# ================= 资源路径 =================

PLUGIN_DIR = Path(__file__).parent
RES_DIR = PLUGIN_DIR / "resource"

# ================= 资源加载 =================

PIG_LIST: list[dict] = []


def reload_rollpig_resources() -> None:
    """刷新内存中的小猪资源快照；云端资源坏掉时由资源管理器自动回退到内置资源。"""
    global PIG_LIST
    pig_resource_manager.reload()
    PIG_LIST = pig_resource_manager.pig_list


reload_rollpig_resources()

# ================= 工具函数 =================

def find_image_file(pig_id: str) -> Path | None:
    return pig_resource_manager.find_image_file(pig_id)


def get_pig_by_id(pig_id: Optional[str]) -> Optional[dict]:
    if not pig_id:
        return None
    for p in PIG_LIST:
        if p["id"] == pig_id:
            return p
    return None


# ================= 辅助渲染函数 =================

async def send_rendered_pig(matcher, event, pig_data: dict, extra_text: str = ""):
    started_at = time.perf_counter()
    pig_id = pig_data.get("id", "")
    avatar_file = find_image_file(pig_id)
    name = pig_data.get("name", "未知小猪")
    payload_ready_at = time.perf_counter()

    try:
        render_started_at = time.perf_counter()
        render_result = await render_pig_card_image(pig_data, avatar_file)
        render_finished_at = time.perf_counter()
    except Exception as e:
        logger.error(f"图片渲染失败: pig_id={pig_id}, renderer=pillow, error={e}")
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
        f"emoji={render_result.emoji_enabled} extra={bool(extra_text)}"
    )
    await matcher.finish(msg)


async def sync_rollpig_resources(force: bool = False) -> str:
    """同步公有云端资源与可选私有 overlay；成功后立即刷新内存快照。"""
    public_result, private_result = await pig_resource_manager.sync_all(force=force, wait_if_busy=force)
    if public_result.updated or private_result.updated:
        PIG_LIST[:] = pig_resource_manager.pig_list

    messages = []
    for result in (public_result, private_result):
        if result.message:
            messages.append(result.message)
    return "；".join(messages) or "小猪资源无需同步"


# ================= 指令处理区域 =================

# 0. 小猪资源同步（管理员）
cmd_sync_resources = on_command("同步小猪资源", aliases={"刷新小猪图鉴"}, block=True)


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
        + f"{message}\n当前资源版本：{pig_resource_manager.resource_version}｜小猪数量：{len(PIG_LIST)}"
    )


# 1. 今日小猪
cmd_today = on_command("今天是什么小猪", aliases={"今日小猪"}, block=True)

@cmd_today.handle()
@guard_group_enabled(cmd_today)
@guard_store_errors(cmd_today)
async def _(event: Event):
    user_id = str(event.user_id)
    group_id = get_event_group_id(event)
    pig_id = await store.get_daily_roll(user_id)
    pig = get_pig_by_id(pig_id)
    extra_text = ""

    if not pig:
        if not PIG_LIST:
            await cmd_today.finish("猪圈塌房了（数据缺失）")
            return
        proposed_pig = await pick_daily_roll_candidate(user_id)
        roll_result = await store.get_or_create_daily_roll(
            user_id,
            proposed_pig["id"],
            group_id=group_id,
        )
        pig = get_pig_by_id(roll_result.pig_id) or proposed_pig
        extra_text = build_roll_growth_text(roll_result, pig)
    elif group_id:
        await store.mark_group_roll_seen(user_id, pig["id"], group_id)

    await send_rendered_pig(cmd_today, event, pig, extra_text=extra_text)


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
                "content": Message(pig.get("title", "随机小猪")) + MessageSegment.image(url),
            },
        })

    if not messages:
        await cmd_roll.finish("PigHub 图片数据异常，请稍后再试。")
        return

    try:
        await bot.send_group_forward_msg(group_id=event.group_id, messages=messages)
    except Exception as error:
        # OneBot 合并转发会让接入端预取远程图片；PigHub 抖动或图片过慢时可能超时。
        # 不降级发送一堆裸链接，避免刷屏；只显式告诉用户当前外部图源或转发链路超时。
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
                    "content": Message(pig.get("title", "未命名小猪")) + MessageSegment.image(image_url),
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
    msg = Message(pig.get("title", "未命名小猪"))
    msg += MessageSegment.image(image_url)
    if len(found_pigs) > 1:
        msg += Message(f"\n共找到 {len(found_pigs)} 张，私聊仅展示第 1 张。")
    await cmd_find.finish(MessageSegment.reply(event.message_id) + msg)


# 3. 明日小猪
cmd_tmr = on_command("明日小猪", block=True)

@cmd_tmr.handle()
@guard_group_enabled(cmd_tmr)
async def _(event: Event):
    await cmd_tmr.finish(MessageSegment.reply(event.message_id) + random.choice(TOMORROW_TEXTS))


# 4. 昨日小猪
cmd_yest = on_command("昨日小猪", block=True)

@cmd_yest.handle()
@guard_group_enabled(cmd_yest)
@guard_store_errors(cmd_yest)
async def _(event: Event):
    user_id = str(event.user_id)
    yesterday = rollpig_date_str(-1)
    pig = get_pig_by_id(await store.get_pig_by_date(user_id, yesterday))

    if not pig:
        await cmd_yest.finish(MessageSegment.reply(event.message_id) + "你昨天没抽猪。")
    msg = f"你昨天是一只【{pig['name']}】！"
    await send_rendered_pig(cmd_yest, event, pig, extra_text=msg)


# 5. 今日烤猪
cmd_roast = on_command("今日烤猪", block=True)

@cmd_roast.handle()
@guard_group_enabled(cmd_roast)
@guard_store_errors(cmd_roast)
async def _(event: Event):
    user_id = str(event.user_id)
    group_id = get_event_group_id(event)
    attacker_name = get_event_user_name(event)
    original_pig = get_pig_by_id(await store.get_daily_roll(user_id))

    auto_roll_hint = ""
    if not original_pig:
        if not PIG_LIST:
            await cmd_roast.finish(MessageSegment.reply(event.message_id) + "猪圈埋房了（数据缺失）")
            return
        proposed_pig = await pick_daily_roll_candidate(user_id)
        roll_result = await store.get_or_create_daily_roll(
            user_id,
            proposed_pig["id"],
            group_id=group_id,
        )
        original_pig = get_pig_by_id(roll_result.pig_id) or proposed_pig
        auto_roll_hint = random.choice(AUTO_ROLL_ROAST_TEXTS).format(name=original_pig["name"]) + "\n"
    elif group_id:
        await store.mark_group_roll_seen(user_id, original_pig["id"], group_id)

    if is_human_pig(original_pig):
        await cmd_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TODAY_ROAST_HUMAN_BLOCK_TEXTS)
        )
        return

    if is_eaten_pig(original_pig):
        await cmd_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TODAY_ROAST_EATEN_BLOCK_TEXTS)
        )
        return

    if is_sold_pig(original_pig):
        await cmd_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TODAY_ROAST_SOLD_BLOCK_TEXTS)
        )
        return

    if is_food_pig(original_pig):
        await cmd_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TODAY_ROAST_FOOD_BLOCK_TEXTS).format(shape=original_pig.get("name", "熟食"))
        )
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
    await send_rendered_pig(cmd_roast, event, roasted_pig_data, extra_text=auto_roll_hint)


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

    # 提取目标 ID 和名字
    target_id = None
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

    # @Bot 时框架会把 at 消费掉，补充判断
    if not target_id and event.to_me:
        target_id = str(event.self_id)

    # 尝试获取更准确的 target_name
    if target_id:
        try:
            member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(target_id))
            target_name = member_info.get("card") or member_info.get("nickname")
        except Exception as e:
            logger.debug(f"获取群成员信息失败: group={event.group_id} user={target_id} error={e}")

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

    if is_human_pig(target_pig):
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_HUMAN_BLOCK_TEXTS).format(target=target_name)
        )
        return

    if is_eaten_pig(target_pig):
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_EATEN_BLOCK_TEXTS).format(target=target_name)
        )
        return

    if is_sold_pig(target_pig):
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_SOLD_BLOCK_TEXTS).format(target=target_name)
        )
        return

    if is_food_pig(target_pig):
        await cmd_roast_member.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_FOOD_BLOCK_TEXTS).format(
                target=target_name, shape=target_pig.get("name", "熟食")
            )
        )
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

    if outcome.event_type == "success":
        log_mode = "后门成功" if force_mode in {"normal", "super"} else "成功"
        mode_suffix = f" 模式={force_mode}" if force_mode in {"normal", "super"} else ""
        logger.info(
            f"[烤群友] {log_mode} | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}){mode_suffix} 结果={outcome.food_name}"
        )
    elif outcome.event_type == "escape":
        logger.info(
            f"[烤群友] 逃脱 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})"
        )
    elif outcome.render_data:
        logger.info(
            f"[烤群友] 反噬 | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}) 凶手变成={outcome.food_name}"
        )
    else:
        logger.info(
            f"[烤群友] 反噬(文字) | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id})"
        )

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
        await send_rendered_pig(cmd_roast_member, event, outcome.render_data, extra_text=outcome.extra_text)
        return
    await cmd_roast_member.finish(MessageSegment.reply(event.message_id) + outcome.plain_text)


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

    # 获取目标昵称
    target_name = "群友"
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(target_id))
        target_name = member_info.get("card") or member_info.get("nickname") or "群友"
    except Exception:
        pass

    # 检查攻击者 CD
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

    # 目标是人类/熟食形态 → 拦截
    if is_human_pig(target_pig):
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id)
            + f"系统随机选中了【{target_name}】，但对方是人类形态，烤架拒绝处理。换一次试试？"
        )
        return

    if is_eaten_pig(target_pig):
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_EATEN_BLOCK_TEXTS).format(target=target_name)
        )
        return

    if is_sold_pig(target_pig):
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id)
            + random.choice(TARGET_SOLD_BLOCK_TEXTS).format(target=target_name)
        )
        return

    if is_food_pig(target_pig):
        await cmd_random_roast.finish(
            MessageSegment.reply(event.message_id)
            + f"系统随机选中了【{target_name}】，但对方已经是【{target_pig.get('name', '熟食')}】了，别鞭尸了。"
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

    if outcome.event_type == "success":
        logger.info(
            f"[随机烤群友] 成功 | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}) 结果={outcome.food_name}"
        )
    elif outcome.event_type == "escape":
        logger.info(
            f"[随机烤群友] 逃脱 | 凶手={attacker_name}({attacker_id}) 目标={target_name}({target_id})"
        )
    elif outcome.render_data:
        logger.info(
            f"[随机烤群友] 反噬 | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id}) 凶手变成={outcome.food_name}"
        )
    else:
        logger.info(
            f"[随机烤群友] 反噬(文字) | 凶手={attacker_name}({attacker_id}) "
            f"目标={target_name}({target_id})"
        )

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
        await send_rendered_pig(cmd_random_roast, event, outcome.render_data, extra_text=outcome.extra_text)
        return
    await cmd_random_roast.finish(MessageSegment.reply(event.message_id) + outcome.plain_text)


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
    if not HAS_PIL:
        await cmd_week.finish("Bot 未安装 PIL 库。")

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
        item_w, item_h = 150, 150
        padding = 20
        total_w = (item_w + padding) * len(images_to_merge) + padding
        total_h = item_h + 80

        canvas = PILImage.new("RGB", (total_w, total_h), (255, 255, 255))
        for idx, img_path in enumerate(images_to_merge):
            with PILImage.open(img_path) as opened:
                img = opened.convert("RGBA").resize((item_w, item_h))
                x = padding + idx * (item_w + padding)
                y = padding
                canvas.paste(img, (x, y), img)

        from io import BytesIO
        output = BytesIO()
        canvas.save(output, format="PNG")

        msg = (
            MessageSegment.reply(event.message_id)
            + f"你这周变了 {len(images_to_merge)} 次猪！"
            + MessageSegment.image(output.getvalue())
        )
    except Exception as e:
        logger.error(f"本周小猪长图生成失败: user={user_id}, error={e}")
        await cmd_week.finish("生成图片失败。")
        return

    await cmd_week.finish(msg)


# ================= 定时任务：每日总结 =================

def get_resource_sync_interval_hours() -> int:
    """读取资源同步间隔；配置异常时回退 24 小时，避免定时任务注册失败。"""
    try:
        config = get_plugin_config(Config)
        return max(1, int(config.rollpig_resource_sync_interval_hours or 24))
    except Exception as error:
        logger.warning(f"rollpig_resource_sync_interval_hours 配置非法，已回退到 24 小时: {error}")
        return 24


async def run_background_resource_sync(source: str) -> None:
    """后台同步云端小猪资源；任何异常都只记日志，不能影响主业务。"""
    try:
        message = await sync_rollpig_resources(force=False)
        logger.info(f"[小猪资源同步] {source}: {message}")
    except Exception as error:
        logger.warning(f"[小猪资源同步] {source} 失败，继续使用当前资源: {error}")


def schedule_background_resource_sync(source: str) -> None:
    """注册后台资源同步任务；统一追踪 task，shutdown 时可取消并等待。"""
    task: asyncio.Task[None] = asyncio.create_task(run_background_resource_sync(source))
    background_resource_sync_tasks.add(task)
    task.add_done_callback(background_resource_sync_tasks.discard)


@get_driver().on_startup
async def startup_pighub_refresh():
    """启动后随机延迟刷新 PigHub 索引；失败只影响外部找猪功能，不影响本地抽猪。"""
    pighub_service.schedule_startup_refresh()


@scheduler.scheduled_job("interval", hours=PIGHUB_REFRESH_INTERVAL_HOURS, id="rollpig_pighub_refresh", max_instances=1)
async def pighub_refresh_job():
    """低频刷新 PigHub 元数据索引；只请求列表 JSON，不下载图片本体。"""
    await pighub_service.refresh("interval")


@get_driver().on_startup
async def startup_resource_sync():
    """启动后异步检查一次资源包；不阻塞 NoneBot 启动和连接。"""
    config = get_plugin_config(Config)
    if not config.rollpig_resource_sync_enabled:
        return
    schedule_background_resource_sync("startup")


@scheduler.scheduled_job("interval", hours=get_resource_sync_interval_hours(), id="rollpig_resource_sync", max_instances=1)
async def resource_sync_job():
    """低频检查云端资源包，减少多实例手动同步新猪素材的运维成本。"""
    config = get_plugin_config(Config)
    if not config.rollpig_resource_sync_enabled:
        return
    await run_background_resource_sync("interval")


def build_daily_summary_text(summary: dict) -> str:
    """将按群聚合后的日报结果拼成文案。"""
    roll_count = summary.get("roll_count", 0)
    roast_total = summary.get("total", 0)

    # 完全无活动
    if roll_count == 0 and roast_total == 0:
        return random.choice(DAILY_SUMMARY_EMPTY_TEXTS)

    lines = [DAILY_SUMMARY_HEADER]

    # 抽猪统计
    if roll_count > 0:
        top_pig_id = summary.get("top_pig_id")
        if top_pig_id:
            pig_data = get_pig_by_id(top_pig_id)
            pig_name = pig_data["name"] if pig_data else top_pig_id
            lines.append(f"\U0001f451 最热门形态：【{pig_name}】（共 {summary.get('top_pig_count', 0)} 人抽到）")
        human_count = summary.get("human_count", 0)
        if human_count > 0:
            lines.append(f"\U0001f9cd 今日人类：{human_count} 位幸运儿逃过了猪化")
        lines.append("")

    # 烧烤统计
    if roast_total > 0:
        lines.append(f"\U0001f525 今日共发生 {roast_total} 场烧烤事件")

        if summary.get("most_active_id"):
            lines.append(f"\U0001f3c6 烧烤狂人：【{summary['most_active_name']}】（发起 {summary['most_active_count']} 次）")

        if summary.get("most_roasted_id"):
            lines.append(f"\U0001f356 最惨食材：【{summary['most_roasted_name']}】（被烤 {summary['most_roasted_count']} 次）")

        if summary.get("escape_king_id") and summary["escape_king_count"] > 0:
            lines.append(f"\U0001f3c3 逃脱大师：【{summary['escape_king_name']}】（成功逃脱 {summary['escape_king_count']} 次）")

        if summary.get("backfire_king_id") and summary["backfire_king_count"] > 0:
            lines.append(f"\U0001f4a5 反噬之王：【{summary['backfire_king_name']}】（自爆 {summary['backfire_king_count']} 次）")

        # 保护提示
        if summary.get("most_roasted_id") and summary["most_roasted_count"] >= 2:
            lines.append(f"\n\U0001f6e1\ufe0f 【{summary['most_roasted_name']}】明天将获得猪圈保护协议，免受一切烧烤！")
    else:
        lines.append("\U0001f54a 今天无人烧烤，猪们度过了平静的一天。")

    lines.append("\n" + DAILY_SUMMARY_FOOTER)
    return "\n".join(lines)


@scheduler.scheduled_job("cron", hour=23, minute=45, id="rollpig_daily_summary")
async def daily_summary_job():
    """每晚 23:45~23:55 推送当日猪圈日报（随机延迟 0~10 分钟防风控）。"""
    import asyncio
    config = get_plugin_config(Config)
    if not config.rollpig_daily_summary_enabled:
        logger.info("[每日总结] rollpig_daily_summary_enabled=false，跳过定时日报任务")
        return

    delay = random.randint(0, 600)  # 0~10 分钟随机延迟
    logger.info(f"[每日总结] 定时触发，随机延迟 {delay} 秒后推送")
    await asyncio.sleep(delay)
    try:
        active_groups = await store.get_active_group_ids()
        if not active_groups:
            logger.info("[每日总结] 今日无活跃群，跳过推送")
            return

        # ================================ 控制台开关过滤 ================================ #
        # 如果宿主项目接入了 admin_console 群开关，这里必须在定时任务层同步收口：
        # 未启用的群既不推日报，也不写次日保护名单，保证“关闭就是彻底关闭”。
        enabled_active_groups = [
            group_id for group_id in sorted(active_groups)
            if is_group_rollpig_enabled(group_id)
        ]
        if not enabled_active_groups:
            logger.info("[每日总结] 今日没有启用 rollpig 的活跃群，跳过推送")
            return

        group_summaries = {}
        protect_date = rollpig_date_str(1)
        for group_id in enabled_active_groups:
            summary = await build_daily_summary(store, group_id=group_id)
            group_summaries[group_id] = summary
            if summary.get("most_roasted_id") and summary.get("most_roasted_count", 0) >= 2:
                await store.replace_group_protections(group_id, [summary["most_roasted_id"]], protect_date)
            else:
                await store.replace_group_protections(group_id, [], protect_date)

        # 清理旧事件
        await store.prune_events(days_to_keep=7)
        await store.prune_history(days_to_keep=14)

        try:
            bot = get_bot()
        except ValueError:
            logger.warning("[每日总结] 无可用 Bot，跳过推送")
            return

        # ================================ 日报推送开关过滤 ================================ #
        # “日报推送”是独立于 rollpig 主功能的第二层开关：
        # 群内玩法可以开启，但日报消息可以单独关闭。
        summary_push_groups = [
            group_id for group_id in enabled_active_groups
            if is_daily_summary_enabled(group_id)
        ]
        if not summary_push_groups:
            logger.info("[每日总结] 已完成保护名单刷新，但没有群开启日报推送")
            return

        for group_id in summary_push_groups:
            try:
                text = build_daily_summary_text(group_summaries[group_id])
                await bot.send_group_msg(group_id=int(group_id), message=text)
            except Exception as e:
                logger.warning(f"[每日总结] 推送失败: group={group_id} error={e}")

        logger.info(f"[每日总结] 推送完成, 共 {len(summary_push_groups)} 个群")
    except CloudStoreError as e:
        logger.warning(f"[每日总结] 云端账本暂时不可用，跳过本轮推送: {e}")
    except Exception as e:
        logger.error(f"[每日总结] 任务异常: {e}")
