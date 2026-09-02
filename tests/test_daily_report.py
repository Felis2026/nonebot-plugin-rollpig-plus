from __future__ import annotations

import unittest

import nonebot
from nonebot.plugin import get_plugin

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus.daily_report import (
    DailyUserReportProfile,
    ProtectionReportItem,
    build_daily_report,
    build_rankings,
    normalize_daily_events,
    select_headline,
    select_overview_metrics,
    select_timeline,
)


DATE = "2026-08-28"
GROUP = "100"


def event(
    event_id: str,
    event_type: str,
    attacker: str,
    target: str,
    hour: int,
    **extra: object,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "type": event_type,
        "attacker": attacker,
        "target": target,
        "attacker_name": f"用户{attacker}",
        "target_name": f"用户{target}",
        "group_id": GROUP,
        "created_at": f"{DATE}T{hour:02d}:00:00+08:00",
        **extra,
    }


class DailyReportNormalizationTests(unittest.TestCase):
    def test_normalization_filters_unknown_and_other_group_events(self) -> None:
        items = normalize_daily_events(
            [
                event("valid", "success", "a", "b", 8),
                event("unknown", "future_type", "a", "b", 9),
                {**event("other", "escape", "a", "b", 10), "group_id": "200"},
                {
                    **event("reservation", "escape", "a", "b", 11),
                    "reservation_id": "r1",
                    "participant_ids": ["a", "c"],
                    "participant_count": "bad",
                },
            ],
            group_id=GROUP,
        )

        self.assertEqual([item.event_id for item in items], ["valid", "reservation"])
        self.assertEqual(items[1].participant_count, 2)
        self.assertTrue(items[1].is_reservation)

    def test_participant_names_keep_alignment_when_some_names_are_empty(self) -> None:
        items = normalize_daily_events(
            [
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    8,
                    reservation_id="r1",
                    participant_ids=["a", "b", "c"],
                    participant_names=["用户A", "", "用户C"],
                )
            ],
            group_id=GROUP,
        )

        self.assertEqual(items[0].participant_ids, ("a", "b", "c"))
        self.assertEqual(items[0].participant_names, ("用户A", "", "用户C"))

    def test_events_after_fixed_cutoff_are_excluded(self) -> None:
        items = normalize_daily_events(
            [
                event("before", "success", "a", "b", 23),
                {
                    **event("after", "escape", "a", "b", 23),
                    "created_at": f"{DATE}T23:46:00+08:00",
                },
            ],
            group_id=GROUP,
            cutoff_at=f"{DATE}T23:45:00+08:00",
        )

        self.assertEqual([item.event_id for item in items], ["before"])

    def test_report_excludes_bot_target_but_keeps_real_participants(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[
                event("bot", "bot_backfire", "a", "bot", 8),
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    9,
                    reservation_id="r1",
                    participant_ids=["a", "c"],
                ),
            ],
            active_user_ids=["d"],
            bot_user_ids=["bot"],
        )

        self.assertEqual(report.participant_ids, ("a", "b", "c", "d"))
        self.assertNotIn("bot", report.participant_ids)

    def test_empty_report_has_no_generated_sections(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={},
            raw_events=[],
        )

        self.assertFalse(report.has_activity)
        self.assertIsNone(report.observation)
        self.assertIsNone(report.headline)
        self.assertIsNone(report.timeline)
        self.assertEqual(report.rankings, ())

    def test_invalid_roll_rows_and_bot_rolls_do_not_enter_overview(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"": "pig-a", "a": "", "bot": "pig-b", "real": "pig-c"},
            raw_events=[],
            bot_user_ids=["bot"],
        )

        self.assertEqual(report.overview.roll_count, 1)
        self.assertEqual(report.overview.pig_variety_count, 1)
        self.assertEqual(report.participant_ids, ("real",))


