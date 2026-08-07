from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from nonebot.plugin import get_plugin

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus import reservation_flow
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.handlers import roast as roast_handler
from nonebot_plugin_rollpig_plus.roast_flow import RoastOutcome
from nonebot_plugin_rollpig_plus.store.cloud import CloudReservationUnsupportedError, CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import (
    RoastReservation,
    RoastReservationParticipant,
    UnrolledRoastAttemptResult,
)


class LocalRoastReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "pig_data.json"
        self.data_file_patch = patch.object(data_manager_module, "DATA_FILE", self.data_file)
        self.data_file_patch.start()
        self.addCleanup(self.data_file_patch.stop)
        self.manager = PigDataManager()

    async def _prepare(self, attacker_id: str = "a", **overrides):
        payload = {
            "attacker_id": attacker_id,
            "attacker_name": attacker_id.upper(),
            "attacker_pig_id": f"pig-{attacker_id}",
            "target_id": "target",
            "target_name": "Target",
            "group_id": "100",
            "delivery_bot_id": "bot-1",
            "date_str": "2026-08-07",
            "cooldown_seconds": 3600,
            "max_charges": 2,
        }
        payload.update(overrides)
        return await self.manager.prepare_roast_reservation(**payload)

    async def test_migration_and_unrolled_attempts_are_date_scoped(self):
        self.assertIn("unrolled_roast_attempts", self.manager.data)
        self.assertIn("roast_reservations", self.manager.data)

        first = await self.manager.record_unrolled_roast_attempt("a", "2026-08-07")
        second = await self.manager.record_unrolled_roast_attempt("a", "2026-08-07")
        third = await self.manager.record_unrolled_roast_attempt("a", "2026-08-07")
        next_day = await self.manager.record_unrolled_roast_attempt("a", "2026-08-08")

        self.assertEqual((first.count, second.count, third.count, next_day.count), (1, 2, 3, 1))

        self.assertEqual(self.manager.data["history"], {})
        self.assertEqual(self.manager.data["collection"], {})
        self.assertEqual(self.manager.data["pig_progress"], {})
        self.assertEqual(self.manager.data["draw_state"], {})
        self.assertEqual(self.manager.data["usage"], {})
        self.assertEqual(self.manager.data["force_usage"], {})

    async def test_create_join_duplicate_and_full_do_not_double_consume(self):
        created = await self._prepare()
        self.assertEqual(created.status, "reservation_created")
        self.assertEqual(created.reservation.participant_count, 1)
        usage_after_create = dict(self.manager.data["usage"]["a"])

        duplicate = await self._prepare()
        self.assertEqual(duplicate.status, "already_joined")
        self.assertEqual(self.manager.data["usage"]["a"], usage_after_create)

        joined = await self._prepare("b")
        self.assertEqual(joined.status, "reservation_joined")
        self.assertEqual(joined.reservation.participant_count, 2)
        self.assertNotIn("b", self.manager.data["usage"])

        for index in range(3, 13):
            result = await self._prepare(f"u{index}")
            self.assertEqual(result.status, "reservation_joined")
        full = await self._prepare("u13")
        self.assertEqual(full.status, "reservation_full")
        self.assertEqual(full.reservation.participant_count, 12)
        self.assertNotIn("u13", self.manager.data["usage"])

    async def test_unrolled_handler_warns_first_then_renders_temporary_food(self):
        matcher = SimpleNamespace(finish=AsyncMock())
        event = SimpleNamespace(user_id=123, message_id=456)
        attempt_results = [
            UnrolledRoastAttemptResult("2026-08-07", "123", 1),
            UnrolledRoastAttemptResult("2026-08-07", "123", 2),
        ]
        food = {"id": "food", "name": "烤乳猪", "analysis": "base"}
        with (
            patch.object(roast_handler, "store") as mocked_store,
            patch.object(roast_handler, "pick_food_pig", return_value=food),
            patch.object(roast_handler, "send_rendered_pig", new_callable=AsyncMock) as send_rendered,
            patch.object(roast_handler.random, "choice", side_effect=lambda pool: pool[0]),
        ):
            mocked_store.record_unrolled_roast_attempt = AsyncMock(side_effect=attempt_results)
            await roast_handler._finish_unrolled_attacker_attempt(
                matcher,
                event,
                "测试员",
                None,
            )
            matcher.finish.assert_awaited_once()
            send_rendered.assert_not_awaited()

            matcher.finish.reset_mock()
            await roast_handler._finish_unrolled_attacker_attempt(
                matcher,
                event,
                "测试员",
                "normal",
            )

        matcher.finish.assert_not_awaited()
        send_rendered.assert_awaited_once()
        rendered_food = send_rendered.await_args.args[2]
        self.assertEqual(rendered_food["name"], "烤乳猪")
        self.assertIn("测试员", rendered_food["analysis"])
        self.assertIn("烤乳猪", rendered_food["analysis"])

    async def test_force_reservation_consumes_only_on_first_creation(self):
        created = await self._prepare(force_mode="normal")
        self.assertEqual(created.status, "reservation_created")
        self.assertEqual(self.manager.data["force_usage"]["a"], "2026-08-07")
        self.assertNotIn("a", self.manager.data["usage"])

        joined = await self._prepare("b", force_mode="normal")
        self.assertEqual(joined.status, "reservation_joined")
        self.assertNotIn("b", self.manager.data["force_usage"])

    async def test_daily_roll_activates_once_and_outcome_survives_release(self):
        created = await self._prepare()
        reservation_id = created.reservation.reservation_id

        roll = await self.manager.get_or_create_today_pig(
            "target",
            "target-pig",
            date_str="2026-08-07",
        )
        self.assertTrue(roll.created)
        repeated = await self.manager.get_or_create_today_pig(
            "target",
            "other-pig",
            date_str="2026-08-07",
        )
        self.assertFalse(repeated.created)

        claimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-07")
        self.assertEqual(len(claimed.reservations), 1)
        reservation = claimed.reservations[0]
        self.assertEqual(reservation.reservation_id, reservation_id)
        self.assertEqual(reservation.target_pig_id, "target-pig")

        snapshot = {"event_type": "escape", "plain_text": "fixed"}
        saved = await self.manager.save_roast_reservation_outcome(
            reservation_id,
            reservation.claim_token,
            snapshot,
        )
        self.assertEqual(saved.outcome_snapshot, snapshot)
        await self.manager.save_roast_reservation_outcome(
            reservation_id,
            reservation.claim_token,
            {"event_type": "success", "plain_text": "must-not-replace"},
        )
        await self.manager.release_roast_reservation(reservation_id, reservation.claim_token)

        reclaimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-07")
        self.assertEqual(reclaimed.reservations[0].outcome_snapshot, snapshot)
        reclaimed_reservation = reclaimed.reservations[0]
        self.assertTrue(
            await self.manager.complete_roast_reservation(
                reservation_id,
                reclaimed_reservation.claim_token,
            )
        )
        self.assertTrue(
            await self.manager.complete_roast_reservation(
                reservation_id,
                reclaimed_reservation.claim_token,
            )
        )
        self.assertFalse((await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations)

    async def test_stale_local_claim_cannot_overwrite_or_release_new_claim(self):
        await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        first = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]
        await self.manager.release_roast_reservation(first.reservation_id, first.claim_token)
        second = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]

        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertIsNone(
            await self.manager.save_roast_reservation_outcome(
                first.reservation_id,
                first.claim_token,
                {"event_type": "escape"},
            )
        )
        self.assertFalse(await self.manager.release_roast_reservation(first.reservation_id, first.claim_token))
        self.assertTrue(
            await self.manager.release_roast_reservation(second.reservation_id, second.claim_token)
        )

    async def test_same_target_reservations_in_multiple_groups_activate_independently(self):
        first = await self._prepare(group_id="100", delivery_bot_id="bot-1")
        second = await self._prepare(group_id="200", delivery_bot_id="bot-2")
        self.assertNotEqual(first.reservation.reservation_id, second.reservation.reservation_id)

        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        bot_one = await self.manager.claim_roast_reservations("bot-1", "2026-08-07")
        bot_two = await self.manager.claim_roast_reservations("bot-2", "2026-08-07")
        self.assertEqual(bot_one.reservations[0].group_id, "100")
        self.assertEqual(bot_two.reservations[0].group_id, "200")

    async def test_owner_can_reserve_multiple_targets_and_restart_keeps_state(self):
        first = await self._prepare(target_id="target-1")
        second = await self._prepare(target_id="target-2")
        self.assertEqual((first.status, second.status), ("reservation_created", "reservation_created"))
        self.assertEqual(self.manager.data["usage"]["a"]["roast_charges"], 0)

        restored = PigDataManager()
        self.assertEqual(len(restored.data["roast_reservations"]), 2)

    async def test_cross_day_reservation_is_not_activated_or_claimed(self):
        await self._prepare(date_str="2026-08-07")
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-08")
        claimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-08")
        self.assertFalse(claimed.reservations)
        reservation = next(iter(self.manager.data["roast_reservations"].values()))
        self.assertEqual(reservation["status"], "pending")

    async def test_target_ready_does_not_consume_resources(self):
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        result = await self._prepare()
        self.assertEqual(result.status, "target_ready")
        self.assertEqual(result.target_pig_id, "target-pig")
        self.assertNotIn("a", self.manager.data["usage"])

    async def test_protection_blocks_creation_but_not_joining_existing_reservation(self):
        self.manager.data["protected"] = {"2026-08-07": {"100": ["target"]}}
        blocked = await self._prepare()
        self.assertEqual(blocked.status, "protected")
        self.assertNotIn("a", self.manager.data["usage"])

        forced = await self._prepare(force_mode="normal")
        self.assertEqual(forced.status, "reservation_created")
        self.assertTrue(forced.protection_broken)

        joined = await self._prepare("b")
        self.assertEqual(joined.status, "reservation_joined")
        self.assertNotIn("b", self.manager.data["usage"])


class RoastReservationOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def _reservation(self, *, force_mode=None, participants=None) -> RoastReservation:
        participants = participants or (
            RoastReservationParticipant("owner", "主厨", "owner-pig"),
            RoastReservationParticipant("helper", "帮厨", "helper-pig"),
        )
        return RoastReservation(
            reservation_id="reservation",
            date_str="2026-08-07",
            group_id="100",
            target_id="target",
            target_name="目标",
            owner_id="owner",
            owner_name="主厨",
            owner_pig_id="owner-pig",
            participants=participants,
            delivery_bot_id="bot-1",
            force_mode=force_mode,
            status="processing",
            target_pig_id="target-pig",
            claim_token="claim",
        )

    async def test_force_reservation_succeeds_and_uses_group_operator_label(self):
        expected = RoastOutcome(event_type="success", plain_text="ok")
        success = AsyncMock(return_value=expected)
        with (
            patch.object(reservation_flow, "get_pig_by_id", return_value={"id": "target-pig", "name": "目标猪"}),
            patch.object(reservation_flow, "is_human_pig", return_value=False),
            patch.object(reservation_flow, "is_food_pig", return_value=False),
            patch.object(reservation_flow, "is_eaten_pig", return_value=False),
            patch.object(reservation_flow, "is_sold_pig", return_value=False),
            patch.object(reservation_flow, "build_success_roast_outcome", success),
            patch.object(reservation_flow.random, "randint") as randint,
        ):
            result = await reservation_flow.build_reservation_outcome(
                self._reservation(force_mode="normal")
            )

        self.assertIs(result, expected)
        randint.assert_not_called()
        self.assertEqual(success.await_args.kwargs["attacker_name"], "主厨 等 2 人")

    async def test_force_reservation_still_stops_for_special_target(self):
        predicates = ("is_human_pig", "is_food_pig", "is_eaten_pig", "is_sold_pig")
        for active_predicate in predicates:
            with self.subTest(active_predicate=active_predicate):
                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(
                            reservation_flow,
                            "get_pig_by_id",
                            return_value={"id": "special", "name": "特殊猪"},
                        )
                    )
                    for predicate in predicates:
                        stack.enter_context(
                            patch.object(
                                reservation_flow,
                                predicate,
                                return_value=predicate == active_predicate,
                            )
                        )
                    randint = stack.enter_context(
                        patch.object(reservation_flow.random, "randint")
                    )
                    success = stack.enter_context(
                        patch.object(
                            reservation_flow,
                            "build_success_roast_outcome",
                            new_callable=AsyncMock,
                        )
                    )
                    result = await reservation_flow.build_reservation_outcome(
                        self._reservation(force_mode="normal")
                    )

                self.assertEqual(result.event_type, "reserved_special")
                randint.assert_not_called()
                success.assert_not_awaited()

    async def test_normal_probability_branches_and_random_backfire_victim(self):
        target = {"id": "target-pig", "name": "目标猪"}
        victim = {"id": "helper-pig", "name": "帮厨猪"}
        success_result = RoastOutcome(event_type="success", plain_text="success")
        backfire_result = RoastOutcome(event_type="backfire", plain_text="backfire")

        with (
            patch.object(reservation_flow, "get_pig_by_id", side_effect=lambda pig_id: target if pig_id == "target-pig" else victim),
            patch.object(reservation_flow, "is_human_pig", return_value=False),
            patch.object(reservation_flow, "is_food_pig", return_value=False),
            patch.object(reservation_flow, "is_eaten_pig", return_value=False),
            patch.object(reservation_flow, "is_sold_pig", return_value=False),
            patch.object(reservation_flow, "build_success_roast_outcome", new_callable=AsyncMock, return_value=success_result),
            patch.object(reservation_flow, "build_backfire_roast_outcome", new_callable=AsyncMock, return_value=backfire_result) as backfire,
        ):
            with patch.object(reservation_flow.random, "randint", return_value=60):
                self.assertEqual((await reservation_flow.build_reservation_outcome(self._reservation())).event_type, "success")
            with patch.object(reservation_flow.random, "randint", return_value=61):
                self.assertEqual((await reservation_flow.build_reservation_outcome(self._reservation())).event_type, "escape")
            with patch.object(reservation_flow.random, "randint", return_value=90):
                self.assertEqual((await reservation_flow.build_reservation_outcome(self._reservation())).event_type, "escape")

            def choose(sequence):
                if sequence and isinstance(sequence[0], RoastReservationParticipant):
                    return sequence[1]
                return sequence[0]

            with (
                patch.object(reservation_flow.random, "randint", return_value=91),
                patch.object(reservation_flow.random, "choice", side_effect=choose),
            ):
                self.assertEqual((await reservation_flow.build_reservation_outcome(self._reservation())).event_type, "backfire")

        self.assertEqual(backfire.await_args.args[0], victim)
        self.assertEqual(backfire.await_args.kwargs["attacker_name"], "帮厨")

    async def test_complete_after_send_retries_without_releasing(self):
        reservation = self._reservation()
        complete = AsyncMock(side_effect=[CloudStoreError("timeout"), True])
        with (
            patch.object(reservation_flow, "store") as mocked_store,
            patch.object(reservation_flow.asyncio, "sleep", new_callable=AsyncMock),
        ):
            mocked_store.complete_roast_reservation = complete
            self.assertTrue(await reservation_flow._complete_after_send(reservation))

        self.assertEqual(complete.await_count, 2)
        mocked_store.release_roast_reservation.assert_not_called()


class CloudReservationCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_cloud_missing_endpoint_raises_compatibility_error(self):
        request = httpx.Request("POST", "https://example.invalid/v1/roast-reservations/prepare")
        response = httpx.Response(404, request=request)
        status_error = httpx.HTTPStatusError("missing", request=request, response=response)
        store = object.__new__(CloudStore)
        store._request = AsyncMock(side_effect=CloudStoreError("missing"))
        store._request.side_effect.__cause__ = status_error

        with self.assertRaises(CloudReservationUnsupportedError):
            await store._reservation_request("POST", "/v1/roast-reservations/prepare")


if __name__ == "__main__":
    unittest.main()
