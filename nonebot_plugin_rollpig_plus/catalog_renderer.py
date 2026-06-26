from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from nonebot import get_plugin_config
from PIL import Image
import nonebot_plugin_localstore as localstore

from .config import Config
from .perf_logging import log_perf
from .render_budget import html_render_budget
from .resource_manager import pig_resource_manager
from .runtime import ROLLPIG_TIMEZONE, rollpig_today
from .store.models import CatalogSnapshot, DrawState, PigProgress


RES_DIR = Path(__file__).parent / "resource"
THUMB_CACHE_DIR = localstore.get_plugin_cache_dir() / "catalog_thumbs"
CATALOG_BASE_IMAGE = RES_DIR / "catalog_base.png"
CATALOG_TEMPLATE = "catalog_template.html"
CATALOG_ANCHOR_HTML = RES_DIR / "catalog_anchor.html"
CATALOG_SIZE = (1536, 1024)
# 当前底图安全区按 38 张卡片精修，开放配置会让最后一行与装饰区重新漂移。
CATALOG_PAGE_SIZE = 38
CATALOG_CACHE_MAX_ENTRIES = 64
CATALOG_CACHE_MAX_BYTES = 64 * 1024 * 1024
MAX_EXPERT_LEVEL = 5
NEW_BADGE_DAYS = 7


@dataclass
class _CachedCatalogImage:
    created_at: float
    payload: bytes


@dataclass
class _PagePoolRenderResult:
    raw_image: bytes
    wait_seconds: float
    page_seconds: float


# ================================ 页面池与缓存状态 ================================ #

_catalog_cache: dict[str, _CachedCatalogImage] = {}
_page_pools: dict[tuple[int, float], "_CatalogPagePool"] = {}


class _CatalogPagePool:
    """复用 htmlrender 已启动的 Chromium，为图鉴保留少量常驻页面。

    `template_to_pic` 每次都会创建/关闭页面并等待通用的 networkidle；图鉴资源固定且
    基本都是本地文件，因此复用页面可以减少冷渲染时的页面生命周期开销。池大小仍由
    并发配置限制，避免多人同时触发时把 Chromium 裸并发打满。
    """

    def __init__(self, *, size: int, scale_factor: float):
        self.size = max(1, min(12, int(size or 1)))
        self.scale_factor = max(1.0, min(3.0, float(scale_factor or 2.0)))
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.size)
        self._lock = asyncio.Lock()
        self._created_count = 0
        self._pages: set[Any] = set()

    async def render(self, html: str, *, timeout_ms: int) -> _PagePoolRenderResult:
        """取出一个常驻页面渲染 HTML；页面异常时丢弃，下一次请求再补建。"""
        wait_started_at = time.perf_counter()
        page = await self._acquire(timeout_ms=timeout_ms)
        acquired_at = time.perf_counter()
        healthy = False
        try:
            page.set_default_timeout(timeout_ms)
            await page.set_viewport_size({"width": CATALOG_SIZE[0], "height": CATALOG_SIZE[1]})
            await page.set_content(html, wait_until="domcontentloaded", timeout=timeout_ms)
            await _wait_for_catalog_assets(page, timeout_ms=timeout_ms)
            raw_image = await page.screenshot(
                full_page=False,
                type="png",
                timeout=timeout_ms,
            )
            healthy = True
            finished_at = time.perf_counter()
            return _PagePoolRenderResult(
                raw_image=raw_image,
                wait_seconds=acquired_at - wait_started_at,
                page_seconds=finished_at - acquired_at,
            )
        finally:
            await self._release(page, healthy=healthy)

    async def close(self) -> None:
        """关闭池内页面；浏览器本体仍交给 htmlrender 的 shutdown 流程管理。"""
        async with self._lock:
            pages = list(self._pages)
            self._pages.clear()
            self._created_count = 0
            while not self._queue.empty():
                with suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
        for page in pages:
            await _safe_close_page(page)

    async def _acquire(self, *, timeout_ms: int) -> Any:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        should_create = False
        async with self._lock:
            if self._created_count < self.size:
                self._created_count += 1
                should_create = True

        if should_create:
            try:
                page = await self._create_page(timeout_ms=timeout_ms)
            except Exception:
                async with self._lock:
                    self._created_count = max(0, self._created_count - 1)
                raise
            return page

        return await asyncio.wait_for(self._queue.get(), timeout=timeout_ms / 1000)

    async def _release(self, page: Any, *, healthy: bool) -> None:
        if healthy and not page.is_closed():
            await self._queue.put(page)
            return

        await _safe_close_page(page)
        async with self._lock:
            self._pages.discard(page)
            self._created_count = max(0, self._created_count - 1)

    async def _create_page(self, *, timeout_ms: int) -> Any:
        from nonebot_plugin_htmlrender import get_browser

        browser = await get_browser()
        page = await browser.new_page(
            device_scale_factor=self.scale_factor,
            viewport={"width": CATALOG_SIZE[0], "height": CATALOG_SIZE[1]},
        )
        page.set_default_timeout(timeout_ms)
        page.on("console", lambda msg: log_perf(f"rollpig catalog browser console: {msg.text}"))
        # 先进入插件资源目录下的 HTML file:// 页面，再 set_content；否则 about:blank
        # 安全上下文会拒绝加载本地图鉴底图和缩略图资源。不能锚到 PNG，否则 Chromium
        # 会把主文档视作图片页面，后续 set_content 可能卡在 domcontentloaded。
        await page.goto(CATALOG_ANCHOR_HTML.as_uri(), wait_until="domcontentloaded", timeout=timeout_ms)
        async with self._lock:
            self._pages.add(page)
        return page


