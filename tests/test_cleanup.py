import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from lumina_pendant_prep.cleanup import CleanupSettings, simplify_image, _merge_adjacent_regions


class CleanupTests(unittest.TestCase):
    def test_edge_smoothing_softens_curve_without_halos_or_flat_interiors(self):
        from dataclasses import replace
        from PIL import ImageDraw
        image = Image.new("RGBA", (120, 100), (240, 240, 240, 255))
        ImageDraw.Draw(image).ellipse((20, 10, 90, 80), fill=(180, 20, 20, 255))
        image.putpixel((0, 0), (0, 0, 0, 0))
        settings = CleanupSettings(colors=4, min_feature_mm=0, color_delta=0)
        sharp = simplify_image(image, settings).image
        smooth = simplify_image(image, replace(settings, smooth_edges=True)).image
        half = simplify_image(image, replace(settings, smooth_edges=True, edge_smoothing_strength=50)).image
        zero = simplify_image(image, replace(settings, smooth_edges=True, edge_smoothing_strength=0)).image
        self.assertEqual(sharp.tobytes(), zero.tobytes())
        self.assertLess(np.abs(np.asarray(half, dtype=np.int16) - np.asarray(sharp, dtype=np.int16)).sum(),
                        np.abs(np.asarray(smooth, dtype=np.int16) - np.asarray(sharp, dtype=np.int16)).sum())
        self.assertEqual(sharp.getpixel((50, 40)), smooth.getpixel((50, 40)))
        self.assertEqual(sharp.getpixel((110, 90)), smooth.getpixel((110, 90)))
        self.assertEqual(smooth.getpixel((0, 0))[3], 0)
        sharp_rgb = np.asarray(sharp)[:, :, :3]
        smooth_rgb = np.asarray(smooth)[:, :, :3]
        self.assertGreater(np.count_nonzero(np.any(sharp_rgb != smooth_rgb, axis=2)), 50)
        self.assertEqual(smooth.getpixel((1, 1))[:3], (240, 240, 240))

    def test_real_photo_cleanup_produces_valid_limited_palette(self):
        for name in ("cat-photo.jpg", "flower-photo.jpg", "dog-photo.jpg", "processor-photo.jpg"):
            with self.subTest(photo=name):
                with Image.open(Path(__file__).parent / "fixtures" / name) as photo:
                    source = photo.convert("RGBA")
                    source.thumbnail((320, 320), Image.Resampling.LANCZOS)
                result = simplify_image(source, CleanupSettings())
                self.assertEqual(result.image.size, source.size)
                self.assertEqual(result.image.getchannel("A").tobytes(), source.getchannel("A").tobytes())
                self.assertLessEqual(result.resulting_colors, 160)
                self.assertGreater(result.original_colors, 1)
                self.assertGreater(result.quantized_pixels, 0)
                self.assertEqual(result.quantized_pixels, np.count_nonzero(np.asarray(result.quantized_mask)))
                self.assertEqual(result.merged_pixels, np.count_nonzero(np.asarray(result.merged_mask)))

    def test_merge_mask_distinguishes_region_merge_from_quantization(self):
        source = Image.new("RGBA", (10, 10), (220, 25, 30, 255))
        source.putpixel((5, 5), (20, 40, 220, 255))
        source.putpixel((0, 0), (20, 40, 220, 0))
        result = simplify_image(source, CleanupSettings(2, 10, 2, 0, False))
        self.assertEqual(result.quantized_pixels, 0)
        self.assertEqual(result.merged_pixels, 1)
        self.assertEqual(result.merged_mask.getpixel((5, 5)), 255)
        self.assertEqual(result.merged_mask.getpixel((0, 0)), 0)

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
            colors=2, target_width_mm=2, min_feature_mm=2, color_delta=0, protect_distinct_colors=False,
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
            colors=4, target_width_mm=100, min_feature_mm=4, color_delta=0, protect_distinct_colors=False,
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

    def test_reports_long_narrow_region_even_when_area_is_large(self):
        pixels = np.empty((80, 1000, 4), dtype=np.uint8)
        pixels[:] = (220, 25, 30, 255)
        pixels[40, :] = (20, 190, 50, 255)
        result = simplify_image(Image.fromarray(pixels, "RGBA"), CleanupSettings(
            colors=4, target_width_mm=1000, min_feature_mm=4, color_delta=0,
        ))
        self.assertEqual(result.narrow_features, 1)
        self.assertEqual(result.image.getpixel((500, 40))[:3], (20, 190, 50))

    def test_zero_feature_threshold_skips_island_merging(self):
        pixels = np.zeros((20, 20, 4), dtype=np.uint8)
        pixels[:] = (230, 20, 30, 255)
        pixels[5:7, 5:7] = (0, 30, 230, 255)
        result = simplify_image(Image.fromarray(pixels, "RGBA"), CleanupSettings(
            colors=4, target_width_mm=20, min_feature_mm=0, color_delta=0,
        ))
        self.assertEqual(result.merged_islands, 0)

    def test_distinct_orange_and_green_islands_are_not_recolored_by_default(self):
        pixels = np.full((40, 60, 4), (176, 150, 60, 255), dtype=np.uint8)
        pixels[5:7, 5:7] = (255, 155, 0, 255)
        pixels[15:17, 15:17] = (0, 175, 60, 255)
        source = Image.fromarray(pixels, "RGBA")
        protected = simplify_image(source, CleanupSettings(8, 60, 4, 0))
        self.assertEqual(protected.image.getpixel((5, 5))[:3], (255, 155, 0))
        self.assertEqual(protected.image.getpixel((15, 15))[:3], (0, 175, 60))
        self.assertEqual(protected.protected_islands, 2)
        force = simplify_image(source, CleanupSettings(8, 60, 4, 0, False))
        self.assertEqual(force.protected_islands, 0)
        self.assertGreaterEqual(force.merged_islands, 2)

    def test_real_photo_default_quantization_reduces_average_color_error(self):
        with Image.open(Path(__file__).parent / "fixtures" / "flower-photo.jpg") as photo:
            source = photo.convert("RGBA")
            source.thumbnail((320, 320), Image.Resampling.LANCZOS)
        old = simplify_image(source, CleanupSettings(32, 60, 0, 0, False))
        new = simplify_image(source, CleanupSettings(96, 60, 0, 0))
        reference = np.asarray(source.convert("RGB"), dtype=np.float32)
        old_error = np.abs(np.asarray(old.image.convert("RGB"), dtype=np.float32) - reference).mean()
        new_error = np.abs(np.asarray(new.image.convert("RGB"), dtype=np.float32) - reference).mean()
        self.assertLess(new_error, old_error)

    def test_printable_flat_red_and_green_survive_quantization_and_export(self):
        with Image.open(Path(__file__).parent / "fixtures" / "flower-photo.jpg") as photo:
            source = photo.convert("RGBA")
            source.thumbnail((320, 320), Image.Resampling.LANCZOS)
        source.paste((255, 0, 0, 255), (10, 10, 25, 25))
        source.paste((0, 255, 0, 255), (40, 10, 55, 25))
        settings = CleanupSettings(colors=8, target_width_mm=60, min_feature_mm=.4, color_delta=2)
        result = simplify_image(source, settings)
        self.assertEqual(result.image.getpixel((17, 17)), (255, 0, 0, 255))
        self.assertEqual(result.image.getpixel((47, 17)), (0, 255, 0, 255))
        self.assertGreaterEqual(result.preserved_flat_colors, 2)
        self.assertEqual(result.quantized_mask.getpixel((17, 17)), 0)
        self.assertEqual(result.merged_mask.getpixel((17, 17)), 0)
        from io import BytesIO
        png = BytesIO()
        result.image.save(png, format="PNG")
        png.seek(0)
        self.assertEqual(Image.open(png).getpixel((17, 17)), (255, 0, 0, 255))
        without_protection = simplify_image(source, CleanupSettings(8, 60, .4, 2, False))
        self.assertNotEqual(without_protection.image.getpixel((17, 17))[:3], (255, 0, 0))

    def test_color_protection_options_are_independent(self):
        pixels = np.full((40, 60, 4), (176, 150, 60, 255), dtype=np.uint8)
        pixels[5:7, 5:7] = (255, 0, 0, 255)
        source = Image.fromarray(pixels, "RGBA")
        protect_small = simplify_image(source, CleanupSettings(8, 60, 4, 0, True, False))
        merge_small = simplify_image(source, CleanupSettings(8, 60, 4, 0, False, True))
        self.assertEqual(protect_small.image.getpixel((5, 5))[:3], (255, 0, 0))
        self.assertNotEqual(merge_small.image.getpixel((5, 5))[:3], (255, 0, 0))
        self.assertEqual(protect_small.protected_islands, 1)


if __name__ == "__main__":
    unittest.main()
