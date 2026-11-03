import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lumina_pendant_prep.app import MainWindow, PreviewJob
from lumina_pendant_prep.cleanup import CleanupSettings
from lumina_pendant_prep.render import PendantSettings
from lumina_pendant_prep.project import save_project


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_photo_preserve_color_project_and_png(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertFalse(window.colors.isEnabled())
            self.assertFalse(window.color_delta.isEnabled())
            window.shape.setCurrentText("矩形")
            window.hole_toggle.setCurrentIndex(1)
            self.assertTrue(window.open_image(str(photo)))
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            self.assertEqual(window.output.tobytes(), window.source.tobytes())
            destination = window.write_png(window.output, str(root / "result.png"))
            with Image.open(destination) as png:
                self.assertEqual(png.convert("RGBA").tobytes(), window.source.tobytes())
            project = root / "project.lpproj"
            save_project(project, window._project_data())
            window.cleanup_toggle.setCurrentText("净化色块")
            self.assertTrue(window.colors.isEnabled())
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName", return_value=(str(project), "")):
                window.load_project_dialog()
            self.assertEqual(window.cleanup_toggle.currentText(), "保留原色")
            self.assertFalse(window.color_delta.isEnabled())
            window.close()

    def test_heic_preview_exports_fully_transparent_hole(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.heic"
            Image.new("RGB", (96, 64), (70, 130, 190)).save(source, format="HEIF", quality=80)
            window = MainWindow()
            self.assertTrue(window.open_image(str(source)))
            deadline = time.monotonic() + 10
            while window.output is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertIsNotNone(window.output)
            destination = window.write_png(window.output, str(root / "result.png"))
            with Image.open(destination) as png:
                self.assertEqual(png.mode, "RGBA")
                self.assertEqual(png.size, (96, 64))
                self.assertEqual(png.getpixel((48, 8))[3], 0)
                self.assertEqual(png.getpixel((0, 0))[3], 0)
                self.assertAlmostEqual(png.info["dpi"][0], 40.64, delta=0.1)
            window.close()

    def test_nozzle_choices_and_legacy_project_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "photo.png"
            Image.new("RGB", (40, 40), "red").save(photo)
            project = root / "old.lpproj"
            save_project(project, {"source_path": str(photo), "nozzle_mm": 0.55})
            window = MainWindow()
            self.assertEqual([window.nozzle_mm.itemData(i) for i in range(window.nozzle_mm.count())],
                             [0.2, 0.4, 0.6])
            self.assertEqual(window.nozzle_mm.currentData(), 0.4)
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName", return_value=(str(project), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.load_project_dialog()
                notice.assert_called_once()
            self.assertEqual(window.nozzle_mm.currentData(), 0.6)
            self.assertEqual(window._project_data()["nozzle_mm"], 0.6)
            for i, diameter in enumerate((0.2, 0.4, 0.6)):
                window.nozzle_mm.setCurrentIndex(i)
                self.assertGreaterEqual(window._settings()[0].min_feature_mm, diameter)
            window.nozzle_mm.setCurrentIndex(1)
            window.nozzle_mm.setCurrentIndex(2)
            window.undo()
            self.assertEqual(window.nozzle_mm.currentData(), 0.4)
            self.assertEqual(window._project_data()["nozzle_mm"], 0.4)
            window.redo()
            self.assertEqual(window.nozzle_mm.currentData(), 0.6)
            self.assertEqual(window._project_data()["nozzle_mm"], 0.6)
            window.close()

    def test_incomplete_polygon_cannot_export_rectangle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            Image.new("RGB", (40, 40), "red").save(path)
            window = MainWindow()
            window.open_image(str(path))
            window.shape.setCurrentText("自定义多边形")
            window.timer.stop()
            window._start_preview()
            self.assertIsNone(window.output)
            self.assertIn("三个节点", window.status.text())
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName") as dialog, \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.export_png()
                dialog.assert_not_called()
                notice.assert_called_once()
            window._set_polygon([(0.1, 0.1), (0.9, 0.1), (0.5, 0.9)])
            window.show()
            self.app.processEvents()
            window.crop_tool.setCurrentIndex(2)
            QTest.mouseClick(window.input_canvas, Qt.MouseButton.LeftButton,
                             pos=window.input_canvas.rect().center())
            self.assertEqual(window.polygon, [])
            self.assertEqual(len(window.input_canvas.polygon), 1)
            QTest.mouseClick(window.input_canvas, Qt.MouseButton.RightButton,
                             pos=window.input_canvas.rect().center())
            self.assertEqual(window.crop_tool.currentIndex(), 0)
            window.close()

    def test_png_export_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pendant.png"
            path.write_bytes(b"previous image")
            with patch("lumina_pendant_prep.app.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    MainWindow.write_png(Image.new("RGBA", (4, 4)), str(path))
            self.assertEqual(path.read_bytes(), b"previous image")
            self.assertEqual(list(root.iterdir()), [path])

    def test_project_relinks_moved_photo_and_preserves_current_image_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "moved.png"
            Image.new("RGB", (40, 30), "red").save(photo)
            project = root / "pendant.lpproj"
            save_project(project, {"source_path": str(root / "missing.png"), "target_width_mm": 42})
            window = MainWindow()
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName",
                       side_effect=[(str(project), ""), (str(photo), "")]), \
                 patch("lumina_pendant_prep.app.QMessageBox.information"):
                window.load_project_dialog()
            self.assertEqual(window.source_path, photo)
            self.assertEqual(window.width_mm.value(), 42)
            bad = root / "bad.png"
            bad.write_bytes(b"not an image")
            with patch("lumina_pendant_prep.app.QMessageBox.critical"):
                self.assertFalse(window.open_image(str(bad)))
            self.assertEqual(window.source_path, photo)
            self.assertEqual(window.width_mm.value(), 42)
            window.close()

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
