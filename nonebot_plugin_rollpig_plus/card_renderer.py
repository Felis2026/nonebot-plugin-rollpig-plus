from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from nonebot.log import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps


CANVAS_SIZE = (800, 800)
CARD_RADIUS = 40
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240

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
EMOJI_FONT_PATH = Path(__file__).parent / "resource" / "fonts" / "NotoColorEmoji.ttf"
EMOJI_BASE_FONT_SIZE = 109
EMOJI_RENDER_PADDING = 28
EMOJI_SCALE_FACTOR = 1.08


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
class _TextRun:
    text: str
    is_emoji: bool


# ================================ 字体与 Emoji 后端 ================================ #


def _font_candidates(*, bold: bool) -> list[Path]:
    """按平台列出候选中文字体，尽量复刻 HTML 版的 Microsoft YaHei 回退习惯。"""

    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    linux_fonts = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    return [*windows_fonts, *linux_fonts]


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


@lru_cache(maxsize=1)
def _load_emoji_font() -> ImageFont.FreeTypeFont | None:
    """加载彩色 Emoji 字体；插件内置 Noto，系统字体只作为兜底。

    NotoColorEmoji 是位图彩色字体，FreeType/Pillow 只接受 109px 这一档。
    因此后续统一先按 109px 渲染，再缩放到当前文字字号，避免运行时联网拉素材。
    """

    font_candidates = [
        EMOJI_FONT_PATH,
        Path("C:/Windows/Fonts/seguiemj.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf"),
    ]
    for font_path in font_candidates:
        if not font_path.exists():
            continue
        try:
            return ImageFont.truetype(str(font_path), size=EMOJI_BASE_FONT_SIZE)
        except Exception as error:
            logger.debug(f"RollPig Emoji 字体加载失败: path={font_path}, error={error}")

    logger.warning("RollPig Pillow 未找到可用彩色 Emoji 字体，Emoji 将按普通字体降级绘制。")
    return None


@lru_cache(maxsize=1)
def _get_emoji_list_func() -> Any | None:
    """懒加载 emoji 包；它负责识别 ZWJ、肤色、国旗等复合 Emoji 边界。"""

    try:
        from emoji import emoji_list
    except Exception as error:
        logger.warning(f"RollPig Emoji 分段依赖不可用，已降级普通文字绘制: {error}")
        return None
    return emoji_list


# ================================ 文本测量与换行 ================================ #


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """按实际像素测量文本宽度；Emoji 使用内置彩色字体的缩放宽度计入。"""

    if not text:
        return 0

    width = 0
    for run in _split_text_runs(text):
        if run.is_emoji:
            emoji_image = _render_emoji_image(run.text, _emoji_target_size(font))
            if emoji_image is not None:
                width += emoji_image.width
                continue
        width += _measure_plain_text(draw, run.text, font)
    return width


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


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#@%-]+|[^\S\n]+|\n|.", re.S)


def _has_emoji_candidate(text: str) -> bool:
    """快速判断文本是否可能含 Emoji，避免普通中文卡片无谓加载 emoji 包。"""

    if any(mark in text for mark in ("\ufe0f", "\u200d", "\u20e3")):
        return True
    return any(
        0x1F000 <= ord(char) <= 0x1FAFF
        or 0x2600 <= ord(char) <= 0x27BF
        or 0x2B00 <= ord(char) <= 0x2BFF
        or ord(char) in (0x00A9, 0x00AE, 0x3030, 0x303D, 0x3297, 0x3299)
        for char in text
    )


@lru_cache(maxsize=4096)
def _emoji_spans(text: str) -> tuple[tuple[int, int], ...]:
    """返回文本中的 Emoji 片段范围；结果缓存避免同一昵称反复解析。"""

    if not _has_emoji_candidate(text):
        return ()

    emoji_list = _get_emoji_list_func()
    if emoji_list is None:
        return ()

    try:
        spans = [
            (int(item["match_start"]), int(item["match_end"]))
            for item in emoji_list(text)
        ]
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


def _split_text_runs(text: str) -> list[_TextRun]:
    """把文本拆成普通文字与完整 Emoji 簇，保证 ZWJ/国旗/肤色不被切碎。"""

    spans = _emoji_spans(text)
    if not spans:
        return [_TextRun(text, False)] if text else []

    runs: list[_TextRun] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            runs.append(_TextRun(text[cursor:start], False))
        runs.append(_TextRun(text[start:end], True))
        cursor = end
    if cursor < len(text):
        runs.append(_TextRun(text[cursor:], False))
    return runs


