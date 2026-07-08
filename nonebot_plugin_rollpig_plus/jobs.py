from __future__ import annotations

import asyncio
import random
from contextlib import suppress

from nonebot import get_bot, get_driver, get_plugin_config
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .config import Config
from .catalog_renderer import shutdown_catalog_renderer
from .runtime import (
    is_daily_summary_enabled,
    is_group_rollpig_enabled,
    rollpig_date_str,
)
from .resource_manager import get_pig_by_id, sync_rollpig_resources
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, pighub_service
from .store import store
from .store.cloud import CloudStoreError
from .texts import DAILY_SUMMARY_EMPTY_TEXTS, DAILY_SUMMARY_FOOTER, DAILY_SUMMARY_HEADER


background_resource_sync_tasks: set[asyncio.Task[None]] = set()


# ================================ 运行时生命周期 ================================ #
@get_driver().on_shutdown
async def shutdown_rollpig_runtime() -> None:
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


# ================================ 资源同步任务 ================================ #
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


# ================================ 每日总结任务 ================================ #
def build_daily_summary_text(summary: dict) -> str:
    """将按群聚合后的日报结果拼成文案。"""

    roll_count = summary.get("roll_count", 0)
    roast_total = summary.get("total", 0)

    if roll_count == 0 and roast_total == 0:
        return random.choice(DAILY_SUMMARY_EMPTY_TEXTS)

    lines = [DAILY_SUMMARY_HEADER]

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

        if summary.get("most_roasted_id") and summary["most_roasted_count"] >= 2:
            lines.append(f"\n\U0001f6e1\ufe0f 【{summary['most_roasted_name']}】明天将获得猪圈保护协议，免受一切烧烤！")
    else:
        lines.append("\U0001f54a 今天无人烧烤，猪们度过了平静的一天。")

    lines.append("\n" + DAILY_SUMMARY_FOOTER)
    return "\n".join(lines)


@scheduler.scheduled_job("cron", hour=23, minute=45, id="rollpig_daily_summary")
async def daily_summary_job():
    """每晚 23:45~23:55 推送当日猪圈日报（随机延迟 0~10 分钟防风控）。"""

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
        # 如果宿主项目接入了群开关，这里必须在定时任务层同步收口：
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
            except Exception as error:
                logger.warning(f"[每日总结] 推送失败: group={group_id} error={error}")

        logger.info(f"[每日总结] 推送完成, 共 {len(summary_push_groups)} 个群")
    except CloudStoreError as error:
        logger.warning(f"[每日总结] 云端账本暂时不可用，跳过本轮推送: {error}")
    except Exception as error:
        logger.error(f"[每日总结] 任务异常: {error}")

