from __future__ import annotations

import asyncio
import datetime
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_roast_reservations as fixtures
from nonebot_plugin_rollpig_plus import daily_report, yesterday_recap
from nonebot_plugin_rollpig_plus.roast_flow import pick_reservation_prepare_text
from nonebot_plugin_rollpig_plus.reservation_flow import _deserialize_outcome, _serialize_outcome
from nonebot_plugin_rollpig_plus.store.cloud import CloudStore, CloudStoreError, CloudReservationUnsupportedError
from nonebot_plugin_rollpig_plus.store.models import RoastReservation, RoastReservationParticipant


class LocalProtectionGatheringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        data_patch = patch.object(fixtures.data_manager_module, "DATA_FILE", Path(self.temporary.name) / "data.json")
        data_patch.start()
        self.addCleanup(data_patch.stop)
        self.manager = fixtures.PigDataManager()
        self.manager.data["protected"] = {"2026-08-07": {"100": ["target"]}}
        self.manager.data["protection_details"] = {"2026-08-07": {"100": {"target": {"base_level": 3, "consecutive_days": 1}}}}

    async def prepare(self, user="a", **changes):
        """在隔离本地账本中提交一次加入或加急请求。"""

        request = dict(attacker_id=user, attacker_name=user, attacker_pig_id="pig", target_id="target",
                       target_name="目标", group_id="100", delivery_bot_id="bot", date_str="2026-08-07")
        request.update(changes)
        return await self.manager.prepare_roast_reservation(**request)

    def target_arrives(self):
        """设置目标已完成今日抽取的持久记录。"""

        self.manager.data.setdefault("history", {})["2026-08-07"] = {"target": "pig"}

    async def finish(self, event_type="success"):
        """按真实领取、固化、发送态与完成流程结束当前场次。"""

        result = await self.manager.claim_roast_reservations("bot", "2026-08-07")
        self.assertEqual(len(result.reservations), 1)
        reservation = result.reservations[0]
        await self.manager.save_roast_reservation_outcome(reservation.reservation_id, reservation.claim_token,
                                                         {"event_type": event_type})
        await self.manager.mark_roast_reservation_sending(reservation.reservation_id, reservation.claim_token)
        await self.manager.complete_roast_reservation(reservation.reservation_id, reservation.claim_token)

    async def test_two_accelerated_people_complete_four_point_protection(self):
        self.target_arrives()
        first = await self.prepare(force_mode="normal")
        self.assertEqual(first.reservation.status, "pending")
        self.assertEqual(first.reservation.contribution_count, 2)
        repeated = await self.prepare(force_mode="normal")
        self.assertEqual(repeated.status, "already_joined")
        joined = await self.prepare("b", force_mode="normal")
        self.assertEqual(joined.reservation.status, "ready")
        self.assertEqual(joined.reservation.contribution_count, 4)
        self.assertEqual((await self.prepare("c")).status, "reservation_busy")

    async def test_level_one_acceleration_needs_no_second_person(self):
        self.manager.data["protection_details"]["2026-08-07"]["100"]["target"]["base_level"] = 1
        self.target_arrives()
        first = await self.prepare(force_mode="normal")
        self.assertEqual(first.reservation.protection_required, 2)
        self.assertEqual(first.reservation.participant_count, 1)
        self.assertEqual(first.reservation.status, "ready")
        flow = fixtures.reservation_flow
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
              patch.object(flow, "_special_target_outcome", return_value=None),
              patch.object(flow.random, "randint", return_value=80) as roll):
            self.assertEqual((await flow.build_reservation_outcome(first.reservation)).event_type, "escape")
        roll.assert_called_once_with(1, 100)

    async def test_super_takeover_full_queue_preserves_roster_and_ordinary_feed(self):
        for index in range(12):
            await self.prepare(f"member-{index}", force_mode="normal" if index == 11 else None)
        takeover = await self.prepare("admin", force_mode="super")
        self.assertEqual(takeover.reservation.participant_count, 12)
        self.assertEqual(takeover.reservation.owner_id, "member-0")
        self.assertEqual(takeover.reservation.force_operator_id, "admin")
        self.assertEqual(len(takeover.reservation.force_feed_user_ids), 11)
        self.assertEqual((await self.prepare("late")).status, "reservation_closed")
        self.assertEqual((await self.prepare("admin", force_mode="super")).status, "already_joined")
        self.assertNotIn("admin", self.manager.data["usage"])
        self.manager.data.setdefault("history", {}).setdefault("2026-08-07", {}).update(
            {f"member-{index}": "pig" for index in range(12)})
        await self.manager.get_or_create_today_pig("target", "pig", "2026-08-07")
        item = (await self.manager.claim_roast_reservations("bot", "2026-08-07")).reservations[0]
        saved = await self.manager.save_roast_reservation_outcome(item.reservation_id, item.claim_token,
                                                                 {"event_type": "success"}, settle_daily_feed=True)
        self.assertEqual({feed.user_id for feed in saved.daily_feed_results}, {f"member-{index}" for index in range(11)})
        reloaded = fixtures.PigDataManager()
        restored = reloaded._reservation_from_raw(reloaded.data["roast_reservations"][item.reservation_id])
        self.assertEqual(restored.force_operator_id, "admin")
        event = fixtures.reservation_flow._build_reservation_event(restored, fixtures.RoastOutcome("success"))
        self.assertEqual((event.attacker_id, event.reservation_owner_id), ("admin", "member-0"))

    async def test_daily_coupon_streak_uses_starting_level_not_breakthroughs(self):
        self.manager.data["protected"] = {}
        self.manager.data["protection_details"] = {}
        for offset, level in enumerate((3, 4, 5, 5)):
            date = (datetime.date(2026, 8, 7) + datetime.timedelta(days=offset)).isoformat()
            await self.manager.replace_group_protected_users("100", ["target"], date)
            await self.manager.replace_group_protected_users("100", ["target"], date)
            detail = self.manager.data["protection_details"][date]["100"]["target"]
            self.assertEqual(detail, {"base_level": level, "consecutive_days": offset + 1})
            self.assertEqual(self.manager._protection_required_locked(date, "100", "target"), level + 1)
        await self.manager.replace_group_protected_users("100", ["target"], "2026-08-12")
        await self.manager.replace_group_protected_users("200", ["target"], "2026-08-10")
        self.assertEqual(self.manager._protection_required_locked("2026-08-12", "100", "target"), 4)
        self.assertEqual(self.manager._protection_required_locked("2026-08-10", "200", "target"), 4)

    async def test_executor_already_in_roster_loses_only_own_feed_eligibility(self):
        await self.prepare("admin")
        await self.prepare("b")
        result = await self.prepare("admin", force_mode="super")
        self.assertEqual(result.reservation.participant_count, 2)
        self.assertEqual(result.reservation.force_feed_user_ids, ("b",))
        self.manager.data["history"] = {"2026-08-07": {"admin": "pig", "b": "pig"}}
        await self.manager.get_or_create_today_pig("target", "pig", "2026-08-07")
        item = (await self.manager.claim_roast_reservations("bot", "2026-08-07")).reservations[0]
        saved = await self.manager.save_roast_reservation_outcome(item.reservation_id, item.claim_token,
                    {"event_type": "success"}, settle_daily_feed=True)
        self.assertEqual([feed.user_id for feed in saved.daily_feed_results], ["b"])

    async def test_backfire_counts_for_protection_without_raising_current_level(self):
        with (patch.object(fixtures.data_manager_module, "rollpig_date_str", return_value="2026-08-07"),
              patch.object(fixtures.data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 7))):
            await self.manager.log_roast_event("backfire", "target", "other", group_id="100")
            self.assertEqual(self.manager.count_success_roasted("target", 1), 1)
        self.assertEqual(self.manager._protection_required_locked("2026-08-07", "100", "target"), 4)

    def test_forced_reservation_success_goes_to_real_operator_ranking(self):
        raw = {"type": "success", "attacker": "admin", "target": "target", "force_mode": "super",
               "reservation_id": "r", "participant_ids": ["a", "b"], "participant_count": 2,
               "reservation_owner_id": "a", "group_id": "100"}
        events = daily_report.normalize_daily_events([raw], group_id="100")
        self.assertEqual(daily_report.reservation_party_size(events[0]), 2)
        rankings = daily_report.build_rankings(["admin", "a", "b"], {"admin": "pig", "a": "pig"}, events, {}, {})
        ranking = next(item for item in rankings if item.kind == "roast_success")
        self.assertEqual([(entry.user_id, entry.score) for entry in ranking.entries], [("admin", 1)])

    def test_each_special_kind_can_together_qualify_for_next_day_protection(self):
        from nonebot_plugin_rollpig_plus.daily_protection import select_daily_protected_user_ids
        events = daily_report.normalize_daily_events([
            {"type": "bot_backfire", "attacker": "a", "target": "bot", "group_id": "100"},
            {"type": "bot_backfire", "attacker": "a", "target": "bot", "group_id": "100"},
            {"type": "backfire", "special_reason": "unrolled", "attacker": "a", "target": "target", "group_id": "100"},
        ], group_id="100")
        self.assertEqual(select_daily_protected_user_ids(events), ["a"])

    async def test_accelerated_upgrade_consumes_once_and_survives_reload(self):
        await self.prepare()
        upgraded = await self.prepare(force_mode="normal")
        self.assertEqual(upgraded.status, "reservation_upgraded")
        reloaded = fixtures.PigDataManager()
        reservation = next(iter(reloaded.data["roast_reservations"].values()))
        self.assertEqual(reservation["participants"][0]["contribution"], 2)
        self.assertEqual((await self.prepare(force_mode="normal")).status, "already_joined")
        self.assertEqual((await self.prepare("a", target_id="another", force_mode="normal")).status, "force_denied")

    async def test_draw_and_points_are_both_required(self):
        for user in "abcd":
            result = await self.prepare(user)
        self.assertEqual(result.reservation.status, "pending")
        self.assertEqual(result.reservation.contribution_count, 4)
        await self.manager.get_or_create_today_pig("target", "pig", "2026-08-07")
        self.assertEqual(next(iter(self.manager.data["roast_reservations"].values()))["status"], "ready")

    async def test_draw_before_threshold_keeps_waiting_and_reply_can_join(self):
        created = await self.prepare()
        await self.manager.get_or_create_today_pig("target", "pig", "2026-08-07")
        raw = self.manager.data["roast_reservations"][created.reservation.reservation_id]
        self.assertEqual(raw["status"], "pending")
        self.assertEqual(raw["target_pig_id"], "pig")
        await self.manager.bind_roast_reservation_message(reservation_id=created.reservation.reservation_id,
                                                          bot_id="bot", group_id="100", message_id="42", date_str="2026-08-07")
        self.manager.data["history"]["2026-08-07"]["b"] = "pig"
        result = await self.manager.join_roast_reservation_by_message(
            bot_id="bot", group_id="100", message_id="42", attacker_id="b", attacker_name="乙", date_str="2026-08-07")
        self.assertEqual(result.status, "reservation_joined")

    async def test_levels_increase_to_eight_and_keep_round_history(self):
        self.target_arrives()
        for round_index, required in enumerate((4, 5, 6, 6, 6, 6)):
            first = await self.prepare(f"r{round_index}-0")
            self.assertEqual(first.reservation.protection_required, required)
            for member in range(1, required):
                await self.prepare(f"r{round_index}-{member}")
            await self.finish()
        self.assertEqual(len(self.manager.data["roast_reservations"]), 6)
        self.assertTrue(self.manager.is_protected("100", "target", "2026-08-07"))

    async def test_failures_do_not_upgrade_and_day_group_are_isolated(self):
        self.target_arrives()
        for user in "abcd":
            await self.prepare(user)
        await self.finish("escape")
        self.assertEqual((await self.prepare("next")).reservation.protection_required, 4)
        self.manager.data["protected"]["2026-08-07"]["200"] = ["target"]
        self.assertEqual((await self.prepare("other", group_id="200")).reservation.protection_required, 2)
        self.manager.data["protected"]["2026-08-08"] = {"100": ["target"]}
        self.assertEqual((await self.prepare("tomorrow", date_str="2026-08-08")).reservation.protection_required, 2)

    async def test_same_day_startup_cleanup_preserves_protection_level_after_reload(self):
        self.target_arrives()
        for user in "abcd":
            await self.prepare(user)
        await self.finish()
        self.manager = fixtures.PigDataManager()
        with patch.object(fixtures.data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 7)):
            await self.manager.clean_old_history()
        self.manager = fixtures.PigDataManager()
        self.assertEqual(len(self.manager.data["roast_reservations"]), 1)
        self.assertEqual((await self.prepare("next")).reservation.protection_required, 5)
        with patch.object(fixtures.data_manager_module, "rollpig_today", return_value=datetime.date(2026, 8, 8)):
            await self.manager.clean_old_history()
        self.manager = fixtures.PigDataManager()
        self.assertFalse(self.manager.data["roast_reservations"])
        self.manager.data["protected"]["2026-08-08"] = {"100": ["target"]}
        self.assertEqual((await self.prepare("tomorrow", date_str="2026-08-08")).reservation.protection_required, 2)

    async def test_concurrent_joins_freeze_one_ready_round(self):
        self.target_arrives()
        await self.prepare()
        results = await asyncio.gather(*(self.prepare(f"u{index}") for index in range(8)))
        self.assertEqual(sum(result.status == "reservation_joined" for result in results), 3)
        self.assertEqual(len(self.manager.data["roast_reservations"]), 1)

    async def test_super_force_bypasses_gathering_without_charges(self):
        self.target_arrives()
        result = await self.prepare(force_mode="super")
        self.assertEqual(result.reservation.status, "ready")
        self.assertEqual(result.reservation.participant_count, 1)
        self.assertNotIn("a", self.manager.data["usage"])

    async def test_mixed_contributions_feed_only_normal_participants(self):
        self.target_arrives()
        await self.prepare(force_mode="normal")
        await self.prepare("b")
        await self.prepare("c")
        item = (await self.manager.claim_roast_reservations("bot", "2026-08-07")).reservations[0]
        saved = await self.manager.save_roast_reservation_outcome(item.reservation_id, item.claim_token,
                    {"event_type": "success"}, settle_daily_feed=True)
        self.assertEqual({result.user_id for result in saved.daily_feed_results}, {"b", "c"})


