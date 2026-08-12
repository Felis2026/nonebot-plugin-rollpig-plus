from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import NoticeEvent
from nonebot.plugin import get_plugin

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus import roast_refill
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.handlers import refill as refill_handler
from nonebot_plugin_rollpig_plus.store.models import roast_refill_threshold
from nonebot_plugin_rollpig_plus.store.cloud import CloudRoastRefillUnsupportedError, CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import (
    GroupRoastRefillCompleteResult,
    GroupRoastRefillPrepareResult,
    GroupRoastRefillRequest,
)


DATE_STR = "2026-08-08"
NOW_TS = 1_786_118_400.0


class LocalRoastRefillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "pig_data.json"
        self.data_file_patch = patch.object(data_manager_module, "DATA_FILE", self.data_file)
        self.data_file_patch.start()
        self.addCleanup(self.data_file_patch.stop)
        self.manager = PigDataManager()

    async def _mark(self, group_id: str = "100", *user_ids: str) -> None:
        await self.manager.mark_group_active_users(group_id, list(user_ids), DATE_STR)

    async def _prepare(self, group_id: str = "100", now_ts: float = NOW_TS):
        return await self.manager.prepare_group_roast_refill(
            group_id=group_id,
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            date_str=DATE_STR,
            now_ts=now_ts,
        )

    def test_threshold_matrix_uses_ceil_and_caps_at_sixty_five_percent(self):
        expected = {
            3: [2, 2, 2, 2, 2],
            5: [2, 2, 3, 3, 4],
            10: [3, 4, 5, 6, 7],
            20: [5, 7, 9, 11, 13],
        }
        for active_count, votes in expected.items():
            with self.subTest(active_count=active_count):
                self.assertEqual(
                    [roast_refill_threshold(active_count, success)[1] for success in range(5)],
                    votes,
                )
        self.assertEqual(roast_refill_threshold(20, 99), (65, 13))

    async def test_prepare_freezes_threshold_and_allows_only_one_voting_request(self):
        await self._mark("100", "a", "b")
        insufficient = await self._prepare()
        self.assertEqual(insufficient.status, "insufficient_active")

        await self._mark("100", "c", "d", "e", "f", "g", "h", "i", "j")
        first, second = await asyncio.gather(self._prepare(), self._prepare())
        self.assertEqual({first.status, second.status}, {"created", "existing"})
        request = first.request if first.status == "created" else second.request
        self.assertEqual((request.active_count_snapshot, request.required_votes), (10, 3))

        await self._mark("100", "late-1", "late-2")
        current = await self.manager.get_group_roast_refill("100", DATE_STR, NOW_TS + 30)
        self.assertEqual((current.active_count_snapshot, current.required_votes), (10, 3))

    async def test_prepare_uses_only_current_member_eligible_users(self):
        await self._mark("100", "a", "b", "c", "left")

        prepared = await self.manager.prepare_group_roast_refill(
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            eligible_user_ids=["a", "b", "c"],
            date_str=DATE_STR,
            now_ts=NOW_TS,
        )

        self.assertEqual(prepared.status, "created")
        self.assertEqual(prepared.active_user_ids, ("a", "b", "c"))
        self.assertEqual(
            (prepared.request.active_count_snapshot, prepared.request.required_votes),
            (3, 2),
        )

    async def test_prepare_returns_unexpired_poll_from_previous_date(self):
        previous_date = "2026-08-07"
        await self.manager.mark_group_active_users("100", ["a", "b", "c"], previous_date)
        previous = await self.manager.prepare_group_roast_refill(
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            date_str=previous_date,
            now_ts=NOW_TS,
        )
        await self._mark("100", "a", "b", "c")

        current = await self._prepare(now_ts=NOW_TS + 30)

        self.assertEqual(current.status, "existing")
        self.assertEqual(current.request.request_id, previous.request.request_id)

    async def test_complete_filters_voters_and_resets_latest_active_users_once(self):
        await self._mark("100", "a", "b", "c", "d", "e")
        created = await self._prepare()
        request = await self.manager.bind_group_roast_refill_message(
            created.request.request_id,
            "message-1",
            now_ts=NOW_TS,
        )
        await self._mark("100", "late")
        self.manager.data["force_usage"]["a"] = DATE_STR
        self.manager.data["unrolled_roast_attempts"][DATE_STR] = {"a": 2}
        self.manager.data["protected"][DATE_STR] = {"100": ["a"]}

        first, second = await asyncio.gather(
            self.manager.complete_group_roast_refill(
                request_id=request.request_id,
                message_id="message-1",
                voter_ids=["bot", "a", "b", "outsider", "a"],
                excluded_user_ids=["bot"],
                max_charges=4,
                now_ts=NOW_TS + 60,
            ),
            self.manager.complete_group_roast_refill(
                request_id=request.request_id,
                message_id="message-1",
                voter_ids=["a", "b", "c"],
                excluded_user_ids=["bot"],
                max_charges=4,
                now_ts=NOW_TS + 60,
            ),
        )
        winners = [result for result in (first, second) if result.completed]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertEqual(set(winner.benefited_user_ids), {"a", "b", "c", "d", "e", "late"})
        for user_id in winner.benefited_user_ids:
            state = self.manager.data["usage"][user_id]
            self.assertEqual((state["roast_charges"], state["roast_charge_updated_ts"]), (4, NOW_TS + 60))
        self.assertEqual(self.manager.data["force_usage"]["a"], DATE_STR)
        self.assertEqual(self.manager.data["unrolled_roast_attempts"][DATE_STR]["a"], 2)
        self.assertEqual(self.manager.data["protected"][DATE_STR]["100"], ["a"])

        next_round = await self._prepare(now_ts=NOW_TS + 61)
        self.assertEqual((next_round.request.success_count_before, next_round.request.required_ratio), (1, 35))

    async def test_complete_requires_a_bound_nonempty_poll_message(self):
        await self._mark("100", "a", "b", "c", "d", "e")
        created = await self._prepare()

        completed = await self.manager.complete_group_roast_refill(
            request_id=created.request.request_id,
            message_id="",
            voter_ids=["a", "b", "c"],
            excluded_user_ids=[],
            max_charges=4,
            now_ts=NOW_TS + 60,
        )

        self.assertFalse(completed.completed)
        self.assertEqual(completed.status, "message_mismatch")
        self.assertNotIn("a", self.manager.data["usage"])

    async def test_bind_rejects_empty_message_and_expires_request_under_lock(self):
        await self._mark("100", "a", "b", "c", "d", "e")
        created = await self._prepare()

        empty = await self.manager.bind_group_roast_refill_message(
            created.request.request_id,
            "",
        )
        with patch.object(data_manager_module.time, "time", return_value=NOW_TS + 601):
            expired = await self.manager.bind_group_roast_refill_message(
                created.request.request_id,
                "message-1",
            )

        self.assertIsNone(empty)
        self.assertIsNone(expired)
        raw = self.manager.data["roast_refill_requests"][created.request.request_id]
        self.assertEqual((raw["status"], raw["message_id"]), ("expired", ""))

    async def test_expiry_does_not_raise_difficulty_and_restart_keeps_voting_request(self):
        await self._mark("100", "a", "b", "c", "d", "e")
        created = await self._prepare()
        await self.manager.bind_group_roast_refill_message(
            created.request.request_id,
            "message-1",
            now_ts=NOW_TS,
        )

        restored = PigDataManager()
        current = await restored.get_group_roast_refill("100", DATE_STR, NOW_TS + 599)
        self.assertEqual(current.message_id, "message-1")
        expired = await restored.get_group_roast_refill("100", DATE_STR, NOW_TS + 601)
        self.assertIsNone(expired)

        next_round = await restored.prepare_group_roast_refill(
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            date_str=DATE_STR,
            now_ts=NOW_TS + 602,
        )
        self.assertEqual((next_round.status, next_round.request.success_count_before), ("created", 0))

    async def test_existing_data_migration_backfills_group_active_users(self):
        self.data_file.write_text(
            '{"group_rolls":{"2026-08-08":{"100":{"a":"pig-a"}}},'
            '"daily_events":{"2026-08-08":[{"group_id":"100","attacker":"b","target":"c",'
            '"participant_ids":["d"]},{"type":"bot_backfire","group_id":"100",'
            '"attacker":"e","target":"bot"}]},"roast_reservations":{},"history":{},"collection":{},'
            '"pig_progress":{},"draw_state":{},"usage":{},"force_usage":{},"protected":{},'
            '"unrolled_roast_attempts":{}}',
            encoding="utf-8",
        )
        migrated = PigDataManager()
        self.assertEqual(migrated.get_group_active_user_ids("100", DATE_STR), {"a", "b", "c", "d", "e"})


