from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


MAX_EXPERT_LEVEL = 5


def expert_level_from_copies(copies: int) -> int:
    """根据累计抽取次数计算 EX Lv.；异常或旧数据统一钳制到 0～5。"""

    return min(max(int(copies or 0) - 1, 0), MAX_EXPERT_LEVEL)


@dataclass(frozen=True)
class PigProgress:
    copies: int = 0
    first_obtained_at: Optional[str] = None

    @property
    def expert_level(self) -> int:
        """返回当前小猪的只读 EX Lv.，避免展示层重复等级公式。"""

        return expert_level_from_copies(self.copies)


@dataclass(frozen=True)
class DrawState:
    pig_ids: list[str]
    progress: dict[str, PigProgress]
    duplicate_streak: int = 0

    def copies_of(self, pig_id: str) -> int:
        item = self.progress.get(pig_id)
        return int(item.copies) if item else 0

    def expert_level_of(self, pig_id: str) -> int:
        """返回指定小猪的 EX Lv.；未拥有或记录缺失时为 EX0。"""

        return expert_level_from_copies(self.copies_of(pig_id))


@dataclass(frozen=True)
class DailyRollResult:
    pig_id: str
    created: bool
    is_new_pig: bool = False
    previous_copies: int = 0
    copies: int = 0
    previous_duplicate_streak: int = 0
    duplicate_streak: int = 0

    def __iter__(self):
        # 兼容旧代码的 `pig_id, created = await get_or_create_daily_roll(...)` 写法。
        yield self.pig_id
        yield self.created


@dataclass(frozen=True)
class CooldownConsumeResult:
    allowed: bool
    remaining_seconds: int = 0
    charges_left: int = 0
    max_charges: int = 1
    next_recover_seconds: int = 0


@dataclass(frozen=True)
class CatalogSnapshot:
    draw_state: DrawState
    recent_rolls: dict[str, str]
    roasted_7d: int = 0


@dataclass(frozen=True)
class RoastEvent:
    event_type: str
    attacker_id: str
    target_id: str
    attacker_name: str = ""
    target_name: str = ""
    food: str = ""
    group_id: str = ""
