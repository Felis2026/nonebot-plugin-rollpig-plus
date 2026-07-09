from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Mapping

from nonebot.log import logger
from pilmoji import Pilmoji
from pilmoji.helpers import EMOJI_REGEX, getsize as pilmoji_getsize
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageSequence

RESOURCE_DIR = Path(__file__).parent / "resource"


CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240
AVATAR_CORNER_RADIUS = 30
AVATAR_CACHE_MAXSIZE = 192
ANIMATED_AVATAR_CACHE_MAXSIZE = 24
GIF_MAX_FRAMES = 80
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100

NAME_FONT_SIZE = 48
DESC_FONT_SIZE = 30
ANALYSIS_FONT_MAX_SIZE = 28
ANALYSIS_FONT_MIN_SIZE = 24

NAME_MARGIN_TOP = 20
DESC_MARGIN_TOP = 20
ANALYSIS_MARGIN_TOP = 30

NAME_LINE_HEIGHT = 58
DESC_LINE_HEIGHT = 38
ANALYSIS_LINE_HEIGHT_FACTOR = 1.5

BACKGROUND_COLOR = (255, 255, 255, 255)
NAME_COLOR = (32, 32, 32, 255)
DESC_COLOR = (85, 85, 85, 255)
ANALYSIS_COLOR = (51, 51, 51, 255)
PLACEHOLDER_BG = (255, 226, 239, 255)
PLACEHOLDER_FG = (154, 92, 135, 255)
EMOJI_SCALE_FACTOR = 1.08
EMOJI_POSITION_OFFSET = (0, -2)
EXTRA_EMOJI_SYMBOLS = {
    # pig.json 中少量文本音乐符号不是标准 Emoji，中文字体又可能缺字；渲染时映射为 Noto Emoji 音符，不修改原始文案数据。
    "\u266a": "\U0001f3b5",
}
PACKAGE_FONT_DIR = RESOURCE_DIR / "fonts"


@dataclass(frozen=True)
class PigCardRenderResult:
    """普通小猪卡片渲染结果；保留格式字段方便后续接入 GIF。"""

    data: bytes
    image_format: str
    renderer: str
    analysis_font_size: int
    analysis_lines: int
    emoji_enabled: bool


@dataclass(frozen=True)
class _TextLayout:
    name_line: str
    desc_line: str
    analysis_lines: list[str]
    analysis_font: ImageFont.ImageFont
    analysis_font_size: int
    analysis_line_height: int
    total_height: int


@dataclass(frozen=True)
class _PreparedCard:
    """不含头像的卡片底层；GIF 渲染时逐帧复用，避免重复排版和绘制文字。"""

    canvas: Image.Image
    layout: _TextLayout
    avatar_y: int
    emoji_enabled: bool


# ================================ 字体与 Emoji 后端 ================================ #


# ================================ 彩色 Emoji 渲染 ================================ #
import threading
import zipfile

from pilmoji.source import BaseSource


NOTO_EMOJI_ZIP_PATH = RESOURCE_DIR / "emoji" / "google-emoji.zip"
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


def _resolve_font_path(value: str | None) -> Path | None:
    """解析用户配置的字体路径；相对路径按 Bot 运行目录而不是插件目录处理。"""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    raw_path = Path(text).expanduser()
    return raw_path if raw_path.is_absolute() else Path.cwd() / raw_path


def _configured_font_candidates(*, bold: bool) -> list[Path]:
    """读取用户显式指定的字体；插件配置已在 config.py 集中加载一次。"""

    from .config import plugin_config

    candidates: list[Path] = []
    # 保留字体配置项：Pillow 无完整字体族管理，标题和正文共享同一字体更可预测。
    configured_path = _resolve_font_path(plugin_config.rollpig_card_font_path)
    if configured_path is not None:
        candidates.append(configured_path)
    return list(dict.fromkeys(candidates))


