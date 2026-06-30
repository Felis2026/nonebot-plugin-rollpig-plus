from __future__ import annotations

import threading
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from nonebot.log import logger
from pilmoji.source import BaseSource


NOTO_EMOJI_ZIP_PATH = Path(__file__).parent / "resource" / "emoji" / "google-emoji.zip"
_VARIATION_SELECTORS = {0xFE0E, 0xFE0F}


def _emoji_to_noto_codepoint(emoji_text: str, *, strip_variation: bool) -> str:
    """把 Unicode Emoji 转成 Noto Emoji PNG 使用的 `emoji_uxxxx` 资源名片段。"""

    codepoints: list[str] = []
    for char in emoji_text:
        codepoint = ord(char)
        if strip_variation and codepoint in _VARIATION_SELECTORS:
            continue
        # Noto PNG 对 BMP 字符使用 4 位补零，例如 #️⃣ 对应 emoji_u0023_20e3.png。
        codepoints.append(f"{codepoint:04x}")
    return "_".join(codepoints)


def _noto_asset_candidates(emoji_text: str) -> tuple[str, ...]:
    """按 Noto Emoji 命名规则给出候选资源名，兼容 FE0F/FE0E 变体选择符。

    Noto 的 png/128 目录通常会省略 emoji presentation 的 FE0F，
    但不能在输入阶段粗暴删除；这里保留“精确匹配 -> 去变体选择符”的顺序，
    能兼容普通 emoji、keycap 和 ZWJ 组合。
    """

    exact = _emoji_to_noto_codepoint(emoji_text, strip_variation=False)
    stripped = _emoji_to_noto_codepoint(emoji_text, strip_variation=True)
    exact_asset = f"emoji_u{exact}.png"
    stripped_asset = f"emoji_u{stripped}.png"
    if exact_asset == stripped_asset:
        return (exact_asset,)
    return (exact_asset, stripped_asset)


class ZipNotoEmojiSource(BaseSource):
    """从内置 ZIP 按需读取 Noto Emoji PNG，避免运行时联网和仓库碎文件。"""

    def __init__(self, zip_path: Path = NOTO_EMOJI_ZIP_PATH) -> None:
        self.zip_path = zip_path
        self._zip_file = zipfile.ZipFile(zip_path, "r")
        self._lock = threading.RLock()

    @lru_cache(maxsize=1024)
    def _read_asset(self, asset_name: str) -> bytes | None:
        """读取并缓存常见 Emoji PNG 字节；每次返回时再包装成新的 BytesIO。"""

        try:
            with self._lock:
                return self._zip_file.read(asset_name)
        except KeyError:
            return None

    def get_emoji(self, emoji: str, /) -> BytesIO | None:
        for asset_name in _noto_asset_candidates(emoji):
            data = self._read_asset(asset_name)
            if data is not None:
                return BytesIO(data)
        logger.debug(f"RollPig Noto Emoji 资源缺失，回退普通字体绘制: emoji={emoji!r}")
        return None

    def get_discord_emoji(self, id: int, /) -> BytesIO | None:
        return None


@lru_cache(maxsize=1)
def get_noto_emoji_source() -> ZipNotoEmojiSource | None:
    """加载本地 Noto Emoji 源；资源缺失时只降级，不影响普通卡片生成。"""

    if not NOTO_EMOJI_ZIP_PATH.exists():
        logger.warning(f"RollPig Noto Emoji ZIP 不存在，Emoji 将按普通字体降级绘制: {NOTO_EMOJI_ZIP_PATH}")
        return None
    try:
        return ZipNotoEmojiSource(NOTO_EMOJI_ZIP_PATH)
    except Exception as error:
        logger.warning(f"RollPig Noto Emoji ZIP 加载失败，Emoji 将按普通字体降级绘制: {error}")
        return None
