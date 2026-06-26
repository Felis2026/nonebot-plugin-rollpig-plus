from __future__ import annotations

from typing import Optional

import httpx
from nonebot import get_plugin_config
from nonebot.log import logger

from ..config import Config
from ..runtime import rollpig_date_str
from .base import RollpigStore
from .models import CatalogSnapshot, CooldownConsumeResult, DailyRollResult, DrawState, PigProgress, RoastEvent


class CloudStoreError(RuntimeError):
    pass


class CloudStore(RollpigStore):
    def __init__(self):
        config = get_plugin_config(Config)
        if not config.rollpig_cloud_api_url:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_api_url")
        if not config.rollpig_cloud_token:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_token")

        self.base_url = config.rollpig_cloud_api_url.rstrip("/")
        self.timeout = max(0.5, float(config.rollpig_cloud_timeout or 3.0))
        self.strict_mode = bool(config.rollpig_cloud_strict_mode)
        self.headers = {
            "Authorization": f"Bearer {config.rollpig_cloud_token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            # CloudStore 是高频路径：复用连接池能减少 TCP/TLS 开销，同时用上限避免异常并发撑爆连接。
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        """NoneBot 关闭时释放长期 HTTP client，避免 reload/退出时留下连接资源。"""
        if not self._client.is_closed:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        fallback=None,
    ):
        url = f"{self.base_url}{path}"
        normalized_params = {key: value for key, value in (params or {}).items() if value is not None}
        normalized_json = {key: value for key, value in (json_body or {}).items() if value is not None}
        try:
            response = await self._client.request(
                method,
                path,
                params=normalized_params,
                json=normalized_json,
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None
        except Exception as error:
            logger.error(f"rollpig cloud 请求失败: {method} {url} error={error}")
            if not self.strict_mode and fallback is not None:
                return fallback
            raise CloudStoreError(str(error)) from error

    async def get_daily_roll(self, user_id: str, date_str: Optional[str] = None) -> Optional[str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/by-date",
            params={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
            fallback={"pig_id": None},
        )
        return payload.get("pig_id") if payload else None

    async def get_daily_rolls(self, date_str: Optional[str] = None) -> dict[str, str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/all",
            params={"date_str": date_str or rollpig_date_str()},
            fallback={"items": []},
        )
        items = payload.get("items", []) if payload else []
        return {
            str(item.get("user_id")): str(item.get("pig_id"))
            for item in items
            if item.get("user_id") and item.get("pig_id")
        }

    async def get_or_create_daily_roll(
        self,
        user_id: str,
        proposed_pig_id: str,
        date_str: Optional[str] = None,
        group_id: str = "",
    ) -> DailyRollResult:
        payload = await self._request(
            "POST",
            "/v1/daily-rolls/get-or-create",
            json_body={
                "user_id": user_id,
                "proposed_pig_id": proposed_pig_id,
                "date_str": date_str or rollpig_date_str(),
                "group_id": group_id,
            },
        )
        return DailyRollResult(
            pig_id=str(payload["pig_id"]),
            created=bool(payload.get("created")),
            is_new_pig=bool(payload.get("is_new_pig")),
            previous_copies=int(payload.get("previous_copies") or 0),
            copies=int(payload.get("copies") or 0),
            previous_duplicate_streak=int(payload.get("previous_duplicate_streak") or 0),
            duplicate_streak=int(payload.get("duplicate_streak") or 0),
        )

    async def get_draw_state(self, user_id: str) -> DrawState:
        payload = await self._request(
            "GET",
            "/v1/draw-state",
            params={"user_id": user_id},
            fallback={"pig_ids": [], "progress": {}, "duplicate_streak": 0},
        )
        progress_payload = payload.get("progress", {}) if payload else {}
        progress: dict[str, PigProgress] = {}
        if isinstance(progress_payload, dict):
            for pig_id, item in progress_payload.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=int(item.get("copies") or 0),
                    first_obtained_at=item.get("first_obtained_at"),
                )
        pig_ids = [str(item) for item in payload.get("pig_ids", [])] if payload else []
        return DrawState(
            pig_ids=pig_ids,
            progress=progress,
            duplicate_streak=int(payload.get("duplicate_streak") or 0) if payload else 0,
        )

    async def mark_group_roll_seen(
        self,
        user_id: str,
        pig_id: str,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> None:
        await self._request(
            "POST",
            "/v1/group-rolls/mark-seen",
            json_body={
                "group_id": group_id,
                "user_id": user_id,
                "pig_id": pig_id,
                "date_str": date_str or rollpig_date_str(),
            },
        )

    async def get_group_rolls(self, group_id: str, date_str: Optional[str] = None) -> dict[str, str]:
        payload = await self._request(
            "GET",
            "/v1/group-rolls",
            params={"group_id": group_id, "date_str": date_str or rollpig_date_str()},
            fallback={"items": []},
        )
        items = payload.get("items", []) if payload else []
        return {
            str(item.get("user_id")): str(item.get("pig_id"))
            for item in items
            if item.get("user_id") and item.get("pig_id")
        }

    async def get_user_collection(self, user_id: str) -> list[str]:
        payload = await self._request(
            "GET",
            "/v1/collections",
            params={"user_id": user_id},
            fallback={"pig_ids": []},
        )
        return [str(item) for item in payload.get("pig_ids", [])] if payload else []

    async def get_pig_by_date(self, user_id: str, date_str: str) -> Optional[str]:
        payload = await self._request(
            "GET",
            "/v1/daily-rolls/by-date",
            params={"user_id": user_id, "date_str": date_str},
            fallback={"pig_id": None},
        )
        return payload.get("pig_id") if payload else None

    async def consume_roast_cooldown(
        self,
        user_id: str,
        now_ts: Optional[float] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> CooldownConsumeResult:
        payload = await self._request(
            "POST",
            "/v1/cooldowns/consume-roast",
            json_body={
                "user_id": user_id,
                "now_ts": now_ts,
                "cooldown_seconds": cooldown_seconds,
                "max_charges": max_charges,
            },
        )
        return CooldownConsumeResult(
            allowed=bool(payload.get("allowed")),
            remaining_seconds=int(payload.get("remaining_seconds", 0)),
            charges_left=int(payload.get("charges_left", 0)),
            max_charges=int(payload.get("max_charges", max_charges or 1)),
            next_recover_seconds=int(payload.get("next_recover_seconds", payload.get("remaining_seconds", 0))),
        )

    async def get_catalog_snapshot(self, user_id: str, days: int = 14) -> CatalogSnapshot:
        payload = await self._request(
            "GET",
            "/v1/catalog-snapshot",
            params={"user_id": user_id, "days": days},
            fallback={"pig_ids": [], "progress": {}, "duplicate_streak": 0, "recent_rolls": [], "roasted_7d": 0},
        )
        progress_payload = payload.get("progress", {})
        progress: dict[str, PigProgress] = {}
        if isinstance(progress_payload, dict):
            for pig_id, item in progress_payload.items():
                if not isinstance(item, dict):
                    continue
                progress[str(pig_id)] = PigProgress(
                    copies=int(item.get("copies") or 0),
                    first_obtained_at=item.get("first_obtained_at"),
                )

        recent_rolls: dict[str, str] = {}
        for item in payload.get("recent_rolls", []) or []:
            if not isinstance(item, dict):
                continue
            date_str = str(item.get("date_str") or "")
            pig_id = str(item.get("pig_id") or "")
            if date_str and pig_id:
                recent_rolls[date_str] = pig_id

        return CatalogSnapshot(
            draw_state=DrawState(
                pig_ids=[str(item) for item in payload.get("pig_ids", []) or []],
                progress=progress,
                duplicate_streak=int(payload.get("duplicate_streak") or 0),
            ),
            recent_rolls=recent_rolls,
            roasted_7d=int(payload.get("roasted_7d") or payload.get("roast_events_7d") or 0),
        )

    async def consume_force_usage(self, user_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._request(
            "POST",
            "/v1/cooldowns/consume-force",
            json_body={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
        )
        return bool(payload.get("allowed"))

    async def append_roast_event(self, event: RoastEvent) -> None:
        await self._request(
            "POST",
            "/v1/events",
            json_body={
                "event_type": event.event_type,
                "attacker_id": event.attacker_id,
                "target_id": event.target_id,
                "attacker_name": event.attacker_name,
                "target_name": event.target_name,
                "food": event.food,
                "group_id": event.group_id,
                "date_str": rollpig_date_str(),
            },
        )

    async def list_daily_events(self, date_str: Optional[str] = None, group_id: Optional[str] = None) -> list[dict]:
        payload = await self._request(
            "GET",
            "/v1/events",
            params={"date_str": date_str or rollpig_date_str(), "group_id": group_id},
            fallback={"items": []},
        )
        return payload.get("items", []) if payload else []

    async def get_active_group_ids(self, date_str: Optional[str] = None) -> set[str]:
        payload = await self._request(
            "GET",
            "/v1/groups/active",
            params={"date_str": date_str or rollpig_date_str()},
            fallback={"group_ids": []},
        )
        return {str(group_id) for group_id in payload.get("group_ids", [])} if payload else set()

    async def replace_group_protections(
        self,
        group_id: str,
        user_ids: list[str],
        protect_date: Optional[str] = None,
    ) -> None:
        await self._request(
            "POST",
            "/v1/protections/replace-group",
            json_body={
                "group_id": group_id,
                "user_ids": user_ids,
                "protect_date": protect_date or rollpig_date_str(1),
            },
        )

    async def is_protected(self, group_id: str, user_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._request(
            "GET",
            "/v1/protections/check",
            params={
                "group_id": group_id,
                "user_id": user_id,
                "protect_date": date_str or rollpig_date_str(),
            },
            fallback={"protected": False},
        )
        return bool(payload.get("protected")) if payload else False

    async def prune_history(self, days_to_keep: int = 14) -> None:
        return None

    async def prune_events(self, days_to_keep: int = 7) -> None:
        return None
