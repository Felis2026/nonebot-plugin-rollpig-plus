from __future__ import annotations

import asyncio
import datetime as dt
import random
import uuid
from collections import Counter
from contextlib import suppress

from nonebot import get_bots, get_driver
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .card_renderer import shutdown_card_renderer
from .catalog_renderer import shutdown_catalog_renderer
from .config import plugin_config
from .daily_report import (
    DailyReport,
    DailyUserReportProfile,
    ProtectionReportItem,
    build_daily_report,
)
from .daily_report_card_renderer import (
    render_daily_report_card,
    shutdown_daily_report_card_renderer,
)
from .pighub_service import PIGHUB_REFRESH_INTERVAL_HOURS, pighub_service
from .resource_manager import get_pig_by_id, pig_resource_manager, sync_rollpig_resources
from .yesterday_card_renderer import shutdown_yesterday_card_renderer
from .runtime import (
    ROLLPIG_TIMEZONE,
    is_daily_report_enabled,
    is_group_rollpig_enabled,
    rollpig_date_str,
)
from .store import store
from .store.base import RollpigStore
from .store.cloud import CloudDailyReportUnsupportedError, CloudStoreError
from .store.models import (
    DailyReportDeliveryClaim,
    DailyReportDeliveryClaimResult,
    DailyReportDeliveryTransitionResult,
)
from .store.local_json import LocalJsonStore

background_resource_sync_tasks: set[asyncio.Task[None]] = set()
background_maintenance_tasks: set[asyncio.Task[None]] = set()
DAILY_REPORT_INSTANCE_ID = uuid.uuid4().hex
DAILY_REPORT_RETRY_CUTOFF = dt.time(0, 10)
DAILY_REPORT_TRANSITION_RETRY_SECONDS = 30


# ================================ 运行时生命周期 ================================ #
@get_driver().on_shutdown
async def shutdown_rollpig_runtime() -> None:
    """释放渲染器缓存与数据库连接。"""

    # 退出前等待后台同步与维护任务结束
    for task_set in (background_resource_sync_tasks, background_maintenance_tasks):
        for task in list(task_set):
            task.cancel()
        for task in list(task_set):
            with suppress(asyncio.CancelledError):
                await task
        task_set.clear()
    from .reservation_delivery import shutdown_reservation_delivery_tasks

    await shutdown_reservation_delivery_tasks()
    await pighub_service.shutdown()
    await shutdown_yesterday_card_renderer()
    await shutdown_daily_report_card_renderer()
    await shutdown_card_renderer()
    await shutdown_catalog_renderer()
    await store.close()


# ================================ 资源同步任务 ================================ #
def get_resource_sync_interval_hours() -> int:
    """读取资源同步间隔配置（默认 24 小时）。"""

    try:
        return max(1, int(plugin_config.rollpig_resource_sync_interval_hours or 24))
    except Exception as error:
        logger.warning(f"rollpig_resource_sync_interval_hours 配置非法，已回退到 24 小时: {error}")
        return 24


async def run_background_resource_sync(source: str) -> None:
    """后台同步云端小猪资源包。"""

    try:
        message = await sync_rollpig_resources(force=False)
        logger.info(f"[小猪资源同步] {source}: {message}")
        # 延迟导入避免循环引用
        from .reservation_delivery import schedule_all_owned_deliveries
        from .reservation_flow import clear_resource_reservation_backoffs

        if clear_resource_reservation_backoffs():
            schedule_all_owned_deliveries(force=True)
    except Exception as error:
        logger.warning(f"[小猪资源同步] {source} 失败，继续使用当前资源: {error}")


def schedule_background_resource_sync(source: str) -> None:
    """注册后台资源同步任务；统一追踪 task，shutdown 时可取消并等待。"""

    task: asyncio.Task[None] = asyncio.create_task(run_background_resource_sync(source))
    background_resource_sync_tasks.add(task)
    task.add_done_callback(background_resource_sync_tasks.discard)


# ================================ 数据维护任务 ================================ #


