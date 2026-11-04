import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest

from lumina_pendant_prep.app import MainWindow, PreviewJob
from lumina_pendant_prep.cleanup import CleanupSettings
from lumina_pendant_prep.render import PendantSettings
from lumina_pendant_prep.project import save_project


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_user_presets_survive_restart_and_require_manual_start(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with tempfile.TemporaryDirectory() as directory, \
             patch("lumina_pendant_prep.presets.preset_path", return_value=Path(directory) / "presets.json"):
            window = MainWindow()
            window.width_mm.setValue(75)
            window.smooth_edges.setChecked(False)
            window.smooth_strength.setValue(35)
            window.hole_x_percent.setValue(40)
            with patch("lumina_pendant_prep.app.QInputDialog.getText", return_value=("我的预设", True)):
                window.save_new_preset()
            self.assertNotIn("source_path", window._presets["我的预设"])
            window.width_mm.setValue(80)
            window.update_preset()
            with patch("lumina_pendant_prep.app.QInputDialog.getText", return_value=("已重命名", True)):
                window.rename_preset()
            window.close()
            window = MainWindow()
            window.preset_select.setCurrentIndex(window.preset_select.findText("已重命名"))
            self.assertEqual(window.width_mm.value(), 80)
            self.assertFalse(window.smooth_edges.isChecked())
            self.assertEqual(window.smooth_strength.value(), 35)
            self.assertAlmostEqual(window.hole[0], .4)
            self.assertTrue(window.open_image(str(photo)))
            self.assertAlmostEqual(window.hole[0], .4)
            self.assertFalse(window.processing_toggle.isChecked())
            self.assertFalse(window.timer.isActive())
            self.assertIsNone(window.output)
            with patch("lumina_pendant_prep.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window.delete_preset()
            self.assertEqual(window.preset_select.count(), 1)
            window.close()
            self.assertEqual(list(Path(directory).iterdir()), [Path(directory) / "presets.json"])

    def test_corrupt_preset_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "presets.json"
            target.write_text("bad json", encoding="utf-8")
            with patch("lumina_pendant_prep.presets.preset_path", return_value=target), \
                 patch("lumina_pendant_prep.app.QInputDialog.getText", return_value=("test", True)), \
                 patch("lumina_pendant_prep.app.QMessageBox.critical") as error:
                window = MainWindow()
                window.save_new_preset()
                self.assertTrue(error.called)
                self.assertEqual(target.read_text(encoding="utf-8"), "bad json")
                window.close()

    def test_window_and_windows_icon_assets_are_available(self):
        from lumina_pendant_prep import app as app_module
        assets = Path(app_module.__file__).parent / "assets"
        window = MainWindow()
        self.assertFalse(window.windowIcon().isNull())
        with Image.open(assets / "app.ico") as icon:
            self.assertTrue({(16, 16), (32, 32), (48, 48), (256, 256)} <= icon.ico.sizes())
        window.close()

    def test_open_does_not_start_processing_until_enabled(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        self.assertFalse(window.processing_toggle.isChecked())
        self.assertFalse(window.timer.isActive())
        self.assertFalse(window.preview_running)
        self.assertIsNone(window.output)
        window.processing_toggle.setChecked(True)
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIsNotNone(window.output)
        self.assertEqual(window.output_generation, window.generation)
        window.close()

    def test_quantization_level_and_custom_color_count_stay_in_sync(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        self.assertEqual(window.quantization_level.currentData(), 96)
        window.quantization_level.setCurrentIndex(window.quantization_level.findData(32))
        self.assertEqual(window.colors.value(), 32)
        self.assertEqual(window._project_data()["colors"], 32)
        window.colors.setValue(73)
        self.assertIsNone(window.quantization_level.currentData())
        self.assertEqual(window._project_data()["colors"], 73)
        window._apply_history({**window._project_data(), "colors": 64})
        self.assertEqual(window.quantization_level.currentData(), 64)
        window.close()

    def test_independent_color_settings_and_legacy_project_restore(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        window.protect_flat.setChecked(False)
        window.protect_colors.setChecked(True)
        window.flat_min_area.setValue(32)
        window.distinct_delta.setValue(7)
        settings, _ = window._settings()
        self.assertFalse(settings.preserve_flat_colors)
        self.assertTrue(settings.protect_distinct_colors)
        self.assertEqual(settings.flat_min_area_px, 32)
        self.assertEqual(settings.distinct_color_delta, 7)
        data = window._project_data()
        window.protect_flat.setChecked(True)
        window._apply_history(data)
        self.assertFalse(window.protect_flat.isChecked())
        self.assertTrue(window.protect_colors.isChecked())
        old = {k: v for k, v in data.items() if k not in ("preserve_flat_colors", "flat_min_area_px", "distinct_color_delta")}
        old["protect_distinct_colors"] = False
        window._apply_history(old)
        self.assertFalse(window.protect_colors.isChecked())
        self.assertFalse(window.protect_flat.isChecked())
        window.close()

    def test_processing_button_and_stage_progress_finish_and_reset(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        self.assertEqual(window.processing_toggle.text(), "开始处理")
        window.processing_toggle.click()
        self.assertEqual(window.processing_toggle.text(), "停止处理")
        window.timer.stop()
        window._start_preview()
        self.assertEqual((window.progress_bar.minimum(), window.progress_bar.maximum()), (0, 0))
        window._preview_progress(window.generation, "颜色量化")
        self.assertIn("颜色量化", window.progress_label.text())
        self.assertIn("已用时", window.progress_label.text())
        window._preview_progress(window.generation - 1, "过时进度")
        self.assertNotIn("过时进度", window.progress_label.text())
        deadline = time.monotonic() + 30
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        self.assertIn("处理完成", window.progress_label.text())
        self.assertEqual(window.progress_bar.value(), 1)
        self.assertEqual(window.processing_toggle.text(), "开始处理")
        window.processing_toggle.click()
        window.processing_toggle.click()
        self.assertEqual(window.processing_toggle.text(), "开始处理")
        self.assertEqual(window.progress_label.text(), "处理已停止")
        self.assertEqual(window.progress_bar.value(), 0)
        window.close()

    def test_worker_reports_real_stages_for_real_photo(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with Image.open(photo) as opened:
            image = opened.convert("RGBA")
            image.thumbnail((160, 160))
        pendant = PendantSettings(crop=(0, 0, 1, 1), shape="Rectangle", hole_enabled=False)
        job = PreviewJob(7, image, CleanupSettings(), pendant, True)
        stages = []
        job.signals.progress.connect(lambda generation, stage: stages.append((generation, stage)))
        job.run()
        self.assertEqual([generation for generation, _ in stages], [7] * 7)
        self.assertEqual([stage.split("：")[0] for _, stage in stages],
                         [f"步骤 {i}/7" for i in range(1, 8)])

    def test_crop_and_border_scroll_to_same_source_location(self):
        from lumina_pendant_prep.render import crop_pixel_box
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        window = MainWindow(); window.resize(1400, 800); window.show()
        self.assertTrue(window.open_image(str(photo)))
        window.cleanup_toggle.setCurrentText("保留原色")
        window._set_crop((.2, .1, .8, .9))
        window.hole_toggle.setCurrentIndex(1)
        window.outer_border_toggle.setChecked(True)
        window.processing_toggle.click()
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        window.output_zoom.setCurrentIndex(window.output_zoom.findData(4))
        self.app.processEvents()
        self.assertAlmostEqual(window.input_canvas._image_rect().width() / window.prepared.width,
                               window.output_canvas._image_rect().width() / window.output.width, delta=.002)
        x0, y0, x1, y1 = crop_pixel_box(window.prepared.size, window.preview_pendant.crop)
        for horizontal in (True, False):
            axis = "horizontalScrollBar" if horizontal else "verticalScrollBar"
            a = getattr(window.input_scroll, axis)(); b = getattr(window.output_scroll, axis)()
            a.setValue(round(a.maximum() * .55)); self.app.processEvents()
            def fraction(scroll, canvas, bar):
                rect = canvas._image_rect()
                start = rect.x() if horizontal else rect.y()
                length = rect.width() if horizontal else rect.height()
                viewport = scroll.viewport().width() if horizontal else scroll.viewport().height()
                return (bar.value() + viewport / 2 - start) / length
            original = fraction(window.input_scroll, window.input_canvas, a)
            result = fraction(window.output_scroll, window.output_canvas, b)
            full = window.prepared.width if horizontal else window.prepared.height
            first, last = (x0, x1) if horizontal else (y0, y1)
            output = window.output.width if horizontal else window.output.height
            border = (output - (last - first)) / 2
            mapped = (result * output - border + first) / full
            self.assertAlmostEqual(original, mapped, delta=.005)
            b.setValue(round(b.maximum() * .45)); self.app.processEvents()
            original = fraction(window.input_scroll, window.input_canvas, a)
            result = fraction(window.output_scroll, window.output_canvas, b)
            mapped = (result * output - border + first) / full
            self.assertAlmostEqual(original, mapped, delta=.005)
        window.close()

    def test_changes_require_explicit_reprocessing_and_block_stale_exports(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        window.cleanup_toggle.setCurrentText("保留原色")
        window.processing_toggle.click()
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        previous = window.output.tobytes()
        window.colors.setValue(64)
        self.assertFalse(window.timer.isActive())
        self.assertFalse(window.processing_toggle.isChecked())
        self.assertEqual(window.output.tobytes(), previous)
        self.assertNotEqual(window.output_generation, window.generation)
        self.assertIn("点击开始处理", window.progress_label.text())
        window.heightmap_enabled.setChecked(True)
        with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName") as dialog, \
             patch("lumina_pendant_prep.app.QMessageBox.information"):
            window.export_png(); window.export_heightmap()
            dialog.assert_not_called()
        window.processing_toggle.click()
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        window.close()

    def test_preview_discloses_actual_shape_when_settings_change(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        window.cleanup_toggle.setCurrentText("保留原色")
        self.assertTrue(window.open_image(str(photo)))
        window.shape.setCurrentText("圆形")
        window.hole_toggle.setCurrentIndex(1)
        window.outer_border_toggle.setChecked(True)
        window.processing_toggle.click()
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        self.assertIn("当前结果：圆形", window.preview_shape_label.text())
        window.shape.setCurrentText("圆角矩形")
        self.assertIn("旧预览：圆形", window.preview_shape_label.text())
        self.assertIn("当前设置：圆角矩形", window.preview_shape_label.text())
        self.assertIn("点击开始处理", window.preview_shape_label.text())
        self.assertFalse(window.timer.isActive())
        window.processing_toggle.click()
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents(); time.sleep(.02)
        self.assertEqual(window.output_generation, window.generation)
        self.assertIn("当前结果：圆角矩形，外边框开启", window.preview_shape_label.text())
        self.assertNotEqual(window.output.width, window.output.height)
        window.close()

    def test_real_photo_heightmap_export_and_project_settings(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            window.outer_border_toggle.setChecked(True)
            window.hole_border_toggle.setChecked(True)
            self.assertTrue(window.open_image(str(photo)))
            window.processing_toggle.click()
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.02)
            self.assertEqual(window.output_generation, window.generation)
            self.assertFalse(window.heightmap_export_button.isEnabled())
            window.heightmap_enabled.setChecked(True)
            window.generate_depth()
            deadline = time.monotonic() + 60
            while window.depth_job is not None and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.02)
            self.assertIsNotNone(window.depth_raw, window.depth_status.text())
            self.assertTrue(window.depth_dialog.isVisible())
            raw_depth = window.depth_raw
            window.heightmap_direction.setCurrentIndex(1)
            window.heightmap_strength.setValue(50)
            window.heightmap_border.setCurrentIndex(1)
            window.preview_kind.setCurrentText("高度图")
            self.assertEqual(window.output_generation, window.generation)
            self.assertIs(window.depth_raw, raw_depth)
            target = root / "height.png"
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.information"):
                window.export_heightmap()
            with Image.open(target) as result:
                self.assertEqual(result.mode, "L")
                self.assertEqual(result.size, window.output.size)
                self.assertEqual(result.getpixel((0, 0)), 0)
                self.assertNotIn("icc_profile", result.info)
                import numpy as np
                border = np.asarray(window.depth_border_mask) > 0
                self.assertTrue(np.all(np.asarray(result)[border] == 255))
                x, y = result.width // 2, result.height // 2
                self.assertEqual(window.output_canvas.image.pixelColor(x, y).red(), result.getpixel((x, y)))
            self.assertTrue(window._project_data()["heightmap_enabled"])
            self.assertTrue(window._project_data()["heightmap_far_high"])
            self.assertEqual(window._project_data()["heightmap_strength_percent"], 50)
            self.assertTrue(window._project_data()["heightmap_border_high"])
            self.assertEqual([p.name for p in root.iterdir()], ["height.png"])
            self.assertTrue(window.open_image(str(Path(__file__).parent / "fixtures" / "cat-photo.jpg")))
            self.assertIsNone(window.depth_raw)
            self.assertIsNone(window.depth_border_mask)
            self.assertIsNone(window.depth_view.image)
            self.assertFalse(window.depth_dialog.isVisible())
            self.assertEqual([p.name for p in root.iterdir()], ["height.png"])
            window.close()

    def test_multiframe_photo_discloses_selected_primary_or_first_page(self):
        fixtures = Path(__file__).parent / "fixtures"
        with Image.open(fixtures / "cat-photo.jpg") as cat, Image.open(fixtures / "flower-photo.jpg") as flower:
            images = [cat.convert("RGB").resize((160, 120)), flower.convert("RGB").resize((160, 120))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heif, tiff = root / "photos.heic", root / "photos.tiff"
            images[0].save(heif, format="HEIF", save_all=True, append_images=[images[1]], primary_index=1)
            images[0].save(tiff, format="TIFF", save_all=True, append_images=[images[1]])
            window = MainWindow()
            self.assertTrue(window.open_image(str(heif)))
            window.processing_toggle.setChecked(True)
            self.assertIn("仅加载主图（第 2 张）", window.source_info.text())
            self.assertTrue(window.open_image(str(tiff)))
            window.processing_toggle.setChecked(True)
            self.assertIn("仅加载第一页（第 1 张）", window.source_info.text())
            window.close()

    def test_small_window_can_scroll_to_status_with_real_photo(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        window.resize(1000, 500)
        window.cleanup_toggle.setCurrentText("保留原色")
        window.show()
        self.assertTrue(window.open_image(str(photo)))
        window.processing_toggle.setChecked(True)
        deadline = time.monotonic() + 20
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(window.output_generation, window.generation)
        self.app.processEvents()
        self.assertLessEqual(window.height(), 500)
        bar = window.controls_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        self.app.processEvents()
        bar.setValue(bar.maximum())
        self.app.processEvents()
        pos = window.status.mapTo(window.controls_scroll.viewport(), QPoint(0, 0))
        self.assertGreaterEqual(pos.y(), 0)
        self.assertLessEqual(pos.y() + window.status.height(), window.controls_scroll.viewport().height())
        window.close()

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
            window.processing_toggle.setChecked(True)
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

    def test_real_photo_highlight_modes_do_not_change_export(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "photo.png"
            with Image.open(photo) as original:
                original.resize((320, 180), Image.Resampling.LANCZOS).save(path)
            window = MainWindow()
            self.assertTrue(window.open_image(str(path)))
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            baseline = window.output.tobytes()
            window.show()
            self.app.processEvents()
            window.output_zoom.setCurrentText("4 倍")
            self.app.processEvents()
            self.assertGreater(window.output_scroll.horizontalScrollBar().maximum(), 0)
            self.assertGreater(window.output_scroll.verticalScrollBar().maximum(), 0)
            self.assertGreater(window.input_scroll.horizontalScrollBar().maximum(), 0)
            window.output_scroll.horizontalScrollBar().setValue(window.output_scroll.horizontalScrollBar().maximum())
            window.output_scroll.verticalScrollBar().setValue(window.output_scroll.verticalScrollBar().maximum())
            self.assertEqual(window.input_scroll.horizontalScrollBar().value(), window.input_scroll.horizontalScrollBar().maximum())
            self.assertEqual(window.input_scroll.verticalScrollBar().value(), window.input_scroll.verticalScrollBar().maximum())
            self.assertIn("量化改变", window.status.text())
            self.assertIn("区域合并改变", window.status.text())
            window.highlight_toggle.setChecked(True)
            for mode in ("所有变化", "颜色量化", "区域合并"):
                window.highlight_mode.setCurrentText(mode)
                self.assertIsNotNone(window.output_canvas.image)
                self.assertEqual(window.output.tobytes(), baseline)
            destination = window.write_png(window.output, str(Path(directory) / "result.png"))
            with Image.open(destination) as png:
                self.assertEqual(png.convert("RGBA").tobytes(), baseline)
            window.output_zoom.setCurrentText("适合窗口")
            self.app.processEvents()
            self.assertEqual(window.output_canvas.minimumWidth(), 280)
            window.close()

    def test_new_setting_cancels_obsolete_real_photo_preview(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        window = MainWindow()
        self.assertTrue(window.open_image(str(photo)))
        window.processing_toggle.setChecked(True)
        window.timer.stop()
        window._start_preview()
        old_job = window.active_job
        cancelled = []
        old_job.signals.cancelled.connect(cancelled.append)
        window.cleanup_toggle.setCurrentText("保留原色")
        self.assertTrue(old_job.cancel_requested.is_set())
        self.assertFalse(window.processing_toggle.isChecked())
        self.assertFalse(window.timer.isActive())
        self.assertIn("点击开始处理", window.progress_label.text())
        window.processing_toggle.click()
        deadline = time.monotonic() + 30
        while window.output_generation != window.generation and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(window.output_generation, window.generation)
        self.assertIn("保留原始颜色", window.status.text())
        self.assertEqual(cancelled, [old_job.generation])
        window.close()

    def test_real_photo_invalid_profile_warning_and_srgb_export(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid-profile.png"
            with Image.open(photo) as original:
                original.resize((320, 180), Image.Resampling.LANCZOS).save(source, icc_profile=b"invalid profile")
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertTrue(window.open_image(str(source)))
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            self.assertIn("ICC 无法转换", window.source_info.text())
            destination = window.write_png(window.output, str(Path(directory) / "output.png"))
            with Image.open(destination) as png:
                from lumina_pendant_prep.image_io import SRGB_PROFILE
                self.assertEqual(png.info.get("icc_profile"), SRGB_PROFILE)
            window.close()

    def test_real_photo_protected_region_is_preserved_and_project_restored(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "photo.png"
            with Image.open(photo) as original:
                original.resize((320, 180), Image.Resampling.LANCZOS).save(path)
            window = MainWindow()
            window.shape.setCurrentText("矩形")
            window.hole_toggle.setCurrentIndex(1)
            window.open_image(str(path))
            window.processing_toggle.setChecked(True)
            window.show()
            self.app.processEvents()
            window.crop_tool.setCurrentText("拖动添加保护区域")
            rect = window.input_canvas._image_rect()
            start = QPoint(round(rect.x() + rect.width() * 0.25), round(rect.y() + rect.height() * 0.25))
            end = QPoint(round(rect.x() + rect.width() * 0.75), round(rect.y() + rect.height() * 0.75))
            QTest.mousePress(window.input_canvas, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(window.input_canvas, end)
            QTest.mouseRelease(window.input_canvas, Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual(len(window.protected_regions), 1)
            region = window.protected_regions[0]
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            from lumina_pendant_prep.render import crop_pixel_box
            box = crop_pixel_box(window.source.size, region)
            self.assertEqual(window.output.crop(box).tobytes(), window.source.crop(box).tobytes())
            self.assertNotEqual(window.output.crop((0, 0, 80, 45)).tobytes(),
                                window.source.crop((0, 0, 80, 45)).tobytes())
            self.assertIsNone(window.preview_merged.crop(box).getbbox())
            self.assertIsNone(window.preview_quantized.crop(box).getbbox())
            project = root / "project.lpproj"
            save_project(project, window._project_data())
            window._clear_protection()
            self.assertEqual(window.protected_regions, [])
            window.undo()
            self.assertEqual(window.protected_regions, [region])
            window._clear_protection()
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName", return_value=(str(project), "")):
                window.load_project_dialog()
            self.assertEqual(window.protected_regions, [region])
            self.assertEqual(window.input_canvas.protected_regions, [region])
            window.close()

    def test_heic_preview_exports_fully_transparent_hole(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.heic"
            Image.new("RGB", (96, 64), (70, 130, 190)).save(source, format="HEIF", quality=80)
            window = MainWindow()
            self.assertTrue(window.open_image(str(source)))
            window.processing_toggle.setChecked(True)
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

    def test_gentle_preset_is_undoable_and_uses_selected_nozzle(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        window.nozzle_mm.setCurrentIndex(2)
        window.cleanup_toggle.setCurrentText("保留原色")
        self.assertTrue(window.open_image(str(photo)))
        window.processing_toggle.setChecked(True)
        window._apply_gentle_preset()
        self.assertEqual(window.colors.value(), 128)
        self.assertEqual(window.color_delta.value(), 1)
        self.assertAlmostEqual(window.min_feature_mm.value(), 0.6)
        self.assertEqual(window.cleanup_toggle.currentText(), "净化色块")
        self.assertTrue(window.colors.isEnabled())
        window.undo()
        self.assertEqual(window.colors.value(), 96)
        self.assertEqual(window.color_delta.value(), 2)
        self.assertAlmostEqual(window.min_feature_mm.value(), 0.45)
        self.assertEqual(window.cleanup_toggle.currentText(), "保留原色")
        window.close()

    def test_incomplete_polygon_cannot_export_rectangle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            Image.new("RGB", (40, 40), "red").save(path)
            window = MainWindow()
            window.open_image(str(path))
            window.shape.setCurrentText("自定义多边形")
            window.processing_toggle.setChecked(True)
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

    def test_cannot_overwrite_real_source_photo_with_export_or_project(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.png"
            with Image.open(photo) as original:
                original.resize((320, 180), Image.Resampling.LANCZOS).save(source)
            original_bytes = source.read_bytes()
            window = MainWindow()
            self.assertTrue(window.open_image(str(source)))
            window.processing_toggle.setChecked(True)
            window.hole_toggle.setCurrentIndex(1)
            window.timer.stop()
            window.output = window.source.copy()
            window.output_generation = window.generation
            # The extension added by the PNG writer must be considered too.
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName",
                       return_value=(str(source.with_suffix("")), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.critical") as error:
                window.export_png()
                error.assert_called_once()
            self.assertEqual(source.read_bytes(), original_bytes)
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName",
                       return_value=(str(source), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.critical") as error:
                window.save_project_dialog()
                error.assert_called_once()
            self.assertEqual(source.read_bytes(), original_bytes)
            alias = root / "alias.png"
            try:
                os.link(source, alias)
            except OSError:
                pass  # Some filesystems do not support hard links.
            else:
                self.assertTrue(window._is_source_path(alias))
            window.close()

    def test_unsafe_requested_hole_blocks_export_until_explicitly_disabled(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertTrue(window.open_image(str(photo)))
            window._set_hole(0.01, 0.01)
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            self.assertFalse(window.hole_safe)
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName") as dialog, \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.export_png()
                dialog.assert_not_called()
                self.assertEqual(notice.call_args.args[1], "挂绳孔无法裁切")
            window.hole_toggle.setCurrentIndex(1)
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            destination = root / "without-hole.png"
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName",
                       return_value=(str(destination), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.information"):
                window.export_png()
            self.assertTrue(destination.exists())
            window.close()

    def test_export_shows_lumina_width_and_hole_size_for_real_photo(self):
        photo = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertTrue(window.open_image(str(photo)))
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 20
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(window.output_generation, window.generation)
            destination = Path(directory) / "pendant.png"
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName",
                       return_value=(str(destination), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.export_png()
            message = notice.call_args.args[2]
            self.assertIn("60 × 33.75 mm", message)
            self.assertIn("挂绳孔直径约 4 mm", message)
            self.assertIn("目标宽度设为 60 mm", message)
            self.assertIn("建模和切片可能改变实际开口", message)
            self.assertTrue(destination.exists())
            window.close()

    def test_real_photo_hole_compensation_changes_mask_and_restores_project(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window = MainWindow()
            window.cleanup_toggle.setCurrentText("保留原色")
            self.assertTrue(window.open_image(str(photo)))
            window.processing_toggle.setChecked(True)

            def wait_for_preview():
                deadline = time.monotonic() + 20
                while window.output_generation != window.generation and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.02)
                self.assertEqual(window.output_generation, window.generation)
                self.assertTrue(window.hole_safe)

            def transparent_width():
                alpha = window.output.getchannel("A")
                y = round(window.output.height * .12)
                return sum(alpha.getpixel((x, y)) == 0 for x in range(alpha.width))

            wait_for_preview()
            base_width = transparent_width()
            window.hole_compensation_mm.setValue(.6)
            window.processing_toggle.setChecked(True)
            wait_for_preview()
            self.assertGreaterEqual(transparent_width(), base_width + 6)
            self.assertAlmostEqual(window._settings()[1].hole_diameter_mm, 4.6)
            self.assertAlmostEqual(window.input_canvas.hole_fraction, 4.6 / 60)
            project = root / "project.lpproj"
            save_project(project, window._project_data())
            window.undo()
            self.assertEqual(window.hole_compensation_mm.value(), 0)
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName", return_value=(str(project), "")):
                window.load_project_dialog()
            self.assertAlmostEqual(window.hole_compensation_mm.value(), .6)
            window.processing_toggle.setChecked(True)
            wait_for_preview()
            with patch("lumina_pendant_prep.app.QFileDialog.getSaveFileName",
                       return_value=(str(root / "compensated.png"), "")), \
                 patch("lumina_pendant_prep.app.QMessageBox.information") as notice:
                window.export_png()
            self.assertIn("目标 4 + 补偿 0.6 mm", notice.call_args.args[2])
            window.close()

    def test_hole_position_and_border_settings_persist(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow()
            self.assertTrue(window.open_image(str(photo)))
            window.hole_x_percent.setValue(45)
            window.hole_y_percent.setValue(20)
            self.assertEqual(window.hole, (.45, .2))
            window.outer_border_toggle.setChecked(True)
            window.outer_border_mm.setValue(2)
            window.outer_border_color = "#ff0000"
            window.hole_border_toggle.setChecked(True)
            window.hole_border_mm.setValue(.5)
            window.hole_border_color = "#0000ff"
            window.protect_colors.setChecked(False)
            window.processing_toggle.setChecked(True)
            deadline = time.monotonic() + 30
            while window.output_generation != window.generation and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.02)
            self.assertEqual(window.output_generation, window.generation, window.status.text())
            self.assertTrue(window.hole_safe)
            self.assertGreater(window.output.width, window.source.width)
            project = Path(directory) / "border.lpproj"
            save_project(project, window._project_data())
            window.protect_colors.setChecked(True)
            with patch("lumina_pendant_prep.app.QFileDialog.getOpenFileName", return_value=(str(project), "")):
                window.load_project_dialog()
            self.assertFalse(window.processing_toggle.isChecked())
            self.assertEqual(window.hole, (.45, .2))
            self.assertTrue(window.outer_border_toggle.isChecked())
            self.assertTrue(window.hole_border_toggle.isChecked())
            self.assertEqual(window.outer_border_color, "#ff0000")
            self.assertEqual(window.hole_border_color, "#0000ff")
            self.assertFalse(window.protect_colors.isChecked())
            window.close()

    def test_drag_hole_updates_numeric_position(self):
        photo = Path(__file__).parent / "fixtures" / "flower-photo.jpg"
        window = MainWindow()
        window.show()
        self.assertTrue(window.open_image(str(photo)))
        self.app.processEvents()
        window.crop_tool.setCurrentText("点击设置孔位置")
        rect = window.input_canvas._image_rect()
        start = QPoint(round(rect.x() + rect.width() * .4), round(rect.y() + rect.height() * .2))
        end = QPoint(round(rect.x() + rect.width() * .6), round(rect.y() + rect.height() * .3))
        QTest.mousePress(window.input_canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(window.input_canvas, end)
        QTest.mouseRelease(window.input_canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertAlmostEqual(window.hole_x_percent.value(), 60, delta=1)
        self.assertAlmostEqual(window.hole_y_percent.value(), 30, delta=1)
        window.close()

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
            window.processing_toggle.setChecked(True)
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
