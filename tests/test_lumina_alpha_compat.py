"""Check exported photo masks at Lumina's documented source-code sampling scale.

This does not invoke Lumina, build a 3MF, or verify printing.
"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageOps

from lumina_pendant_prep.app import MainWindow
from lumina_pendant_prep.render import PendantSettings, make_pendant


class LuminaAlphaCompatibilityTests(unittest.TestCase):
    def test_photo_pendant_holes_survive_high_fidelity_sampling(self):
        fixture_dir = Path(__file__).parent / "fixtures"
        for filename in ("cat-photo.jpg", "flower-photo.jpg"):
            with Image.open(fixture_dir / filename) as image:
                photo = ImageOps.fit(image.convert("RGBA"), (320, 320), method=Image.Resampling.LANCZOS)
            for width_mm in (30, 60):
                with self.subTest(photo=filename, width_mm=width_mm):
                    settings = PendantSettings(crop=(0, 0, 1, 1), shape="Rounded rectangle",
                                               target_width_mm=width_mm, corner_radius_mm=4,
                                               hole_enabled=True, hole_x=.5, hole_y=.18,
                                               hole_diameter_mm=4)
                    pendant = make_pendant(photo, settings)
                    with tempfile.TemporaryDirectory() as directory:
                        path = MainWindow.write_png(pendant, str(Path(directory) / "pendant.png"))
                        with Image.open(path) as exported:
                            # Pinned upstream raster path uses 10 px/mm and nearest sampling,
                            # then regards alpha values below 10 as transparent.
                            pixels = int(width_mm * 10)
                            sampled = exported.resize((pixels, pixels), Image.Resampling.NEAREST)
                            self.assertLess(sampled.getpixel((0, 0))[3], 10)
                            self.assertLess(sampled.getpixel((pixels // 2, round(pixels * .18)))[3], 10)
                            self.assertGreaterEqual(sampled.getpixel((pixels // 2, pixels // 2))[3], 10)


if __name__ == "__main__":
    unittest.main()