async def run_data_maintenance(source: str) -> None:
    """独立清理过期历史和事件；单项失败不能阻止另一项继续执行。"""

    failures: list[str] = []
    for name, operation in (
        ("events", lambda: store.prune_events(days_to_keep=7)),
        ("history", lambda: store.prune_history(days_to_keep=14)),
    ):
        try:
            await operation()
        except Exception as error:
            failures.append(f"{name}={error}")
    if failures:
        logger.warning(f"[数据维护] {source} 部分失败: {'; '.join(failures)}")
        return
    logger.info(f"[数据维护] {source} 完成")


def schedule_background_maintenance(source: str) -> None:
    """把启动期清理放入后台并纳入 shutdown 收束，避免拖慢插件启动。"""

    task: asyncio.Task[None] = asyncio.create_task(run_data_maintenance(source))
    background_maintenance_tasks.add(task)
    task.add_done_callback(background_maintenance_tasks.discard)


@get_driver().on_startup
async def startup_data_maintenance() -> None:
    """启动后补做一次清理，覆盖 Bot 长期无群活动或停机后的积压。"""

    schedule_background_maintenance("startup")


@scheduler.scheduled_job(
    "cron",
    hour=3,
    minute=30,
    timezone=ROLLPIG_TIMEZONE,
    id="rollpig_data_maintenance",
    max_instances=1,
)
async def data_maintenance_job() -> None:
    """每天独立执行数据保留策略，不再依赖日报是否启用或当天是否活跃。"""

    await run_data_maintenance("scheduled")


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
    """启动后异步检查资源；关闭联网时仍需应用共享文案的本地清理语义。"""

    # 各同步器会自行判断总开关。这里始终安排一次轻量后台任务，
    # 让用户把共享文案 URL 设为空后，无需临时重新开启联网也能移除纯共享正文。
    schedule_background_resource_sync("startup")


@scheduler.scheduled_job("interval", hours=get_resource_sync_interval_hours(), id="rollpig_resource_sync", max_instances=1)
async def resource_sync_job():
    """低频检查云端资源包，减少多实例手动同步新猪素材的运维成本。"""

    if not plugin_config.rollpig_resource_sync_enabled:
        return
    await run_background_resource_sync("interval")


# ================================ 猪圈日报任务 ================================ #


def _group_ids_from_response(response: object) -> set[str] | None:
    """兼容 OneBot 直接列表与包裹 data 的群列表响应；格式非法返回 None。"""

    if isinstance(response, dict):
        response = response.get("data")
    if not isinstance(response, list):
        return None
    return {
        str(group_id)
        for item in response
        if isinstance(item, dict)
        for group_id in (item.get("group_id") or item.get("groupId"),)
        if group_id not in {None, ""}
    }


async def resolve_daily_report_bots(group_ids: list[str]) -> dict[str, Bot]:
    """为目标群匹配发送 Bot（多 Bot 同群取最小 self_id）。"""

    unresolved = {str(group_id) for group_id in group_ids if group_id}
    resolved: dict[str, Bot] = {}
    bots = sorted(get_bots().items(), key=lambda item: str(item[0]))
    if not bots:
        return resolved

    # 优先批量读取群列表
    for self_id, bot in bots:
        try:
            visible_group_ids = _group_ids_from_response(await bot.get_group_list())
        except Exception as error:
            logger.warning(f"[猪圈日报] Bot 群列表读取失败，准备逐群确认: bot={self_id} error={error}")
            continue
        if visible_group_ids is None:
            logger.warning(f"[猪圈日报] Bot 群列表格式无效，准备逐群确认: bot={self_id}")
            continue
        for group_id in sorted(unresolved & visible_group_ids):
            resolved[group_id] = bot
            unresolved.remove(group_id)
        if not unresolved:
            return resolved

    # 列表接口失败时逐群确认，避免向未知群发送
    for group_id in sorted(unresolved):
        for self_id, bot in bots:
            try:
                await bot.get_group_info(group_id=int(group_id), no_cache=False)
            except Exception:
                continue
            resolved[group_id] = bot
            break
        if group_id not in resolved:
            logger.error(f"[猪圈日报] 没有在线 Bot 能确认目标群，已跳过投递: group={group_id}")
    return resolved


# ================================ 日报群资料聚合 ================================ #


