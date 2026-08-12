from __future__ import annotations

import asyncio
import datetime
import random
from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.log import logger

from .runtime import resolve_roast_charge_max
from .store import store
from .store.models import GroupRoastRefillRequest
from .texts import (
    ROAST_REFILL_CREATED_TEXTS,
    ROAST_REFILL_EXISTING_TEXTS,
    ROAST_REFILL_REACTION_HINT_TEXTS,
    ROAST_REFILL_SUCCESS_TEXTS,
    ROAST_REFILL_UNSUPPORTED_TEXTS,
)


ROAST_REFILL_EMOJI_ID = "424"
ROAST_REFILL_EMOJI_TYPE = 1
ROAST_REFILL_FETCH_PAGE_SIZE = 100
ROAST_REFILL_FETCH_MAX_PAGES = 20
ROAST_REFILL_IMAGE_PATH = Path(__file__).parent / "resource" / "refill.png"


class RoastRefillReactionError(RuntimeError):
    def __init__(self, message: str, *, message_missing: bool = False):
        super().__init__(message)
        self.message_missing = message_missing


def extract_message_id(send_result: Any) -> str:
    """兼容 OneBot 直接整数和标准 `{message_id}` 返回。"""

    if isinstance(send_result, dict):
        value = send_result.get("message_id")
    else:
        value = send_result
    return str(value) if value not in {None, ""} else ""


async def add_refill_reaction(bot: Bot, message_id: str) -> bool:
    """给投票消息贴 QQ「续标识」；失败只影响引导，不直接判整场失败。"""

    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=message_id,
            emoji_id=ROAST_REFILL_EMOJI_ID,
            set=True,
        )
        return True
    except Exception as error:
        logger.warning(f"rollpig 烤箱补货自动添加续标识失败: message={message_id} error={error}")
        return False


def _reaction_payload(value: Any) -> dict:
    if not isinstance(value, dict):
        raise RoastRefillReactionError("fetch_emoji_like 返回格式无效")
    nested = value.get("data")
    return nested if isinstance(nested, dict) else value


def _is_truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


async def fetch_refill_reactors(bot: Bot, message_id: str) -> set[str]:
    """分页读取续标识用户列表；QQ raw count 从不作为业务真值。"""

    user_ids: set[str] = set()
    cookie = ""
    seen_cookies: set[str] = set()
    for _ in range(ROAST_REFILL_FETCH_MAX_PAGES):
        try:
            response = await bot.call_api(
                "fetch_emoji_like",
                message_id=message_id,
                emojiId=ROAST_REFILL_EMOJI_ID,
                emojiType=ROAST_REFILL_EMOJI_TYPE,
                count=ROAST_REFILL_FETCH_PAGE_SIZE,
                cookie=cookie,
            )
        except Exception as error:
            lowered = str(error).lower()
            missing = any(token in lowered for token in ("消息不存在", "msg not found", "message not found", "404"))
            raise RoastRefillReactionError(str(error), message_missing=missing) from error

        payload = _reaction_payload(response)
        result_code = payload.get("result")
        if result_code not in {None, 0, "0"}:
            error_message = str(payload.get("errMsg") or payload.get("message") or result_code)
            missing = any(token in error_message.lower() for token in ("消息不存在", "msg not found", "message not found"))
            raise RoastRefillReactionError(error_message, message_missing=missing)
        likes = payload.get("emojiLikesList", [])
        if not isinstance(likes, list):
            raise RoastRefillReactionError("fetch_emoji_like 缺少 emojiLikesList")
        for item in likes:
            if not isinstance(item, dict):
                continue
            user_id = item.get("tinyId") or item.get("tiny_id") or item.get("user_id")
            if user_id:
                user_ids.add(str(user_id))

        if _is_truthy(payload.get("isLastPage")):
            break
        next_cookie = str(payload.get("cookie") or "")
        if not next_cookie or next_cookie == cookie or next_cookie in seen_cookies:
            break
        seen_cookies.add(next_cookie)
        cookie = next_cookie
    return user_ids


async def fetch_refill_group_members(bot: Bot, group_id: str) -> set[str]:
    """读取当前群成员，防止已退群用户遗留的续标识继续计票。"""

    try:
        response = await bot.call_api("get_group_member_list", group_id=int(group_id))
    except Exception as error:
        raise RoastRefillReactionError(f"群成员名单读取失败: {error}") from error

    if isinstance(response, dict) and isinstance(response.get("data"), list):
        members = response["data"]
    else:
        members = response
    if not isinstance(members, list):
        raise RoastRefillReactionError("get_group_member_list 返回格式无效")
    return {
        str(user_id)
        for item in members
        if isinstance(item, dict)
        for user_id in (item.get("user_id") or item.get("userId") or item.get("uin"),)
        if user_id
    }


# ================================ 文案与票数 ================================ #