def _emoji_target_size(font: ImageFont.ImageFont) -> int:
    """按当前文字字号推导 Emoji 贴图大小，避免图标挤爆行高。"""

    font_size = int(getattr(font, "size", ANALYSIS_FONT_MIN_SIZE))
    return max(18, round(font_size * EMOJI_SCALE_FACTOR))


@lru_cache(maxsize=4096)
def _render_emoji_image(emoji_text: str, target_size: int) -> Image.Image | None:
    """使用内置 NotoColorEmoji 渲染单个 Emoji 簇，并缓存缩放后的 RGBA 小图。"""

    emoji_font = _load_emoji_font()
    if emoji_font is None:
        return None

    canvas_width = EMOJI_BASE_FONT_SIZE * 4
    canvas_height = EMOJI_BASE_FONT_SIZE + EMOJI_RENDER_PADDING * 2
    image = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    try:
        draw.text(
            (EMOJI_RENDER_PADDING, EMOJI_RENDER_PADDING),
            emoji_text,
            font=emoji_font,
            embedded_color=True,
        )
    except Exception as error:
        logger.debug(f"RollPig Emoji 绘制失败，回退普通文字: emoji={emoji_text!r}, error={error}")
        return None

    bbox = image.getbbox()
    if bbox is None:
        return None

    cropped = image.crop(bbox)
    scale = target_size / max(1, cropped.height)
    target_width = max(1, round(cropped.width * scale))
    return cropped.resize((target_width, target_size), Image.Resampling.LANCZOS)


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
    canvas = Image.new("RGBA", CANVAS_SIZE, (255, 255, 255, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, CANVAS_SIZE[0] - 1, CANVAS_SIZE[1] - 1),
        radius=CARD_RADIUS,
        fill=BACKGROUND_COLOR,
    )
    return canvas


def _load_avatar(image_file: Path | None) -> Image.Image | None:
    """载入并裁切小猪图；P1 阶段 GIF 只取首帧作为静态图。"""

    if image_file is None:
        return None

    try:
        with Image.open(image_file) as opened:
            frame = opened.copy()
    except Exception as error:
        logger.warning(f"RollPig 小猪图片读取失败，使用占位图: file={image_file}, error={error}")
        return None

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


def _draw_avatar(canvas: Image.Image, avatar: Image.Image | None, y: int) -> None:
    x = (CANVAS_SIZE[0] - AVATAR_SIZE) // 2
    if avatar is not None:
        canvas.alpha_composite(avatar, (x, y))
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
) -> None:
    """水平居中绘制单行文本；中文与 Emoji 分段绘制，避免依赖浏览器字体回退。"""

    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2
    cursor_x = x

    for run in _split_text_runs(text):
        if run.is_emoji:
            emoji_image = _render_emoji_image(run.text, _emoji_target_size(font))
            if emoji_image is not None:
                emoji_y = int(y + (line_height - emoji_image.height) / 2)
                canvas.alpha_composite(emoji_image, (round(cursor_x), emoji_y))
                cursor_x += emoji_image.width
                continue

        # 普通文本按各自 bbox 垂直居中；Emoji 缺字时也会走这里作为最后兜底。
        bbox = draw.textbbox((0, 0), run.text, font=font)
        text_h = max(1, bbox[3] - bbox[1])
        text_y = int(y + (line_height - text_h) / 2 - bbox[1])
        draw.text((cursor_x, text_y), run.text, fill=fill, font=font)
        cursor_x += _measure_plain_text(draw, run.text, font)


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """同步生成 800×800 PNG；外层 async API 会放入线程执行。"""

    name = str(pig_data.get("name") or "未知小猪")
    desc = str(pig_data.get("description") or "")
    analysis = str(pig_data.get("analysis") or "你今天是只神秘小猪。")

    canvas = _make_canvas()
    draw = ImageDraw.Draw(canvas)
    layout = _build_text_layout(draw, name=name, desc=desc, analysis=analysis)

    start_y = max(20, (CANVAS_SIZE[1] - layout.total_height) // 2)
    y = start_y

    _draw_avatar(canvas, _load_avatar(image_file), y)
    y += AVATAR_SIZE + NAME_MARGIN_TOP

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
            )
            y += layout.analysis_line_height

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    contains_emoji = any(
        _has_emoji_candidate(text)
        for text in (layout.name_line, layout.desc_line, *layout.analysis_lines)
    )
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=layout.analysis_font_size,
        analysis_lines=len(layout.analysis_lines),
        emoji_enabled=contains_emoji and _load_emoji_font() is not None and _get_emoji_list_func() is not None,
    )


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """异步入口：普通 PNG 也放到线程中，避免文件读取和 Emoji 拉取阻塞事件循环。"""

    return await asyncio.to_thread(_render_pig_card_image_sync, pig_data, image_file)
