from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSpinBox, QCheckBox,
    QSplitter, QVBoxLayout, QWidget,
)

from .canvas import ImageCanvas
from .cleanup import CleanupSettings, simplify_image
from .image_io import open_rgba
from .project import load_project, save_project
from .render import (PendantSettings, crop_pixel_box, hole_has_clearance, hole_has_opaque_surrounding,
                     make_pendant, pendant_pixel_width)


class PreviewSignals(QObject):
    finished = Signal(int, object, object, object)
    failed = Signal(int, str)


class PreviewJob(QRunnable):
    def __init__(self, generation: int, image: Image.Image, cleanup: CleanupSettings,
                 pendant: PendantSettings, enabled: bool):
        super().__init__()
        self.generation = generation
        self.image = image.copy()
        self.cleanup = cleanup
        self.pendant = pendant
        self.enabled = enabled
        self.signals = PreviewSignals()

    def run(self):
        try:
            if self.enabled:
                # Analyze only pixels that survive the pendant outline. Restore
                # the original alpha before final rendering to avoid applying
                # antialiased outline transparency twice.
                outline = make_pendant(self.image, replace(self.pendant, hole_enabled=False))
                x0, y0, x1, y1 = crop_pixel_box(self.image.size, self.pendant.crop)
                if self.pendant.shape == "Circle":
                    side = min(x1 - x0, y1 - y0)
                    x0 += (x1 - x0 - side) // 2
                    y0 += (y1 - y0 - side) // 2
                selection = Image.new("L", self.image.size, 0)
                selection.paste(outline.getchannel("A"), (x0, y0))
                analysis_image = self.image.convert("RGBA")
                original_alpha = analysis_image.getchannel("A")
                analysis_image.putalpha(selection)
                result = simplify_image(analysis_image, self.cleanup)
                prepared = result.image
                prepared.putalpha(original_alpha)
            else:
                result = None
                prepared = self.image
            output = make_pendant(prepared, self.pendant)
            self.signals.finished.emit(self.generation, prepared, output, result)
        except Exception as exc:
            self.signals.failed.emit(self.generation, str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lumina Pendant Prep")
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.source: Image.Image | None = None
        self.source_path: Path | None = None
        self.crop = (0.0, 0.0, 1.0, 1.0)
        self.hole = (0.5, 0.12)
        self.polygon: list[tuple[float, float]] = []
        self.prepared: Image.Image | None = None
        self.output: Image.Image | None = None
        self.preview_changed: Image.Image | None = None
        self.hole_safe = False
        self.generation = 0
        self.output_generation = -1
        self.preview_running = False
        self.active_job: PreviewJob | None = None
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self._history_current: dict | None = None
        self._loading_project = False
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._start_preview)
        self._build_ui()
        self._build_menu()
        self._update_enabled()

    def _build_ui(self):
        root = QWidget()
        layout = QHBoxLayout(root)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)
        controls = QWidget()
        controls.setMinimumWidth(270)
        controls.setMaximumWidth(340)
        control_layout = QVBoxLayout(controls)
        toolbar = QHBoxLayout()
        open_button = QPushButton("打开照片")
        open_button.clicked.connect(self.open_dialog)
        export_button = QPushButton("导出 PNG")
        export_button.clicked.connect(self.export_png)
        toolbar.addWidget(open_button)
        toolbar.addWidget(export_button)
        control_layout.addLayout(toolbar)

        print_group = QGroupBox("打印与色块净化")
        form = QFormLayout(print_group)
        self.width_mm = self._double(60, 1, 300, 0.5)
        self.nozzle_mm = self._double(0.4, 0.1, 2, 0.05)
        self.min_feature_mm = self._double(0.45, 0, 5, 0.05)
        self.colors = QSpinBox(); self.colors.setRange(2, 128); self.colors.setValue(32)
        self.color_delta = self._double(8, 0, 40, 1)
        self.cleanup_toggle = QComboBox(); self.cleanup_toggle.addItems(["净化色块", "保留原色"])
        form.addRow("目标宽度 (mm)", self.width_mm)
        form.addRow("喷嘴直径 (mm)", self.nozzle_mm)
        form.addRow("最小特征宽度 (mm)", self.min_feature_mm)
        form.addRow("颜色上限", self.colors)
        form.addRow("相近色阈值", self.color_delta)
        form.addRow("处理模式", self.cleanup_toggle)
        control_layout.addWidget(print_group)

        shape_group = QGroupBox("挂坠裁切")
        shape_form = QFormLayout(shape_group)
        self.shape = QComboBox(); self.shape.addItems(["圆角矩形", "矩形", "圆形", "自定义多边形"])
        self.corner_mm = self._double(3, 0, 30, 0.5)
        self.crop_tool = QComboBox(); self.crop_tool.addItems(["拖动设置裁切框", "点击设置孔位置", "点击绘制多边形"])
        self.hole_toggle = QComboBox(); self.hole_toggle.addItems(["启用透明挂绳孔", "关闭挂绳孔"])
        self.hole_mm = self._double(4, 0.5, 30, 0.5)
        shape_form.addRow("外形", self.shape)
        shape_form.addRow("圆角半径 (mm)", self.corner_mm)
        shape_form.addRow("画布操作", self.crop_tool)
        shape_form.addRow("挂绳孔", self.hole_toggle)
        shape_form.addRow("孔径 (mm)", self.hole_mm)
        control_layout.addWidget(shape_group)
        edit_hint = QLabel("拖动照片设置裁切框；点击孔位置工具后可定位挂绳孔。绘制多边形：左键添加节点，右键结束。")
        edit_hint.setWordWrap(True)
        edit_hint.setStyleSheet("color:#9caab7;padding:4px")
        control_layout.addWidget(edit_hint)
        self.reset_crop = QPushButton("重置裁切框")
        self.reset_crop.clicked.connect(self.reset_geometry)
        control_layout.addWidget(self.reset_crop)
        self.status = QLabel("打开一张照片开始处理。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#b8c2cc;padding:8px")
        control_layout.addWidget(self.status)
        control_layout.addStretch(1)
        splitter.addWidget(controls)

        previews = QWidget()
        preview_layout = QVBoxLayout(previews)
        headings = QHBoxLayout()
        headings.addWidget(QLabel("原图 / 裁切操作"))
        headings.addStretch()
        headings.addWidget(QLabel("挂坠 PNG 预览（棋盘格为透明）"))
        self.highlight_toggle = QCheckBox("标记已简化区域")
        self.highlight_toggle.toggled.connect(self._refresh_output_canvas)
        headings.addWidget(self.highlight_toggle)
        preview_layout.addLayout(headings)
        panes = QSplitter(Qt.Orientation.Horizontal)
        self.input_canvas = ImageCanvas(interactive=True)
        self.output_canvas = ImageCanvas(interactive=False)
        panes.addWidget(self.input_canvas); panes.addWidget(self.output_canvas)
        preview_layout.addWidget(panes, 1)
        self.source_info = QLabel("支持 JPEG、PNG、WebP、BMP、TIFF、HEIC/HEIF、AVIF")
        preview_layout.addWidget(self.source_info)
        splitter.addWidget(previews)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(root)

        self.input_canvas.cropChanged.connect(self._set_crop)
        self.input_canvas.holeMoved.connect(self._set_hole)
        self.input_canvas.polygonChanged.connect(self._set_polygon)
        self.crop_tool.currentIndexChanged.connect(
            lambda i: self.input_canvas.set_mode(["crop", "hole", "outline"][i])
        )
        for widget in (self.width_mm, self.min_feature_mm, self.colors, self.color_delta,
                       self.shape, self.corner_mm, self.hole_toggle, self.hole_mm,
                       self.cleanup_toggle):
            signal = widget.valueChanged if isinstance(widget, (QDoubleSpinBox, QSpinBox)) else widget.currentIndexChanged
            signal.connect(self._schedule_preview)
        self.nozzle_mm.valueChanged.connect(self._update_status)
        self.nozzle_mm.valueChanged.connect(self._schedule_preview)

    @staticmethod
    def _double(value: float, minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(); spin.setRange(minimum, maximum); spin.setSingleStep(step)
        spin.setDecimals(2); spin.setValue(value)
        return spin

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        edit_menu = self.menuBar().addMenu("编辑")
        undo_action = QAction("撤销", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.undo)
        redo_action = QAction("重做", self)
        redo_action.setShortcut("Ctrl+Shift+Z")
        redo_action.triggered.connect(self.redo)
        edit_menu.addAction(undo_action)
        edit_menu.addAction(redo_action)
        for title, callback, shortcut in [
            ("打开图像…", self.open_dialog, "Ctrl+O"),
            ("打开项目…", self.load_project_dialog, "Ctrl+Shift+O"),
            ("保存项目…", self.save_project_dialog, "Ctrl+S"),
            ("导出 PNG…", self.export_png, "Ctrl+E"),
        ]:
            action = QAction(title, self)
            action.triggered.connect(callback)
            if shortcut:
                action.setShortcut(shortcut)
            file_menu.addAction(action)

    def open_dialog(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "打开照片", "", "图像 (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.heic *.heif *.avif)"
        )
        if filename:
            self.open_image(filename)

    def open_image(self, filename: str):
        try:
            image = open_rgba(filename)
        except Exception as exc:
            QMessageBox.critical(self, "无法打开图像", str(exc))
            return False
        self.source, self.source_path = image, Path(filename)
        self.crop, self.hole = (0.0, 0.0, 1.0, 1.0), (0.5, 0.12)
        self.polygon = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._history_current = self._project_data()
        self.input_canvas.set_image(image)
        self.source_info.setText(f"{self.source_path.name}  ·  {image.width} × {image.height} px")
        self._update_enabled()
        self._schedule_preview()
        return True

    def _set_crop(self, crop):
        self.crop = tuple(crop)
        self._sync_geometry()
        self._schedule_preview()

    def _set_hole(self, x: float, y: float):
        self.hole = (x, y)
        self._sync_geometry()
        self._schedule_preview()

    def _set_polygon(self, points):
        if len(points) >= 3:
            self.polygon = list(points)
            self._sync_geometry()
            self._schedule_preview()

    def reset_geometry(self):
        self.crop, self.hole = (0.0, 0.0, 1.0, 1.0), (0.5, 0.12)
        self.polygon = []
        self.input_canvas.set_mode(["crop", "hole", "outline"][self.crop_tool.currentIndex()])
        self._sync_geometry()
        self._schedule_preview()

    def _sync_geometry(self):
        if self.source is None:
            return
        shape = "Circle" if self.shape.currentText() == "圆形" else "Rectangle"
        output_width = pendant_pixel_width(self.source.size, self.crop, shape)
        fraction = self.hole_mm.value() / self.width_mm.value() * output_width / self.source.width
        self.input_canvas.set_geometry(self.crop, self.hole, fraction, self.polygon)

    def _settings(self):
        crop_left, crop_top, crop_right, crop_bottom = self.crop
        hole_in_crop = crop_left <= self.hole[0] <= crop_right and crop_top <= self.hole[1] <= crop_bottom
        x0, y0, x1, y1 = crop_pixel_box(self.source.size, self.crop)
        hx = min(1.0, max(0.0, (self.hole[0] * self.source.width - x0) / (x1 - x0)))
        hy = min(1.0, max(0.0, (self.hole[1] * self.source.height - y0) / (y1 - y0)))
        shape = "Circle" if self.shape.currentText() == "圆形" else "Rectangle"
        output_width = pendant_pixel_width(self.source.size, self.crop, shape)
        effective_width = self.width_mm.value() * self.source.width / output_width
        cleanup = CleanupSettings(self.colors.value(), effective_width,
                                  max(self.min_feature_mm.value(), self.nozzle_mm.value()),
                                  self.color_delta.value())
        requested_hole = self.hole_toggle.currentIndex() == 0
        pendant = PendantSettings(
            crop=self.crop, shape={"圆角矩形": "Rounded rectangle", "矩形": "Rectangle",
                                  "圆形": "Circle", "自定义多边形": "Polygon"}[self.shape.currentText()],
            corner_radius_mm=self.corner_mm.value(), target_width_mm=self.width_mm.value(),
            hole_enabled=False, hole_x=hx, hole_y=hy,
            hole_diameter_mm=self.hole_mm.value(), polygon=tuple(self.polygon),
        )
        wall_mm = max(1.2, self.nozzle_mm.value() * 2)
        self.hole_safe = (requested_hole and hole_in_crop
                          and hole_has_clearance(self.source.size, pendant, wall_mm)
                          and hole_has_opaque_surrounding(self.source, pendant, wall_mm))
        pendant = replace(pendant, hole_enabled=self.hole_safe)
        return cleanup, pendant

    def _schedule_preview(self, *_):
        if self.source is not None:
            self._record_history()
            self._sync_geometry()
            self.generation += 1
            self.timer.start()

    def _start_preview(self):
        if self.source is None:
            return
        if self.preview_running:
            return
        cleanup, pendant = self._settings()
        job = PreviewJob(self.generation, self.source, cleanup, pendant,
                         self.cleanup_toggle.currentIndex() == 0)
        self.active_job = job
        job.signals.finished.connect(self._preview_ready)
        job.signals.failed.connect(self._preview_failed)
        self.preview_running = True
        self.pool.start(job)
        self.status.setText("正在生成预览…")

    def _preview_ready(self, generation, prepared, output, result):
        self.preview_running = False
        self.active_job = None
        if generation != self.generation:
            self.timer.start(0)
            return
        self.prepared, self.output = prepared, output
        self.output_generation = generation
        self.preview_changed = result.changed_mask if result else None
        self._refresh_output_canvas()
        if result:
            hole_note = " · 孔附近的轮廓或透明区域无法留出材料边距；当前不会裁孔" if self.hole_toggle.currentIndex() == 0 and not self.hole_safe else ""
            self.status.setText(
                f"预览就绪 · 调色 {result.original_colors} → {result.resulting_colors} 色 · "
                f"合并 {result.merged_islands} 个小色岛 · 最小特征约 {result.minimum_feature_px}px "
                f"(阈值不低于喷嘴 {self.nozzle_mm.value():.2f}mm) · 狭窄区域提示 {result.narrow_features} 个\n"
                f"孔和背景均导出为透明像素。{hole_note}"
            )
        else:
            hole_note = " · 孔附近的轮廓或透明区域无法留出材料边距；当前不会裁孔" if self.hole_toggle.currentIndex() == 0 and not self.hole_safe else ""
            self.status.setText(f"预览就绪 · 保留原始颜色 · 孔和背景均导出为透明像素。{hole_note}")
        self._update_status()

    def _preview_failed(self, generation, message):
        self.preview_running = False
        self.active_job = None
        if generation != self.generation:
            self.timer.start(0)
        else:
            self.status.setText(f"预览失败：{message}")

    def _update_status(self, *_):
        if self.source is None:
            return
        shape = "Circle" if self.shape.currentText() == "圆形" else "Rectangle"
        width_px = pendant_pixel_width(self.source.size, self.crop, shape)
        physical_width = self.width_mm.value()
        min_px = width_px / self.width_mm.value() * max(self.min_feature_mm.value(), self.nozzle_mm.value())
        name = self.source_path.name if self.source_path else "图像"
        summary = f"{name} · 成品宽度约 {physical_width:.1f} mm · 目标最小特征约 {min_px:.1f} px"
        if min_px < 1:
            summary += " · 特征阈值低于 1 px，请提高输出分辨率或物理特征宽度"
        self.source_info.setText(summary)

    def _update_enabled(self):
        has_image = self.source is not None
        self.input_canvas.setEnabled(has_image)
        self.output_canvas.setEnabled(has_image)

    def _refresh_output_canvas(self, *_):
        if self.output is None:
            self.output_canvas.set_image(None)
            return
        preview = self.output
        if self.highlight_toggle.isChecked() and self.preview_changed is not None and self.prepared is not None:
            tint = Image.new("RGBA", self.prepared.size, (255, 74, 65, 0))
            changed_alpha = self.preview_changed.point(lambda value: value * 105 // 255)
            changed_alpha = ImageChops.multiply(changed_alpha, self.prepared.getchannel("A"))
            tint.putalpha(changed_alpha)
            preview = make_pendant(Image.alpha_composite(self.prepared, tint), self._settings()[1])
        self.output_canvas.set_image(preview)

    def _record_history(self):
        if self._loading_project:
            return
        current = self._project_data()
        if self._history_current is None:
            self._history_current = current
        elif current != self._history_current:
            self.undo_stack.append(self._history_current)
            if len(self.undo_stack) > 50:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
            self._history_current = current

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._project_data())
            self._apply_history(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self._project_data())
            self._apply_history(self.redo_stack.pop())

    def _apply_history(self, data):
        previous_loading = self._loading_project
        self._loading_project = True
        try:
            self.crop = tuple(data.get("crop", self.crop))
            self.hole = tuple(data.get("hole", self.hole))
            self.polygon = [tuple(point) for point in data.get("polygon", [])]
            self.width_mm.setValue(data.get("target_width_mm", 60))
            self.nozzle_mm.setValue(data.get("nozzle_mm", 0.4))
            self.min_feature_mm.setValue(data.get("min_feature_mm", 0.45))
            self.colors.setValue(data.get("colors", 32))
            self.color_delta.setValue(data.get("color_delta", 8))
            self.cleanup_toggle.setCurrentIndex(0 if data.get("cleanup", True) else 1)
            self.shape.setCurrentText(data.get("shape", "圆角矩形"))
            self.corner_mm.setValue(data.get("corner_radius_mm", 3))
            self.hole_toggle.setCurrentIndex(0 if data.get("hole_enabled", True) else 1)
            self.hole_mm.setValue(data.get("hole_diameter_mm", 4))
        finally:
            self._loading_project = previous_loading
        self._history_current = self._project_data()
        self._sync_geometry()
        self._schedule_preview()

    def export_png(self):
        if self.output is None:
            QMessageBox.information(self, "尚无图像", "请先打开一张照片并等待预览完成。")
            return
        if self.output_generation != self.generation:
            QMessageBox.information(self, "预览尚未更新", "请等待当前设置的预览完成后再导出。")
            return
        default = (self.source_path.stem if self.source_path else "pendant") + "-pendant.png"
        filename, _ = QFileDialog.getSaveFileName(self, "导出透明 PNG", default, "PNG 图像 (*.png)")
        if not filename:
            return
        try:
            filename = self.write_png(self.output, filename)
            QMessageBox.information(self, "已导出", f"透明 PNG 已保存：\n{filename}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    @staticmethod
    def write_png(image: Image.Image, filename: str) -> str:
        path = Path(filename)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(path.suffix + ".png" if path.suffix else ".png")
        image.save(path, format="PNG", dpi=image.info.get("dpi", (300, 300)))
        return str(path)

    def _project_data(self):
        return {
            "source_path": str(self.source_path) if self.source_path else "",
            "crop": list(self.crop), "hole": list(self.hole),
            "target_width_mm": self.width_mm.value(), "nozzle_mm": self.nozzle_mm.value(),
            "min_feature_mm": self.min_feature_mm.value(), "colors": self.colors.value(),
            "color_delta": self.color_delta.value(), "cleanup": self.cleanup_toggle.currentIndex() == 0,
            "shape": self.shape.currentText(), "corner_radius_mm": self.corner_mm.value(),
            "polygon": self.polygon,
            "hole_enabled": self.hole_toggle.currentIndex() == 0,
            "hole_diameter_mm": self.hole_mm.value(),
        }

    def save_project_dialog(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存项目", "pendant.lpproj", "项目文件 (*.lpproj)")
        if filename:
            try:
                save_project(filename, self._project_data())
            except OSError as exc:
                QMessageBox.critical(self, "保存失败", str(exc))

    def load_project_dialog(self):
        filename, _ = QFileDialog.getOpenFileName(self, "打开项目", "", "项目文件 (*.lpproj)")
        if not filename:
            return
        try:
            data = load_project(filename)
            source_path = data.get("source_path", "")
            if not source_path or not Path(source_path).exists():
                QMessageBox.information(self, "原始照片已移动", f"找不到原始照片：{source_path}\n请选择该项目使用的照片。")
                source_path, _ = QFileDialog.getOpenFileName(
                    self, "重新定位原始照片", "", "图像 (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.heic *.heif *.avif)"
                )
                if not source_path:
                    return
            self._loading_project = True
            try:
                if not self.open_image(source_path):
                    return
                self._apply_history(data)
            finally:
                self._loading_project = False
            self.undo_stack.clear(); self.redo_stack.clear()
            self._history_current = self._project_data()
            self._sync_geometry(); self._schedule_preview()
        except Exception as exc:
            QMessageBox.critical(self, "无法打开项目", str(exc))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.open_image(urls[0].toLocalFile())


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #20252b; color: #edf2f7; font-size: 13px; }
        QGroupBox { border: 1px solid #414b55; border-radius: 8px; margin-top: 12px; padding: 9px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        QComboBox, QDoubleSpinBox, QSpinBox { background: #303840; border: 1px solid #535f6b; border-radius: 5px; padding: 5px; min-height: 20px; }
        QPushButton { background: #256c58; border: 0; border-radius: 6px; padding: 8px 12px; font-weight: 600; }
        QPushButton:hover { background: #2f856b; }
        QMenuBar, QMenu { background: #20252b; }
        QSplitter::handle { background: #343d46; }
    """)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

