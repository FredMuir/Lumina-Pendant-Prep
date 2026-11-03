import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from lumina_pendant_prep.cleanup import CleanupSettings, simplify_image, _merge_adjacent_regions


class CleanupTests(unittest.TestCase):
    def test_real_photo_cleanup_produces_valid_limited_palette(self):
        with Image.open(Path(__file__).parent / "fixtures" / "cat-photo.jpg") as photo:
            source = photo.convert("RGBA").resize((320, 180), Image.Resampling.LANCZOS)
        result = simplify_image(source, CleanupSettings())
        self.assertEqual(result.image.size, source.size)
        self.assertEqual(result.image.getchannel("A").tobytes(), source.getchannel("A").tobytes())
        self.assertLessEqual(result.resulting_colors, 32)
        self.assertGreater(result.original_colors, 1)

    def test_similar_color_merge_is_local_and_respects_transparency(self):
        labels = np.array([[1, 1, 2, 0, 1, 1, 0, 2]], dtype=np.uint8)
        visible = np.array([[1, 1, 1, 0, 1, 1, 0, 1]], dtype=bool)
        labs = {1: np.array([50, 0, 0]), 2: np.array([51, 0, 0])}
        _merge_adjacent_regions(labels, visible, labs, 2)
        self.assertEqual(labels[0, :3].tolist(), [1, 1, 1])
        self.assertEqual(labels[0, 4:6].tolist(), [1, 1])
        self.assertEqual(labels[0, 7], 2)

    def test_transparent_background_does_not_consume_palette_colors(self):
        pixels = np.zeros((100, 100, 4), dtype=np.uint8)
        pixels[10:20, 10:20] = (255, 0, 0, 255)
        pixels[30:40, 30:40] = (0, 0, 255, 255)
        result = simplify_image(Image.fromarray(pixels, "RGBA"), CleanupSettings(
            colors=2, target_width_mm=100, min_feature_mm=0, color_delta=0,
        ))
        self.assertEqual(result.image.getpixel((15, 15)), (255, 0, 0, 255))
        self.assertEqual(result.image.getpixel((35, 35)), (0, 0, 255, 255))
        self.assertEqual(result.image.getpixel((0, 0))[3], 0)

    def test_adjacent_small_islands_do_not_swap_colors(self):
        source = Image.new("RGB", (2, 1))
        source.putdata([(230, 20, 30), (20, 40, 230)])
        result = simplify_image(source, CleanupSettings(
            colors=2, target_width_mm=2, min_feature_mm=2, color_delta=0,
        ))
        self.assertEqual(result.resulting_colors, 1)
        self.assertEqual(result.image.getpixel((0, 0)), result.image.getpixel((1, 0)))

    def test_merges_small_island_and_preserves_transparency(self):
        pixels = np.zeros((80, 100, 4), dtype=np.uint8)
        pixels[:] = (220, 25, 30, 255)
        pixels[25:55, 50:90] = (20, 190, 50, 255)
        pixels[10:12, 10:12] = (20, 40, 220, 255)
        pixels[0:8, 0:8, 3] = 0
        source = Image.fromarray(pixels, "RGBA")

        result = simplify_image(source, CleanupSettings(
            colors=4, target_width_mm=100, min_feature_mm=4, color_delta=0,
        ))

        output = np.asarray(result.image)
        self.assertGreaterEqual(result.merged_islands, 1)
        self.assertTrue(np.array_equal(output[:, :, 3], pixels[:, :, 3]))
        self.assertEqual(len(np.unique(output[:, :, :3][output[:, :, 3] > 0], axis=0)), 2)
        self.assertEqual(result.minimum_feature_px, 4)
        self.assertTrue(np.all(output[0:8, 0:8, 3] == 0))

    def test_keeps_long_narrow_regions_and_reports_them(self):
        pixels = np.zeros((100, 100, 4), dtype=np.uint8)
        pixels[:] = (220, 25, 30, 255)
        pixels[50, 20:80] = (20, 190, 50, 255)
        result = simplify_image(Image.fromarray(pixels, "RGBA"), CleanupSettings(
            colors=4, target_width_mm=100, min_feature_mm=4, color_delta=0,
        ))
        output = np.asarray(result.image)
        self.assertGreaterEqual(result.narrow_features, 1)
        self.assertEqual(len(np.unique(output[50, 20:80, :3], axis=0)), 1)

    def test_zero_feature_threshold_skips_island_merging(self):
        pixels = np.zeros((20, 20, 4), dtype=np.uint8)
        pixels[:] = (230, 20, 30, 255)
        pixels[5:7, 5:7] = (0, 30, 230, 255)
        result = simplify_image(Image.fromarray(pixels, "RGBA"), CleanupSettings(
            colors=4, target_width_mm=20, min_feature_mm=0, color_delta=0,
        ))
        self.assertEqual(result.merged_islands, 0)


if __name__ == "__main__":
    unittest.main()
