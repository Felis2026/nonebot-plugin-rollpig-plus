from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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
    reservation_id: str = ""
    participant_ids: tuple[str, ...] = ()
    participant_names: tuple[str, ...] = ()
    participant_count: int = 0


@dataclass(frozen=True)
class RoastReservationParticipant:
    user_id: str
    display_name: str = ""
    pig_id: str = ""


@dataclass(frozen=True)
class RoastReservation:
    reservation_id: str
    date_str: str
    group_id: str
    target_id: str
    target_name: str
    owner_id: str
    owner_name: str
    owner_pig_id: str
    participants: tuple[RoastReservationParticipant, ...] = ()
    delivery_bot_id: str = ""
    force_mode: Optional[str] = None
    status: str = "pending"
    target_pig_id: str = ""
    outcome_snapshot: Optional[dict[str, Any]] = None
    claim_token: str = ""

    @property
    def participant_count(self) -> int:
        return len(self.participants)


@dataclass(frozen=True)
class UnrolledRoastAttemptResult:
    date_str: str
    user_id: str
    count: int


@dataclass(frozen=True)
class RoastReservationPrepareResult:
    status: str
    reservation: Optional[RoastReservation] = None
    cooldown: Optional[CooldownConsumeResult] = None
    target_pig_id: str = ""
    protection_broken: bool = False


@dataclass(frozen=True)
class RoastReservationClaimResult:
    reservations: tuple[RoastReservation, ...] = ()
