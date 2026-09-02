from __future__ import annotations

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from ..helpers import is_superuser_user
from ..runtime import (
    get_daily_report_group_status,
    set_daily_report_group_enabled,
)


# ================================ 小猪日报群开关 ================================ #
# 控制单群日报推送开关（依赖 rollpig 主开关开启）。

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
    """检查是否具备管理当前群日报开关的权限。"""

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
        f"小猪日报状态：{'开启' if enabled else '关闭'}\n"
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
                await cmd_daily_report_switch.finish(_event_reply(event) + "只有超级用户可以查看其他群的小猪日报状态。")
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
            await cmd_daily_report_switch.finish(_event_reply(event) + f"小猪日报开启失败：{error}")
            return
        await cmd_daily_report_switch.finish(_event_reply(event) + f"已开启群 {target_group_id} 的小猪日报。\n{_format_status(target_group_id)}")
        return

    if action == "disable":
        try:
            await set_daily_report_group_enabled(target_group_id, False)
        except Exception as error:
            await cmd_daily_report_switch.finish(_event_reply(event) + f"小猪日报关闭失败：{error}")
            return
        await cmd_daily_report_switch.finish(_event_reply(event) + f"已关闭群 {target_group_id} 的小猪日报。\n{_format_status(target_group_id)}")
        return