class DailyReportObservationTests(unittest.TestCase):
    def test_self_roast_is_not_counted_as_ordinary_roast(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[
                event("self", "self_roast", "a", "a", 1),
                event("ordinary", "success", "a", "b", 2),
            ],
        )

        self.assertEqual(report.overview.ordinary_roast_count, 1)
        self.assertEqual(report.overview.ordinary_success_count, 1)

    def test_overview_third_metric_uses_fixed_fallback_order(self) -> None:
        reservation = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[event("r1", "escape", "a", "b", 1, reservation_id="r1")],
        )
        escape = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[event("e1", "escape", "a", "b", 1)],
        )
        backfire = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[event("b1", "backfire", "a", "b", 1)],
        )
        variety = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[],
        )

        self.assertEqual(reservation.overview_metrics[-1].kind, "reservation")
        self.assertEqual(escape.overview_metrics[-1].kind, "escape")
        self.assertEqual(backfire.overview_metrics[-1].kind, "backfire")
        self.assertEqual(variety.overview_metrics[-1].kind, "pig_variety")
        self.assertEqual(len(select_overview_metrics(type(variety.overview)())), 2)

    def test_each_observation_threshold_selects_expected_kind(self) -> None:
        cases = (
            (
                "backfire",
                {},
                [
                    event("b1", "backfire", "a", "b", 1),
                    event("b2", "backfire", "c", "d", 2),
                    event("s1", "success", "e", "f", 3),
                    event("s2", "success", "g", "h", 4),
                ],
            ),
            (
                "escape",
                {},
                [
                    event("e1", "escape", "a", "b", 1),
                    event("e2", "escape", "c", "d", 2),
                    event("s1", "success", "e", "f", 3),
                    event("s2", "success", "g", "h", 4),
                    event("s3", "success", "i", "j", 5),
                ],
            ),
            (
                "success",
                {},
                [
                    event("s1", "success", "a", "b", 1),
                    event("s2", "success", "c", "d", 2),
                    event("s3", "success", "e", "f", 3),
                    event("e1", "escape", "g", "h", 4),
                    event("b1", "backfire", "i", "j", 5),
                ],
            ),
            ("human", {"a": "human", "b": "human"}, []),
        )
        for expected_kind, rolls, raw_events in cases:
            with self.subTest(expected_kind=expected_kind):
                report = build_daily_report(
                    date_str=DATE,
                    group_id=GROUP,
                    group_rolls=rolls,
                    raw_events=raw_events,
                )
                self.assertEqual(report.observation.kind, expected_kind)  # type: ignore[union-attr]

    def test_observation_is_hidden_without_a_clear_pattern(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a", "b": "pig-b"},
            raw_events=[event("success", "success", "a", "b", 1)],
        )

        self.assertIsNone(report.observation)

    def test_reservation_observation_wins_over_other_matching_conditions(self) -> None:
        rolls = {
            "u1": "human",
            "u2": "human",
            "u3": "pig-a",
            "u4": "pig-a",
            "u5": "pig-a",
            "u6": "pig-b",
            "u7": "pig-c",
            "u8": "pig-d",
        }
        events = [
            event("r1", "success", "a", "b", 1, reservation_id="r1", participant_count=3),
            event("r2", "escape", "a", "b", 2, reservation_id="r2", participant_count=3),
            event("r3", "backfire", "a", "b", 3, reservation_id="r3", participant_count=3),
            event("n1", "backfire", "c", "d", 4),
            event("n2", "backfire", "c", "d", 5),
        ]

        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls=rolls,
            raw_events=events,
        )

        self.assertIsNotNone(report.observation)
        assert report.observation is not None
        self.assertEqual(report.observation.kind, "reservation")
        self.assertEqual(report.observation.dominant_result, "success")
        self.assertEqual(report.observation.success_count, 1)
        self.assertEqual(report.observation.escape_count, 1)
        self.assertEqual(report.observation.backfire_count, 1)

    def test_variety_requires_threshold_and_collision_has_higher_priority(self) -> None:
        diverse = {f"u{index}": f"pig-{index}" for index in range(10)}
        variety_report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls=diverse,
            raw_events=[],
        )
        self.assertEqual(variety_report.observation.kind, "variety")  # type: ignore[union-attr]

        collided = dict(diverse)
        collided.update({"u8": "pig-1", "u9": "pig-1"})
        collision_report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls=collided,
            raw_events=[],
        )
        self.assertEqual(collision_report.observation.kind, "collision")  # type: ignore[union-attr]


