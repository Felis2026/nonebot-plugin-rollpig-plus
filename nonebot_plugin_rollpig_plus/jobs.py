from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections import Counter
from contextlib import suppress

from nonebot import get_bot, get_driver
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .config import plugin_config
from .catalog_renderer import shutdown_catalog_renderer
from .runtime import (
    is_daily_summary_enabled,
    is_group_rollpig_enabled,
    rollpig_date_str,
)
from .resource_manager import get_pig_by_id, sync_rollpig_resources
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, pighub_service
from .store import store
from .store.base import RollpigStore
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
        return max(1, int(plugin_config.rollpig_resource_sync_interval_hours or 24))
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

    if not plugin_config.rollpig_resource_sync_enabled:
        return
    schedule_background_resource_sync("startup")


@scheduler.scheduled_job("interval", hours=get_resource_sync_interval_hours(), id="rollpig_resource_sync", max_instances=1)
async def resource_sync_job():
    """低频检查云端资源包，减少多实例手动同步新猪素材的运维成本。"""

    if not plugin_config.rollpig_resource_sync_enabled:
        return
    await run_background_resource_sync("interval")


# ================================ 每日总结任务 ================================ #
async def build_daily_summary(
    summary_store: RollpigStore,
    date_str: str | None = None,
    group_id: str | None = None,
) -> dict:
    """通过统一存储接口聚合指定日期、指定群的抽猪与烧烤数据。"""

    target_date = date_str or rollpig_date_str()
    today_rolls = (
        await summary_store.get_group_rolls(group_id, target_date)
        if group_id
        else await summary_store.get_daily_rolls(target_date)
    )
    events = await summary_store.list_daily_events(
        date_str=target_date,
        group_id=group_id,
    )

    roll_stats = _get_roll_stats(today_rolls)
    if not events and roll_stats.get("roll_count", 0) == 0:
        return {"total": 0, **roll_stats}

    roasted_counter: Counter[str] = Counter()
    attacker_counter: Counter[str] = Counter()
    escape_counter: Counter[str] = Counter()
    backfire_counter: Counter[str] = Counter()
    name_map: dict[str, str] = {}

    for event in events:
        attacker_id = str(event.get("attacker") or "")
        target_id = str(event.get("target") or "")
        if event.get("attacker_name"):
            name_map[attacker_id] = str(event["attacker_name"])
        if event.get("target_name"):
            name_map[target_id] = str(event["target_name"])

        event_type = event.get("type", "")
        if event_type == "success":
            attacker_counter[attacker_id] += 1
            # 自烤只计入发起次数，不能把自己算进“最惨食材”。
            if attacker_id and target_id and attacker_id != target_id:
                roasted_counter[target_id] += 1
        elif event_type == "self_roast":
            attacker_counter[attacker_id] += 1
        elif event_type == "escape":
            escape_counter[target_id] += 1
            attacker_counter[attacker_id] += 1
        elif event_type in {"backfire", "bot_backfire"}:
            backfire_counter[attacker_id] += 1
            attacker_counter[attacker_id] += 1

    def get_top(counter: Counter[str]) -> tuple[str | None, str, int]:
        """返回计数最高的用户 ID、显示名和次数；空计数器返回稳定空值。"""

        if not counter:
            return None, "", 0
        user_id, count = counter.most_common(1)[0]
        return user_id, name_map.get(user_id, user_id), count

    most_roasted_id, most_roasted_name, most_roasted_count = get_top(roasted_counter)
    most_active_id, most_active_name, most_active_count = get_top(attacker_counter)
    escape_king_id, escape_king_name, escape_king_count = get_top(escape_counter)
    backfire_king_id, backfire_king_name, backfire_king_count = get_top(backfire_counter)

    return {
        "total": len(events),
        "most_roasted_id": most_roasted_id,
        "most_roasted_name": most_roasted_name,
        "most_roasted_count": most_roasted_count,
        "most_active_id": most_active_id,
        "most_active_name": most_active_name,
        "most_active_count": most_active_count,
        "escape_king_id": escape_king_id,
        "escape_king_name": escape_king_name,
        "escape_king_count": escape_king_count,
        "backfire_king_id": backfire_king_id,
        "backfire_king_name": backfire_king_name,
        "backfire_king_count": backfire_king_count,
        **roll_stats,
    }


