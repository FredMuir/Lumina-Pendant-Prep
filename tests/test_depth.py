import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from lumina_pendant_prep.depth import model_path, estimate_relative_depth, align_depth, depth_heightmap
from lumina_pendant_prep.render import PendantSettings, make_pendant


class DepthTests(unittest.TestCase):
    @unittest.skipUnless(model_path().is_file(), "Run scripts/fetch_depth_model.ps1 to install model")
    def test_real_photo_depth_and_in_memory_processing(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        original = photo.read_bytes()
        with Image.open(photo) as opened:
            image = opened.convert("RGBA")
            image.thumbnail((640, 640))
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"TEMP": temp, "TMP": temp}), \
             patch.object(tempfile, "tempdir", temp), \
             patch.object(Image.Image, "save", side_effect=AssertionError("Inference must not save an image")):
            # The inference API accepts image pixels, never a photo path or
            # output/cache path. It must not create intermediate photos here.
            depth = estimate_relative_depth(image)
            self.assertEqual(list(Path(temp).iterdir()), [])
        self.assertEqual(photo.read_bytes(), original)
        self.assertEqual(depth.shape, (image.height, image.width))
        self.assertTrue(np.isfinite(depth).all())
        def area(x, y):
            cx, cy = round(x * image.width), round(y * image.height)
            return float(depth[cy-6:cy+6, cx-6:cx+6].mean())
        self.assertGreater(area(.52, .32), area(.31, .08))  # Cat is in front of building.
        self.assertGreater(area(.70, .74), area(.52, .32))  # Foreground cloth is nearer than cat.

    def test_border_heights_are_absolute_and_hole_remains_clear(self):
        image = Image.new("RGBA", (320, 180), (255, 255, 255, 255))
        image.putpixel((160, 90), (255, 255, 255, 0))
        depth = np.tile(np.linspace(0, 1, image.width, dtype=np.float32), (image.height, 1))
        for shape in ("Rounded rectangle", "Circle", "Rectangle"):
            for outer, hole in ((True, False), (False, True), (True, True)):
                settings = PendantSettings(crop=(0, 0, 1, 1), shape=shape, target_width_mm=60, outer_border_enabled=outer,
                    outer_border_mm=2, hole_enabled=True, hole_border_enabled=hole,
                    hole_border_mm=.5, hole_x=.5, hole_y=.25, hole_diameter_mm=8)
                color, mask = make_pendant(image, settings, return_border_mask=True)
                aligned = align_depth(depth, color, settings, mask)
                border = np.asarray(mask) > 0
                alpha = np.asarray(color.getchannel("A"))
                self.assertGreater(border.sum(), 0)
                for far_high in (False, True):
                    for strength in (0, .5, 2):
                        for high in (False, True):
                            result = np.asarray(depth_heightmap(aligned, color, far_high, strength, mask, high))
                            self.assertTrue(np.all(result[border & (alpha > 0)] == (255 if high else 0)))
                            self.assertTrue(np.all(result[alpha == 0] == 0))
                # Photograph pixels matching the border color are not considered borders.
                self.assertEqual(mask.getpixel((color.width // 2 - 20, color.height // 2)), 0)

    def test_cleanup_removes_islands_but_keeps_large_regions_and_exclusions(self):
        values = np.full((60, 80), 51, dtype=np.uint8)
        values[10:35, 40:70] = 204
        values[5:7, 5:7] = 204
        values[20, 43] = 51
        depth = Image.fromarray(values, "L")
        color = Image.new("RGBA", depth.size, (255, 255, 255, 255))
        color.info["dpi"] = (254, 254)
        color.putpixel((6, 50), (0, 0, 0, 0))
        border = Image.new("L", depth.size, 0)
        border.putpixel((8, 50), 255)
        clean = depth_heightmap(depth, color, False, 1, border, True, 6, .5)
        self.assertEqual(clean.getpixel((5, 5)), 51)
        self.assertEqual(clean.getpixel((43, 20)), 204)
        self.assertEqual(clean.getpixel((50, 25)), 204)
        self.assertEqual(clean.getpixel((6, 50)), 0)
        self.assertEqual(clean.getpixel((8, 50)), 255)
        unchanged = depth_heightmap(depth, color, False, 1, border, True, 6, 0)
        self.assertEqual(unchanged.getpixel((5, 5)), 204)
        self.assertEqual(unchanged.getpixel((43, 20)), 51)

    def test_continuous_cleanup_removes_single_speck_without_touching_alpha(self):
        values = np.full((40, 40), 120, dtype=np.uint8)
        values[20, 20] = 255
        depth = Image.fromarray(values, "L")
        color = Image.new("RGBA", depth.size, (255, 255, 255, 255))
        color.info["dpi"] = (254, 254)
        color.putpixel((1, 1), (0, 0, 0, 0))
        result = depth_heightmap(depth, color, False, 1, cleanup_mm=.4)
        self.assertEqual(result.getpixel((20, 20)), 120)
        self.assertEqual(result.getpixel((1, 1)), 0)
        self.assertEqual(result.getpixel((20, 21)), 120)

    def test_terrace_contours_straighten_without_blending_or_filling_holes(self):
        import cv2
        values = np.full((100, 120), 51, dtype=np.uint8)
        for y in range(15, 85):
            values[y, 30 + (y % 4):100] = 204
        values[40:60, 60:80] = 51
        depth = Image.fromarray(values, "L")
        color = Image.new("RGBA", depth.size, (255, 255, 255, 255))
        color.info["dpi"] = (254, 254)
        color.putpixel((50, 50), (0, 0, 0, 0))
        border = Image.new("L", depth.size, 0)
        border.putpixel((5, 5), 255)
        original = depth_heightmap(depth, color, False, 1, border, True, 6)
        simplified = depth_heightmap(depth, color, False, 1, border, True, 6, contour_mm=.4)
        a = np.asarray(original); b = np.asarray(simplified)
        def perimeter(image):
            curves, _ = cv2.findContours((image == 204).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            return sum(cv2.arcLength(c, True) for c in curves)
        self.assertLess(perimeter(b), perimeter(a))
        self.assertEqual(simplified.getpixel((50, 50)), 0)
        self.assertEqual(simplified.getpixel((5, 5)), 255)
        self.assertEqual(simplified.getpixel((70, 50)), 51)  # Interior hole remains low.
        self.assertEqual(simplified.getpixel((90, 50)), 204)
        self.assertTrue(set(np.unique(b)).issubset({0, 51, 204, 255}))
        self.assertEqual(depth_heightmap(depth, color, False, 1, border, True, 6, contour_mm=0).tobytes(), original.tobytes())

    def test_terraces_have_discrete_levels_and_keep_hole_and_border(self):
        depth = Image.fromarray(np.tile(np.arange(256, dtype=np.uint8), (5, 1)), "L")
        color = Image.new("RGBA", depth.size, (255, 255, 255, 255))
        color.putpixel((5, 2), (0, 0, 0, 0))
        border = Image.new("L", depth.size, 0)
        border.putpixel((20, 2), 255)
        continuous = depth_heightmap(depth, color, False, 1)
        self.assertGreater(len(set(continuous.getdata())), 32)
        for direction in (False, True):
            for strength in (.5, 1, 2):
                for high in (False, True):
                    result = depth_heightmap(depth, color, direction, strength, border, high, 6)
                    self.assertEqual(result.getpixel((5, 2)), 0)
                    self.assertEqual(result.getpixel((20, 2)), 255 if high else 0)
                    self.assertLessEqual(len(set(result.getdata())), 6)
                    self.assertTrue(set(result.getdata()).issubset({0, 51, 102, 153, 204, 255}))
        with self.assertRaises(ValueError):
            depth_heightmap(depth, color, False, 1, terrace_levels=1)

    def test_alignment_hole_border_and_direction(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with Image.open(photo) as opened:
            image = opened.convert("RGBA")
            image.thumbnail((320, 320))
        depth = np.tile(np.linspace(0, 1, image.width, dtype=np.float32), (image.height, 1))
        settings = PendantSettings(crop=(.1, .05, .9, .95), shape="Rounded rectangle",
                                   target_width_mm=60, outer_border_enabled=True,
                                   hole_x=.5, hole_y=.2, hole_diameter_mm=4)
        color = make_pendant(image, settings)
        aligned = align_depth(depth, color, settings)
        near = depth_heightmap(aligned, color, False, 1)
        far = depth_heightmap(aligned, color, True, 1)
        self.assertEqual(near.size, color.size)
        alpha = np.asarray(color.getchannel("A"))
        self.assertTrue(np.all(np.asarray(near)[alpha == 0] == 0))
        self.assertTrue(np.all(np.asarray(far)[alpha == 0] == 0))
        self.assertEqual(near.mode, "L")
        center_y, center_x = color.height // 2, color.width // 2
        self.assertAlmostEqual(near.getpixel((center_x, center_y)) + far.getpixel((center_x, center_y)), 255, delta=1)


if __name__ == "__main__":
    unittest.main()
