import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from lumina_pendant_prep.app import MainWindow, PreviewJob
from lumina_pendant_prep.cleanup import CleanupSettings
from lumina_pendant_prep.render import PendantSettings


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_palette_ignores_outside_crop_and_preserves_outline_alpha(self):
        outputs = []
        pendant = PendantSettings(crop=(0.25, 0, 0.75, 1), shape="Rounded rectangle",
                                  target_width_mm=50, corner_radius_mm=8, hole_enabled=False)
        for background in ((0, 255, 0, 255), (255, 255, 0, 255)):
            image = Image.new("RGBA", (100, 50), background)
            image.paste((255, 0, 0, 255), (25, 0, 50, 50))
            image.paste((0, 0, 255, 255), (50, 0, 75, 50))
            job = PreviewJob(1, image, CleanupSettings(2, 100, 0, 0), pendant, True)
            captured = []
            job.signals.finished.connect(lambda generation, prepared, output, result: captured.append(output))
            job.run()
            self.assertEqual(len(captured), 1)
            outputs.append(captured[0])
        self.assertEqual(outputs[0].tobytes(), outputs[1].tobytes())
        from lumina_pendant_prep.render import make_pendant
        self.assertEqual(outputs[0].getchannel("A").tobytes(),
                         make_pendant(image, pendant).getchannel("A").tobytes())

    def test_window_loads_photo_and_updates_preview_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            Image.new("RGBA", (80, 60), (30, 150, 210, 255)).save(path)
            window = MainWindow()
            window.open_image(str(path))
            deadline = time.monotonic() + 10
            while window.output is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertIsNotNone(window.output)
            self.assertEqual(window.output.mode, "RGBA")
            self.assertEqual(window.output_canvas.image.width(), 80)
            self.assertTrue(window._settings()[1].hole_enabled)

            window.shape.setCurrentText("圆形")
            cleanup, _ = window._settings()
            self.assertAlmostEqual(window.source.width / cleanup.target_width_mm, 1.0)
            window.shape.setCurrentText("圆角矩形")
            window.crop = (0.123, 0.123, 0.877, 0.877)
            window.hole = (0.5, 0.4)
            _, pendant = window._settings()
            self.assertAlmostEqual(pendant.hole_y, (24 - 7) / (53 - 7))
            window.crop, window.hole = (0, 0, 1, 1), (0.5, 0.12)

            # A setting change must not export the previous preview while the
            # replacement preview is still queued or running.
            window.width_mm.setValue(61)
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName") as dialog, \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.export_png()
                dialog.assert_not_called()
                notice.assert_called_once()
            window.width_mm.setValue(60)

            # Exercise the same writer used by the export dialog and verify the
            # transparency and physical-size metadata survive a real PNG roundtrip.
            exported = Path(directory) / "pendant"
            saved_path = window.write_png(window.output, str(exported))
            with Image.open(saved_path) as reopened:
                rgba = reopened.convert("RGBA")
                self.assertEqual(rgba.getpixel((0, 0))[3], 0)
                self.assertEqual(rgba.getpixel((40, 7))[3], 0)
                self.assertGreater(rgba.getpixel((40, 35))[3], 240)
                self.assertAlmostEqual(reopened.info["dpi"][0], window.output.info["dpi"][0], delta=0.1)

            window.crop = (0.08, 0.10, 0.92, 0.90)
            window.hole = (0.5, 0.12)
            self.assertFalse(window._settings()[1].hole_enabled)

            window.width_mm.setValue(70)
            self.assertEqual(window.width_mm.value(), 70)
            window.undo()
            self.assertEqual(window.width_mm.value(), 60)
            window.redo()
            self.assertEqual(window.width_mm.value(), 70)
            window.close()


if __name__ == "__main__":
    unittest.main()
