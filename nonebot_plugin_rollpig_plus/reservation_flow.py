from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass, replace
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .card_renderer import render_pig_card_image
from .resource_manager import get_pig_by_id, pig_resource_manager
from .runtime import is_group_rollpig_enabled
from .roast_flow import (
    RoastOutcome,
    build_backfire_roast_outcome,
    build_success_roast_outcome,
    can_backfire_roast,
    is_eaten_pig,
    is_food_pig,
    is_human_pig,
    is_sold_pig,
    pick_food_pig,
    pick_protected_waiting_text,
)
from .store import store
from .store.models import RoastEvent, RoastReservation
from .texts import (
    DAILY_FEED_RESERVATION_TEXTS,
    PROTECTION_LEVEL_UP_TEXTS,
    PROTECTED_ROAST_BACKFIRE_TEXTS,
    PROTECTED_ROAST_ESCAPE_TEXTS,
    PROTECTED_ROAST_SUCCESS_TEXTS,
    RESERVED_TARGET_EATEN_TEXTS,
    RESERVED_TARGET_FOOD_TEXTS,
    RESERVED_TARGET_HUMAN_TEXTS,
    RESERVED_TARGET_SOLD_TEXTS,
    ROAST_RESERVATION_BACKFIRE_TEXTS,
    ROAST_RESERVATION_ESCAPE_TEXTS,
    ROAST_RESERVATION_MULTI_BACKFIRE_CARD_TEXTS,
    ROAST_RESERVATION_MULTI_BACKFIRE_NO_FOOD_TEXTS,
    ROAST_RESERVATION_MULTI_BACKFIRE_SUBTITLES,
    ROAST_RESERVATION_MULTI_BACKFIRE_TEXTS,
    ROAST_RESERVATION_SINGLE_ROASTABLE_BACKFIRE_CARD_TEXTS,
    ROAST_RESERVATION_SUCCESS_TEXTS,
    SUPER_FORCE_RESERVATION_SUCCESS_TEXT,
    SUPER_FORCE_RESERVATION_TAKEOVER_SUCCESS_TEXT,
)


class ReservationResourceUnavailableError(RuntimeError):
    """预约引用的小猪尚未出现在当前 Bot 的资源版本中，可在同步后重试。"""


@dataclass(frozen=True)
class ReservationDeliveryResult:
    """一次 claim/deliver 的结果，同时携带 Owner 是否仍需继续轮询。"""

    completed: int = 0
    has_owned: bool = False


# ================================ 本地失败退避 ================================ #
# 退避只抑制当前进程重复领取，不改变 Cloud 的可靠状态。资源同步或群内正常指令
# 可以提前解除；进程重启最多重新发现一次，不会留下新的持久状态。
RESOURCE_RETRY_BACKOFF_SECONDS = 5 * 60.0
GROUP_DISABLED_BACKOFF_SECONDS = 15 * 60.0
_resource_backoff_until: dict[str, float] = {}
_group_backoff: dict[str, tuple[str, float]] = {}


def _active_backoff_reservation_ids() -> set[str]:
    """返回仍在暂缓期的预约，并顺手清理过期本地记录。"""

    now = time.monotonic()
    for reservation_id, retry_at in list(_resource_backoff_until.items()):
        if retry_at <= now:
            _resource_backoff_until.pop(reservation_id, None)
    for reservation_id, (_group_id, retry_at) in list(_group_backoff.items()):
        if retry_at <= now:
            _group_backoff.pop(reservation_id, None)
    return set(_resource_backoff_until) | set(_group_backoff)


def _defer_for_resource_sync(reservation: RoastReservation) -> None:
    _resource_backoff_until[reservation.reservation_id] = (
        time.monotonic() + RESOURCE_RETRY_BACKOFF_SECONDS
    )


def _defer_for_disabled_group(reservation: RoastReservation) -> None:
    _group_backoff[reservation.reservation_id] = (
        reservation.group_id,
        time.monotonic() + GROUP_DISABLED_BACKOFF_SECONDS,
    )


def clear_resource_reservation_backoffs() -> bool:
    """资源同步成功后解除资源暂缓；返回是否存在需要立即唤醒的预约。"""

    had_backoff = bool(_resource_backoff_until)
    _resource_backoff_until.clear()
    return had_backoff


