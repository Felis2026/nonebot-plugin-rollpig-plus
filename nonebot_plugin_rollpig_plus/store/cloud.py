from __future__ import annotations

from typing import Optional

import httpx
from nonebot.log import logger

from ..config import plugin_config
from ..runtime import rollpig_date_str
from .base import RollpigStore
from .models import (
    CatalogSnapshot,
    CooldownConsumeResult,
    DailyRollResult,
    DrawState,
    GroupRoastRefillCompleteResult,
    GroupRoastRefillPrepareResult,
    GroupRoastRefillRequest,
    PigProgress,
    RoastEvent,
    RoastReservation,
    RoastReservationClaimResult,
    RoastReservationParticipant,
    RoastReservationPrepareResult,
    UnrolledRoastAttemptResult,
)


class CloudStoreError(RuntimeError):
    pass


class CloudReservationUnsupportedError(CloudStoreError):
    """旧版 Cloud 没有预约接口时抛出，handler 可只降级预约场景。"""


class CloudRoastRefillUnsupportedError(CloudStoreError):
    """旧版 Cloud 没有补货接口时抛出，不影响其他 RollPig 功能。"""


class CloudStore(RollpigStore):
    def __init__(self):
        if not plugin_config.rollpig_cloud_api_url:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_api_url")
        if not plugin_config.rollpig_cloud_token:
            raise ValueError("启用 cloud 存储时必须配置 rollpig_cloud_token")

        self.base_url = plugin_config.rollpig_cloud_api_url.rstrip("/")
        # CloudStore 位于命令主链路，等待过久会直接拖住用户响应；有效范围固定为 0.5～60 秒。
        self.timeout = min(60.0, max(0.5, float(plugin_config.rollpig_cloud_timeout or 5.0)))
        self.strict_mode = bool(plugin_config.rollpig_cloud_strict_mode)
        self.headers = {
            "Authorization": f"Bearer {plugin_config.rollpig_cloud_token}",
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

    async def _reservation_request(self, method: str, path: str, **kwargs):
        """预约接口使用独立兼容错误，避免旧 Cloud 拖垮正常烧烤。"""

        try:
            return await self._request(method, path, **kwargs)
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudReservationUnsupportedError(str(error)) from error
            raise

    async def _refill_request(self, method: str, path: str, **kwargs):
        """补货接口独立兼容降级，避免新插件连接旧 Cloud 时扩大故障面。"""

        try:
            return await self._request(method, path, **kwargs)
        except CloudStoreError as error:
            cause = error.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {404, 405}:
                raise CloudRoastRefillUnsupportedError(str(error)) from error
            raise

    @staticmethod
    def _parse_reservation(payload: dict | None) -> Optional[RoastReservation]:
        if not isinstance(payload, dict) or not payload.get("reservation_id"):
            return None
        participants = tuple(
            RoastReservationParticipant(
                user_id=str(item.get("user_id") or ""),
                display_name=str(item.get("display_name") or ""),
                pig_id=str(item.get("pig_id") or ""),
            )
            for item in payload.get("participants", [])
            if isinstance(item, dict) and item.get("user_id")
        )
        return RoastReservation(
            reservation_id=str(payload["reservation_id"]),
            date_str=str(payload.get("date_str") or ""),
            group_id=str(payload.get("group_id") or ""),
            target_id=str(payload.get("target_id") or ""),
            target_name=str(payload.get("target_name") or ""),
            owner_id=str(payload.get("owner_id") or ""),
            owner_name=str(payload.get("owner_name") or ""),
            owner_pig_id=str(payload.get("owner_pig_id") or ""),
            participants=participants,
            delivery_bot_id=str(payload.get("delivery_bot_id") or ""),
            force_mode=payload.get("force_mode"),
            status=str(payload.get("status") or "pending"),
            target_pig_id=str(payload.get("target_pig_id") or ""),
            outcome_snapshot=(dict(payload["outcome_snapshot"]) if isinstance(payload.get("outcome_snapshot"), dict) else None),
            claim_token=str(payload.get("claim_token") or ""),
        )

    @staticmethod
    def _parse_refill(payload: dict | None) -> Optional[GroupRoastRefillRequest]:
        if not isinstance(payload, dict) or not payload.get("request_id"):
            return None
        return GroupRoastRefillRequest(
            request_id=str(payload["request_id"]),
            date_str=str(payload.get("date_str") or ""),
            group_id=str(payload.get("group_id") or ""),
            initiator_id=str(payload.get("initiator_id") or ""),
            initiator_name=str(payload.get("initiator_name") or ""),
            delivery_bot_id=str(payload.get("delivery_bot_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            active_count_snapshot=int(payload.get("active_count_snapshot") or 0),
            required_ratio=int(payload.get("required_ratio") or 25),
            required_votes=int(payload.get("required_votes") or 2),
            success_count_before=int(payload.get("success_count_before") or 0),
            status=str(payload.get("status") or "voting"),
            created_at=str(payload.get("created_at") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            completed_at=str(payload.get("completed_at") or ""),
            benefited_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("benefited_user_ids", []) if user_id
            })),
            failure_reason=str(payload.get("failure_reason") or ""),
        )

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

    # ================================ 事件与预约完成 ================================ #

    @staticmethod
    def _roast_event_payload(event: RoastEvent, *, date_str: str) -> dict:
        """生成 Cloud 事件负载；预约完成可显式沿用预约所属业务日期。"""

        return {
            "event_type": event.event_type,
            "attacker_id": event.attacker_id,
            "target_id": event.target_id,
            "attacker_name": event.attacker_name,
            "target_name": event.target_name,
            "food": event.food,
            "group_id": event.group_id,
            "date_str": date_str,
            "reservation_id": event.reservation_id,
            "participant_ids": list(event.participant_ids),
            "participant_names": list(event.participant_names),
            "participant_count": event.participant_count,
            "backfire_victim_id": event.backfire_victim_id,
            "backfire_victim_name": event.backfire_victim_name,
        }

    async def _append_roast_event(self, event: RoastEvent, *, date_str: str) -> None:
        await self._request(
            "POST",
            "/v1/events",
            json_body=self._roast_event_payload(event, date_str=date_str),
        )

    async def append_roast_event(self, event: RoastEvent) -> None:
        await self._append_roast_event(event, date_str=rollpig_date_str())

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

    async def record_unrolled_roast_attempt(
        self, user_id: str, date_str: Optional[str] = None
    ) -> UnrolledRoastAttemptResult:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/unrolled-attempt",
            json_body={"user_id": user_id, "date_str": date_str or rollpig_date_str()},
        )
        return UnrolledRoastAttemptResult(str(payload["date_str"]), str(payload["user_id"]), int(payload["count"]))

    async def prepare_roast_reservation(
        self,
        *,
        attacker_id: str,
        attacker_name: str,
        attacker_pig_id: str,
        target_id: str,
        target_name: str,
        group_id: str,
        delivery_bot_id: str,
        force_mode: Optional[str] = None,
        date_str: Optional[str] = None,
        cooldown_seconds: Optional[int] = None,
        max_charges: Optional[int] = None,
    ) -> RoastReservationPrepareResult:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/prepare",
            json_body={
                "attacker_id": attacker_id,
                "attacker_name": attacker_name,
                "attacker_pig_id": attacker_pig_id,
                "target_id": target_id,
                "target_name": target_name,
                "group_id": group_id,
                "delivery_bot_id": delivery_bot_id,
                "force_mode": force_mode,
                "date_str": date_str or rollpig_date_str(),
                "cooldown_seconds": cooldown_seconds,
                "max_charges": max_charges,
            },
        )
        cooldown_payload = payload.get("cooldown") if isinstance(payload, dict) else None
        cooldown = None
        if isinstance(cooldown_payload, dict):
            cooldown = CooldownConsumeResult(
                allowed=bool(cooldown_payload.get("allowed")),
                remaining_seconds=int(cooldown_payload.get("remaining_seconds") or 0),
                charges_left=int(cooldown_payload.get("charges_left") or 0),
                max_charges=int(cooldown_payload.get("max_charges") or 1),
                next_recover_seconds=int(cooldown_payload.get("next_recover_seconds") or 0),
            )
        return RoastReservationPrepareResult(
            status=str(payload.get("status") or "error"),
            reservation=self._parse_reservation(payload.get("reservation")),
            cooldown=cooldown,
            target_pig_id=str(payload.get("target_pig_id") or ""),
            protection_broken=bool(payload.get("protection_broken")),
        )

    async def claim_roast_reservations(
        self,
        delivery_bot_id: str,
        date_str: Optional[str] = None,
        excluded_reservation_ids: Optional[set[str]] = None,
    ) -> RoastReservationClaimResult:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/claim",
            json_body={
                "delivery_bot_id": delivery_bot_id,
                "date_str": date_str or rollpig_date_str(),
                "supports_prepared": True,
                "excluded_reservation_ids": sorted(excluded_reservation_ids or ()),
            },
        )
        return RoastReservationClaimResult(
            tuple(
                reservation
                for item in payload.get("items", [])
                if (reservation := self._parse_reservation(item)) is not None
            ),
            # 旧 Cloud 没有 has_owned；若已经返回项目，至少保留本轮 Owner 状态，
            # 避免安全降级期间过早停止后续恢复。
            has_owned=bool(payload.get("has_owned", payload.get("items"))),
        )

    async def has_owned_roast_reservations(self, delivery_bot_id: str, date_str: Optional[str] = None) -> bool:
        payload = await self._reservation_request(
            "GET",
            "/v1/roast-reservations/owned",
            params={"delivery_bot_id": delivery_bot_id, "date_str": date_str or rollpig_date_str()},
        )
        return bool(payload.get("has_owned"))

    async def save_roast_reservation_outcome(
        self, reservation: RoastReservation, outcome_snapshot: dict
    ) -> Optional[RoastReservation]:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/outcome/prepare",
            json_body={
                "reservation_id": reservation.reservation_id,
                "claim_token": reservation.claim_token,
                "outcome_snapshot": outcome_snapshot,
            },
        )
        updated = self._parse_reservation(payload.get("reservation"))
        if updated and reservation.claim_token:
            return RoastReservation(**{**updated.__dict__, "claim_token": reservation.claim_token})
        return updated

    async def mark_roast_reservation_sending(
        self,
        reservation: RoastReservation,
    ) -> Optional[RoastReservation]:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/sending",
            json_body={
                "reservation_id": reservation.reservation_id,
                "claim_token": reservation.claim_token,
            },
        )
        updated = self._parse_reservation(payload.get("reservation"))
        if updated and reservation.claim_token:
            return RoastReservation(**{**updated.__dict__, "claim_token": reservation.claim_token})
        return updated

    async def complete_roast_reservation(
        self,
        reservation: RoastReservation,
        event: RoastEvent | None = None,
    ) -> bool:
        request_body = {
            "reservation_id": reservation.reservation_id,
            "claim_token": reservation.claim_token,
        }
        if event is not None:
            request_body["event"] = self._roast_event_payload(
                event,
                date_str=reservation.date_str,
            )
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/complete",
            json_body=request_body,
        )
        if not payload.get("ok"):
            return False
        if event is not None and not payload.get("event_recorded"):
            # 旧 Cloud 会忽略新增 event 字段；保持兼容时退回旧的独立事件接口。
            await self._append_roast_event(event, date_str=reservation.date_str)
        return True

    async def release_roast_reservation(self, reservation: RoastReservation) -> bool:
        payload = await self._reservation_request(
            "POST",
            "/v1/roast-reservations/release",
            json_body={"reservation_id": reservation.reservation_id, "claim_token": reservation.claim_token},
        )
        return bool(payload.get("ok"))

    # ================================ 烤箱补货 ================================ #

    async def mark_group_active_users(
        self,
        group_id: str,
        user_ids: list[str],
        date_str: Optional[str] = None,
    ) -> None:
        await self._refill_request(
            "POST",
            "/v1/group-roast-refills/active-users/mark",
            json_body={
                "group_id": group_id,
                "user_ids": user_ids,
                "date_str": date_str or rollpig_date_str(),
            },
        )

    async def get_group_active_user_ids(
        self,
        group_id: str,
        date_str: Optional[str] = None,
    ) -> set[str]:
        payload = await self._refill_request(
            "GET",
            "/v1/group-roast-refills/active-users",
            params={"group_id": group_id, "date_str": date_str or rollpig_date_str()},
        )
        return {str(user_id) for user_id in payload.get("user_ids", []) if user_id}

    async def prepare_group_roast_refill(
        self,
        *,
        group_id: str,
        initiator_id: str,
        initiator_name: str,
        delivery_bot_id: str,
        eligible_user_ids: Optional[list[str]] = None,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> GroupRoastRefillPrepareResult:
        json_body = {
            "group_id": group_id,
            "initiator_id": initiator_id,
            "initiator_name": initiator_name,
            "delivery_bot_id": delivery_bot_id,
            "date_str": date_str or rollpig_date_str(),
            "now_ts": now_ts,
        }
        if eligible_user_ids is not None:
            # 新 Cloud 会据此冻结群成员交集；旧 Cloud 默认忽略未知字段，调用方
            # 仍会校验响应快照，绝不静默接受未过滤门槛。
            json_body["eligible_user_ids"] = eligible_user_ids
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/prepare",
            json_body=json_body,
        )
        return GroupRoastRefillPrepareResult(
            status=str(payload.get("status") or "error"),
            request=self._parse_refill(payload.get("request")),
            active_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("active_user_ids", []) if user_id
            })),
        )

    async def bind_group_roast_refill_message(
        self,
        request_id: str,
        message_id: str,
    ) -> Optional[GroupRoastRefillRequest]:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/bind-message",
            json_body={"request_id": request_id, "message_id": message_id},
        )
        return self._parse_refill(payload.get("request"))

    async def get_group_roast_refill(
        self,
        group_id: str,
        date_str: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[GroupRoastRefillRequest]:
        payload = await self._refill_request(
            "GET",
            "/v1/group-roast-refills/active",
            params={
                "group_id": group_id,
                "date_str": date_str or rollpig_date_str(),
                "now_ts": now_ts,
            },
        )
        return self._parse_refill(payload.get("request"))

    async def fail_group_roast_refill(
        self,
        request_id: str,
        message_id: str,
        reason: str,
    ) -> bool:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/fail",
            json_body={"request_id": request_id, "message_id": message_id, "reason": reason},
        )
        return bool(payload.get("allowed"))

    async def complete_group_roast_refill(
        self,
        *,
        request_id: str,
        message_id: str,
        voter_ids: list[str],
        excluded_user_ids: list[str],
        max_charges: int = 2,
        now_ts: Optional[float] = None,
    ) -> GroupRoastRefillCompleteResult:
        payload = await self._refill_request(
            "POST",
            "/v1/group-roast-refills/complete",
            json_body={
                "request_id": request_id,
                "message_id": message_id,
                "voter_ids": voter_ids,
                "excluded_user_ids": excluded_user_ids,
                "max_charges": max_charges,
                "now_ts": now_ts,
            },
        )
        return GroupRoastRefillCompleteResult(
            completed=bool(payload.get("completed")),
            status=str(payload.get("status") or "error"),
            request=self._parse_refill(payload.get("request")),
            valid_voter_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("valid_voter_ids", []) if user_id
            })),
            benefited_user_ids=tuple(sorted({
                str(user_id) for user_id in payload.get("benefited_user_ids", []) if user_id
            })),
        )
