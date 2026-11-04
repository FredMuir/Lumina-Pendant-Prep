"""Opt-in test for a user's private photo; never bundles or uploads it."""
import os
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from lumina_pendant_prep.app import MainWindow


@unittest.skipUnless(os.environ.get("LPP_USER_PHOTO"), "Set LPP_USER_PHOTO to a local photo")
class LocalPhotoTest(unittest.TestCase):
    def test_open_wait_and_process(self):
        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        try:
            self.assertTrue(window.open_image(str(Path(os.environ["LPP_USER_PHOTO"]))))
            self.assertFalse(window.processing_toggle.isChecked())
            self.assertFalse(window.timer.isActive())
            self.assertIsNone(window.output)
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 180
            while window.output_generation != window.generation and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.05)
            self.assertEqual(window.output_generation, window.generation, window.status.text())
            self.assertIsNotNone(window.output)
            self.assertLessEqual(max(window.output.size), 2400)
        finally:
            window.close()
