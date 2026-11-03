import unittest

import numpy as np
from PIL import Image

from lumina_pendant_prep.cleanup import CleanupSettings, simplify_image


class CleanupTests(unittest.TestCase):
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
