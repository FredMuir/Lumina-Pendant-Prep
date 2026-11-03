import unittest

import numpy as np
from PIL import Image

from lumina_pendant_prep.render import PendantSettings, hole_has_clearance, hole_has_opaque_surrounding, make_pendant


class PendantRenderTests(unittest.TestCase):
    def test_small_hole_and_outside_corner_are_fully_transparent(self):
        image = Image.new("RGBA", (96, 64), (70, 130, 190, 255))
        result = make_pendant(image, PendantSettings(
            crop=(0, 0, 1, 1), shape="Rounded rectangle", target_width_mm=60,
            corner_radius_mm=3, hole_enabled=True, hole_x=0.5, hole_y=0.12,
            hole_diameter_mm=4,
        ))
        self.assertEqual(result.getpixel((48, 8))[3], 0)
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        self.assertEqual(result.getpixel((48, 32))[3], 255)

    def test_transparent_source_near_hole_fails_wall_check(self):
        image = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        settings = PendantSettings(crop=(0, 0, 1, 1), shape="Rectangle",
                                   target_width_mm=100, hole_x=0.5, hole_y=0.5,
                                   hole_diameter_mm=4)
        self.assertTrue(hole_has_opaque_surrounding(image, settings))
        image.putpixel((54, 50), (0, 0, 0, 0))
        self.assertFalse(hole_has_opaque_surrounding(image, settings))

    def setUp(self):
        self.image = Image.new("RGBA", (100, 80), (220, 40, 30, 255))

    def test_round_corners_and_hole_are_alpha_cutouts(self):
        output = make_pendant(self.image, PendantSettings(
            crop=(0, 0, 1, 1), shape="Rounded rectangle", corner_radius_mm=8,
            target_width_mm=100, hole_enabled=True, hole_x=0.5, hole_y=0.35,
            hole_diameter_mm=10,
        ))
        alpha = np.asarray(output.getchannel("A"))
        self.assertEqual(output.mode, "RGBA")
        self.assertEqual(alpha[0, 0], 0)
        self.assertEqual(alpha[28, 50], 0)
        self.assertGreater(alpha[65, 50], 240)
        self.assertAlmostEqual(output.info["dpi"][0], 25.4, places=1)

    def test_circle_outputs_square_and_clips_corners(self):
        output = make_pendant(self.image, PendantSettings(
            crop=(0, 0, 1, 1), shape="Circle", target_width_mm=60,
            hole_enabled=False,
        ))
        self.assertEqual(output.size, (80, 80))
        self.assertEqual(output.getchannel("A").getpixel((0, 0)), 0)
        self.assertGreater(output.getchannel("A").getpixel((40, 40)), 240)

    def test_polygon_clips_outside_points(self):
        output = make_pendant(self.image, PendantSettings(
            crop=(0, 0, 1, 1), shape="Polygon", target_width_mm=100,
            hole_enabled=False, polygon=((0.5, 0), (1, 1), (0, 1)),
        ))
        alpha = np.asarray(output.getchannel("A"))
        self.assertEqual(alpha[5, 5], 0)
        self.assertGreater(alpha[70, 50], 240)

    def test_hole_clearance_prevents_edge_notches(self):
        settings = PendantSettings(
            crop=(0.08, 0.10, 0.92, 0.90), shape="Rounded rectangle",
            target_width_mm=60, hole_enabled=False, hole_x=0.5, hole_y=0.025,
            hole_diameter_mm=4,
        )
        self.assertFalse(hole_has_clearance((480, 360), settings, minimum_wall_mm=1.2))
        safe = PendantSettings(**{**settings.__dict__, "hole_y": 0.30})
        self.assertTrue(hole_has_clearance((480, 360), safe, minimum_wall_mm=1.2))

    def test_polygon_hole_respects_crop_boundary(self):
        settings = PendantSettings(
            crop=(0.25, 0.25, 0.75, 0.75), shape="Polygon",
            target_width_mm=50, hole_x=0.5, hole_y=0.02,
            hole_diameter_mm=4, polygon=((0, 0), (1, 0), (1, 1), (0, 1)),
        )
        self.assertFalse(hole_has_clearance((100, 100), settings))

    def test_small_raster_hole_reserves_rounded_radius(self):
        settings = PendantSettings(
            crop=(0, 0, 1, 1), shape="Rectangle", target_width_mm=100,
            hole_x=0.5, hole_y=0.1, hole_diameter_mm=1,
        )
        self.assertFalse(hole_has_clearance((20, 20), settings))


if __name__ == "__main__":
    unittest.main()
