from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from nonebot import get_plugin_config
from nonebot.log import logger

from .config import Config


# ================================ Chromium 渲染总预算 ================================ #
# 图鉴有自己的页面池；普通小猪卡片已迁移到 Pillow，不再占用 Chromium。
# 这里直接复用图鉴并发配置作为外围预算，避免维护两套含义接近的参数。

_html_render_semaphore: asyncio.Semaphore | None = None
_html_render_limit: int | None = None
_html_render_lock = asyncio.Lock()


def _resolve_html_render_limit() -> int:
    try:
        config = get_plugin_config(Config)
        raw_limit = config.rollpig_catalog_render_concurrency
    except Exception as error:
        logger.warning(f"rollpig_catalog_render_concurrency 配置读取失败，已回退到 2: {error}")
        raw_limit = 2

    try:
        return max(1, min(6, int(raw_limit or 2)))
    except (TypeError, ValueError):
        logger.warning(f"rollpig_catalog_render_concurrency 配置非法，已回退到 2: {raw_limit}")
        return 2


async def _get_html_render_semaphore() -> asyncio.Semaphore:
    global _html_render_limit, _html_render_semaphore
    limit = _resolve_html_render_limit()
    async with _html_render_lock:
        if _html_render_semaphore is None or _html_render_limit != limit:
            _html_render_semaphore = asyncio.Semaphore(limit)
            _html_render_limit = limit
    return _html_render_semaphore


@asynccontextmanager
async def html_render_budget(label: str) -> AsyncIterator[None]:
    """进入全局 HTML 渲染预算；异常和超时都必须释放 semaphore。"""
    semaphore = await _get_html_render_semaphore()
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