async def _safe_close_page(page: Any) -> None:
    if page is None or page.is_closed():
        return
    with suppress(Exception):
        await page.close()


def _get_page_pool(limit: int, scale_factor: float) -> _CatalogPagePool:
    safe_limit = max(1, min(12, int(limit or 1)))
    safe_scale = max(1.0, min(3.0, float(scale_factor or 2.0)))
    key = (safe_limit, safe_scale)
    pool = _page_pools.get(key)
    if pool is None:
        pool = _CatalogPagePool(size=safe_limit, scale_factor=safe_scale)
        _page_pools[key] = pool
    return pool


async def shutdown_catalog_renderer() -> None:
    """关闭图鉴页面池；用于 Bot 退出时释放常驻页面。"""
    pools = list(_page_pools.values())
    _page_pools.clear()
    for pool in pools:
        await pool.close()


# ================================ 图鉴结果缓存 ================================ #

def _catalog_cache_bytes() -> int:
    return sum(len(cached.payload) for cached in _catalog_cache.values())


def _prune_catalog_cache(*, ttl: int, now: float) -> None:
    """清理图片结果缓存；TTL 控制新鲜度，硬上限防止多用户短时生成导致内存膨胀。"""
    if not _catalog_cache:
        return
    if ttl <= 0:
        _catalog_cache.clear()
        return

    expired_keys = [key for key, cached in _catalog_cache.items() if now - cached.created_at > ttl]
    for key in expired_keys:
        _catalog_cache.pop(key, None)

    overflow = len(_catalog_cache) - CATALOG_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return

    oldest_keys = sorted(_catalog_cache, key=lambda key: _catalog_cache[key].created_at)[:overflow]
    for key in oldest_keys:
        _catalog_cache.pop(key, None)

    # PNG 图鉴单张可达数 MB，仅限制条数不足以约束内存；总字节超限时继续按最老优先淘汰。
    for key in sorted(_catalog_cache, key=lambda key: _catalog_cache[key].created_at):
        if _catalog_cache_bytes() <= CATALOG_CACHE_MAX_BYTES:
            break
        _catalog_cache.pop(key, None)