def _get_roll_stats(today_rolls: dict[str, str]) -> dict:
    """统计抽猪人数、最热门形态和人类形态人数。"""

    if not today_rolls:
        return {"roll_count": 0}

    pig_counter = Counter(today_rolls.values())
    top_pig_id, top_pig_count = pig_counter.most_common(1)[0]
    return {
        "roll_count": len(today_rolls),
        "top_pig_id": top_pig_id,
        "top_pig_count": top_pig_count,
        "human_count": sum(pig_id == "human" for pig_id in today_rolls.values()),
    }


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


@scheduler.scheduled_job("cron", hour=23, minute=45, timezone="Asia/Shanghai", id="rollpig_daily_summary")
async def daily_summary_job():
    """每晚 23:45~23:55 推送当日猪圈日报（随机延迟 0~10 分钟防风控）。"""

    # 日期必须在随机延迟和逐群查询前固定，避免任务跨过零点后混入次日数据。
    summary_date = rollpig_date_str()
    protect_date = (dt.date.fromisoformat(summary_date) + dt.timedelta(days=1)).isoformat()
    delay = random.randint(0, 600)  # 0~10 分钟随机延迟
    logger.info(f"[每日总结] 定时触发，随机延迟 {delay} 秒后推送")
    await asyncio.sleep(delay)
    try:
        active_groups = await store.get_active_group_ids(summary_date)
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

        # ================================ 日报推送开关过滤 ================================ #
        # 日报支持“默认关闭 + 分群开启”。必须先过滤出真正开启日报的群，
        # 再计算日报与次日保护名单，避免关闭日报的群被定时任务产生副作用。
        summary_push_groups = [
            group_id for group_id in enabled_active_groups
            if is_daily_summary_enabled(group_id)
        ]
        if not summary_push_groups:
            await store.prune_events(days_to_keep=7)
            await store.prune_history(days_to_keep=14)
            logger.info("[每日总结] 已完成旧数据清理，但没有群开启日报推送")
            return

        group_summaries: dict[str, dict] = {}
        for group_id in summary_push_groups:
            try:
                summary = await build_daily_summary(
                    store,
                    date_str=summary_date,
                    group_id=group_id,
                )
                protected_ids = (
                    [summary["most_roasted_id"]]
                    if summary.get("most_roasted_id") and summary.get("most_roasted_count", 0) >= 2
                    else []
                )
                await store.replace_group_protections(group_id, protected_ids, protect_date)
            except Exception as error:
                # 单群数据或云请求异常不能阻断其它群；保护写入失败时也不发送承诺了保护的日报。
                logger.warning(f"[每日总结] 汇总失败，已跳过该群: group={group_id} error={error}")
                continue
            group_summaries[group_id] = summary

        await store.prune_events(days_to_keep=7)
        await store.prune_history(days_to_keep=14)

        try:
            bot = get_bot()
        except ValueError:
            logger.warning("[每日总结] 无可用 Bot，跳过推送")
            return

        for group_id, summary in group_summaries.items():
            try:
                text = build_daily_summary_text(summary)
                await bot.send_group_msg(group_id=int(group_id), message=text)
            except Exception as error:
                logger.warning(f"[每日总结] 推送失败: group={group_id} error={error}")

        logger.info(f"[每日总结] 推送完成, 成功汇总 {len(group_summaries)}/{len(summary_push_groups)} 个群")
    except CloudStoreError as error:
        logger.warning(f"[每日总结] 云端账本暂时不可用，跳过本轮推送: {error}")
    except Exception as error:
        logger.error(f"[每日总结] 任务异常: {error}")
