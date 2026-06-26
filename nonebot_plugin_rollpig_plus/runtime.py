from __future__ import annotations

import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nonebot import get_plugin_config
from nonebot.log import logger

from .config import Config


# ================================ 日期边界 ================================ #
# RollPig 的“今天/明天/近 7 天”都按中国用户习惯使用 Asia/Shanghai。
# 不直接调用 date.today()，避免服务器部署在 UTC 或其他时区时跨日边界漂移。
try:
    ROLLPIG_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows 精简环境可能缺少 IANA tzdata；中国业务日期当前无夏令时，
    # 固定 UTC+8 兜底可以避免插件在导入阶段直接失败。
    ROLLPIG_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")


def rollpig_now() -> datetime.datetime:
    """返回 RollPig 业务时区下的当前时间。"""
    return datetime.datetime.now(ROLLPIG_TIMEZONE)


def rollpig_today() -> datetime.date:
    """返回 RollPig 业务时区下的今天日期。"""
    return rollpig_now().date()


def rollpig_date_str(offset_days: int = 0) -> str:
    """返回 RollPig 业务日期字符串；offset_days 用于昨日/明日等相对日期。"""
    return (rollpig_today() + datetime.timedelta(days=offset_days)).isoformat()


# ================================ 外部群开关适配 ================================ #
# rollpig 作为通用插件，不直接依赖 nekobot_v2 的 admin_console。
# 这里仅暴露“可选群启用检查器”接口，宿主项目若有控制台/控制面，
# 可以把自己的群开关逻辑挂进来；没有则保持默认放行。
_group_enable_checker: Optional[Callable[[str], bool]] = None
_daily_summary_checker: Optional[Callable[[str], bool]] = None


def resolve_roast_cooldown_seconds() -> int:
    """解析普通烤群友 CD（秒），支持通过配置覆盖。"""
    plugin_config = get_plugin_config(Config)
    raw_hours = getattr(plugin_config, "rollpig_roast_cooldown_hours", 8.0)
    try:
        hours = float(raw_hours)
    except (TypeError, ValueError):
        logger.warning(f"rollpig_roast_cooldown_hours 配置非法: {raw_hours}，已回退到 8 小时")
        hours = 8.0

    if hours <= 0:
        logger.warning(f"rollpig_roast_cooldown_hours 必须 > 0，当前值: {hours}，已回退到 8 小时")
        hours = 8.0

    return max(1, int(hours * 3600))


def resolve_roast_charge_max() -> int:
    """解析普通烤群友充能上限；第一版限制在 1~6，避免群内刷屏。"""
    plugin_config = get_plugin_config(Config)
    raw_max = getattr(plugin_config, "rollpig_roast_charge_max", 2)
    try:
        max_charges = int(raw_max)
    except (TypeError, ValueError):
        logger.warning(f"rollpig_roast_charge_max 配置非法: {raw_max}，已回退到 2")
        max_charges = 2
    if max_charges <= 0:
        logger.warning(f"rollpig_roast_charge_max 必须 > 0，当前值: {max_charges}，已回退到 2")
        max_charges = 2
    return max(1, min(6, max_charges))


def set_group_enable_checker(checker: Optional[Callable[[str], bool]]) -> None:
    """注册外部群启用检查器；传入 None 时恢复为“未接控制台，默认开启”模式。"""
    global _group_enable_checker
    _group_enable_checker = checker


def set_daily_summary_checker(checker: Optional[Callable[[str], bool]]) -> None:
    """注册外部日报开关检查器；传入 None 时恢复为“未接控制台，默认开启”模式。"""
    global _daily_summary_checker
    _daily_summary_checker = checker


def _check_optional_group_switch(
    checker: Optional[Callable[[str], bool]],
    group_id: str,
    *,
    switch_name: str,
) -> bool:
    """统一处理可选群开关：未接控制系统默认开启，检查异常时按关闭处理。"""
    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        return True

    if checker is None:
        return True

    try:
        return bool(checker(normalized_group_id))
    except Exception as error:
        # 一旦宿主项目主动挂了外部检查器，就说明它希望群开关生效。
        # 这里若检查器本身异常，宁可按“关闭”处理，也不要意外把所有群放开。
        logger.warning(f"rollpig {switch_name} 检查失败: group={normalized_group_id} error={error}")
        return False


def is_group_rollpig_enabled(group_id: str) -> bool:
    """判断当前群是否启用 rollpig；未接入外部控制系统时默认返回 True。"""
    return _check_optional_group_switch(_group_enable_checker, group_id, switch_name="群启用")


def is_daily_summary_enabled(group_id: str) -> bool:
    """判断当前群是否启用日报推送；未接入外部控制系统时默认返回 True。"""
    return _check_optional_group_switch(_daily_summary_checker, group_id, switch_name="日报开关")
