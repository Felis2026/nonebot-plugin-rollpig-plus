from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import test_roast_reservations as fixtures
from nonebot_plugin_rollpig_plus import texts
from nonebot_plugin_rollpig_plus.handlers import control
from nonebot_plugin_rollpig_plus.roast_flow import pick_reservation_prepare_text, pick_protected_waiting_text
from nonebot_plugin_rollpig_plus.roll_flow import resolve_pig_protection_level
from nonebot_plugin_rollpig_plus.store.cloud import CloudManualProtectionUnsupportedError, CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import ProtectionGrantResult, protection_source


DATE = "2026-10-27"


class LocalManualProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        files = patch.object(fixtures.data_manager_module, "DATA_FILE", Path(self.temporary.name) / "data.json")
        files.start()
        self.addCleanup(files.stop)
        today = patch.object(fixtures.data_manager_module, "rollpig_date_str", return_value=DATE)
        today.start()
        self.addCleanup(today.stop)
        self.manager = fixtures.PigDataManager()

    async def prepare(self, user="3", **changes):
        """通过真实本地预约入口创建或加入场次。"""
        values = dict(attacker_id=user, attacker_name=user, attacker_pig_id="pig", target_id="2", target_name="目标",
                      group_id="100", delivery_bot_id="bot", date_str=DATE)
        values.update(changes)
        return await self.manager.prepare_roast_reservation(**values)

    async def grant(self, level=5):
        return await self.manager.grant_protection("100", "2", level, DATE)

    def details(self):
        return self.manager.data["protection_details"][DATE]["100"]["2"]

    async def test_grant_persists_and_duplicate_never_lowers(self):
        self.assertEqual(await self.grant(), ProtectionGrantResult(5, True))
        for level in (5, 1, 3):
            self.assertEqual(await self.grant(level), ProtectionGrantResult(5, False))
        reloaded = fixtures.PigDataManager()
        self.assertTrue(reloaded.is_protected("100", "2", DATE))
        self.assertFalse(reloaded.is_protected("200", "2", DATE))
        self.assertFalse(reloaded.is_protected("100", "2", "2026-10-28"))
        self.assertEqual(self.details()["source_levels"], {"manual": 5})
        self.assertEqual(self.details()["consecutive_days"], 0)

    async def arrive_for_notice(self):
        """使用正式抽猪事务使目标到场，覆盖 JSON 保存与重新加载。"""
        await self.manager.get_or_create_today_pig("2", "pig", DATE, group_id="100")

    async def test_waiting_notice_arrival_persistence_and_no_duplicate_after_send(self):
        await self.prepare()
        await self.grant(1)
        await self.arrive_for_notice()
        reloaded = fixtures.PigDataManager()
        claimed = await reloaded.claim_roast_reservations("bot", DATE)
        item, = claimed.waiting_notices
        self.assertFalse(claimed.reservations)
        self.assertEqual((item.status, item.contribution_count, item.protection_required), ("pending", 1, 2))
        message = pick_protected_waiting_text(item)
        self.assertIn("1/2", message)
        self.assertIn("Lv.1", message)
        self.assertIn("【目标】", message)
        self.assertFalse((await reloaded.claim_roast_reservations("bot", DATE)).waiting_notices)
        for action in ("sending", "sending", "sent", "sent"):
            self.assertTrue(await reloaded.transition_roast_waiting_notice(item.reservation_id, item.claim_token, action))
        await reloaded.get_or_create_today_pig("2", "pig", DATE, group_id="100")
        self.assertFalse((await reloaded.claim_roast_reservations("bot", DATE)).waiting_notices)
        self.assertEqual(reloaded.data["roast_reservations"][item.reservation_id]["status"], "pending")

    async def test_waiting_notice_lease_release_and_uncertain_send(self):
        await self.grant()
        await self.prepare()
        await self.arrive_for_notice()
        first, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        self.assertTrue(await self.manager.transition_roast_waiting_notice(first.reservation_id, first.claim_token, "release"))
        second, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertFalse(await self.manager.transition_roast_waiting_notice(first.reservation_id, first.claim_token, "sending"))
        raw = self.manager.data["roast_reservations"][first.reservation_id]
        raw["waiting_notice"]["claimed_at"] = "2000-01-01T00:00:00+00:00"
        third, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        self.assertNotEqual(second.claim_token, third.claim_token)
        self.assertTrue(await self.manager.transition_roast_waiting_notice(third.reservation_id, third.claim_token, "sending"))
        raw["waiting_notice"]["claimed_at"] = "2000-01-01T00:00:00+00:00"
        self.assertFalse((await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices)
        self.assertFalse(await self.manager.transition_roast_waiting_notice(third.reservation_id, third.claim_token, "release"))

    async def test_direct_prepare_and_later_join_do_not_duplicate_background_progress(self):
        await self.grant(2)
        await self.arrive_for_notice()
        await self.prepare()
        self.assertFalse((await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices)
        await self.grant(3)
        item, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        await self.prepare("4")
        self.assertFalse(await self.manager.transition_roast_waiting_notice(item.reservation_id, item.claim_token, "sending"))
        self.assertFalse((await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices)

    async def test_waiting_notice_owner_exclusion_and_new_threshold(self):
        await self.grant(1)
        prepared = await self.prepare()
        await self.arrive_for_notice()
        self.assertFalse((await self.manager.claim_roast_reservations("other", DATE)).waiting_notices)
        self.assertFalse((await self.manager.claim_roast_reservations("bot", DATE, {prepared.reservation.reservation_id})).waiting_notices)
        first, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        for action in ("sending", "sent"):
            await self.manager.transition_roast_waiting_notice(first.reservation_id, first.claim_token, action)
        await self.grant(1)
        self.assertFalse((await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices)
        await self.grant(3)
        second, = (await self.manager.claim_roast_reservations("bot", DATE)).waiting_notices
        self.assertEqual(second.protection_required, 4)
        await self.prepare("3", force_mode="super")
        claimed = await self.manager.claim_roast_reservations("bot", DATE)
        self.assertFalse(claimed.waiting_notices)
        self.assertEqual(len(claimed.reservations), 1)

    async def test_daily_replacement_and_manual_streak_are_independent(self):
        await self.grant()
        await self.manager.replace_group_protected_users("100", ["2", "4"], DATE)
        self.assertEqual(self.details()["source_levels"], {"manual": 5, "daily": 3})
        self.assertEqual(await self.manager.get_protection_base_level("100", "2", DATE, daily_only=True), 3)
        await self.manager.replace_group_protected_users("100", [], DATE)
        self.assertEqual(self.details()["source_levels"], {"manual": 5})
        self.assertEqual(await self.manager.get_protection_base_level("100", "2", DATE, daily_only=True), 0)
        await self.manager.replace_group_protected_users("100", ["2"], "2026-10-28")
        tomorrow = self.manager.data["protection_details"]["2026-10-28"]["100"]["2"]
        self.assertEqual((tomorrow["base_level"], tomorrow["consecutive_days"]), (3, 1))

    async def test_equal_or_lower_grant_preserves_daily_and_manual_sources(self):
        await self.manager.replace_group_protected_users("100", ["2"], DATE)
        self.assertEqual(await self.grant(3), ProtectionGrantResult(3, False))
        result = await self.prepare()
        self.assertEqual(result.reservation.protection_source, "daily")
        await self.manager.replace_group_protected_users("100", [], DATE)
        self.assertEqual(self.details()["source_levels"], {"manual": 3})
        pending = self.manager.data["roast_reservations"][result.reservation.reservation_id]
        self.assertEqual(pending["protection_source"], "manual")

    async def test_grant_does_not_double_add_breakthrough_level(self):
        await self.manager.replace_group_protected_users("100", ["2"], DATE)
        self.manager.data["roast_reservations"]["completed"] = {
            "date_str": DATE, "group_id": "100", "target_id": "2", "protection_required": 4,
            "status": "completed", "outcome_snapshot": {"event_type": "success"},
        }
        self.assertEqual(await self.grant(), ProtectionGrantResult(5, True))
        self.assertEqual(self.details()["base_level"], 5)
        self.assertEqual(self.details()["source_baselines"]["manual"], 1)
        self.assertEqual(await self.grant(), ProtectionGrantResult(5, False))

    async def test_removing_daily_source_uses_remaining_manual_level_for_pending_round(self):
        await self.manager.replace_group_protected_users("100", ["2"], DATE)
        self.details().update(base_level=5, source_levels={"daily": 5})
        await self.grant(3)
        for user in ("3", "4", "5", "6"):
            result = await self.prepare(user)
        raw = self.manager.data["roast_reservations"][result.reservation.reservation_id]
        self.manager._activate_reservation_locked(raw, "pig")
        self.assertEqual(raw["status"], "pending")
        await self.manager.replace_group_protected_users("100", [], DATE)
        self.assertEqual((raw["protection_required"], raw["protection_source"], raw["status"]), (4, "manual", "ready"))

    async def test_grant_after_old_protection_removed_starts_at_requested_level(self):
        self.manager.data["roast_reservations"]["completed"] = {
            "date_str": DATE, "group_id": "100", "target_id": "2", "protection_required": 4,
            "status": "completed", "outcome_snapshot": {"event_type": "success"},
        }
        self.assertEqual(await self.grant(1), ProtectionGrantResult(1, True))
        self.assertEqual(self.manager._protection_required_locked(DATE, "100", "2"), 2)

    async def test_grant_updates_pending_roster_without_extra_charges(self):
        created = await self.prepare()
        before = dict(self.manager.data["usage"]["3"])
        await self.grant()
        raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
        self.assertEqual((raw["protection_required"], raw["protection_source"], raw["status"]), (6, "manual", "pending"))
        self.assertEqual(self.manager.data["usage"]["3"], before)
        self.manager.data.setdefault("history", {})[DATE] = {"2": "pig"}
        joined = await self.prepare("4")
        self.assertEqual((joined.reservation.status, joined.reservation.contribution_count), ("pending", 2))

    async def test_pending_accelerated_round_retains_double_fire(self):
        created = await self.prepare(force_mode="normal")
        await self.grant()
        raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
        self.assertEqual(raw["participants"][0]["contribution"], 2)

    async def test_ready_and_super_rounds_keep_original_conditions(self):
        for index, (status, mode) in enumerate((("ready", None), ("processing", None), ("prepared", None),
                                              ("sending", None), ("completed", None), ("pending", "super"))):
            with self.subTest(status=status, mode=mode):
                self.manager.data["protected"] = {}
                self.manager.data["protection_details"] = {}
                self.manager.data["roast_reservations"] = {}
                created = await self.prepare(user=str(30 + index), force_mode=mode)
                raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
                raw["status"] = status
                await self.grant()
                self.assertEqual((raw["status"], raw["protection_required"]), (status, 0))

    async def test_invalid_levels_dates_and_ids_do_not_write(self):
        for changes in (("100", "2", 0, DATE), ("100", "2", 6, DATE), ("100", "2", True, DATE),
                        ("__all__", "2", 3, DATE), ("100", "2", 3, "2026-10-28")):
            with self.assertRaises(ValueError):
                await self.manager.grant_protection(*changes)
        self.assertFalse(self.manager.is_protected("100", "2", DATE))

    async def test_concurrent_grants_and_daily_replacement_keep_both_sources(self):
        await asyncio.gather(self.grant(), self.manager.replace_group_protected_users("100", ["2", "4"], DATE), self.grant(3))
        self.assertEqual(self.details()["source_levels"], {"manual": 5, "daily": 3})
        self.assertTrue(self.manager.is_protected("100", "4", DATE))

    async def test_legacy_level_and_streak_are_preserved_without_source_metadata(self):
        self.manager.data["protected"] = {DATE: {"100": ["2"]}}
        self.manager.data["protection_details"] = {DATE: {"100": {"2": {"base_level": 4, "consecutive_days": 2}}}}
        reservation = (await self.prepare()).reservation
        self.assertEqual((reservation.protection_required, reservation.protection_source), (5, "legacy"))
        self.assertEqual(self.details(), {"base_level": 4, "consecutive_days": 2})

    async def test_new_manual_source_ignores_old_breakthroughs_and_keeps_later_upgrades(self):
        for count in (2, 6):
            with self.subTest(count=count):
                self.manager.data["protected"] = {}
                self.manager.data["protection_details"] = {}
                self.manager.data["roast_reservations"] = {}
                await self.manager.replace_group_protected_users("100", ["2"], DATE)
                for index in range(count):
                    self.manager.data["roast_reservations"][str(index)] = {
                        "date_str": DATE, "group_id": "100", "target_id": "2", "protection_required": 4,
                        "status": "completed", "outcome_snapshot": {"event_type": "success"},
                    }
                await self.grant(1)
                await self.manager.replace_group_protected_users("100", [], DATE)
                self.assertEqual(self.manager._protection_required_locked(DATE, "100", "2"), 2)
                self.manager.data["roast_reservations"]["later"] = {
                    "date_str": DATE, "group_id": "100", "target_id": "2", "protection_required": 2,
                    "status": "completed", "outcome_snapshot": {"event_type": "success"},
                }
                self.assertEqual(await self.grant(1), ProtectionGrantResult(2, False))
                self.assertEqual(self.details()["source_baselines"]["manual"], count)
                self.assertEqual(await self.grant(3), ProtectionGrantResult(3, True))
                self.assertEqual(self.details()["source_baselines"]["manual"], count + 1)

    async def test_new_daily_source_does_not_inherit_manual_breakthroughs(self):
        await self.grant(1)
        for index in range(2):
            self.manager.data["roast_reservations"][str(index)] = {
                "date_str": DATE, "group_id": "100", "target_id": "2", "protection_required": 2,
                "status": "completed", "outcome_snapshot": {"event_type": "success"},
            }
        await self.manager.replace_group_protected_users("100", ["2"], DATE)
        self.assertEqual(self.details()["source_baselines"]["daily"], 2)
        self.assertEqual(self.manager._protection_required_locked(DATE, "100", "2"), 4)

    async def test_removing_last_source_restores_ordinary_reservation_before_or_after_arrival(self):
        for index, arrived in enumerate((False, True)):
            with self.subTest(arrived=arrived):
                self.manager.data["history"] = {}
                self.manager.data["roast_reservations"] = {}
                created = await self.prepare(user=str(30 + index))
                await self.manager.replace_group_protected_users("100", ["2"], DATE)
                if arrived:
                    await self.manager.get_or_create_today_pig("2", "pig", DATE, group_id="100")
                raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
                self.assertEqual(raw["status"], "pending")
                await self.manager.replace_group_protected_users("100", [], DATE)
                self.assertFalse(self.manager.is_protected("100", "2", DATE))
                self.assertEqual(raw["protection_required"], 0)
                self.assertEqual(raw["status"], "ready" if arrived else "pending")
                await self.manager.get_or_create_today_pig("2", "pig", DATE, group_id="100")
                self.assertEqual(raw["status"], "ready")

    async def test_removing_last_source_does_not_rewrite_fixed_or_sending_results(self):
        for index, status in enumerate(("processing", "prepared", "sending")):
            with self.subTest(status=status):
                self.manager.data["roast_reservations"] = {}
                created = await self.prepare(user=str(50 + index))
                await self.manager.replace_group_protected_users("100", ["2"], DATE)
                raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
                raw.update(status=status, outcome_snapshot={"event_type": "success"}, claim_token="token")
                await self.manager.replace_group_protected_users("100", [], DATE)
                self.assertEqual((raw["status"], raw["protection_required"], raw["claim_token"]), (status, 4, "token"))
                self.assertEqual(raw["outcome_snapshot"], {"event_type": "success"})

    async def test_reserved_pig_source_survives_daily_replacement_without_affecting_streak(self):
        await self.manager.replace_group_protected_users("100", ["2"], DATE)
        self.details().update(base_level=4, source_levels={"daily": 3, "pig": 4})
        await self.manager.replace_group_protected_users("100", [], DATE)
        self.assertEqual(self.details()["source_levels"], {"pig": 4})
        self.assertEqual(await self.manager.get_protection_base_level("100", "2", DATE, daily_only=True), 0)
        await self.manager.replace_group_protected_users("100", ["2"], "2026-10-28")
        tomorrow = self.manager.data["protection_details"]["2026-10-28"]["100"]["2"]
        self.assertEqual((tomorrow["base_level"], tomorrow["consecutive_days"]), (3, 1))


class ProtectionPoolTests(unittest.TestCase):
    def test_saturated_sources_tie_uses_daily_pool(self):
        self.assertEqual(protection_source({"source_levels": {"daily": 3, "manual": 4}}, successes=2), "daily")

    def test_daily_and_generic_sources_use_separate_openings(self):
        for source in ("daily", "manual", "pig", "legacy"):
            reservation = SimpleNamespace(protection_required=4, force_mode=None, status="pending", target_pig_id="",
                                          contribution_count=1, participants=[], protection_source=source)
            with patch("nonebot_plugin_rollpig_plus.roast_flow.random.choice", side_effect=lambda pool: pool[0]):
                message = pick_reservation_prepare_text(SimpleNamespace(status="reservation_created", reservation=reservation),
                                                       attacker_name="主厨", target_name="目标")
            self.assertEqual("昨天被烤太惨了" in message, source == "daily")
            self.assertIn("1/4", message)
        for template in texts.GENERAL_PROTECTION_BLOCK_TEXTS:
            self.assertNotIn("昨天", template)
            self.assertNotIn("昨日", template)


class CloudManualProtectionClientTests(unittest.IsolatedAsyncioTestCase):
    def test_old_reservation_without_source_retains_its_protection_threshold(self):
        reservation = CloudStore._parse_reservation({
            "reservation_id": "old", "date_str": DATE, "group_id": "100", "target_id": "2",
            "owner_id": "3", "owner_pig_id": "pig", "status": "pending", "protection_required": 4,
        })
        self.assertEqual((reservation.protection_required, reservation.protection_source), (4, "legacy"))

    async def test_grant_parses_only_explicit_valid_success(self):
        client = object.__new__(CloudStore)
        client._request = AsyncMock(return_value={"level": 5, "changed": True})
        self.assertEqual(await client.grant_protection("100", "2", 5, DATE), ProtectionGrantResult(5, True))
        client._request.assert_awaited_once_with("POST", "/v1/protections/grant", json_body={
            "group_id": "100", "user_id": "2", "level": 5, "protect_date": DATE,
        })
        for payload in (None, {}, {"level": 0, "changed": True}, {"level": 5, "changed": "false"}):
            client._request.return_value = payload
            with self.assertRaises(CloudStoreError):
                await client.grant_protection("100", "2", 5, DATE)

    async def test_only_missing_route_is_reported_as_unsupported(self):
        client = object.__new__(CloudStore)
        for status in (404, 405, 401, 422, 500):
            cause = httpx.HTTPStatusError("failed", request=httpx.Request("POST", "https://example.invalid/grant"),
                                         response=httpx.Response(status))
            error = CloudStoreError("failed")
            error.__cause__ = cause
            client._request = AsyncMock(side_effect=error)
            expected = CloudManualProtectionUnsupportedError if status in (404, 405) else CloudStoreError
            with self.assertRaises(expected) as raised:
                await client.grant_protection("100", "2", 5, DATE)
            if status not in (404, 405):
                self.assertNotIsInstance(raised.exception, CloudManualProtectionUnsupportedError)


class ProtectionEntryBoundaryTests(unittest.TestCase):
    def test_bot_does_not_register_manual_grant_command_or_replies(self):
        self.assertFalse(hasattr(control, "cmd_grant_protection"))
        self.assertFalse(any(name.startswith("GRANT_PROTECTION_") for name in vars(texts)))

    def test_pig_protection_entry_is_reserved_without_enabling_rules(self):
        for level in range(6):
            self.assertEqual(resolve_pig_protection_level({"id": "pig"}, level), 0)
        self.assertEqual(protection_source({"source_levels": {"daily": 3, "pig": 4}}), "pig")


class ReassignedReservationNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reassigned_ready_round_delivers_without_sending_another_join_notice(self):
        preparation = SimpleNamespace(status="reservation_busy", reservation=SimpleNamespace(
            status="ready", delivery_bot_id="new", reservation_id="round",
        ))
        matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
        event = SimpleNamespace(self_id="new", message_id=1, group_id=100)
        with patch.object(fixtures.roast_handler, "deliver_newly_ready_reservations", new=AsyncMock()) as deliver:
            await fixtures.roast_handler._send_reservation_notice(matcher, event, preparation,
                                                                attacker_name="主厨", target_name="目标")
        deliver.assert_awaited_once_with("new")
        matcher.send.assert_not_awaited()
        matcher.finish.assert_awaited_once_with()
