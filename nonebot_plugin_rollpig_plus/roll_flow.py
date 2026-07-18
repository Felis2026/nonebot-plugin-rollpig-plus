from __future__ import annotations

import random
from dataclasses import dataclass

from nonebot.log import logger

from .resource_manager import pig_resource_manager
from .store import store
from .store.models import DailyRollResult, DrawState
from .texts import (
    DAILY_ROLL_DUPLICATE_LEVEL_UP_TEXTS,
    DAILY_ROLL_DUPLICATE_SAME_LEVEL_TEXTS,
    DAILY_ROLL_NEW_PIG_TEXTS,
)


MAX_EXPERT_LEVEL = 5
DUPLICATE_PITY_WEIGHT_STEP = 0.5
DUPLICATE_PITY_WEIGHT_CAP = 4.0


@dataclass(frozen=True)
class DailyPigResolution:
    """今日形态解析结果；命令层用它决定发卡片、提示成长，或提示资源缺失。"""

    pig: dict | None
    roll_result: DailyRollResult | None = None
    growth_text: str = ""
    missing_resources: bool = False

    @property
    def was_auto_created(self) -> bool:
        """本次命令是否顺手创建了今日抽猪记录。"""

        return bool(self.roll_result and self.roll_result.created)


def get_expert_level(copies: int) -> int:
    """根据累计抽到次数计算专家等级：1 次为 Lv.0，6 次及以上封顶 Lv.5。"""
    return min(max(int(copies) - 1, 0), MAX_EXPERT_LEVEL)


# ================================ 今日小猪成长流程 ================================ #
# 集中处理“今日小猪”与图鉴成长相关的纯业务规则。


async def pick_daily_roll_candidate(user_id: str) -> dict:
    """按用户当前图鉴状态选择今日候选猪；连续重复越多，新猪权重越高。"""
    pig_list = pig_resource_manager.pig_list
    draw_state = await store.get_draw_state(user_id)
    owned_pig_ids = set(draw_state.pig_ids)
    duplicate_streak = max(0, int(draw_state.duplicate_streak or 0))
    new_pig_bonus = min(duplicate_streak * DUPLICATE_PITY_WEIGHT_STEP, DUPLICATE_PITY_WEIGHT_CAP)

    weights = []
    for pig in pig_list:
        pig_id = str(pig.get("id", ""))
        is_unowned = pig_id and pig_id not in owned_pig_ids
        weights.append(1.0 + new_pig_bonus if is_unowned else 1.0)

    # random.choices 比手写累计权重更不容易写出边界错误；pig_list 为空时调用方已拦截。
    return random.choices(pig_list, weights=weights, k=1)[0]


def build_roll_growth_text(result: DailyRollResult, pig_data: dict) -> str:
    """生成今日首次抽猪后的成长提示；重复查看当天结果时不刷提示也不刷等级。"""
    if not result.created:
        return ""

    pig_name = pig_data.get("name", "未知小猪")
    current_level = get_expert_level(result.copies)
    if result.is_new_pig:
        return random.choice(DAILY_ROLL_NEW_PIG_TEXTS).format(pig=pig_name, level=current_level)

    previous_level = get_expert_level(result.previous_copies)
    if previous_level != current_level:
        return random.choice(DAILY_ROLL_DUPLICATE_LEVEL_UP_TEXTS).format(
            pig=pig_name,
            old_level=previous_level,
            new_level=current_level,
        )
    return random.choice(DAILY_ROLL_DUPLICATE_SAME_LEVEL_TEXTS).format(pig=pig_name, level=current_level)


