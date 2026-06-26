from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import CatalogSnapshot, CooldownConsumeResult, DailyRollResult, DrawState, RoastEvent


class RollpigStore(ABC):
    async def close(self) -> None:
        """释放后端持有的连接或句柄；本地 JSON 后端没有常驻资源，默认无需处理。"""
        return None

    @abstractmethod
    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
    ) -> DailyRollResult:
        raise NotImplementedError

    @abstractmethod
    async def get_draw_state(self, user_id: str) -> DrawState:
        raise NotImplementedError

    @abstractmethod
    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_group_rolls(self, group_id: str, date_str: Optional[str] = None) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def get_user_collection(self, user_id: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        raise NotImplementedError

    @abstractmethod
    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        raise NotImplementedError

    @abstractmethod
    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def append_roast_event(self, event: RoastEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_daily_events(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        raise NotImplementedError

    @abstractmethod
    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def prune_history(self, days_to_keep: int = 14) -> None:
        raise NotImplementedError

    @abstractmethod
    async def prune_events(self, days_to_keep: int = 7) -> None:
        raise NotImplementedError
