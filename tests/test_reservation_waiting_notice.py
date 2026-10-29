from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_roast_reservations as fixtures
from nonebot_plugin_rollpig_plus.store.cloud import CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import (
    RoastReservation, RoastReservationClaimResult, RoastReservationParticipant,
)


class WaitingNoticeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = fixtures.reservation_flow
        self.flow._group_backoff.clear()
        self.flow._resource_backoff_until.clear()
        self.item = RoastReservation(
            reservation_id="waiting", date_str="2026-10-27", group_id="100", target_id="2",
            target_name="目标", target_pig_id="pig", owner_id="3", owner_name="主厨", owner_pig_id="pig",
            delivery_bot_id="bot", claim_token="notice-token", protection_required=2, protection_source="manual",
            participants=(RoastReservationParticipant(user_id="3", display_name="主厨", pig_id="pig"),),
        )
        self.bot = SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 123}))
        self.store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(return_value=RoastReservationClaimResult(
                waiting_notices=(self.item,), has_owned=True,
            )),
            transition_roast_waiting_notice=AsyncMock(return_value=True),
            bind_roast_reservation_message=AsyncMock(return_value=True),
        )

    async def deliver(self, enabled=True):
        """调用真实投递入口，只替换存储和外部 QQ 发送。"""
        with (patch.object(self.flow, "store", self.store),
              patch.object(self.flow, "get_bots", return_value={"bot": self.bot}),
              patch.object(self.flow, "is_group_rollpig_enabled", return_value=enabled),
              patch.object(self.flow.asyncio, "sleep", new_callable=AsyncMock)):
            return await self.flow.deliver_ready_reservations("bot")

    async def test_notice_sends_existing_copy_binds_reply_and_never_opens_round(self):
        result = await self.deliver()
        self.assertEqual((result.completed, result.has_owned), (0, True))
        self.bot.send_group_msg.assert_awaited_once()
        message = str(self.bot.send_group_msg.await_args.kwargs["message"])
        self.assertIn("【目标】", message)
        self.assertIn("Lv.1", message)
        self.assertIn("1/2", message)
        self.assertNotIn("昨天", message)
        self.assertEqual([call.args[1] for call in self.store.transition_roast_waiting_notice.await_args_list], ["sending", "sent"])
        self.store.bind_roast_reservation_message.assert_awaited_once_with(
            reservation_id="waiting", bot_id="bot", group_id="100", message_id="123", date_str="2026-10-27",
        )

    async def test_disabled_group_releases_notice_without_sending(self):
        await self.deliver(enabled=False)
        self.bot.send_group_msg.assert_not_awaited()
        self.store.transition_roast_waiting_notice.assert_awaited_once_with(self.item, "release")

    async def test_stale_or_cancelled_notice_does_not_send(self):
        self.store.transition_roast_waiting_notice.return_value = False
        await self.deliver()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_ack_response_loss_only_retries_ack(self):
        self.store.transition_roast_waiting_notice.side_effect = [True, CloudStoreError("response lost"), True]
        await self.deliver()
        self.bot.send_group_msg.assert_awaited_once()
        self.assertEqual([call.args[1] for call in self.store.transition_roast_waiting_notice.await_args_list], ["sending", "sent", "sent"])

    async def test_external_send_error_keeps_notice_sending(self):
        self.bot.send_group_msg.side_effect = RuntimeError("unknown send result")
        await self.deliver()
        self.store.transition_roast_waiting_notice.assert_awaited_once_with(self.item, "sending")
        self.store.bind_roast_reservation_message.assert_not_awaited()

    async def test_notice_failure_does_not_escape_into_result_delivery(self):
        self.store.transition_roast_waiting_notice.side_effect = CloudStoreError("offline")
        result = await self.deliver()
        self.assertEqual((result.completed, result.has_owned), (0, True))
        self.bot.send_group_msg.assert_not_awaited()


class WaitingNoticeCloudAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_parses_notice_separately_and_transition_uses_notice_token(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(return_value={"items": [], "has_owned": True, "waiting_notices": [{
            "reservation_id": "waiting", "date_str": "2026-10-27", "group_id": "100", "target_id": "2",
            "target_name": "目标", "owner_id": "3", "owner_name": "主厨", "owner_pig_id": "pig",
            "delivery_bot_id": "bot", "status": "pending", "protection_required": 2, "claim_token": "notice-token",
        }]})
        result = await store.claim_roast_reservations("bot", "2026-10-27")
        self.assertFalse(result.reservations)
        item, = result.waiting_notices
        self.assertEqual((item.claim_token, item.protection_required), ("notice-token", 2))
        self.assertTrue(store._reservation_request.await_args.kwargs["json_body"]["supports_waiting_notice"])
        store._reservation_request.return_value = {"ok": True}
        self.assertTrue(await store.transition_roast_waiting_notice(item, "sending"))
        store._reservation_request.assert_awaited_with(
            "POST", "/v1/roast-reservations/waiting-notice",
            json_body={"reservation_id": "waiting", "claim_token": "notice-token", "action": "sending"},
        )

    async def test_old_cloud_response_remains_usable_without_waiting_notices(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(return_value={"items": [], "has_owned": True})
        result = await store.claim_roast_reservations("bot", "2026-10-27")
        self.assertEqual(result.waiting_notices, ())
        self.assertTrue(result.has_owned)
