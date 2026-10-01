from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from ..data_manager import LocalStoreUnavailableError
from ..helpers import command_has_no_argument, guard_group_enabled, guard_store_errors, is_superuser_user
from ..jobs import latest_daily_report_query_date, render_latest_daily_report_query_card
from ..runtime import (
    get_daily_report_group_status,
    set_daily_report_group_enabled,
)
from ..store.cloud import CloudStoreError


# ================================ 猪圈日报主动查询 ================================ #


cmd_daily_report_card = on_command("猪圈日报", rule=command_has_no_argument, block=True)


@cmd_daily_report_card.handle()
@guard_group_enabled(cmd_daily_report_card)
@guard_store_errors(cmd_daily_report_card)
async def _(bot: Bot, event: Event):
    """查本群最近一期封版卡，不触发自动投递或保护结算。"""

    reply = _event_reply(event)
    if not isinstance(event, GroupMessageEvent):
        await cmd_daily_report_card.finish(reply + "请在群里发送「猪圈日报」。")
        return

    report_date = latest_daily_report_query_date()
    if report_date is None:
        await cmd_daily_report_card.finish(reply + "本期日报正在出刊，00:10 后再来翻报纸。")
        return

    try:
        image, protection_unconfirmed = await render_latest_daily_report_query_card(
            bot, str(event.group_id), report_date,
        )
    except (CloudStoreError, LocalStoreUnavailableError):
        raise
    except Exception as error:
        logger.exception(f"猪圈日报查卡失败: group={event.group_id} date={report_date} error={error}")
        await cmd_daily_report_card.finish(reply + "猪圈日报暂时查不到，请稍后再试。")
        return

    if image is None:
        await cmd_daily_report_card.finish(reply + "这期猪圈休刊，没攒出一张日报。")
        return
    message = reply + MessageSegment.image(image)
    if protection_unconfirmed:
        message += MessageSegment.text("次日保护结算暂未确认。")
    await cmd_daily_report_card.finish(message)


# ================================ 小猪日报推送开关 ================================ #
# 控制单群日报定时推送，不影响主动查询。

cmd_daily_report_switch = on_command(
    "小猪日报",
    aliases={"每日总结设置", "rollpig日报"},
    force_whitespace=True,
    block=True,
)

ENABLE_WORDS = {"开启", "打开", "启用", "开", "on", "enable", "true"}
DISABLE_WORDS = {"关闭", "停用", "关", "off", "disable", "false"}
STATUS_WORDS = {"状态", "查看", "查询", "status", "info"}


def _event_reply(event: Event) -> MessageSegment:
    message_id = getattr(event, "message_id", None)
    return MessageSegment.reply(message_id) if message_id is not None else MessageSegment.text("")


def _is_group_manager(event: Event) -> bool:
    """检查是否具备管理当前群日报推送开关的权限。"""

    if is_superuser_user(str(event.user_id)):
        return True
    if not isinstance(event, GroupMessageEvent):
        return False
    return getattr(event.sender, "role", "") in {"admin", "owner"}


def _parse_action_and_group_id(raw_text: str, event: Event) -> tuple[str, str]:
    """解析开关动作与目标群号。"""

    tokens = raw_text.split()
    action = "status"
    target_group_id = ""

    for token in tokens:
        normalized = token.lower()
        if normalized in ENABLE_WORDS:
            action = "enable"
        elif normalized in DISABLE_WORDS:
            action = "disable"
        elif normalized in STATUS_WORDS:
            action = "status"
        elif token.isdigit():
            target_group_id = token

    if not target_group_id and isinstance(event, GroupMessageEvent):
        target_group_id = str(event.group_id)
    return action, target_group_id


def _can_control_target_group(event: Event, target_group_id: str) -> bool:
    """检查对目标群的管理权限。"""

    if is_superuser_user(str(event.user_id)):
        return True
    if not isinstance(event, GroupMessageEvent):
        return False
    return str(event.group_id) == target_group_id and _is_group_manager(event)


def _format_status(group_id: str) -> str:
    """格式化单群日报状态文本。"""

    enabled, source = get_daily_report_group_status(group_id)
    return (
        f"小猪日报推送状态：{'开启' if enabled else '关闭'}\n"
        f"群号：{group_id}\n"
        f"来源：{source}"
    )


@cmd_daily_report_switch.handle()
async def _(event: Event, args: Message = CommandArg()):
    raw_text = args.extract_plain_text().strip()
    action, target_group_id = _parse_action_and_group_id(raw_text, event)

    if not target_group_id:
        await cmd_daily_report_switch.finish(
            _event_reply(event)
            + "请在群内使用，或由超级用户指定群号：小猪日报 开启 123456"
        )
        return

    if action == "status":
        if not isinstance(event, GroupMessageEvent) or str(event.group_id) != target_group_id:
            if not is_superuser_user(str(event.user_id)):
                await cmd_daily_report_switch.finish(_event_reply(event) + "只有超级用户可以查看其他群的日报推送状态。")
                return
        await cmd_daily_report_switch.finish(_event_reply(event) + _format_status(target_group_id))
        return

    if not _can_control_target_group(event, target_group_id):
        await cmd_daily_report_switch.finish(
            _event_reply(event)
            + "只有本群群主/管理员可以控制本群；控制其他群需要超级用户权限。"
        )
        return

    if action == "enable":
        try:
            await set_daily_report_group_enabled(target_group_id, True)
        except Exception as error:
            await cmd_daily_report_switch.finish(_event_reply(event) + f"日报推送开启失败：{error}")
            return
        await cmd_daily_report_switch.finish(_event_reply(event) + f"已开启群 {target_group_id} 的猪圈日报推送。\n{_format_status(target_group_id)}")
        return

    if action == "disable":
        try:
            await set_daily_report_group_enabled(target_group_id, False)
        except Exception as error:
            await cmd_daily_report_switch.finish(_event_reply(event) + f"日报推送关闭失败：{error}")
            return
        await cmd_daily_report_switch.finish(_event_reply(event) + f"已关闭群 {target_group_id} 的猪圈日报推送。\n{_format_status(target_group_id)}")
        return
