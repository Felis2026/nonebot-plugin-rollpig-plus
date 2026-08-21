from __future__ import annotations

import datetime
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.plugin import get_plugin
from nonebot.rule import CommandRule, TrieRule

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus import helpers
from nonebot_plugin_rollpig_plus import reservation_delivery
from nonebot_plugin_rollpig_plus import reservation_flow
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.handlers import collection as collection_handler
from nonebot_plugin_rollpig_plus.handlers import control as control_handler
from nonebot_plugin_rollpig_plus.handlers import refill as refill_handler
from nonebot_plugin_rollpig_plus.handlers import roast as roast_handler
from nonebot_plugin_rollpig_plus.handlers import roll as roll_handler
from nonebot_plugin_rollpig_plus.roll_flow import build_pigsty_growth_summary
from nonebot_plugin_rollpig_plus.roast_flow import RoastOutcome
from nonebot_plugin_rollpig_plus.store.cloud import CloudReservationUnsupportedError, CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import (
    RoastEvent,
    RoastReservation,
    RoastReservationParticipant,
    UnrolledRoastAttemptResult,
)


# ================================ 命令参数边界 ================================ #


def _command_rule(matcher) -> CommandRule:
    for checker in matcher.rule.checkers:
        if isinstance(checker.call, CommandRule):
            return checker.call
    raise AssertionError(f"matcher {matcher} has no CommandRule")


def _has_checker(matcher, checker_call) -> bool:
    return any(checker.call is checker_call for checker in matcher.rule.checkers)


class _MessageEvent:
    """为命令 Trie 和 Rule 提供最小消息事件接口。"""

    def __init__(self, message: Message):
        self.message = message

    def get_type(self) -> str:
        return "message"

    def get_message(self) -> Message:
        return self.message


async def _matches(matcher, message: Message) -> bool:
    """按 NoneBot 实际顺序完成 Trie 预处理，再执行 Matcher Rule。"""

    event = _MessageEvent(message)
    state = {}
    TrieRule.get_value(None, event, state)
    return await matcher.rule(None, event, state)