async def _wait_for_catalog_assets(page: Any, *, timeout_ms: int) -> None:
    """等待图鉴关键本地资源完成加载，避免比 networkidle 更重的通用等待策略。"""
    script = """
    async ({ maxWaitMs }) => {
      const waitImage = (source) => new Promise((resolve) => {
        if (!source) return resolve(null);
        const img = new Image();
        let done = false;
        const finish = () => {
          if (!done) {
            done = true;
            resolve(null);
          }
        };
        img.onload = finish;
        img.onerror = finish;
        img.src = source;
        if (img.complete) finish();
      });

      const tasks = Array.from(document.images).map((img) => {
        if (img.complete) return Promise.resolve(null);
        return new Promise((resolve) => {
          img.onload = () => resolve(null);
          img.onerror = () => resolve(null);
        });
      });

      const background = getComputedStyle(document.body).backgroundImage || "";
      const match = background.match(/url\\(["']?(.*?)["']?\\)/);
      if (match && match[1]) tasks.push(waitImage(match[1]));
      if (document.fonts && document.fonts.ready) tasks.push(document.fonts.ready.catch(() => null));

      await Promise.race([
        Promise.all(tasks),
        new Promise((resolve) => setTimeout(resolve, Math.min(maxWaitMs, 1500))),
      ]);
    }
    """
    await page.evaluate(script, {"maxWaitMs": timeout_ms})


def get_expert_level(copies: int) -> int:
    """图鉴渲染侧的等级算法必须和命令文案保持一致：1 次为 Lv.0，6 次封顶。"""
    return min(max(int(copies or 0) - 1, 0), MAX_EXPERT_LEVEL)


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # first_obtained_at 本地模式按 UTC 写入；NEW 徽章属于用户可见业务日期，
        # 因此要先换算到 RollPig 日期边界再取 date。
        parsed = parsed.astimezone(ROLLPIG_TIMEZONE).replace(tzinfo=None)
    return parsed


def _is_recent_new(first_obtained_at: str | None, *, today: dt.date) -> bool:
    obtained_at = _parse_datetime(first_obtained_at)
    if obtained_at is None:
        return False
    return 0 <= (today - obtained_at.date()).days < NEW_BADGE_DAYS