def _group_member_rows(response: object) -> list[dict]:
    """兼容 OneBot 直接列表与包裹 data 的群成员列表响应。"""

    if isinstance(response, dict):
        response = response.get("data")
    if not isinstance(response, list):
        return []
    return [dict(item) for item in response if isinstance(item, dict)]


async def load_daily_group_member_names(bot: Bot, group_id: str) -> dict[str, str]:
    """每群只读取一次成员列表；失败时由事件快照姓名和用户 ID 继续降级。"""

    try:
        response = await bot.get_group_member_list(group_id=int(group_id), no_cache=False)
    except Exception as error:
        logger.warning(f"[猪圈日报] 群成员昵称读取失败，继续使用事件快照: group={group_id} error={error}")
        return {}
    names: dict[str, str] = {}
    for item in _group_member_rows(response):
        user_id = str(item.get("user_id") or item.get("userId") or "").strip()
        display_name = str(item.get("card") or item.get("nickname") or "").strip()
        if user_id and display_name:
            names[user_id] = display_name
    return names


def _resolved_profile_pig(pig_id: str, expert_level: int) -> tuple[str, str]:
    """解析排行用小猪名称和实际生效立绘；缺图交给 renderer 使用猪章兜底。"""

    pig = get_pig_by_id(pig_id) if pig_id else None
    if not pig:
        return pig_id, ""
    appearance = pig_resource_manager.resolve_pig_appearance(pig, expert_level)
    image_path = appearance.image_path or appearance.base_image_path
    return str(pig.get("name") or pig_id), str(image_path.resolve()) if image_path else ""


async def build_daily_user_profiles(
    report_store: RollpigStore,
    *,
    report: DailyReport,
    group_id: str,
    date_str: str,
    cutoff_at: str,
    group_rolls: dict[str, str],
    member_names: dict[str, str],
) -> dict[str, DailyUserReportProfile]:
    """补齐昵称、EX 与图鉴资料；Local 与 Cloud 都使用整群批量快照。"""

    profiles = {
        user_id: DailyUserReportProfile(
            user_id=user_id,
            display_name=member_names.get(user_id, ""),
        )
        for user_id in report.participant_ids
    }
    try:
        snapshots = await report_store.get_daily_report_profiles(
            group_id=group_id,
            date_str=date_str,
            cutoff_at=cutoff_at,
            user_ids=report.participant_ids,
        )
    except CloudDailyReportUnsupportedError:
        # 仅当 Cloud 已支持日报领取、但尚未提供批量资料接口时才会走到这里。
        # 不能逐用户补查，否则千人群会把一次日报放大成数百个请求；真正的
        # 旧 Cloud 会在领取阶段停止日报，避免多个实例各自降级后重复发送。
        logger.info(
            f"[猪圈日报] 当前 Cloud 不支持批量排行资料，已隐藏 EX 与图鉴榜: "
            f"group={group_id}"
        )
        return profiles
    except Exception as error:
        if not isinstance(report_store, LocalJsonStore):
            raise
        logger.warning(f"[猪圈日报] 本地排行资料读取失败，已隐藏相关排行: group={group_id} error={error}")
        return profiles

    snapshot_by_user = {item.user_id: item for item in snapshots}
    for user_id in report.participant_ids:
        snapshot = snapshot_by_user.get(user_id)
        if snapshot is None:
            continue

        daily_pig_id = str(group_rolls.get(user_id) or "")
        daily_matches = bool(
            daily_pig_id and snapshot.daily_pig_id == daily_pig_id
        )
        daily_level = snapshot.daily_ex_level if daily_matches else None
        daily_name, daily_image = (
            _resolved_profile_pig(daily_pig_id, daily_level or 0)
            if daily_pig_id
            else ("", "")
        )
        recent_name, recent_image = (
            _resolved_profile_pig(
                snapshot.recent_pig_id,
                snapshot.recent_ex_level or 0,
            )
            if snapshot.recent_pig_id
            else ("", "")
        )
        profiles[user_id] = DailyUserReportProfile(
            user_id=user_id,
            display_name=member_names.get(user_id, ""),
            daily_pig_id=daily_pig_id,
            daily_pig_name=daily_name,
            daily_ex_level=daily_level,
            daily_image_name=daily_image,
            daily_achieved_at=(snapshot.daily_achieved_at if daily_matches else ""),
            catalog_count=snapshot.catalog_count,
            catalog_achieved_at=snapshot.catalog_achieved_at,
            recent_pig_id=snapshot.recent_pig_id,
            recent_pig_name=recent_name,
            recent_image_name=recent_image,
        )
    return profiles