class DailyReportHeadlineTests(unittest.TestCase):
    def test_low_score_normal_event_is_hidden_but_bot_backfire_qualifies(self) -> None:
        normalized = normalize_daily_events(
            [
                event("normal", "success", "a", "b", 8),
                event("bot", "bot_backfire", "c", "bot", 9),
            ],
            group_id=GROUP,
        )

        headline = select_headline(normalized)

        self.assertIsNotNone(headline)
        assert headline is not None
        self.assertEqual(headline.kind, "bot_backfire")
        self.assertEqual(headline.score, 50)

    def test_reservation_score_uses_participants_size_and_repeat_bonus(self) -> None:
        normalized = normalize_daily_events(
            [
                event("normal", "escape", "a", "b", 8),
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    9,
                    reservation_id="r1",
                    participant_count=6,
                ),
            ],
            group_id=GROUP,
        )

        headline = select_headline(normalized)

        self.assertIsNotNone(headline)
        assert headline is not None
        self.assertEqual(headline.kind, "reservation_escape")
        self.assertEqual(headline.score, 100)
        self.assertEqual(headline.repeated_pair_events, 1)

    def test_special_target_is_split_for_renderer_asset_selection(self) -> None:
        normalized = normalize_daily_events(
            [
                event(
                    "special",
                    "reserved_special",
                    "a",
                    "b",
                    8,
                    reservation_id="r1",
                    special_reason="sold",
                    participant_count=2,
                )
            ],
            group_id=GROUP,
        )

        headline = select_headline(normalized)

        self.assertEqual(headline.kind, "reservation_sold")  # type: ignore[union-attr]

    def test_repeated_self_roast_can_reach_headline_threshold(self) -> None:
        normalized = normalize_daily_events(
            [event(f"self-{index}", "self_roast", "a", "a", index) for index in range(1, 6)],
            group_id=GROUP,
        )

        headline = select_headline(normalized)

        self.assertIsNotNone(headline)
        self.assertEqual(headline.kind, "self_roast")  # type: ignore[union-attr]
        self.assertEqual(headline.score, 50)  # type: ignore[union-attr]

    def test_all_reservation_headline_assets_have_reachable_kinds(self) -> None:
        cases = (
            ("success", "", "reservation_success"),
            ("escape", "", "reservation_escape"),
            ("backfire", "", "reservation_backfire"),
            ("reserved_special", "human", "reservation_human"),
            ("reserved_special", "food", "reservation_food"),
            ("reserved_special", "eaten", "reservation_eaten"),
            ("reserved_special", "sold", "reservation_sold"),
        )
        for event_type, special_reason, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                normalized = normalize_daily_events(
                    [
                        event(
                            "reservation",
                            event_type,
                            "a",
                            "b",
                            8,
                            reservation_id="r1",
                            participant_count=2,
                            special_reason=special_reason,
                        )
                    ],
                    group_id=GROUP,
                )
                self.assertEqual(select_headline(normalized).kind, expected_kind)  # type: ignore[union-attr]


class DailyReportTimelineTests(unittest.TestCase):
    def test_mutual_timeline_wins_and_removes_headline_event(self) -> None:
        normalized = normalize_daily_events(
            [
                event("a-to-b-1", "success", "a", "b", 1),
                event("b-to-a", "escape", "b", "a", 2),
                event("a-to-b-2", "bot_backfire", "a", "b", 3),
                event("a-to-b-3", "success", "a", "b", 4),
            ],
            group_id=GROUP,
        )
        headline = select_headline(normalized)

        timeline = select_timeline(normalized, headline)

        self.assertIsNotNone(headline)
        self.assertEqual(headline.event.event_id, "a-to-b-2")  # type: ignore[union-attr]
        self.assertIsNotNone(timeline)
        assert timeline is not None
        self.assertEqual(timeline.kind, "mutual")
        self.assertEqual(
            [item.event_id for item in timeline.events],
            ["a-to-b-1", "b-to-a", "a-to-b-3"],
        )

    def test_candidate_disappears_when_headline_removal_leaves_one_event(self) -> None:
        normalized = normalize_daily_events(
            [
                event("normal", "success", "a", "b", 1),
                event("headline", "bot_backfire", "a", "b", 2),
            ],
            group_id=GROUP,
        )

        headline = select_headline(normalized)

        self.assertIsNone(select_timeline(normalized, headline))

    def test_personal_turn_is_selected_without_mutual_interaction(self) -> None:
        normalized = normalize_daily_events(
            [
                event("one", "success", "a", "b", 1),
                event("two", "success", "a", "c", 2),
                event("three", "escape", "a", "d", 3),
            ],
            group_id=GROUP,
        )

        timeline = select_timeline(normalized, None)

        self.assertEqual(timeline.kind, "personal_turn")  # type: ignore[union-attr]

    def test_reservation_followup_is_selected_before_repeat_target(self) -> None:
        normalized = normalize_daily_events(
            [
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    1,
                    reservation_id="r1",
                    participant_ids=["a", "c"],
                ),
                event("after", "success", "a", "b", 2),
            ],
            group_id=GROUP,
        )

        timeline = select_timeline(normalized, None)

        self.assertEqual(timeline.kind, "reservation_followup")  # type: ignore[union-attr]

    def test_interaction_before_reservation_is_not_followup(self) -> None:
        normalized = normalize_daily_events(
            [
                event("before", "success", "a", "b", 1),
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    2,
                    reservation_id="r1",
                    participant_ids=["a", "c"],
                ),
            ],
            group_id=GROUP,
        )

        timeline = select_timeline(normalized, None)

        self.assertEqual(timeline.kind, "repeat_target")  # type: ignore[union-attr]

    def test_participant_interaction_is_not_reservation_followup(self) -> None:
        normalized = normalize_daily_events(
            [
                event(
                    "reservation",
                    "escape",
                    "a",
                    "b",
                    1,
                    reservation_id="r1",
                    participant_ids=["a", "c"],
                ),
                event("participant", "success", "c", "b", 2),
            ],
            group_id=GROUP,
        )

        self.assertIsNone(select_timeline(normalized, None))

    def test_repeat_target_is_fallback_timeline_kind(self) -> None:
        normalized = normalize_daily_events(
            [
                event("one", "success", "a", "b", 1),
                event("two", "success", "a", "b", 2),
            ],
            group_id=GROUP,
        )

        timeline = select_timeline(normalized, None)

        self.assertEqual(timeline.kind, "repeat_target")  # type: ignore[union-attr]


