from __future__ import annotations

import unittest

import test_roast_reservations

from nonebot_plugin_rollpig_plus.roast_statistics import count_roasted_users


class RoastVictimStatisticsTests(unittest.TestCase):
    def test_each_special_kind_counts_once_per_group_day_and_user(self):
        events = [
            {"type": "bot_backfire", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
            {"type": "bot_backfire", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
            {"type": "backfire", "special_reason": "unrolled", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
            {"type": "backfire", "special_reason": "unrolled", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
            {"type": "backfire", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
            {"type": "backfire", "attacker": "a", "group_id": "100", "date_str": "2026-10-24"},
        ]
        self.assertEqual(count_roasted_users(events)["a"], 4)
        events.append({**events[0], "group_id": "200"})
        events.append({**events[0], "date_str": "2026-10-25"})
        self.assertEqual(count_roasted_users(events)["a"], 6)

    def test_multiple_victims_legacy_fallback_and_event_deduplication(self):
        events = [
            {"event_id": "1", "type": "success", "attacker": "cook", "target": "a"},
            {"event_id": "2", "type": "backfire", "attacker": "cook", "target": "target",
             "backfire_victim_id": "a", "backfire_victim_ids": ["a", "b", "b"]},
            {"event_id": "2", "type": "backfire", "attacker": "cook", "backfire_victim_ids": ["a", "b"]},
            {"type": "backfire", "attacker": "c"},
            {"type": "self_roast", "attacker": "a", "target": "a"},
            {"type": "escape", "attacker": "a", "target": "a"},
            {"type": "reserved_special", "attacker": "a", "target": "a"},
        ]
        self.assertEqual(dict(count_roasted_users(events)), {"a": 2, "b": 1, "c": 1})
