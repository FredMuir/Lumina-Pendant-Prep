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
