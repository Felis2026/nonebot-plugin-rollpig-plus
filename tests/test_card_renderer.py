from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from test_ex_variants import card_module


class PigArtworkCornersTests(unittest.TestCase):
    def test_draw_preserves_source_alpha_and_all_corners(self):
        with (
            Image.new("RGBA", card_module.CANVAS_SIZE, "white") as canvas,
            Image.new("RGBA", (card_module.AVATAR_SIZE,) * 2, (200, 40, 60, 255)) as avatar,
        ):
            avatar.putpixel((0, 0), (200, 40, 60, 0))
            avatar.putpixel((239, 0), (200, 40, 60, 128))
            source_bytes = avatar.tobytes()
            x = (canvas.width - avatar.width) // 2
            y = 30
            card_module._draw_avatar(canvas, avatar, y)
            self.assertEqual(canvas.getpixel((x, y)), (255, 255, 255, 255))
            self.assertEqual(canvas.getpixel((x + 239, y)), (227, 147, 157, 255))
            for dx in (0, 239):
                self.assertEqual(canvas.getpixel((x + dx, y + 239)), (200, 40, 60, 255))
            self.assertEqual(avatar.tobytes(), source_bytes)

    def test_png_keeps_opaque_artwork_corners(self):
        self._check_encoded_corners("PNG", [(220, 30, 50)])

    def test_gif_keeps_artwork_corners_in_every_frame(self):
        self._check_encoded_corners("GIF", [(220, 30, 50), (30, 50, 220)])

    def _check_encoded_corners(self, image_format: str, colors: list[tuple[int, int, int]]):
        """核对最终 PNG 或 GIF 每帧的立绘四角，覆盖编码与调色板流程。"""

        pig = {"name": "测试猪", "description": "立绘四角", "analysis": "保留完整图片。"}
        with tempfile.TemporaryDirectory() as temporary:
            image_file = Path(temporary) / f"pig.{image_format.lower()}"
            frames = [Image.new("RGB", (240, 240), color) for color in colors]
            try:
                frames[0].save(
                    image_file,
                    format=image_format,
                    **({"save_all": True, "append_images": frames[1:], "duration": 100, "loop": 0}
                       if image_format == "GIF" else {}),
                )
            finally:
                for frame in frames:
                    frame.close()
            prepared = card_module._prepare_card_without_avatar(pig)
            y = prepared.avatar_y
            prepared.canvas.close()
            result = card_module._render_pig_card_image_sync(pig, image_file, cache_final_card=False)
            with Image.open(BytesIO(result.data)) as output:
                self.assertEqual(output.n_frames, len(colors))
                for index, expected in enumerate(colors):
                    output.seek(index)
                    with output.convert("RGB") as frame:
                        for dx, dy in ((0, 0), (239, 0), (0, 239), (239, 239)):
                            pixel = frame.getpixel((280 + dx, y + dy))
                            self.assertTrue(all(abs(a - b) <= 5 for a, b in zip(pixel, expected)))

    def test_cache_key_does_not_reuse_rounded_card_version(self):
        pig = {"name": "测试猪"}
        current_key = card_module._fixed_card_cache_key(pig, None)
        with patch.object(card_module, "CARD_CACHE_VERSION", 4):
            rounded_key = card_module._fixed_card_cache_key(pig, None)
        self.assertNotEqual(current_key, rounded_key)


if __name__ == "__main__":
    unittest.main()
