from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from nonebot.log import logger
import nonebot_plugin_localstore as localstore

from .config import Config, plugin_config

PACKAGE_DIR = Path(__file__).parent
RESOURCE_DIR = PACKAGE_DIR / "resource"


PLUGIN_DIR = PACKAGE_DIR
BUILTIN_RESOURCE_DIR = RESOURCE_DIR
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_RULES_JSON = BUILTIN_RESOURCE_DIR / "pig_rules.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"

CACHE_ROOT = localstore.get_plugin_data_dir() / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
ACTIVE_IMAGE_DIR = ACTIVE_RESOURCE_DIR / "images"
STATE_FILE = CACHE_ROOT / "state.json"
PRIVATE_RESOURCE_DIR = CACHE_ROOT / "private_active"
PRIVATE_STATE_FILE = CACHE_ROOT / "private_state.json"
PRIVATE_RESOURCE_ROOT = CACHE_ROOT / "private_overlays"
OFFICIAL_GIF_RESOURCE_NAME = "official-gif"
OFFICIAL_GIF_RESOURCE_MANIFEST_URL = "https://pig.felislab.cc/resources/rollpig-gif/manifest.json"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PRIVATE_SOURCE_NAME_PATTERN = re.compile(r"[^a-z0-9_-]+")
# GIF 资源应优先于同名 PNG，让资源包把某只猪替换为动态版时无需改 pig.json。
IMAGE_SUFFIX_PRIORITY = (".gif", ".png")
ALLOWED_IMAGE_SUFFIXES = set(IMAGE_SUFFIX_PRIORITY)
RESOURCE_MANIFEST_MAX_SIZE = 1 * 1024 * 1024
RESOURCE_PIG_JSON_MAX_SIZE = 2 * 1024 * 1024
RESOURCE_RULES_JSON_MAX_SIZE = 256 * 1024
RESOURCE_PACKAGE_MAX_SIZE = 128 * 1024 * 1024
RESOURCE_MAX_IMAGES = 500
RESOURCE_MAX_FILES = 700


@dataclass
class ResourceSyncResult:
    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


@dataclass
class _DownloadBudget:
    """限制单次资源同步的总文件数和总字节数，避免异常 manifest 拖垮内存或磁盘。"""

    max_total_size: int
    max_file_count: int
    total_size: int = 0
    file_count: int = 0

    def add_file(self, *, path: str, size: int) -> None:
        self.file_count += 1
        self.total_size += size
        if self.file_count > self.max_file_count:
            raise ValueError(f"资源包文件数量超过上限: {self.file_count}/{self.max_file_count}")
        if self.total_size > self.max_total_size:
            raise ValueError(f"资源包总大小超过上限: {path}")


@dataclass(frozen=True)
class _PrivateResourceSource:
    """一个私有 overlay 的运行时描述；缓存路径必须稳定，避免每次启动重复下载。"""

    name: str
    manifest_url: str
    token: str = ""
    active_dir: Path = PRIVATE_RESOURCE_DIR
    previous_dir: Path = CACHE_ROOT / "private_previous"
    state_file: Path = PRIVATE_STATE_FILE