class CommandBoundaryRuleTests(unittest.IsolatedAsyncioTestCase):
    def test_pigsty_summary_advertises_submission_command(self):
        summary = build_pigsty_growth_summary(
            "Felis",
            SimpleNamespace(pig_ids=[], progress={}, duplicate_streak=0),
            165,
        )

        self.assertTrue(summary.endswith("💡 有新的小猪创意？发送「小猪投稿」把它送进猪圈。"))

    def test_no_argument_matchers_share_strict_rule(self):
        matchers = (
            roll_handler.cmd_sync_resources,
            roll_handler.cmd_today,
            roll_handler.cmd_tmr,
            roll_handler.cmd_yest,
            roast_handler.cmd_roast,
            roast_handler.cmd_random_roast,
            collection_handler.cmd_sty,
            collection_handler.cmd_submit_pig,
            collection_handler.cmd_week,
            refill_handler.cmd_roast_refill,
        )
        for matcher in matchers:
            with self.subTest(matcher=matcher):
                self.assertTrue(_has_checker(matcher, helpers.command_has_no_argument))

    async def test_matchers_apply_expected_boundaries(self):
        self.assertTrue(await _matches(roll_handler.cmd_today, Message("/今日小猪")))
        self.assertFalse(await _matches(roll_handler.cmd_today, Message("/今日小猪测试")))
        self.assertFalse(await _matches(roast_handler.cmd_random_roast, Message("/随机烤猪测试")))
        self.assertTrue(await _matches(collection_handler.cmd_submit_pig, Message("/小猪投稿")))
        self.assertTrue(await _matches(collection_handler.cmd_submit_pig, Message("/投稿小猪")))
        self.assertFalse(await _matches(collection_handler.cmd_submit_pig, Message("/小猪投稿测试")))

        self.assertTrue(await _matches(roast_handler.cmd_roast_member, Message("/烤群友 张三")))
        self.assertFalse(await _matches(roast_handler.cmd_roast_member, Message("/烤群友张三")))
        self.assertTrue(
            await _matches(
                roast_handler.cmd_roast_member,
                Message("/烤群友") + MessageSegment.at(123456),
            )
        )

        self.assertTrue(await _matches(control_handler.cmd_daily_summary_switch, Message("/小猪日报 开启")))
        self.assertFalse(await _matches(control_handler.cmd_daily_summary_switch, Message("/小猪日报开启")))

        # 用户明确要求这三个既有文本参数命令继续兼容黏连写法。
        self.assertTrue(await _matches(roll_handler.cmd_roll, Message("/随机小猪3")))
        self.assertTrue(await _matches(collection_handler.cmd_catalog, Message("/小猪图鉴2")))
        self.assertTrue(await _matches(roll_handler.cmd_find, Message("/找猪玩偶")))

    def test_existing_text_parameter_commands_keep_default_matching(self):
        for matcher in (
            roll_handler.cmd_roll,
            roll_handler.cmd_find,
            collection_handler.cmd_catalog,
        ):
            with self.subTest(matcher=matcher):
                self.assertIsNone(_command_rule(matcher).force_whitespace)


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

    async def test_migration_quarantines_ambiguous_processing_outcome(self):
        migrated = self.manager._migrate(
            {
                "roast_reservations": {
                    "legacy": {
                        "status": "processing",
                        "outcome_snapshot": {"event_type": "escape"},
                    }
                }
            },
            persist=False,
        )

        self.assertEqual(migrated["roast_reservations"]["legacy"]["status"], "sending")

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

    async def test_missing_local_resource_does_not_count_as_unrolled_attempt(self):
        matcher = SimpleNamespace(finish=AsyncMock())
        event = SimpleNamespace(user_id=123, message_id=456)
        mocked_store = SimpleNamespace(get_daily_roll=AsyncMock(return_value="new-resource-pig"))
        with (
            patch.object(roast_handler, "store", mocked_store),
            patch.object(roast_handler, "get_pig_by_id", return_value=None),
            patch.object(
                roast_handler,
                "_finish_unrolled_attacker_attempt",
                new_callable=AsyncMock,
            ) as finish_unrolled,
        ):
            resolved = await roast_handler._load_attacker_pig_or_finish(
                matcher,
                event,
                "测试员",
                None,
            )

        self.assertIsNone(resolved)
        finish_unrolled.assert_not_awaited()
        matcher.finish.assert_awaited_once()
        self.assertIn("资源暂时缺失", str(matcher.finish.await_args.args[0]))

    async def test_random_roast_checks_candidates_before_recording_unrolled_attempt(self):
        matcher = SimpleNamespace(finish=AsyncMock())
        bot = SimpleNamespace()
        event = SimpleNamespace(user_id=123, self_id=456, group_id=789, message_id=101)
        with (
            patch.object(
                roast_handler,
                "get_group_roll_candidates",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                roast_handler,
                "_load_attacker_pig_or_finish",
                new_callable=AsyncMock,
            ) as load_attacker,
        ):
            attacker_pig, candidates = await roast_handler._load_random_roast_context_or_finish(
                matcher,
                bot,
                event,
                "测试员",
            )

        self.assertIsNone(attacker_pig)
        self.assertFalse(candidates)
        matcher.finish.assert_awaited_once()
        load_attacker.assert_not_awaited()

    async def test_mentioned_target_requires_current_group_membership(self):
        event = SimpleNamespace(
            reply=None,
            message=[SimpleNamespace(type="at", data={"qq": "999"})],
            to_me=False,
            self_id=456,
            group_id=789,
        )
        bot = SimpleNamespace(
            get_group_member_info=AsyncMock(side_effect=RuntimeError("member not found"))
        )

        target = await helpers.resolve_roast_target(bot, event)

        self.assertEqual(target.target_id, "999")
        self.assertFalse(target.is_group_member)

    async def test_random_candidates_do_not_fall_back_to_departed_group_records(self):
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=RuntimeError("temporary failure")))
        with (
            patch.object(helpers.store, "get_daily_rolls", new_callable=AsyncMock, return_value={"left": "pig"}),
            self.assertRaises(helpers.GroupMemberLookupError),
        ):
            await helpers.get_group_roll_candidates(bot, 100, set())

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
        self.assertEqual(saved.status, "prepared")
        await self.manager.save_roast_reservation_outcome(
            reservation_id,
            reservation.claim_token,
            {"event_type": "success", "plain_text": "must-not-replace"},
        )
        await self.manager.release_roast_reservation(reservation_id, reservation.claim_token)

        reclaimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-07")
        self.assertEqual(reclaimed.reservations[0].outcome_snapshot, snapshot)
        reclaimed_reservation = reclaimed.reservations[0]
        self.assertEqual(reclaimed_reservation.status, "prepared")
        sending = await self.manager.mark_roast_reservation_sending(
            reservation_id,
            reclaimed_reservation.claim_token,
        )
        self.assertEqual(sending.status, "sending")
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

    async def test_sending_reservation_is_not_reclaimed_after_claim_timeout(self):
        await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        reservation = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]
        saved = await self.manager.save_roast_reservation_outcome(
            reservation.reservation_id,
            reservation.claim_token,
            {"event_type": "escape", "plain_text": "fixed"},
        )
        sending = await self.manager.mark_roast_reservation_sending(
            reservation.reservation_id,
            reservation.claim_token,
        )
        raw = self.manager.data["roast_reservations"][reservation.reservation_id]
        raw["claimed_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        ).isoformat()

        reclaimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-07")

        self.assertEqual(saved.status, "prepared")
        self.assertEqual(sending.status, "sending")
        self.assertFalse(reclaimed.reservations)
        self.assertFalse(self.manager.has_owned_roast_reservations("bot-1", "2026-08-07"))

    async def test_prepared_reservation_is_reclaimed_after_claim_timeout(self):
        await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        first = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]
        saved = await self.manager.save_roast_reservation_outcome(
            first.reservation_id,
            first.claim_token,
            {"event_type": "escape", "plain_text": "fixed"},
        )
        raw = self.manager.data["roast_reservations"][first.reservation_id]
        raw["claimed_at"] = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        ).isoformat()

        reclaimed = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]

        self.assertEqual(saved.status, "prepared")
        self.assertEqual(reclaimed.status, "prepared")
        self.assertEqual(reclaimed.outcome_snapshot["plain_text"], "fixed")
        self.assertNotEqual(reclaimed.claim_token, first.claim_token)

    async def test_local_claim_exclusion_keeps_owner_without_releasing_reservation(self):
        await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        reservation_id = next(iter(self.manager.data["roast_reservations"]))

        excluded = await self.manager.claim_roast_reservations(
            "bot-1",
            "2026-08-07",
            excluded_reservation_ids={reservation_id},
        )

        self.assertFalse(excluded.reservations)
        self.assertTrue(excluded.has_owned)
        self.assertEqual(
            self.manager.data["roast_reservations"][reservation_id]["status"],
            "ready",
        )

    async def test_in_flight_reservation_can_release_after_day_changes(self):
        await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        reservation = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]

        with patch.object(data_manager_module, "rollpig_date_str", return_value="2026-08-08"):
            saved = await self.manager.save_roast_reservation_outcome(
                reservation.reservation_id,
                reservation.claim_token,
                {"event_type": "escape"},
            )
            released = await self.manager.release_roast_reservation(
                reservation.reservation_id,
                reservation.claim_token,
            )

        self.assertEqual(saved.status, "prepared")
        self.assertTrue(released)
        self.assertTrue(self.manager.has_owned_roast_reservations("bot-1", "2026-08-08"))

        # 每日清理不能在短暂跨日重试窗口内删除已付费预约。
        with patch.object(data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 8)):
            await self.manager.clean_old_history()

        reclaimed = await self.manager.claim_roast_reservations("bot-1", "2026-08-08")
        self.assertEqual(len(reclaimed.reservations), 1)
        self.assertEqual(reclaimed.reservations[0].reservation_id, reservation.reservation_id)
        self.assertEqual(reclaimed.reservations[0].status, "prepared")

    async def test_history_cleanup_prunes_all_expired_reservations(self):
        self.manager.data["roast_reservations"] = {
            "old-completed": {"date_str": "2026-08-07", "status": "completed"},
            "old-pending": {"date_str": "2026-08-07", "status": "pending"},
            "old-released": {
                "date_str": "2026-08-07",
                "status": "ready",
                "released_at": "2026-08-07T16:00:00+00:00",
            },
            "current-completed": {"date_str": "2026-08-08", "status": "completed"},
            "current-pending": {"date_str": "2026-08-08", "status": "pending"},
        }

        with patch.object(data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 8)):
            await self.manager.clean_old_history()

        self.assertEqual(set(self.manager.data["roast_reservations"]), {"current-pending"})

    async def test_history_cleanup_prunes_expired_unrolled_attempts(self):
        self.manager.data["unrolled_roast_attempts"] = {
            "2026-08-07": {"old": 3},
            "2026-08-08": {"current": 2},
        }

        with patch.object(data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 8)):
            await self.manager.clean_old_history()

        self.assertEqual(self.manager.data["unrolled_roast_attempts"], {"2026-08-08": {"current": 2}})

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

    async def test_complete_atomically_records_reservation_event_once(self):
        created = await self._prepare()
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        claimed = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]
        prepared = await self.manager.save_roast_reservation_outcome(
            claimed.reservation_id,
            claimed.claim_token,
            {"event_type": "escape", "plain_text": "fixed"},
        )
        sending = await self.manager.mark_roast_reservation_sending(
            prepared.reservation_id,
            prepared.claim_token,
        )
        event = RoastEvent(
            event_type="escape",
            attacker_id="a",
            target_id="target",
            group_id="100",
            reservation_id=created.reservation.reservation_id,
        )

        first = await self.manager.complete_roast_reservation(
            sending.reservation_id,
            sending.claim_token,
            event,
        )
        repeated = await self.manager.complete_roast_reservation(
            sending.reservation_id,
            sending.claim_token,
            event,
        )

        self.assertTrue(first and repeated)
        self.assertEqual(
            self.manager.data["roast_reservations"][sending.reservation_id]["status"],
            "completed",
        )
        self.assertEqual(len(self.manager.data["daily_events"]["2026-08-07"]), 1)

    async def test_complete_binds_event_identity_to_reservation_snapshot(self):
        created = await self._prepare()
        joined = await self._prepare("b")
        await self.manager.get_or_create_today_pig("target", "target-pig", date_str="2026-08-07")
        claimed = (await self.manager.claim_roast_reservations("bot-1", "2026-08-07")).reservations[0]
        prepared = await self.manager.save_roast_reservation_outcome(
            claimed.reservation_id,
            claimed.claim_token,
            {"event_type": "backfire", "plain_text": "fixed"},
        )
        sending = await self.manager.mark_roast_reservation_sending(
            prepared.reservation_id,
            prepared.claim_token,
        )

        completed = await self.manager.complete_roast_reservation(
            sending.reservation_id,
            sending.claim_token,
            RoastEvent(
                event_type="backfire",
                attacker_id="wrong-owner",
                target_id="wrong-target",
                attacker_name="错误主厨",
                target_name="错误目标",
                group_id="999",
                reservation_id="wrong-reservation",
                participant_ids=("intruder",),
                participant_names=("闯入者",),
                participant_count=99,
                backfire_victim_id="b",
                backfire_victim_name="B",
            ),
        )

        self.assertTrue(completed)
        self.assertEqual(created.reservation.reservation_id, joined.reservation.reservation_id)
        event = self.manager.data["daily_events"]["2026-08-07"][0]
        self.assertEqual(
            (
                event["reservation_id"],
                event["group_id"],
                event["attacker"],
                event["target"],
            ),
            (created.reservation.reservation_id, "100", "a", "target"),
        )
        self.assertEqual(event["participant_ids"], ["a", "b"])
        self.assertEqual(event["participant_names"], ["A", "B"])
        self.assertEqual(event["participant_count"], 2)
        self.assertEqual(event["backfire_victim_id"], "b")

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


class ReservationDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reservation_delivery._owned_bot_ids.clear()
        reservation_delivery._retryable_bot_ids.clear()
        reservation_delivery._last_check_by_bot.clear()
        reservation_delivery._tasks_by_bot.clear()
        reservation_flow._resource_backoff_until.clear()
        reservation_flow._group_backoff.clear()

    async def test_failed_restore_retries_on_next_opportunistic_check(self):
        has_owned = AsyncMock(side_effect=CloudStoreError("timeout"))
        deliver = AsyncMock(
            return_value=reservation_flow.ReservationDeliveryResult(
                completed=0,
                has_owned=True,
            )
        )
        mocked_store = SimpleNamespace(has_owned_roast_reservations=has_owned)
        with (
            patch.object(reservation_delivery, "store", mocked_store),
            patch.object(reservation_flow, "deliver_ready_reservations", deliver),
            patch.object(reservation_delivery.time, "monotonic", return_value=1000.0),
        ):
            await reservation_delivery.restore_owned_reservations("bot-1")
            self.assertIn("bot-1", reservation_delivery._retryable_bot_ids)

            await reservation_delivery._deliver_if_due("bot-1")

        self.assertIn("bot-1", reservation_delivery._owned_bot_ids)
        self.assertNotIn("bot-1", reservation_delivery._retryable_bot_ids)
        deliver.assert_awaited_once_with("bot-1")
        has_owned.assert_awaited_once_with("bot-1")

    async def test_retryable_bot_is_allowed_to_schedule_recovery(self):
        reservation_delivery._retryable_bot_ids.add("bot-1")
        created_coroutines = []
        fake_task = SimpleNamespace(
            done=lambda: False,
            add_done_callback=lambda _callback: None,
        )

        def create_task(coroutine):
            created_coroutines.append(coroutine)
            coroutine.close()
            return fake_task

        with patch.object(reservation_delivery.asyncio, "create_task", side_effect=create_task):
            reservation_delivery.schedule_opportunistic_delivery("bot-1")

        self.assertEqual(len(created_coroutines), 1)
        self.assertIs(reservation_delivery._tasks_by_bot["bot-1"], fake_task)

    async def test_owner_poll_wakes_bot_without_local_command(self):
        reservation_delivery._owned_bot_ids.add("bot-1")
        with (
            patch.object(reservation_delivery, "get_bots", return_value={"bot-1": object()}),
            patch.object(reservation_delivery, "schedule_opportunistic_delivery") as schedule,
        ):
            await reservation_delivery.poll_owned_reservations()

        schedule.assert_called_once_with("bot-1")

    async def test_unsupported_cloud_does_not_enter_permanent_retry_poll(self):
        mocked_store = SimpleNamespace(
            has_owned_roast_reservations=AsyncMock(
                side_effect=CloudReservationUnsupportedError("unsupported")
            )
        )
        with patch.object(reservation_delivery, "store", mocked_store):
            await reservation_delivery.restore_owned_reservations("bot-1")

        self.assertNotIn("bot-1", reservation_delivery._owned_bot_ids)
        self.assertNotIn("bot-1", reservation_delivery._retryable_bot_ids)

    async def test_unsupported_claim_clears_previous_retry_state(self):
        reservation_delivery._retryable_bot_ids.add("bot-1")
        deliver = AsyncMock(side_effect=CloudReservationUnsupportedError("unsupported"))
        with (
            patch.object(reservation_flow, "deliver_ready_reservations", deliver),
            patch.object(reservation_delivery.time, "monotonic", return_value=1000.0),
        ):
            await reservation_delivery._deliver_if_due("bot-1")

        self.assertNotIn("bot-1", reservation_delivery._owned_bot_ids)
        self.assertNotIn("bot-1", reservation_delivery._retryable_bot_ids)

    def test_existing_owner_response_restores_poll_registration(self):
        reservation = RoastReservation(
            reservation_id="reservation",
            date_str="2026-08-07",
            group_id="100",
            target_id="target",
            target_name="目标",
            owner_id="owner",
            owner_name="主厨",
            owner_pig_id="owner-pig",
            delivery_bot_id="bot-1",
        )
        preparation = SimpleNamespace(status="already_joined", reservation=reservation)
        with patch.object(roast_handler, "register_owned_reservation") as register:
            roast_handler._register_preparation_owner(preparation, "bot-1")
            roast_handler._register_preparation_owner(preparation, "bot-2")

        register.assert_called_once_with("bot-1")

    def test_only_valid_member_target_reaches_unrolled_penalty(self):
        self.assertEqual(roast_handler._classify_roast_target("a", "", "bot"), "missing")
        self.assertEqual(roast_handler._classify_roast_target("a", "a", "bot"), "self")
        self.assertEqual(roast_handler._classify_roast_target("a", "bot", "bot"), "bot")
        self.assertEqual(roast_handler._classify_roast_target("a", "b", "bot"), "member")


class RoastReservationOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reservation_flow._resource_backoff_until.clear()
        reservation_flow._group_backoff.clear()

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
                outcome = await reservation_flow.build_reservation_outcome(self._reservation())
                self.assertEqual(outcome.event_type, "backfire")

        self.assertEqual(backfire.await_args.args[0], victim)
        self.assertEqual(backfire.await_args.kwargs["attacker_name"], "帮厨")
        self.assertEqual(outcome.backfire_victim_id, "helper")
        self.assertEqual(outcome.backfire_victim_name, "帮厨")

    async def test_backfire_event_keeps_old_roles_and_snapshot_records_victim(self):
        reservation = self._reservation()
        outcome = RoastOutcome(
            event_type="backfire",
            backfire_victim_id="helper",
            backfire_victim_name="帮厨",
        )

        event = reservation_flow._build_reservation_event(reservation, outcome)
        legacy_event = reservation_flow._build_reservation_event(
            reservation,
            reservation_flow._deserialize_outcome({"event_type": "backfire"}),
        )

        self.assertEqual((event.attacker_id, event.attacker_name), ("owner", "主厨"))
        self.assertEqual((legacy_event.attacker_id, legacy_event.attacker_name), ("owner", "主厨"))
        self.assertEqual((event.backfire_victim_id, event.backfire_victim_name), ("helper", "帮厨"))
        snapshot = reservation_flow._serialize_outcome(outcome)
        self.assertEqual(snapshot["backfire_victim_id"], "helper")
        self.assertEqual(snapshot["backfire_victim_name"], "帮厨")

    async def test_release_after_delivery_failure_retries_exception_and_false(self):
        reservation = self._reservation()
        release = AsyncMock(side_effect=[CloudStoreError("timeout"), False, True])
        with (
            patch.object(reservation_flow, "store") as mocked_store,
            patch.object(reservation_flow.asyncio, "sleep", new_callable=AsyncMock),
        ):
            mocked_store.release_roast_reservation = release
            released = await reservation_flow._release_after_delivery_failure(
                reservation,
                reason="test",
            )

        self.assertTrue(released)
        self.assertEqual(release.await_count, 3)

    async def test_disabled_destination_group_releases_without_sending(self):
        reservation = self._reservation()
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                side_effect=[
                    SimpleNamespace(reservations=(reservation,), has_owned=True),
                    SimpleNamespace(reservations=(), has_owned=True),
                ]
            ),
            release_roast_reservation=AsyncMock(return_value=True),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=False),
            patch.object(reservation_flow, "build_reservation_outcome", new_callable=AsyncMock) as build,
        ):
            completed = await reservation_flow.deliver_ready_reservations("bot-1")
            repeated = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(completed.completed, 0)
        self.assertEqual(repeated.completed, 0)
        second_claim = mocked_store.claim_roast_reservations.await_args_list[1]
        self.assertIn(
            reservation.reservation_id,
            second_claim.kwargs["excluded_reservation_ids"],
        )
        self.assertIn(reservation.reservation_id, reservation_flow._group_backoff)
        mocked_store.release_roast_reservation.assert_awaited_once_with(reservation)
        build.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

    async def test_group_disabled_after_outcome_save_still_prevents_sending(self):
        reservation = self._reservation()
        updated = RoastReservation(**{**reservation.__dict__, "status": "prepared"})
        outcome = RoastOutcome(event_type="escape", plain_text="fixed")
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                return_value=SimpleNamespace(reservations=(reservation,), has_owned=True)
            ),
            save_roast_reservation_outcome=AsyncMock(return_value=updated),
            release_roast_reservation=AsyncMock(return_value=True),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", side_effect=[True, False]),
            patch.object(
                reservation_flow,
                "build_reservation_outcome",
                new_callable=AsyncMock,
                return_value=outcome,
            ),
        ):
            completed = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(completed.completed, 0)
        mocked_store.save_roast_reservation_outcome.assert_awaited_once()
        mocked_store.release_roast_reservation.assert_awaited_once_with(updated)
        bot.send_group_msg.assert_not_awaited()

    async def test_render_completes_before_reservation_enters_sending(self):
        reservation = self._reservation()
        outcome = RoastOutcome(event_type="escape", plain_text="fixed")
        prepared = RoastReservation(
            **{
                **reservation.__dict__,
                "status": "prepared",
                "outcome_snapshot": reservation_flow._serialize_outcome(outcome),
            }
        )
        sending = RoastReservation(**{**prepared.__dict__, "status": "sending"})
        trace = []

        async def prepare_message(_outcome):
            trace.append("render")
            return "message"

        async def mark_sending(_reservation):
            trace.append("mark_sending")
            return sending

        async def send_group_msg(**_kwargs):
            trace.append("send")

        async def complete(_reservation, _event):
            trace.append("complete")
            return True

        bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=send_group_msg))
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                return_value=SimpleNamespace(reservations=(reservation,), has_owned=True)
            ),
            save_roast_reservation_outcome=AsyncMock(return_value=prepared),
            mark_roast_reservation_sending=AsyncMock(side_effect=mark_sending),
            complete_roast_reservation=AsyncMock(side_effect=complete),
            append_roast_event=AsyncMock(),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=True),
            patch.object(
                reservation_flow,
                "build_reservation_outcome",
                new_callable=AsyncMock,
                return_value=outcome,
            ),
            patch.object(
                reservation_flow,
                "_prepare_reservation_message",
                side_effect=prepare_message,
            ),
        ):
            completed = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(completed.completed, 1)
        self.assertEqual(trace, ["render", "mark_sending", "send", "complete"])

    async def test_render_cancellation_leaves_recoverable_prepared_state(self):
        reservation = self._reservation()
        outcome = RoastOutcome(event_type="escape", plain_text="fixed")
        prepared = RoastReservation(
            **{
                **reservation.__dict__,
                "status": "prepared",
                "outcome_snapshot": reservation_flow._serialize_outcome(outcome),
            }
        )
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                return_value=SimpleNamespace(reservations=(reservation,), has_owned=True)
            ),
            save_roast_reservation_outcome=AsyncMock(return_value=prepared),
            mark_roast_reservation_sending=AsyncMock(),
            release_roast_reservation=AsyncMock(return_value=True),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=True),
            patch.object(
                reservation_flow,
                "build_reservation_outcome",
                new_callable=AsyncMock,
                return_value=outcome,
            ),
            patch.object(
                reservation_flow,
                "_prepare_reservation_message",
                new_callable=AsyncMock,
                side_effect=reservation_flow.asyncio.CancelledError,
            ),
        ):
            with self.assertRaises(reservation_flow.asyncio.CancelledError):
                await reservation_flow.deliver_ready_reservations("bot-1")

        mocked_store.save_roast_reservation_outcome.assert_awaited_once()
        mocked_store.mark_roast_reservation_sending.assert_not_awaited()
        mocked_store.release_roast_reservation.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

    async def test_missing_target_resource_releases_without_finalizing(self):
        reservation = self._reservation()
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        empty_claim = SimpleNamespace(reservations=(), has_owned=True)
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                side_effect=[
                    SimpleNamespace(reservations=(reservation,), has_owned=True),
                    empty_claim,
                ]
            ),
            save_roast_reservation_outcome=AsyncMock(),
            mark_roast_reservation_sending=AsyncMock(),
            release_roast_reservation=AsyncMock(return_value=True),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=True),
            patch.object(reservation_flow, "get_pig_by_id", return_value=None),
        ):
            completed = await reservation_flow.deliver_ready_reservations("bot-1")
            repeated = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(completed.completed, 0)
        self.assertEqual(repeated.completed, 0)
        second_claim = mocked_store.claim_roast_reservations.await_args_list[1]
        self.assertIn(
            reservation.reservation_id,
            second_claim.kwargs["excluded_reservation_ids"],
        )
        self.assertIn(
            reservation.reservation_id,
            reservation_flow._resource_backoff_until,
        )
        mocked_store.release_roast_reservation.assert_awaited_once_with(reservation)
        mocked_store.save_roast_reservation_outcome.assert_not_awaited()
        mocked_store.mark_roast_reservation_sending.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()

        self.assertTrue(reservation_flow.clear_resource_reservation_backoffs())
        self.assertFalse(reservation_flow._resource_backoff_until)

    async def test_external_send_failure_keeps_sending_and_never_releases(self):
        outcome = RoastOutcome(event_type="escape", plain_text="fixed")
        prepared = RoastReservation(
            **{
                **self._reservation().__dict__,
                "status": "prepared",
                "outcome_snapshot": reservation_flow._serialize_outcome(outcome),
            }
        )
        sending = RoastReservation(**{**prepared.__dict__, "status": "sending"})
        bot = SimpleNamespace(send_group_msg=AsyncMock(side_effect=RuntimeError("unknown result")))
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                return_value=SimpleNamespace(reservations=(prepared,), has_owned=True)
            ),
            mark_roast_reservation_sending=AsyncMock(return_value=sending),
            release_roast_reservation=AsyncMock(return_value=True),
            complete_roast_reservation=AsyncMock(),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=True),
        ):
            completed = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(completed.completed, 0)
        mocked_store.mark_roast_reservation_sending.assert_awaited_once_with(prepared)
        mocked_store.release_roast_reservation.assert_not_awaited()
        mocked_store.complete_roast_reservation.assert_not_awaited()

    async def test_lost_prepare_and_sending_responses_are_retried_before_send(self):
        reservation = self._reservation()
        outcome = RoastOutcome(event_type="escape", plain_text="fixed")
        snapshot = reservation_flow._serialize_outcome(outcome)
        prepared = RoastReservation(
            **{
                **reservation.__dict__,
                "status": "prepared",
                "outcome_snapshot": snapshot,
            }
        )
        sending = RoastReservation(**{**prepared.__dict__, "status": "sending"})
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        mocked_store = SimpleNamespace(
            claim_roast_reservations=AsyncMock(
                return_value=SimpleNamespace(
                    reservations=(reservation,),
                    has_owned=True,
                )
            ),
            save_roast_reservation_outcome=AsyncMock(
                side_effect=[CloudStoreError("prepare response lost"), prepared]
            ),
            mark_roast_reservation_sending=AsyncMock(
                side_effect=[CloudStoreError("sending response lost"), sending]
            ),
            complete_roast_reservation=AsyncMock(return_value=True),
            append_roast_event=AsyncMock(),
            release_roast_reservation=AsyncMock(return_value=True),
        )
        with (
            patch.object(reservation_flow, "store", mocked_store),
            patch.object(reservation_flow, "get_bots", return_value={"bot-1": bot}),
            patch.object(reservation_flow, "is_group_rollpig_enabled", return_value=True),
            patch.object(
                reservation_flow,
                "build_reservation_outcome",
                new_callable=AsyncMock,
                return_value=outcome,
            ),
            patch.object(
                reservation_flow.asyncio,
                "sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await reservation_flow.deliver_ready_reservations("bot-1")

        self.assertEqual(result.completed, 1)
        self.assertEqual(mocked_store.save_roast_reservation_outcome.await_count, 2)
        self.assertEqual(mocked_store.mark_roast_reservation_sending.await_count, 2)
        bot.send_group_msg.assert_awaited_once()
        expected_event = reservation_flow._build_reservation_event(sending, outcome)
        mocked_store.complete_roast_reservation.assert_awaited_once_with(sending, expected_event)
        mocked_store.release_roast_reservation.assert_not_awaited()

    async def test_complete_after_send_retries_without_releasing(self):
        reservation = self._reservation()
        event = reservation_flow._build_reservation_event(
            reservation,
            RoastOutcome(event_type="escape"),
        )
        complete = AsyncMock(side_effect=[CloudStoreError("timeout"), True])
        with (
            patch.object(reservation_flow, "store") as mocked_store,
            patch.object(reservation_flow.asyncio, "sleep", new_callable=AsyncMock),
        ):
            mocked_store.complete_roast_reservation = complete
            self.assertTrue(await reservation_flow._complete_after_send(reservation, event))

        self.assertEqual(complete.await_count, 2)
        mocked_store.release_roast_reservation.assert_not_called()

    async def test_complete_after_send_exhaustion_keeps_unconfirmed_state(self):
        reservation = self._reservation()
        event = reservation_flow._build_reservation_event(
            reservation,
            RoastOutcome(event_type="escape"),
        )
        complete = AsyncMock(side_effect=CloudStoreError("timeout"))
        with (
            patch.object(reservation_flow, "store") as mocked_store,
            patch.object(reservation_flow.asyncio, "sleep", new_callable=AsyncMock),
        ):
            mocked_store.complete_roast_reservation = complete
            self.assertFalse(await reservation_flow._complete_after_send(reservation, event))

        self.assertEqual(complete.await_count, 3)
        mocked_store.release_roast_reservation.assert_not_called()


class CloudReservationCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_advertises_prepared_capability_and_local_exclusions(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(
            return_value={"items": [], "has_owned": True}
        )

        result = await store.claim_roast_reservations(
            "bot-1",
            "2026-08-07",
            excluded_reservation_ids={"reservation-b", "reservation-a"},
        )

        self.assertFalse(result.reservations)
        self.assertTrue(result.has_owned)
        store._reservation_request.assert_awaited_once_with(
            "POST",
            "/v1/roast-reservations/claim",
            json_body={
                "delivery_bot_id": "bot-1",
                "date_str": "2026-08-07",
                "supports_prepared": True,
                "excluded_reservation_ids": ["reservation-a", "reservation-b"],
            },
        )

    async def test_claim_falls_back_for_old_cloud_without_owner_flag(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(return_value={"items": []})

        result = await store.claim_roast_reservations("bot-1", "2026-08-07")

        self.assertFalse(result.has_owned)

    async def test_old_cloud_missing_endpoint_raises_compatibility_error(self):
        request = httpx.Request("POST", "https://example.invalid/v1/roast-reservations/prepare")
        response = httpx.Response(404, request=request)
        status_error = httpx.HTTPStatusError("missing", request=request, response=response)
        store = object.__new__(CloudStore)
        store._request = AsyncMock(side_effect=CloudStoreError("missing"))
        store._request.side_effect.__cause__ = status_error

        with self.assertRaises(CloudReservationUnsupportedError):
            await store._reservation_request("POST", "/v1/roast-reservations/prepare")

    async def test_complete_submits_event_atomically_to_new_cloud(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(return_value={"ok": True, "event_recorded": True})
        store._append_roast_event = AsyncMock()
        reservation = RoastReservation(
            reservation_id="reservation",
            date_str="2026-08-07",
            group_id="100",
            target_id="target",
            target_name="目标",
            owner_id="owner",
            owner_name="主厨",
            owner_pig_id="owner-pig",
            delivery_bot_id="bot-1",
            claim_token="claim",
        )
        event = RoastEvent(
            event_type="escape",
            attacker_id="owner",
            target_id="target",
            group_id="100",
            reservation_id="reservation",
        )

        completed = await store.complete_roast_reservation(reservation, event)

        self.assertTrue(completed)
        body = store._reservation_request.await_args.kwargs["json_body"]
        self.assertEqual(body["event"]["date_str"], "2026-08-07")
        self.assertEqual(body["event"]["reservation_id"], "reservation")
        store._append_roast_event.assert_not_awaited()

    async def test_complete_falls_back_to_event_endpoint_for_old_cloud(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(return_value={"ok": True})
        store._append_roast_event = AsyncMock()
        reservation = RoastReservation(
            reservation_id="reservation",
            date_str="2026-08-07",
            group_id="100",
            target_id="target",
            target_name="目标",
            owner_id="owner",
            owner_name="主厨",
            owner_pig_id="owner-pig",
            delivery_bot_id="bot-1",
            claim_token="claim",
        )
        event = RoastEvent(
            event_type="escape",
            attacker_id="owner",
            target_id="target",
            group_id="100",
            reservation_id="reservation",
        )

        completed = await store.complete_roast_reservation(reservation, event)

        self.assertTrue(completed)
        store._append_roast_event.assert_awaited_once_with(event, date_str="2026-08-07")


if __name__ == "__main__":
    unittest.main()
