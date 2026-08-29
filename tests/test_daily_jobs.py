from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class DailySummaryBotRoutingTests(unittest.IsolatedAsyncioTestCase):
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
            resolved = await jobs.resolve_daily_summary_bots(["100", "200"])

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
            resolved = await jobs.resolve_daily_summary_bots(["300", "400"])

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


if __name__ == "__main__":
    unittest.main()