def clear_group_reservation_backoffs(group_id: str) -> bool:
    """群内再次出现有效指令时提前解除该群暂缓。"""

    normalized_group_id = str(group_id or "")
    cleared = False
    for reservation_id, (blocked_group_id, _retry_at) in list(_group_backoff.items()):
        if blocked_group_id == normalized_group_id:
            _group_backoff.pop(reservation_id, None)
            cleared = True
    return cleared


def _clear_reservation_backoff(reservation_id: str) -> None:
    _resource_backoff_until.pop(reservation_id, None)
    _group_backoff.pop(reservation_id, None)


# ================================ 预约参与者展示 ================================ #


def _participant_names(reservation: RoastReservation) -> list[str]:
    """按预约顺序返回参与者名称，并为旧记录补回可能缺失的主厨。"""

    names = [
        item.display_name
        or (reservation.owner_name if item.user_id == reservation.owner_id else "")
        or item.user_id
        or "一名群友"
        for item in reservation.participants
    ]
    if reservation.owner_id and all(
        item.user_id != reservation.owner_id for item in reservation.participants
    ):
        names.insert(0, reservation.owner_name or reservation.owner_id)
    if not names:
        names.append(reservation.owner_name or reservation.owner_id or "一名群友")
    return names


def _participants_label(reservation: RoastReservation) -> str:
    """卡片外最多列出三名参与者，人数更多时折叠为主厨与总人数。"""

    names = _participant_names(reservation)
    wrapped = [f"【{name}】" for name in names]
    if len(wrapped) == 1:
        return wrapped[0]
    if len(wrapped) == 2:
        return "和".join(wrapped)
    if len(wrapped) == 3:
        return "、".join(wrapped[:2]) + f"和{wrapped[2]}"
    owner = reservation.owner_name or reservation.owner_id or names[0]
    return f"【{owner}】等 {len(names)} 人"


def _operator_label(reservation: RoastReservation) -> str:
    """熟食卡署名在三人以内列全名，人数更多时保持紧凑。"""

    if reservation.force_operator_id:
        return reservation.force_operator_name or reservation.force_operator_id
    names = _participant_names(reservation)
    if len(names) <= 3:
        return "、".join(names)
    owner = reservation.owner_name or reservation.owner_id or names[0]
    return f"{owner} 等 {len(names)} 人"


def _multi_backfire_origins_label(victim_pigs: list[dict]) -> str:
    """将多名反噬受害者的小猪名称按被卷入顺序去重，生成自然的多猪合体短语。"""

    total = len(victim_pigs)
    unique_names: list[str] = []
    for pig in victim_pigs:
        pig_name = str(pig.get("name") or "未知小猪").strip() or "未知小猪"
        if pig_name not in unique_names:
            unique_names.append(pig_name)
    if len(unique_names) == 1:
        return f"{total} 只【{unique_names[0]}】"
    if total == 2 and len(unique_names) == 2:
        return f"【{unique_names[0]}】和【{unique_names[1]}】"
    if total == 3 and len(unique_names) == 3:
        return f"【{unique_names[0]}】、【{unique_names[1]}】和【{unique_names[2]}】"
    head = "、".join(f"【{name}】" for name in unique_names[:3])
    return f"{head}等 {total} 只小猪"


def _serialize_outcome(outcome: RoastOutcome) -> dict[str, Any]:
    return {
        "event_type": outcome.event_type,
        "render_data": outcome.render_data,
        "plain_text": outcome.plain_text,
        "extra_text": outcome.extra_text,
        "food_name": outcome.food_name,
        "backfire_victim_id": outcome.backfire_victim_id,
        "backfire_victim_name": outcome.backfire_victim_name,
        "backfire_victim_ids": list(outcome.backfire_victim_ids),
        "backfire_victim_names": list(outcome.backfire_victim_names),
        "special_reason": outcome.special_reason,
    }


def _deserialize_outcome(snapshot: dict[str, Any]) -> RoastOutcome:
    return RoastOutcome(
        event_type=str(snapshot.get("event_type") or "escape"),
        render_data=dict(snapshot["render_data"]) if isinstance(snapshot.get("render_data"), dict) else None,
        plain_text=str(snapshot.get("plain_text") or ""),
        extra_text=str(snapshot.get("extra_text") or ""),
        food_name=str(snapshot.get("food_name") or ""),
        backfire_victim_id=str(snapshot.get("backfire_victim_id") or ""),
        backfire_victim_name=str(snapshot.get("backfire_victim_name") or ""),
        backfire_victim_ids=tuple(snapshot.get("backfire_victim_ids") or ()),
        backfire_victim_names=tuple(snapshot.get("backfire_victim_names") or ()),
        special_reason=str(snapshot.get("special_reason") or ""),
    )