def _thumbnail_uri(pig_id: str, image_file: Path) -> str:
    """为图鉴卡片生成小缩略图；指纹包含资源版本和源文件状态，资源更新后会自动失效。"""
    try:
        stat = image_file.stat()
    except OSError:
        return image_file.as_uri()
    cache_key = hashlib.sha256(
        "|".join(
            [
                pig_resource_manager.resource_version,
                pig_id,
                str(image_file.resolve()),
                str(stat.st_size),
                str(stat.st_mtime_ns),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    target = THUMB_CACHE_DIR / f"{pig_id}_{cache_key}.png"
    if target.exists():
        return target.as_uri()

    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with Image.open(image_file) as image:
        image = image.convert("RGBA")
        image.thumbnail((128, 128), Image.Resampling.LANCZOS)
        image.save(target, format="PNG", optimize=True)
    return target.as_uri()


def _image_uri(pig_id: str) -> str:
    image_file = pig_resource_manager.find_image_file(pig_id)
    return _thumbnail_uri(pig_id, image_file) if image_file else ""


def _calculate_checkin_streak(recent_rolls: dict[str, str], *, today: dt.date) -> int:
    """从今天往前数连续抽猪天数；图鉴只展示已有记录，不补写任何状态。"""
    streak = 0
    for offset in range(60):
        date_str = (today - dt.timedelta(days=offset)).isoformat()
        if date_str not in recent_rolls:
            break
        streak += 1
    return streak


def _next_milestone(unlocked: int, total: int) -> int:
    if total <= 0:
        return 0
    if unlocked >= total:
        return total
    return min(total, max(10, ((unlocked // 10) + 1) * 10))


def _sort_progress_items(draw_state: DrawState) -> list[tuple[str, PigProgress]]:
    resource_order = {str(pig.get("id")): index for index, pig in enumerate(pig_resource_manager.pig_list)}
    return sorted(
        draw_state.progress.items(),
        key=lambda item: (
            -get_expert_level(item[1].copies),
            -int(item[1].copies or 0),
            item[1].first_obtained_at or "",
            resource_order.get(item[0], 10**9),
            item[0],
        ),
    )


def _build_template_payload(
    *,
    user_name: str,
    snapshot: CatalogSnapshot,
    page: int,
) -> dict[str, Any]:
    today = rollpig_today()
    page_size = CATALOG_PAGE_SIZE
    progress_items = _sort_progress_items(snapshot.draw_state)
    total_pigs = len(pig_resource_manager.pig_list)
    unlocked = len(snapshot.draw_state.pig_ids)
    pages = max(1, math.ceil(max(1, unlocked) / page_size))
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    page_items = progress_items[start : start + page_size]

    cards: list[dict[str, Any]] = []
    for pig_id, progress in page_items:
        pig = pig_resource_manager.pig_map.get(pig_id, {})
        level = get_expert_level(progress.copies)
        is_max = level >= MAX_EXPERT_LEVEL
        is_new = (not is_max) and _is_recent_new(progress.first_obtained_at, today=today)
        badge = "MAX" if is_max else ("NEW" if is_new else "")
        cards.append(
            {
                "id": pig_id,
                "name": str(pig.get("name") or pig_id),
                "image": _image_uri(pig_id),
                "level": level,
                "badge": badge,
                "badge_class": badge.lower(),
            }
        )

    if progress_items:
        favorite_id, favorite_progress = progress_items[0]
        favorite_pig = pig_resource_manager.pig_map.get(favorite_id, {})
        favorite = {
            "name": str(favorite_pig.get("name") or favorite_id),
            "image": _image_uri(favorite_id),
            "level": get_expert_level(favorite_progress.copies),
            "copies": int(favorite_progress.copies or 0),
        }
    else:
        favorite = {"name": "暂无", "image": "", "level": 0, "copies": 0}

    levels = [get_expert_level(progress.copies) for _, progress in progress_items]
    recent_new_count = sum(
        1
        for _, progress in progress_items
        if _is_recent_new(progress.first_obtained_at, today=today)
    )
    progress_percent = round((unlocked / total_pigs) * 100, 1) if total_pigs > 0 else 0.0
    stats = {
        "unlocked": unlocked,
        "total": total_pigs,
        "progress_percent": progress_percent,
        "max_level": max(levels) if levels else 0,
        "maxed_count": sum(1 for level in levels if level >= MAX_EXPERT_LEVEL),
        "recent_new_count": recent_new_count,
        "checkin_streak": _calculate_checkin_streak(snapshot.recent_rolls, today=today),
        "roasted_7d": int(snapshot.roasted_7d or 0),
        "next_milestone": _next_milestone(unlocked, total_pigs),
        "page": page,
        "pages": pages,
    }
    return {
        "base_image": CATALOG_BASE_IMAGE.as_uri(),
        "user_name": user_name,
        "stats": stats,
        "favorite": favorite,
        "cards": cards,
    }


def _build_cache_key(payload: dict[str, Any], snapshot: CatalogSnapshot, page: int) -> str:
    """缓存指纹只使用状态摘要，不把 HTML 或图片二进制塞进 key，避免内存膨胀。"""
    key_payload = {
        "resource_version": pig_resource_manager.resource_version,
        "page": page,
        "page_size": CATALOG_PAGE_SIZE,
        "user_name": payload["user_name"],
        "stats": payload["stats"],
        "cards": [(card["id"], card["level"], card["badge"]) for card in payload["cards"]],
        "favorite": payload["favorite"],
        "recent_rolls": snapshot.recent_rolls,
        "roasted_7d": snapshot.roasted_7d,
    }
    raw = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_output_format(raw_format: str) -> str:
    """图鉴默认坚持 PNG；允许显式切到 JPEG 时统一收敛到 Pillow 可识别格式。"""
    value = str(raw_format or "png").strip().lower()
    if value in {"jpg", "jpeg"}:
        return "JPEG"
    return "PNG"


def _resize_to_catalog_size(raw_image: bytes, *, output_format: str = "PNG") -> bytes:
    """htmlrender 2x 截图后缩回 1536x1024，保证发送原图尺寸但保留抗锯齿收益。"""
    with Image.open(BytesIO(raw_image)) as image:
        image = image.convert("RGBA")
        if image.size != CATALOG_SIZE:
            image = image.resize(CATALOG_SIZE, Image.Resampling.LANCZOS)
        output = BytesIO()
        if output_format == "JPEG":
            # JPEG 不支持透明通道；底图本身是不透明成图，这里铺白只为防御异常透明像素。
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            background.save(output, format="JPEG", quality=92, optimize=True)
        else:
            # 大图 PNG 的 optimize=True 只能省出很少体积，却会在实测中阻塞数秒；
            # 这里保留默认压缩等级，把交互延迟优先级放在文件极限压缩之前。
            image.save(output, format="PNG")
        return output.getvalue()


async def render_catalog_image(
    *,
    user_name: str,
    snapshot: CatalogSnapshot,
    page: int = 1,
) -> bytes:
    """渲染图片版小猪图鉴；只读取快照，不修改抽猪状态或 copies。"""
    from nonebot_plugin_htmlrender import template_to_html

    config = get_plugin_config(Config)
    started_at = time.perf_counter()
    payload = _build_template_payload(user_name=user_name, snapshot=snapshot, page=page)
    payload_ready_at = time.perf_counter()
    output_format = _normalize_output_format(config.rollpig_catalog_output_format)
    cache_key = f"{output_format}:{_build_cache_key(payload, snapshot, page)}"
    ttl = max(0, int(config.rollpig_catalog_cache_seconds or 0))
    now = time.time()
    _prune_catalog_cache(ttl=ttl, now=now)
    cached = _catalog_cache.get(cache_key)
    if cached and ttl > 0 and now - cached.created_at <= ttl:
        log_perf(
            f"rollpig catalog cache hit: user={user_name} page={page} "
            f"payload={payload_ready_at - started_at:.2f}s bytes={len(cached.payload)}"
        )
        return cached.payload

    html_started_at = time.perf_counter()
    html = await template_to_html(str(RES_DIR), CATALOG_TEMPLATE, **payload)
    html_ready_at = time.perf_counter()
    timeout_ms = max(1000, int(float(config.rollpig_catalog_render_timeout or 8.0) * 1000))
    scale_factor = float(config.rollpig_catalog_scale_factor or 2.0)
    pool = _get_page_pool(int(config.rollpig_catalog_render_concurrency or 2), scale_factor)
    async with html_render_budget("catalog"):
        page_result = await pool.render(html, timeout_ms=timeout_ms)
    postprocess_started_at = time.perf_counter()
    result = _resize_to_catalog_size(page_result.raw_image, output_format=output_format)
    finished_at = time.perf_counter()
    log_perf(
        f"rollpig catalog rendered: user={user_name} page={page} "
        f"payload={payload_ready_at - started_at:.2f}s "
        f"html={html_ready_at - html_started_at:.2f}s "
        f"page_wait={page_result.wait_seconds:.2f}s "
        f"page={page_result.page_seconds:.2f}s "
        f"postprocess={finished_at - postprocess_started_at:.2f}s "
        f"total={finished_at - started_at:.2f}s bytes={len(result)}"
    )
    if ttl > 0:
        _catalog_cache[cache_key] = _CachedCatalogImage(created_at=time.time(), payload=result)
        _prune_catalog_cache(ttl=ttl, now=time.time())
    return result
