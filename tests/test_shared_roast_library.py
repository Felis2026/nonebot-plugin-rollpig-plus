from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot
from nonebot.plugin import get_plugin

# 单元测试只需要 NoneBot 的插件生命周期，不应额外依赖 FastAPI Driver。
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")
if get_plugin("nonebot_plugin_rollpig_plus") is None:
    if nonebot.load_plugin("nonebot_plugin_rollpig_plus") is None:
        raise RuntimeError("failed to load nonebot_plugin_rollpig_plus for tests")

config_module = import_module("nonebot_plugin_rollpig_plus.config")
roast_manager_module = import_module("nonebot_plugin_rollpig_plus.roast_manager")
Config = config_module.Config
SOURCE_LOCAL = roast_manager_module.SOURCE_LOCAL
SOURCE_SHARED = roast_manager_module.SOURCE_SHARED
RoastManager = roast_manager_module.RoastManager
_roast_text_identity = roast_manager_module._roast_text_identity


class SharedRoastLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.remote_dir = self.root / "remote"
        self.remote_dir.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _write_remote(
        self,
        version: str,
        library: dict,
        *,
        sha256_override: str | None = None,
        statistics_override: dict | None = None,
    ) -> Path:
        library_path = self.remote_dir / "roast_library.json"
        self._write_json(library_path, library)
        library_bytes = library_path.read_bytes()
        statistics = {
            "origin_count": len(library),
            "pair_count": sum(len(targets) for targets in library.values()),
            "text_count": sum(
                len(texts)
                for targets in library.values()
                for texts in targets.values()
            ),
        }
        manifest = {
            "schema_version": 1,
            "package_type": "roast_library",
            "resource_version": version,
            "min_plugin_version": "0.10.0",
            "roast_library": {
                "path": "roast_library.json",
                "size": len(library_bytes),
                "sha256": sha256_override or hashlib.sha256(library_bytes).hexdigest(),
            },
            "statistics": statistics_override or statistics,
            "created_at": "2026-07-29T00:00:00+00:00",
        }
        manifest_path = self.remote_dir / "manifest.json"
        self._write_json(manifest_path, manifest)
        return manifest_path

    def _make_manager(
        self,
        manifest_path: Path | None,
        *,
        library: dict | None = None,
        sources: dict | None = None,
    ) -> RoastManager:
        library_file = self.root / "data" / "roast_library.json"
        sources_file = self.root / "data" / "roast_library_sources.json"
        if library is not None:
            self._write_json(library_file, library)
        if sources is not None:
            self._write_json(sources_file, sources)
        config = Config(
            rollpig_resource_sync_enabled=True,
            rollpig_roast_library_manifest_url=(
                str(manifest_path) if manifest_path is not None else ""
            ),
        )
        return RoastManager(
            config,
            library_file=library_file,
            sources_file=sources_file,
            cache_dir=self.root / "cache",
        )

    def test_explicit_null_manifest_url_overrides_json_config(self) -> None:
        config_path = self.root / "rollpig_config.json"
        self._write_json(
            config_path,
            {
                "rollpig": {
                    "rollpig_roast_library_manifest_url": (
                        "https://example.invalid/roasts/manifest.json"
                    ),
                    "rollpig_proxy": "http://127.0.0.1:7890",
                }
            },
        )

        with patch.dict(os.environ, {"ROLLPIG_CONFIG_FILE": str(config_path)}):
            inherited = Config()
            disabled = Config(
                rollpig_roast_library_manifest_url=None,
                rollpig_proxy=None,
            )

        self.assertEqual(
            inherited.rollpig_roast_library_manifest_url,
            "https://example.invalid/roasts/manifest.json",
        )
        self.assertIsNone(disabled.rollpig_roast_library_manifest_url)
        # 其他默认即为 None 的旧字段继续把 None 视为“未提供”，避免改变既有优先级。
        self.assertEqual(disabled.rollpig_proxy, "http://127.0.0.1:7890")

    async def test_first_sync_preserves_existing_local_and_marks_collision(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared", "same"]}},
        )
        manager = self._make_manager(
            manifest,
            library={"pig": {"food": ["local", "same"]}},
        )

        result = await manager.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertEqual(result.added, 1)
        self.assertEqual(manager.library["pig"]["food"], ["shared", "same", "local"])
        flags = manager._source_flags[("pig", "food")]
        self.assertEqual(flags[_roast_text_identity("shared")], SOURCE_SHARED)
        self.assertEqual(
            flags[_roast_text_identity("same")],
            SOURCE_LOCAL | SOURCE_SHARED,
        )
        self.assertEqual(flags[_roast_text_identity("local")], SOURCE_LOCAL)
        persisted_sources = json.loads(manager.sources_file.read_text(encoding="utf-8"))
        persisted_hashes = persisted_sources["entries"]["pig"]["food"]
        self.assertNotIn(_roast_text_identity("local"), persisted_hashes)
        self.assertIn(_roast_text_identity("shared"), persisted_hashes)

    async def test_remote_removal_deletes_only_shared_only_text(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared", "same"]}},
        )
        manager = self._make_manager(
            manifest,
            library={"pig": {"food": ["local", "same"]}},
        )
        await manager.sync_shared_library()
        self._write_remote(
            "roasts-2026-07-29.2",
            {"pig": {"food": ["new-shared"]}},
        )

        result = await manager.sync_shared_library()

        self.assertEqual(result.added, 1)
        self.assertEqual(result.removed, 1)
        self.assertEqual(manager.library["pig"]["food"], ["new-shared", "same", "local"])

    async def test_explicit_empty_url_removes_pure_shared_and_keeps_local(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared", "same"]}},
        )
        manager = self._make_manager(
            manifest,
            library={"pig": {"food": ["local", "same"]}},
        )
        await manager.sync_shared_library()
        manager.config.rollpig_roast_library_manifest_url = ""

        result = await manager.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertEqual(result.removed, 1)
        self.assertEqual(manager.library["pig"]["food"], ["same", "local"])
        self.assertTrue(
            all(
                not flags & SOURCE_SHARED
                for flags_by_hash in manager._source_flags.values()
                for flags in flags_by_hash.values()
            )
        )

    async def test_reenable_same_version_reapplies_snapshot(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()
        manager.config.rollpig_roast_library_manifest_url = ""
        await manager.sync_shared_library()
        manager.config.rollpig_roast_library_manifest_url = str(manifest)

        result = await manager.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertEqual(manager.library["pig"]["food"], ["shared"])

    async def test_unchanged_url_version_sha_and_applied_state_skips(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()

        result = await manager.sync_shared_library()

        self.assertTrue(result.skipped)
        self.assertFalse(result.updated)

    async def test_invalid_state_counts_force_safe_reapply(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()
        state = json.loads(manager.state_file.read_text(encoding="utf-8"))
        state["text_count"] = "broken"
        state["local_count"] = "broken"
        self._write_json(manager.state_file, state)

        result = await manager.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertFalse(result.skipped)
        self.assertEqual(manager.library["pig"]["food"], ["shared"])

    async def test_sha_failure_does_not_modify_local_library(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
            sha256_override="0" * 64,
        )
        manager = self._make_manager(
            manifest,
            library={"pig": {"food": ["local"]}},
        )
        before = manager.file.read_bytes()

        with self.assertRaisesRegex(ValueError, "SHA256"):
            await manager.sync_shared_library()

        self.assertEqual(manager.library, {"pig": {"food": ["local"]}})
        self.assertEqual(manager.file.read_bytes(), before)
        self.assertFalse(manager.sources_file.exists())

    async def test_statistics_failure_does_not_modify_local_library(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
            statistics_override={"origin_count": 99, "pair_count": 1, "text_count": 1},
        )
        manager = self._make_manager(
            manifest,
            library={"pig": {"food": ["local"]}},
        )

        with self.assertRaisesRegex(ValueError, "statistics"):
            await manager.sync_shared_library()

        self.assertEqual(manager.library, {"pig": {"food": ["local"]}})

    async def test_corrupt_source_index_conservatively_marks_current_text_local(self) -> None:
        library_file = self.root / "data" / "roast_library.json"
        sources_file = self.root / "data" / "roast_library_sources.json"
        self._write_json(library_file, {"pig": {"food": ["local"]}})
        sources_file.parent.mkdir(parents=True, exist_ok=True)
        sources_file.write_text("{broken", encoding="utf-8")

        manager = RoastManager(
            Config(rollpig_roast_library_manifest_url=""),
            library_file=library_file,
            sources_file=sources_file,
            cache_dir=self.root / "cache",
        )

        flags = manager._source_flags[("pig", "food")]
        self.assertEqual(flags[_roast_text_identity("local")], SOURCE_LOCAL)
        self.assertTrue(list(sources_file.parent.glob("roast_library_sources.corrupt-*.json")))

    async def test_missing_source_index_forces_reapply_even_when_state_is_current(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()
        manager.sources_file.unlink()

        restarted = RoastManager(
            Config(
                rollpig_resource_sync_enabled=True,
                rollpig_roast_library_manifest_url=str(manifest),
            ),
            library_file=manager.file,
            sources_file=manager.sources_file,
            cache_dir=manager.cache_dir,
        )
        result = await restarted.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertFalse(result.skipped)
        flags = restarted._source_flags[("pig", "food")]
        self.assertEqual(
            flags[_roast_text_identity("shared")],
            SOURCE_LOCAL | SOURCE_SHARED,
        )

    async def test_missing_local_library_forces_shared_snapshot_rebuild(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()
        manager.file.unlink()

        restarted = RoastManager(
            Config(
                rollpig_resource_sync_enabled=True,
                rollpig_roast_library_manifest_url=str(manifest),
            ),
            library_file=manager.file,
            sources_file=manager.sources_file,
            cache_dir=manager.cache_dir,
        )
        result = await restarted.sync_shared_library()

        self.assertTrue(result.updated)
        self.assertEqual(restarted.library["pig"]["food"], ["shared"])
        self.assertEqual(
            restarted._source_flags[("pig", "food")][_roast_text_identity("shared")],
            SOURCE_SHARED,
        )

    async def test_shared_texts_do_not_count_toward_five_local_ai_target(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["s1", "s2", "s3", "s4", "s5"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()
        manager.ai_ready = True
        manager._call_ai = AsyncMock(return_value="new-local")

        with patch("nonebot_plugin_rollpig_plus.roast_manager.random.random", return_value=0.99):
            result = await manager.get_roast_text(
                {"id": "pig", "name": "猪", "description": "测试"},
                {"id": "food", "name": "熟食"},
            )

        self.assertEqual(result, "new-local")
        manager._call_ai.assert_awaited_once()
        flags = manager._source_flags[("pig", "food")]
        self.assertEqual(flags[_roast_text_identity("new-local")], SOURCE_LOCAL)

    async def test_ai_duplicate_of_shared_text_becomes_dual_source(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["same"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()

        await manager._save_new_text("pig", "food", "same")

        flags = manager._source_flags[("pig", "food")]
        self.assertEqual(
            flags[_roast_text_identity("same")],
            SOURCE_LOCAL | SOURCE_SHARED,
        )
        self.assertEqual(manager.library["pig"]["food"], ["same"])

    async def test_ai_normalized_duplicate_does_not_append_second_copy(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["same"]}},
        )
        manager = self._make_manager(manifest)
        await manager.sync_shared_library()

        await manager._save_new_text("pig", "food", " “same” ")

        flags = manager._source_flags[("pig", "food")]
        self.assertEqual(
            flags[_roast_text_identity("same")],
            SOURCE_LOCAL | SOURCE_SHARED,
        )
        self.assertEqual(manager.library["pig"]["food"], ["same"])

    async def test_concurrent_syncs_are_serialized_and_second_one_skips(self) -> None:
        manifest = self._write_remote(
            "roasts-2026-07-29.1",
            {"pig": {"food": ["shared"]}},
        )
        manager = self._make_manager(manifest)

        first, second = await asyncio.gather(
            manager.sync_shared_library(),
            manager.sync_shared_library(),
        )

        self.assertEqual(sum(result.updated for result in (first, second)), 1)
        self.assertEqual(sum(result.skipped for result in (first, second)), 1)


if __name__ == "__main__":
    unittest.main()
