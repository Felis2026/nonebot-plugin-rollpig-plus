from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.plugin import get_plugin
from PIL import Image

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

from nonebot_plugin_rollpig_plus import catalog_renderer as catalog_module
from nonebot_plugin_rollpig_plus import helpers as helpers_module
from nonebot_plugin_rollpig_plus import resource_manager as resource_module
from nonebot_plugin_rollpig_plus import roll_flow as roll_flow_module
from nonebot_plugin_rollpig_plus.card_renderer import PigCardRenderResult
from nonebot_plugin_rollpig_plus.config import Config
from nonebot_plugin_rollpig_plus.resource_manager import RollPigResourceManager
from nonebot_plugin_rollpig_plus.store.models import (
    CatalogSnapshot,
    DailyRollResult,
    DrawState,
    PigProgress,
    expert_level_from_copies,
)


class ExVariantFixtureMixin:
    root: Path

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _write_png(self, path: Path, color: tuple[int, int, int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGBA", (32, 32), color) as image:
            image.save(path, format="PNG")

    def _create_pack(
        self,
        *,
        missing_ex5: bool = False,
        variants: dict | None = None,
    ) -> Path:
        pack_dir = self.root / "pack"
        self._write_json(
            pack_dir / "pig.json",
            [
                {
                    "id": "pig",
                    "name": "猪",
                    "description": "基础描述",
                    "analysis": "基础分析",
                }
            ],
        )
        self._write_json(pack_dir / "pig_rules.json", {})
        self._write_png(pack_dir / "images" / "pig.png", (255, 180, 200, 255))
        self._write_png(pack_dir / "images" / "pig_ex2.png", (100, 180, 255, 255))
        if not missing_ex5:
            self._write_png(pack_dir / "images" / "pig_ex5.png", (255, 210, 80, 255))
        self._write_json(
            pack_dir / "pig_ex_variants.json",
            variants
            or {
                "schema_version": 1,
                "pigs": {
                    "pig": {
                        "levels": {
                            "2": {
                                "image": "pig_ex2.png",
                                "description": "EX2 描述",
                                "analysis": "EX2 分析",
                            },
                            "5": {
                                "image": "pig_ex5.png",
                                "description": "EX5 描述",
                                "analysis": "EX5 分析",
                            },
                        }
                    }
                },
            },
        )
        return pack_dir

    def _manager_from_pack(self, pack_dir: Path) -> RollPigResourceManager:
        manager = RollPigResourceManager()
        manager._load_from_dir(pack_dir, resource_version="test-variants")
        return manager

    def _file_meta(self, path: Path, relative_path: str) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "path": relative_path,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _write_manifest(self, pack_dir: Path, *, version: str, bad_variant_hash: bool = False) -> Path:
        pig_json = pack_dir / "pig.json"
        variants_json = pack_dir / "pig_ex_variants.json"
        base_image = pack_dir / "images" / "pig.png"
        variant_items = []
        variants = json.loads(variants_json.read_text(encoding="utf-8"))
        for pig_id, pig_variants in variants["pigs"].items():
            for raw_level, variant in pig_variants["levels"].items():
                filename = variant.get("image")
                if filename is None:
                    continue
                level = int(raw_level)
                meta = self._file_meta(pack_dir / "images" / filename, f"images/{filename}")
                variant_items.append(
                    {
                        "pig_id": pig_id,
                        "level": level,
                        "filename": filename,
                        **meta,
                    }
                )
        if bad_variant_hash:
            variant_items[-1]["sha256"] = "0" * 64

        manifest = {
            "schema_version": 1,
            "resource_version": version,
            "min_plugin_version": "0.2.0",
            "pig_json": self._file_meta(pig_json, "pig.json"),
            "images": [
                {
                    "id": "pig",
                    "filename": "pig.png",
                    **self._file_meta(base_image, "images/pig.png"),
                }
            ],
            "optional_files": {
                "pig_ex_variants": self._file_meta(variants_json, "pig_ex_variants.json")
            },
            "variant_images": variant_items,
            "created_at": "2026-08-02T00:00:00+00:00",
        }
        manifest_path = pack_dir / "manifest.json"
        self._write_json(manifest_path, manifest)
        return manifest_path


class ExVariantModelTests(unittest.TestCase):
    def test_expert_level_is_clamped(self) -> None:
        expected = {
            -10: 0,
            0: 0,
            1: 0,
            2: 1,
            3: 2,
            5: 4,
            6: 5,
            999: 5,
        }
        for copies, level in expected.items():
            with self.subTest(copies=copies):
                self.assertEqual(expert_level_from_copies(copies), level)

    def test_progress_and_draw_state_share_the_same_level_rule(self) -> None:
        progress = PigProgress(copies=6)
        state = DrawState(pig_ids=["pig"], progress={"pig": progress})

        self.assertEqual(progress.expert_level, 5)
        self.assertEqual(state.expert_level_of("pig"), 5)
        self.assertEqual(state.expert_level_of("missing"), 0)


class ExVariantResourceTests(ExVariantFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sparse_variants_resolve_without_mutating_base_data(self) -> None:
        manager = self._manager_from_pack(self._create_pack())
        base = manager.pig_map["pig"]
        original = dict(base)

        levels = {
            level: manager.resolve_pig_appearance(base, level)
            for level in range(6)
        }

        self.assertEqual(levels[0].applied_level, 0)
        self.assertEqual(levels[1].applied_level, 0)
        self.assertEqual(levels[2].applied_level, 2)
        self.assertEqual(levels[3].applied_level, 2)
        self.assertEqual(levels[4].applied_level, 2)
        self.assertEqual(levels[5].applied_level, 5)
        self.assertEqual(levels[3].pig_data["description"], "EX2 描述")
        self.assertEqual(levels[5].pig_data["analysis"], "EX5 分析")
        self.assertEqual(base, original)
        self.assertIsNot(levels[5].pig_data, base)

    def test_variant_fields_inherit_independently(self) -> None:
        manager = self._manager_from_pack(
            self._create_pack(
                variants={
                    "schema_version": 1,
                    "pigs": {
                        "pig": {
                            "levels": {
                                "1": {"analysis": "EX1 分析"},
                                "2": {"image": "pig_ex2.png"},
                                "4": {"description": "EX4 描述"},
                                "5": {"analysis": "EX5 分析"},
                            }
                        }
                    },
                }
            )
        )

        appearance = manager.resolve_pig_appearance(manager.pig_map["pig"], 5)

        self.assertEqual(appearance.applied_level, 5)
        self.assertEqual(appearance.image_path.name, "pig_ex2.png")
        self.assertEqual(appearance.pig_data["description"], "EX4 描述")
        self.assertEqual(appearance.pig_data["analysis"], "EX5 分析")
        self.assertEqual(manager.available_variant_levels("pig"), (1, 2, 4, 5))

    def test_empty_variant_is_rejected(self) -> None:
        pack_dir = self._create_pack(
            variants={
                "schema_version": 1,
                "pigs": {"pig": {"levels": {"2": {}}}},
            }
        )
        manager = RollPigResourceManager()

        with self.assertRaisesRegex(ValueError, "至少需要 image、description、analysis 之一"):
            manager._load_ex_variants(
                pack_dir / "pig_ex_variants.json",
                pig_ids={"pig"},
                image_dir=pack_dir / "images",
                strict=True,
            )

    def test_missing_active_variant_is_skipped_but_lower_variant_survives(self) -> None:
        manager = self._manager_from_pack(self._create_pack(missing_ex5=True))

        appearance = manager.resolve_pig_appearance(manager.pig_map["pig"], 5)

        self.assertEqual(appearance.applied_level, 2)
        self.assertEqual(manager.available_variant_levels("pig"), (2,))

    def test_strict_parser_rejects_path_traversal(self) -> None:
        pack_dir = self._create_pack(
            variants={
                "schema_version": 1,
                "pigs": {
                    "pig": {
                        "levels": {
                            "2": {
                                "image": "../pig_ex2.png",
                            }
                        }
                    }
                },
            }
        )
        manager = RollPigResourceManager()

        with self.assertRaisesRegex(ValueError, "文件名不能包含路径"):
            manager._load_ex_variants(
                pack_dir / "pig_ex_variants.json",
                pig_ids={"pig"},
                image_dir=pack_dir / "images",
                strict=True,
            )

    def test_private_override_disables_public_variant_for_same_pig(self) -> None:
        manager = self._manager_from_pack(self._create_pack())
        overlay_dir = self.root / "overlay"
        self._write_json(overlay_dir / "pig.json", [])
        self._write_json(overlay_dir / "pig_rules.json", {})
        self._write_json(
            overlay_dir / "pig_overrides.json",
            [{"id": "pig", "description": "私有覆盖"}],
        )
        (overlay_dir / "images").mkdir(parents=True)

        manager._apply_private_overlay(overlay_dir, resource_version="private-test")
        appearance = manager.resolve_pig_appearance(manager.pig_map["pig"], 5)

        self.assertEqual(appearance.applied_level, 0)
        self.assertEqual(appearance.pig_data["description"], "私有覆盖")
        self.assertEqual(manager.available_variant_levels("pig"), ())
        self.assertEqual(manager.variant_change_fields("pig", 2), frozenset())

    def test_unlock_prompt_only_uses_real_variant_thresholds(self) -> None:
        manager = self._manager_from_pack(self._create_pack())

        self.assertEqual(manager.newly_unlocked_variant_levels("pig", 1, 2), (2,))
        self.assertEqual(manager.newly_unlocked_variant_levels("pig", 2, 4), ())
        self.assertEqual(manager.newly_unlocked_variant_levels("pig", 4, 5), (5,))

    def test_catalog_uses_resolved_variant_image(self) -> None:
        manager = self._manager_from_pack(self._create_pack())
        snapshot = CatalogSnapshot(
            draw_state=DrawState(
                pig_ids=["pig"],
                progress={"pig": PigProgress(copies=6, first_obtained_at="2026-08-01T00:00:00")},
            ),
            recent_rolls={},
        )

        with patch.object(catalog_module, "pig_resource_manager", manager):
            data = catalog_module._build_catalog_data(user_name="测试用户", snapshot=snapshot, page=1)

        self.assertEqual(data.cards[0].level, 5)
        self.assertEqual(data.cards[0].image_path.name, "pig_ex5.png")
        self.assertEqual(data.favorite.image_path.name, "pig_ex5.png")


class ExVariantFlowTests(ExVariantFixtureMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = self._manager_from_pack(self._create_pack())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_daily_view_reads_progress_but_roast_path_can_skip_it(self) -> None:
        fake_store = SimpleNamespace(
            get_daily_roll=AsyncMock(return_value="pig"),
            get_draw_state=AsyncMock(
                return_value=DrawState(
                    pig_ids=["pig"],
                    progress={"pig": PigProgress(copies=6)},
                )
            ),
            mark_group_roll_seen=AsyncMock(),
        )

        with (
            patch.object(roll_flow_module, "store", fake_store),
            patch.object(roll_flow_module, "pig_resource_manager", self.manager),
        ):
            daily = await roll_flow_module.resolve_daily_pig("user", "group", include_progress=True)
            roast = await roll_flow_module.resolve_daily_pig("user", "group", include_progress=False)

        self.assertEqual(daily.ex_level, 5)
        self.assertIsNone(roast.ex_level)
        self.assertEqual(fake_store.get_draw_state.await_count, 1)

    def test_growth_text_uses_matching_variant_change_pool(self) -> None:
        result = DailyRollResult(
            pig_id="pig",
            created=True,
            is_new_pig=False,
            previous_copies=2,
            copies=3,
        )
        cases = {
            "image": {"image": "pig_ex2.png"},
            "text": {"description": "EX2 描述"},
            "image_text": {"image": "pig_ex2.png", "analysis": "EX2 分析"},
        }

        for pool_key, variant in cases.items():
            with self.subTest(pool_key=pool_key):
                manager = self._manager_from_pack(
                    self._create_pack(
                        variants={
                            "schema_version": 1,
                            "pigs": {"pig": {"levels": {"2": variant}}},
                        }
                    )
                )
                with (
                    patch.object(roll_flow_module, "pig_resource_manager", manager),
                    patch.object(
                        roll_flow_module.random,
                        "choice",
                        side_effect=lambda choices: choices[0],
                    ) as choice_mock,
                ):
                    text = roll_flow_module.build_roll_growth_text(result, manager.pig_map["pig"])

                self.assertIs(
                    choice_mock.call_args.args[0],
                    roll_flow_module.DAILY_ROLL_VARIANT_LEVEL_UP_TEXTS[pool_key],
                )
                self.assertIn("EX Lv. 1 → 2", text)
                self.assertNotIn("\n", text)
                self.assertNotIn("已解锁", text)

    def test_growth_text_keeps_regular_level_up_pool_without_variant(self) -> None:
        result = DailyRollResult(
            pig_id="pig",
            created=True,
            is_new_pig=False,
            previous_copies=3,
            copies=4,
        )
        with (
            patch.object(roll_flow_module, "pig_resource_manager", self.manager),
            patch.object(
                roll_flow_module.random,
                "choice",
                side_effect=lambda choices: choices[0],
            ) as choice_mock,
        ):
            text = roll_flow_module.build_roll_growth_text(result, self.manager.pig_map["pig"])

        self.assertIs(choice_mock.call_args.args[0], roll_flow_module.DAILY_ROLL_DUPLICATE_LEVEL_UP_TEXTS)
        self.assertIn("EX Lv. 2 → 3", text)

    async def test_variant_render_failure_retries_base_card(self) -> None:
        result = PigCardRenderResult(
            data=b"image",
            image_format="PNG",
            renderer="pillow",
            analysis_font_size=28,
            analysis_lines=2,
            emoji_enabled=True,
        )
        fake_matcher = SimpleNamespace(finish=AsyncMock())
        fake_event = SimpleNamespace(message_id=123)
        render_mock = AsyncMock(side_effect=[RuntimeError("bad variant"), result])

        with (
            patch.object(helpers_module, "pig_resource_manager", self.manager),
            patch.object(helpers_module, "render_pig_card_image", render_mock),
            patch.object(helpers_module, "log_perf"),
        ):
            await helpers_module.send_rendered_pig(
                fake_matcher,
                fake_event,
                self.manager.pig_map["pig"],
                ex_level=5,
            )

        self.assertEqual(render_mock.await_count, 2)
        self.assertEqual(render_mock.await_args_list[0].args[1].name, "pig_ex5.png")
        self.assertEqual(render_mock.await_args_list[1].args[1].name, "pig.png")
        fake_matcher.finish.assert_awaited_once()


class ExVariantSyncTests(ExVariantFixtureMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_local_sync_activates_complete_variants_and_preserves_old_active_on_hash_failure(self) -> None:
        pack_dir = self._create_pack()
        manifest_path = self._write_manifest(pack_dir, version="2026-08-02.1")
        cache_root = self.root / "cache"
        config = Config(
            rollpig_resource_sync_enabled=True,
            rollpig_resource_manifest_url=str(manifest_path),
        )
        manager = RollPigResourceManager()

        patched_constants = {
            "CACHE_ROOT": cache_root,
            "ACTIVE_RESOURCE_DIR": cache_root / "active",
            "ACTIVE_IMAGE_DIR": cache_root / "active" / "images",
            "STATE_FILE": cache_root / "state.json",
            "PRIVATE_RESOURCE_DIR": cache_root / "private_active",
            "PRIVATE_STATE_FILE": cache_root / "private_state.json",
            "PRIVATE_RESOURCE_ROOT": cache_root / "private_overlays",
            "plugin_config": config,
        }
        with patch.multiple(resource_module, **patched_constants):
            first = await manager.sync_from_remote(force=True)
            self.assertTrue(first.updated)
            self.assertEqual(manager.resolve_pig_appearance(manager.pig_map["pig"], 5).applied_level, 5)
            active_variants = (cache_root / "active" / "pig_ex_variants.json").read_bytes()

            self._write_manifest(pack_dir, version="2026-08-02.2", bad_variant_hash=True)
            with self.assertRaisesRegex(ValueError, "sha256 校验失败"):
                await manager.sync_from_remote(force=True)

            self.assertEqual((cache_root / "active" / "pig_ex_variants.json").read_bytes(), active_variants)
            state = json.loads((cache_root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["resource_version"], "2026-08-02.1")

    async def test_local_sync_accepts_text_only_variant_without_manifest_image(self) -> None:
        pack_dir = self._create_pack(
            variants={
                "schema_version": 1,
                "pigs": {"pig": {"levels": {"5": {"description": "EX5 纯文案"}}}},
            }
        )
        manifest_path = self._write_manifest(pack_dir, version="2026-08-02.1")
        cache_root = self.root / "cache"
        config = Config(
            rollpig_resource_sync_enabled=True,
            rollpig_resource_manifest_url=str(manifest_path),
        )
        manager = RollPigResourceManager()
        patched_constants = {
            "CACHE_ROOT": cache_root,
            "ACTIVE_RESOURCE_DIR": cache_root / "active",
            "ACTIVE_IMAGE_DIR": cache_root / "active" / "images",
            "STATE_FILE": cache_root / "state.json",
            "PRIVATE_RESOURCE_DIR": cache_root / "private_active",
            "PRIVATE_STATE_FILE": cache_root / "private_state.json",
            "PRIVATE_RESOURCE_ROOT": cache_root / "private_overlays",
            "plugin_config": config,
        }

        with patch.multiple(resource_module, **patched_constants):
            result = await manager.sync_from_remote(force=True)
            appearance = manager.resolve_pig_appearance(manager.pig_map["pig"], 5)

        self.assertTrue(result.updated)
        self.assertEqual(appearance.applied_level, 5)
        self.assertEqual(appearance.image_path.name, "pig.png")
        self.assertEqual(appearance.pig_data["description"], "EX5 纯文案")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["variant_images"], [])

    async def test_same_version_from_old_client_downloads_missing_variants_once(self) -> None:
        pack_dir = self._create_pack()
        manifest_path = self._write_manifest(pack_dir, version="2026-08-02.1")
        cache_root = self.root / "cache"
        active_dir = cache_root / "active"
        (active_dir / "images").mkdir(parents=True)
        (active_dir / "pig.json").write_bytes((pack_dir / "pig.json").read_bytes())
        (active_dir / "pig_rules.json").write_bytes((pack_dir / "pig_rules.json").read_bytes())
        (active_dir / "images" / "pig.png").write_bytes((pack_dir / "images" / "pig.png").read_bytes())
        self._write_json(cache_root / "state.json", {"resource_version": "2026-08-02.1"})
        config = Config(
            rollpig_resource_sync_enabled=True,
            rollpig_resource_manifest_url=str(manifest_path),
        )
        manager = RollPigResourceManager()

        patched_constants = {
            "CACHE_ROOT": cache_root,
            "ACTIVE_RESOURCE_DIR": active_dir,
            "ACTIVE_IMAGE_DIR": active_dir / "images",
            "STATE_FILE": cache_root / "state.json",
            "PRIVATE_RESOURCE_DIR": cache_root / "private_active",
            "PRIVATE_STATE_FILE": cache_root / "private_state.json",
            "PRIVATE_RESOURCE_ROOT": cache_root / "private_overlays",
            "plugin_config": config,
        }
        with patch.multiple(resource_module, **patched_constants):
            first = await manager.sync_from_remote(force=False)
            second = await manager.sync_from_remote(force=False)

        self.assertTrue(first.updated)
        self.assertTrue((active_dir / "pig_ex_variants.json").is_file())
        self.assertTrue((active_dir / "images" / "pig_ex5.png").is_file())
        self.assertTrue(second.skipped)

    async def test_variant_image_cannot_overwrite_base_image_path(self) -> None:
        pack_dir = self._create_pack()
        manifest_path = self._write_manifest(pack_dir, version="2026-08-02.1")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        variant_image = pack_dir / "images" / "pig_ex2.png"
        manifest["images"].append(
            {
                "id": "pig-ex2",
                "filename": "pig_ex2.png",
                **self._file_meta(variant_image, "images/pig_ex2.png"),
            }
        )
        self._write_json(manifest_path, manifest)
        cache_root = self.root / "cache"
        config = Config(
            rollpig_resource_sync_enabled=True,
            rollpig_resource_manifest_url=str(manifest_path),
        )
        manager = RollPigResourceManager()

        patched_constants = {
            "CACHE_ROOT": cache_root,
            "ACTIVE_RESOURCE_DIR": cache_root / "active",
            "ACTIVE_IMAGE_DIR": cache_root / "active" / "images",
            "STATE_FILE": cache_root / "state.json",
            "PRIVATE_RESOURCE_DIR": cache_root / "private_active",
            "PRIVATE_STATE_FILE": cache_root / "private_state.json",
            "PRIVATE_RESOURCE_ROOT": cache_root / "private_overlays",
            "plugin_config": config,
        }
        with patch.multiple(resource_module, **patched_constants):
            with self.assertRaisesRegex(ValueError, "差分图片与基础图片路径冲突"):
                await manager.sync_from_remote(force=True)


if __name__ == "__main__":
    unittest.main()
