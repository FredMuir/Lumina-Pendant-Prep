from __future__ import annotations

import sys
import os
import tempfile
import time
from threading import Event
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSpinBox, QCheckBox,
    QColorDialog, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from .canvas import ImageCanvas
from . import __version__
from .cleanup import CleanupSettings, ProcessingCancelled, simplify_image
from .image_io import open_rgba, SRGB_PROFILE
from .project import load_project, save_project, source_path_for_project, resolve_project_source
from .render import (PendantSettings, crop_pixel_box, hole_has_clearance, hole_has_opaque_surrounding,
                     make_pendant, pendant_pixel_width)


class PreviewSignals(QObject):
    finished = Signal(int, object, object, object)
    failed = Signal(int, str)
    cancelled = Signal(int)


class PreviewJob(QRunnable):
    def __init__(self, generation: int, image: Image.Image, cleanup: CleanupSettings,
                 pendant: PendantSettings, enabled: bool, protected_regions=(), max_dimension=0):
        super().__init__()
        self.generation = generation
        self.image = image
        self.max_dimension = max_dimension
        self.cleanup = cleanup
        self.pendant = pendant
        self.enabled = enabled
        self.protected_regions = tuple(tuple(region) for region in protected_regions)
        self.signals = PreviewSignals()
        self.cancel_requested = Event()

    def cancel(self):
        self.cancel_requested.set()

    def _check_cancel(self):
        if self.cancel_requested.is_set():
            raise ProcessingCancelled()

    def run(self):
        try:
            self._check_cancel()
            if self.max_dimension and max(self.image.size) > self.max_dimension:
                image = self.image.copy()
                image.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)
                self.image = image
            self._check_cancel()
            if self.enabled:
                # Analyze only pixels that survive the pendant outline. Restore
                # the original alpha before final rendering to avoid applying
                # antialiased outline transparency twice.
                outline = make_pendant(self.image, replace(self.pendant, hole_enabled=False, outer_border_enabled=False))
                self._check_cancel()
                x0, y0, x1, y1 = crop_pixel_box(self.image.size, self.pendant.crop)
                if self.pendant.shape == "Circle":
                    side = min(x1 - x0, y1 - y0)
                    x0 += (x1 - x0 - side) // 2
                    y0 += (y1 - y0 - side) // 2
                selection = Image.new("L", self.image.size, 0)
                selection.paste(outline.getchannel("A"), (x0, y0))
                protection = Image.new("L", self.image.size, 0)
                draw = ImageDraw.Draw(protection)
                for region in self.protected_regions:
                    left, top, right, bottom = crop_pixel_box(self.image.size, region)
                    draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
                selection = ImageChops.multiply(selection, ImageChops.invert(protection))
                analysis_image = self.image.convert("RGBA")
                original_alpha = analysis_image.getchannel("A")
                analysis_image.putalpha(selection)
                result = simplify_image(analysis_image, self.cleanup, self.cancel_requested.is_set)
                prepared = result.image
                prepared.paste(self.image.convert("RGBA"), mask=protection)
                prepared.putalpha(original_alpha)
            else:
                result = None
                prepared = self.image
            self._check_cancel()
            output = make_pendant(prepared, self.pendant)
            self._check_cancel()
            self.signals.finished.emit(self.generation, prepared, output, result)
        except ProcessingCancelled:
            self.signals.cancelled.emit(self.generation)
        except Exception as exc:
            self.signals.failed.emit(self.generation, str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Lumina Pendant Prep {__version__}")
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.source: Image.Image | None = None
        self.source_path: Path | None = None
        self.crop = (0.0, 0.0, 1.0, 1.0)
        self.hole = (0.5, 0.12)
        self.polygon: list[tuple[float, float]] = []
        self.protected_regions: list[tuple[float, float, float, float]] = []
        self.prepared: Image.Image | None = None
        self.output: Image.Image | None = None
        self.preview_changed: Image.Image | None = None
        self.preview_quantized: Image.Image | None = None
        self.preview_merged: Image.Image | None = None
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
        self.processing_toggle = QCheckBox("开始处理并生成预览")
        self.processing_toggle.setToolTip("打开照片后先调整参数；勾选后才开始净化和生成导出预览。")
        self.processing_toggle.toggled.connect(self._processing_changed)
        control_layout.addWidget(self.processing_toggle)

        print_group = QGroupBox("打印与色块净化")
        form = QFormLayout(print_group)
        self.width_mm = self._double(60, 1, 300, 0.5)
        self.nozzle_mm = QComboBox()
        for diameter in (0.2, 0.4, 0.6):
            self.nozzle_mm.addItem(f"{diameter:.1f}", diameter)
        self.nozzle_mm.setCurrentIndex(1)
        self.min_feature_mm = self._double(0.45, 0, 5, 0.05)
        self.colors = QSpinBox(); self.colors.setRange(2, 128); self.colors.setValue(32)
        self.color_delta = self._double(8, 0, 40, 1)
        self.resolution = QComboBox()
        self.resolution.addItem("长边 2400 px（推荐）", 2400)
        self.resolution.addItem("长边 4096 px", 4096)
        self.resolution.addItem("原始分辨率（耗内存）", 0)
        self.cleanup_toggle = QComboBox(); self.cleanup_toggle.addItems(["净化色块", "保留原色"])
        self.cleanup_toggle.setToolTip("保留原色会跳过颜色量化和色块合并，仍会应用裁切和透明孔。")
        self.cleanup_toggle.currentIndexChanged.connect(self._update_cleanup_controls)
        form.addRow("目标宽度 (mm)", self.width_mm)
        form.addRow("喷嘴直径 (mm)", self.nozzle_mm)
        form.addRow("最小特征宽度 (mm)", self.min_feature_mm)
        form.addRow("颜色上限", self.colors)
        form.addRow("相近色阈值", self.color_delta)
        form.addRow("处理模式", self.cleanup_toggle)
        form.addRow("输出分辨率", self.resolution)
        gentle_preset = QPushButton("温和预设（试用）")
        gentle_preset.setToolTip("启用净化，颜色上限 96，相近色阈值 2，最小特征宽度设为当前喷嘴直径；效果需通过预览和打印确认。")
        gentle_preset.clicked.connect(self._apply_gentle_preset)
        form.addRow(gentle_preset)
        control_layout.addWidget(print_group)

        shape_group = QGroupBox("挂坠裁切")
        shape_form = QFormLayout(shape_group)
        self.shape = QComboBox(); self.shape.addItems(["圆角矩形", "矩形", "圆形", "自定义多边形"])
        self.corner_mm = self._double(3, 0, 30, 0.5)
        self.crop_tool = QComboBox(); self.crop_tool.addItems(["拖动设置裁切框", "点击设置孔位置", "点击绘制多边形", "拖动添加保护区域"])
        self.hole_toggle = QComboBox(); self.hole_toggle.addItems(["启用透明挂绳孔", "关闭挂绳孔"])
        self.hole_mm = self._double(4, 0.5, 30, 0.5)
        self.hole_x_percent = self._double(50, 0, 100, 1)
        self.hole_y_percent = self._double(12, 0, 100, 1)
        self.hole_compensation_mm = self._double(0, 0, 5, 0.05)
        self.hole_compensation_mm.setToolTip("将补偿值加到目标孔径后裁切 PNG。默认 0；请根据切片或实物测量调整。")
        shape_form.addRow("外形", self.shape)
        shape_form.addRow("圆角半径 (mm)", self.corner_mm)
        shape_form.addRow("画布操作", self.crop_tool)
        shape_form.addRow("挂绳孔", self.hole_toggle)
        shape_form.addRow("孔水平位置 (%)", self.hole_x_percent)
        shape_form.addRow("孔垂直位置 (%)", self.hole_y_percent)
        shape_form.addRow("目标孔径 (mm)", self.hole_mm)
        shape_form.addRow("孔径补偿 (mm)", self.hole_compensation_mm)
        self.outer_border_toggle = QCheckBox("启用外边框（向外扩展）")
        self.outer_border_mm = self._double(1, .1, 5, .1)
        self.outer_border_color = "#ffffff"
        self.outer_color_button = QPushButton("选择外边框颜色")
        self.outer_color_button.clicked.connect(lambda: self._choose_border_color("outer"))
        self.hole_border_toggle = QCheckBox("启用孔内边框")
        self.hole_border_toggle.setToolTip("边框位于孔内透明区域，不覆盖照片；会缩小孔的透明开口。")
        self.hole_border_mm = self._double(.5, .1, 5, .1)
        self.hole_border_color = "#ffffff"
        self.hole_color_button = QPushButton("选择孔边框颜色")
        self.hole_color_button.clicked.connect(lambda: self._choose_border_color("hole"))
        shape_form.addRow(self.outer_border_toggle)
        shape_form.addRow("外边框宽度 (mm)", self.outer_border_mm)
        shape_form.addRow(self.outer_color_button)
        shape_form.addRow(self.hole_border_toggle)
        shape_form.addRow("孔边框宽度 (mm)", self.hole_border_mm)
        shape_form.addRow(self.hole_color_button)
        control_layout.addWidget(shape_group)
        edit_hint = QLabel("拖动照片设置裁切框或添加保护区域；保护区域保留原色。绘制多边形：左键添加节点，右键结束。")
        edit_hint.setWordWrap(True)
        edit_hint.setStyleSheet("color:#9caab7;padding:4px")
        control_layout.addWidget(edit_hint)
        self.reset_crop = QPushButton("重置裁切框")
        self.reset_crop.clicked.connect(self.reset_geometry)
        control_layout.addWidget(self.reset_crop)
        clear_protection = QPushButton("清除保护区域")
        clear_protection.clicked.connect(self._clear_protection)
        control_layout.addWidget(clear_protection)
        self.status = QLabel("打开一张照片开始处理。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#b8c2cc;padding:8px")
        control_layout.addWidget(self.status)
        control_layout.addStretch(1)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls_scroll.setMinimumWidth(290)
        self.controls_scroll.setMaximumWidth(365)
        self.controls_scroll.setWidget(controls)
        splitter.addWidget(self.controls_scroll)

        previews = QWidget()
        preview_layout = QVBoxLayout(previews)
        headings = QHBoxLayout()
        headings.addWidget(QLabel("原图 / 裁切操作"))
        headings.addStretch()
        headings.addWidget(QLabel("挂坠 PNG 预览（棋盘格为透明）"))
        self.highlight_toggle = QCheckBox("标记已简化区域")
        self.highlight_toggle.toggled.connect(self._refresh_output_canvas)
        headings.addWidget(self.highlight_toggle)
        self.highlight_mode = QComboBox()
        self.highlight_mode.addItems(["所有变化", "颜色量化", "区域合并"])
        self.highlight_mode.currentIndexChanged.connect(self._refresh_output_canvas)
        headings.addWidget(self.highlight_mode)
        preview_layout.addLayout(headings)
        zoom_row = QHBoxLayout()
        zoom_row.addStretch()
        zoom_row.addWidget(QLabel("前后同步缩放"))
        self.output_zoom = QComboBox()
        for label, factor in (("适合窗口", 1), ("2 倍", 2), ("4 倍", 4)):
            self.output_zoom.addItem(label, factor)
        zoom_row.addWidget(self.output_zoom)
        zoom_row.addWidget(QLabel("放大后用滚动条查看"))
        preview_layout.addLayout(zoom_row)
        panes = QSplitter(Qt.Orientation.Horizontal)
        self.input_canvas = ImageCanvas(interactive=True)
        self.output_canvas = ImageCanvas(interactive=False)
        self.input_scroll = QScrollArea()
        self.input_scroll.setWidgetResizable(True)
        self.input_scroll.setWidget(self.input_canvas)
        self.output_scroll = QScrollArea()
        self.output_scroll.setWidgetResizable(True)
        self.output_scroll.setWidget(self.output_canvas)
        self.output_zoom.currentIndexChanged.connect(self._set_output_zoom)
        for axis in ("horizontalScrollBar", "verticalScrollBar"):
            left = getattr(self.input_scroll, axis)()
            right = getattr(self.output_scroll, axis)()
            left.valueChanged.connect(lambda value, a=left, b=right: self._sync_scroll(a, b, value))
            right.valueChanged.connect(lambda value, a=right, b=left: self._sync_scroll(a, b, value))
        panes.addWidget(self.input_scroll); panes.addWidget(self.output_scroll)
        preview_layout.addWidget(panes, 1)
        self.source_info = QLabel("支持 JPEG、PNG、WebP、BMP、TIFF、HEIC/HEIF、AVIF")
        self.source_info.setWordWrap(True)
        preview_layout.addWidget(self.source_info)
        splitter.addWidget(previews)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(root)

        self.input_canvas.cropChanged.connect(self._set_crop)
        self.input_canvas.holeMoved.connect(self._set_hole)
        self.input_canvas.polygonChanged.connect(self._set_polygon)
        self.input_canvas.protectionAdded.connect(self._add_protection)
        self.input_canvas.outlineFinished.connect(lambda: self.crop_tool.setCurrentIndex(0))
        self.hole_x_percent.valueChanged.connect(self._hole_parameters_changed)
        self.hole_y_percent.valueChanged.connect(self._hole_parameters_changed)
        self.outer_border_toggle.toggled.connect(self._schedule_preview)
        self.hole_border_toggle.toggled.connect(self._schedule_preview)
        self.crop_tool.currentIndexChanged.connect(
            lambda i: self.input_canvas.set_mode(["crop", "hole", "outline", "protect"][i])
        )
        for widget in (self.width_mm, self.min_feature_mm, self.colors, self.color_delta,
                       self.shape, self.corner_mm, self.hole_toggle, self.hole_mm, self.hole_compensation_mm,
                       self.cleanup_toggle, self.resolution, self.outer_border_mm, self.hole_border_mm):
            signal = widget.valueChanged if isinstance(widget, (QDoubleSpinBox, QSpinBox)) else widget.currentIndexChanged
            signal.connect(self._schedule_preview)
        self.nozzle_mm.currentIndexChanged.connect(self._update_status)
        self.nozzle_mm.currentIndexChanged.connect(self._schedule_preview)
        self.resolution.currentIndexChanged.connect(self._update_status)

    def _update_cleanup_controls(self, *_):
        enabled = self.cleanup_toggle.currentIndex() == 0
        self.colors.setEnabled(enabled)
        self.color_delta.setEnabled(enabled)

    def _set_output_zoom(self, *_):
        factor = self.output_zoom.currentData()
        for scroll, canvas in ((self.input_scroll, self.input_canvas), (self.output_scroll, self.output_canvas)):
            if factor == 1:
                canvas.setMinimumSize(280, 250)
            else:
                viewport = scroll.viewport().size()
                canvas.setMinimumSize(max(280, viewport.width() * factor),
                                      max(250, viewport.height() * factor))

    @staticmethod
    def _sync_scroll(source, target, value):
        maximum = source.maximum()
        with QSignalBlocker(target):
            target.setValue(round(value / maximum * target.maximum()) if maximum else 0)

    def _apply_gentle_preset(self):
        widgets = (self.colors, self.color_delta, self.min_feature_mm, self.cleanup_toggle)
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.colors.setValue(96)
        self.color_delta.setValue(2)
        self.min_feature_mm.setValue(self.nozzle_mm.currentData())
        self.cleanup_toggle.setCurrentIndex(0)
        del blockers
        self._update_cleanup_controls()
        self._schedule_preview()

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
        if self.active_job is not None:
            self.active_job.cancel()
        self.timer.stop()
        with QSignalBlocker(self.processing_toggle):
            self.processing_toggle.setChecked(False)
        self.generation += 1
        self.output_generation = -1
        self.prepared = self.output = self.preview_changed = None
        self.preview_quantized = self.preview_merged = None
        self.output_canvas.set_image(None)
        self.source, self.source_path = image, Path(filename)
        self.crop, self.hole = (0.0, 0.0, 1.0, 1.0), (0.5, 0.12)
        self.polygon = []
        self.protected_regions = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._history_current = self._project_data()
        self.input_canvas.set_image(image)
        self.input_canvas.set_protected_regions(self.protected_regions)
        self._update_status()
        self._update_enabled()
        self._sync_geometry()
        self.status.setText("照片已打开。调整参数后勾选“开始处理并生成预览”。")
        return True

    def _processing_changed(self, checked):
        if checked:
            if self.source is None:
                with QSignalBlocker(self.processing_toggle):
                    self.processing_toggle.setChecked(False)
                return
            self._schedule_preview()
        else:
            self.timer.stop()
            self.generation += 1
            if self.active_job is not None:
                self.active_job.cancel()
            self.output_generation = -1
            self.status.setText("处理已停止；勾选开始后重新生成预览。")

    def _set_crop(self, crop):
        self.crop = tuple(crop)
        self._sync_geometry()
        self._schedule_preview()

    def _set_hole(self, x: float, y: float):
        self.hole = (x, y)
        self._sync_geometry()
        self._schedule_preview()

    def _hole_parameters_changed(self, *_):
        self._set_hole(self.hole_x_percent.value() / 100, self.hole_y_percent.value() / 100)

    def _choose_border_color(self, kind):
        current = self.outer_border_color if kind == "outer" else self.hole_border_color
        color = QColorDialog.getColor(QColor(current), self, "选择边框颜色")
        if color.isValid():
            if kind == "outer":
                self.outer_border_color = color.name()
            else:
                self.hole_border_color = color.name()
            self._update_border_buttons()
            self._schedule_preview()

    def _update_border_buttons(self):
        self.outer_color_button.setText(f"外边框颜色 {self.outer_border_color}")
        self.hole_color_button.setText(f"孔边框颜色 {self.hole_border_color}")

    def _set_polygon(self, points):
        self.polygon = list(points) if len(points) >= 3 else []
        self._sync_geometry()
        self._schedule_preview()

    def _add_protection(self, region):
        if len(self.protected_regions) >= 100:
            self.status.setText("保护区域最多 100 个；可先清除后重新绘制。")
            return
        self.protected_regions.append(tuple(region))
        self._sync_geometry()
        self._schedule_preview()

    def _clear_protection(self):
        if self.protected_regions:
            self.protected_regions = []
            self._sync_geometry()
            self._schedule_preview()

    def reset_geometry(self):
        self.crop, self.hole = (0.0, 0.0, 1.0, 1.0), (0.5, 0.12)
        self.polygon = []
        self.input_canvas.set_mode(["crop", "hole", "outline", "protect"][self.crop_tool.currentIndex()])
        self._sync_geometry()
        self._schedule_preview()

    def _sync_geometry(self):
        if self.source is None:
            return
        shape = "Circle" if self.shape.currentText() == "圆形" else "Rectangle"
        output_width = pendant_pixel_width(self.source.size, self.crop, shape)
        fraction = self._effective_hole_mm() / self.width_mm.value() * output_width / self.source.width
        self.input_canvas.set_geometry(self.crop, self.hole, fraction, self.polygon)
        self.input_canvas.set_protected_regions(self.protected_regions)
        with QSignalBlocker(self.hole_x_percent), QSignalBlocker(self.hole_y_percent):
            self.hole_x_percent.setValue(self.hole[0] * 100)
            self.hole_y_percent.setValue(self.hole[1] * 100)

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
                                  max(self.min_feature_mm.value(), self.nozzle_mm.currentData()),
                                  self.color_delta.value())
        requested_hole = self.hole_toggle.currentIndex() == 0
        pendant = PendantSettings(
            crop=self.crop, shape={"圆角矩形": "Rounded rectangle", "矩形": "Rectangle",
                                  "圆形": "Circle", "自定义多边形": "Polygon"}[self.shape.currentText()],
            corner_radius_mm=self.corner_mm.value(), target_width_mm=self.width_mm.value(),
            hole_enabled=False, hole_x=hx, hole_y=hy,
            hole_diameter_mm=self._effective_hole_mm(), polygon=tuple(self.polygon),
            outer_border_enabled=self.outer_border_toggle.isChecked(), outer_border_mm=self.outer_border_mm.value(),
            outer_border_color=self.outer_border_color,
            hole_border_enabled=self.hole_border_toggle.isChecked(), hole_border_mm=self.hole_border_mm.value(),
            hole_border_color=self.hole_border_color,
        )
        wall_mm = max(1.2, self.nozzle_mm.currentData() * 2)
        self.hole_safe = (requested_hole and hole_in_crop
                          and (not self.hole_border_toggle.isChecked() or 2 * self.hole_border_mm.value() < self._effective_hole_mm())
                          and hole_has_clearance(self.source.size, pendant, wall_mm)
                          and hole_has_opaque_surrounding(self.source, pendant, wall_mm))
        pendant = replace(pendant, hole_enabled=self.hole_safe)
        return cleanup, pendant

    def _effective_hole_mm(self):
        return self.hole_mm.value() + self.hole_compensation_mm.value()

    def _schedule_preview(self, *_):
        if self.source is not None:
            self._record_history()
            self._sync_geometry()
            self._update_status()
            self.generation += 1
            if self.active_job is not None:
                self.active_job.cancel()
            if self.processing_toggle.isChecked():
                self.timer.start()

    def _start_preview(self):
        if self.source is None or not self.processing_toggle.isChecked():
            return
        if self.preview_running:
            return
        if self.shape.currentText() == "自定义多边形" and len(self.polygon) < 3:
            self.prepared = self.output = self.preview_changed = None
            self.preview_quantized = self.preview_merged = None
            self.output_canvas.set_image(None)
            self.status.setText("请用多边形工具至少绘制三个节点，并右键完成轮廓。")
            return
        cleanup, pendant = self._settings()
        job = PreviewJob(self.generation, self.source, cleanup, pendant,
                         self.cleanup_toggle.currentIndex() == 0, self.protected_regions,
                         self.resolution.currentData())
        self.active_job = job
        job.signals.finished.connect(self._preview_ready)
        job.signals.failed.connect(self._preview_failed)
        job.signals.cancelled.connect(self._preview_cancelled)
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
        self.preview_quantized = result.quantized_mask if result else None
        self.preview_merged = result.merged_mask if result else None
        self._refresh_output_canvas()
        if result:
            total = result.visible_pixels or 1
            hole_note = " · 孔附近的轮廓或透明区域无法留出材料边距；当前不会裁孔" if self.hole_toggle.currentIndex() == 0 and not self.hole_safe else ""
            self.status.setText(
                f"预览就绪 · 调色 {result.original_colors} → {result.resulting_colors} 色 · "
                f"合并 {result.merged_islands} 个小色岛 · 最小特征约 {result.minimum_feature_px}px "
                f"(阈值不低于喷嘴 {self.nozzle_mm.currentData():.2f}mm) · 狭窄区域提示 {result.narrow_features} 个\n"
                f"量化改变 {result.quantized_pixels / total:.1%} · 区域合并改变 {result.merged_pixels / total:.1%} 的待处理像素（孔切除前、不含保护区域，可重叠）。\n"
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

    def _preview_cancelled(self, generation):
        self.preview_running = False
        self.active_job = None
        if self.source is not None and self.processing_toggle.isChecked() and generation != self.generation:
            self.timer.start(0)

    def closeEvent(self, event):
        self.timer.stop()
        if self.active_job is not None:
            self.active_job.cancel()
        super().closeEvent(event)

    def _update_status(self, *_):
        if self.source is None:
            return
        shape = "Circle" if self.shape.currentText() == "圆形" else "Rectangle"
        image_size = self.source.size
        limit = self.resolution.currentData()
        if limit and max(image_size) > limit:
            ratio = limit / max(image_size)
            image_size = (max(1, round(image_size[0] * ratio)), max(1, round(image_size[1] * ratio)))
        width_px = pendant_pixel_width(image_size, self.crop, shape)
        physical_width = self.width_mm.value()
        min_px = width_px / self.width_mm.value() * max(self.min_feature_mm.value(), self.nozzle_mm.currentData())
        name = self.source_path.name if self.source_path else "图像"
        summary = f"{name} · 处理尺寸 {image_size[0]} × {image_size[1]} px · 照片宽度约 {physical_width:.1f} mm · 目标最小特征约 {min_px:.1f} px"
        color_status = self.source.info.get("lpp_color_status")
        color_note = {"converted_srgb": "ICC 已转换为 sRGB",
                      "assumed_srgb": "无 ICC，按 sRGB 处理",
                      "invalid_icc": "ICC 无法转换，按 sRGB 处理，颜色可能不准确"}.get(color_status)
        if color_note:
            summary += f" · {color_note}"
        frame_count = self.source.info.get("lpp_frame_count", 1)
        if frame_count > 1:
            frame_index = self.source.info.get("lpp_frame_index", 0) + 1
            kind = "主图" if self.source.info.get("lpp_frame_is_primary") else "第一页"
            summary += f" · 文件含 {frame_count} 张，仅加载{kind}（第 {frame_index} 张）"
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
        mask = (self.preview_changed, self.preview_quantized, self.preview_merged)[self.highlight_mode.currentIndex()]
        if self.highlight_toggle.isChecked() and mask is not None and self.prepared is not None:
            tint = Image.new("RGBA", self.prepared.size, (255, 74, 65, 0))
            changed_alpha = mask.point(lambda value: value * 105 // 255)
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
            self.protected_regions = [tuple(region) for region in data.get("protected_regions", [])]
            self.width_mm.setValue(data.get("target_width_mm", 60))
            self.nozzle_mm.setCurrentIndex(self.nozzle_mm.findData(data.get("nozzle_mm", 0.4)))
            self.min_feature_mm.setValue(data.get("min_feature_mm", 0.45))
            self.colors.setValue(data.get("colors", 32))
            self.color_delta.setValue(data.get("color_delta", 8))
            self.resolution.setCurrentIndex(max(0, self.resolution.findData(data.get("max_dimension", 2400))))
            self.cleanup_toggle.setCurrentIndex(0 if data.get("cleanup", True) else 1)
            self.shape.setCurrentText(data.get("shape", "圆角矩形"))
            self.corner_mm.setValue(data.get("corner_radius_mm", 3))
            self.hole_toggle.setCurrentIndex(0 if data.get("hole_enabled", True) else 1)
            self.hole_mm.setValue(data.get("hole_diameter_mm", 4))
            self.hole_compensation_mm.setValue(data.get("hole_compensation_mm", 0))
            self.outer_border_toggle.setChecked(data.get("outer_border_enabled", False))
            self.outer_border_mm.setValue(data.get("outer_border_mm", 1))
            self.outer_border_color = data.get("outer_border_color", "#ffffff")
            self.hole_border_toggle.setChecked(data.get("hole_border_enabled", False))
            self.hole_border_mm.setValue(data.get("hole_border_mm", .5))
            self.hole_border_color = data.get("hole_border_color", "#ffffff")
            self._update_border_buttons()
        finally:
            self._loading_project = previous_loading
        self._history_current = self._project_data()
        self._sync_geometry()
        self._schedule_preview()

    def export_png(self):
        if self.shape.currentText() == "自定义多边形" and len(self.polygon) < 3:
            QMessageBox.information(self, "轮廓未完成", "请至少绘制三个节点并右键完成多边形后再导出。")
            return
        if self.output is None:
            QMessageBox.information(self, "尚无图像", "请先打开一张照片并等待预览完成。")
            return
        if self.output_generation != self.generation:
            QMessageBox.information(self, "预览尚未更新", "请等待当前设置的预览完成后再导出。")
            return
        if self.hole_toggle.currentIndex() == 0 and not self.hole_safe:
            QMessageBox.information(self, "挂绳孔无法裁切",
                                    "当前孔的位置、大小或周围透明区域无法留出足够边距，预览中没有裁孔。"
                                    "请调整孔位置或孔径；如不需要孔，请先关闭挂绳孔。")
            return
        default = (self.source_path.stem if self.source_path else "pendant") + "-pendant.png"
        filename, _ = QFileDialog.getSaveFileName(self, "导出透明 PNG", default, "PNG 图像 (*.png)")
        if not filename:
            return
        if self._is_source_path(self._png_path(filename)):
            QMessageBox.critical(self, "无法导出", "导出路径与原照片相同，请选择其他文件名。")
            return
        try:
            filename = self.write_png(self.output, filename)
            dpi = self.output.info.get("dpi", (300, 300))[0]
            width_mm = 25.4 * self.output.width / dpi
            height_mm = 25.4 * self.output.height / dpi
            if self.hole_toggle.currentIndex() == 0:
                clear_hole = self._effective_hole_mm() - (2 * self.hole_border_mm.value() if self.hole_border_toggle.isChecked() else 0)
                hole_info = f"挂绳孔透明开口约 {clear_hole:g} mm" if self.hole_border_toggle.isChecked() else f"挂绳孔直径约 {clear_hole:g} mm"
                if self.hole_compensation_mm.value():
                    hole_info += (f"（目标 {self.hole_mm.value():g} + "
                                  f"补偿 {self.hole_compensation_mm.value():g} mm）")
                hole_info += "。"
            else:
                hole_info = "挂绳孔已关闭。"
            QMessageBox.information(self, "已导出", f"透明 PNG 已保存：\n{filename}\n\n"
                                    f"目标尺寸约 {width_mm:g} × {height_mm:.2f} mm；{hole_info}\n"
                                    f"导入 Lumina 时请将目标宽度设为 {width_mm:g} mm，"
                                    "否则孔和成品尺寸会按比例改变。\n"
                                    "孔径是图片中的设计值；建模和切片可能改变实际开口，请检查切片结果。")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    @staticmethod
    def _png_path(filename: str) -> Path:
        path = Path(filename)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(path.suffix + ".png" if path.suffix else ".png")
        return path

    def _is_source_path(self, path: Path) -> bool:
        if self.source_path is None:
            return False
        try:
            if path.resolve() == self.source_path.resolve():
                return True
            return path.exists() and self.source_path.exists() and path.samefile(self.source_path)
        except OSError:
            # Do not write if a filesystem error prevents a safe comparison.
            return True

    @staticmethod
    def write_png(image: Image.Image, filename: str) -> str:
        path = MainWindow._png_path(filename)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w+b", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                image.save(file, format="PNG", dpi=image.info.get("dpi", (300, 300)),
                           icc_profile=SRGB_PROFILE)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return str(path)

    def _project_data(self):
        return {
            "source_path": str(self.source_path) if self.source_path else "",
            "crop": list(self.crop), "hole": list(self.hole),
            "target_width_mm": self.width_mm.value(), "nozzle_mm": self.nozzle_mm.currentData(),
            "min_feature_mm": self.min_feature_mm.value(), "colors": self.colors.value(),
            "color_delta": self.color_delta.value(), "cleanup": self.cleanup_toggle.currentIndex() == 0,
            "max_dimension": self.resolution.currentData(),
            "shape": self.shape.currentText(), "corner_radius_mm": self.corner_mm.value(),
            "polygon": self.polygon,
            "protected_regions": self.protected_regions,
            "hole_enabled": self.hole_toggle.currentIndex() == 0,
            "hole_diameter_mm": self.hole_mm.value(),
            "hole_compensation_mm": self.hole_compensation_mm.value(),
            "outer_border_enabled": self.outer_border_toggle.isChecked(),
            "outer_border_mm": self.outer_border_mm.value(), "outer_border_color": self.outer_border_color,
            "hole_border_enabled": self.hole_border_toggle.isChecked(),
            "hole_border_mm": self.hole_border_mm.value(), "hole_border_color": self.hole_border_color,
        }

    def save_project_dialog(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存项目", "pendant.lpproj", "项目文件 (*.lpproj)")
        if filename:
            if self._is_source_path(Path(filename)):
                QMessageBox.critical(self, "无法保存项目", "项目路径与原照片相同，请选择其他文件名。")
                return
            try:
                data = self._project_data()
                data["source_path"] = source_path_for_project(filename, data["source_path"])
                save_project(filename, data)
            except OSError as exc:
                QMessageBox.critical(self, "保存失败", str(exc))

    def load_project_dialog(self):
        filename, _ = QFileDialog.getOpenFileName(self, "打开项目", "", "项目文件 (*.lpproj)")
        if not filename:
            return
        try:
            data = load_project(filename)
            old_nozzle = data.get("nozzle_mm", 0.4)
            if self.nozzle_mm.findData(old_nozzle) < 0:
                new_nozzle = min((0.2, 0.4, 0.6), key=lambda value: (abs(value - old_nozzle), -value))
                QMessageBox.information(self, "喷嘴直径已调整",
                                        f"项目原来的喷嘴直径为 {old_nozzle:g} mm，当前使用最接近的 {new_nozzle:.1f} mm。请检查打印设置。")
                data["nozzle_mm"] = new_nozzle
            source_path = resolve_project_source(filename, data.get("source_path", ""))
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
    smoke = len(sys.argv) == 4 and sys.argv[1] == "--smoke-test"
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
    if smoke:
        source, destination = sys.argv[2:4]
        window.cleanup_toggle.setCurrentText("保留原色")
        # Avoid a modal error dialog if the fixture is missing or undecodable.
        try:
            open_rgba(source)
        except Exception:
            sys.exit(2)
        if not window.open_image(source):
            sys.exit(2)
        window.processing_toggle.setChecked(True)
        deadline = time.monotonic() + 90

        def finish_smoke():
            if window.output is not None and window.output_generation == window.generation:
                try:
                    window.write_png(window.output, destination)
                except Exception:
                    app.exit(3)
                    return
                app.exit(0)
            elif time.monotonic() >= deadline or window.status.text().startswith("预览失败"):
                app.exit(4)
            else:
                QTimer.singleShot(100, finish_smoke)

        QTimer.singleShot(0, finish_smoke)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

