from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_roast_reservations as fixtures
from nonebot_plugin_rollpig_plus import daily_report_card_renderer as renderer
from nonebot_plugin_rollpig_plus.daily_report import (
    ProtectionReportItem, build_daily_report,
)
from nonebot_plugin_rollpig_plus.store.cloud import CloudStore
from nonebot_plugin_rollpig_plus.store.local_json import LocalJsonStore


DATE = "2026-10-23"


def event(event_type="success", **changes):
    """构造带队伍、保护与昵称快照的本群事件。"""
    return {
        "event_id": "sample", "type": event_type, "attacker": "a", "attacker_name": "甲",
        "target": "target", "target_name": "目标", "group_id": "100", "reservation_id": "round",
        "participant_ids": ["a", "b", "c"], "participant_names": ["甲", "乙", "丙"],
        "participant_count": 3, "protection_level": 3, **changes,
    }


def report(*events):
    """使用真实日报装配流程生成供卡片转换使用的快照。"""
    return build_daily_report(
        date_str=DATE, group_id="100", group_rolls={"a": "pig", "target": "pig"},
        raw_events=events,
    )


def text(card):
    return "".join(span.text for span in card.spans)


class DailyReportProtectionCopyTests(unittest.TestCase):
    def test_protection_break_headline_uses_approved_copy_and_pig_name(self):
        with patch.object(renderer, "get_pig_by_id", return_value={"name": "测试猪"}):
            card = renderer.build_daily_report_card_data(report(event())).headline
        self.assertEqual(card.tags, ("聚众烤猪", "3 人围攻", "突破 Lv.3"))
        self.assertEqual(text(card), "甲、乙等 3 人合力围住目标，硬是撬开了 Lv.3 的保护防线，这只测试猪最终还是上了菜单。")

    def test_super_independent_and_takeover_have_distinct_headlines(self):
        independent = event(force_mode="super", participant_ids=["a"], participant_names=["甲"], participant_count=1)
        card = renderer.build_daily_report_card_data(report(independent)).headline
        self.assertEqual(card.tags, ("强行点火", "无视保护", "强制出餐"))
        self.assertEqual(text(card), "甲直接亮出最高权限闯进后厨，目标的保护券还没来得及亮，烤箱门就已经关上了。")
        takeover = event(force_mode="super", attacker="admin", attacker_name="管理员",
                         reservation_owner_id="a", reservation_owner_name="甲")
        card = renderer.build_daily_report_card_data(report(takeover)).headline
        self.assertEqual(card.tags, ("强行点火", "半路接管", "强制出餐"))
        self.assertEqual(text(card), "甲、乙等 3 人还在炉边攒火，管理员已经亮出最高权限接过夹子，直接把目标送进了烤箱。")
        self.assertNotIn("管理员、", text(card))

    def test_unprotected_super_does_not_invent_a_protection_coupon(self):
        events = [event(event_id=str(index), reservation_id="", force_mode="super", protection_level=0)
                  for index in range(3)]
        card = renderer.build_daily_report_card_data(report(*events)).headline
        self.assertEqual(card.tags, ("强行点火", "强制出餐"))
        self.assertNotIn("保护券", text(card))

    def test_multi_backfire_headline_names_actual_victims(self):
        card = renderer.build_daily_report_card_data(report(event(
            "backfire", backfire_victim_ids=["b", "c"], backfire_victim_names=["乙", "丙"],
        ))).headline
        self.assertEqual(card.tags, ("预约烤猪", "3 人参与", "连环走火"))
        self.assertEqual(text(card), "甲、乙等 3 人原本冲着目标去，结果炉火当场掉头，乙、丙反倒一起上了菜单。")

    def test_protected_observation_uses_reservation_count_and_results(self):
        data = report(event(event_id="one"), event("escape", event_id="two", reservation_id="second", protection_level=0))
        card = renderer.build_daily_report_card_data(data).observation
        self.assertEqual(text(card), "今天 2 炉预约里，有 1 炉冲着保护目标下手，\n最终 1 炉开席、1 炉扑空，还有 0 炉把自己人搭了进去。")

    def test_backfire_observation_counts_person_times_including_repeated_victim(self):
        events = [event("backfire", event_id=str(index), reservation_id="", protection_level=0,
                        backfire_victim_ids=["b", "c", "b"], backfire_victim_names=["乙", "丙", "乙"])
                  for index in range(2)]
        data = report(*events, event("escape", event_id="other", reservation_id="", protection_level=0))
        card = renderer.build_daily_report_card_data(data).observation
        self.assertEqual(text(card), "后厨今天一共回火 2 次，累计把 4 人次卷进了烤箱，\n凑热闹添柴的人不少，最后添成食材的也不少。")

    def test_timeline_uses_all_approved_short_details_and_respects_max_level(self):
        cases = (
            (event(), "众人突破目标的 Lv.3 保护\n目标保护升至 Lv.4"),
            (event(protection_level=5), "众人突破目标的 Lv.5 保护"),
            (event("escape"), "目标顶着 Lv.3 保护溜了"),
            (event("backfire", backfire_victim_ids=["a", "b"]), "炉火倒灌，卷入 2 人"),
            (event(force_mode="super", protection_level=0, attacker="admin", attacker_name="管理员",
                   reservation_owner_id="a"), "管理员强行点火接管烤架"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                data = report(raw)
                self.assertEqual(renderer._timeline_event_detail(data, data.events[0]), expected)

    def test_coupon_uses_actual_base_level_and_retains_original_layout(self):
        for level in (1, 3, 4, 5):
            with self.subTest(level=level):
                data = replace(report(), protections=(ProtectionReportItem("target", base_level=level),))
                coupon = renderer.build_daily_report_card_data(data).coupon
                self.assertEqual(coupon.benefit, f"保护 Lv.{level}")
                self.assertNotIn("免烤", coupon.benefit)

    def test_new_copy_is_stable_for_repeated_render_data_conversion(self):
        data = report(event(), event("escape", event_id="escape"))
        self.assertEqual(renderer.build_daily_report_card_data(data), renderer.build_daily_report_card_data(data))


class ProtectionCouponStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_coupon_level_uses_saved_base_not_current_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(fixtures.data_manager_module, "DATA_FILE", Path(directory) / "data.json"):
                manager = fixtures.PigDataManager()
                store = LocalJsonStore(lambda: manager)
                for day, level in (("2026-10-22", 3), ("2026-10-23", 4), ("2026-10-24", 5)):
                    await store.replace_group_protections("100", ["target"], day)
                    self.assertEqual(await store.get_protection_base_level("100", "target", day), level)
                manager.data.setdefault("roast_reservations", {})["break"] = {
                    "date_str": DATE, "group_id": "100", "target_id": "target",
                    "protection_required": 5, "outcome_snapshot": {"event_type": "success"},
                }
                self.assertEqual(await store.get_protection_base_level("100", "target", DATE), 4)
                self.assertEqual(await store.get_protection_base_level("200", "target", DATE), 0)
                manager.data["protected"][DATE]["100"].append("legacy")
                self.assertEqual(await store.get_protection_base_level("100", "legacy", DATE), 1)

    async def test_cloud_coupon_uses_base_level_and_legacy_response(self):
        store = object.__new__(CloudStore)
        for payload, expected in (
            ({"protected": True, "base_level": 4, "level": 5}, 4),
            ({"protected": True}, 1), ({"protected": False}, 0),
        ):
            store._request = AsyncMock(return_value=payload)
            self.assertEqual(await store.get_protection_base_level("100", "target", DATE), expected)
            store._request.assert_awaited_once_with(
                "GET", "/v1/protections/check",
                params={"group_id": "100", "user_id": "target", "protect_date": DATE},
            )

    async def test_super_protected_success_uses_approved_success_copy(self):
        flow = fixtures.reservation_flow
        reservation = fixtures.RoastReservation(
            "round", DATE, "100", "target", "目标", "a", "甲", "pig",
            participants=(fixtures.RoastReservationParticipant("a", "甲", "pig"),),
            force_mode="super", target_pig_id="pig", protection_required=4,
        )
        success = AsyncMock(return_value=fixtures.RoastOutcome("success"))
        with (patch.object(flow, "get_pig_by_id", return_value={"id": "pig", "name": "小猪"}),
              patch.object(flow, "_special_target_outcome", return_value=None),
              patch.object(flow, "build_success_roast_outcome", success)):
            await flow.build_reservation_outcome(reservation)
        self.assertEqual(success.await_args.kwargs["extra_text"],
                         fixtures.texts_module.SUPER_FORCE_RESERVATION_SUCCESS_TEXT.format(
                             target="目标", pig="小猪", operator="甲"))