def select_daily_protected_user_ids(report: DailyReport) -> list[str]:
    """沿用现有规则：被成功烤至少两次且次数最高的一人获得次日保护。"""

    roasted_counter: Counter[str] = Counter()
    for event in report.events:
        if (
            event.event_type == "success"
            and event.target_id
            and event.target_id != event.attacker_id
        ):
            roasted_counter[event.target_id] += 1
    if not roasted_counter:
        return []
    user_id, count = roasted_counter.most_common(1)[0]
    return [user_id] if count >= 2 else []


async def build_group_daily_report(
    report_store: RollpigStore,
    bot: Bot,
    *,
    date_str: str,
    protect_date: str,
    group_id: str,
    cutoff_at: str,
) -> DailyReport:
    """读取固定日期快照、写入次日保护，再返回可安全承诺保护结果的日报。"""

    group_rolls, event_query = await asyncio.gather(
        report_store.get_group_rolls(
            group_id,
            date_str,
            cutoff_at=cutoff_at,
        ),
        report_store.query_daily_events(
            date_str=date_str,
            group_id=group_id,
            cutoff_at=cutoff_at,
        ),
    )
    if not event_query.available:
        raise CloudStoreError("日报事件记录暂时不可用")
    try:
        active_user_ids = await report_store.get_group_active_user_ids(
            group_id,
            date_str,
            cutoff_at=cutoff_at,
        )
    except Exception as error:
        logger.warning(f"[猪圈日报] 活跃用户读取失败，排行仅使用抽猪和互动用户: group={group_id} error={error}")
        active_user_ids = set()

    bot_user_ids = tuple(str(self_id) for self_id in get_bots())
    preliminary = build_daily_report(
        date_str=date_str,
        group_id=group_id,
        group_rolls=group_rolls,
        raw_events=event_query.items,
        active_user_ids=active_user_ids,
        bot_user_ids=bot_user_ids,
        cutoff_at=cutoff_at,
    )
    member_names = await load_daily_group_member_names(bot, group_id)
    profiles = await build_daily_user_profiles(
        report_store,
        report=preliminary,
        group_id=group_id,
        date_str=date_str,
        cutoff_at=cutoff_at,
        group_rolls=group_rolls,
        member_names=member_names,
    )
    report = build_daily_report(
        date_str=date_str,
        group_id=group_id,
        group_rolls=group_rolls,
        raw_events=event_query.items,
        active_user_ids=active_user_ids,
        bot_user_ids=bot_user_ids,
        user_profiles=profiles,
        cutoff_at=cutoff_at,
    )
    if not report.has_activity:
        return report
    protected_ids = select_daily_protected_user_ids(report)
    await report_store.replace_group_protections(
        group_id,
        protected_ids,
        protect_date,
    )

    expires_at = dt.datetime.combine(
        dt.date.fromisoformat(protect_date),
        dt.time(23, 59),
        tzinfo=ROLLPIG_TIMEZONE,
    ).isoformat()
    protections = tuple(
        ProtectionReportItem(
            user_id=user_id,
            display_name=profiles.get(
                user_id,
                DailyUserReportProfile(user_id=user_id),
            ).display_name
            or report.display_names.get(user_id, ""),
            expires_at=expires_at,
        )
        for user_id in protected_ids
    )
    return build_daily_report(
        date_str=date_str,
        group_id=group_id,
        group_rolls=group_rolls,
        raw_events=event_query.items,
        active_user_ids=active_user_ids,
        bot_user_ids=bot_user_ids,
        user_profiles=profiles,
        protections=protections,
        cutoff_at=cutoff_at,
    )


def _daily_report_message_id(response: object) -> str:
    """兼容 OneBot 直接字典与包裹 data 的发送响应，只记录可确认的消息 ID。"""

    if not isinstance(response, dict):
        return ""
    payload = response.get("data") if isinstance(response.get("data"), dict) else response
    return str(payload.get("message_id") or payload.get("messageId") or "")


