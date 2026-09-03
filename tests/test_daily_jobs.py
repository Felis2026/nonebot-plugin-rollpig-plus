from __future__ import annotations

import asyncio
import datetime as dt
import json
import tempfile
import unittest
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

from nonebot_plugin_rollpig_plus import jobs
from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.store import local_json as local_json_module
from nonebot_plugin_rollpig_plus.store.base import RollpigStore
from nonebot_plugin_rollpig_plus.store.cloud import (
    CloudDailyReportUnsupportedError,
    CloudStore,
)
from nonebot_plugin_rollpig_plus.store.local_json import LocalJsonStore
from nonebot_plugin_rollpig_plus.store.models import (
    DailyEventQueryResult,
    DailyReportDeliveryClaim,
    DailyReportDeliveryClaimResult,
    DailyReportDeliveryTransitionResult,
    DailyReportProfileSnapshot,
)


class DailyReportBotRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_are_routed_to_the_bot_that_can_actually_see_them(self) -> None:
        bot_a = SimpleNamespace(
            get_group_list=AsyncMock(return_value=[{"group_id": 100}]),
            get_group_info=AsyncMock(),
        )
        bot_b = SimpleNamespace(
            get_group_list=AsyncMock(return_value={"data": [{"group_id": "200"}]}),
            get_group_info=AsyncMock(),
        )

        with patch.object(jobs, "get_bots", return_value={"bot-b": bot_b, "bot-a": bot_a}):
            resolved = await jobs.resolve_daily_report_bots(["100", "200"])

        self.assertIs(resolved["100"], bot_a)
        self.assertIs(resolved["200"], bot_b)
        bot_a.get_group_info.assert_not_awaited()
        bot_b.get_group_info.assert_not_awaited()

    async def test_group_info_fallback_never_assigns_an_unconfirmed_bot(self) -> None:
        bot_a = SimpleNamespace(
            get_group_list=AsyncMock(side_effect=RuntimeError("temporary failure")),
            get_group_info=AsyncMock(side_effect=RuntimeError("not in group")),
        )
        bot_b = SimpleNamespace(
            get_group_list=AsyncMock(return_value=[]),
            get_group_info=AsyncMock(side_effect=[{"group_id": 300}, RuntimeError("not in group")]),
        )

        with patch.object(jobs, "get_bots", return_value={"bot-a": bot_a, "bot-b": bot_b}):
            resolved = await jobs.resolve_daily_report_bots(["300", "400"])

        self.assertEqual(set(resolved), {"300"})
        self.assertIs(resolved["300"], bot_b)
        self.assertNotIn("400", resolved)


class DataMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_cleanup_still_runs_when_event_cleanup_fails(self) -> None:
        with patch.object(jobs, "store") as mocked_store:
            mocked_store.prune_events = AsyncMock(side_effect=RuntimeError("event cleanup failed"))
            mocked_store.prune_history = AsyncMock()

            await jobs.run_data_maintenance("test")

        mocked_store.prune_events.assert_awaited_once_with(days_to_keep=7)
        mocked_store.prune_history.assert_awaited_once_with(days_to_keep=14)


class DailySnapshotCutoffTests(unittest.TestCase):
    def test_local_rolls_events_and_active_users_share_fixed_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "pig_data.json"
            with patch.object(data_manager_module, "DATA_FILE", data_file):
                manager = PigDataManager()

            date_str = "2026-08-26"
            group_id = "100"
            manager.data["group_rolls"] = {
                date_str: {group_id: {"before": "pig-a", "after": "pig-b", "legacy": "pig-c"}}
            }
            manager.data["group_roll_seen_at"] = {
                date_str: {
                    group_id: {
                        "before": "2026-08-26T15:44:00+00:00",
                        "after": "2026-08-26T15:46:00+00:00",
                    }
                }
            }
            manager.data["group_daily_active_users"] = {
                date_str: {group_id: ["before", "after", "legacy"]}
            }
            manager.data["group_daily_active_at"] = {
                date_str: {
                    group_id: {
                        "before": "2026-08-26T15:44:00+00:00",
                        "after": "2026-08-26T15:46:00+00:00",
                    }
                }
            }
            manager.data["daily_events"] = {
                date_str: [
                    {"event_id": "before", "group_id": group_id, "created_at": "2026-08-26T15:44:00+00:00"},
                    {"event_id": "after", "group_id": group_id, "created_at": "2026-08-26T15:46:00+00:00"},
                    {"event_id": "legacy", "group_id": group_id},
                ]
            }
            cutoff_at = "2026-08-26T23:45:00+08:00"

            self.assertEqual(
                manager.get_group_rolls(group_id, date_str, cutoff_at=cutoff_at),
                {"before": "pig-a", "legacy": "pig-c"},
            )
            self.assertEqual(
                manager.get_group_active_user_ids(group_id, date_str, cutoff_at=cutoff_at),
                {"before", "legacy"},
            )
            self.assertEqual(
                [
                    item["event_id"]
                    for item in manager.get_daily_events(
                        date_str,
                        group_id,
                        cutoff_at=cutoff_at,
                    )
                ],
                ["before", "legacy"],
            )


class LocalDailyReportProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_are_batched_in_worker_and_frozen_at_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "pig_data.json"
            with patch.object(data_manager_module, "DATA_FILE", data_file):
                manager = PigDataManager()

        manager.data["history"] = {
            "2026-08-25": {"before": "pig-a", "late": "pig-old"},
            "2026-08-26": {"before": "pig-a", "late": "pig-new"},
            "2026-08-27": {"before": "pig-a"},
        }
        manager.data["daily_roll_snapshots"] = {
            "2026-08-25": {
                "before": {
                    "pig_id": "pig-a",
                    "previous_copies": 0,
                    "copies_after_roll": 1,
                    "created_at": "2026-08-25T10:00:00+00:00",
                },
                "late": {
                    "pig_id": "pig-old",
                    "previous_copies": 0,
                    "copies_after_roll": 1,
                    "created_at": "2026-08-25T10:00:00+00:00",
                },
            },
            "2026-08-26": {
                "before": {
                    "pig_id": "pig-a",
                    "previous_copies": 1,
                    "copies_after_roll": 2,
                    "created_at": "2026-08-26T15:44:00+00:00",
                },
                "late": {
                    "pig_id": "pig-new",
                    "previous_copies": 0,
                    "copies_after_roll": 1,
                    "created_at": "2026-08-26T15:46:00+00:00",
                },
            },
            "2026-08-27": {
                "before": {
                    "pig_id": "pig-a",
                    "previous_copies": 2,
                    "copies_after_roll": 3,
                    "created_at": "2026-08-26T16:01:00+00:00",
                }
            },
        }
        manager.data["collection"] = {
            "before": ["pig-a", "pig-future"],
            "late": ["pig-old", "pig-new"],
        }
        manager.data["pig_progress"] = {
            "before": {
                "pig-a": {"copies": 3, "first_obtained_at": "2026-08-25T10:00:00+00:00"},
                "pig-future": {"copies": 1, "first_obtained_at": "2026-08-26T16:01:00+00:00"},
            },
            "late": {
                "pig-old": {"copies": 1, "first_obtained_at": "2026-08-25T10:00:00+00:00"},
                "pig-new": {"copies": 1, "first_obtained_at": "2026-08-26T15:46:00+00:00"},
            },
        }
        local_store = LocalJsonStore(lambda: manager)

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with patch.object(
            data_manager_module.asyncio,
            "to_thread",
            new=AsyncMock(side_effect=run_inline),
        ) as to_thread:
            profiles = await local_store.get_daily_report_profiles(
                group_id="100",
                date_str="2026-08-26",
                cutoff_at="2026-08-26T23:45:00+08:00",
                user_ids=("before", "late"),
            )

        by_user = {item.user_id: item for item in profiles}
        self.assertEqual(by_user["before"].daily_pig_id, "pig-a")
        self.assertEqual(by_user["before"].daily_ex_level, 1)
        self.assertEqual(by_user["before"].recent_ex_level, 1)
        self.assertEqual(by_user["before"].catalog_count, 1)
        self.assertEqual(by_user["late"].daily_pig_id, "")
        self.assertEqual(by_user["late"].recent_pig_id, "pig-old")
        self.assertEqual(by_user["late"].catalog_count, 1)
        to_thread.assert_awaited_once()


class DailyReportDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _create_local_store(
        self,
        data_file: Path | None = None,
    ) -> tuple[LocalJsonStore, Path]:
        if data_file is None:
            temp_dir = tempfile.TemporaryDirectory()
            self.addCleanup(temp_dir.cleanup)
            data_file = Path(temp_dir.name) / "pig_data.json"
        with patch.object(data_manager_module, "DATA_FILE", data_file):
            manager = PigDataManager()
        return LocalJsonStore(lambda: manager), data_file

    async def test_base_store_defaults_keep_legacy_backends_compatible(self) -> None:
        self.assertNotIn("claim_daily_report_deliveries", RollpigStore.__abstractmethods__)
        self.assertNotIn("transition_daily_report_delivery", RollpigStore.__abstractmethods__)

        claim_result = await RollpigStore.claim_daily_report_deliveries(
            object(),
            instance_id="instance-a",
            delivery_bots={"100": "bot-a"},
            date_str="2026-08-26",
            cutoff_at="2026-08-26T23:45:00+08:00",
        )
        transition = await RollpigStore.transition_daily_report_delivery(
            object(),
            DailyReportDeliveryClaim(
                "2026-08-26",
                "100",
                "bot-a",
                "2026-08-26T23:45:00+08:00",
                "claim-a",
            ),
            "sending",
        )
        self.assertEqual(claim_result, DailyReportDeliveryClaimResult())
        self.assertFalse(transition)

    async def test_local_release_retries_without_reclaiming_completed_groups(self) -> None:
        date_str = jobs.rollpig_date_str()
        local_store, _ = self._create_local_store()
        candidates = {"100": "bot-a", "200": "bot-a"}

        first = await local_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots=candidates,
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        sent_claim, retry_claim = first.claims
        self.assertTrue(
            await local_store.transition_daily_report_delivery(sent_claim, "sending")
        )
        self.assertTrue(
            await local_store.transition_daily_report_delivery(sent_claim, "sent")
        )
        with patch.object(local_json_module, "LOCAL_DAILY_REPORT_RETRY_SECONDS", 0):
            released = await local_store.transition_daily_report_delivery(
                retry_claim,
                "release",
            )

        self.assertTrue(released)
        self.assertTrue(released.next_attempt_at)
        self.assertIsNotNone(
            jobs._daily_report_retry_delay(date_str, [released.next_attempt_at])
        )

        second = await local_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots=candidates,
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        self.assertEqual([claim.group_id for claim in second.claims], ["200"])
        self.assertEqual(second.claims[0].attempt_count, 2)
        self.assertTrue(
            await local_store.transition_daily_report_delivery(second.claims[0], "sending")
        )
        self.assertTrue(
            await local_store.transition_daily_report_delivery(second.claims[0], "sent")
        )

        completed = await local_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots=candidates,
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        self.assertEqual(completed.claims, ())

    async def test_local_release_stops_after_four_attempts(self) -> None:
        date_str = jobs.rollpig_date_str()
        local_store, _ = self._create_local_store()
        candidates = {"100": "bot-a"}

        with patch.object(local_json_module, "LOCAL_DAILY_REPORT_RETRY_SECONDS", 0):
            for attempt in range(1, 5):
                result = await local_store.claim_daily_report_deliveries(
                    instance_id="instance-a",
                    delivery_bots=candidates,
                    date_str=date_str,
                    cutoff_at=f"{date_str}T23:45:00+08:00",
                )
                self.assertEqual(len(result.claims), 1)
                self.assertEqual(result.claims[0].attempt_count, attempt)
                released = await local_store.transition_daily_report_delivery(
                    result.claims[0],
                    "release",
                )

        self.assertTrue(released)
        self.assertEqual(released.status, "failed")
        self.assertEqual(released.attempt_count, 4)
        self.assertEqual(released.next_attempt_at, "")
        exhausted = await local_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots=candidates,
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        self.assertEqual(exhausted.claims, ())

    async def test_local_claim_is_recovered_after_process_restart(self) -> None:
        date_str = jobs.rollpig_date_str()
        first_store, data_file = self._create_local_store()
        first = await first_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots={"100": "bot-a"},
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        self.assertEqual(first.claims[0].attempt_count, 1)

        # 新建 Manager 模拟进程重启；claimed 尚未提交发送意图，可以安全重领。
        restarted_store, _ = self._create_local_store(data_file)
        recovered = await restarted_store.claim_daily_report_deliveries(
            instance_id="instance-b",
            delivery_bots={"100": "bot-b"},
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )

        self.assertEqual(len(recovered.claims), 1)
        self.assertEqual(recovered.claims[0].attempt_count, 2)
        self.assertEqual(recovered.claims[0].delivery_bot_id, "bot-b")

    async def test_local_sending_state_freezes_as_uncertain_after_restart(self) -> None:
        date_str = jobs.rollpig_date_str()
        first_store, data_file = self._create_local_store()
        first = await first_store.claim_daily_report_deliveries(
            instance_id="instance-a",
            delivery_bots={"100": "bot-a"},
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )
        self.assertTrue(
            await first_store.transition_daily_report_delivery(
                first.claims[0],
                "sending",
            )
        )

        restarted_store, _ = self._create_local_store(data_file)
        recovered = await restarted_store.claim_daily_report_deliveries(
            instance_id="instance-b",
            delivery_bots={"100": "bot-b"},
            date_str=date_str,
            cutoff_at=f"{date_str}T23:45:00+08:00",
        )

        self.assertEqual(recovered.claims, ())
        self.assertEqual(
            restarted_store.manager.data["daily_report_deliveries"][date_str]["100"]["status"],
            "uncertain",
        )

    def test_startup_recovery_date_only_covers_safe_delivery_window(self) -> None:
        timezone = jobs.ROLLPIG_TIMEZONE
        self.assertEqual(
            jobs._daily_report_startup_recovery_date(
                dt.datetime(2026, 9, 3, 23, 50, tzinfo=timezone)
            ),
            "2026-09-03",
        )
        self.assertEqual(
            jobs._daily_report_startup_recovery_date(
                dt.datetime(2026, 9, 4, 0, 5, tzinfo=timezone)
            ),
            "2026-09-03",
        )
        self.assertEqual(
            jobs._daily_report_startup_recovery_date(
                dt.datetime(2026, 9, 4, 12, 0, tzinfo=timezone)
            ),
            "",
        )

    async def test_startup_recovery_runs_without_random_delay(self) -> None:
        with (
            patch.object(jobs, "get_bots", return_value={"bot-a": object()}),
            patch.object(
                jobs,
                "_daily_report_startup_recovery_date",
                return_value="2026-09-03",
            ),
            patch.object(jobs, "daily_report_job", new=AsyncMock()) as report_job,
        ):
            await jobs.startup_daily_report_recovery()
            await asyncio.sleep(0)

        report_job.assert_awaited_once_with(
            report_date="2026-09-03",
            random_delay_enabled=False,
        )

    async def test_startup_recovery_waits_for_late_bot_connection(self) -> None:
        with (
            patch.object(jobs, "get_bots", return_value={}),
            patch.object(jobs, "schedule_daily_report_recovery") as schedule_recovery,
        ):
            await jobs.startup_daily_report_recovery()

        schedule_recovery.assert_not_called()

    async def test_bot_connection_restarts_recovery_in_safe_window(self) -> None:
        with (
            patch.object(
                jobs,
                "_daily_report_startup_recovery_date",
                return_value="2026-09-03",
            ),
            patch.object(jobs, "schedule_daily_report_recovery") as schedule_recovery,
        ):
            await jobs.bot_connect_daily_report_recovery(SimpleNamespace(self_id="bot-a"))

        schedule_recovery.assert_called_once_with("2026-09-03")

    async def test_group_report_writes_protection_before_returning_coupon(self) -> None:
        summary_store = SimpleNamespace(
            get_group_rolls=AsyncMock(return_value={"a": "pig", "b": "pig"}),
            query_daily_events=AsyncMock(
                return_value=SimpleNamespace(
                    available=True,
                    items=(
                        {
                            "event_id": "one",
                            "type": "success",
                            "attacker": "a",
                            "target": "b",
                            "attacker_name": "甲",
                            "target_name": "乙",
                            "group_id": "100",
                            "created_at": "2026-08-26T12:00:00+08:00",
                        },
                        {
                            "event_id": "two",
                            "type": "success",
                            "attacker": "a",
                            "target": "b",
                            "attacker_name": "甲",
                            "target_name": "乙",
                            "group_id": "100",
                            "created_at": "2026-08-26T12:01:00+08:00",
                        },
                    ),
                )
            ),
            get_group_active_user_ids=AsyncMock(return_value={"a", "b"}),
            get_daily_report_profiles=AsyncMock(return_value=()),
            replace_group_protections=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_group_member_list=AsyncMock(
                return_value=[
                    {"user_id": "a", "card": "甲"},
                    {"user_id": "b", "card": "乙"},
                ]
            )
        )

        with patch.object(jobs, "get_bots", return_value={"999": bot}):
            report = await jobs.build_group_daily_report(
                summary_store,
                bot,
                date_str="2026-08-26",
                protect_date="2026-08-27",
                group_id="100",
                cutoff_at="2026-08-26T23:45:00+08:00",
            )

        summary_store.replace_group_protections.assert_awaited_once_with(
            "100",
            ["b"],
            "2026-08-27",
        )
        self.assertEqual(report.protections[0].user_id, "b")
        self.assertEqual(report.protections[0].display_name, "乙")
        summary_store.get_group_rolls.assert_awaited_once_with(
            "100",
            "2026-08-26",
            cutoff_at="2026-08-26T23:45:00+08:00",
        )
        summary_store.query_daily_events.assert_awaited_once_with(
            date_str="2026-08-26",
            group_id="100",
            cutoff_at="2026-08-26T23:45:00+08:00",
        )
        summary_store.get_group_active_user_ids.assert_awaited_once_with(
            "100",
            "2026-08-26",
            cutoff_at="2026-08-26T23:45:00+08:00",
        )

    async def test_cloud_profiles_are_loaded_in_one_group_request(self) -> None:
        summary_store = SimpleNamespace(
            get_daily_report_profiles=AsyncMock(
                return_value=(
                    DailyReportProfileSnapshot(
                        user_id="a",
                        daily_pig_id="pig-a",
                        daily_ex_level=4,
                        daily_achieved_at="2026-08-26T12:00:00",
                        catalog_count=18,
                        catalog_achieved_at="2026-08-25T08:00:00",
                        recent_pig_id="pig-b",
                        recent_ex_level=2,
                    ),
                )
            )
        )
        report = SimpleNamespace(participant_ids=("a", "b"))

        with patch.object(
            jobs,
            "_resolved_profile_pig",
            side_effect=[("今日猪", "daily.png"), ("最近猪", "recent.png")],
        ):
            profiles = await jobs.build_daily_user_profiles(
                summary_store,
                report=report,
                group_id="100",
                date_str="2026-08-26",
                cutoff_at="2026-08-26T23:45:00+08:00",
                group_rolls={"a": "pig-a"},
                member_names={"a": "甲", "b": "乙"},
            )

        summary_store.get_daily_report_profiles.assert_awaited_once_with(
            group_id="100",
            date_str="2026-08-26",
            cutoff_at="2026-08-26T23:45:00+08:00",
            user_ids=("a", "b"),
        )
        self.assertEqual(profiles["a"].daily_ex_level, 4)
        self.assertEqual(profiles["a"].catalog_count, 18)
        self.assertEqual(profiles["a"].daily_image_name, "daily.png")
        self.assertEqual(profiles["a"].recent_image_name, "recent.png")
        self.assertIsNone(profiles["b"].daily_ex_level)

    async def test_scheduled_job_sends_rendered_image_instead_of_legacy_text(self) -> None:
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        report = SimpleNamespace(has_activity=True)
        rendered = SimpleNamespace(data=b"valid-image-bytes")
        claim = DailyReportDeliveryClaim(
            date_str="2026-08-26",
            group_id="100",
            delivery_bot_id="bot-a",
            cutoff_at="2026-08-26T23:45:00+08:00",
            claim_token="claim-a",
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            claim_daily_report_deliveries=AsyncMock(return_value=(claim,)),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=report),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=rendered),
            ),
        ):
            await jobs.daily_report_job()

        message = bot.send_group_msg.await_args.kwargs["message"]
        self.assertEqual(message.type, "image")
        self.assertNotIn("今日猪圈日报", str(message))
        self.assertEqual(
            [call.args[1] for call in mocked_store.transition_daily_report_delivery.await_args_list],
            ["sending", "sent"],
        )

    async def test_failure_before_sending_releases_claim(self) -> None:
        bot = SimpleNamespace(self_id="bot-a", send_group_msg=AsyncMock())
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            claim_daily_report_deliveries=AsyncMock(return_value=(claim,)),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(jobs, "resolve_daily_report_bots", new=AsyncMock(return_value={"100": bot})),
            patch.object(jobs, "build_group_daily_report", new=AsyncMock(side_effect=RuntimeError("build failed"))),
        ):
            await jobs.daily_report_job()

        bot.send_group_msg.assert_not_awaited()
        mocked_store.transition_daily_report_delivery.assert_awaited_once()
        self.assertEqual(mocked_store.transition_daily_report_delivery.await_args.args[1], "release")

    async def test_send_failure_freezes_claim_as_uncertain(self) -> None:
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(side_effect=RuntimeError("send result unknown")),
        )
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            claim_daily_report_deliveries=AsyncMock(return_value=(claim,)),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(jobs, "resolve_daily_report_bots", new=AsyncMock(return_value={"100": bot})),
            patch.object(jobs, "build_group_daily_report", new=AsyncMock(return_value=SimpleNamespace(has_activity=True))),
            patch.object(jobs, "render_daily_report_card", new=AsyncMock(return_value=SimpleNamespace(data=b"image"))),
        ):
            await jobs.daily_report_job()

        self.assertEqual(
            [call.args[1] for call in mocked_store.transition_daily_report_delivery.await_args_list],
            ["sending", "uncertain"],
        )

    async def test_disabled_group_is_rechecked_before_claim_side_effects(self) -> None:
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(self_id="bot-a", send_group_msg=AsyncMock())

        for rollpig_enabled, report_enabled in ((False, True), (True, False)):
            with self.subTest(
                rollpig_enabled=rollpig_enabled,
                report_enabled=report_enabled,
            ):
                mocked_store = SimpleNamespace(
                    transition_daily_report_delivery=AsyncMock(
                        return_value=DailyReportDeliveryTransitionResult(
                            ok=True,
                            status="skipped",
                        )
                    )
                )
                with (
                    patch.object(jobs, "store", mocked_store),
                    patch.object(
                        jobs,
                        "is_group_rollpig_enabled",
                        return_value=rollpig_enabled,
                    ),
                    patch.object(
                        jobs,
                        "is_daily_report_enabled",
                        return_value=report_enabled,
                    ),
                    patch.object(
                        jobs,
                        "build_group_daily_report",
                        new=AsyncMock(),
                    ) as build_report,
                ):
                    result = await jobs._deliver_daily_report_claim(
                        claim,
                        delivery_bots={"100": bot},
                        protect_date="2026-08-27",
                        cutoff_time="23:45",
                    )

                self.assertEqual(result, (False, False, ""))
                build_report.assert_not_awaited()
                bot.send_group_msg.assert_not_awaited()
                self.assertEqual(
                    mocked_store.transition_daily_report_delivery.await_args.args[1],
                    "skip",
                )

    def test_retry_stops_when_current_time_reaches_deadline(self) -> None:
        date_str = "2026-08-26"
        deadline = dt.datetime(
            2026,
            8,
            27,
            0,
            10,
            tzinfo=jobs.ROLLPIG_TIMEZONE,
        )

        self.assertIsNone(
            jobs._daily_report_retry_delay(
                date_str,
                ["2026-08-26T16:09:00+00:00"],
                now=deadline,
            )
        )

    async def test_batch_releases_remaining_claims_when_deadline_is_reached(self) -> None:
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        claims = tuple(
            DailyReportDeliveryClaim(
                "2026-08-26",
                group_id,
                "bot-a",
                "2026-08-26T23:45:00+08:00",
                f"claim-{group_id}",
            )
            for group_id in ("100", "200", "300")
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100", "200", "300"}),
            claim_daily_report_deliveries=AsyncMock(
                return_value=DailyReportDeliveryClaimResult(claims=claims)
            ),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(
                jobs,
                "_daily_report_deadline_reached",
                side_effect=[False, False, True],
            ),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={group_id: bot for group_id in ("100", "200", "300")}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ) as build_report,
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        build_report.assert_awaited_once()
        bot.send_group_msg.assert_awaited_once()
        transitions = mocked_store.transition_daily_report_delivery.await_args_list
        self.assertEqual(
            [(call.args[0].group_id, call.args[1]) for call in transitions],
            [
                ("100", "sending"),
                ("100", "sent"),
                ("200", "release"),
                ("300", "release"),
            ],
        )
        self.assertEqual(
            [call.kwargs.get("error") for call in transitions[2:]],
            ["delivery_deadline_passed", "delivery_deadline_passed"],
        )

    async def test_rendering_past_deadline_releases_before_sending(self) -> None:
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(),
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            claim_daily_report_deliveries=AsyncMock(
                return_value=DailyReportDeliveryClaimResult(claims=(claim,))
            ),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(
                jobs,
                "_daily_report_deadline_reached",
                side_effect=[False, True],
            ),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        bot.send_group_msg.assert_not_awaited()
        self.assertEqual(
            [call.args[1] for call in mocked_store.transition_daily_report_delivery.await_args_list],
            ["release"],
        )
        self.assertEqual(
            mocked_store.transition_daily_report_delivery.await_args.kwargs["error"],
            "delivery_deadline_passed_after_render",
        )

    async def test_disabled_group_is_skipped_after_render(self) -> None:
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(self_id="bot-a", send_group_msg=AsyncMock())

        # 投递前开关全部通过，渲染完成后任一开关关闭都必须跳过发送。
        for rollpig_enabled, report_enabled in ((False, True), (True, False)):
            with self.subTest(
                rollpig_enabled=rollpig_enabled,
                report_enabled=report_enabled,
            ):
                mocked_store = SimpleNamespace(
                    transition_daily_report_delivery=AsyncMock(
                        return_value=DailyReportDeliveryTransitionResult(
                            ok=True,
                            status="skipped",
                        )
                    )
                )
                with (
                    patch.object(jobs, "store", mocked_store),
                    patch.object(
                        jobs,
                        "is_group_rollpig_enabled",
                        side_effect=[True, rollpig_enabled],
                    ),
                    patch.object(
                        jobs,
                        "is_daily_report_enabled",
                        side_effect=[True, report_enabled],
                    ),
                    patch.object(
                        jobs,
                        "_daily_report_deadline_reached",
                        return_value=False,
                    ),
                    patch.object(
                        jobs,
                        "build_group_daily_report",
                        new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
                    ) as build_report,
                    patch.object(
                        jobs,
                        "render_daily_report_card",
                        new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
                    ) as render_card,
                ):
                    result = await jobs._deliver_daily_report_claim(
                        claim,
                        delivery_bots={"100": bot},
                        protect_date="2026-08-27",
                        cutoff_time="23:45",
                    )

                self.assertEqual(result, (True, False, ""))
                build_report.assert_awaited_once()
                render_card.assert_awaited_once()
                bot.send_group_msg.assert_not_awaited()
                self.assertEqual(
                    mocked_store.transition_daily_report_delivery.await_args.args[1],
                    "skip",
                )
                self.assertEqual(
                    mocked_store.transition_daily_report_delivery.await_args.kwargs["error"],
                    "daily_report_disabled_after_render",
                )

    async def test_rendering_skip_failure_schedules_transition_retry(self) -> None:
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(self_id="bot-a", send_group_msg=AsyncMock())
        mocked_store = SimpleNamespace(
            transition_daily_report_delivery=AsyncMock(
                return_value=DailyReportDeliveryTransitionResult(ok=False)
            )
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "is_group_rollpig_enabled", side_effect=[True, False]),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            result = await jobs._deliver_daily_report_claim(
                claim,
                delivery_bots={"100": bot},
                protect_date="2026-08-27",
                cutoff_time="23:45",
            )

        self.assertTrue(result[0])
        self.assertFalse(result[1])
        # skip 状态未获确认时必须返回重领时间，由下一轮重新确认 skip。
        self.assertTrue(result[2])
        parsed_retry = jobs._parse_daily_report_retry_at(result[2])
        self.assertIsNotNone(parsed_retry)
        bot.send_group_msg.assert_not_awaited()
        self.assertEqual(
            mocked_store.transition_daily_report_delivery.await_args.args[1],
            "skip",
        )

    async def test_failure_before_sending_is_reclaimed_at_cloud_retry_time(self) -> None:
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        first_claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
            attempt_count=1,
        )
        second_claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-b",
            attempt_count=2,
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            claim_daily_report_deliveries=AsyncMock(
                side_effect=[
                    DailyReportDeliveryClaimResult(claims=(first_claim,)),
                    DailyReportDeliveryClaimResult(claims=(second_claim,)),
                ]
            ),
            transition_daily_report_delivery=AsyncMock(
                side_effect=[
                    DailyReportDeliveryTransitionResult(
                        ok=True,
                        status="pending",
                        attempt_count=1,
                        next_attempt_at="2026-08-26T15:46:00",
                    ),
                    DailyReportDeliveryTransitionResult(ok=True, status="sending"),
                    DailyReportDeliveryTransitionResult(ok=True, status="sent"),
                ]
            ),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "_daily_report_retry_delay", side_effect=[1.0, None]),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(
                    side_effect=[
                        RuntimeError("build failed"),
                        SimpleNamespace(has_activity=True),
                    ]
                ),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        self.assertEqual(mocked_store.claim_daily_report_deliveries.await_count, 2)
        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(
            [call.args[1] for call in mocked_store.transition_daily_report_delivery.await_args_list],
            ["release", "sending", "sent"],
        )