# ================================ 预约投递可靠性 ================================ #


async def _prepare_outcome_with_retry(
    reservation: RoastReservation,
    outcome_snapshot: dict[str, Any],
) -> RoastReservation | None:
    """利用相同 token/snapshot 幂等重试，覆盖 Cloud 成功但响应丢失的窗口。"""

    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            updated = await store.save_roast_reservation_outcome(
                reservation,
                outcome_snapshot,
                settle_daily_feed=(
                    (reservation.force_feed_user_ids or reservation.force_mode is None
                     or (reservation.protection_required and reservation.force_mode != "super"))
                    and str(outcome_snapshot.get("event_type") or "") == "success"
                ),
            )
            if updated is not None:
                return updated
            logger.error(
                "rollpig 预约结果固化被拒绝: "
                f"reservation={reservation.reservation_id} attempt={attempt}"
            )
            return None
        except Exception as error:
            logger.warning(
                "rollpig 预约结果固化响应失败并将幂等重试: "
                f"reservation={reservation.reservation_id} attempt={attempt} error={error}"
            )
    return None


async def _mark_sending_with_retry(reservation: RoastReservation) -> RoastReservation | None:
    """幂等确认 sending；只有拿到成功响应后才调用外部 QQ 发送接口。"""

    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            updated = await store.mark_roast_reservation_sending(reservation)
            if updated is not None:
                return updated
            logger.error(
                "rollpig 预约进入发送态被拒绝: "
                f"reservation={reservation.reservation_id} attempt={attempt}"
            )
            return None
        except Exception as error:
            logger.warning(
                "rollpig 预约发送态响应失败并将幂等重试: "
                f"reservation={reservation.reservation_id} attempt={attempt} error={error}"
            )
    return None


async def _release_after_delivery_failure(reservation: RoastReservation, *, reason: str) -> bool:
    """安全释放明确未发送的预约；Cloud 瞬时异常或拒绝都会短暂重试。"""

    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await store.release_roast_reservation(reservation):
                return True
            logger.warning(
                "rollpig 预约释放被拒绝并将重试: "
                f"reservation={reservation.reservation_id} attempt={attempt} reason={reason}"
            )
        except Exception as error:
            logger.warning(
                "rollpig 预约释放失败并将重试: "
                f"reservation={reservation.reservation_id} attempt={attempt} reason={reason} error={error}"
            )

    logger.error(
        "rollpig 预约释放重试耗尽，保留当前状态等待人工核查: "
        f"reservation={reservation.reservation_id} reason={reason}"
    )
    return False


async def _complete_after_send(reservation: RoastReservation, event: RoastEvent) -> bool:
    """消息成功后幂等完成预约与事件；此阶段绝不能再 release 已发送结果。"""

    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await store.complete_roast_reservation(reservation, event):
                return True
            logger.error(
                "rollpig 预约消息已发送但完成状态被拒绝: "
                f"reservation={reservation.reservation_id} attempt={attempt}"
            )
            return False
        except Exception as error:
            logger.warning(
                "rollpig 预约消息已发送，完成状态写入失败并将重试: "
                f"reservation={reservation.reservation_id} attempt={attempt} error={error}"
            )
    return False


def _special_target_outcome(reservation: RoastReservation, target_pig: dict) -> RoastOutcome | None:
    values = {
        "participants": _participants_label(reservation),
        "target": reservation.target_name or reservation.target_id,
        "pig": target_pig.get("name", "未知小猪"),
    }
    if is_human_pig(target_pig):
        pool = RESERVED_TARGET_HUMAN_TEXTS
        special_reason = "human"
    elif is_food_pig(target_pig):
        pool = RESERVED_TARGET_FOOD_TEXTS
        special_reason = "food"
    elif is_eaten_pig(target_pig):
        pool = RESERVED_TARGET_EATEN_TEXTS
        special_reason = "eaten"
    elif is_sold_pig(target_pig):
        pool = RESERVED_TARGET_SOLD_TEXTS
        special_reason = "sold"
    else:
        return None
    return RoastOutcome(
        event_type="reserved_special",
        plain_text=random.choice(pool).format(**values),
        special_reason=special_reason,
    )