class ProtectionCopyTests(unittest.IsolatedAsyncioTestCase):
    def preparation(self, status="reservation_created", **changes):
        """构造保护添柴通知所需的场次状态。"""
        values = dict(protection_required=4, force_mode=None, contribution_count=1, participant_count=1,
                      status="pending", target_pig_id="pig", participants=[fixtures.SimpleNamespace(contribution=1)])
        values.update(changes)
        return fixtures.SimpleNamespace(status=status, reservation=fixtures.SimpleNamespace(**values))

    def text(self, preparation):
        """读取实际通知文案，并固定随机候选以便核对。"""
        with patch("nonebot_plugin_rollpig_plus.roast_flow.random.choice", side_effect=lambda pool: pool[0]):
            return pick_reservation_prepare_text(preparation, attacker_name="小红", target_name="小蓝")

    def test_created_and_joined_show_fire_without_repeating_instructions(self):
        created = self.text(self.preparation())
        self.assertEqual(
            created,
            "【小蓝】今天受到猪圈保护协议庇护——昨天被烤太惨了，本该歇上一天。\n"
            "但【小红】不打算放过，已经在门口架起炉子摇人了——或许再多凑几个人一起添柴，就能硬生生打破保护！",
        )
        joined = self.text(self.preparation("reservation_joined", contribution_count=2, participant_count=2))
        self.assertIn("【小红】", joined)
        self.assertIn("【小蓝】", joined)
        self.assertNotIn("Lv.", joined)
        self.assertNotIn("回复", joined)

    def test_accelerated_join_upgrade_and_duplicate(self):
        created_boost = self.text(self.preparation("reservation_created", participants=[fixtures.SimpleNamespace(contribution=2)]))
        self.assertIn("双倍干柴", created_boost)
        joined = self.text(self.preparation("reservation_joined", participants=[fixtures.SimpleNamespace(contribution=2)]))
        self.assertIn("双倍猛火", joined)
        upgraded = self.text(self.preparation("reservation_upgraded"))
        self.assertIn("掏出今日加急", upgraded)
        self.assertNotIn("2 点", upgraded)
        self.assertIn("小板凳", self.text(self.preparation("already_joined")))

    def test_points_ready_without_pig_explains_only_remaining_wait(self):
        text = self.text(self.preparation("reservation_joined", target_pig_id="", contribution_count=4, participant_count=4))
        self.assertIn("就差【小蓝】今天露面抽猪", text)
        self.assertNotIn("判定", text)
        self.assertEqual(self.text(self.preparation(status="reservation_busy")), "后厨已经关门点火了，别在门口挤了，等着端盘子看结果吧！")
        self.assertIn("已经结束", self.text(self.preparation(status="reservation_closed")))
        self.assertNotIn("还没抽猪", self.text(self.preparation(status="reservation_full", participant_count=20)))

    async def test_ready_round_delivers_without_sending_or_binding_notice(self):
        preparation = self.preparation()
        preparation.reservation.status = "ready"
        self.assertEqual(self.text(preparation), "")
        matcher = fixtures.SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
        event = fixtures.SimpleNamespace(self_id=1000)
        handler = fixtures.roast_handler
        with (patch.object(handler, "deliver_newly_ready_reservations", new=AsyncMock()) as deliver,
              patch.object(handler, "store") as store):
            await handler._send_reservation_notice(matcher, event, preparation, attacker_name="小红", target_name="小蓝")
        deliver.assert_awaited_once_with("1000")
        matcher.send.assert_not_awaited()
        store.bind_roast_reservation_message.assert_not_called()
        matcher.finish.assert_awaited_once_with()


class ProtectionOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def reservation(self, mode=None, required=4):
        """构造带长昵称的多人结果样本。"""

        return RoastReservation("sample", "2026-08-07", "100", "target", "目标", "a", "甲", "pig",
                                participants=tuple(RoastReservationParticipant(str(index), "长昵称" * 12 + str(index), "pig")
                                                   for index in range(6)),
                                target_pig_id="pig", force_mode=mode, protection_required=required)

    async def test_accelerated_protected_round_still_rolls_and_super_does_not(self):
        flow = fixtures.reservation_flow
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
              patch.object(flow, "is_human_pig", return_value=False),
              patch.object(flow, "is_food_pig", return_value=False),
              patch.object(flow, "is_eaten_pig", return_value=False),
              patch.object(flow, "is_sold_pig", return_value=False),
              patch.object(flow.random, "randint", return_value=80) as roll,
              patch.object(flow, "build_success_roast_outcome", new=AsyncMock(return_value=fixtures.RoastOutcome("success")))):
            self.assertEqual((await flow.build_reservation_outcome(self.reservation("normal"))).event_type, "escape")
            roll.assert_called_once_with(1, 100)
            roll.reset_mock()
            self.assertEqual((await flow.build_reservation_outcome(self.reservation("super"))).event_type, "success")
            roll.assert_not_called()

    async def test_protected_results_use_their_own_copy_without_new_draw_assumption(self):
        flow = fixtures.reservation_flow
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
              patch.object(flow, "_special_target_outcome", return_value=None),
              patch.object(flow.random, "randint", return_value=1),
              patch.object(flow.random, "choice", side_effect=lambda pool: pool[0]),
              patch.object(flow, "build_success_roast_outcome", new=AsyncMock(return_value=fixtures.RoastOutcome("success"))) as success):
            await flow.build_reservation_outcome(self.reservation())
            self.assertIn("保护令也没拦住这群饿狼", success.await_args.kwargs["extra_text"])
            self.assertNotIn("刚抽", success.await_args.kwargs["extra_text"])
            await flow.build_reservation_outcome(self.reservation("super"))
            self.assertIn("直接亮出最高权限，把【目标】（猪）连猪带盘推进了烤箱！",
                          success.await_args.kwargs["extra_text"])
            await flow.build_reservation_outcome(self.reservation(required=0))
            self.assertIn("等候结束", success.await_args.kwargs["extra_text"])
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
              patch.object(flow, "_special_target_outcome", return_value=None),
              patch.object(flow.random, "randint", return_value=80),
              patch.object(flow.random, "choice", side_effect=lambda pool: pool[0])):
            outcome = await flow.build_reservation_outcome(self.reservation())
        self.assertIn("柴添够了，猪溜了", outcome.plain_text)
        self.assertNotIn("刚抽", outcome.plain_text)

    async def test_multiple_victims_survive_snapshot_and_send_exactly_one_image(self):
        flow = fixtures.reservation_flow
        reservation = self.reservation()
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
              patch.object(flow, "is_human_pig", return_value=False),
              patch.object(flow, "is_food_pig", return_value=False),
              patch.object(flow, "is_eaten_pig", return_value=False),
              patch.object(flow, "is_sold_pig", return_value=False),
              patch.object(flow.random, "randint", return_value=95),
              patch.object(flow.random, "randrange", return_value=6),
              patch.object(flow.random, "sample", return_value=list(reservation.participants)),
              patch.object(flow.random, "choice", side_effect=lambda pool: pool[0]),
              patch.object(flow, "pick_food_pig", return_value={"id": "food", "name": "烤猪", "description": "原副标题", "analysis": "原本熟食描述"})):
            outcome = await flow.build_reservation_outcome(reservation)
        restored = _deserialize_outcome(_serialize_outcome(outcome))
        self.assertEqual(restored, outcome)
        self.assertEqual(len(outcome.backfire_victim_ids), 6)
        self.assertIn("6 位帮厨被一锅端了", outcome.extra_text)
        self.assertNotIn("反噬", outcome.extra_text)
        self.assertEqual(outcome.extra_text.count("\n"), 2)
        self.assertTrue(outcome.extra_text.endswith("倒霉蛋：" + "、".join(outcome.backfire_victim_names) + "\n"))
        self.assertEqual(outcome.render_data["description"], "说好一起添柴，怎么一起熟了")
        self.assertEqual(
            outcome.render_data["analysis"],
            "原本是6 只【猪】，进炉走了一遭后彻底放下了生前的体面，整整齐齐在同一个盘子里合体成了这份【烤猪】。",
        )
        for participant in reservation.participants:
            self.assertIn(participant.display_name, outcome.extra_text)
            self.assertNotIn(participant.display_name, outcome.render_data["analysis"])
        with patch.object(flow, "render_pig_card_image", new=AsyncMock(return_value=fixtures.SimpleNamespace(data=b"image"))):
            message = await flow._prepare_reservation_message(restored)
        self.assertEqual(sum(segment.type == "image" for segment in message), 1)
        event = flow._build_reservation_event(reservation, outcome)
        self.assertEqual(event.backfire_victim_ids, outcome.backfire_victim_ids)

    async def test_old_cloud_does_not_receive_protected_mutation(self):
        store = object.__new__(CloudStore)
        store._reservation_request = AsyncMock(side_effect=CloudReservationUnsupportedError("old"))
        store._request = AsyncMock(return_value={"protected": True})
        with self.assertRaises(CloudStoreError):
            await store.prepare_roast_reservation(attacker_id="a", attacker_name="甲", attacker_pig_id="pig",
                                                  target_id="target", target_name="乙", group_id="100",
                                                  delivery_bot_id="bot", force_mode="normal")
        self.assertEqual(store._reservation_request.await_args.args[0], "GET")
        self.assertEqual(store._reservation_request.await_count, 1)

    async def test_multi_backfire_copy_keeps_inline_names_for_all_templates(self):
        flow = fixtures.reservation_flow
        for count in (2, 12):
            participants = tuple(RoastReservationParticipant(str(index), "长昵称" * 12 + str(index), "pig")
                                 for index in range(count))
            reservation = replace(self.reservation(required=0), participants=participants)
            for template in flow.ROAST_RESERVATION_MULTI_BACKFIRE_TEXTS:
                with (
                    self.subTest(count=count, template=template),
                    patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
                    patch.object(flow, "_special_target_outcome", return_value=None),
                    patch.object(flow, "can_backfire_roast", return_value=True),
                    patch.object(flow.random, "randint", return_value=95),
                    patch.object(flow.random, "randrange", return_value=count),
                    patch.object(flow.random, "sample", return_value=list(participants)),
                    patch.object(flow.random, "choice", side_effect=lambda pool: template if pool is flow.ROAST_RESERVATION_MULTI_BACKFIRE_TEXTS else pool[0]),
                    patch.object(flow, "pick_food_pig", return_value={"id": "food", "name": "烤猪", "analysis": "原文\n第二段"}),
                ):
                    outcome = await flow.build_reservation_outcome(reservation)
                lines = outcome.extra_text.rstrip("\n").split("\n")
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines[1], "倒霉蛋：" + "、".join(item.display_name for item in participants))
                self.assertIn("目标", lines[0])
                self.assertIn(str(count), lines[0])
                self.assertIn("【烤猪】", outcome.render_data["analysis"])
                self.assertEqual(len(outcome.backfire_victim_ids), count)

    async def test_all_unroastable_victims_use_compact_text_without_card(self):
        flow = fixtures.reservation_flow
        participants = (RoastReservationParticipant("a", "甲", "pig"),
                        RoastReservationParticipant("b", "", "pig"))
        reservation = replace(self.reservation(), participants=participants)
        for template in flow.ROAST_RESERVATION_MULTI_BACKFIRE_NO_FOOD_TEXTS:
            with (
                self.subTest(template=template),
                patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "猪"}),
                patch.object(flow, "_special_target_outcome", return_value=None),
                patch.object(flow, "can_backfire_roast", return_value=False),
                patch.object(flow.random, "randint", return_value=95),
                patch.object(flow.random, "randrange", return_value=2),
                patch.object(flow.random, "sample", return_value=list(participants)),
                patch.object(flow.random, "choice", return_value=template),
                patch.object(flow, "pick_food_pig") as pick_food,
            ):
                outcome = await flow.build_reservation_outcome(reservation)
            pick_food.assert_not_called()
            self.assertIsNone(outcome.render_data)
            self.assertEqual(outcome.plain_text.split("\n")[1:], ["倒霉蛋：甲、b"])
            self.assertEqual(outcome.backfire_victim_ids, ("a", "b"))

    async def test_mixed_backfire_excludes_unroastable_members_from_card_ingredients(self):
        flow = fixtures.reservation_flow
        reservation = replace(self.reservation(), participants=(
            RoastReservationParticipant("b", "长昵称乙", "human"),
            RoastReservationParticipant("a", "长昵称甲", "pig"),
        ))
        pig = {"id": "pig", "name": "小猪"}
        human = {"id": "human", "name": "人类猪"}
        food = {"id": "food", "name": "烤猪", "analysis": "抽猪正文"}
        with (
            patch.object(flow, "get_pig_by_id", side_effect=lambda pig_id: human if pig_id == "human" else pig),
            patch.object(flow, "_special_target_outcome", return_value=None),
            patch.object(flow, "can_backfire_roast", side_effect=lambda item: item["id"] != "human"),
            patch.object(flow.random, "randint", return_value=95),
            patch.object(flow.random, "randrange", return_value=2),
            patch.object(flow.random, "sample", return_value=list(reservation.participants)),
            patch.object(flow.random, "choice", side_effect=lambda pool: pool[0]),
            patch.object(flow, "pick_food_pig", return_value=food),
        ):
            outcome = await flow.build_reservation_outcome(reservation)
        self.assertEqual(outcome.backfire_victim_id, "a")
        self.assertEqual(outcome.backfire_victim_ids, ("b", "a"))
        self.assertEqual(outcome.render_data["description"], "说好一起添柴，怎么一起熟了")
        self.assertIn("【小猪】", outcome.render_data["analysis"])
        self.assertNotIn("人类猪", outcome.render_data["analysis"])
        self.assertIn("独自撑起了这盘【烤猪】", outcome.render_data["analysis"])
        self.assertEqual(food["analysis"], "抽猪正文")

    def test_all_victims_have_personal_backfire_view_and_daily_names(self):
        raw = dict(type="backfire", reservation_id="round", attacker="a", target="target", group_id="100",
                   participant_ids=["a", "b", "c"], participant_names=["甲", "乙", "丙"],
                   backfire_victim_id="a", backfire_victim_name="甲",
                   backfire_victim_ids=["a", "c"], backfire_victim_names=["甲", "丙"])
        for user in ("a", "c"):
            events = yesterday_recap.normalize_yesterday_events([raw], date_str="2026-08-07", user_id=user)
            self.assertEqual(events[0].family, "reservation_backfire_victim")
        self.assertEqual(yesterday_recap.normalize_yesterday_events([raw], date_str="2026-08-07", user_id="b")[0].family,
                         "reservation_backfire_participant")
        event = daily_report.normalize_daily_events([raw], group_id="100")[0]
        self.assertEqual(event.backfire_victim_ids, ("a", "c"))
