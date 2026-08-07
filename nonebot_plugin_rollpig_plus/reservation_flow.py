from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .card_renderer import render_pig_card_image
from .resource_manager import get_pig_by_id, pig_resource_manager
from .roast_flow import (
    RoastFoodMissingError,
    RoastOutcome,
    build_backfire_roast_outcome,
    build_success_roast_outcome,
    is_eaten_pig,
    is_food_pig,
    is_human_pig,
    is_sold_pig,
)
from .store import store
from .store.models import RoastEvent, RoastReservation
from .texts import (
    RESERVED_TARGET_EATEN_TEXTS,
    RESERVED_TARGET_FOOD_TEXTS,
    RESERVED_TARGET_HUMAN_TEXTS,
    RESERVED_TARGET_SOLD_TEXTS,
    ROAST_RESERVATION_BACKFIRE_TEXTS,
    ROAST_RESERVATION_ESCAPE_TEXTS,
    ROAST_RESERVATION_SUCCESS_TEXTS,
)


def _participants_label(reservation: RoastReservation) -> str:
    names = [item.display_name or item.user_id for item in reservation.participants]
    if len(names) <= 4:
        return "、".join(f"【{name}】" for name in names)
    return "、".join(f"【{name}】" for name in names[:3]) + f"和另外 {len(names) - 3} 名群友"


def _operator_label(reservation: RoastReservation) -> str:
    owner = reservation.owner_name or reservation.owner_id
    return owner if reservation.participant_count <= 1 else f"{owner} 等 {reservation.participant_count} 人"


def _serialize_outcome(outcome: RoastOutcome) -> dict[str, Any]:
    return {
        "event_type": outcome.event_type,
        "render_data": outcome.render_data,
        "plain_text": outcome.plain_text,
        "extra_text": outcome.extra_text,
        "food_name": outcome.food_name,
    }


def _deserialize_outcome(snapshot: dict[str, Any]) -> RoastOutcome:
    return RoastOutcome(
        event_type=str(snapshot.get("event_type") or "escape"),
        render_data=dict(snapshot["render_data"]) if isinstance(snapshot.get("render_data"), dict) else None,
        plain_text=str(snapshot.get("plain_text") or ""),
        extra_text=str(snapshot.get("extra_text") or ""),
        food_name=str(snapshot.get("food_name") or ""),
    )


async def _complete_after_send(reservation: RoastReservation) -> bool:
    """消息成功后短暂重试幂等 complete；此阶段绝不能再 release 已发送结果。"""

    for attempt, delay in enumerate((0.0, 0.25, 0.75), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if await store.complete_roast_reservation(reservation):
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
        "count": reservation.participant_count,
    }
    if is_human_pig(target_pig):
        pool = RESERVED_TARGET_HUMAN_TEXTS
    elif is_food_pig(target_pig):
        pool = RESERVED_TARGET_FOOD_TEXTS
    elif is_eaten_pig(target_pig):
        pool = RESERVED_TARGET_EATEN_TEXTS
    elif is_sold_pig(target_pig):
        pool = RESERVED_TARGET_SOLD_TEXTS
    else:
        return None
    return RoastOutcome(event_type="reserved_special", plain_text=random.choice(pool).format(**values))


async def build_reservation_outcome(reservation: RoastReservation) -> RoastOutcome:
    """只在没有结果快照时执行一次随机判定；调用方必须立即持久化结果。"""

    target_pig = get_pig_by_id(reservation.target_pig_id)
    if not target_pig:
        return RoastOutcome(event_type="reserved_missing", plain_text="预约目标的小猪资源暂时缺失，本场预约烤猪已安全停机。")

    special = _special_target_outcome(reservation, target_pig)
    if special:
        return special

    values = {
        "participants": _participants_label(reservation),
        "target": reservation.target_name or reservation.target_id,
        "pig": target_pig.get("name", "未知小猪"),
        "count": reservation.participant_count,
    }
    if reservation.force_mode in {"normal", "super"}:
        roll = 1
    else:
        roll = random.randint(1, 100)
    if roll <= 60:
        prefix = random.choice(ROAST_RESERVATION_SUCCESS_TEXTS).format(**values) + "\n\n"
        return await build_success_roast_outcome(
            target_pig,
            attacker_name=_operator_label(reservation),
            target_name=reservation.target_name or reservation.target_id,
            extra_text=prefix,
        )
    if roll <= 90:
        return RoastOutcome(
            event_type="escape",
            plain_text=random.choice(ROAST_RESERVATION_ESCAPE_TEXTS).format(**values),
        )

    victim = random.choice(reservation.participants)
    victim_pig = get_pig_by_id(victim.pig_id)
    prefix = random.choice(ROAST_RESERVATION_BACKFIRE_TEXTS).format(
        **values,
        victim=victim.display_name or victim.user_id,
    ) + "\n\n"
    return await build_backfire_roast_outcome(
        victim_pig,
        attacker_name=victim.display_name or victim.user_id,
        target_name=reservation.target_name or reservation.target_id,
        extra_text=prefix,
    )