async def build_reservation_outcome(reservation: RoastReservation) -> RoastOutcome:
    """只在没有结果快照时执行一次随机判定；调用方必须立即持久化结果。"""

    target_pig = get_pig_by_id(reservation.target_pig_id)
    if not target_pig:
        raise ReservationResourceUnavailableError(
            f"预约目标资源暂时缺失: pig_id={reservation.target_pig_id}"
        )

    special = _special_target_outcome(reservation, target_pig)
    if special:
        return special

    values = {
        "participants": _participants_label(reservation),
        "target": reservation.target_name or reservation.target_id,
        "pig": target_pig.get("name", "未知小猪"),
    }
    if reservation.force_mode == "super" or (reservation.force_mode == "normal" and not reservation.protection_required):
        roll = 1
    else:
        roll = random.randint(1, 100)
    if roll <= 60:
        if reservation.force_mode == "super":
            if reservation.force_operator_id:
                # 接管者单独作为执行者展示，等待队友的名单按账号排除本人。
                others = replace(
                    reservation,
                    participants=tuple(item for item in reservation.participants
                                       if item.user_id != reservation.force_operator_id),
                    owner_id=reservation.owner_id if reservation.owner_id != reservation.force_operator_id else "",
                    owner_name=reservation.owner_name if reservation.owner_id != reservation.force_operator_id else "",
                )
                if others.participants or others.owner_id:
                    values["participants"] = _participants_label(others)
                    text = SUPER_FORCE_RESERVATION_TAKEOVER_SUCCESS_TEXT
                else:
                    text = SUPER_FORCE_RESERVATION_SUCCESS_TEXT
            else:
                text = SUPER_FORCE_RESERVATION_SUCCESS_TEXT
        else:
            pool = PROTECTED_ROAST_SUCCESS_TEXTS if reservation.protection_required else ROAST_RESERVATION_SUCCESS_TEXTS
            text = random.choice(pool)
        prefix = text.format(**values, operator=_operator_label(reservation))
        return await build_success_roast_outcome(
            target_pig,
            attacker_name=_operator_label(reservation),
            target_name=reservation.target_name or reservation.target_id,
            extra_text=prefix,
        )
    if roll <= 90:
        pool = PROTECTED_ROAST_ESCAPE_TEXTS if reservation.protection_required else ROAST_RESERVATION_ESCAPE_TEXTS
        return RoastOutcome(
            event_type="escape",
            plain_text=random.choice(pool).format(**values),
        )

    # 每场只判定一次反噬，再等概率选取 1～N 人；完整名单随结果快照固定。
    if len(reservation.participants) > 1:
        victims = random.sample(reservation.participants, random.randrange(1, len(reservation.participants) + 1))
    else:
        victims = [random.choice(reservation.participants)]
    victim = victims[0]
    victim_pigs = []
    for participant in victims:
        pig = get_pig_by_id(participant.pig_id)
        if not pig:
            raise ReservationResourceUnavailableError(
                f"预约参与者资源暂时缺失: user_id={participant.user_id} pig_id={participant.pig_id}"
            )
        victim_pigs.append(pig)
    victim_pig = victim_pigs[0]
    if len(victims) > 1:
        # 旧客户端只认识单个受害者，优先用可烤者作代表，完整名单另存。
        for index, pig in enumerate(victim_pigs):
            if can_backfire_roast(pig):
                victim = victims[index]
                victim_pig = pig
                break
    names = tuple(item.display_name or item.user_id for item in victims)
    if len(victims) > 1:
        # 名单在图外横排；卡片原料与成菜人数仅统计可烤者，避免特殊形态被写进二次加工食材。
        roastable_pigs = [pig for pig in victim_pigs if can_backfire_roast(pig)]
        food = pick_food_pig() if roastable_pigs else None
        pool = ROAST_RESERVATION_MULTI_BACKFIRE_TEXTS if food is not None else ROAST_RESERVATION_MULTI_BACKFIRE_NO_FOOD_TEXTS
        summary = random.choice(pool).format(
            **values,
            count=len(victims),
            victims="、".join(names),
        )
        if food is not None:
            render_data = dict(food)
            food_name = str(food.get("name") or "")
            render_data["description"] = random.choice(ROAST_RESERVATION_MULTI_BACKFIRE_SUBTITLES)
            if len(roastable_pigs) == 1:
                origin_name = str(roastable_pigs[0].get("name") or "未知小猪").strip() or "未知小猪"
                render_data["analysis"] = random.choice(ROAST_RESERVATION_SINGLE_ROASTABLE_BACKFIRE_CARD_TEXTS).format(
                    origin=origin_name,
                    food=food_name or "熟食",
                )
            else:
                render_data["analysis"] = random.choice(ROAST_RESERVATION_MULTI_BACKFIRE_CARD_TEXTS).format(
                    origins=_multi_backfire_origins_label(roastable_pigs),
                    count=len(roastable_pigs),
                    food=food_name or "熟食",
                )
            outcome = RoastOutcome(
                event_type="backfire",
                render_data=render_data,
                extra_text=summary,
                food_name=food_name,
            )
        else:
            outcome = RoastOutcome(
                event_type="backfire",
                plain_text=summary,
            )
    else:
        backfire_pool = (
            PROTECTED_ROAST_BACKFIRE_TEXTS
            if reservation.protection_required
            else ROAST_RESERVATION_BACKFIRE_TEXTS
        )
        prefix = random.choice(backfire_pool).format(
            **values,
            victim=victim.display_name or victim.user_id,
        )
        outcome = await build_backfire_roast_outcome(
            victim_pig,
            attacker_name=victim.display_name or victim.user_id,
            target_name=reservation.target_name or reservation.target_id,
            extra_text=prefix,
        )
    return replace(
        outcome,
        backfire_victim_id=victim.user_id,
        backfire_victim_name=victim.display_name or victim.user_id,
        backfire_victim_ids=tuple(item.user_id for item in victims),
        backfire_victim_names=names,
    )