class DailyReportRankingTests(unittest.TestCase):
    def test_missing_cloud_profile_fields_hide_ex_and_catalog_rankings(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a", "b": "pig-b"},
            raw_events=[event("success", "success", "a", "b", 8)],
            user_profiles={
                "a": DailyUserReportProfile(user_id="a", display_name="甲"),
                "b": DailyUserReportProfile(user_id="b", display_name="乙"),
            },
        )

        kinds = {ranking.kind for ranking in report.rankings}
        self.assertEqual(kinds, {"roast_success"})

    def test_rankings_use_stable_ties_and_hide_reservation_success(self) -> None:
        participants = ("a", "b", "c", "d")
        rolls = {user_id: f"pig-{user_id}" for user_id in participants}
        events = normalize_daily_events(
            [
                event("a1", "success", "a", "x", 1),
                event("a2", "success", "a", "y", 4),
                event("b1", "success", "b", "x", 2),
                event("b2", "success", "b", "y", 3),
                event("reservation", "success", "c", "z", 5, reservation_id="r1"),
            ],
            group_id=GROUP,
        )
        profiles = {
            "a": DailyUserReportProfile(
                "a", "甲", "pig-a", "甲猪", 5, "a.png", f"{DATE}T02:00:00+08:00", 20
            ),
            "b": DailyUserReportProfile(
                "b", "乙", "pig-b", "乙猪", 5, "b.png", f"{DATE}T01:00:00+08:00", 18
            ),
            "c": DailyUserReportProfile(
                "c", "丙", "pig-c", "丙猪", 3, "c.png", f"{DATE}T03:00:00+08:00", 20
            ),
            "d": DailyUserReportProfile(
                "d",
                "丁",
                "pig-d",
                "丁猪",
                2,
                "d.png",
                f"{DATE}T04:00:00+08:00",
                5,
            ),
        }

        rankings = build_rankings(participants, rolls, events, profiles, {})
        by_kind = {ranking.kind: ranking for ranking in rankings}

        ex_entries = by_kind["expert_level"].entries
        self.assertEqual([(item.user_id, item.rank) for item in ex_entries], [("b", 1), ("a", 1), ("c", 3)])
        success_entries = by_kind["roast_success"].entries
        self.assertEqual([item.user_id for item in success_entries], ["b", "a"])
        self.assertNotIn("c", [item.user_id for item in success_entries])
        catalog_entries = by_kind["catalog"].entries
        self.assertEqual([(item.user_id, item.rank) for item in catalog_entries], [("a", 1), ("c", 1), ("b", 3)])

    def test_catalog_uses_recent_pig_then_fallback_stamp(self) -> None:
        profiles = {
            "a": DailyUserReportProfile(
                "a",
                "甲",
                catalog_count=8,
                recent_pig_id="recent-pig",
                recent_pig_name="最近猪",
                recent_image_name="recent.png",
            ),
            "b": DailyUserReportProfile("b", "乙", catalog_count=7),
        }

        rankings = build_rankings(("a", "b"), {}, (), profiles, {})
        catalog = next(item for item in rankings if item.kind == "catalog")

        self.assertEqual(catalog.entries[0].pig_id, "recent-pig")
        self.assertFalse(catalog.entries[0].uses_fallback_pig_stamp)
        self.assertTrue(catalog.entries[1].uses_fallback_pig_stamp)


class DailyReportAssemblyTests(unittest.TestCase):
    def test_protections_are_passed_through_without_derivation(self) -> None:
        protection = ProtectionReportItem(
            user_id="b",
            display_name="乙",
            expires_at="2026-08-29T23:59:59+08:00",
        )

        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"a": "pig-a"},
            raw_events=[event("success", "success", "a", "b", 8)],
            protections=[protection],
        )

        self.assertEqual(report.protections, (protection,))
        self.assertNotEqual(report.protections[0].user_id, report.headline)


if __name__ == "__main__":
    unittest.main()
