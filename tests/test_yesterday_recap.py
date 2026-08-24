from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from nonebot.plugin import get_plugin
from PIL import Image, ImageDraw, ImageFont

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus import roll_flow as roll_flow_module
from nonebot_plugin_rollpig_plus import yesterday_card_renderer as yesterday_card_module
from nonebot_plugin_rollpig_plus.handlers import roll as roll_handler_module
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.resource_manager import PigExVariant, RollPigResourceManager
from nonebot_plugin_rollpig_plus.store.cloud import CloudStore, CloudStoreError
from nonebot_plugin_rollpig_plus.store.models import (
    DailyEventQueryResult,
    DailyRollResult,
    DailyRollSnapshot,
)
from nonebot_plugin_rollpig_plus.texts import YESTERDAY_EXPERIENCE_TEXTS, YESTERDAY_SUMMARY_TEXTS
from nonebot_plugin_rollpig_plus.yesterday_recap import (
    YesterdayExperience,
    YesterdayFootprint,
    YesterdayRecap,
    YesterdaySummary,
    build_yesterday_outcome_text,
    build_yesterday_recap,
    build_yesterday_summary,
    normalize_yesterday_events,
    select_yesterday_experiences,
)


DATE = "2026-08-23"


class LocalDailyRollSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_file = Path(self.temp_dir.name) / "pig_data.json"
        self.data_file_patch = patch.object(data_manager_module, "DATA_FILE", self.data_file)
        self.data_file_patch.start()
        self.addCleanup(self.data_file_patch.stop)
        self.manager = PigDataManager()

    async def test_created_snapshot_is_atomic_and_completion_is_first_writer_wins(self):
        result = await self.manager.get_or_create_today_pig("user", "pig", date_str=DATE)

        self.assertTrue(result.snapshot and result.snapshot.outcome_available)
        self.assertEqual(
            (
                result.snapshot.is_new_pig,
                result.snapshot.previous_copies,
                result.snapshot.copies_after_roll,
                result.snapshot.collection_size_after_roll,
            ),
            (True, 0, 1, 1),
        )

        completed = replace(
            result.snapshot,
            resource_version="2026-08-20.1",
            resolved_variant_level=2,
            resolved_image_name="pig_ex2.png",
            unlocked_variant_levels=(2,),
            unlocked_variant_fields=frozenset({"image", "description"}),
        )
        self.assertTrue(await self.manager.complete_daily_roll_snapshot("user", completed))
        self.assertTrue(await self.manager.complete_daily_roll_snapshot("user", completed))

        with self.assertRaises(ValueError):
            await self.manager.complete_daily_roll_snapshot(
                "user",
                replace(completed, resource_version="different"),
            )

        restored = PigDataManager().get_daily_roll_snapshot("user", DATE)
        self.assertEqual(restored, completed)

    async def test_existing_roll_uses_frozen_snapshot_instead_of_current_progress(self):
        created = await self.manager.get_or_create_today_pig("user", "pig", date_str=DATE)
        self.manager.data["pig_progress"]["user"]["pig"]["copies"] = 5

        existing = await self.manager.get_or_create_today_pig("user", "other", date_str=DATE)

        self.assertFalse(existing.created)
        self.assertEqual((existing.previous_copies, existing.copies), (0, 1))
        self.assertEqual(existing.snapshot, created.snapshot)

    async def test_old_history_remains_readable_without_fabricated_growth(self):
        self.data_file.write_text(
            json.dumps({"history": {DATE: {"user": "pig"}}}, ensure_ascii=False),
            encoding="utf-8",
        )
        manager = PigDataManager()

        snapshot = manager.get_daily_roll_snapshot("user", DATE)

        self.assertEqual((snapshot.date_str, snapshot.pig_id), (DATE, "pig"))
        self.assertFalse(snapshot.outcome_available)

    async def test_broken_snapshot_day_is_isolated_without_losing_history(self):
        self.data_file.write_text(
            json.dumps({
                "history": {DATE: {"user": "pig"}},
                "daily_roll_snapshots": {DATE: "broken"},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        manager = PigDataManager()
        snapshot = manager.get_daily_roll_snapshot("user", DATE)

        self.assertEqual((snapshot.date_str, snapshot.pig_id), (DATE, "pig"))
        self.assertFalse(snapshot.outcome_available)
        self.assertEqual(manager.data["daily_roll_snapshots"][DATE], {})

    async def test_event_ids_timestamps_and_user_filter_are_persisted(self):
        with patch.object(data_manager_module, "rollpig_date_str", return_value=DATE):
            await self.manager.log_roast_event(
                "success",
                "user",
                "target",
                attacker_name="用户",
                target_name="目标",
                food="烤猪",
                group_id="100",
            )
            await self.manager.log_roast_event(
                "success",
                "other",
                "target-2",
                group_id="100",
            )

        events = self.manager.get_daily_events(DATE, group_id="100", user_id="user")

        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["event_id"]), 32)
        self.assertIn("T", events[0]["created_at"])

    async def test_history_cleanup_removes_matching_snapshots(self):
        await self.manager.get_or_create_today_pig("user", "pig", date_str="2026-01-01")

        await self.manager.clean_old_history(days_to_keep=14)

        self.assertNotIn("2026-01-01", self.manager.data["history"])
        self.assertNotIn("2026-01-01", self.manager.data["daily_roll_snapshots"])


class CloudDailyRollSnapshotCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_and_legacy_responses_are_distinguished(self):
        store = object.__new__(CloudStore)
        store._request = AsyncMock(return_value={
            "pig_id": "pig",
            "is_new_pig": False,
            "previous_copies": 2,
            "copies": 3,
            "outcome_snapshot": {
                "snapshot_available": True,
                "collection_size_after_roll": 37,
                "resource_version": "2026-08-20.1",
                "resolved_variant_level": 2,
                "resolved_image_name": "pig_ex2.png",
                "unlocked_variant_levels": [2],
                "unlocked_variant_fields": ["image", "analysis"],
            },
        })

        snapshot = await store.get_daily_roll_snapshot("user", DATE)

        self.assertTrue(snapshot.outcome_available)
        self.assertEqual(snapshot.unlocked_variant_fields, frozenset({"image", "analysis"}))

        store._request = AsyncMock(return_value={"pig_id": "pig", "created": False})
        legacy = await store.get_daily_roll_snapshot("user", DATE)
        self.assertFalse(legacy.outcome_available)

    async def test_pending_cloud_snapshot_is_only_exposed_to_completion_flow(self):
        payload = {
            "pig_id": "pig",
            "created": True,
            "is_new_pig": True,
            "previous_copies": 0,
            "copies": 1,
            "previous_duplicate_streak": 0,
            "duplicate_streak": 0,
            "outcome_snapshot": {
                "snapshot_available": False,
                "collection_size_after_roll": 37,
                "resource_version": "",
                "resolved_variant_level": 0,
                "resolved_image_name": "",
                "unlocked_variant_levels": [],
                "unlocked_variant_fields": [],
            },
        }
        store = object.__new__(CloudStore)
        store._request = AsyncMock(return_value=payload)

        historical = await store.get_daily_roll_snapshot("user", DATE)
        current = await store.get_or_create_daily_roll("user", "pig", date_str=DATE)

        self.assertFalse(historical.outcome_available)
        self.assertTrue(current.snapshot and current.snapshot.outcome_available)
        self.assertEqual(current.snapshot.collection_size_after_roll, 37)
        self.assertEqual(current.snapshot.resource_version, "")
        self.assertIsNone(current.snapshot.resolved_variant_level)

    async def test_old_cloud_snapshot_endpoint_is_disabled_after_first_404(self):
        request = httpx.Request("PUT", "https://cloud.example/v1/daily-rolls/snapshot")
        response = httpx.Response(404, request=request)
        status_error = httpx.HTTPStatusError("not found", request=request, response=response)
        cloud_error = CloudStoreError("not found")
        cloud_error.__cause__ = status_error

        store = object.__new__(CloudStore)
        store._daily_roll_snapshot_supported = None
        store._request = AsyncMock(side_effect=cloud_error)
        snapshot = DailyRollSnapshot(
            date_str=DATE,
            pig_id="pig",
            is_new_pig=True,
            previous_copies=0,
            copies_after_roll=1,
            collection_size_after_roll=1,
            resource_version="version",
        )

        self.assertFalse(await store.complete_daily_roll_snapshot("user", snapshot))
        self.assertFalse(await store.complete_daily_roll_snapshot("user", snapshot))
        self.assertEqual(store._request.await_count, 1)

    async def test_event_query_failure_is_not_reported_as_zero_events(self):
        store = object.__new__(CloudStore)
        store._request = AsyncMock(side_effect=CloudStoreError("offline"))

        result = await store.query_daily_events(DATE, user_id="user")

        self.assertFalse(result.available)
        self.assertEqual(result.items, ())


class DailyRollAppearanceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        with Image.new("RGBA", (8, 8), (255, 180, 200, 255)) as image:
            image.save(self.root / "pig.png", format="PNG")
        with Image.new("RGBA", (8, 8), (120, 180, 255, 255)) as image:
            image.save(self.root / "pig_ex2.png", format="PNG")
        self.manager = RollPigResourceManager()
        self.manager.pig_list = [{"id": "pig", "name": "猪", "description": "基础", "analysis": "基础"}]
        self.manager.pig_map = {"pig": self.manager.pig_list[0]}
        self.manager.image_dirs = [self.root]
        self.manager.resource_version = "test-version"

    def _result(self) -> DailyRollResult:
        snapshot = DailyRollSnapshot(
            date_str=DATE,
            pig_id="pig",
            is_new_pig=False,
            previous_copies=2,
            copies_after_roll=3,
            collection_size_after_roll=20,
        )
        return DailyRollResult("pig", True, previous_copies=2, copies=3, snapshot=snapshot)

    def test_precise_variant_fields_are_saved(self):
        self.manager.ex_variants = {
            "pig": {
                2: PigExVariant(
                    pig_id="pig",
                    level=2,
                    image_path=self.root / "pig_ex2.png",
                    description="新描述",
                )
            }
        }
        with patch.object(roll_flow_module, "pig_resource_manager", self.manager):
            snapshot = roll_flow_module.build_completed_daily_roll_snapshot(
                self._result(),
                self.manager.pig_map["pig"],
            )

        self.assertEqual(snapshot.resolved_image_name, "pig_ex2.png")
        self.assertEqual(snapshot.unlocked_variant_levels, (2,))
        self.assertEqual(snapshot.unlocked_variant_fields, frozenset({"image", "description"}))

    def test_missing_variant_image_does_not_claim_unlock(self):
        self.manager.ex_variants = {
            "pig": {
                2: PigExVariant(
                    pig_id="pig",
                    level=2,
                    image_path=self.root / "missing.png",
                    description="不会单独应用",
                )
            }
        }
        with patch.object(roll_flow_module, "pig_resource_manager", self.manager):
            snapshot = roll_flow_module.build_completed_daily_roll_snapshot(
                self._result(),
                self.manager.pig_map["pig"],
            )

        self.assertEqual(snapshot.resolved_image_name, "pig.png")
        self.assertEqual(snapshot.unlocked_variant_levels, ())
        self.assertEqual(snapshot.unlocked_variant_fields, frozenset())

    def test_broken_variant_image_does_not_claim_unlock(self):
        broken = self.root / "pig_ex2_broken.png"
        broken.write_bytes(b"this is not a png")
        self.manager.ex_variants = {
            "pig": {
                2: PigExVariant(
                    pig_id="pig",
                    level=2,
                    image_path=broken,
                    analysis="不会随坏图应用",
                )
            }
        }
        with patch.object(roll_flow_module, "pig_resource_manager", self.manager):
            snapshot = roll_flow_module.build_completed_daily_roll_snapshot(
                self._result(),
                self.manager.pig_map["pig"],
            )

        self.assertEqual(snapshot.resolved_image_name, "pig.png")
        self.assertEqual(snapshot.resolved_variant_level, 0)
        self.assertEqual(snapshot.unlocked_variant_fields, frozenset())


class DailyRollSnapshotCompletionIsolationTests(unittest.IsolatedAsyncioTestCase):
    def _result(self, *, resource_version: str = "") -> DailyRollResult:
        return DailyRollResult(
            "pig",
            True,
            previous_copies=0,
            copies=1,
            snapshot=DailyRollSnapshot(
                date_str=DATE,
                pig_id="pig",
                is_new_pig=True,
                previous_copies=0,
                copies_after_roll=1,
                collection_size_after_roll=1,
                resource_version=resource_version,
            ),
        )

    async def test_resource_resolution_failure_does_not_block_daily_roll(self):
        result = self._result()
        with patch.object(
            roll_flow_module,
            "build_completed_daily_roll_snapshot",
            side_effect=RuntimeError("broken resources"),
        ), patch.object(roll_flow_module.store, "complete_daily_roll_snapshot", new=AsyncMock()) as complete:
            resolved = await roll_flow_module._complete_daily_roll_snapshot("user", result, {"id": "pig"})

        self.assertIs(resolved, result)
        complete.assert_not_awaited()

    async def test_completed_snapshot_is_not_redecoded_or_resubmitted(self):
        result = self._result(resource_version="2026-08-20.1")
        with patch.object(
            roll_flow_module,
            "build_completed_daily_roll_snapshot",
        ) as build, patch.object(
            roll_flow_module.store,
            "complete_daily_roll_snapshot",
            new=AsyncMock(),
        ) as complete:
            resolved = await roll_flow_module._complete_daily_roll_snapshot("user", result, {"id": "pig"})

        self.assertIs(resolved, result)
        build.assert_not_called()
        complete.assert_not_awaited()


class YesterdayRecapBusinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        (self.root / "pig.png").write_bytes(b"base")
        self.resources = RollPigResourceManager()
        self.resources.pig_list = [{"id": "pig", "name": "猪序员"}]
        self.resources.pig_map = {"pig": self.resources.pig_list[0]}
        self.resources.image_dirs = [self.root]
        self.snapshot = DailyRollSnapshot(
            date_str=DATE,
            pig_id="pig",
            is_new_pig=False,
            previous_copies=2,
            copies_after_roll=3,
            collection_size_after_roll=37,
            resource_version="2026-08-20.1",
            resolved_variant_level=2,
            resolved_image_name="pig_ex2.png",
            unlocked_variant_levels=(2,),
            unlocked_variant_fields=frozenset({"image", "description"}),
        )

    def _event(self, index: int, event_type: str = "success", **overrides) -> dict:
        payload = {
            "event_id": str(index),
            "created_at": f"2026-08-23T0{index}:00:00+00:00",
            "type": event_type,
            "attacker": "user",
            "target": f"target-{index}",
            "attacker_name": "本人",
            "target_name": f"目标{index}",
            "food": "烤乳猪",
            "group_id": "100",
        }
        payload.update(overrides)
        return payload

    def _store(self, events: list[dict], *, available: bool = True, protected: bool = False):
        return SimpleNamespace(
            get_daily_roll_snapshot=AsyncMock(return_value=self.snapshot),
            query_daily_events=AsyncMock(return_value=DailyEventQueryResult(tuple(events), available)),
            is_protected=AsyncMock(return_value=protected),
        )

    def test_outcome_uses_one_combined_slot(self):
        self.assertEqual(
            build_yesterday_outcome_text(self.snapshot),
            "EX Lv.1 → 2 · 新立绘与介绍已解锁",
        )
        new_pig = replace(
            self.snapshot,
            is_new_pig=True,
            previous_copies=0,
            copies_after_roll=1,
            collection_size_after_roll=37,
            unlocked_variant_fields=frozenset(),
        )
        self.assertEqual(build_yesterday_outcome_text(new_pig), "新猪入圈 · 图鉴第 37 只")
        self.assertEqual(build_yesterday_outcome_text(DailyRollSnapshot(DATE, "pig")), "")

    async def test_zero_events_has_fact_only_and_missing_variant_falls_back(self):
        recap = await build_yesterday_recap(
            "user",
            group_id="100",
            date_str=DATE,
            recap_store=self._store([]),
            resources=self.resources,
        )

        self.assertEqual(recap.image_path, self.root / "pig.png")
        self.assertEqual(recap.fallback_image_path, self.root / "pig.png")
        self.assertEqual(recap.summary.text, "本群昨天没有发生与你有关的烤猪事件。")
        self.assertEqual((recap.footprints, recap.experiences), ((), ()))

    async def test_multi_events_select_two_families_and_stable_summary(self):
        events = [self._event(index) for index in range(1, 5)]
        events.append(self._event(
            5,
            "escape",
            attacker="other",
            target="user",
            attacker_name="追猪人",
            target_name="本人",
        ))
        store = self._store(events)

        first = await build_yesterday_recap(
            "user",
            group_id="100",
            date_str=DATE,
            recap_store=store,
            resources=self.resources,
        )
        second = await build_yesterday_recap(
            "user",
            group_id="100",
            date_str=DATE,
            recap_store=store,
            resources=self.resources,
        )

        self.assertEqual(first, second)
        self.assertEqual([item.family for item in first.experiences], ["escape_as_target", "success_as_attacker"])
        self.assertEqual(first.summary.kind, "chef")
        self.assertEqual([(item.kind, item.count) for item in first.footprints], [
            ("escaped_count", 1),
            ("success_count", 4),
        ])

    async def test_private_scope_anonymizes_external_names(self):
        events = [self._event(
            1,
            "success",
            attacker="Alice-id",
            target="user",
            attacker_name="Alice",
            target_name="本人",
            group_id="987654",
        )]
        store = self._store(events)

        recap = await build_yesterday_recap(
            "user",
            date_str=DATE,
            recap_store=store,
            resources=self.resources,
        )

        output = " ".join(item.text for item in recap.experiences)
        self.assertNotIn("Alice", output)
        self.assertNotIn("987654", output)
        self.assertEqual(recap.scope, "cross_group")
        store.query_daily_events.assert_awaited_with(date_str=DATE, group_id=None, user_id="user")

    async def test_reservation_backfire_uses_real_victim_instead_of_owner(self):
        raw = self._event(
            1,
            "backfire",
            attacker="owner",
            target="target",
            reservation_id="reservation",
            participant_ids=["owner", "user"],
            participant_names=["主厨", "本人"],
            participant_count=2,
            backfire_victim_id="user",
            backfire_victim_name="本人",
        )
        normalized = normalize_yesterday_events([raw], date_str=DATE, user_id="user")

        self.assertEqual((normalized[0].family, normalized[0].user_role), (
            "reservation_backfire_victim",
            "backfire_victim",
        ))

        owner_view = normalize_yesterday_events([raw], date_str=DATE, user_id="owner")
        self.assertEqual(owner_view[0].family, "reservation_backfire_participant")

    def test_reservation_count_pools_include_owner_and_keep_unknown_fallback(self):
        single = normalize_yesterday_events(
            [self._event(
                1,
                "success",
                reservation_id="single",
                participant_ids=[],
                participant_count=0,
            )],
            date_str=DATE,
            user_id="user",
        )[0]
        self.assertEqual((single.family, single.participant_ids, single.participant_count), (
            "reservation_success_participant",
            ("user",),
            1,
        ))
        single_text = select_yesterday_experiences(
            (single,),
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )[0].text
        self.assertIn(single_text, {
            template.format(target="目标1", food="烤乳猪")
            for template in YESTERDAY_EXPERIENCE_TEXTS["reservation_success_participant_single"]
        })

        multi = normalize_yesterday_events(
            [self._event(
                2,
                "success",
                reservation_id="multi",
                participant_ids=["user", "helper"],
                participant_names=["本人", "帮厨"],
                participant_count=2,
            )],
            date_str=DATE,
            user_id="user",
        )[0]
        multi_text = select_yesterday_experiences(
            (multi,),
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )[0].text
        self.assertEqual(multi.participant_count, 2)
        self.assertIn(multi_text, {
            template.format(target="目标2", food="烤乳猪", participant_count=2)
            for template in YESTERDAY_EXPERIENCE_TEXTS["reservation_success_participant_multi"]
        })

        unknown = normalize_yesterday_events(
            [self._event(
                3,
                "success",
                attacker="",
                target="user",
                target_name="本人",
                reservation_id="unknown",
                participant_ids=[],
                participant_count=0,
            )],
            date_str=DATE,
            user_id="user",
        )[0]
        unknown_text = select_yesterday_experiences(
            (unknown,),
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )[0].text
        self.assertEqual(unknown.participant_count, 0)
        self.assertIn(unknown_text, {
            template.format(food="烤乳猪")
            for template in YESTERDAY_EXPERIENCE_TEXTS["reservation_success_target_unknown"]
        })

    def test_special_target_uses_role_and_state_specific_pool(self):
        for index, reason in enumerate(("human", "food", "eaten", "sold"), start=1):
            raw = self._event(
                index,
                "reserved_special",
                attacker="owner",
                target="user",
                attacker_name="主厨",
                target_name="本人",
                reservation_id=f"special-{reason}",
                participant_ids=["owner"],
                participant_names=["主厨"],
                special_reason=reason,
            )
            target_view = normalize_yesterday_events([raw], date_str=DATE, user_id="user")
            owner_view = normalize_yesterday_events([raw], date_str=DATE, user_id="owner")

            self.assertEqual(target_view[0].family, f"reserved_special_target_{reason}")
            self.assertEqual(owner_view[0].family, "reserved_special_participant")

        missing_reason = self._event(
            5,
            "reserved_special",
            attacker="owner",
            target="user",
            reservation_id="legacy-special",
            participant_ids=["owner"],
        )
        self.assertEqual(normalize_yesterday_events(
            [missing_reason],
            date_str=DATE,
            user_id="user",
        ), ())

    def test_summary_uses_fixed_conflict_priority(self):
        chef_and_chaotic = [self._event(index) for index in range(1, 5)]
        chef_and_chaotic.extend(
            self._event(
                index,
                "success",
                attacker=f"other-{index}",
                target="user",
                attacker_name=f"群友{index}",
                target_name="本人",
            )
            for index in range(5, 7)
        )
        events = normalize_yesterday_events(chef_and_chaotic, date_str=DATE, user_id="user")
        self.assertEqual(build_yesterday_summary(
            events,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        ).kind, "chef")

        team_and_chef = normalize_yesterday_events(
            [
                self._event(
                    index,
                    "success",
                    reservation_id=f"reservation-{index}",
                    participant_ids=["user", f"helper-{index}"],
                    participant_count=2,
                )
                for index in range(1, 4)
            ],
            date_str=DATE,
            user_id="user",
        )
        self.assertEqual(build_yesterday_summary(
            team_and_chef,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        ).kind, "team_player")

        solo_reservations = normalize_yesterday_events(
            [
                self._event(
                    index,
                    "success",
                    reservation_id=f"solo-reservation-{index}",
                    participant_ids=["user"],
                    participant_count=1,
                )
                for index in range(1, 4)
            ],
            date_str=DATE,
            user_id="user",
        )
        self.assertEqual(build_yesterday_summary(
            solo_reservations,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        ).kind, "chef")

        self_and_cursed = normalize_yesterday_events(
            [
                self._event(1, "self_roast", target="user"),
                self._event(2, "self_roast", target="user"),
                self._event(3, "backfire"),
                self._event(4, "backfire"),
            ],
            date_str=DATE,
            user_id="user",
        )
        self.assertEqual(build_yesterday_summary(
            self_and_cursed,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        ).kind, "self_service")

    async def test_event_query_failure_hides_zero_event_conclusion(self):
        recap = await build_yesterday_recap(
            "user",
            group_id="100",
            date_str=DATE,
            recap_store=self._store([], available=False),
            resources=self.resources,
        )

        self.assertFalse(recap.events_available)
        self.assertIsNone(recap.summary)
        self.assertEqual((recap.footprints, recap.experiences), ((), ()))

    async def test_protection_becomes_today_aftereffect_only_in_group(self):
        group_recap = await build_yesterday_recap(
            "user",
            group_id="100",
            date_str=DATE,
            recap_store=self._store([], protected=True),
            resources=self.resources,
        )
        private_store = self._store([], protected=True)
        private_recap = await build_yesterday_recap(
            "user",
            date_str=DATE,
            recap_store=private_store,
            resources=self.resources,
        )

        self.assertEqual(
            group_recap.aftereffect_text,
            "昨天挨的烤没白挨：今天你在本群获得了一层保护，普通烤猪会被拦下。",
        )
        self.assertEqual(private_recap.aftereffect_text, "")
        private_store.is_protected.assert_not_awaited()


class YesterdayTextPoolTests(unittest.TestCase):
    def test_pool_sizes_placeholders_and_lengths(self):
        expected_experience_keys = {
            "success_as_attacker",
            "success_as_target",
            "escape_as_target",
            "escape_as_attacker",
            "normal_backfire",
            "bot_backfire",
            "self_roast",
            "reservation_success_participant_multi",
            "reservation_success_participant_single",
            "reservation_success_participant_unknown",
            "reservation_success_target_multi",
            "reservation_success_target_single",
            "reservation_success_target_unknown",
            "reservation_escape_participant_multi",
            "reservation_escape_participant_single",
            "reservation_escape_participant_unknown",
            "reservation_escape_target_multi",
            "reservation_escape_target_single",
            "reservation_escape_target_unknown",
            "reservation_backfire_victim",
            "reservation_backfire_participant",
            "reserved_special_participant",
            "reserved_special_target_human",
            "reserved_special_target_food",
            "reserved_special_target_eaten",
            "reserved_special_target_sold",
        }
        context = {
            "attacker": "群友",
            "target": "目标",
            "victim": "倒霉蛋",
            "food": "烤乳猪",
            "participant_count": 12,
            "other_count": 11,
            "special_reason": "它以人类形态出现",
        }
        self.assertEqual(set(YESTERDAY_EXPERIENCE_TEXTS), expected_experience_keys)
        self.assertEqual(sum(map(len, YESTERDAY_EXPERIENCE_TEXTS.values())), 124)
        for family, pool in YESTERDAY_EXPERIENCE_TEXTS.items():
            self.assertTrue(pool, family)
            for template in pool:
                rendered = template.format(**context)
                self.assertNotIn("{", rendered)
                self.assertLessEqual(len(rendered), 60, (family, rendered))
        self.assertEqual(sum(map(len, YESTERDAY_SUMMARY_TEXTS.values())), 56)
        for kind, pool in YESTERDAY_SUMMARY_TEXTS.items():
            self.assertTrue(pool, kind)
            self.assertTrue(all(len(text) <= 36 for text in pool), kind)

    def test_same_signature_keeps_experience_and_summary_stable(self):
        events = normalize_yesterday_events(
            [
                {
                    "type": "success",
                    "attacker": "user",
                    "target": f"target-{index}",
                    "food": "烤乳猪",
                    "event_id": str(index),
                    "created_at": f"2026-08-23T0{index}:00:00+00:00",
                }
                for index in range(1, 4)
            ],
            date_str=DATE,
            user_id="user",
        )
        first_experiences = select_yesterday_experiences(
            events,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )
        second_experiences = select_yesterday_experiences(
            events,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )
        first_summary = build_yesterday_summary(
            events,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )
        second_summary = build_yesterday_summary(
            events,
            date_str=DATE,
            user_id="user",
            scope="group",
            group_id="100",
        )

        self.assertEqual(first_experiences, second_experiences)
        self.assertEqual(first_summary, second_summary)


class YesterdayCardRendererTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    @staticmethod
    def _snapshot(image_name: str = "pig.png") -> DailyRollSnapshot:
        return DailyRollSnapshot(
            date_str=DATE,
            pig_id="pig",
            is_new_pig=False,
            previous_copies=1,
            copies_after_roll=2,
            collection_size_after_roll=37,
            resource_version="2026-08-20.1",
            resolved_variant_level=2,
            resolved_image_name=image_name,
            unlocked_variant_levels=(2,),
            unlocked_variant_fields=frozenset({"image", "description"}),
        )

    def _recap(
        self,
        image_path: Path | None,
        *,
        fallback_image_path: Path | None = None,
        image_name: str = "pig.png",
    ) -> YesterdayRecap:
        return YesterdayRecap(
            date_str=DATE,
            scope="group",
            group_id="100",
            roll=self._snapshot(image_name),
            pig_name="猪序员",
            image_path=image_path,
            fallback_image_path=fallback_image_path or image_path,
            resource_version="2026-08-20.1",
            outcome_text="EX Lv.1 → 2 · 新立绘与介绍已解锁",
            footprints=(
                YesterdayFootprint("success_count", "烤成别人", 4),
                YesterdayFootprint("escaped_count", "成功逃脱", 2),
                YesterdayFootprint("reservation_result_count", "已结算预约", 3),
            ),
            experiences=(
                YesterdayExperience(
                    "reservation_success_participant",
                    "你和【小明】联手，把【倒霉蛋】送进了烤箱。",
                    "event-1",
                ),
                YesterdayExperience(
                    "escape_as_target",
                    "【老王】刚伸出夹子，你已经从烤架边溜走了。",
                    "event-2",
                ),
            ),
            summary=YesterdaySummary(
                "chef",
                "昨天基本住在后厨，路过的群友都得防着你。",
            ),
            aftereffect_text=(
                "昨天挨的烤没白挨：今天你在本群获得了一层保护，普通烤猪会被拦下。"
            ),
            events_available=True,
        )

    @staticmethod
    def _hero_center(data: yesterday_card_module.CardData) -> tuple[int, int]:
        layout = yesterday_card_module._YESTERDAY_CARD_RENDERER.build_layout(
            data,
            width=yesterday_card_module.YESTERDAY_CARD_WIDTH,
            supersample=yesterday_card_module.YESTERDAY_CARD_SUPERSAMPLE,
        )
        hero_box = layout.boxes["hero"]
        scale = yesterday_card_module.YESTERDAY_CARD_WIDTH / yesterday_card_module.BASE_WIDTH
        return (
            round((hero_box.x + 109 + 310) * scale),
            round((hero_box.y + 225) * scale),
        )

    def test_card_data_maps_dynamic_sections_and_explicit_line_breaks(self) -> None:
        recap = self._recap(None)
        data = yesterday_card_module.build_yesterday_card_data(recap, hero_path=None)

        self.assertEqual(data.date, "8月23日")
        self.assertEqual(data.role, "【猪序员】")
        self.assertEqual(data.outcome_text, recap.outcome_text)
        self.assertEqual([item.icon for item in data.stats], ["flame", "runner", "flame"])
        self.assertEqual(
            [section.title for section in data.sections],
            ["昨日高光", "昨日小结", "今日余波"],
        )
        self.assertEqual(
            [section.kind for section in data.sections],
            ["highlight", "summary", "aftereffect"],
        )
        highlight = "".join(span.text for span in data.sections[0].body)
        self.assertIn("\n", highlight)
        self.assertIn("【小明】", highlight)

    def test_emoji_nickname_keeps_zwj_cluster_and_uses_noto_image(self) -> None:
        renderer = yesterday_card_module._YESTERDAY_CARD_RENDERER
        family_emoji = "👨‍👩‍👧‍👦"
        tokens = renderer._span_tokens(
            (yesterday_card_module.TextSpan(f"A{family_emoji}B", yesterday_card_module.BODY_INK),),
            28,
        )
        self.assertEqual([token.text for token in tokens], ["A", family_emoji, "B"])

        canvas = Image.new("RGBA", (180, 100), (255, 255, 255, 255))
        try:
            renderer._draw_text(
                canvas,
                ImageDraw.Draw(canvas),
                (yesterday_card_module.TextSpan("🐷", yesterday_card_module.BODY_INK),),
                "body",
                40,
                yesterday_card_module.Box(10, 10, 140, 60),
                60,
                1,
            )
            self.assertTrue(
                any(
                    red > 240 and 80 < green < 230 and 80 < blue < 220 and alpha > 200
                    for red, green, blue, alpha in canvas.get_flattened_data()
                )
            )
        finally:
            canvas.close()

    def test_yesterday_title_and_body_fonts_can_be_replaced_independently(self) -> None:
        source_han = yesterday_card_module.YESTERDAY_CARD_DEFAULT_BODY_FONT
        zcool = yesterday_card_module.YESTERDAY_CARD_DEFAULT_TITLE_FONT
        renderer = yesterday_card_module.PillowCardRenderer(
            yesterday_card_module.YESTERDAY_CARD_RESOURCE_DIR,
            display_font_path=source_han,
            body_font_path=zcool,
        )

        self.assertEqual(renderer._font_paths["display"], source_han)
        self.assertEqual(renderer._font_paths["body"], zcool)
        self.assertNotEqual(
            renderer._font("display", 24, 1).getname(),
            renderer._font("body", 24, 1).getname(),
        )

    def test_bad_custom_body_font_falls_back_to_source_han(self) -> None:
        bad_font = self.root / "bad-font.ttf"
        bad_font.write_bytes(b"this is not a font")
        renderer = yesterday_card_module.PillowCardRenderer(
            yesterday_card_module.YESTERDAY_CARD_RESOURCE_DIR,
            body_font_path=bad_font,
        )

        rendered_font = renderer._font("body", 24, 1)

        self.assertEqual(
            renderer._font_paths["body"],
            yesterday_card_module.YESTERDAY_CARD_DEFAULT_BODY_FONT,
        )
        self.assertEqual(
            rendered_font.getname(),
            ImageFont.truetype(
                str(yesterday_card_module.YESTERDAY_CARD_DEFAULT_BODY_FONT),
                24,
            ).getname(),
        )

    async def test_static_card_uses_real_hero(self) -> None:
        hero = self.root / "pig.png"
        Image.new("RGBA", (240, 240), (28, 210, 70, 255)).save(hero)
        recap = self._recap(hero)

        result = await yesterday_card_module.render_yesterday_recap_card(recap)

        self.assertEqual((result.image_format, result.used_fallback_image), ("png", False))
        data = yesterday_card_module.build_yesterday_card_data(recap, hero_path=hero.resolve())
        center = self._hero_center(data)
        with Image.open(BytesIO(result.data)) as rendered:
            self.assertEqual(rendered.format, "PNG")
            red, green, blue = rendered.convert("RGB").getpixel(center)
        self.assertGreater(green, red + 80)
        self.assertGreater(green, blue + 80)

    async def test_bad_variant_falls_back_to_base_and_drops_art_claim(self) -> None:
        bad_variant = self.root / "pig_ex2.png"
        bad_variant.write_bytes(b"this is not a png")
        base = self.root / "pig.png"
        Image.new("RGBA", (240, 240), (30, 80, 230, 255)).save(base)
        recap = self._recap(
            bad_variant,
            fallback_image_path=base,
            image_name=bad_variant.name,
        )

        result = await yesterday_card_module.render_yesterday_recap_card(recap)

        self.assertTrue(result.used_fallback_image)
        fallback_data = yesterday_card_module.build_yesterday_card_data(
            recap,
            hero_path=base.resolve(),
            image_fallback=True,
        )
        self.assertEqual(
            fallback_data.outcome_text,
            "EX Lv.1 → 2 · 新介绍已解锁",
        )
        center = self._hero_center(fallback_data)
        with Image.open(BytesIO(result.data)) as rendered:
            red, green, blue = rendered.convert("RGB").getpixel(center)
        self.assertGreater(blue, red + 80)
        self.assertGreater(blue, green + 80)

    async def test_gif_card_preserves_frames_and_durations(self) -> None:
        gif_path = self.root / "pig.gif"
        frames = [
            Image.new("RGB", (120, 120), color)
            for color in ((230, 30, 30), (30, 210, 70), (30, 80, 230))
        ]
        try:
            frames[0].save(
                gif_path,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=[80, 100, 120],
                loop=0,
                disposal=2,
            )
        finally:
            for frame in frames:
                frame.close()
        recap = self._recap(gif_path, image_name=gif_path.name)

        result = await yesterday_card_module.render_yesterday_recap_card(recap)

        self.assertEqual(result.image_format, "gif")
        data = yesterday_card_module.build_yesterday_card_data(
            recap,
            hero_path=gif_path.resolve(),
        )
        center = self._hero_center(data)
        with Image.open(BytesIO(result.data)) as rendered:
            self.assertTrue(rendered.is_animated)
            self.assertEqual(rendered.n_frames, 3)
            durations = []
            colors = set()
            for index in range(rendered.n_frames):
                rendered.seek(index)
                durations.append(rendered.info.get("duration"))
                colors.add(rendered.convert("RGB").getpixel(center))
        self.assertEqual(durations, [80, 100, 120])
        self.assertEqual(len(colors), 3)

    async def test_command_sends_new_recap_card(self) -> None:
        recap = self._recap(None)
        render_result = yesterday_card_module.YesterdayCardRenderResult(
            data=b"image-data",
            image_format="png",
            renderer="pillow-yesterday",
            width=720,
            height=900,
            used_fallback_image=False,
        )
        event = SimpleNamespace(user_id="user", message_id=123)
        build_mock = AsyncMock(return_value=recap)
        render_mock = AsyncMock(return_value=render_result)
        finish_mock = AsyncMock()

        with (
            patch.object(roll_handler_module, "get_event_group_id", return_value="100"),
            patch.object(roll_handler_module, "build_yesterday_recap", new=build_mock),
            patch.object(roll_handler_module, "render_yesterday_recap_card", new=render_mock),
            patch.object(roll_handler_module.cmd_yest, "finish", new=finish_mock),
        ):
            await roll_handler_module._handle_yesterday_pig(event)

        build_mock.assert_awaited_once_with("user", group_id="100")
        render_mock.assert_awaited_once_with(recap)
        finish_mock.assert_awaited_once()
        message = finish_mock.await_args.args[0]
        self.assertEqual([segment.type for segment in message], ["reply", "image"])

    async def test_command_render_failure_never_uses_legacy_card(self) -> None:
        recap = self._recap(None)
        event = SimpleNamespace(user_id="user", message_id=123)
        build_mock = AsyncMock(return_value=recap)
        render_mock = AsyncMock(side_effect=RuntimeError("broken renderer"))
        finish_mock = AsyncMock()
        legacy_render_mock = AsyncMock()

        with (
            patch.object(roll_handler_module, "get_event_group_id", return_value="100"),
            patch.object(roll_handler_module, "build_yesterday_recap", new=build_mock),
            patch.object(roll_handler_module, "render_yesterday_recap_card", new=render_mock),
            patch.object(roll_handler_module, "send_rendered_pig", new=legacy_render_mock),
            patch.object(roll_handler_module.cmd_yest, "finish", new=finish_mock),
        ):
            await roll_handler_module._handle_yesterday_pig(event)

        legacy_render_mock.assert_not_awaited()
        finish_mock.assert_awaited_once()
        message = finish_mock.await_args.args[0]
        self.assertEqual([segment.type for segment in message], ["reply", "text"])
        self.assertIn("昨日回顾卡生成失败", str(message))


if __name__ == "__main__":
    unittest.main()
