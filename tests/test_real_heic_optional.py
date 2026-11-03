"""Optional regression using a locally supplied, publicly linked iPhone photo."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from lumina_pendant_prep.app import MainWindow
from lumina_pendant_prep.cleanup import CleanupSettings, simplify_image


SAMPLE_SHA256 = "4b887e6bc345191cbb9836602373b3ec454558ccc62d3518041e085117238fe0"


@unittest.skipUnless(os.environ.get("LPP_REAL_HEIC"), "Set LPP_REAL_HEIC to the documented original iPhone sample")
class RealHeicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_original_iphone_photo_preview_and_png(self):
        source = Path(os.environ["LPP_REAL_HEIC"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), SAMPLE_SHA256)
        window = MainWindow()
        try:
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertTrue(window.open_image(str(source)))
            self.assertEqual(window.source.size, (3024, 4032))
            deadline = time.monotonic() + 90
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation, window.status.text())
            self.assertTrue(window.hole_safe)
            with tempfile.TemporaryDirectory() as directory:
                saved = window.write_png(window.output, str(Path(directory) / "pendant.png"))
                with Image.open(saved) as png:
                    self.assertEqual(png.size, (3024, 4032))
                    self.assertEqual(png.mode, "RGBA")
                    self.assertEqual(png.getpixel((0, 0))[3], 0)
                    self.assertEqual(png.getpixel((1512, round(4032 * .12)))[3], 0)
                    self.assertEqual(png.getpixel((1512, 2016))[3], 255)
                    self.assertAlmostEqual(png.info["dpi"][0], 3024 * 25.4 / 60, delta=0.1)
            # Exercise cleanup on a downsampled version of the actual photo;
            # this does not stand in for a full-resolution performance test.
            small = window.source.resize((300, 400), Image.Resampling.LANCZOS)
            result = simplify_image(small, CleanupSettings())
            self.assertEqual(result.image.size, small.size)
            self.assertLessEqual(result.resulting_colors, 32)
        finally:
            window.close()