def _build_reservation_event(reservation: RoastReservation, outcome: RoastOutcome) -> RoastEvent:
    """保留旧事件语义：attacker 是主厨，target 是原烧烤目标。"""

    return RoastEvent(
        event_type=outcome.event_type,
        attacker_id=reservation.force_operator_id or reservation.owner_id,
        target_id=reservation.target_id,
        attacker_name=reservation.force_operator_name or reservation.owner_name,
        target_name=reservation.target_name,
        food=outcome.food_name,
        group_id=reservation.group_id,
        reservation_id=reservation.reservation_id,
        participant_ids=tuple(item.user_id for item in reservation.participants),
        participant_names=tuple(item.display_name for item in reservation.participants),
        participant_count=reservation.participant_count,
        backfire_victim_id=outcome.backfire_victim_id,
        backfire_victim_name=outcome.backfire_victim_name,
        backfire_victim_ids=outcome.backfire_victim_ids,
        backfire_victim_names=outcome.backfire_victim_names,
        special_reason=outcome.special_reason,
        force_mode=reservation.force_mode or "",
        protection_level=max(0, reservation.protection_required - 1),
        reservation_owner_id=reservation.owner_id,
        reservation_owner_name=reservation.owner_name,
    )


def _build_protection_upgrade_text(reservation: RoastReservation, outcome: RoastOutcome) -> str:
    """保护目标被成功烤成且尚未满级时，在结算末尾追加保护升级提示。"""

    if outcome.event_type != "success" or not reservation.protection_required:
        return ""
    before_level = max(1, reservation.protection_required - 1)
    if before_level >= 5:
        return ""
    digest = hashlib.sha256(str(reservation.reservation_id or "").encode("utf-8")).digest()
    template = PROTECTION_LEVEL_UP_TEXTS[digest[1] % len(PROTECTION_LEVEL_UP_TEXTS)]
    return template.format(
        name=reservation.target_name or reservation.target_id,
        level=before_level + 1,
    )


