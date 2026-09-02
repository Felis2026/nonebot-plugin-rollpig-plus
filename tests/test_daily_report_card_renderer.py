from __future__ import annotations

import json
import unittest
from dataclasses import replace
from io import BytesIO
from unittest.mock import patch

import nonebot
from nonebot.plugin import get_plugin
from PIL import Image
from PIL import ImageDraw

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
)
from nonebot_plugin_rollpig_plus.daily_report_card_renderer import (
    DAILY_REPORT_CARD_RESOURCE_DIR,
    BLUE,
    INK,
    RED,
    Observation,
    RichSpan,
    build_daily_report_card_data,
    create_daily_report_card_renderer,
    get_noto_emoji_source,
    render_daily_report_card,
)


DATE = "2026-08-26"
GROUP = "100"


def _event(
    event_id: str,
    event_type: str,
    attacker: str,
    target: str,
    minute: int,
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
        "created_at": f"{DATE}T12:{minute:02d}:00+08:00",
        **extra,
    }


def _full_report():
    group_rolls = {
        "u1": "pig",
        "u2": "pig",
        "u3": "pig",
        "u4": "coder-pig",
        "u5": "chef-pig",
        "u6": "human",
        "u7": "magic-pig",
        "u8": "pig-cat",
        "u9": "sleepy-pig",
        "u10": "laborer-pig",
    }
    events = [
        _event(
            "headline",
            "escape",
            "u1",
            "u2",
            1,
            reservation_id="r1",
            participant_ids=["u3", "u4", "u5", "u6", "u7"],
            participant_count=5,
        ),
        _event("track-1", "success", "u3", "u4", 2),
        _event("track-2", "success", "u3", "u4", 3),
        _event("track-3", "escape", "u3", "u4", 4),
        _event("track-4", "backfire", "u3", "u4", 5),
    ]
    profiles = {
        user_id: DailyUserReportProfile(
            user_id=user_id,
            display_name=f"群友{index}",
            daily_ex_level=index % 6,
            daily_pig_name=f"测试猪{index}",
            catalog_count=180 - index,
            catalog_achieved_at=f"{DATE}T10:{index:02d}:00+08:00",
        )
        for index, user_id in enumerate(group_rolls, start=1)
    }
    return build_daily_report(
        date_str=DATE,
        group_id=GROUP,
        group_rolls=group_rolls,
        raw_events=events,
        user_profiles=profiles,
        protections=(
            ProtectionReportItem(
                user_id="u4",
                display_name="群友4",
                expires_at="2026-08-27T23:59:00+08:00",
            ),
        ),
    )


class DailyReportCardRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.renderer = create_daily_report_card_renderer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.renderer.close()

    def test_full_report_maps_every_dynamic_section(self) -> None:
        data = build_daily_report_card_data(_full_report())

        self.assertEqual(len(data.stats), 3)
        self.assertIsNotNone(data.observation)
        self.assertIsNotNone(data.headline)
        self.assertEqual(len(data.events), 3)
        self.assertEqual(len(data.rankings), 3)
        self.assertIsNotNone(data.coupon)
        self.assertEqual(data.volume, "VOL.2026-0826   ★")
        self.assertEqual(
            [(item.label, item.unit) for item in data.stats],
            [("小猪数量", "头"), ("普通烤猪", "次"), ("预约烤猪", "场")],
        )
        self.assertEqual(
            [ranking.title for ranking in data.rankings],
            ["严选好猪", "烧烤狂人", "养猪大户"],
        )
        assert data.coupon is not None
        self.assertEqual(data.coupon.roast_count, 2)

    def test_header_top_row_uses_shared_baseline_and_design_anchors(self) -> None:
        data = build_daily_report_card_data(_full_report())
        header_box = self.renderer.build_layout(data).boxes["header"]
        canvas = Image.new("RGBA", (2048, 560), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        with patch.object(self.renderer, "_baseline_text") as draw_baseline:
            self.renderer._draw_header(canvas, draw, data, header_box, 2)

        top_row_calls = draw_baseline.call_args_list[:3]
        self.assertEqual([call.args[3] for call in top_row_calls], [header_box.y + 27] * 3)
        self.assertEqual(
            [call.args[2] for call in top_row_calls],
            [
                header_box.x + 15,
                header_box.x + header_box.width / 2,
                header_box.right - 38,
            ],
        )
        self.assertEqual(top_row_calls[1].args[8], "center")
        self.assertEqual(top_row_calls[2].args[8], "right")

    def test_empty_optional_sections_collapse_and_two_stats_share_width(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={},
            raw_events=[_event("one", "success", "u1", "u2", 1)],
        )
        compact = build_daily_report_card_data(report)
        full = build_daily_report_card_data(_full_report())
        compact_layout = self.renderer.build_layout(compact)
        full_layout = self.renderer.build_layout(full)

        self.assertEqual(len(compact.stats), 2)
        self.assertNotIn("main", compact_layout.boxes)
        self.assertNotIn("ranking", compact_layout.boxes)
        self.assertNotIn("coupon", compact_layout.boxes)
        self.assertEqual(len(compact_layout.stat_cells), 2)
        self.assertAlmostEqual(compact_layout.stat_cells[0].width, 467.0)
        self.assertLess(compact_layout.height, full_layout.height)

    def test_tied_ranks_are_preserved_in_card_rows(self) -> None:
        report = _full_report()
        first = report.rankings[0]
        tied_entries = (
            replace(first.entries[0], rank=1, score=5),
            replace(first.entries[1], rank=1, score=5),
            replace(first.entries[2], rank=3, score=4),
        )
        report = replace(
            report,
            rankings=(replace(first, entries=tied_entries), *report.rankings[1:]),
        )

        data = build_daily_report_card_data(report)

        self.assertEqual([entry.rank for entry in data.rankings[0].entries], [1, 1, 3])

    def test_ranking_titles_use_the_fixed_serif_subset(self) -> None:
        data = build_daily_report_card_data(_full_report())
        box = self.renderer.build_layout(data).boxes["ranking"]
        canvas = Image.new("RGBA", (2048, 1600), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            with patch.object(self.renderer, "_ink_centered_text") as draw_centered:
                self.renderer._draw_ranking(canvas, draw, data, box, 2)
            title_calls = {
                call.args[1]: call.args[7]
                for call in draw_centered.call_args_list
                if call.args[1] in {"严选好猪", "烧烤狂人", "养猪大户"}
            }
            self.assertEqual(
                title_calls,
                {"严选好猪": "display", "烧烤狂人": "display", "养猪大户": "display"},
            )
        finally:
            canvas.close()

    def test_reservation_count_already_includes_owner(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"u1": "pig", "u2": "coder-pig"},
            raw_events=[
                _event(
                    "reservation",
                    "success",
                    "u1",
                    "u2",
                    1,
                    reservation_id="r1",
                    participant_ids=["u1", "u3"],
                    participant_count=2,
                )
            ],
        )

        data = build_daily_report_card_data(report)

        self.assertIsNotNone(data.headline)
        assert data.headline is not None
        text = "".join(span.text for span in data.headline.spans)
        self.assertTrue(
            any(
                marker in text
                for marker in (
                    "召集 1 名群友",
                    "另外 1 名群友",
                    "2 人预约队伍",
                    "2 把烤叉",
                )
            )
        )
        self.assertFalse(
            any(
                marker in text
                for marker in (
                    "召集 2 名群友",
                    "另外 2 名群友",
                    "3 人预约队伍",
                    "3 把烤叉",
                )
            )
        )
        self.assertIn("2 人参与", data.headline.tags)
        self.assertNotIn("3 人参与", data.headline.tags)

    def test_headline_tags_use_rollpig_wording(self) -> None:
        cases = (
            ("backfire", "", 5, ("普通烤猪", "当场翻车")),
            ("self_roast", "", 5, ("主动自烤", "自觉上桌")),
            ("bot_backfire", "", 1, ("挑战 Bot", "当场伏诛")),
            ("escape", "reservation", 6, ("大型预约", "6 人参与", "集体扑空")),
            ("backfire", "reservation", 6, ("大型预约", "6 人参与", "意外走火")),
            ("reserved_special", "food", 6, ("预约烤猪", "6 人围观", "早已熟透")),
        )
        for event_type, reservation_kind, count, expected_tags in cases:
            with self.subTest(event_type=event_type, reservation_kind=reservation_kind):
                raw_events = [
                    _event(
                        f"event-{index}",
                        event_type,
                        "u1",
                        "u1" if event_type == "self_roast" else "u2",
                        index + 1,
                        reservation_id="r1" if reservation_kind else "",
                        participant_count=count if reservation_kind else 0,
                        special_reason=(
                            reservation_kind if event_type == "reserved_special" else ""
                        ),
                    )
                    for index in range(count if not reservation_kind else 1)
                ]
                report = build_daily_report(
                    date_str=DATE,
                    group_id=GROUP,
                    group_rolls={"u1": "pig", "u2": "coder-pig"},
                    raw_events=raw_events,
                )
                data = build_daily_report_card_data(report)
                assert data.headline is not None
                self.assertEqual(data.headline.tags, expected_tags)

    def test_same_snapshot_uses_stable_copy_for_every_dynamic_section(self) -> None:
        report = _full_report()

        first = build_daily_report_card_data(report)
        second = build_daily_report_card_data(report)

        self.assertEqual(first.observation, second.observation)
        self.assertEqual(first.headline, second.headline)
        self.assertEqual(first.event_intro, second.event_intro)
        self.assertEqual(first.events, second.events)

    def test_event_identity_can_select_multiple_headline_templates(self) -> None:
        rendered_texts: set[str] = set()
        for index in range(32):
            report = build_daily_report(
                date_str=DATE,
                group_id=GROUP,
                group_rolls={"u1": "pig", "u2": "coder-pig"},
                raw_events=[
                    _event(
                        f"reservation-{index}",
                        "success",
                        "u1",
                        "u2",
                        1,
                        reservation_id=f"r-{index}",
                        participant_ids=["u1", "u3"],
                        participant_count=2,
                    )
                ],
            )
            data = build_daily_report_card_data(report)
            assert data.headline is not None
            rendered_texts.add("".join(span.text for span in data.headline.spans))

        self.assertGreaterEqual(len(rendered_texts), 2)

    def test_single_person_reservation_never_mentions_zero_teammates(self) -> None:
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"u1": "pig", "u2": "coder-pig"},
            raw_events=[
                _event(
                    "solo-reservation",
                    "success",
                    "u1",
                    "u2",
                    1,
                    reservation_id="solo-r",
                    participant_count=1,
                )
            ],
        )

        data = build_daily_report_card_data(report)

        assert data.headline is not None
        text = "".join(span.text for span in data.headline.spans)
        self.assertTrue(
            any(
                marker in text
                for marker in ("独自守着", "独自", "单人预约", "1 把烤叉")
            )
        )
        self.assertNotIn("0 名群友", text)
        self.assertIn("1 人参与", data.headline.tags)

    def test_all_declared_assets_exist_and_are_decodable(self) -> None:
        manifest_path = DAILY_REPORT_CARD_RESOURCE_DIR / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        declared = {
            name
            for key, names in manifest.items()
            if key not in {"schema_version", "placeholders"}
            for name in names
        }
        for name in sorted(declared):
            with self.subTest(asset=name):
                candidates = [
                    DAILY_REPORT_CARD_RESOURCE_DIR / f"{name}{suffix}"
                    for suffix in (".png", ".jpg", ".jpeg")
                ]
                existing = [path for path in candidates if path.is_file()]
                self.assertEqual(len(existing), 1)
                path = existing[0]
                with Image.open(path) as opened:
                    opened.load()

    def test_dynamic_text_reuses_one_complete_plugin_body_font(self) -> None:
        body_font = DAILY_REPORT_CARD_RESOURCE_DIR.parent / "fonts" / "SourceHanSansSC-Medium.otf"

        self.assertTrue(body_font.is_file())
        self.assertEqual(self.renderer.fonts.body_path, body_font.resolve())
        tokens = self.renderer._tokens(
            (RichSpan("超级加班群友烤到了新小猪", INK),)
        )
        self.assertTrue(tokens)
        self.assertEqual({token.role for token in tokens}, {"body"})

    def test_daily_display_fonts_share_global_plugin_font_directory(self) -> None:
        font_dir = DAILY_REPORT_CARD_RESOURCE_DIR.parent / "fonts"
        font_paths = {
            self.renderer.fonts.display_path,
            self.renderer.fonts.regular_path,
            self.renderer.fonts.black_path,
        }

        self.assertEqual({path.parent for path in font_paths}, {font_dir})
        self.assertTrue(all(path.is_file() for path in font_paths))
        self.assertFalse((DAILY_REPORT_CARD_RESOURCE_DIR / "fonts").exists())

    def test_fixed_serif_subset_covers_all_fixed_daily_report_titles(self) -> None:
        for text in (
            "今日猪圈",
            "猪圈见闻",
            "猪圈名人堂",
            "今日头条",
            "事件追踪",
            "严选好猪",
            "烧烤狂人",
            "养猪大户",
            "次日保护券",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.renderer.fonts.resolve_role(text, "display"), "display")
        self.assertEqual(self.renderer.fonts.resolve_role("未收录固定字𠀀", "display"), "body")

    def test_keep_together_span_is_not_split_at_line_end(self) -> None:
        spans = (
            RichSpan("前面的正文已经占去大半行", INK),
            RichSpan("成功逃脱", RED, True),
        )

        lines = self.renderer._wrap_rich(spans, 29, 260, 2)
        red_tokens = [
            token.text
            for line in lines
            for token in line.tokens
            if token.color == RED
        ]

        self.assertEqual(red_tokens, ["成功逃脱"])

    def test_single_line_fields_are_fitted_to_their_pixel_width(self) -> None:
        cases = (
            ("特别特别特别长的排行榜群友名称", 151, 22),
            ("名字非常非常长的小猪 Lv.5", 151, 19),
        )

        for text, max_width, size in cases:
            with self.subTest(text=text):
                fitted = self.renderer._fit_single_line(text, max_width, size, 2)
                font = self.renderer.fonts.get("body", size, 2)
                self.assertLessEqual(font.getlength(fitted), max_width * 2)

        fitted_name, fitted_size = self.renderer._fit_text_size(
            "保护券上也塞了一个特别长的群友名称",
            328,
            40,
            20,
            2,
        )
        name_font = self.renderer.fonts.get("body", fitted_size, 2)
        self.assertLessEqual(name_font.getlength(fitted_name), 656)

    def test_coupon_uses_full_middle_column_before_truncating_name(self) -> None:
        name = "这是一位昵称特别特别长的群友"

        fitted, size = self.renderer._fit_text_size(name, 328, 40, 20, 2)

        self.assertEqual(fitted, name)
        self.assertGreaterEqual(size, 20)

    def test_long_observation_fits_fixed_panel_and_moves_within_it(self) -> None:
        data = replace(
            build_daily_report_card_data(_full_report()),
            observation=Observation(
                spans=(
                    RichSpan("本群今天抽出了 128 种不同的小猪，许多成员连续多次出现相同形态。", INK),
                    RichSpan("超级加班猪", RED),
                    RichSpan("以 17 次成为当天最常见的小猪，并刷新了本群的单日纪录。", INK),
                )
            ),
        )
        layout = self.renderer.build_layout(data)
        observation_box = layout.boxes["observation"]
        text_width = observation_box.width - 238 - 30

        size, line_height, text_height = self.renderer._fit_rich_block(
            data.observation.spans,
            text_width,
            158,
            29,
            20,
            2,
        )

        self.assertEqual(observation_box.height, 218)
        self.assertLess(size, 29)
        self.assertLessEqual(text_height, 158)
        self.assertGreater(line_height, size)

        overflow_spans = (RichSpan("超长观察正文" * 80, INK),)
        wrapped = self.renderer._wrap_rich(
            overflow_spans,
            20,
            text_width,
            2,
        )
        max_lines = max(1, int(158 // line_height))
        limited = self.renderer._limit_rich_lines(
            wrapped,
            max_lines,
            20,
            text_width,
            2,
        )
        self.assertLessEqual(len(limited), max_lines)
        self.assertTrue(limited[-1].tokens[-1].text.endswith("…"))

    def test_zwj_emoji_is_kept_as_one_truncation_unit(self) -> None:
        family = "👨‍👩‍👧‍👦"

        units = self.renderer._display_units(f"{family}超长昵称")

        self.assertEqual(units[0], family)
        self.assertNotIn("\u200d", units[1:])

    def test_centered_emoji_text_uses_color_emoji_renderer(self) -> None:
        canvas = Image.new("RGBA", (480, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        self.renderer._ink_centered_text(
            draw,
            "👨‍👩‍👧‍👦",
            120,
            45,
            32,
            BLUE,
            2,
            "body",
            "center",
        )

        colored_pixels = {
            pixel[:3]
            for pixel in canvas.getdata()
            if pixel[3] > 0
        }
        self.assertTrue(colored_pixels)
        self.assertTrue(any(color != BLUE for color in colored_pixels))

    def test_rich_text_uses_its_own_emoji_baseline_offset(self) -> None:
        canvas = Image.new("RGBA", (480, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            with patch.object(self.renderer, "_draw_text_with_emoji") as draw_emoji:
                self.renderer._draw_rich(
                    draw,
                    (RichSpan("Felis🐷", INK, True),),
                    self.renderer.build_layout(build_daily_report_card_data(_full_report())).boxes[
                        "stats"
                    ],
                    29,
                    43,
                    2,
                )
            self.assertEqual(draw_emoji.call_args.kwargs["emoji_position_offset_y"], -6)
        finally:
            canvas.close()

    def test_rich_text_splits_mixed_emoji_tokens_before_drawing(self) -> None:
        canvas = Image.new("RGBA", (480, 180), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            with patch.object(self.renderer, "_draw_text_with_emoji") as draw_emoji:
                self.renderer._draw_rich(
                    draw,
                    (RichSpan("Felis🐷", INK, True),),
                    self.renderer.build_layout(build_daily_report_card_data(_full_report())).boxes[
                        "stats"
                    ],
                    29,
                    43,
                    2,
                    emoji_position_offset_y=-24,
                )
            self.assertEqual(
                [call.args[2] for call in draw_emoji.call_args_list],
                ["Felis", "🐷"],
            )
            self.assertTrue(
                all(
                    call.kwargs["emoji_position_offset_y"] == -24
                    for call in draw_emoji.call_args_list
                )
            )
        finally:
            canvas.close()

    def test_headline_uses_a_separate_emoji_baseline_offset(self) -> None:
        data = build_daily_report_card_data(_full_report())
        with patch.object(self.renderer, "_draw_rich") as draw_rich:
            image, _ = self.renderer.render(data, width=512, supersample=1)
        image.close()
        self.assertTrue(
            any(
                call.kwargs.get("emoji_position_offset_y") == -24
                for call in draw_rich.call_args_list
            )
        )

    def test_emoji_nickname_renders_with_existing_emoji_archive(self) -> None:
        self.assertIsNotNone(get_noto_emoji_source())
        report = replace(
            _full_report(),
            protections=(
                ProtectionReportItem(
                    user_id="u4",
                    display_name="群友👨‍👩‍👧‍👦超长昵称",
                    expires_at="2026-08-27T23:59:00+08:00",
                ),
            ),
        )

        image, _ = self.renderer.render(
            build_daily_report_card_data(report),
            width=512,
            supersample=1,
        )

        self.assertEqual(image.mode, "RGB")
        self.assertGreater(image.getbbox()[2], 0)

    def test_event_detail_shrinks_before_truncating(self) -> None:
        detail = "超长昵称再次从预约现场逃脱并带走了烤叉"

        lines, size = self.renderer._fit_wrapped_text_size(detail, 186, 19, 15, 2, 2)
        font = self.renderer.fonts.get("body", size, 2)

        self.assertEqual(len(lines), 2)
        self.assertLess(size, 19)
        self.assertFalse(lines[-1].endswith("…"))
        self.assertTrue(all(font.getlength(line) <= 372 for line in lines))

    def test_event_detail_truncates_only_at_minimum_size(self) -> None:
        detail = "超长昵称群友再次从多人预约烤猪现场成功逃脱并顺手带走了烤叉而且继续绕场三圈"

        lines, size = self.renderer._fit_wrapped_text_size(detail, 186, 19, 15, 2, 2)
        font = self.renderer.fonts.get("body", size, 2)

        self.assertEqual(size, 15)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(font.getlength(line) <= 372 for line in lines))

    def test_event_intro_keeps_full_name_and_shrinks_before_truncating(self) -> None:
        display_name = "群友今天也想吃烤猪"
        report = build_daily_report(
            date_str=DATE,
            group_id=GROUP,
            group_rolls={"u1": "pig", "u2": "coder-pig"},
            raw_events=[
                _event(
                    f"repeat-{index}",
                    "success",
                    "u1",
                    "u2",
                    index + 1,
                    attacker_name=display_name,
                )
                for index in range(3)
            ],
            user_profiles={
                "u1": DailyUserReportProfile(user_id="u1", display_name=display_name),
                "u2": DailyUserReportProfile(user_id="u2", display_name="另一位群友"),
            },
        )
        data = build_daily_report_card_data(report)

        self.assertIn(display_name, data.event_intro)
        self.assertNotIn("群友今…", data.event_intro)
        lines, size, _ = self.renderer._fit_event_intro(data.event_intro, 160, 2)
        self.assertLess(size, 22)
        self.assertFalse(lines[-1].endswith("…"))

    def test_event_intro_truncates_only_after_reaching_minimum_size(self) -> None:
        text = "超长昵称群友今天连续发起很多很多很多很多次烧烤并且每次都围着猪圈跑了好几圈"

        lines, size, _ = self.renderer._fit_event_intro(text, 120, 2)

        self.assertEqual(size, 16)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("…"))

    def test_render_outputs_valid_png(self) -> None:
        image, layout = self.renderer.render(
            build_daily_report_card_data(_full_report()),
            width=512,
            supersample=1,
        )

        self.assertEqual(image.width, 512)
        self.assertEqual(image.height, round(layout.height * 0.5))


class DailyReportAsyncRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_entry_returns_png_bytes(self) -> None:
        result = await render_daily_report_card(
            _full_report(),
            width=512,
            supersample=1,
        )

        self.assertTrue(result.data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(BytesIO(result.data)) as rendered:
            self.assertEqual(rendered.size, (result.width, result.height))


if __name__ == "__main__":
    unittest.main()