async def _send_reservation_outcome(bot, reservation: RoastReservation, outcome: RoastOutcome) -> None:
    if outcome.render_data:
        image_file = pig_resource_manager.find_image_file(str(outcome.render_data.get("id") or ""))
        render_result = await render_pig_card_image(
            outcome.render_data,
            image_file,
            cache_final_card=False,
        )
        message = MessageSegment.text(outcome.extra_text) + MessageSegment.image(render_result.data)
    else:
        message = MessageSegment.text(outcome.plain_text)
    await bot.send_group_msg(group_id=int(reservation.group_id), message=message)


async def deliver_ready_reservations(delivery_bot_id: str) -> int:
    """机会式领取并投递当前 Bot 的 ready 预约；失败只 release，不重新随机。"""

    bot = get_bots().get(str(delivery_bot_id))
    if bot is None:
        return 0
    claimed = await store.claim_roast_reservations(str(delivery_bot_id))
    completed = 0
    for reservation in claimed.reservations:
        try:
            if reservation.outcome_snapshot:
                outcome = _deserialize_outcome(reservation.outcome_snapshot)
            else:
                outcome = await build_reservation_outcome(reservation)
                updated = await store.save_roast_reservation_outcome(reservation, _serialize_outcome(outcome))
                if updated is None:
                    # 结果必须先可靠落盘再发送，否则发送成功后进程退出会在重试时重新随机。
                    raise RuntimeError("预约结果持久化失败，取消本次投递")
                reservation = replace(updated, claim_token=reservation.claim_token or updated.claim_token)

            await _send_reservation_outcome(bot, reservation, outcome)
        except Exception as error:
            logger.warning(
                "rollpig 预约投递失败，已保留固定结果等待重试: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id} error={error}"
            )
            await store.release_roast_reservation(reservation)
            continue

        # 从这里开始 QQ 消息已经成功发送。即使 Cloud complete 暂时失败，也不能再把
        # processing 放回 ready，否则下一次机会式检查会把同一条结果重复发进群。
        if not await _complete_after_send(reservation):
            logger.error(
                "rollpig 预约消息已发送但完成状态仍未确认，保留 processing 等待人工核查: "
                f"reservation={reservation.reservation_id} bot={delivery_bot_id}"
            )
            continue
        completed += 1
        try:
            await store.append_roast_event(
                RoastEvent(
                    event_type=outcome.event_type,
                    attacker_id=reservation.owner_id,
                    target_id=reservation.target_id,
                    attacker_name=reservation.owner_name,
                    target_name=reservation.target_name,
                    food=outcome.food_name,
                    group_id=reservation.group_id,
                    reservation_id=reservation.reservation_id,
                    participant_ids=tuple(item.user_id for item in reservation.participants),
                    participant_names=tuple(item.display_name for item in reservation.participants),
                    participant_count=reservation.participant_count,
                )
            )
        except Exception as event_error:
            # 消息已经发出且预约已完成，事件只是日报扩展数据；不能因为统计写入失败
            # 把预约重新放回 ready，否则下次会向群里重复投递同一场结果。
            logger.warning(
                "rollpig 预约事件记录失败，预约保持 completed: "
                f"reservation={reservation.reservation_id} error={event_error}"
            )
    return completed


async def deliver_newly_ready_reservations(current_bot_id: str) -> None:
    """首次抽猪后的即时机会：当前进程内所有 Bot 各领取自己负责的预约。"""

    bot_ids = {str(current_bot_id), *map(str, get_bots().keys())}
    for bot_id in bot_ids:
        try:
            if await store.has_owned_roast_reservations(bot_id):
                await deliver_ready_reservations(bot_id)
        except Exception as error:
            logger.warning(f"rollpig 检查预约投递失败: bot={bot_id} error={error}")