def _build_reservation_feed_text(reservation: RoastReservation) -> str:
    """把本轮真实成长者合并成一行，并按预约 ID 稳定选择文案。"""

    fed_results = [
        result
        for result in reservation.daily_feed_results
        if result.status == "fed" and result.new_level > result.previous_level
    ]
    if not fed_results:
        return ""
    display_names = {
        participant.user_id: participant.display_name or participant.user_id
        for participant in reservation.participants
    }
    names = [display_names.get(result.user_id, result.user_id) for result in fed_results]
    if len(names) <= 3:
        participants = "、".join(f"【{name}】" for name in names)
    else:
        participants = f"【{names[0]}】等 {len(names)} 人"
    digest = hashlib.sha256(reservation.reservation_id.encode("utf-8")).digest()
    template = DAILY_FEED_RESERVATION_TEXTS[digest[0] % len(DAILY_FEED_RESERVATION_TEXTS)]
    return template.format(participants=participants)


async def _prepare_reservation_message(outcome: RoastOutcome):
    """在进入 sending 前完成所有可能失败或耗时的图片渲染。"""

    if outcome.render_data:
        image_file = pig_resource_manager.find_image_file(str(outcome.render_data.get("id") or ""))
        render_result = await render_pig_card_image(
            outcome.render_data,
            image_file,
            cache_final_card=False,
        )
        # QQ 会自行分隔文字和图片；旧结果快照可能仍带有尾部换行。
        return MessageSegment.text(outcome.extra_text.rstrip("\n")) + MessageSegment.image(render_result.data)
    return MessageSegment.text(outcome.plain_text)


async def _send_reservation_message(bot, reservation: RoastReservation, message) -> None:
    """调用外部 OneBot 发送接口；调用前预约必须已经进入 sending。"""

    await bot.send_group_msg(group_id=int(reservation.group_id), message=message)


async def _transition_waiting_notice(reservation: RoastReservation, action: str) -> bool:
    """使用同一 token 幂等重试提示状态，响应丢失时不重复发送群消息。"""
    for delay in (0.0, 0.25, 0.75):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await store.transition_roast_waiting_notice(reservation, action)
        except Exception as error:
            logger.warning(
                f"rollpig 等待提示状态更新失败: reservation={reservation.reservation_id} action={action} error={error}"
            )
    return False


async def _deliver_waiting_notices(bot, notices: tuple[RoastReservation, ...]) -> None:
    """发送目标到场后未达标的保护提示，不生成烧烤结果或消费额度。"""
    for reservation in notices:
        try:
            if not is_group_rollpig_enabled(reservation.group_id):
                _defer_for_disabled_group(reservation)
                await _transition_waiting_notice(reservation, "release")
                continue
            message = MessageSegment.text(pick_protected_waiting_text(reservation))
        except Exception as error:
            await _transition_waiting_notice(reservation, "release")
            logger.warning(f"rollpig 等待提示准备失败: reservation={reservation.reservation_id} error={error}")
            continue
        if not await _transition_waiting_notice(reservation, "sending"):
            continue
        try:
            result = await bot.send_group_msg(group_id=int(reservation.group_id), message=message)
        except Exception as error:
            # 与烧烤结果一样，外部发送报错可能发生在消息送达后，保留 sending 防止刷屏。
            logger.error(f"rollpig 等待提示发送结果不确定: reservation={reservation.reservation_id} error={error}")
            continue
        await _transition_waiting_notice(reservation, "sent")
        try:
            if isinstance(result, dict) and result.get("message_id"):
                await store.bind_roast_reservation_message(
                    reservation_id=reservation.reservation_id, bot_id=reservation.delivery_bot_id,
                    group_id=reservation.group_id, message_id=str(result["message_id"]), date_str=reservation.date_str,
                )
        except Exception as error:
            logger.warning(f"rollpig 等待提示关联未保存，仍可通过烤群友加入: reservation={reservation.reservation_id} error={error}")