def _font_candidates(*, bold: bool) -> list[Path]:
    """按优先级列出候选字体；用户配置最高，系统字体兜底。"""

    packaged_fonts = [
        PACKAGE_FONT_DIR / "SourceHanSansSC-Medium.otf",
        PACKAGE_FONT_DIR / ("msyhbd.ttc" if bold else "msyh.ttc"),
        PACKAGE_FONT_DIR / "msyh.ttc",
        PACKAGE_FONT_DIR / "msyhbd.ttc",
        # 必须显式列路径，否则容器里仍会退回默认位图字体。
        Path.cwd() / "fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path.cwd() / "fonts" / "msyh.ttc",
        Path.cwd() / "fonts" / "msyhbd.ttc",
        Path.cwd() / "fonts" / "MicrosoftYaHei" / "Microsoft Yahei.ttf",
        Path("/app/fonts") / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/app/fonts/msyh.ttc"),
        Path("/app/fonts/msyhbd.ttc"),
        Path("/app/fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
        Path("/root/.fonts") / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/root/.fonts/msyh.ttc"),
        Path("/root/.fonts/msyhbd.ttc"),
        Path("/root/.fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
    ]
    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    linux_fonts = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf" if bold else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Bold.otf" if bold else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    return [*_configured_font_candidates(bold=bold), *packaged_fonts, *windows_fonts, *linux_fonts]


@lru_cache(maxsize=32)
def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """加载指定字号字体；找不到中文字体时降级但不中断渲染。"""

    for font_path in _font_candidates(bold=bold):
        if not font_path.exists():
            continue
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception as error:
            logger.debug(f"RollPig Pillow 字体加载失败: path={font_path}, error={error}")

    logger.warning("RollPig Pillow 未找到可用中文字体，已退回 Pillow 默认字体。")
    return ImageFont.load_default()


# ================================ 文本测量与换行 ================================ #


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """按实际像素测量文本宽度；Emoji 使用 pilmoji 的贴图宽度计入。"""

    if not text:
        return 0

    if _has_emoji_candidate(text) and get_noto_emoji_source() is not None:
        try:
            render_text = _normalize_extra_emoji_symbols(text)
            width, _ = pilmoji_getsize(render_text, font=font, spacing=0, emoji_scale_factor=EMOJI_SCALE_FACTOR)
            return int(width)
        except Exception as error:
            logger.debug(f"RollPig pilmoji 文本测量失败，回退普通测量: text={text!r}, error={error}")

    return _measure_plain_text(draw, text, font)


def _measure_plain_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """测量非 Emoji 文本；Pillow 版本差异导致 textlength 失败时回退 bbox。"""

    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0])


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    sample = text or "国"
    bbox = draw.textbbox((0, 0), sample, font=font)
    return max(1, bbox[3] - bbox[1])


def _font_line_top(font: ImageFont.ImageFont, y: int, line_height: int) -> int:
    """按字体整体指标定位行顶；正文多行用它固定基线，避免每行按内容 bbox 上下漂移。"""

    try:
        ascent, descent = font.getmetrics()
        font_height = ascent + descent
    except Exception:
        # Pillow 默认字体等少数对象没有 getmetrics；用中英文混合样本估一个稳定高度。
        probe = "国Hg"
        mask = font.getmask(probe)
        bbox = mask.getbbox() or (0, 0, 1, line_height)
        font_height = max(1, bbox[3] - bbox[1])
    return int(y + max(0, line_height - font_height) / 2)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#@%-]+|[^\S\n]+|\n|.", re.S)


def _has_emoji_candidate(text: str) -> bool:
    """快速判断文本是否可能含 Emoji，避免普通中文卡片无谓加载 emoji 包。"""

    if any(symbol in text for symbol in EXTRA_EMOJI_SYMBOLS):
        return True
    if any(mark in text for mark in ("\ufe0f", "\u200d", "\u20e3")):
        return True
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF
        or 0x2600 <= ord(char) <= 0x27BF
        or 0x2B00 <= ord(char) <= 0x2BFF
        or ord(char) in (0x00A9, 0x00AE, 0x3030, 0x303D, 0x3297, 0x3299)
        for char in text
    )