def format_refill_created(request: GroupRoastRefillRequest, max_charges: int) -> str:
    return random.choice(ROAST_REFILL_CREATED_TEXTS).format(
        initiator=request.initiator_name or request.initiator_id,
        active_count=request.active_count_snapshot,
        required_votes=request.required_votes,
        success_count=request.success_count_before,
        max_charges=max_charges,
    )


def build_refill_created_message(request: GroupRoastRefillRequest, max_charges: int) -> Message:
    """按图片在前、申请文案在后的顺序构造消息；图片内嵌以兼容跨容器 OneBot。"""

    return MessageSegment.image(ROAST_REFILL_IMAGE_PATH.read_bytes()) + MessageSegment.text(
        "\n" + format_refill_created(request, max_charges)
    )


def _remaining_minutes(request: GroupRoastRefillRequest) -> int:
    try:
        expires_at = datetime.datetime.fromisoformat(request.expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        remaining = max(0.0, (expires_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    except ValueError:
        return 0
    return max(1, int((remaining + 59) // 60)) if remaining > 0 else 0


async def get_valid_refill_voters(bot: Bot, request: GroupRoastRefillRequest) -> tuple[set[str], set[str]]:
    raw_voters, group_member_ids, active_user_ids = await asyncio.gather(
        fetch_refill_reactors(bot, request.message_id),
        fetch_refill_group_members(bot, request.group_id),
        store.get_group_active_user_ids(request.group_id, request.date_str),
    )
    valid_voters = (raw_voters & group_member_ids & active_user_ids) - {str(bot.self_id)}
    return raw_voters, valid_voters


async def format_existing_refill(bot: Bot, request: GroupRoastRefillRequest) -> str:
    _, valid_voters = await get_valid_refill_voters(bot, request)
    current = len(valid_voters)
    return random.choice(ROAST_REFILL_EXISTING_TEXTS).format(
        current=current,
        required=request.required_votes,
        remaining_votes=max(0, request.required_votes - current),
        minutes=_remaining_minutes(request),
    )


def format_refill_success(
    request: GroupRoastRefillRequest,
    votes: int,
    benefited: int,
    max_charges: int,
) -> str:
    return random.choice(ROAST_REFILL_SUCCESS_TEXTS).format(
        votes=votes,
        benefited=benefited,
        success_count=request.success_count_before + 1,
        max_charges=max_charges,
    )


def pick_refill_unsupported_text() -> str:
    return random.choice(ROAST_REFILL_UNSUPPORTED_TEXTS)


def pick_refill_reaction_hint() -> str:
    return random.choice(ROAST_REFILL_REACTION_HINT_TEXTS)


# ================================ Notice 结算 ================================ #

def is_refill_notice(event: Any) -> bool:
    if getattr(event, "notice_type", "") != "group_msg_emoji_like":
        return False
    likes = getattr(event, "likes", None)
    if not isinstance(likes, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("emoji_id") or "") == ROAST_REFILL_EMOJI_ID
        for item in likes
    )


async def process_refill_notice(bot: Bot, event: Any) -> None:
    """Notice 只唤醒校验；最终名单、资格与一次性状态迁移都重新从后端确认。"""

    if not is_refill_notice(event):
        return
    group_id = str(getattr(event, "group_id", "") or "")
    message_id = str(getattr(event, "message_id", "") or "")
    if not group_id or not message_id:
        return

    request = await store.get_group_roast_refill(group_id)
    if (
        request is None
        or request.message_id != message_id
        or request.delivery_bot_id != str(bot.self_id)
    ):
        return
    try:
        raw_voters = await fetch_refill_reactors(bot, message_id)
        # reaction 原始人数尚未达到门槛时必然无法通过，跳过昂贵的完整群成员列表读取。
        if len(raw_voters - {str(bot.self_id)}) < request.required_votes:
            return
        group_member_ids = await fetch_refill_group_members(bot, group_id)
    except RoastRefillReactionError as error:
        logger.warning(f"rollpig 烤箱补货验票失败: request={request.request_id} error={error}")
        if error.message_missing:
            await store.fail_group_roast_refill(request.request_id, message_id, "message_missing")
        return

    max_charges = resolve_roast_charge_max()
    result = await store.complete_group_roast_refill(
        request_id=request.request_id,
        message_id=message_id,
        # Store 会在原子事务内再次与最新日活求交集；这里先剔除已不在群内的 reaction 用户。
        voter_ids=sorted(raw_voters & group_member_ids),
        excluded_user_ids=[str(bot.self_id)],
        max_charges=max_charges,
    )
    if not result.completed or result.request is None:
        return
    await bot.send_group_msg(
        group_id=int(result.request.group_id),
        message=format_refill_success(
            result.request,
            votes=len(result.valid_voter_ids),
            benefited=len(result.benefited_user_ids),
            max_charges=max_charges,
        ),
    )