# ================================ 次日保护结算 ================================ #


class DailyProtectionSettlementTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _roast_event(group_id: str, attacker: str, target: str) -> dict:
        return {
            "type": "success",
            "group_id": group_id,
            "attacker": attacker,
            "target": target,
            "created_at": "2026-08-26T20:00:00+08:00",
        }

    async def test_undelivered_groups_get_protection_settlement(self) -> None:
        # 群 100 开日报走投递；群 200 关日报但 rollpig 开启，保护照常结算。
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        call_order: list[str] = []

        async def mark_report_sent(*args, **kwargs):
            call_order.append("report")

        async def settle_protections(*args, **kwargs):
            call_order.append("protection")

        bot.send_group_msg.side_effect = mark_report_sent

        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100", "200"}),
            query_daily_events=AsyncMock(
                return_value=DailyEventQueryResult(
                    items=(
                        self._roast_event("200", "user-a", "user-b"),
                        self._roast_event("200", "user-c", "user-b"),
                    ),
                    available=True,
                )
            ),
            replace_group_protections=AsyncMock(side_effect=settle_protections),
            claim_daily_report_deliveries=AsyncMock(
                return_value=DailyReportDeliveryClaimResult(claims=(claim,))
            ),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(
                jobs,
                "is_daily_report_enabled",
                side_effect=lambda group_id: group_id == "100",
            ),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(call_order, ["report", "protection"])
        mocked_store.replace_group_protections.assert_awaited_once_with(
            "200", ["user-b"], "2026-08-27"
        )

    async def test_settlement_skipped_when_events_unavailable(self) -> None:
        # 事件记录不可用时宁可跳过结算，也不能把保护名单误写成空。
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"200"}),
            query_daily_events=AsyncMock(
                return_value=DailyEventQueryResult(items=(), available=False)
            ),
            replace_group_protections=AsyncMock(),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=False),
        ):
            await jobs.daily_report_job()

        mocked_store.query_daily_events.assert_awaited_once()
        mocked_store.replace_group_protections.assert_not_awaited()

    async def test_settlement_continues_after_group_failure(self) -> None:
        # 单群重试耗尽只损失该群保护，不能阻断其余群结算。
        events = (
            self._roast_event("200", "user-a", "user-b"),
            self._roast_event("200", "user-c", "user-b"),
            self._roast_event("300", "user-d", "user-e"),
            self._roast_event("300", "user-f", "user-e"),
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"200", "300"}),
            query_daily_events=AsyncMock(
                return_value=DailyEventQueryResult(items=events, available=True)
            ),
            replace_group_protections=AsyncMock(
                side_effect=[
                    RuntimeError("cloud write failed 1"),
                    RuntimeError("cloud write failed 2"),
                    RuntimeError("cloud write failed 3"),
                    None,
                ]
            ),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "PROTECTION_SETTLEMENT_RETRY_DELAYS", (0.0, 0.0, 0.0)),
        ):
            await jobs.settle_daily_protections(
                ["200", "300"],
                date_str="2026-08-26",
                protect_date="2026-08-27",
                cutoff_at="2026-08-26T23:45:00+08:00",
            )

        self.assertEqual(mocked_store.replace_group_protections.await_count, 4)
        final_call = mocked_store.replace_group_protections.await_args_list[-1]
        self.assertEqual(final_call.args, ("300", ["user-e"], "2026-08-27"))

    async def test_settlement_retries_group_write_until_success(self) -> None:
        mocked_store = SimpleNamespace(
            query_daily_events=AsyncMock(
                return_value=DailyEventQueryResult(
                    items=(
                        self._roast_event("200", "user-a", "user-b"),
                        self._roast_event("200", "user-c", "user-b"),
                    ),
                    available=True,
                )
            ),
            replace_group_protections=AsyncMock(
                side_effect=[RuntimeError("temporary failure"), None]
            ),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "PROTECTION_SETTLEMENT_RETRY_DELAYS", (0.0, 0.0, 0.0)),
        ):
            await jobs.settle_daily_protections(
                ["200"],
                date_str="2026-08-26",
                protect_date="2026-08-27",
                cutoff_at="2026-08-26T23:45:00+08:00",
            )

        self.assertEqual(mocked_store.replace_group_protections.await_count, 2)

    async def test_settlement_event_query_failure_is_isolated(self) -> None:
        mocked_store = SimpleNamespace(
            query_daily_events=AsyncMock(side_effect=RuntimeError("event store damaged")),
            replace_group_protections=AsyncMock(),
        )

        with patch.object(jobs, "store", mocked_store):
            await jobs.settle_daily_protections(
                ["200"],
                date_str="2026-08-26",
                protect_date="2026-08-27",
                cutoff_at="2026-08-26T23:45:00+08:00",
            )

        mocked_store.replace_group_protections.assert_not_awaited()

    async def test_unexpected_settlement_failure_does_not_block_report_delivery(self) -> None:
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100", "200"}),
            claim_daily_report_deliveries=AsyncMock(
                return_value=DailyReportDeliveryClaimResult(claims=(claim,))
            ),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(
                jobs,
                "is_daily_report_enabled",
                side_effect=lambda group_id: group_id == "100",
            ),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "settle_daily_protections",
                new=AsyncMock(side_effect=RuntimeError("unexpected settlement error")),
            ),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        bot.send_group_msg.assert_awaited_once()

    async def test_no_settlement_query_when_all_groups_deliver(self) -> None:
        # 回归保障：所有活跃群都开日报时不产生额外事件查询与保护写入。
        claim = DailyReportDeliveryClaim(
            "2026-08-26",
            "100",
            "bot-a",
            "2026-08-26T23:45:00+08:00",
            "claim-a",
        )
        bot = SimpleNamespace(
            self_id="bot-a",
            send_group_msg=AsyncMock(return_value={"message_id": 123}),
        )
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"100"}),
            query_daily_events=AsyncMock(),
            replace_group_protections=AsyncMock(),
            claim_daily_report_deliveries=AsyncMock(
                return_value=DailyReportDeliveryClaimResult(claims=(claim,))
            ),
            transition_daily_report_delivery=AsyncMock(return_value=True),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=True),
            patch.object(jobs, "is_daily_report_enabled", return_value=True),
            patch.object(jobs, "_daily_report_deadline_reached", return_value=False),
            patch.object(
                jobs,
                "resolve_daily_report_bots",
                new=AsyncMock(return_value={"100": bot}),
            ),
            patch.object(
                jobs,
                "build_group_daily_report",
                new=AsyncMock(return_value=SimpleNamespace(has_activity=True)),
            ),
            patch.object(
                jobs,
                "render_daily_report_card",
                new=AsyncMock(return_value=SimpleNamespace(data=b"image")),
            ),
        ):
            await jobs.daily_report_job()

        mocked_store.query_daily_events.assert_not_awaited()
        mocked_store.replace_group_protections.assert_not_awaited()
        bot.send_group_msg.assert_awaited_once()

    async def test_rollpig_disabled_groups_are_not_settled(self) -> None:
        # rollpig 总开关关闭的群既不投递也不结算，保持"彻底关闭"。
        mocked_store = SimpleNamespace(
            get_active_group_ids=AsyncMock(return_value={"200"}),
            query_daily_events=AsyncMock(),
            replace_group_protections=AsyncMock(),
        )

        with (
            patch.object(jobs, "store", mocked_store),
            patch.object(jobs, "rollpig_date_str", return_value="2026-08-26"),
            patch.object(jobs.random, "randint", return_value=0),
            patch.object(jobs.asyncio, "sleep", new=AsyncMock()),
            patch.object(jobs, "is_group_rollpig_enabled", return_value=False),
            patch.object(jobs, "is_daily_report_enabled", return_value=False),
        ):
            await jobs.daily_report_job()

        mocked_store.query_daily_events.assert_not_awaited()
        mocked_store.replace_group_protections.assert_not_awaited()


