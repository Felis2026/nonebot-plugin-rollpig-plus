from __future__ import annotations

import asyncio
import datetime as dt
import json
import tempfile
import threading
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

from nonebot_plugin_rollpig_plus import daily_protection as protection
from nonebot_plugin_rollpig_plus import data_manager as data_manager_module
from nonebot_plugin_rollpig_plus import jobs
from nonebot_plugin_rollpig_plus.data_manager import PigDataManager
from nonebot_plugin_rollpig_plus.store.cloud import CloudStore
from nonebot_plugin_rollpig_plus.store.local_json import LocalJsonStore
from nonebot_plugin_rollpig_plus.store.models import DailyEventQueryResult


class ProtectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "protection_recovery.json"
        self.now = dt.datetime(2026, 10, 7, 23, 50, tzinfo=protection.ROLLPIG_TIMEZONE)
        self.clock = patch.object(protection, "rollpig_now", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.enabled = patch.object(protection, "is_group_rollpig_enabled", return_value=True)
        self.enabled_mock = self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.queue = protection.ProtectionRecoveryQueue(self.path)
        self.store = SimpleNamespace(
            query_daily_events=AsyncMock(return_value=DailyEventQueryResult(items=(), available=True)),
            replace_group_protections=AsyncMock(),
            claim_daily_report_deliveries=AsyncMock(),
            send_group_msg=AsyncMock(),
        )

    async def enqueue(self, protected_ids: list[str] | None, group_id: str = "100") -> None:
        await self.queue.enqueue(
            group_id, "2026-10-07", "2026-10-08", "2026-10-07T23:45:00+08:00",
            protected_ids=protected_ids,
        )

    def records(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))["records"]

    @staticmethod
    def roast_event(target: str, *, group_id: str = "100", time: str = "20:00:00") -> dict:
        return {"type": "success", "attacker": "cook", "target": target,
                "group_id": group_id, "created_at": f"2026-10-07T{time}+08:00"}

    # ================================ 持久化与有限预算 ================================ #
    async def test_known_list_is_persisted_and_success_removes_only_failed_group(self) -> None:
        await self.enqueue(["小猪🐷"])
        self.assertEqual(self.records()[0]["protected_ids"], ["小猪🐷"])
        self.assertNotIn("events", self.records()[0])
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=29))
        self.store.replace_group_protections.assert_not_awaited()
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.replace_group_protections.assert_awaited_once_with("100", ["小猪🐷"], "2026-10-08")
        self.store.query_daily_events.assert_not_awaited()
        self.store.claim_daily_report_deliveries.assert_not_awaited()
        self.store.send_group_msg.assert_not_awaited()
        self.assertEqual(self.records(), [])

    async def test_restart_preserves_attempt_count_deadline_and_computed_list(self) -> None:
        await self.enqueue(None)
        self.store.query_daily_events.return_value = DailyEventQueryResult(items=(
            self.roast_event("victim"), self.roast_event("victim"),
        ), available=True)
        self.store.replace_group_protections.side_effect = RuntimeError("offline")
        first_attempt = self.now + dt.timedelta(seconds=30)
        await self.queue.run_due(self.store, now=first_attempt)
        record = self.records()[0]
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["protected_ids"], ["victim"])
        self.assertEqual(record["cutoff_at"], "2026-10-07T23:45:00+08:00")
        self.queue = protection.ProtectionRecoveryQueue(self.path)
        await self.enqueue(None)
        self.assertEqual(self.records()[0], record)
        await self.queue.run_due(self.store, now=first_attempt + dt.timedelta(seconds=59))
        self.assertEqual(self.store.replace_group_protections.await_count, 1)
        self.store.replace_group_protections.side_effect = None
        await self.queue.run_due(self.store, now=first_attempt + dt.timedelta(seconds=60))
        self.assertEqual(self.store.replace_group_protections.await_count, 2)
        self.store.query_daily_events.assert_awaited_once()
        self.assertEqual(self.records(), [])

    async def test_budget_exhaustion_survives_restart_and_repeated_enqueuing(self) -> None:
        await self.enqueue(["victim"])
        self.store.replace_group_protections.side_effect = RuntimeError("offline")
        current = self.now
        for delay in protection.PROTECTION_RECOVERY_DELAYS:
            current += dt.timedelta(seconds=delay)
            self.queue = protection.ProtectionRecoveryQueue(self.path)
            await self.queue.run_due(self.store, now=current)
        self.assertEqual(self.store.replace_group_protections.await_count, 5)
        self.assertEqual(self.records()[0]["attempt_count"], 5)
        await self.enqueue(["victim"])
        await self.queue.run_due(self.store, now=current + dt.timedelta(hours=1))
        self.assertEqual(self.store.replace_group_protections.await_count, 5)

    async def test_empty_confirmed_list_is_not_treated_as_missing_query(self) -> None:
        await self.enqueue([])
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.query_daily_events.assert_not_awaited()
        self.store.replace_group_protections.assert_awaited_once_with("100", [], "2026-10-08")

    async def test_corrupt_queue_is_not_overwritten(self) -> None:
        content = '{"records": 未完成'
        self.path.write_text(content, encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.enqueue(["victim"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    async def test_atomic_write_failure_keeps_previous_record_and_budget(self) -> None:
        await self.enqueue(["victim"])
        original = self.records()
        with patch.object(self.queue, "_write_sync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.assertEqual(self.records(), original)
        self.store.replace_group_protections.assert_not_awaited()
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.replace_group_protections.assert_awaited_once()

    async def test_cancelling_persistence_waits_for_writer_before_releasing_lock(self) -> None:
        started = threading.Event()
        release = threading.Event()
        original_write = self.queue._write_sync

        def write(records):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test writer was not released")
            original_write(records)

        with patch.object(self.queue, "_write_sync", side_effect=write):
            task = asyncio.create_task(self.enqueue(["victim"]))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(self.queue._lock.locked())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.records()[0]["protected_ids"], ["victim"])
        await self.queue.discard("2026-10-07", "100")
        self.assertEqual(self.records(), [])

    # ================================ 固定截止点、跨日与隔离 ================================ #
    async def test_missing_query_uses_original_group_date_and_cutoff_after_midnight(self) -> None:
        await self.enqueue(None)
        self.store.query_daily_events.return_value = DailyEventQueryResult(items=(
            self.roast_event("victim"), self.roast_event("victim"),
            *[self.roast_event("late", time="23:46:00") for _ in range(3)],
            *[self.roast_event("other", group_id="200") for _ in range(3)],
        ), available=True)
        current = dt.datetime(2026, 10, 8, 0, 20, tzinfo=protection.ROLLPIG_TIMEZONE)
        await self.queue.run_due(self.store, now=current)
        self.store.query_daily_events.assert_awaited_once_with(
            date_str="2026-10-07", group_id="100", cutoff_at="2026-10-07T23:45:00+08:00",
        )
        self.store.replace_group_protections.assert_awaited_once_with("100", ["victim"], "2026-10-08")

    async def test_unavailable_events_never_clear_existing_protection(self) -> None:
        await self.enqueue(None)
        self.store.query_daily_events.return_value = DailyEventQueryResult(available=False)
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.replace_group_protections.assert_not_awaited()
        self.assertIsNone(self.records()[0]["protected_ids"])
        self.assertEqual(self.records()[0]["attempt_count"], 1)

    async def test_lost_write_response_retries_same_list_without_querying_again(self) -> None:
        await self.enqueue(["victim"])
        applied = []

        async def write(group_id, user_ids, protect_date):
            applied.append((group_id, list(user_ids), protect_date))
            if len(applied) == 1:
                raise TimeoutError("write applied but response lost")

        self.store.replace_group_protections.side_effect = write
        current = self.now + dt.timedelta(seconds=30)
        await self.queue.run_due(self.store, now=current)
        self.queue = protection.ProtectionRecoveryQueue(self.path)
        await self.queue.run_due(self.store, now=current + dt.timedelta(seconds=60))
        self.assertEqual(applied, [("100", ["victim"], "2026-10-08")] * 2)
        self.store.query_daily_events.assert_not_awaited()
        self.assertEqual(self.records(), [])

    async def test_one_group_query_failure_does_not_block_other_group(self) -> None:
        await self.enqueue(None)
        await self.enqueue(["victim"], group_id="200")
        self.store.query_daily_events.side_effect = RuntimeError("offline")
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.replace_group_protections.assert_awaited_once_with("200", ["victim"], "2026-10-08")
        self.assertEqual([record["group_id"] for record in self.records()], ["100"])

    async def test_expired_or_disabled_groups_are_removed_without_query_or_write(self) -> None:
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.enabled_mock.return_value = True
                await self.enqueue(None)
                self.enabled_mock.return_value = expired
                current = self.now + dt.timedelta(days=2) if expired else self.now + dt.timedelta(seconds=30)
                await self.queue.run_due(self.store, now=current)
                self.assertEqual(self.records(), [])
                self.store.query_daily_events.assert_not_awaited()
                self.store.replace_group_protections.assert_not_awaited()

    async def test_disabling_group_during_query_prevents_protection_write(self) -> None:
        await self.enqueue(None)

        async def query(**kwargs):
            self.enabled_mock.return_value = False
            return DailyEventQueryResult(items=(), available=True)

        self.store.query_daily_events.side_effect = query
        await self.queue.run_due(self.store, now=self.now + dt.timedelta(seconds=30))
        self.store.replace_group_protections.assert_not_awaited()
        self.assertEqual(self.records(), [])

    async def test_concurrent_recovery_is_coalesced(self) -> None:
        await self.enqueue(["victim"])
        started = asyncio.Event()
        release = asyncio.Event()

        async def write(*args):
            started.set()
            await release.wait()

        self.store.replace_group_protections.side_effect = write
        current = self.now + dt.timedelta(seconds=30)
        task = asyncio.create_task(self.queue.run_due(self.store, now=current))
        await asyncio.wait_for(started.wait(), timeout=2)
        await self.queue.run_due(self.store, now=current)
        release.set()
        await task
        self.store.replace_group_protections.assert_awaited_once()

    async def test_shutdown_cancellation_leaves_durable_retry_budget(self) -> None:
        await self.enqueue(["victim"])
        started = asyncio.Event()

        async def write(*args):
            started.set()
            await asyncio.Event().wait()

        self.store.replace_group_protections.side_effect = write
        current = self.now + dt.timedelta(seconds=30)
        task = asyncio.create_task(self.queue.run_due(self.store, now=current))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.records()[0]["attempt_count"], 1)
        self.queue = protection.ProtectionRecoveryQueue(self.path)
        self.store.replace_group_protections.side_effect = None
        await self.queue.run_due(self.store, now=current + dt.timedelta(seconds=60))
        self.assertEqual(self.records(), [])

    # ================================ 两种后端实际写入 ================================ #
    async def test_local_store_receives_protection_after_restart(self) -> None:
        await self.enqueue(["victim"])
        self.queue = protection.ProtectionRecoveryQueue(self.path)
        with patch.object(data_manager_module, "DATA_FILE", self.path.parent / "pig_data.json"):
            manager = PigDataManager()
        local_store = LocalJsonStore(lambda: manager)
        await self.queue.run_due(local_store, now=self.now + dt.timedelta(seconds=30))
        self.assertTrue(await local_store.is_protected("100", "victim", "2026-10-08"))
        self.assertFalse(await local_store.is_protected("200", "victim", "2026-10-08"))
        self.assertEqual(self.records(), [])

    async def test_cloud_reuses_existing_idempotent_replace_endpoint(self) -> None:
        await self.enqueue(["victim"])
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, json.loads((await request.aread()).decode("utf-8"))))
            return httpx.Response(200, json={"ok": True})

        cloud_store = object.__new__(CloudStore)
        cloud_store.base_url = "https://cloud.example"
        cloud_store.strict_mode = True
        cloud_store._client = httpx.AsyncClient(base_url=cloud_store.base_url, transport=httpx.MockTransport(handler))
        try:
            await self.queue.run_due(cloud_store, now=self.now + dt.timedelta(seconds=30))
        finally:
            await cloud_store.close()
        self.assertEqual(requests, [("/v1/protections/replace-group", {
            "group_id": "100", "user_ids": ["victim"], "protect_date": "2026-10-08",
        })])
        self.assertEqual(self.records(), [])


class ProtectionRecoveryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_timer_share_one_tracked_background_task(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def recover(report_store) -> None:
            entered.set()
            await release.wait()

        queue = SimpleNamespace(run_due=AsyncMock(side_effect=recover))
        tasks = set()
        with (
            patch.object(jobs, "protection_recovery_queue", queue),
            patch.object(jobs, "protection_recovery_task", None),
            patch.object(jobs, "background_maintenance_tasks", tasks),
        ):
            await jobs.startup_protection_recovery()
            task = jobs.protection_recovery_task
            await asyncio.wait_for(entered.wait(), timeout=2)
            await jobs.protection_recovery_job()
            self.assertEqual(tasks, {task})
            queue.run_due.assert_awaited_once()
            release.set()
            await task
            await asyncio.sleep(0)
            self.assertEqual(tasks, set())
