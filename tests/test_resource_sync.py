from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

# 复用现有测试的 NoneBot 初始化，只在测试临时目录内创建插件数据。
from test_ex_variants import Config, RollPigResourceManager, catalog_module, resource_module


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _image_bytes(color: str, image_format: str = "PNG") -> bytes:
    """生成可解码小图；PNG 禁用压缩，便于验证相同大小的内容替换。"""

    output = BytesIO()
    with Image.new("RGB", (16, 16), color) as image:
        image.save(output, format=image_format, compress_level=0)
    return output.getvalue()


class ResourceSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.active = self.cache / "active"
        self.url = "https://resources.test/public/manifest.json"
        self.requests: list[str] = []
        self.responses: dict[str, bytes] = {}
        self.failed_urls: set[str] = set()
        self.pigs = [
            {"id": "pig", "name": "猪", "description": "基础描述", "analysis": "基础分析"},
            {"id": "second-pig", "name": "另一只猪", "description": "第二只", "analysis": "分析"},
        ]
        self.files = {
            "pig.json": _json_bytes(self.pigs), "pig_rules.json": _json_bytes({"food_pigs": ["second-pig"]}),
            "pig_ex_variants.json": _json_bytes({"schema_version": 1, "pigs": {"pig": {"levels": {"2": {"image": "pig_ex2.png", "description": "EX2 文案"}}}}}),
            "images/pig.png": _image_bytes("pink"), "images/second-pig.png": _image_bytes("green"),
            "images/pig_ex2.png": _image_bytes("blue"),
        }
        self.config = Config(rollpig_resource_manifest_url=self.url)
        self.constants = patch.multiple(resource_module, **{
            "CACHE_ROOT": self.cache, "ACTIVE_RESOURCE_DIR": self.active,
            "ACTIVE_IMAGE_DIR": self.active / "images", "STATE_FILE": self.cache / "state.json",
            "PRIVATE_RESOURCE_DIR": self.cache / "private_active", "PRIVATE_STATE_FILE": self.cache / "private_state.json",
            "PRIVATE_RESOURCE_ROOT": self.cache / "overlays", "plugin_config": self.config,
        })
        self.constants.start()
        self.addCleanup(self.constants.stop)
        original_client = httpx.AsyncClient

        async def respond(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            if url in self.failed_urls:
                return httpx.Response(503)
            if url not in self.responses:
                return httpx.Response(404)
            return httpx.Response(200, content=self.responses[url])

        self.transport = httpx.MockTransport(respond)
        self.client_patch = patch.object(resource_module.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=self.transport, **kwargs))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.manager = RollPigResourceManager()
        self.publish()

    def publish(self, version: str = "2026-10-10.1", *, files: dict[str, bytes] | None = None, url: str | None = None, overlay: bool = False) -> dict:
        """每次发布完整清单；服务器可以保留旧实体，客户端仍必须按新清单撤下。"""

        files = self.files if files is None else files
        url = url or self.url

        def meta(path: str) -> dict:
            content = files[path]
            return {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}

        manifest = {"schema_version": 1, "resource_version": version, "pig_json": meta("pig.json"), "images": [], "optional_files": {}}
        if overlay:
            manifest.update(overlay=True, allow_override=True)
        for path in files:
            if path in ("pig_rules.json", "pig_ex_variants.json", "pig_overrides.json"):
                manifest["optional_files"][Path(path).stem] = meta(path)
            elif path.startswith("images/"):
                filename = Path(path).name
                if "_ex" in Path(path).stem:
                    pig_id, level = Path(path).stem.rsplit("_ex", 1)
                    manifest.setdefault("variant_images", []).append({"pig_id": pig_id, "level": int(level), "filename": filename, **meta(path)})
                else:
                    manifest["images"].append({"id": Path(path).stem, "filename": filename, **meta(path)})
            self.responses[url.rsplit("/", 1)[0] + "/" + path] = files[path]
        self.responses[url] = _json_bytes(manifest)
        return manifest

    def body_requests(self) -> list[str]:
        return [url.rsplit("/public/", 1)[-1] for url in self.requests if not url.endswith("manifest.json")]

    async def initial_sync(self) -> None:
        result = await self.manager.sync_from_remote()
        self.assertTrue(result.updated)
        self.requests.clear()

    # ================================ 更新准确性与下载请求 ================================ #
    async def test_unchanged_automatic_and_manual_only_fetch_manifest(self) -> None:
        await self.initial_sync()
        for force in (False, True):
            with self.subTest(force=force), patch.object(self.manager, "_new_staging_dir", wraps=self.manager._new_staging_dir) as staging:
                self.requests.clear()
                result = await self.manager.sync_from_remote(force=force)
                self.assertTrue(result.skipped)
                self.assertEqual(self.requests, [self.url])
                staging.assert_not_called()

    async def test_same_version_same_size_image_replacement_only_downloads_changed_image(self) -> None:
        await self.initial_sync()
        old_revision = self.manager.resource_revision
        new_image = _image_bytes("red")
        self.assertEqual(len(new_image), len(self.files["images/pig.png"]))
        self.files["images/pig.png"] = new_image
        self.publish()
        result = await self.manager.sync_from_remote()
        self.assertTrue(result.updated)
        self.assertEqual(self.body_requests(), ["images/pig.png"])
        self.assertEqual((self.active / "images/pig.png").read_bytes(), new_image)
        self.assertNotEqual(old_revision, self.manager.resource_revision)
        self.assertIn("替换 1", result.message)
        self.assertIn("复用 5", result.message)

    async def test_version_only_change_reuses_all_files(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        result = await self.manager.sync_from_remote()
        self.assertTrue(result.updated)
        self.assertEqual(self.body_requests(), [])
        self.assertEqual(self.manager.resource_version, "2026-10-10.2")
        self.assertIn("复用 6", result.message)

    async def test_add_pig_only_downloads_json_and_new_image(self) -> None:
        await self.initial_sync()
        self.pigs.append({"id": "new-pig", "name": "新猪"})
        self.files["pig.json"] = _json_bytes(self.pigs)
        self.files["images/new-pig.png"] = _image_bytes("yellow")
        self.publish()
        await self.manager.sync_from_remote()
        self.assertCountEqual(self.body_requests(), ["pig.json", "images/new-pig.png"])
        self.assertIn("new-pig", self.manager.pig_map)

    async def test_delete_pig_rules_and_ex_files_removes_disk_and_memory(self) -> None:
        await self.initial_sync()
        self.files["pig.json"] = _json_bytes(self.pigs[:1])
        for path in ("images/second-pig.png", "pig_rules.json", "pig_ex_variants.json", "images/pig_ex2.png"):
            del self.files[path]
        self.publish()
        result = await self.manager.sync_from_remote()
        self.assertEqual(self.body_requests(), ["pig.json"])
        self.assertNotIn("second-pig", self.manager.pig_map)
        self.assertEqual(self.manager.food_pig_ids, set())
        self.assertEqual(self.manager.ex_variants, {})
        self.assertFalse((self.active / "images/second-pig.png").exists())
        self.assertFalse((self.active / "pig_rules.json").exists())
        self.assertFalse((self.active / "pig_ex_variants.json").exists())
        self.assertIn("移除 4", result.message)
        self.assertTrue((self.cache / "previous/images/second-pig.png").exists())

    async def test_png_replaced_by_gif_or_jpeg_leaves_no_old_format(self) -> None:
        await self.initial_sync()
        for suffix, image_format in ((".gif", "GIF"), (".jpg", "JPEG")):
            with self.subTest(suffix=suffix):
                self.requests.clear()
                for path in list(self.files):
                    if path.startswith("images/pig."):
                        del self.files[path]
                new_path = "images/pig" + suffix
                self.files[new_path] = _image_bytes("orange", image_format)
                self.publish()
                await self.manager.sync_from_remote()
                self.assertEqual(self.body_requests(), [new_path])
                self.assertEqual(self.manager.find_image_file("pig").name, "pig" + suffix)
                self.assertEqual(sorted(path.name for path in (self.active / "images").glob("pig.*")), ["pig" + suffix])

    async def test_missing_and_same_size_corrupt_files_repair_individually(self) -> None:
        await self.initial_sync()
        (self.active / "images/pig.png").unlink()
        corrupt = self.active / "images/second-pig.png"
        corrupt.write_bytes(b"x" * corrupt.stat().st_size)
        await self.manager.sync_from_remote()
        self.assertCountEqual(self.body_requests(), ["images/pig.png", "images/second-pig.png"])
        self.assertEqual(corrupt.read_bytes(), self.files["images/second-pig.png"])

    async def test_extra_old_file_is_removed_without_body_download(self) -> None:
        await self.initial_sync()
        extra = self.active / "images/old-unlisted.png"
        extra.write_bytes(_image_bytes("black"))
        result = await self.manager.sync_from_remote()
        self.assertFalse(extra.exists())
        self.assertEqual(self.body_requests(), [])
        self.assertIn("移除 1", result.message)

    async def test_legacy_cache_without_manifest_state_is_reused_and_migrated(self) -> None:
        self.active.mkdir(parents=True)
        for path, data in self.files.items():
            target = self.active / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.cache / "state.json").write_bytes(_json_bytes({"resource_version": "2026-10-10.1"}))
        result = await self.manager.sync_from_remote()
        self.assertTrue(result.updated)
        self.assertEqual(self.body_requests(), [])
        self.assertIn("复用 6", result.message)
        state = json.loads((self.cache / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["files"]), 6)
        self.assertTrue((self.active / ".sync.json").is_file())

    async def test_source_changed_with_same_version_and_different_content_updates(self) -> None:
        await self.initial_sync()
        new_url = "https://resources.test/other/manifest.json"
        self.files["images/pig.png"] = _image_bytes("red")
        self.publish(url=new_url)
        self.config.rollpig_resource_manifest_url = new_url
        result = await self.manager.sync_from_remote()
        self.assertTrue(result.updated)
        self.assertEqual([url for url in self.requests if not url.endswith("manifest.json")], [new_url.rsplit("/", 1)[0] + "/images/pig.png"])

    async def test_explicit_full_sync_downloads_every_file(self) -> None:
        await self.initial_sync()
        result = await self.manager.sync_from_remote(force=True, redownload=True)
        self.assertTrue(result.updated)
        self.assertCountEqual(self.body_requests(), list(self.files))
        self.assertIn("复用 0", result.message)

    # ================================ 无效更新与失败回退 ================================ #
    async def test_failure_never_deletes_old_files_or_advances_state(self) -> None:
        await self.initial_sync()
        before = (self.cache / "state.json").read_bytes()
        del self.files["images/second-pig.png"]
        self.files["pig.json"] = _json_bytes(self.pigs[:1])
        self.files["images/pig.png"] = _image_bytes("red")
        self.publish("2026-10-10.2")
        self.failed_urls.add(self.url.rsplit("/", 1)[0] + "/images/pig.png")
        with self.assertRaises(httpx.HTTPStatusError):
            await self.manager.sync_from_remote()
        self.assertEqual((self.cache / "state.json").read_bytes(), before)
        self.assertIn("second-pig", self.manager.pig_map)
        self.assertTrue((self.active / "images/second-pig.png").is_file())
        self.assertEqual(list(self.cache.glob(".incoming*")), [])

    async def test_removed_referenced_ex_image_rejects_update_before_image_download(self) -> None:
        await self.initial_sync()
        del self.files["images/pig_ex2.png"]
        self.publish()
        with self.assertRaisesRegex(ValueError, "引用的图片未写入 manifest"):
            await self.manager.sync_from_remote()
        self.assertTrue((self.active / "images/pig_ex2.png").is_file())
        self.assertEqual(self.body_requests(), [])

    async def test_manifest_matching_undecodable_image_is_not_activated(self) -> None:
        await self.initial_sync()
        self.files["images/pig.png"] = b"not an image"
        self.publish()
        with self.assertRaisesRegex(ValueError, "无法解码"):
            await self.manager.sync_from_remote()
        self.assertNotEqual((self.active / "images/pig.png").read_bytes(), b"not an image")

    async def test_state_commit_failure_restores_active_and_previous(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        await self.manager.sync_from_remote()
        old_state = (self.cache / "state.json").read_bytes()
        old_image = (self.active / "images/pig.png").read_bytes()
        old_previous = (self.cache / "previous/.sync.json").read_bytes()
        self.files["images/pig.png"] = _image_bytes("red")
        self.publish("2026-10-10.3")
        original_replace = Path.replace

        def fail_state(path: Path, target: Path) -> Path:
            if Path(target) == self.cache / "state.json":
                raise OSError("state write failed")
            return original_replace(path, target)

        with patch.object(Path, "replace", new=fail_state), self.assertRaisesRegex(OSError, "state write failed"):
            await self.manager.sync_from_remote()
        self.assertEqual((self.cache / "state.json").read_bytes(), old_state)
        self.assertEqual((self.active / "images/pig.png").read_bytes(), old_image)
        self.assertEqual((self.cache / "previous/.sync.json").read_bytes(), old_previous)

    async def test_reused_files_count_toward_actual_package_budget(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        limit = sum(len(data) for data in self.files.values()) - 1
        with patch.object(resource_module, "RESOURCE_PACKAGE_MAX_SIZE", limit), self.assertRaisesRegex(ValueError, "总大小超过上限"):
            await self.manager.sync_from_remote()
        self.assertEqual(self.manager.resource_version, "2026-10-10.1")

    async def test_legacy_entry_missing_hash_is_downloaded_not_reused(self) -> None:
        await self.initial_sync()
        manifest = self.publish()
        del manifest["images"][0]["sha256"]
        self.responses[self.url] = _json_bytes(manifest)
        await self.manager.sync_from_remote()
        self.assertEqual(self.body_requests(), ["images/pig.png"])

    async def test_case_insensitive_target_collision_rejected_before_body_requests(self) -> None:
        manifest = self.publish()
        meta = dict(manifest["images"][0])
        meta.update(filename="pig.PNG", path="images/pig.PNG")
        manifest["images"].append(meta)
        self.responses[self.url] = _json_bytes(manifest)
        with self.assertRaisesRegex(ValueError, "路径冲突|重复声明"):
            await self.manager.sync_from_remote()
        self.assertEqual(self.body_requests(), [])

    async def test_changed_local_file_after_check_is_fetched_from_origin(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        original_copy = self.manager._copy_local_file_to_temp_sync

        def mutate_before_copy(source: Path, target: Path, max_size: int):
            if source == self.active / "images/pig.png":
                source.write_bytes(_image_bytes("red"))
            return original_copy(source, target, max_size)

        with patch.object(self.manager, "_copy_local_file_to_temp_sync", side_effect=mutate_before_copy):
            await self.manager.sync_from_remote()
        self.assertEqual(self.body_requests(), ["images/pig.png"])
        self.assertEqual((self.active / "images/pig.png").read_bytes(), self.files["images/pig.png"])

    # ================================ 并发与任务取消 ================================ #
    async def test_cancel_waits_for_local_copy_before_removing_staging(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        started = threading.Event()
        release = threading.Event()
        original_copy = self.manager._copy_local_file_to_temp_sync

        def delayed_copy(source: Path, target: Path, max_size: int):
            started.set()
            release.wait(timeout=5)
            return original_copy(source, target, max_size)

        with patch.object(self.manager, "_copy_local_file_to_temp_sync", side_effect=delayed_copy):
            task = asyncio.create_task(self.manager.sync_from_remote())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.cache.glob(".incoming*")), [])
        self.assertEqual(self.manager.resource_version, "2026-10-10.1")

    async def test_cancel_waits_for_candidate_validation_to_close_files(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        started = threading.Event()
        release = threading.Event()
        original_validate = self.manager._validate_pack

        def delayed_validate(resource_dir, manifest, source):
            started.set()
            release.wait(timeout=5)
            return original_validate(resource_dir, manifest, source)

        with patch.object(self.manager, "_validate_pack", side_effect=delayed_validate):
            task = asyncio.create_task(self.manager.sync_from_remote())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(list(self.cache.glob(".incoming*")), [])
        self.assertEqual(self.manager.resource_version, "2026-10-10.1")

    async def test_cancel_after_public_commit_publishes_completed_snapshot(self) -> None:
        await self.initial_sync()
        self.files["pig.json"] = _json_bytes(self.pigs[:1])
        del self.files["images/second-pig.png"]
        self.publish("2026-10-10.2")
        started = asyncio.Event()

        async def wait_private(**kwargs):
            started.set()
            await asyncio.Event().wait()

        previous_pigs = list(resource_module.PIG_LIST)
        with patch.object(resource_module, "pig_resource_manager", self.manager), patch.object(self.manager, "_sync_private_overlays_from_remote_unlocked", side_effect=wait_private):
            task = asyncio.create_task(resource_module.sync_rollpig_resources(force=True))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.manager.resource_version, "2026-10-10.2")
                self.assertEqual([pig["id"] for pig in resource_module.PIG_LIST], ["pig"])
                self.assertFalse((self.active / "images/second-pig.png").exists())
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                resource_module.PIG_LIST[:] = previous_pigs

    async def test_overlay_configuration_failure_does_not_hide_public_success(self) -> None:
        await self.initial_sync()
        self.publish("2026-10-10.2")
        with patch.object(self.manager, "_sync_private_overlays_from_remote_unlocked", side_effect=ValueError("bad overlay configuration")):
            public, private = await self.manager.sync_all()
        self.assertTrue(public.updated)
        self.assertIn("bad overlay configuration", private.message)
        self.assertEqual(self.manager.resource_version, "2026-10-10.2")

    async def test_image_download_concurrency_is_bounded_and_greater_than_one(self) -> None:
        original = self.manager._download_file_to_temp
        running = 0
        peak = 0

        async def delayed(client, url, target, *, max_size):
            nonlocal running, peak
            if "/images/" not in url:
                return await original(client, url, target, max_size=max_size)
            running += 1
            peak = max(peak, running)
            try:
                await asyncio.sleep(0.02)
                return await original(client, url, target, max_size=max_size)
            finally:
                running -= 1

        with patch.object(self.manager, "_download_file_to_temp", side_effect=delayed):
            await self.manager.sync_from_remote()
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, resource_module.RESOURCE_DOWNLOAD_CONCURRENCY)

    # ================================ Overlay 与渲染缓存 ================================ #
    def setup_overlay(self, files: dict[str, bytes]) -> tuple[str, Path]:
        """配置独立 Overlay 和空 GIF 包，核对真实加载顺序而不是只测下载函数。"""

        gif_url = "https://resources.test/gif/manifest.json"
        official = patch.object(resource_module, "OFFICIAL_GIF_RESOURCE_MANIFEST_URL", gif_url)
        official.start()
        self.addCleanup(official.stop)
        self.publish("gif-test", files={"pig.json": b"[]"}, url=gif_url, overlay=True)
        url = "https://resources.test/custom/manifest.json"
        self.config.rollpig_private_resource_manifests = [{"name": "custom", "manifest_url": url}]
        self.publish("private-1", files=files, url=url, overlay=True)
        return url, self.cache / "overlays/custom/active"

    async def test_overlay_same_version_image_change_and_corruption_are_incremental(self) -> None:
        await self.initial_sync()
        files = {"pig.json": _json_bytes([{"id": "custom-pig", "name": "私有猪"}]), "images/custom-pig.png": _image_bytes("orange")}
        url, active = self.setup_overlay(files)
        await self.manager.sync_private_from_remote()
        self.requests.clear()
        files["images/custom-pig.png"] = _image_bytes("red")
        self.publish("private-1", files=files, url=url, overlay=True)
        await self.manager.sync_private_from_remote()
        self.assertEqual([request for request in self.requests if not request.endswith("manifest.json")], [url.rsplit("/", 1)[0] + "/images/custom-pig.png"])
        self.assertEqual((active / "images/custom-pig.png").read_bytes(), files["images/custom-pig.png"])
        self.requests.clear()
        (active / "images/custom-pig.png").unlink()
        await self.manager.sync_private_from_remote()
        self.assertTrue((active / "images/custom-pig.png").is_file())
        self.assertEqual(len([request for request in self.requests if not request.endswith("manifest.json")]), 1)

    async def test_removing_overlay_rules_override_and_image_restores_lower_layer(self) -> None:
        await self.initial_sync()
        files = {
            "pig.json": b"[]", "pig_overrides.json": _json_bytes([{"id": "pig", "description": "私有描述"}]),
            "pig_rules.json": _json_bytes({"food_pigs": ["pig"]}), "images/pig.png": _image_bytes("red"),
        }
        url, active = self.setup_overlay(files)
        await self.manager.sync_private_from_remote()
        self.assertEqual(self.manager.pig_map["pig"]["description"], "私有描述")
        self.assertIn("pig", self.manager.variant_blocked_pig_ids)
        self.assertIn("pig", self.manager.food_pig_ids)
        self.requests.clear()
        self.publish("private-2", files={"pig.json": b"[]"}, url=url, overlay=True)
        await self.manager.sync_private_from_remote()
        self.assertEqual(self.manager.pig_map["pig"]["description"], "基础描述")
        self.assertNotIn("pig", self.manager.variant_blocked_pig_ids)
        self.assertNotIn("pig", self.manager.food_pig_ids)
        self.assertEqual(self.manager.find_image_file("pig"), self.active / "images/pig.png")
        self.assertFalse((active / "pig_overrides.json").exists())
        self.assertEqual([request for request in self.requests if not request.endswith("manifest.json")], [])

    async def test_invalid_overlay_update_keeps_previous_valid_overlay(self) -> None:
        await self.initial_sync()
        files = {"pig.json": b"[]", "pig_overrides.json": _json_bytes([{"id": "pig", "description": "有效覆盖"}])}
        url, active = self.setup_overlay(files)
        await self.manager.sync_private_from_remote()
        before = (active / "pig_overrides.json").read_bytes()
        files["pig_overrides.json"] = _json_bytes([{"id": "missing-pig", "description": "无效覆盖"}])
        self.publish("private-2", files=files, url=url, overlay=True)
        result = await self.manager.sync_private_from_remote()
        self.assertIn("不存在", result.message)
        self.assertEqual((active / "pig_overrides.json").read_bytes(), before)
        self.assertEqual(self.manager.pig_map["pig"]["description"], "有效覆盖")

    async def test_public_failure_does_not_block_valid_overlay_update(self) -> None:
        await self.initial_sync()
        files = {"pig.json": _json_bytes([{"id": "custom-pig", "name": "私有猪"}]), "images/custom-pig.png": _image_bytes("orange")}
        self.setup_overlay(files)
        self.failed_urls.add(self.url)
        public, private = await self.manager.sync_all()
        self.assertFalse(public.updated)
        self.assertIn("同步失败", public.message)
        self.assertTrue(private.updated)
        self.assertIn("custom-pig", self.manager.pig_map)

    async def test_base_removal_skips_now_incompatible_cached_overlay(self) -> None:
        await self.initial_sync()
        files = {"pig.json": b"[]", "pig_overrides.json": _json_bytes([{"id": "pig", "description": "覆盖猪"}])}
        _, overlay_active = self.setup_overlay(files)
        await self.manager.sync_private_from_remote()
        self.assertIn("pig", self.manager.pig_map)
        self.files["pig.json"] = _json_bytes(self.pigs[1:])
        for path in ("images/pig.png", "pig_ex_variants.json", "images/pig_ex2.png"):
            del self.files[path]
        self.publish("2026-10-10.2")
        public, private = await self.manager.sync_all()
        self.assertTrue(public.updated)
        self.assertIn("不存在", private.message)
        self.assertNotIn("pig", self.manager.pig_map)
        self.assertEqual(self.manager.variant_blocked_pig_ids, set())
        self.assertTrue((overlay_active / "pig_overrides.json").is_file())

    async def test_same_version_image_update_changes_catalog_result_cache_key(self) -> None:
        await self.initial_sync()
        stats = SimpleNamespace(**{key: 1 for key in (
            "page", "unlocked", "total", "progress_percent", "max_level", "maxed_count", "recent_new_count",
            "checkin_streak", "roasted_7d", "next_milestone", "pages",
        )})
        favorite = SimpleNamespace(name="猪", image_path=self.active / "images/pig.png", fallback_image_path=None, level=0, copies=1)
        data = SimpleNamespace(stats=stats, favorite=favorite, user_name="测试用户", cards=[])
        snapshot = SimpleNamespace(recent_rolls=(), roasted_7d=0)
        with patch.object(catalog_module, "pig_resource_manager", self.manager):
            before = catalog_module._build_cache_key(data, snapshot)
            self.files["images/pig.png"] = _image_bytes("red")
            self.publish()
            await self.manager.sync_from_remote()
            after = catalog_module._build_cache_key(data, snapshot)
        self.assertNotEqual(before, after)

    async def test_full_sync_option_reaches_images_and_shared_library(self) -> None:
        from nonebot_plugin_rollpig_plus import roast_manager as roast_module

        result = resource_module.ResourceSyncResult(False, True)
        images = AsyncMock(return_value=(result, result))
        shared = AsyncMock(return_value=SimpleNamespace(message="共享文案：已是最新"))
        with patch.object(resource_module.pig_resource_manager, "sync_all", images), patch.object(roast_module.roast_manager, "sync_shared_library", shared):
            await resource_module.sync_rollpig_resources(force=True, redownload=True)
        images.assert_awaited_once_with(force=True, wait_if_busy=True, redownload=True)
        shared.assert_awaited_once_with(force=True, wait_if_busy=True, redownload=True)

    async def test_both_sync_commands_require_superuser_and_pass_correct_mode(self) -> None:
        from nonebot_plugin_rollpig_plus.handlers import roll as roll_module

        for redownload in (False, True):
            matcher = SimpleNamespace(finish=AsyncMock())
            event = SimpleNamespace(user_id=123, message_id=456)
            with patch.object(roll_module, "is_superuser_user", return_value=False), patch.object(roll_module, "sync_rollpig_resources", AsyncMock()) as sync:
                await roll_module._sync_resources_for_event(matcher, event, redownload=redownload)
                sync.assert_not_awaited()
                self.assertIn("只有超级用户", str(matcher.finish.await_args.args[0]))
            with patch.object(roll_module, "is_superuser_user", return_value=True), patch.object(roll_module, "sync_rollpig_resources", AsyncMock(return_value="已更新")) as sync:
                await roll_module._sync_resources_for_event(matcher, event, redownload=redownload)
                sync.assert_awaited_once_with(force=True, redownload=redownload)


if __name__ == "__main__":
    unittest.main()