async def resolve_daily_pig(user_id: str, group_id: str = "") -> DailyPigResolution:
    """
    取得用户今日形态；没有记录时自动抽取并更新图鉴进度。

    今日小猪与今日烤猪都依赖这条流程。集中处理后可以保证：
    1. 重复查看不会重复增加图鉴进度；
    2. 群内可见记录统一写入；
    3. 资源缺失时由命令层按各自口吻提示。
    """

    pig_id = await store.get_daily_roll(user_id)
    current_pig = pig_resource_manager.pig_map.get(pig_id) if pig_id else None
    if current_pig:
        if group_id:
            await store.mark_group_roll_seen(user_id, current_pig["id"], group_id)
        return DailyPigResolution(pig=current_pig)

    if pig_id:
        # 已保存的 ID 缺失时绝不能用随机候选替代，否则展示结果会与账本永久不一致。
        # 具体 ID 只进入管理员日志；命令层继续复用现有数据缺失提示，无需改变返回结构。
        logger.warning(f"RollPig 今日形态资源缺失: user={user_id} pig_id={pig_id}")
        return DailyPigResolution(pig=None, missing_resources=True)

    if not pig_resource_manager.pig_list:
        return DailyPigResolution(pig=None, missing_resources=True)

    proposed_pig = await pick_daily_roll_candidate(user_id)
    roll_result = await store.get_or_create_daily_roll(
        user_id,
        proposed_pig["id"],
        group_id=group_id,
    )
    pig = pig_resource_manager.pig_map.get(roll_result.pig_id) or proposed_pig
    return DailyPigResolution(
        pig=pig,
        roll_result=roll_result,
        growth_text=build_roll_growth_text(roll_result, pig),
    )


def build_pigsty_growth_summary(user_name: str, draw_state: DrawState, total_pigs: int) -> str:
    """生成文本版猪圈摘要；图片版图鉴由“小猪图鉴”命令独立提供。"""
    user_count = len(draw_state.pig_ids)
    percent = int((user_count / total_pigs) * 100) if total_pigs > 0 else 0

    ranked_progress = sorted(
        draw_state.progress.items(),
        key=lambda item: (-item[1].copies, item[1].first_obtained_at or "", item[0]),
    )
    favorite_line = "🐷 本命猪：暂无"
    top_repeat_line = "⭐ 高等级小猪：暂无重复猪，猪圈还很清新"
    max_level = 0
    maxed_count = 0
    if ranked_progress:
        levels = [get_expert_level(progress.copies) for _, progress in ranked_progress]
        max_level = max(levels)
        maxed_count = sum(1 for level in levels if level >= MAX_EXPERT_LEVEL)

        favorite_id, favorite_progress = ranked_progress[0]
        favorite = pig_resource_manager.pig_map.get(favorite_id)
        favorite_name = favorite.get("name", favorite_id) if favorite else favorite_id
        favorite_level = get_expert_level(favorite_progress.copies)
        favorite_line = f"🐷 本命猪：【{favorite_name}】EX Lv.{favorite_level}（累计 {favorite_progress.copies} 次）"

        repeat_items = [
            (pig_id, progress)
            for pig_id, progress in ranked_progress
            if progress.copies >= 2
        ][:5]
        if repeat_items:
            parts = []
            for pig_id, progress in repeat_items:
                pig = pig_resource_manager.pig_map.get(pig_id)
                pig_name = pig.get("name", pig_id) if pig else pig_id
                parts.append(f"【{pig_name}】EX Lv.{get_expert_level(progress.copies)}")
            top_repeat_line = "⭐ 高等级小猪：" + "、".join(parts)

    if draw_state.duplicate_streak > 0:
        streak_line = f"🔥 连续重复：{draw_state.duplicate_streak} 次（新猪气息正在靠近）"
    else:
        streak_line = "🔥 连续重复：0 次（下一只从平常心开始）"

    footer_line = "发送「今日小猪」开始收集。" if user_count <= 0 else "发送「小猪图鉴」查看图片版完整图鉴。"

    return (
        f"【我的猪圈统计】\n"
        f"👑 猪圈主人：{user_name}\n"
        f"📦 已收集：{user_count} / {total_pigs} 只\n"
        f"📈 收藏率：{percent}%\n"
        f"🏅 最高等级：EX Lv. {max_level}｜满级 {maxed_count} 只\n"
        f"{favorite_line}\n"
        f"{top_repeat_line}\n"
        f"{streak_line}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{footer_line}"
    )