# ================================ Cloud 日报 HTTP 契约 ================================ #


class CloudDailyReportContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _create_store(handler) -> CloudStore:
        """绕过运行配置创建真实 HTTP client，仅替换网络传输层。"""

        cloud_store = object.__new__(CloudStore)
        cloud_store.base_url = "https://cloud.example"
        cloud_store.strict_mode = True
        cloud_store._client = httpx.AsyncClient(
            base_url=cloud_store.base_url,
            transport=httpx.MockTransport(handler),
        )
        return cloud_store

    async def test_profiles_claim_and_transition_match_cloud_contract(self) -> None:
        requests: list[tuple[str, dict]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads((await request.aread()).decode("utf-8"))
            requests.append((request.url.path, payload))
            if request.url.path.endswith("/profiles"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "user_id": "user-a",
                                "daily_pig_id": "pig-a",
                                "daily_ex_level": 2,
                                "daily_achieved_at": "2026-08-31T15:20:00",
                                "catalog_count": 12,
                                "catalog_achieved_at": "2026-08-31T15:20:00",
                                "recent_pig_id": "pig-a",
                                "recent_ex_level": 2,
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/claim"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "date_str": "2026-08-31",
                                "group_id": "100",
                                "delivery_bot_id": "bot-a",
                                "cutoff_at": "2026-08-31T15:45:00",
                                "claim_token": "claim-token",
                                "status": "claimed",
                                "attempt_count": 1,
                            }
                        ],
                        "next_claim_at": "2026-08-31T15:50:00",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "status": "sending",
                    "attempt_count": 1,
                    "next_attempt_at": None,
                },
            )

        cloud_store = self._create_store(handler)
        cutoff_at = "2026-08-31T23:45:00+08:00"
        try:
            profiles = await cloud_store.get_daily_report_profiles(
                group_id="100",
                date_str="2026-08-31",
                cutoff_at=cutoff_at,
                user_ids=("user-a",),
            )
            claim_result = await cloud_store.claim_daily_report_deliveries(
                instance_id="instance-a",
                delivery_bots={"100": "bot-a"},
                date_str="2026-08-31",
                cutoff_at=cutoff_at,
            )
            transition = await cloud_store.transition_daily_report_delivery(
                claim_result.claims[0],
                "sending",
            )
        finally:
            await cloud_store.close()

        self.assertEqual(profiles[0].daily_ex_level, 2)
        self.assertEqual(profiles[0].catalog_count, 12)
        self.assertEqual(claim_result.claims[0].claim_token, "claim-token")
        self.assertEqual(claim_result.next_claim_at, "2026-08-31T15:50:00")
        self.assertTrue(transition.ok)
        self.assertEqual(
            [path for path, _ in requests],
            [
                "/v1/daily-reports/profiles",
                "/v1/daily-reports/claim",
                "/v1/daily-reports/transition",
            ],
        )
        self.assertEqual(requests[0][1]["user_ids"], ["user-a"])
        self.assertEqual(
            requests[1][1]["candidates"],
            [{"group_id": "100", "delivery_bot_id": "bot-a"}],
        )
        self.assertEqual(requests[2][1]["action"], "sending")

    async def test_later_claim_batch_failure_preserves_acquired_claims(self) -> None:
        requests: list[dict] = []
        date_str = jobs.rollpig_date_str()

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads((await request.aread()).decode("utf-8"))
            requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "date_str": date_str,
                                "group_id": "000",
                                "delivery_bot_id": "bot-a",
                                "cutoff_at": "2026-08-31T15:45:00",
                                "claim_token": "claim-first-batch",
                                "status": "claimed",
                                "attempt_count": 1,
                            }
                        ]
                    },
                )
            raise httpx.ConnectError("second batch offline", request=request)

        cloud_store = self._create_store(handler)
        try:
            result = await cloud_store.claim_daily_report_deliveries(
                instance_id="instance-a",
                delivery_bots={f"{index:03}": "bot-a" for index in range(257)},
                date_str=date_str,
                cutoff_at=f"{date_str}T23:45:00+08:00",
            )
        finally:
            await cloud_store.close()

        self.assertEqual([len(item["candidates"]) for item in requests], [256, 1])
        self.assertEqual(len(result.claims), 1)
        self.assertEqual(result.claims[0].claim_token, "claim-first-batch")
        self.assertTrue(result.next_claim_at)
        self.assertIsNotNone(jobs._daily_report_retry_delay(date_str, [result.next_claim_at]))

    async def test_missing_daily_report_routes_raise_scoped_compatibility_error(self) -> None:
        for status_code in (404, 405):
            with self.subTest(status_code=status_code):
                async def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(status_code, request=request)

                cloud_store = self._create_store(handler)
                try:
                    with self.assertRaises(CloudDailyReportUnsupportedError):
                        await cloud_store.claim_daily_report_deliveries(
                            instance_id="instance-a",
                            delivery_bots={"100": "bot-a"},
                            date_str="2026-08-31",
                            cutoff_at="2026-08-31T23:45:00+08:00",
                        )
                finally:
                    await cloud_store.close()


if __name__ == "__main__":
    unittest.main()