async def deliver_ready_reservations(delivery_bot_id: str) -> ReservationDeliveryResult:
    """机会式领取并投递当前 Bot 的 ready 预约；失败只 release，不重新随机。"""

    bot = get_bots().get(str(delivery_bot_id))
    if bot is None:
        return ReservationDeliveryResult()
    claimed = await store.claim_roast_reservations(
        str(delivery_bot_id),
        excluded_reservation_ids=_active_backoff_reservation_ids(),
    )
    await _deliver_waiting_notices(bot, claimed.waiting_notices)
    completed = 0
    for reservation in claimed.reservations:
        if not is_group_rollpig_enabled(reservation.group_id):
            logger.info(
                "rollpig 目标群已关闭，预约结果延期投递: "
                f"reservation={reservation.reservation_id} group={reservation.group_id}"
            )
            _defer_for_disabled_group(reservation)
            await _release_after_delivery_failure(reservation, reason="group_disabled")
            continue
        # ================================ 可恢复的发送前阶段 ================================ #
        # 结果生成、快照落盘和图片渲染都可能被取消或因资源同步滞后而失败；这些步骤
        # 只能处于 processing/prepared，使进程退出后仍可依赖租约重新领取。
        try:
            if reservation.outcome_snapshot:
                outcome = _deserialize_outcome(reservation.outcome_snapshot)
            else:
                outcome = await build_reservation_outcome(reservation)

            if reservation.status != "prepared":
                updated = await _prepare_outcome_with_retry(
                    reservation,
                    _serialize_outcome(outcome),
                )
                if updated is None:
                    raise RuntimeError("预约结果持久化失败，取消本次投递")
                reservation = replace(updated, claim_token=reservation.claim_token or updated.claim_token)

            message = await _prepare_reservation_message(outcome)
            upgrade_text = _build_protection_upgrade_text(reservation, outcome)
            if upgrade_text:
                separator = "" if outcome.render_data else "\n"
                message += MessageSegment.text(separator + upgrade_text)
            feed_text = _build_reservation_feed_text(reservation)
            if feed_text:
                separator = "\n" if (not outcome.render_data or upgrade_text) else ""
                message += MessageSegment.text(separator + feed_text)

            # 群开关可能在结果生成期间变化，真正发送前必须再次确认。
            if not is_group_rollpig_enabled(reservation.group_id):
                _defer_for_disabled_group(reservation)
                await _release_after_delivery_failure(reservation, reason="group_disabled_before_send")
                continue
        except ReservationResourceUnavailableError as error:
            _defer_for_resource_sync(reservation)
            logger.warning(
                "rollpig 预约资源暂时缺失，已延后至资源同步或退避到期后重试: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id} error={error}"
            )
            await _release_after_delivery_failure(reservation, reason="resource_unavailable")
            continue
        except Exception as error:
            logger.warning(
                "rollpig 预约发送前准备失败，已保留固定结果等待重试: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id} error={error}"
            )
            await _release_after_delivery_failure(reservation, reason="prepare_failed")
            continue

        # ================================ 不可判定的外部发送阶段 ================================ #
        # 状态推进与 OneBot 发送无法组成同一事务。先在独立事务进入 sending，再立即
        # 调用外部接口；此后的异常可能发生在消息已送达之后，绝不能自动 release。
        try:
            updated = await _mark_sending_with_retry(reservation)
            if updated is None:
                raise RuntimeError("预约发送状态持久化失败，取消本次投递")
            reservation = replace(updated, claim_token=reservation.claim_token or updated.claim_token)
        except Exception as error:
            logger.warning(
                "rollpig 预约进入发送态失败，将尝试释放发送前租约: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id} error={error}"
            )
            await _release_after_delivery_failure(reservation, reason="mark_sending_failed")
            continue

        try:
            await _send_reservation_message(bot, reservation, message)
        except Exception as error:
            logger.error(
                "rollpig 预约外部发送结果不确定，保留 sending 防止重复消息: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id} error={error}"
            )
            continue

        # 从这里开始 QQ 消息已经成功发送。即使 Cloud complete 暂时失败，也不能再把
        # sending 放回 ready，否则下一次机会式检查会把同一条结果重复发进群。
        event = _build_reservation_event(reservation, outcome)
        if not await _complete_after_send(reservation, event):
            logger.error(
                "rollpig 预约消息已发送但完成状态仍未确认，保留 sending 等待人工核查: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id}"
            )
            continue
        completed += 1
        _clear_reservation_backoff(reservation.reservation_id)
    return ReservationDeliveryResult(completed=completed, has_owned=claimed.has_owned)


async def deliver_newly_ready_reservations(current_bot_id: str) -> None:
    """首次抽猪后的即时机会：当前进程内所有 Bot 各领取自己负责的预约。"""

    bot_ids = {str(current_bot_id), *map(str, get_bots().keys())}
    for bot_id in bot_ids:
        try:
            await deliver_ready_reservations(bot_id)
        except Exception as error:
            logger.warning(f"rollpig 检查预约投递失败: bot={bot_id} error={error}")