class RoastRefillReactionTests(unittest.IsolatedAsyncioTestCase):
    def test_refill_texts_render_configured_charge_max(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            required_votes=2,
        )
        with patch.object(roast_refill.random, "choice", side_effect=lambda choices: choices[0]):
            created = roast_refill.format_refill_created(request, max_charges=4)
            succeeded = roast_refill.format_refill_success(
                request,
                votes=2,
                benefited=3,
                max_charges=4,
            )
        self.assertIn("4 / 4", created)
        self.assertIn("4 / 4", succeeded)

    def test_refill_created_message_places_image_before_text(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            required_votes=2,
        )

        with patch.object(roast_refill.random, "choice", side_effect=lambda choices: choices[0]):
            message = roast_refill.build_refill_created_message(request, max_charges=4)

        self.assertTrue(roast_refill.ROAST_REFILL_IMAGE_PATH.is_file())
        self.assertEqual([segment.type for segment in message], ["image", "text"])
        self.assertTrue(message[0].data["file"].startswith("base64://"))
        self.assertIn("4 / 4", message[1].data["text"])

    def test_refill_multiline_texts_do_not_insert_blank_lines(self):
        for template in (*roast_refill.ROAST_REFILL_CREATED_TEXTS, *roast_refill.ROAST_REFILL_SUCCESS_TEXTS):
            self.assertNotIn("\n\n", template)

    async def test_fetch_reactors_uses_napcat_fields_and_paginates(self):
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=[
            {
                "emojiLikesList": [{"tinyId": "a"}, {"tinyId": "bot"}],
                "cookie": "next",
                "isLastPage": False,
            },
            {
                "emojiLikesList": [{"tinyId": "b"}, {"tinyId": "a"}],
                "cookie": "",
                "isLastPage": True,
            },
        ]))
        self.assertEqual(await roast_refill.fetch_refill_reactors(bot, "123"), {"a", "b", "bot"})
        first_call = bot.call_api.await_args_list[0]
        self.assertEqual(first_call.args[0], "fetch_emoji_like")
        self.assertEqual(first_call.kwargs["emojiId"], "424")
        self.assertEqual(first_call.kwargs["emojiType"], 1)

    async def test_fetch_reactors_rejects_napcat_failure_result(self):
        bot = SimpleNamespace(call_api=AsyncMock(return_value={
            "emojiLikesList": [],
            "cookie": "",
            "isLastPage": True,
            "result": 1,
            "errMsg": "消息不存在",
        }))
        with self.assertRaises(roast_refill.RoastRefillReactionError) as raised:
            await roast_refill.fetch_refill_reactors(bot, "missing")
        self.assertTrue(raised.exception.message_missing)

    async def test_fetch_reactors_distinguishes_unsupported_and_transient_errors(self):
        unsupported_bot = SimpleNamespace(
            call_api=AsyncMock(side_effect=RuntimeError("unknown action: fetch_emoji_like"))
        )
        with self.assertRaises(roast_refill.RoastRefillReactionError) as unsupported:
            await roast_refill.fetch_refill_reactors(unsupported_bot, "message-1")
        self.assertTrue(unsupported.exception.capability_unsupported)

        transient_bot = SimpleNamespace(
            call_api=AsyncMock(side_effect=TimeoutError("request timed out"))
        )
        with self.assertRaises(roast_refill.RoastRefillReactionError) as transient:
            await roast_refill.fetch_refill_reactors(transient_bot, "message-1")
        self.assertFalse(transient.exception.capability_unsupported)
        self.assertFalse(transient.exception.message_missing)

    async def test_group_member_filter_reads_current_member_ids(self):
        bot = SimpleNamespace(call_api=AsyncMock(return_value=[
            {"user_id": 10001},
            {"user_id": "10002"},
        ]))
        self.assertEqual(await roast_refill.fetch_refill_group_members(bot, "100"), {"10001", "10002"})

    def test_notice_only_accepts_qq_continue_marker(self):
        self.assertTrue(roast_refill.is_refill_notice(SimpleNamespace(
            notice_type="group_msg_emoji_like",
            likes=[{"emoji_id": "424", "count": 1}],
        )))
        self.assertFalse(roast_refill.is_refill_notice(SimpleNamespace(
            notice_type="group_msg_emoji_like",
            likes=[{"emoji_id": "123", "count": 99}],
        )))
        parsed = NoticeEvent.model_validate({
            "time": 1,
            "self_id": 10000,
            "post_type": "notice",
            "notice_type": "group_msg_emoji_like",
            "group_id": 100,
            "user_id": 10001,
            "message_id": 123,
            "likes": [{"emoji_id": "424", "count": 1}],
            "is_add": True,
        })
        self.assertTrue(roast_refill.is_refill_notice(parsed))

    def test_permission_allows_owner_admin_and_superuser_only(self):
        with patch.object(refill_handler, "is_superuser_user", side_effect=lambda user_id: user_id == "su"):
            for role in ("owner", "admin"):
                self.assertTrue(refill_handler._can_start_refill(SimpleNamespace(user_id="u", sender=SimpleNamespace(role=role))))
            self.assertTrue(refill_handler._can_start_refill(SimpleNamespace(user_id="su", sender=SimpleNamespace(role="member"))))
            self.assertFalse(refill_handler._can_start_refill(SimpleNamespace(user_id="u", sender=SimpleNamespace(role="member"))))

    async def test_cross_date_lookup_matches_original_poll_message(self):
        current = GroupRoastRefillRequest(
            request_id="current",
            date_str="2026-08-09",
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="other-message",
        )
        previous = GroupRoastRefillRequest(
            request_id="previous",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
        )
        with (
            patch.object(roast_refill, "_refill_lookup_dates", return_value=("2026-08-09", DATE_STR)),
            patch.object(roast_refill, "store") as mocked_store,
        ):
            mocked_store.get_group_roast_refill = AsyncMock(side_effect=[current, previous])
            found = await roast_refill.find_group_roast_refill("100", message_id="message-1")

        self.assertEqual(found.request_id, "previous")
        self.assertEqual(
            [call.kwargs["date_str"] for call in mocked_store.get_group_roast_refill.await_args_list],
            ["2026-08-09", DATE_STR],
        )

    async def test_unbound_existing_request_waits_without_fetching_or_failing(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
        )
        bot = SimpleNamespace(self_id="bot")
        with (
            patch.object(refill_handler, "reconcile_refill_request", new_callable=AsyncMock) as reconcile,
            patch.object(refill_handler, "store") as mocked_store,
        ):
            text = await refill_handler._describe_existing_refill(bot, request)

        self.assertIn("正在生成", text)
        reconcile.assert_not_awaited()
        mocked_store.fail_group_roast_refill.assert_not_called()

    def test_prepare_snapshot_must_match_current_group_members(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            active_count_snapshot=3,
            required_ratio=25,
            required_votes=2,
        )
        valid = GroupRoastRefillPrepareResult(
            "created",
            request,
            active_user_ids=("a", "b", "c"),
        )
        stale = GroupRoastRefillPrepareResult(
            "created",
            GroupRoastRefillRequest(**{**request.__dict__, "active_count_snapshot": 4}),
            active_user_ids=("a", "b", "c", "left"),
        )

        self.assertTrue(refill_handler._preparation_matches_members(valid, {"a", "b", "c"}))
        self.assertFalse(refill_handler._preparation_matches_members(stale, {"a", "b", "c"}))

    async def test_notice_fetches_real_users_and_only_winner_sends_success(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
            active_count_snapshot=3,
            required_votes=2,
        )
        completed = GroupRoastRefillCompleteResult(
            completed=True,
            status="succeeded",
            request=GroupRoastRefillRequest(**{**request.__dict__, "status": "succeeded"}),
            valid_voter_ids=("a", "b"),
            benefited_user_ids=("a", "b", "c"),
        )
        async def call_api(api_name, **kwargs):
            if api_name == "fetch_emoji_like":
                return {
                    "emojiLikesList": [
                        {"tinyId": "bot"},
                        {"tinyId": "a"},
                        {"tinyId": "b"},
                        {"tinyId": "left"},
                    ],
                    "cookie": "",
                    "isLastPage": True,
                }
            if api_name == "get_group_member_list":
                return [{"user_id": "bot"}, {"user_id": "a"}, {"user_id": "b"}]
            raise AssertionError(api_name)

        bot = SimpleNamespace(
            self_id="bot",
            call_api=AsyncMock(side_effect=call_api),
            send_group_msg=AsyncMock(),
        )
        event = SimpleNamespace(
            notice_type="group_msg_emoji_like",
            group_id=100,
            message_id="message-1",
            likes=[{"emoji_id": "424", "count": 3}],
        )
        with (
            patch.object(roast_refill, "store") as mocked_store,
            patch.object(roast_refill, "resolve_roast_charge_max", return_value=4),
        ):
            mocked_store.get_group_roast_refill = AsyncMock(return_value=request)
            mocked_store.get_group_active_user_ids = AsyncMock(
                return_value={"bot", "a", "b", "left"}
            )
            mocked_store.complete_group_roast_refill = AsyncMock(side_effect=[completed, GroupRoastRefillCompleteResult(False, "succeeded")])
            await asyncio.gather(
                roast_refill.process_refill_notice(bot, event),
                roast_refill.process_refill_notice(bot, event),
            )

        self.assertEqual(mocked_store.complete_group_roast_refill.await_count, 2)
        bot.send_group_msg.assert_awaited_once()
        complete_kwargs = mocked_store.complete_group_roast_refill.await_args_list[0].kwargs
        self.assertEqual(set(complete_kwargs["voter_ids"]), {"a", "b"})
        self.assertEqual(complete_kwargs["excluded_user_ids"], ["bot", "left"])
        self.assertEqual(complete_kwargs["max_charges"], 4)

    async def test_existing_command_reconciles_missed_notice_and_excludes_nonmembers(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
            active_count_snapshot=4,
            required_votes=2,
        )
        completed = GroupRoastRefillCompleteResult(
            True,
            "succeeded",
            GroupRoastRefillRequest(**{**request.__dict__, "status": "succeeded"}),
            valid_voter_ids=("a", "b"),
            benefited_user_ids=("a", "b", "c"),
        )

        async def call_api(api_name, **kwargs):
            if api_name == "fetch_emoji_like":
                return {
                    "emojiLikesList": [
                        {"tinyId": "a"},
                        {"tinyId": "b"},
                        {"tinyId": "left"},
                    ],
                    "isLastPage": True,
                }
            if api_name == "get_group_member_list":
                return [{"user_id": "bot"}, {"user_id": "a"}, {"user_id": "b"}, {"user_id": "c"}]
            raise AssertionError(api_name)

        bot = SimpleNamespace(
            self_id="bot",
            call_api=AsyncMock(side_effect=call_api),
            send_group_msg=AsyncMock(),
        )
        with (
            patch.object(roast_refill, "store") as mocked_store,
            patch.object(roast_refill, "resolve_roast_charge_max", return_value=4),
        ):
            mocked_store.get_group_active_user_ids = AsyncMock(
                return_value={"a", "b", "c", "left"}
            )
            mocked_store.complete_group_roast_refill = AsyncMock(return_value=completed)
            text = await refill_handler._describe_existing_refill(bot, request)

        self.assertIsNone(text)
        complete_kwargs = mocked_store.complete_group_roast_refill.await_args.kwargs
        self.assertEqual(complete_kwargs["voter_ids"], ["a", "b"])
        self.assertEqual(complete_kwargs["excluded_user_ids"], ["bot", "left"])
        bot.send_group_msg.assert_awaited_once()

    async def test_transient_existing_poll_check_keeps_request_voting(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
        )
        bot = SimpleNamespace(self_id="bot")
        with (
            patch.object(
                refill_handler,
                "reconcile_refill_request",
                new_callable=AsyncMock,
                side_effect=roast_refill.RoastRefillReactionError("群成员名单读取失败"),
            ),
            patch.object(refill_handler, "store") as mocked_store,
        ):
            text = await refill_handler._describe_existing_refill(bot, request)

        self.assertIn("申请仍然有效", text)
        mocked_store.fail_group_roast_refill.assert_not_called()

    async def test_transient_post_send_probe_keeps_bound_request_voting(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
        )
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        error = roast_refill.RoastRefillReactionError("request timed out")
        with patch.object(refill_handler, "store") as mocked_store:
            terminal = await refill_handler._handle_post_send_probe_error(bot, request, error)

        self.assertFalse(terminal)
        mocked_store.fail_group_roast_refill.assert_not_called()
        bot.send_group_msg.assert_not_awaited()

    async def test_explicitly_unsupported_post_send_probe_stops_request(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
        )
        bot = SimpleNamespace(send_group_msg=AsyncMock())
        error = roast_refill.RoastRefillReactionError(
            "unknown action: fetch_emoji_like",
            capability_unsupported=True,
        )
        with patch.object(refill_handler, "store") as mocked_store:
            mocked_store.fail_group_roast_refill = AsyncMock(return_value=True)
            terminal = await refill_handler._handle_post_send_probe_error(bot, request, error)

        self.assertTrue(terminal)
        mocked_store.fail_group_roast_refill.assert_awaited_once_with(
            "request",
            "message-1",
            "reaction_unsupported",
        )
        bot.send_group_msg.assert_awaited_once()

    async def test_notice_below_raw_threshold_skips_group_member_fetch(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            message_id="message-1",
            required_votes=3,
        )
        bot = SimpleNamespace(
            self_id="bot",
            call_api=AsyncMock(return_value={
                "emojiLikesList": [{"tinyId": "bot"}, {"tinyId": "a"}],
                "cookie": "",
                "isLastPage": True,
            }),
            send_group_msg=AsyncMock(),
        )
        event = SimpleNamespace(
            notice_type="group_msg_emoji_like",
            group_id=100,
            message_id="message-1",
            likes=[{"emoji_id": "424", "count": 2}],
        )
        with patch.object(roast_refill, "store") as mocked_store:
            mocked_store.get_group_roast_refill = AsyncMock(return_value=request)
            await roast_refill.process_refill_notice(bot, event)

        bot.call_api.assert_awaited_once()
        self.assertEqual(bot.call_api.await_args.args[0], "fetch_emoji_like")
        mocked_store.complete_group_roast_refill.assert_not_called()

    async def test_notice_ignores_request_owned_by_another_bot(self):
        request = GroupRoastRefillRequest(
            request_id="request",
            date_str=DATE_STR,
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="other-bot",
            message_id="message-1",
        )
        bot = SimpleNamespace(self_id="bot", call_api=AsyncMock(), send_group_msg=AsyncMock())
        event = SimpleNamespace(
            notice_type="group_msg_emoji_like",
            group_id=100,
            message_id="message-1",
            likes=[{"emoji_id": "424", "count": 3}],
        )
        with patch.object(roast_refill, "store") as mocked_store:
            mocked_store.get_group_roast_refill = AsyncMock(return_value=request)
            await roast_refill.process_refill_notice(bot, event)
        bot.call_api.assert_not_awaited()
        mocked_store.complete_group_roast_refill.assert_not_called()

    async def test_old_cloud_missing_refill_endpoint_has_scoped_compatibility_error(self):
        request = httpx.Request("POST", "https://example.invalid/v1/group-roast-refills/prepare")
        response = httpx.Response(404, request=request)
        status_error = httpx.HTTPStatusError("missing", request=request, response=response)
        cloud_store = object.__new__(CloudStore)
        cloud_store._request = AsyncMock(side_effect=CloudStoreError("missing"))
        cloud_store._request.side_effect.__cause__ = status_error

        with self.assertRaises(CloudRoastRefillUnsupportedError):
            await cloud_store._refill_request("POST", "/v1/group-roast-refills/prepare")

    async def test_cloud_complete_forwards_configured_charge_max(self):
        cloud_store = object.__new__(CloudStore)
        cloud_store._request = AsyncMock(return_value={"completed": False, "status": "pending"})

        await cloud_store.complete_group_roast_refill(
            request_id="request",
            message_id="message-1",
            voter_ids=["a", "b"],
            excluded_user_ids=["bot"],
            max_charges=4,
        )

        request_kwargs = cloud_store._request.await_args.kwargs
        self.assertEqual(request_kwargs["json_body"]["max_charges"], 4)

    async def test_cloud_prepare_forwards_current_member_eligibility(self):
        cloud_store = object.__new__(CloudStore)
        cloud_store._request = AsyncMock(return_value={"status": "insufficient_active"})

        await cloud_store.prepare_group_roast_refill(
            group_id="100",
            initiator_id="admin",
            initiator_name="管理员",
            delivery_bot_id="bot",
            eligible_user_ids=["a", "b", "c"],
            date_str=DATE_STR,
        )

        request_kwargs = cloud_store._request.await_args.kwargs
        self.assertEqual(
            request_kwargs["json_body"]["eligible_user_ids"],
            ["a", "b", "c"],
        )
