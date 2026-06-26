from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .base import RollpigStore
from .models import CatalogSnapshot, CooldownConsumeResult, DailyRollResult, DrawState, RoastEvent

if TYPE_CHECKING:
    from ..data_manager import PigDataManager


class LocalJsonStore(RollpigStore):
    def __init__(self, manager_factory: Callable[[], "PigDataManager"]):
        self._manager_factory = manager_factory

    @property
    def manager(self) -> "PigDataManager":
        return self._manager_factory()

    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        return self.manager.get_today_pig(user_id, date_str=date_str)

    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        return self.manager.get_daily_rolls(date_str)

    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
    ) -> DailyRollResult:
        return await self.manager.get_or_create_today_pig(
            user_id=user_id,
            proposed_pig_id=proposed_pig_id,
            date_str=date_str,
            group_id=group_id,
        )

    async def get_draw_state(self, user_id: str) -> DrawState:
        return self.manager.get_draw_state(user_id)

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        await self.manager.mark_group_roll_seen(user_id, pig_id, group_id, date_str=date_str)

    async def get_group_rolls(self, group_id: str, date_str: Optional[str] = None) -> dict[str, str]:
        return self.manager.get_group_rolls(group_id, date_str)

    async def get_user_collection(self, user_id: str) -> list[str]:
        return self.manager.get_user_collection(user_id)

    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        return self.manager.get_pig_by_date(user_id, date_str)

    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        return await self.manager.consume_roast_usage(
            user_id,
            now_ts=now_ts,
            cooldown_seconds=cooldown_seconds,
            max_charges=max_charges,
        )

    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        return self.manager.get_catalog_snapshot(user_id, days=days)

    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        return await self.manager.consume_force_roast_usage(user_id, date_str=date_str)

    async def append_roast_event(self, event: RoastEvent) -> None:
        await self.manager.log_roast_event(
            event.event_type,
            event.attacker_id,
            event.target_id,
            attacker_name=event.attacker_name,
            target_name=event.target_name,
            food=event.food,
            group_id=event.group_id,
        )

    async def list_daily_events(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> list[dict]:
        return self.manager.get_daily_events(date_str=date_str, group_id=group_id)

    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        return self.manager.get_active_group_ids(date_str=date_str)

    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        await self.manager.replace_group_protected_users(group_id, user_ids, protect_date=protect_date)

    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        return self.manager.is_protected(group_id, user_id, date_str=date_str)

    async def prune_history(self, days_to_keep: int = 14) -> None:
        await self.manager.clean_old_history(days_to_keep=days_to_keep)

    async def prune_events(self, days_to_keep: int = 7) -> None:
        await self.manager.clean_old_events(days_to_keep=days_to_keep)