def _normalize_extra_emoji_symbols(text: str) -> str:
    """把少量 emoji 风格文本符号转换为 Noto Emoji 可识别字符。"""

    if not text or not any(symbol in text for symbol in EXTRA_EMOJI_SYMBOLS):
        return text
    return "".join(EXTRA_EMOJI_SYMBOLS.get(char, char) for char in text)


@lru_cache(maxsize=4096)
def _emoji_spans(text: str) -> tuple[tuple[int, int], ...]:
    """返回文本中的 Emoji 片段范围；与 pilmoji 使用同一套解析规则。"""

    if not _has_emoji_candidate(text):
        return ()

    normalized_text = _normalize_extra_emoji_symbols(text)
    try:
        spans = [match.span() for match in EMOJI_REGEX.finditer(normalized_text)]
    except Exception as error:
        logger.debug(f"RollPig Emoji 分段失败，回退普通文字: text={text!r}, error={error}")
        return ()

    # 防御第三方库异常返回，避免错位范围破坏中文切片。
    valid_spans: list[tuple[int, int]] = []
    last_end = 0
    for start, end in sorted(spans):
        if 0 <= start < end <= len(text) and start >= last_end:
            valid_spans.append((start, end))
            last_end = end
    return tuple(valid_spans)


def _tokenize_text(text: str) -> list[str]:
    """中文逐字、英文数字成词；Emoji 按完整簇成词，避免换行切碎。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    spans = _emoji_spans(normalized)
    if not spans:
        return _TOKEN_RE.findall(normalized)

    tokens: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            tokens.extend(_TOKEN_RE.findall(normalized[cursor:start]))
        tokens.append(normalized[start:end])
        cursor = end
    if cursor < len(normalized):
        tokens.extend(_TOKEN_RE.findall(normalized[cursor:]))
    return tokens


def _drop_last_text_unit(text: str) -> str:
    """删除最后一个显示单元；末尾是 Emoji 簇时整簇删除，避免截断出残缺符号。"""

    if not text:
        return ""

    spans = _emoji_spans(text)
    if spans and spans[-1][1] == len(text):
        return text[:spans[-1][0]]
    return text[:-1]


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """把单行文本收敛到给定宽度，末尾使用中文省略号。"""

    if _measure_text(draw, text, font) <= max_width:
        return text

    ellipsis = "…"
    result = text
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _append_ellipsis_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """强制给截断行追加省略号，并保证追加后仍不超出最大宽度。"""

    ellipsis = "…"
    result = text.rstrip()
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _wrap_text_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int | None = None,
) -> list[str]:
    """按像素宽度换行；超过最大行数时在最后一行省略。"""

    if not text:
        return []

    lines: list[str] = []
    current = ""

    def push_line(line: str) -> bool:
        lines.append(line.rstrip())
        return max_lines is not None and len(lines) >= max_lines

    for token in _tokenize_text(text):
        if token == "\n":
            if push_line(current):
                break
            current = ""
            continue

        candidate = f"{current}{token}"
        if current and _measure_text(draw, candidate.rstrip(), font) > max_width:
            if push_line(current):
                break
            current = token.lstrip()
            continue
        current = candidate
    else:
        if current or not lines:
            push_line(current)

    if max_lines is not None and len(lines) >= max_lines:
        consumed = "".join(lines)
        source_compact = text.replace("\n", "")
        if len(consumed) < len(source_compact):
            lines[-1] = _append_ellipsis_to_width(draw, lines[-1].rstrip(), font, max_width)

    return lines


# ================================ 图片与布局生成 ================================ #


def _make_canvas() -> Image.Image:
    """创建满版白底卡片画布；外框不再做透明裁切、描边或圆角，避免产生内收感。"""

    return Image.new("RGBA", CANVAS_SIZE, BACKGROUND_COLOR)


def _load_avatar(image_file: Path | None) -> Image.Image | None:
    """载入并裁切小猪图；缓存键包含 mtime/size，资源更新后会自动失效。"""

    if image_file is None:
        return None

    try:
        stat = image_file.stat()
    except OSError as error:
        logger.warning(f"RollPig 小猪图片状态读取失败，使用占位图: file={image_file}, error={error}")
        return None

    cached = _load_avatar_cached(str(image_file), stat.st_mtime_ns, stat.st_size)
    return cached.copy() if cached is not None else None


def _fit_avatar_frame(frame: Image.Image) -> Image.Image:
    """把任意来源图片统一规整为 240×240 RGBA 头像帧。"""

    frame = ImageOps.exif_transpose(frame)
    if frame.mode not in ("RGBA", "LA"):
        frame = frame.convert("RGBA")
    else:
        frame = frame.convert("RGBA")

    return ImageOps.fit(
        frame,
        (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


@lru_cache(maxsize=AVATAR_CACHE_MAXSIZE)
def _load_avatar_cached(path: str, mtime_ns: int, file_size: int) -> Image.Image | None:
    """读取并缩放头像资源；mtime/size 参数只用于构成 LRU 缓存失效键。"""

    image_file = Path(path)
    try:
        with Image.open(image_file) as opened:
            frame = opened.copy()
    except Exception as error:
        logger.warning(f"RollPig 小猪图片读取失败，使用占位图: file={image_file}, error={error}")
        return None

    return _fit_avatar_frame(frame)


def _normalize_gif_duration(raw_duration: object) -> int:
    """规整 GIF 帧间隔；防御 0ms/异常值让客户端播放过快或卡住。"""

    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


def _load_animated_avatar_frames(image_file: Path | None) -> tuple[tuple[Image.Image, int], ...]:
    """载入动态头像帧；非 GIF 或单帧 GIF 返回空元组并交给静态 PNG 路径处理。"""

    if image_file is None or image_file.suffix.lower() != ".gif":
        return ()

    try:
        stat = image_file.stat()
    except OSError as error:
        logger.warning(f"RollPig GIF 图片状态读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()

    cached = _load_animated_avatar_frames_cached(str(image_file), stat.st_mtime_ns, stat.st_size)
    return tuple((frame.copy(), duration) for frame, duration in cached)


@lru_cache(maxsize=ANIMATED_AVATAR_CACHE_MAXSIZE)
def _load_animated_avatar_frames_cached(path: str, mtime_ns: int, file_size: int) -> tuple[tuple[Image.Image, int], ...]:
    """读取 GIF 全帧并统一裁切；mtime/size 参数只用于构成 LRU 缓存失效键。"""

    image_file = Path(path)
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            if not getattr(opened, "is_animated", False) or frame_count <= 1:
                return ()
            if frame_count > GIF_MAX_FRAMES:
                logger.warning(f"RollPig GIF 帧数超过上限，已截断: file={image_file}, frames={frame_count}/{GIF_MAX_FRAMES}")

            frames: list[tuple[Image.Image, int]] = []
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                if index >= GIF_MAX_FRAMES:
                    break
                duration = _normalize_gif_duration(frame.info.get("duration", opened.info.get("duration")))
                frames.append((_fit_avatar_frame(frame.copy()), duration))
    except Exception as error:
        logger.warning(f"RollPig GIF 图片读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()

    return tuple(frames)


@lru_cache(maxsize=8)
def _avatar_corner_mask() -> Image.Image:
    """生成抗锯齿圆角蒙版；透明小猪图不受影响，实底图片会获得柔和圆角。"""

    scale = 3
    mask = Image.new("L", (AVATAR_SIZE * scale, AVATAR_SIZE * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, AVATAR_SIZE * scale, AVATAR_SIZE * scale),
        radius=AVATAR_CORNER_RADIUS * scale,
        fill=255,
    )
    return mask.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)


def _prepare_avatar_for_draw(avatar: Image.Image) -> Image.Image:
    """给头像套圆角边界；使用 ImageChops 保留原始透明通道。"""

    prepared = avatar.copy()
    alpha = prepared.getchannel("A")
    prepared.putalpha(ImageChops.multiply(alpha, _avatar_corner_mask()))
    return prepared


def _draw_avatar(canvas: Image.Image, avatar: Image.Image | None, y: int) -> None:
    x = (CANVAS_SIZE[0] - AVATAR_SIZE) // 2
    if avatar is not None:
        prepared_avatar = _prepare_avatar_for_draw(avatar)
        canvas.alpha_composite(prepared_avatar, (x, y))
        return

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (x, y, x + AVATAR_SIZE, y + AVATAR_SIZE),
        radius=36,
        fill=PLACEHOLDER_BG,
    )
    placeholder_font = _load_font(86, bold=True)
    _draw_text_line(
        canvas,
        "猪",
        placeholder_font,
        y + (AVATAR_SIZE - 100) // 2,
        100,
        PLACEHOLDER_FG,
        max_width=AVATAR_SIZE,
    )


def _build_text_layout(
    draw: ImageDraw.ImageDraw,
    *,
    name: str,
    desc: str,
    analysis: str,
) -> _TextLayout:
    """计算普通卡片排版；分析正文优先缩字号，最后才省略。"""

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    desc_font = _load_font(DESC_FONT_SIZE)

    name_line = _truncate_to_width(draw, name, name_font, CONTENT_WIDTH)
    desc_line = _truncate_to_width(draw, desc, desc_font, CONTENT_WIDTH) if desc else ""

    static_height = AVATAR_SIZE + NAME_MARGIN_TOP + NAME_LINE_HEIGHT
    if desc_line:
        static_height += DESC_MARGIN_TOP + DESC_LINE_HEIGHT
    if analysis:
        static_height += ANALYSIS_MARGIN_TOP

    for analysis_font_size in range(ANALYSIS_FONT_MAX_SIZE, ANALYSIS_FONT_MIN_SIZE - 1, -1):
        analysis_font = _load_font(analysis_font_size)
        analysis_line_height = round(analysis_font_size * ANALYSIS_LINE_HEIGHT_FACTOR)
        analysis_lines = _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH) if analysis else []
        total_height = static_height + len(analysis_lines) * analysis_line_height
        if total_height <= CONTENT_SAFE_HEIGHT:
            return _TextLayout(
                name_line=name_line,
                desc_line=desc_line,
                analysis_lines=analysis_lines,
                analysis_font=analysis_font,
                analysis_font_size=analysis_font_size,
                analysis_line_height=analysis_line_height,
                total_height=total_height,
            )

    analysis_font = _load_font(ANALYSIS_FONT_MIN_SIZE)
    analysis_line_height = round(ANALYSIS_FONT_MIN_SIZE * ANALYSIS_LINE_HEIGHT_FACTOR)
    available_height = max(analysis_line_height, CONTENT_SAFE_HEIGHT - static_height)
    max_lines = max(1, available_height // analysis_line_height)
    analysis_lines = _wrap_text_by_width(
        draw,
        analysis,
        analysis_font,
        CONTENT_WIDTH,
        max_lines=max_lines,
    ) if analysis else []
    total_height = static_height + len(analysis_lines) * analysis_line_height
    return _TextLayout(
        name_line=name_line,
        desc_line=desc_line,
        analysis_lines=analysis_lines,
        analysis_font=analysis_font,
        analysis_font_size=ANALYSIS_FONT_MIN_SIZE,
        analysis_line_height=analysis_line_height,
        total_height=total_height,
    )


# ================================ 绘制与对外入口 ================================ #


def _draw_text_line(
    canvas: Image.Image,
    text: str,
    font: ImageFont.ImageFont,
    y: int,
    line_height: int,
    fill: tuple[int, int, int, int],
    *,
    max_width: int,
    align_by_baseline: bool = False,
) -> None:
    """水平居中绘制单行文本；正文可按统一基线绘制，避免混合字符导致行距视觉漂移。"""

    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2

    if _has_emoji_candidate(text):
        source = get_noto_emoji_source()
        if source is not None:
            try:
                render_text = _normalize_extra_emoji_symbols(text)
                _, measured_height = pilmoji_getsize(
                    render_text,
                    font=font,
                    spacing=0,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                )
                text_y = _font_line_top(font, y, line_height) if align_by_baseline else int(y + (line_height - measured_height) / 2)
                with Pilmoji(
                    canvas,
                    source=source,
                    render_discord_emoji=False,
                    emoji_scale_factor=EMOJI_SCALE_FACTOR,
                    emoji_position_offset=EMOJI_POSITION_OFFSET,
                ) as pilmoji:
                    pilmoji.text(
                        (x, text_y),
                        render_text,
                        fill=fill,
                        font=font,
                        emoji_scale_factor=EMOJI_SCALE_FACTOR,
                        emoji_position_offset=EMOJI_POSITION_OFFSET,
                    )
                return
            except Exception as error:
                logger.debug(f"RollPig pilmoji 文本绘制失败，回退普通绘制: text={text!r}, error={error}")

    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = max(1, bbox[3] - bbox[1])
    if align_by_baseline:
        text_y = _font_line_top(font, y, line_height)
    else:
        text_y = int(y + (line_height - text_h) / 2 - bbox[1])
    draw.text((x, text_y), text, fill=fill, font=font)


def _prepare_card_without_avatar(pig_data: Mapping[str, Any]) -> _PreparedCard:
    """生成不含头像的静态卡片层；动态头像逐帧贴在同一位置。"""

    name = str(pig_data.get("name") or "未知小猪")
    desc = str(pig_data.get("description") or "")
    analysis = str(pig_data.get("analysis") or "你今天是只神秘小猪。")

    canvas = _make_canvas()
    draw = ImageDraw.Draw(canvas)
    layout = _build_text_layout(draw, name=name, desc=desc, analysis=analysis)

    start_y = max(20, (CANVAS_SIZE[1] - layout.total_height) // 2)
    y = start_y + AVATAR_SIZE + NAME_MARGIN_TOP

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    _draw_text_line(canvas, layout.name_line, name_font, y, NAME_LINE_HEIGHT, NAME_COLOR, max_width=CONTENT_WIDTH)
    y += NAME_LINE_HEIGHT

    if layout.desc_line:
        y += DESC_MARGIN_TOP
        desc_font = _load_font(DESC_FONT_SIZE)
        _draw_text_line(canvas, layout.desc_line, desc_font, y, DESC_LINE_HEIGHT, DESC_COLOR, max_width=CONTENT_WIDTH)
        y += DESC_LINE_HEIGHT

    if layout.analysis_lines:
        y += ANALYSIS_MARGIN_TOP
        for line in layout.analysis_lines:
            _draw_text_line(
                canvas,
                line,
                layout.analysis_font,
                y,
                layout.analysis_line_height,
                ANALYSIS_COLOR,
                max_width=CONTENT_WIDTH,
                align_by_baseline=True,
            )
            y += layout.analysis_line_height

    contains_emoji = any(
        _has_emoji_candidate(text)
        for text in (layout.name_line, layout.desc_line, *layout.analysis_lines)
    )
    return _PreparedCard(
        canvas=canvas,
        layout=layout,
        avatar_y=start_y,
        emoji_enabled=contains_emoji and get_noto_emoji_source() is not None,
    )


def _encode_png_card(prepared: _PreparedCard, image_file: Path | None) -> PigCardRenderResult:
    """把静态卡片编码为 PNG；静态 GIF 也会走这里取首帧。"""

    canvas = prepared.canvas.copy()
    _draw_avatar(canvas, _load_avatar(image_file), prepared.avatar_y)

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
        emoji_enabled=prepared.emoji_enabled,
    )


def _build_gif_palette(rgb_frames: list[Image.Image], avatar_y: int) -> Image.Image:
    """从静态文字层和全部头像帧采样调色板，避免彩色动画被第一帧压没。"""

    sample_size = AVATAR_SIZE
    palette_source = Image.new("RGB", (sample_size, sample_size * (len(rgb_frames) + 1)), BACKGROUND_COLOR[:3])

    # 第一块放整卡缩略图，确保文字黑灰、白底、Emoji 等静态元素能进入全局调色板。
    full_card_sample = rgb_frames[0].resize((sample_size, sample_size), Image.Resampling.LANCZOS)
    palette_source.paste(full_card_sample, (0, 0))

    # 后续每块只采样头像区域；动态色彩主要集中在这里，完整采样可保住蹦迪类彩色帧。
    avatar_box = (
        (CANVAS_SIZE[0] - AVATAR_SIZE) // 2,
        avatar_y,
        (CANVAS_SIZE[0] + AVATAR_SIZE) // 2,
        avatar_y + AVATAR_SIZE,
    )
    for index, frame in enumerate(rgb_frames, 1):
        palette_source.paste(frame.crop(avatar_box), (0, sample_size * index))

    return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)


def _encode_gif_card(prepared: _PreparedCard, avatar_frames: tuple[tuple[Image.Image, int], ...]) -> PigCardRenderResult:
    """把动态头像逐帧合成到静态卡片层，输出 GIF。"""

    rgb_frames: list[Image.Image] = []
    durations: list[int] = []
    for avatar, duration in avatar_frames:
        frame = prepared.canvas.copy()
        _draw_avatar(frame, avatar, prepared.avatar_y)
        rgb_frames.append(frame.convert("RGB"))
        durations.append(duration)

    # 使用同一张全帧采样调色板，兼顾文字不闪色和动态头像不丢彩。
    palette = _build_gif_palette(rgb_frames, prepared.avatar_y)
    output_frames = [
        frame.quantize(palette=palette, dither=Image.Dither.NONE)
        for frame in rgb_frames
    ]

    output = BytesIO()
    output_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=output_frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="gif",
        renderer="pillow-gif",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
        emoji_enabled=prepared.emoji_enabled,
    )


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """同步生成 800×800 卡片；动态 GIF 资源输出 GIF，其余输出 PNG。"""

    prepared = _prepare_card_without_avatar(pig_data)
    avatar_frames = _load_animated_avatar_frames(image_file)
    if avatar_frames:
        return _encode_gif_card(prepared, avatar_frames)
    return _encode_png_card(prepared, image_file)


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """异步入口：普通 PNG 也放到线程中，避免文件读取和 Emoji 拉取阻塞事件循环。"""

    return await asyncio.to_thread(_render_pig_card_image_sync, pig_data, image_file)


# ================================ 本周小猪长图 ================================ #
ITEM_SIZE = (150, 150)
PADDING = 20
BOTTOM_TEXT_SPACE = 80


def _render_weekly_pig_image_sync(image_paths: list[Path]) -> bytes:
    """同步绘制本周小猪长图；外层必须丢到线程，避免阻塞 NoneBot 事件循环。"""

    item_width, item_height = ITEM_SIZE
    total_width = (item_width + PADDING) * len(image_paths) + PADDING
    total_height = item_height + BOTTOM_TEXT_SPACE

    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    for index, image_path in enumerate(image_paths):
        with Image.open(image_path) as opened:
            image = opened.convert("RGBA").resize(ITEM_SIZE)
            x = PADDING + index * (item_width + PADDING)
            y = PADDING
            canvas.paste(image, (x, y), image)

    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


async def render_weekly_pig_image(image_paths: list[Path]) -> bytes:
    """异步渲染本周小猪长图。"""

    return await asyncio.to_thread(_render_weekly_pig_image_sync, image_paths)
