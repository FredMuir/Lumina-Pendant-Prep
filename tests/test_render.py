import unittest

import numpy as np
from PIL import Image

from lumina_pendant_prep.render import PendantSettings, hole_has_clearance, make_pendant


class PendantRenderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