class RollPigResourceManager:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()
        self.pig_list: list[dict[str, Any]] = []
        self.pig_map: dict[str, dict[str, Any]] = {}
        self.food_pig_ids: set[str] = set()
        self.human_pig_ids: set[str] = set()
        self.eaten_pig_ids: set[str] = set()
        self.sold_pig_ids: set[str] = set()
        self.roast_excluded_pig_ids: set[str] = set()
        self.image_dirs: list[Path] = []
        self.resource_version: str = "builtin"

    # ================================ 资源读取与内存快照 ================================ #
    # 资源读取统一走这里，命令层继续使用 PIG_LIST/find_image_file 这类旧接口，减少侵入面。
    # 缓存资源必须完整可读，否则直接回退到插件内置资源，避免坏资源包导致 bot 启动失败。
    def reload(self) -> None:
        active_pig_json = ACTIVE_RESOURCE_DIR / "pig.json"
        if active_pig_json.exists():
            try:
                self._load_from_dir(ACTIVE_RESOURCE_DIR, resource_version=self._read_state_version())
                self._load_private_overlay()
                return
            except Exception as error:
                logger.warning(f"rollpig 云端资源缓存读取失败，回退到内置资源: {error}")

        self._load_from_builtin()
        self._load_private_overlay()

    def _load_from_builtin(self) -> None:
        self._apply_snapshot(
            pig_list=self._read_pig_json(BUILTIN_PIG_JSON),
            rules=self._read_rules_json(BUILTIN_RULES_JSON),
            image_dirs=[BUILTIN_IMAGE_DIR],
            resource_version="builtin",
        )

    def _load_from_dir(self, resource_dir: Path, *, resource_version: str) -> None:
        pig_list = self._read_pig_json(resource_dir / "pig.json")
        rules = self._read_rules_json(resource_dir / "pig_rules.json")
        self._ensure_images_exist(pig_list, [resource_dir / "images", BUILTIN_IMAGE_DIR])
        self._apply_snapshot(
            pig_list=pig_list,
            rules=rules,
            image_dirs=[resource_dir / "images", BUILTIN_IMAGE_DIR],
            resource_version=resource_version or "cloud",
        )

    def _load_private_overlay(self) -> None:
        """按配置顺序叠加私有资源包；单个私有包坏掉时不影响其它资源包可用性。"""
        try:
            private_sources = self._resolve_private_sources(plugin_config)
        except Exception as error:
            logger.warning(f"rollpig 私有资源运行时配置读取失败，已忽略全部私有 overlay: {error}")
            return
        if not private_sources:
            return

        for source in private_sources:
            active_private_pig_json = source.active_dir / "pig.json"
            if not active_private_pig_json.exists():
                continue
            try:
                self._apply_private_overlay(source.active_dir, resource_version=self._read_private_state_version(source))
            except Exception as error:
                logger.warning(f"rollpig 私有资源缓存读取失败，已忽略该 overlay: name={source.name} error={error}")

    def _apply_private_overlay(self, resource_dir: Path, *, resource_version: str) -> None:
        # 私有包只允许通过 pig.json 追加新猪；覆盖公有包或前序私有包必须显式写入 pig_overrides.json。
        private_pigs = self._read_pig_json(resource_dir / "pig.json")
        private_rules = self._read_rules_json(resource_dir / "pig_rules.json")
        pig_overrides = self._read_pig_overrides_json(resource_dir / "pig_overrides.json")

        self._ensure_images_exist(private_pigs, [resource_dir / "images"])

        base_ids = set(self.pig_map)
        duplicate_ids = [str(item["id"]) for item in private_pigs if str(item["id"]) in base_ids]
        if duplicate_ids:
            raise ValueError(f"私有资源 pig.json 不能重复已有 ID，请改用 pig_overrides.json: {', '.join(duplicate_ids[:10])}")

        merged_pig_map = {str(item["id"]): dict(item) for item in self.pig_list}
        for override in pig_overrides:
            pig_id = str(override["id"])
            if pig_id not in merged_pig_map:
                raise ValueError(f"pig_overrides 指向不存在的公有 ID: {pig_id}")
            updated_item = dict(merged_pig_map[pig_id])
            updated_item.update({key: value for key, value in override.items() if key != "id"})
            updated_item["id"] = pig_id
            merged_pig_map[pig_id] = updated_item

        merged_pig_list = [merged_pig_map[str(item["id"])] for item in self.pig_list]
        merged_pig_list.extend(private_pigs)
        self._validate_pig_list(merged_pig_list)

        self.pig_list = merged_pig_list
        self.pig_map = {str(item["id"]): item for item in merged_pig_list}
        self.food_pig_ids.update(self._read_id_set(private_rules, "food_pigs"))
        self.human_pig_ids.update(self._read_id_set(private_rules, "human_pigs"))
        self.eaten_pig_ids.update(self._read_id_set(private_rules, "eaten_pigs"))
        self.sold_pig_ids.update(self._read_id_set(private_rules, "sold_pigs"))
        self.roast_excluded_pig_ids.update(self._read_id_set(private_rules, "roast_excluded_pigs"))
        self.image_dirs = [resource_dir / "images", *self.image_dirs]
        self.resource_version = f"{self.resource_version}+{resource_version or 'private'}"
        logger.info(
            f"rollpig 私有资源已叠加: version={resource_version}, private_pigs={len(private_pigs)}, total={len(self.pig_list)}"
        )

    def _apply_snapshot(
        self,
        *,
        pig_list: list[dict[str, Any]],
        rules: dict[str, Any],
        image_dirs: list[Path],
        resource_version: str,
    ) -> None:
        self._validate_pig_list(pig_list)
        self.pig_list = pig_list
        self.pig_map = {str(item["id"]): item for item in pig_list}
        self.food_pig_ids = self._read_id_set(rules, "food_pigs")
        self.human_pig_ids = self._read_id_set(rules, "human_pigs")
        self.eaten_pig_ids = self._read_id_set(rules, "eaten_pigs")
        self.sold_pig_ids = self._read_id_set(rules, "sold_pigs")
        self.roast_excluded_pig_ids = self._read_id_set(rules, "roast_excluded_pigs")
        self.image_dirs = image_dirs
        self.resource_version = resource_version
        logger.info(f"rollpig 资源已加载: version={resource_version}, pigs={len(pig_list)}")

    def find_image_file(self, pig_id: str) -> Path | None:
        for image_dir in self.image_dirs:
            for suffix in IMAGE_SUFFIX_PRIORITY:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    def _read_state_version(self) -> str:
        try:
            state = json.loads(self._read_json_text(STATE_FILE))
            return str(state.get("resource_version") or "cloud")
        except Exception:
            return "cloud"

    def _read_private_state_version(self, source: _PrivateResourceSource | None = None) -> str:
        state_file = source.state_file if source is not None else PRIVATE_STATE_FILE
        try:
            state = json.loads(self._read_json_text(state_file))
            return str(state.get("resource_version") or "private")
        except Exception:
            return "private"

    def _resolve_private_sources(self, config: Config) -> list[_PrivateResourceSource]:
        """解析官方与用户 overlay；官方 GIF 包固定随云端资源启用，不暴露移除开关。"""

        raw_sources = list(getattr(config, "rollpig_private_resource_manifests", []) or [])
        sources: list[_PrivateResourceSource] = []
        seen_urls: set[str] = set()
        seen_names: set[str] = set()

        if config.rollpig_resource_sync_enabled:
            official_source = self._with_unique_private_source_name(
                self._official_gif_private_source(),
                index=0,
                seen_names=seen_names,
            )
            sources.append(official_source)
            seen_urls.add(official_source.manifest_url)

        for index, raw_source in enumerate(raw_sources, start=1):
            source = self._coerce_private_source(raw_source, index=index, default_token=config.rollpig_private_resource_token)
            if source is None:
                continue
            if source.manifest_url in seen_urls:
                logger.warning(f"rollpig 私有资源配置存在重复 manifest，已忽略: {source.manifest_url}")
                continue
            source = self._with_unique_private_source_name(source, index=index, seen_names=seen_names)
            sources.append(source)
            seen_urls.add(source.manifest_url)

        legacy_manifest_url = str(config.rollpig_private_resource_manifest_url or "").strip()
        if legacy_manifest_url and legacy_manifest_url not in seen_urls:
            sources.append(
                _PrivateResourceSource(
                    name="legacy",
                    manifest_url=legacy_manifest_url,
                    token=str(config.rollpig_private_resource_token or "").strip(),
                    active_dir=PRIVATE_RESOURCE_DIR,
                    previous_dir=CACHE_ROOT / "private_previous",
                    state_file=PRIVATE_STATE_FILE,
                )
            )

        return sources

    def _official_gif_private_source(self) -> _PrivateResourceSource:
        """官方 GIF 动态小猪包属于 Plus 云端资源组成部分，不走用户私有包配置。"""

        root = PRIVATE_RESOURCE_ROOT / OFFICIAL_GIF_RESOURCE_NAME
        return _PrivateResourceSource(
            name=OFFICIAL_GIF_RESOURCE_NAME,
            manifest_url=OFFICIAL_GIF_RESOURCE_MANIFEST_URL,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    def _coerce_private_source(
        self,
        raw_source: Any,
        *,
        index: int,
        default_token: str | None,
    ) -> _PrivateResourceSource | None:
        """把 JSON 中的字符串或 object 配置归一化为运行时 source。"""

        if isinstance(raw_source, str):
            manifest_url = raw_source.strip()
            raw_name = ""
            token = str(default_token or "").strip()
        elif isinstance(raw_source, dict) or hasattr(raw_source, "manifest_url"):
            if not isinstance(raw_source, dict):
                raw_source = raw_source.dict() if hasattr(raw_source, "dict") else vars(raw_source)
            manifest_url = str(raw_source.get("manifest_url") or raw_source.get("url") or "").strip()
            raw_name = str(raw_source.get("name") or "").strip()
            token = str(raw_source.get("token") or default_token or "").strip()
        else:
            raise ValueError(f"rollpig_private_resource_manifests[{index}] 必须是字符串或 object")

        if not manifest_url:
            return None

        name = self._normalize_private_source_name(raw_name or self._guess_private_source_name(manifest_url), index=index)
        root = PRIVATE_RESOURCE_ROOT / name
        return _PrivateResourceSource(
            name=name,
            manifest_url=manifest_url,
            token=token,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    def _guess_private_source_name(self, manifest_url: str) -> str:
        parsed = urlparse(manifest_url)
        if parsed.scheme in {"http", "https", "file"}:
            path = Path(unquote(parsed.path))
        else:
            path = Path(manifest_url)
        parent_name = path.parent.name if path.name == "manifest.json" else path.stem
        return parent_name or "private"

    def _normalize_private_source_name(self, name: str, *, index: int) -> str:
        normalized = PRIVATE_SOURCE_NAME_PATTERN.sub("-", name.strip().lower()).strip("-_")
        return normalized[:48] or f"private-{index}"

    def _with_unique_private_source_name(
        self,
        source: _PrivateResourceSource,
        *,
        index: int,
        seen_names: set[str],
    ) -> _PrivateResourceSource:
        """避免多个 overlay 使用同一缓存目录；同名时追加序号，保留配置顺序。"""

        if source.name not in seen_names:
            seen_names.add(source.name)
            return source

        base_name = source.name[:43].strip("-_") or "private"
        candidate = f"{base_name}-{index}"
        while candidate in seen_names:
            candidate = f"{base_name}-{uuid.uuid4().hex[:6]}"
        seen_names.add(candidate)
        logger.warning(f"rollpig 私有资源缓存名重复，已自动调整缓存目录名: {source.name} -> {candidate}")
        root = PRIVATE_RESOURCE_ROOT / candidate
        return replace(
            source,
            name=candidate,
            active_dir=root / "active",
            previous_dir=root / "previous",
            state_file=root / "state.json",
        )

    # ================================ 云端同步 ================================ #
    # 同步流程采用“临时目录下载 -> 完整校验 -> 原子替换 active”的方式，避免半包覆盖。
    async def sync_all(self, *, force: bool = False, wait_if_busy: bool = True) -> tuple[ResourceSyncResult, ResourceSyncResult]:
        """串行同步公有包与所有私有 overlay；手动同步等待，后台同步可选择忙时跳过。"""
        if not wait_if_busy and self._sync_lock.locked():
            return (
                ResourceSyncResult(updated=False, skipped=True, message="已有资源同步任务运行中"),
                ResourceSyncResult(updated=False, skipped=True, message=""),
        )
        async with self._sync_lock:
            public_result = await self._sync_from_remote_unlocked(force=force)
            private_result = await self._sync_private_overlays_from_remote_unlocked(force=force)
            if public_result.updated or private_result.updated:
                self.reload()
            return public_result, private_result

    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """兼容旧调用：单独同步公有包时也进入同一把锁。"""
        async with self._sync_lock:
            result = await self._sync_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def sync_private_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """兼容旧调用：单独同步私有包时也进入同一把锁，返回多 overlay 聚合结果。"""
        async with self._sync_lock:
            result = await self._sync_private_overlays_from_remote_unlocked(force=force)
            if result.updated:
                self.reload()
            return result

    async def _sync_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        if not plugin_config.rollpig_resource_sync_enabled and not force:
            return ResourceSyncResult(updated=False, skipped=True, message="资源同步未启用")

        manifest_url = str(plugin_config.rollpig_resource_manifest_url or "").strip()
        if not manifest_url:
            return ResourceSyncResult(updated=False, skipped=True, message="未配置资源 manifest URL")

        timeout = max(1.0, float(plugin_config.rollpig_resource_sync_timeout or 10.0))
        max_file_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)

            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")
            if not force and resource_version == self._read_state_version():
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"公有资源：已是最新（{resource_version}）",
                )

            staging_dir = self._new_staging_dir("incoming")
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)

            try:
                await self._download_manifest_files(
                    client,
                    manifest_url=manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_file_size,
                )
                pig_list = self._read_pig_json(staging_dir / "pig.json")
                self._ensure_images_exist(pig_list, [staging_dir / "images"])
                self._activate_staging_dir(staging_dir, resource_version)
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"公有资源：已更新（{resource_version}）",
        )

    async def _sync_private_overlays_from_remote_unlocked(self, *, force: bool = False) -> ResourceSyncResult:
        sources = self._resolve_private_sources(plugin_config)
        if not sources:
            return ResourceSyncResult(updated=False, skipped=True, message="")

        results: list[ResourceSyncResult] = []
        for source in sources:
            try:
                results.append(await self._sync_private_source_from_remote_unlocked(source, force=force))
            except Exception as error:
                # 私有 overlay 是附加包：某个包失败必须报告，但不能让公有包或其它私有包失效。
                logger.warning(f"rollpig 私有资源同步失败，继续使用当前缓存: name={source.name} error={error}")
                results.append(
                    ResourceSyncResult(
                        updated=False,
                        skipped=False,
                        message=f"{self._private_source_label(source)}：同步失败（{error}）",
                    )
                )

        messages = [result.message for result in results if result.message]
        return ResourceSyncResult(
            updated=any(result.updated for result in results),
            skipped=all(result.skipped for result in results),
            resource_version="+".join(result.resource_version for result in results if result.resource_version),
            message="\n".join(messages),
        )

    async def _sync_private_source_from_remote_unlocked(
        self,
        source: _PrivateResourceSource,
        *,
        force: bool = False,
    ) -> ResourceSyncResult:
        timeout = max(1.0, float(plugin_config.rollpig_resource_sync_timeout or 10.0))
        headers: dict[str, str] = {}
        if source.token:
            headers["Authorization"] = f"Bearer {source.token}"

        max_file_size = max(1024, int(plugin_config.rollpig_resource_max_file_size or 10 * 1024 * 1024))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            manifest = await self._download_json(client, source.manifest_url, max_size=RESOURCE_MANIFEST_MAX_SIZE)

            if not bool(manifest.get("overlay")):
                raise ValueError(f"私有资源 manifest 必须标记 overlay=true: {source.name}")

            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError(f"私有资源 manifest 缺少 resource_version: {source.name}")
            if not force and resource_version == self._read_private_state_version(source):
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"{self._private_source_label(source)}：已是最新（{resource_version}）",
                )

            staging_dir = self._new_staging_dir(f"incoming_private_{source.name}")
            (staging_dir / "images").mkdir(parents=True, exist_ok=True)

            try:
                await self._download_private_manifest_files(
                    client,
                    manifest_url=source.manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_file_size,
                )
                private_pigs = self._read_pig_json(staging_dir / "pig.json")
                self._ensure_images_exist(private_pigs, [staging_dir / "images"])
                self._activate_private_staging_dir(source, staging_dir, resource_version)
            except Exception:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                raise

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"{self._private_source_label(source)}：已更新（{resource_version}）",
        )

    def _private_source_label(self, source: _PrivateResourceSource) -> str:
        """把内部 overlay 名转成面向 QQ 消息的短标签，避免把缓存名直接暴露给用户。"""

        if source.name == OFFICIAL_GIF_RESOURCE_NAME:
            return "GIF 动态包"
        if source.name == "legacy":
            return "旧版私有资源"
        return f"私有资源 {source.name}"

    async def _download_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("manifest 缺少 pig_json")
        budget = _DownloadBudget(max_total_size=RESOURCE_PACKAGE_MAX_SIZE, max_file_count=RESOURCE_MAX_FILES)
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=min(max_size, RESOURCE_PIG_JSON_MAX_SIZE),
            budget=budget,
        )

        optional_files = manifest.get("optional_files") or {}
        rules_meta = optional_files.get("pig_rules") if isinstance(optional_files, dict) else None
        if isinstance(rules_meta, dict):
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=rules_meta,
                target=staging_dir / "pig_rules.json",
                max_size=min(max_size, RESOURCE_RULES_JSON_MAX_SIZE),
                budget=budget,
            )

        image_items = manifest.get("images")
        if not isinstance(image_items, list):
            raise ValueError("manifest 缺少 images 列表")
        if len(image_items) > RESOURCE_MAX_IMAGES:
            raise ValueError(f"manifest images 数量超过上限: {len(image_items)}/{RESOURCE_MAX_IMAGES}")
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("manifest images 存在非法条目")
            filename = str(image_meta.get("filename") or "")
            self._validate_image_filename(filename)
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=staging_dir / "images" / filename,
                max_size=max_size,
                budget=budget,
            )

    async def _download_private_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("私有资源 manifest 缺少 pig_json")
        budget = _DownloadBudget(max_total_size=RESOURCE_PACKAGE_MAX_SIZE, max_file_count=RESOURCE_MAX_FILES)
        await self._download_file_by_meta(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=min(max_size, RESOURCE_PIG_JSON_MAX_SIZE),
            budget=budget,
        )

        optional_files = manifest.get("optional_files") or {}
        if not isinstance(optional_files, dict):
            raise ValueError("私有资源 optional_files 必须是 object")
        for key, filename in (("pig_rules", "pig_rules.json"), ("pig_overrides", "pig_overrides.json")):
            file_meta = optional_files.get(key)
            if isinstance(file_meta, dict):
                await self._download_file_by_meta(
                    client,
                    manifest_url=manifest_url,
                    meta=file_meta,
                    target=staging_dir / filename,
                    max_size=min(max_size, RESOURCE_RULES_JSON_MAX_SIZE),
                    budget=budget,
                )

        image_items = manifest.get("images") or []
        if not isinstance(image_items, list):
            raise ValueError("私有资源 manifest images 必须是 list")
        if len(image_items) > RESOURCE_MAX_IMAGES:
            raise ValueError(f"私有资源 images 数量超过上限: {len(image_items)}/{RESOURCE_MAX_IMAGES}")
        for image_meta in image_items:
            if not isinstance(image_meta, dict):
                raise ValueError("私有资源 images 存在非法条目")
            filename = str(image_meta.get("filename") or "")
            self._validate_image_filename(filename)
            await self._download_file_by_meta(
                client,
                manifest_url=manifest_url,
                meta=image_meta,
                target=staging_dir / "images" / filename,
                max_size=max_size,
                budget=budget,
            )

    async def _download_json(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> dict[str, Any]:
        content = await self._download_bytes(client, url, max_size=max_size)
        data = json.loads(content.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON object")
        return data

    async def _download_file_by_meta(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
        budget: _DownloadBudget,
    ) -> None:
        path = str(meta.get("path") or meta.get("filename") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        self._validate_manifest_path(path)

        expected_size = meta.get("size")
        if expected_size is not None and int(expected_size) > max_size:
            raise ValueError(f"文件超过大小上限: {path}")

        size, actual_hash, tmp = await self._copy_manifest_file_to_temp(
            client,
            manifest_url=manifest_url,
            path=path,
            target=target,
            max_size=max_size,
        )

        try:
            if expected_size is not None and int(expected_size) != size:
                raise ValueError(f"文件大小校验失败: {path}")

            expected_hash = str(meta.get("sha256") or "").lower()
            if expected_hash and actual_hash != expected_hash:
                raise ValueError(f"sha256 校验失败: {path}")

            budget.add_file(path=path, size=size)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

    async def _download_bytes(self, client: httpx.AsyncClient, url: str, *, max_size: int) -> bytes:
        if self._is_local_manifest_url(url):
            path = self._local_manifest_path(url)
            return await asyncio.to_thread(self._read_local_bytes_sync, path, max_size)

        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_size:
                    raise ValueError(f"文件超过大小上限: {url}")
                chunks.append(chunk)
        return b"".join(chunks)

    async def _copy_manifest_file_to_temp(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        path: str,
        target: Path,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """按 manifest 来源复制文件；本地包直接拷贝，远端包继续走流式下载。"""

        if self._is_local_manifest_url(manifest_url):
            source = self._local_manifest_path(manifest_url).parent / path
            return await asyncio.to_thread(self._copy_local_file_to_temp_sync, source, target, max_size)

        url = urljoin(manifest_url, path)
        return await self._download_file_to_temp(client, url, target, max_size=max_size)

    def _is_local_manifest_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme not in {"http", "https"}

    def _local_manifest_path(self, url: str) -> Path:
        """解析本地 manifest 路径；支持普通路径与 file:// URL，供自建私有包离线使用。"""

        parsed = urlparse(url)
        if parsed.scheme == "file":
            raw_path = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:/", raw_path):
                raw_path = raw_path[1:]
            if parsed.netloc:
                raw_path = f"//{parsed.netloc}{raw_path}"
        else:
            raw_path = url
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else Path.cwd() / path

    def _read_local_bytes_sync(self, path: Path, max_size: int) -> bytes:
        if not path.exists():
            raise FileNotFoundError(f"本地资源文件不存在: {path}")
        size = path.stat().st_size
        if size > max_size:
            raise ValueError(f"文件超过大小上限: {path}")
        return path.read_bytes()

    def _copy_local_file_to_temp_sync(self, source: Path, target: Path, max_size: int) -> tuple[int, str, Path]:
        """把本地资源文件复制到临时文件并计算 sha256，校验通过前不覆盖 active。"""

        if not source.exists():
            raise FileNotFoundError(f"本地资源文件不存在: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            with source.open("rb") as source_file, tmp.open("wb") as target_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_size:
                        raise ValueError(f"文件超过大小上限: {source}")
                    hasher.update(chunk)
                    target_file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def _download_file_to_temp(
        self,
        client: httpx.AsyncClient,
        url: str,
        target: Path,
        *,
        max_size: int,
    ) -> tuple[int, str, Path]:
        """流式下载到临时文件；校验通过前绝不覆盖目标文件。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        total = 0
        hasher = hashlib.sha256()
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._validate_content_length(response.headers.get("Content-Length"), max_size=max_size, label=url)
                with tmp.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_size:
                            raise ValueError(f"文件超过大小上限: {url}")
                        hasher.update(chunk)
                        file.write(chunk)
            return total, hasher.hexdigest(), tmp
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _activate_staging_dir(self, staging_dir: Path, resource_version: str) -> None:
        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=ACTIVE_RESOURCE_DIR,
            previous_dir=CACHE_ROOT / "previous",
            state_file=STATE_FILE,
            state_payload={"resource_version": resource_version, "synced_at": int(time.time())},
        )

    def _activate_private_staging_dir(
        self,
        source: _PrivateResourceSource,
        staging_dir: Path,
        resource_version: str,
    ) -> None:
        self._activate_resource_dir(
            staging_dir=staging_dir,
            active_dir=source.active_dir,
            previous_dir=source.previous_dir,
            state_file=source.state_file,
            state_payload={"name": source.name, "manifest_url": source.manifest_url, "resource_version": resource_version, "synced_at": int(time.time())},
        )

    def _activate_resource_dir(
        self,
        *,
        staging_dir: Path,
        active_dir: Path,
        previous_dir: Path,
        state_file: Path,
        state_payload: dict[str, Any],
    ) -> None:
        """事务式激活资源目录；任何一步失败都尽量恢复旧 active，避免资源目录被切空。"""
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        active_dir.parent.mkdir(parents=True, exist_ok=True)
        previous_dir.parent.mkdir(parents=True, exist_ok=True)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_tmp = state_file.with_name(f".{state_file.name}.{uuid.uuid4().hex}.tmp")
        moved_old = False
        activated_new = False
        old_active_backup = active_dir.exists()
        old_previous_backup = previous_dir.exists()

        previous_backup_dir = CACHE_ROOT / f".{previous_dir.name}_rollback_{uuid.uuid4().hex}"
        if old_previous_backup:
            previous_dir.rename(previous_backup_dir)

        try:
            if active_dir.exists():
                active_dir.rename(previous_dir)
                moved_old = True
            staging_dir.rename(active_dir)
            activated_new = True
            state_tmp.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            state_tmp.replace(state_file)
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir)
        except Exception:
            state_tmp.unlink(missing_ok=True)
            if activated_new and active_dir.exists():
                shutil.rmtree(active_dir, ignore_errors=True)
            if moved_old and previous_dir.exists() and not active_dir.exists():
                previous_dir.rename(active_dir)
            if old_previous_backup and previous_backup_dir.exists() and not previous_dir.exists():
                previous_backup_dir.rename(previous_dir)
            raise
        finally:
            if previous_backup_dir.exists():
                shutil.rmtree(previous_backup_dir, ignore_errors=True)

        if not old_active_backup and previous_dir.exists():
            # 没有旧 active 时，previous 不应凭空保留；这个分支只用于清理异常历史残留。
            shutil.rmtree(previous_dir, ignore_errors=True)

    def _new_staging_dir(self, prefix: str) -> Path:
        """每次同步使用 UUID staging，避免同一秒内多任务撞目录。"""
        return CACHE_ROOT / f".{prefix}_{uuid.uuid4().hex}"

    def _validate_content_length(self, content_length: str | None, *, max_size: int, label: str) -> None:
        if not content_length:
            return
        try:
            declared_size = int(content_length)
        except ValueError:
            return
        if declared_size > max_size:
            raise ValueError(f"文件超过大小上限: {label}")

    def _validate_manifest_path(self, path: str) -> None:
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc or path.startswith("/") or "\\" in path:
            raise ValueError(f"manifest 文件路径非法: {path}")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"manifest 文件路径非法: {path}")

    # ================================ 校验与解析 ================================ #
    def _read_json_text(self, path: Path) -> str:
        """读取资源 JSON 文本；兼容 1Panel/Windows 上传链路偶发写入的 UTF-8 BOM。"""
        return path.read_text(encoding="utf-8-sig")

    def _read_pig_json(self, path: Path) -> list[dict[str, Any]]:
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        return data

    def _read_rules_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, dict):
            raise ValueError(f"pig_rules.json 必须是 object: {path}")
        return data

    def _read_pig_overrides_json(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = json.loads(self._read_json_text(path))
        if not isinstance(data, list):
            raise ValueError(f"pig_overrides.json 必须是 list: {path}")
        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig_overrides.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"pig_overrides.json 存在非法 ID: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"pig_overrides.json 存在重复 ID: {pig_id}")
            seen_ids.add(pig_id)
        return data

    def _validate_pig_list(self, pig_list: list[dict[str, Any]]) -> None:
        seen_ids: set[str] = set()
        for item in pig_list:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非法条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            if not item.get("name"):
                raise ValueError(f"pig 缺少 name: {pig_id}")
            seen_ids.add(pig_id)

    def _ensure_images_exist(self, pig_list: list[dict[str, Any]], image_dirs: list[Path]) -> None:
        missing: list[str] = []
        for item in pig_list:
            pig_id = str(item.get("id") or "")
            if not any(
                (image_dir / f"{pig_id}{suffix}").exists()
                for image_dir in image_dirs
                for suffix in IMAGE_SUFFIX_PRIORITY
            ):
                missing.append(pig_id)
        if missing:
            raise ValueError(f"资源包缺少图片: {', '.join(missing[:10])}")

    def _read_id_set(self, rules: dict[str, Any], key: str) -> set[str]:
        raw_items = rules.get(key) or []
        if not isinstance(raw_items, list):
            raise ValueError(f"pig_rules.{key} 必须是 list")
        result: set[str] = set()
        for raw_id in raw_items:
            pig_id = str(raw_id)
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"pig_rules.{key} 存在非法 ID: {pig_id}")
            result.add(pig_id)
        return result

    def _validate_image_filename(self, filename: str) -> None:
        path = Path(filename)
        if path.name != filename:
            raise ValueError(f"图片文件名不能包含路径: {filename}")
        if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"不支持的图片格式: {filename}")
        pig_id = path.stem
        if not PIG_ID_PATTERN.match(pig_id):
            raise ValueError(f"图片文件名非法: {filename}")


pig_resource_manager = RollPigResourceManager()


# ================================ 小猪资源快照 ================================ #
# 命令层高频读取 `PIG_LIST`，这里保留同一个 list 对象并用切片刷新。
# 这样 `from ... import PIG_LIST` 的旧调用方式不会因为资源重载而拿到过期引用。
PIG_LIST: list[dict] = []


def reload_rollpig_resources() -> None:
    """刷新内存中的小猪资源快照；资源管理器会在云端资源损坏时回退内置资源。"""

    pig_resource_manager.reload()
    PIG_LIST[:] = pig_resource_manager.pig_list


def find_image_file(pig_id: str) -> Path | None:
    """返回指定小猪的本地图片路径；不存在时返回 None 交给渲染层兜底。"""

    return pig_resource_manager.find_image_file(pig_id)


def get_pig_by_id(pig_id: str | None) -> dict | None:
    """从当前资源快照按 id 查找小猪数据。"""

    if not pig_id:
        return None
    for pig in PIG_LIST:
        if pig["id"] == pig_id:
            return pig
    return None


async def sync_rollpig_resources(force: bool = False) -> str:
    """同步公有云端资源与可选私有 overlay；成功后立即刷新内存快照。"""

    public_result, private_result = await pig_resource_manager.sync_all(force=force, wait_if_busy=force)
    if public_result.updated or private_result.updated:
        PIG_LIST[:] = pig_resource_manager.pig_list

    messages = []
    for result in (public_result, private_result):
        if result.message:
            messages.append(result.message)
    return "\n".join(messages) or "小猪资源无需同步"


reload_rollpig_resources()