def _normalize_daily_report_claim_result(
    result: object,
) -> DailyReportDeliveryClaimResult:
    """兼容尚未升级的自定义 Store，使日报升级不会直接打断宿主扩展。"""

    if isinstance(result, DailyReportDeliveryClaimResult):
        return result
    if isinstance(result, (tuple, list)):
        return DailyReportDeliveryClaimResult(
            claims=tuple(
                item for item in result if isinstance(item, DailyReportDeliveryClaim)
            )
        )
    return DailyReportDeliveryClaimResult()


def _normalize_daily_report_transition_result(
    result: object,
) -> DailyReportDeliveryTransitionResult:
    """将旧 Store 的 bool 返回值归一成包含状态与重试时间的新结果。"""

    if isinstance(result, DailyReportDeliveryTransitionResult):
        return result
    return DailyReportDeliveryTransitionResult(ok=bool(result))


def _parse_daily_report_retry_at(raw_value: str) -> dt.datetime | None:
    """Cloud 数据库存储 UTC naive；客户端明确按 UTC 解释，避免宿主时区漂移。"""

    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        parsed = dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"[猪圈日报] Cloud 返回了非法重试时间，已停止本次重领: value={normalized}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(ROLLPIG_TIMEZONE)


def _daily_report_retry_delay(
    date_str: str,
    retry_times: list[str],
    *,
    now: dt.datetime | None = None,
) -> float | None:
    """选择服务端允许的最早重领时间，并硬性限制在次日 00:10 以前。"""

    deadline = dt.datetime.combine(
        dt.date.fromisoformat(date_str) + dt.timedelta(days=1),
        DAILY_REPORT_RETRY_CUTOFF,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    current_time = now or dt.datetime.now(ROLLPIG_TIMEZONE)
    if current_time >= deadline:
        return None
    parsed_times = [
        parsed
        for raw_value in retry_times
        if (parsed := _parse_daily_report_retry_at(raw_value)) is not None
        and parsed < deadline
    ]
    if not parsed_times:
        return None
    delay = (min(parsed_times) - current_time).total_seconds()
    # 到点后仍未领取通常只是客户端与 Cloud 存在亚秒级时钟差；保留一秒下限，
    # 避免异常响应造成定时任务原地空转。
    return max(1.0, delay)


def _daily_report_deadline_reached(date_str: str) -> bool:
    """判断指定日报日期是否已越过次日投递硬截止点。"""

    deadline = dt.datetime.combine(
        dt.date.fromisoformat(date_str) + dt.timedelta(days=1),
        DAILY_REPORT_RETRY_CUTOFF,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    return dt.datetime.now(ROLLPIG_TIMEZONE) >= deadline


def _daily_report_transition_retry_at() -> str:
    """状态迁移响应丢失时短暂再查 Cloud，由服务端最终状态决定能否重领。"""

    return (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=DAILY_REPORT_TRANSITION_RETRY_SECONDS)
    ).isoformat()


async def _transition_daily_report_safely(
    claim: DailyReportDeliveryClaim,
    action: str,
    *,
    message_id: str = "",
    error: str = "",
) -> DailyReportDeliveryTransitionResult:
    """收束失败后的 Cloud 状态；二次异常只能记录，不能阻断其他群。"""

    try:
        return _normalize_daily_report_transition_result(
            await store.transition_daily_report_delivery(
                claim,
                action,
                message_id=message_id,
                error=error,
            )
        )
    except Exception as transition_error:
        logger.error(
            f"[猪圈日报] 投递状态更新失败: group={claim.group_id} "
            f"action={action} error={transition_error}"
        )
        return DailyReportDeliveryTransitionResult(ok=False)


async def _deliver_daily_report_claim(
    claim: DailyReportDeliveryClaim,
    *,
    delivery_bots: dict[str, Bot],
    protect_date: str,
    cutoff_time: str,
) -> tuple[bool, bool, str]:
    """处理一份已领取日报，返回是否生成、是否发送及下一次安全重领时间。"""

    group_id = claim.group_id
    bot = delivery_bots.get(group_id)
    if bot is None or str(bot.self_id) != claim.delivery_bot_id:
        transition = await _transition_daily_report_safely(
            claim,
            "release",
            error="delivery_bot_unavailable",
        )
        retry_at = transition.next_attempt_at
        if not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        return False, False, retry_at

    # Claim 与实际处理之间可能隔着前序群渲染或 Cloud 退避；必须在任何日报
    # 聚合及保护名单写入前重读开关，避免管理员关闭后仍产生群内副作用。
    if not is_group_rollpig_enabled(group_id) or not is_daily_report_enabled(group_id):
        transition = await _transition_daily_report_safely(
            claim,
            "skip",
            error="daily_report_disabled_before_delivery",
        )
        retry_at = transition.next_attempt_at
        if not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        logger.info(f"[猪圈日报] 群开关已关闭，跳过已领取日报: group={group_id}")
        return False, False, retry_at

    sending_started = False
    report_built = False
    try:
        report = await build_group_daily_report(
            store,
            bot,
            date_str=claim.date_str,
            protect_date=protect_date,
            group_id=group_id,
            cutoff_at=claim.cutoff_at,
        )
        if not report.has_activity:
            if not await store.transition_daily_report_delivery(claim, "skip"):
                raise CloudStoreError("日报空活动状态确认失败")
            logger.info(f"[猪圈日报] 群内没有可展示活动，已跳过: group={group_id}")
            return False, False, ""

        report_built = True
        rendered = await render_daily_report_card(
            report,
            cutoff_time=cutoff_time,
        )
        if not await store.transition_daily_report_delivery(claim, "sending"):
            raise CloudStoreError("日报发送意图未获 Cloud 确认")
        sending_started = True
        response = await bot.send_group_msg(
            group_id=int(group_id),
            message=MessageSegment.image(rendered.data),
        )
        if not await store.transition_daily_report_delivery(
            claim,
            "sent",
            message_id=_daily_report_message_id(response),
        ):
            raise CloudStoreError("日报发送完成状态未获 Cloud 确认")
        return True, True, ""
    except Exception as error:
        # sending 前可以安全释放并按 Cloud 退避重领；进入 sending 后消息结果可能
        # 已不可确认，只能冻结为 uncertain，绝不能自动重复发送。
        transition = await _transition_daily_report_safely(
            claim,
            "uncertain" if sending_started else "release",
            error=str(error),
        )
        retry_at = transition.next_attempt_at if not sending_started else ""
        if not sending_started and not transition and not retry_at:
            retry_at = _daily_report_transition_retry_at()
        logger.warning(
            f"[猪圈日报] 推送失败: group={group_id} attempt={claim.attempt_count} "
            f"fallback={'uncertain' if sending_started else 'retry'} error={error}"
        )
        return report_built, False, retry_at


@scheduler.scheduled_job("cron", hour=23, minute=45, timezone=ROLLPIG_TIMEZONE, id="rollpig_daily_report")
async def daily_report_job():
    """每晚 23:45 起推送日报，失败重试最晚持续到次日 00:10。"""

    # 日期与截止点必须在随机延迟前固定；所有后端读取都使用同一截止点，
    # 23:45 后的新抽取和互动只能进入下一日实时功能，不能混入本期日报。
    report_date = rollpig_date_str()
    protect_date = (dt.date.fromisoformat(report_date) + dt.timedelta(days=1)).isoformat()
    cutoff = dt.datetime.combine(
        dt.date.fromisoformat(report_date),
        dt.time(23, 45),
        tzinfo=ROLLPIG_TIMEZONE,
    )
    cutoff_at = cutoff.isoformat()
    cutoff_time = cutoff.strftime("%H:%M")
    delay = random.randint(0, 600)  # 0~10 分钟随机延迟
    logger.info(f"[猪圈日报] 定时触发，随机延迟 {delay} 秒后推送")
    await asyncio.sleep(delay)
    try:
        active_groups = await store.get_active_group_ids(report_date)
        if not active_groups:
            logger.info("[猪圈日报] 今日无活跃群，跳过推送")
            return

        # ================================ 控制台开关过滤 ================================ #
        # 如果宿主项目接入了群开关，这里必须在定时任务层同步收口：
        # 未启用的群既不推日报，也不写次日保护名单，保证“关闭就是彻底关闭”。
        enabled_active_groups = [
            group_id for group_id in sorted(active_groups)
            if is_group_rollpig_enabled(group_id)
        ]
        if not enabled_active_groups:
            logger.info("[猪圈日报] 今日没有启用 rollpig 的活跃群，跳过推送")
            return

        # ================================ 日报推送开关过滤 ================================ #
        # 日报支持“默认关闭 + 分群开启”。必须先过滤出真正开启日报的群，
        # 再计算日报与次日保护名单，避免关闭日报的群被定时任务产生副作用。
        report_push_groups = [
            group_id for group_id in enabled_active_groups
            if is_daily_report_enabled(group_id)
        ]
        if not report_push_groups:
            logger.info("[猪圈日报] 没有群开启日报推送")
            return

        delivery_bots = await resolve_daily_report_bots(report_push_groups)
        if not delivery_bots:
            logger.warning("[猪圈日报] 无可用 Bot，跳过推送")
            return

        report_groups: set[str] = set()
        sent_groups: set[str] = set()
        claimed_attempts = 0

        # ================================ Cloud 安全重领循环 ================================ #
        # Claim 响应同时携带“其他实例租约何时到期”；release 响应携带当前群的
        # 服务端退避时间。这里只按这些明确时间唤醒，不做固定频率空轮询。
        while delivery_bots:
            claim_result = _normalize_daily_report_claim_result(
                await store.claim_daily_report_deliveries(
                    instance_id=DAILY_REPORT_INSTANCE_ID,
                    delivery_bots={
                        group_id: str(bot.self_id)
                        for group_id, bot in delivery_bots.items()
                    },
                    date_str=report_date,
                    cutoff_at=cutoff_at,
                )
            )
            retry_times = [claim_result.next_claim_at]
            claimed_attempts += len(claim_result.claims)
            deadline_reached = False
            for index, claim in enumerate(claim_result.claims):
                if _daily_report_deadline_reached(report_date):
                    # 同一批 claim 会顺序渲染和发送；一旦到达硬截止点，当前及
                    # 后续租约都必须主动释放，不能让慢群拖着整批越界投递。
                    for remaining_claim in claim_result.claims[index:]:
                        await _transition_daily_report_safely(
                            remaining_claim,
                            "release",
                            error="delivery_deadline_passed",
                        )
                    logger.info(
                        f"[猪圈日报] 已到次日投递截止时间，释放剩余 "
                        f"{len(claim_result.claims) - index} 个领取任务"
                    )
                    deadline_reached = True
                    break
                report_built, sent, retry_at = await _deliver_daily_report_claim(
                    claim,
                    delivery_bots=delivery_bots,
                    protect_date=protect_date,
                    cutoff_time=cutoff_time,
                )
                if report_built:
                    report_groups.add(claim.group_id)
                if sent:
                    sent_groups.add(claim.group_id)
                if retry_at:
                    retry_times.append(retry_at)

            if deadline_reached:
                break
            retry_delay = _daily_report_retry_delay(report_date, retry_times)
            if retry_delay is None:
                break
            logger.info(f"[猪圈日报] 等待 {retry_delay:.1f} 秒后重新领取未完成日报")
            await asyncio.sleep(retry_delay)

            # 等待期间 Bot 可能上下线或改变群可见性；重领前重新确认路由，不能沿用
            # 旧 Bot 对象把恢复任务发往已经断开的连接。
            delivery_bots = await resolve_daily_report_bots(report_push_groups)

        if claimed_attempts == 0:
            logger.info("[猪圈日报] 当前群日报已由其他实例完成，或没有可安全领取的任务")
            return
        logger.info(
            f"[猪圈日报] 推送完成, 成功发送 {len(sent_groups)}/{len(report_groups)}，"
            f"累计领取 {claimed_attempts} 次，候选群 {len(report_push_groups)} 个"
        )
    except CloudStoreError as error:
        logger.warning(f"[猪圈日报] 云端账本暂时不可用，跳过本轮推送: {error}")
    except Exception as error:
        logger.error(f"[猪圈日报] 任务异常: {error}")
