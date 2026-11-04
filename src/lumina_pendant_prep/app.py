from __future__ import annotations

import sys
import os
import math
import tempfile
import time
from threading import Event
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSpinBox, QCheckBox,
    QColorDialog, QDialog, QInputDialog, QProgressBar, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from .canvas import ImageCanvas
from . import __version__
from .cleanup import CleanupSettings, ProcessingCancelled, simplify_image
from .image_io import open_rgba, SRGB_PROFILE
from .depth import estimate_relative_depth, align_depth, depth_heightmap
from .project import load_project, save_project, source_path_for_project, resolve_project_source
from .presets import load_presets, write_presets
from .render import (PendantSettings, crop_pixel_box, hole_has_clearance, hole_has_opaque_surrounding,
                     make_pendant, pendant_pixel_width)


class PreviewSignals(QObject):
    finished = Signal(int, object, object, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    progress = Signal(int, str)


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
            self.signals.progress.emit(self.generation, "步骤 1/7：准备图片" if self.enabled else "步骤 1/2：准备图片")
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
                steps = {"检测纯色区域": 2, "颜色量化": 3, "合并相近色块": 4,
                         "检查并合并小色块": 5, "生成处理结果": 6}
                result = simplify_image(analysis_image, self.cleanup, self.cancel_requested.is_set,
                                        lambda stage: self.signals.progress.emit(
                                            self.generation, f"步骤 {steps[stage]}/7：{stage}"))
                prepared = result.image
                prepared.paste(self.image.convert("RGBA"), mask=protection)
                prepared.putalpha(original_alpha)
            else:
                result = None
                prepared = self.image
            self._check_cancel()
            self.signals.progress.emit(self.generation, "步骤 7/7：生成挂坠预览" if self.enabled
                                       else "步骤 2/2：生成挂坠预览")
            output = make_pendant(prepared, self.pendant)
            self._check_cancel()
            self.signals.finished.emit(self.generation, prepared, output, result)
        except ProcessingCancelled:
            self.signals.cancelled.emit(self.generation)
        except Exception as exc:
            self.signals.failed.emit(self.generation, str(exc))


class DepthSignals(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)


class DepthJob(QRunnable):
    def __init__(self, generation, image, processed_size, output, pendant):
        super().__init__()
        self.generation = generation
        self.image = image
        self.processed_size = processed_size
        self.output = output
        self.pendant = pendant
        self.signals = DepthSignals()
        self.cancel_requested = Event()

    def cancel(self):
        self.cancel_requested.set()

    def run(self):
        try:
            source = self.image
            if source.size != self.processed_size:
                source = source.resize(self.processed_size, Image.Resampling.LANCZOS)
            depth = estimate_relative_depth(source, self.cancel_requested.is_set)
            _, border_mask = make_pendant(source, self.pendant, return_border_mask=True)
            result = align_depth(depth, self.output, self.pendant, border_mask)
            if self.cancel_requested.is_set():
                raise InterruptedError()
            self.signals.finished.emit(self.generation, (result, border_mask))
        except InterruptedError:
            self.signals.cancelled.emit(self.generation)
        except Exception as exc:
            self.signals.failed.emit(self.generation, str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Lumina Pendant Prep {__version__}")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "app.png")))
        self.resize(1320, 820)
        self.setAcceptDrops(True)
        self.source: Image.Image | None = None
        self.source_path: Path | None = None
        self.last_color_export_path: Path | None = None
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
        self.pending_start = False
        self.preview_pendant: PendantSettings | None = None
        self.depth_raw: Image.Image | None = None
        self.depth_border_mask: Image.Image | None = None
        self.depth_generation = -1
        self.depth_job: DepthJob | None = None
        self.depth_dialog: QDialog | None = None
        self.depth_view: ImageCanvas | None = None
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self._history_current: dict | None = None
        self._loading_project = False
        self._preset_pending_photo = False
        self._presets = {}
        self._preset_error = None
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self._start_preview)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(250)
        self.progress_timer.timeout.connect(self._update_progress_label)
        self.progress_started = None
        self.progress_stage = ""
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
        self.processing_toggle = QPushButton("开始处理")
        self.processing_toggle.setCheckable(True)
        self.processing_toggle.setToolTip("打开照片后先调整参数；点击开始处理，再次点击可停止。")
        self.processing_toggle.toggled.connect(self._processing_changed)
        toolbar.addWidget(open_button, 1)
        toolbar.addWidget(export_button, 1)
        toolbar.addWidget(self.processing_toggle, 1)
        control_layout.addLayout(toolbar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_label = QLabel("等待开始处理")
        control_layout.addWidget(self.progress_bar)
        control_layout.addWidget(self.progress_label)

        preset_group = QGroupBox("参数预设（处理前选择）")
        preset_layout = QVBoxLayout(preset_group)
        self.preset_select = QComboBox()
        self.preset_select.currentIndexChanged.connect(self._preset_selected)
        preset_layout.addWidget(self.preset_select)
        preset_row1 = QHBoxLayout()
        preset_row2 = QHBoxLayout()
        for title, action, row in (("保存为新预设", self.save_new_preset, preset_row1),
                                   ("更新所选预设", self.update_preset, preset_row1),
                                   ("重命名", self.rename_preset, preset_row2),
                                   ("删除", self.delete_preset, preset_row2)):
            button = QPushButton(title)
            button.clicked.connect(action)
            row.addWidget(button)
        preset_layout.addLayout(preset_row1)
        preset_layout.addLayout(preset_row2)
        preset_hint = QLabel("预设只保存参数；选择后请点击开始处理。")
        preset_hint.setWordWrap(True)
        preset_layout.addWidget(preset_hint)
        control_layout.addWidget(preset_group)
        self._refresh_presets()

        print_group = QGroupBox("打印与色块净化")
        form = QFormLayout(print_group)
        self.width_mm = self._double(60, 1, 300, 0.5)
        self.nozzle_mm = QComboBox()
        for diameter in (0.2, 0.4, 0.6):
            self.nozzle_mm.addItem(f"{diameter:.1f}", diameter)
        self.nozzle_mm.setCurrentIndex(1)
        self.min_feature_mm = self._double(0.45, 0, 5, 0.05)
        self.colors = QSpinBox(); self.colors.setRange(2, 128); self.colors.setValue(96)
        self.quantization_level = QComboBox()
        for name, amount in (("细致 · 128 色", 128), ("较细 · 96 色（推荐）", 96),
                             ("均衡 · 64 色", 64), ("较强 · 32 色", 32), ("最强 · 16 色", 16),
                             ("自定义", None)):
            self.quantization_level.addItem(name, amount)
        self.quantization_level.setCurrentIndex(self.quantization_level.findData(96))
        self.quantization_level.currentIndexChanged.connect(self._quantization_level_changed)
        self.colors.valueChanged.connect(self._sync_quantization_level)
        self.color_delta = self._double(2, 0, 40, 1)
        self.protect_flat = QCheckBox("保留纯色区域")
        self.protect_flat.setChecked(True)
        self.flat_min_area = QSpinBox(); self.flat_min_area.setRange(1, 1000000); self.flat_min_area.setValue(16)
        self.flat_min_area.setToolTip("纯色区域至少需要此像素面积，还需满足当前最小特征面积。")
        self.smooth_edges = QCheckBox("平滑色块边缘（减少锯齿）")
        self.smooth_edges.setChecked(True)
        self.smooth_edges.setToolTip("仅平滑相邻色块交界处约一像素；会增加少量过渡颜色，细节仍需检查切片。")
        self.smooth_strength = QSpinBox(); self.smooth_strength.setRange(0, 100); self.smooth_strength.setValue(100); self.smooth_strength.setSuffix("%")
        self.smooth_strength.setToolTip("0% 不平滑，100% 为完整轻度平滑；修改后需点击开始处理。")
        self.protect_colors = QCheckBox("保留明显不同的小色块")
        self.protect_colors.setChecked(True)
        self.protect_colors.setToolTip("避免小色块被强行替换成色差过大的邻色；保留的细节仍可能无法打印。")
        self.distinct_delta = self._double(12, 0, 100, 1)
        self.distinct_delta.setToolTip("小色块只会合并到色差不大于此值的邻色；数值越小，保留越严格。")
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
        form.addRow("颜色量化级别", self.quantization_level)
        form.addRow("颜色上限（不含保护色）", self.colors)
        form.addRow("相近色阈值", self.color_delta)
        form.addRow(self.protect_flat)
        form.addRow("纯色最小面积 (像素)", self.flat_min_area)
        form.addRow(self.smooth_edges)
        form.addRow("边缘平滑强度", self.smooth_strength)
        form.addRow(self.protect_colors)
        form.addRow("小色块最大合并色差", self.distinct_delta)
        form.addRow("处理模式", self.cleanup_toggle)
        form.addRow("输出分辨率", self.resolution)
        gentle_preset = QPushButton("温和预设（试用）")
        gentle_preset.setToolTip("启用净化和色彩保护，颜色上限 128，相近色阈值 1，最小特征宽度设为当前喷嘴直径；效果需通过预览和打印确认。")
        gentle_preset.clicked.connect(self._apply_gentle_preset)
        form.addRow(gentle_preset)
        control_layout.addWidget(print_group)

        height_group = QGroupBox("Lumina 浮雕高度图")
        height_form = QFormLayout(height_group)
        self.heightmap_enabled = QCheckBox("启用 AI 深度高度图")
        self.heightmap_direction = QComboBox()
        self.heightmap_direction.addItems(["近处凸起", "远处凸起"])
        self.heightmap_border = QComboBox()
        self.heightmap_border.addItems(["最低（黑色）", "最高（白色）"])
        self.heightmap_border.setToolTip("统一设置外边框和孔边框的高度，不受深度方向或强度影响；透明孔保持黑色。")
        self.heightmap_strength = QSpinBox(); self.heightmap_strength.setRange(0, 200); self.heightmap_strength.setValue(100); self.heightmap_strength.setSuffix("%")
        self.heightmap_strength.setToolTip("按比例调整相对深度：超过 100% 可能截断高值；最终毫米高度由 Lumina 设置。")
        self.heightmap_generate_button = QPushButton("生成 AI 深度图")
        self.heightmap_generate_button.clicked.connect(self.generate_depth)
        self.heightmap_preview_button = QPushButton("单独预览深度图")
        self.heightmap_preview_button.clicked.connect(self.show_depth_preview)
        self.heightmap_export_button = QPushButton("导出浮雕高度图 PNG")
        self.heightmap_export_button.clicked.connect(self.export_heightmap)
        self.heightmap_enabled.toggled.connect(self._heightmap_options_changed)
        self.heightmap_direction.currentIndexChanged.connect(self._heightmap_options_changed)
        self.heightmap_strength.valueChanged.connect(self._heightmap_options_changed)
        self.heightmap_border.currentIndexChanged.connect(self._heightmap_options_changed)
        height_form.addRow(self.heightmap_enabled)
        height_form.addRow("高度方向", self.heightmap_direction)
        height_form.addRow("高度图强度", self.heightmap_strength)
        height_form.addRow("边框高度", self.heightmap_border)
        height_form.addRow(self.heightmap_generate_button)
        height_form.addRow(self.heightmap_preview_button)
        height_form.addRow(self.heightmap_export_button)
        self.depth_status = QLabel("仅在本机估计相对深度，不上传照片。")
        self.depth_status.setWordWrap(True)
        height_form.addRow(self.depth_status)
        hint = QLabel("AI 根据原图估计物体前后关系；深度可能有误，并非实测距离。")
        hint.setWordWrap(True)
        height_form.addRow(hint)
        control_layout.addWidget(height_group)
        self._heightmap_options_changed()

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
        self.preview_kind = QComboBox()
        self.preview_kind.addItems(["彩色结果", "高度图"])
        self.preview_kind.currentIndexChanged.connect(self._refresh_output_canvas)
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
        zoom_row.addWidget(QLabel("预览"))
        zoom_row.addWidget(self.preview_kind)
        zoom_row.addWidget(QLabel("前后同步缩放"))
        self.output_zoom = QComboBox()
        for label, factor in (("适合窗口", 1), ("2 倍", 2), ("4 倍", 4)):
            self.output_zoom.addItem(label, factor)
        zoom_row.addWidget(self.output_zoom)
        zoom_row.addWidget(QLabel("放大后用滚动条查看"))
        preview_layout.addLayout(zoom_row)
        self.preview_shape_label = QLabel("尚无处理结果")
        self.preview_shape_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_shape_label)
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
            left.valueChanged.connect(lambda value, axis=axis: self._sync_scroll(axis, True, value))
            right.valueChanged.connect(lambda value, axis=axis: self._sync_scroll(axis, False, value))
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
        self.smooth_edges.toggled.connect(self._schedule_preview)
        self.smooth_edges.toggled.connect(self._update_cleanup_controls)
        self.protect_colors.toggled.connect(self._schedule_preview)
        self.protect_flat.toggled.connect(self._schedule_preview)
        self.protect_colors.toggled.connect(self._update_cleanup_controls)
        self.protect_flat.toggled.connect(self._update_cleanup_controls)
        self.crop_tool.currentIndexChanged.connect(
            lambda i: self.input_canvas.set_mode(["crop", "hole", "outline", "protect"][i])
        )
        for widget in (self.width_mm, self.min_feature_mm, self.colors, self.color_delta, self.flat_min_area, self.distinct_delta,
                       self.shape, self.corner_mm, self.hole_toggle, self.hole_mm, self.hole_compensation_mm, self.smooth_strength,
                       self.cleanup_toggle, self.resolution, self.outer_border_mm, self.hole_border_mm):
            signal = widget.valueChanged if isinstance(widget, (QDoubleSpinBox, QSpinBox)) else widget.currentIndexChanged
            signal.connect(self._schedule_preview)
        self.nozzle_mm.currentIndexChanged.connect(self._update_status)
        self.nozzle_mm.currentIndexChanged.connect(self._schedule_preview)
        self.resolution.currentIndexChanged.connect(self._update_status)

    def _update_cleanup_controls(self, *_):
        enabled = self.cleanup_toggle.currentIndex() == 0
        self.colors.setEnabled(enabled)
        self.quantization_level.setEnabled(enabled)
        self.color_delta.setEnabled(enabled)
        self.smooth_edges.setEnabled(enabled)
        self.smooth_strength.setEnabled(enabled and self.smooth_edges.isChecked())
        self.protect_colors.setEnabled(enabled)
        self.protect_flat.setEnabled(enabled)
        self.flat_min_area.setEnabled(enabled and self.protect_flat.isChecked())
        self.distinct_delta.setEnabled(enabled and self.protect_colors.isChecked())

    def _heightmap_options_changed(self, *_):
        enabled = self.heightmap_enabled.isChecked()
        if not enabled and self.depth_job is not None:
            self.depth_job.cancel()
        self.heightmap_direction.setEnabled(enabled)
        self.heightmap_strength.setEnabled(enabled)
        self.heightmap_border.setEnabled(enabled)
        ready = self.depth_raw is not None and self.depth_generation == self.generation
        self.heightmap_generate_button.setEnabled(enabled and self.depth_job is None)
        self.heightmap_preview_button.setEnabled(enabled and ready)
        self.heightmap_export_button.setEnabled(enabled and ready)
        if self.depth_view is not None and self.depth_dialog is not None and self.depth_dialog.isVisible() and ready:
            self.depth_view.set_image(self._current_depth_heightmap())
        if hasattr(self, "preview_kind") and self.preview_kind.currentIndex() == 1:
            self._refresh_output_canvas()
        if hasattr(self, "_history_current") and self.source is not None:
            self._record_history()

    def _quantization_level_changed(self, *_):
        amount = self.quantization_level.currentData()
        if amount is not None:
            self.colors.setValue(amount)

    def _sync_quantization_level(self, value):
        index = self.quantization_level.findData(value)
        if index < 0:
            index = self.quantization_level.findData(None)
        with QSignalBlocker(self.quantization_level):
            self.quantization_level.setCurrentIndex(index)

    def _set_output_zoom(self, *_):
        factor = self.output_zoom.currentData()
        pairs = ((self.input_scroll, self.input_canvas), (self.output_scroll, self.output_canvas))
        if factor == 1 or self.input_canvas.image is None or self.output_canvas.image is None or self.prepared is None:
            for scroll, canvas in pairs:
                canvas.display_scale = None
                canvas.setMinimumSize(280, 250)
                canvas.update()
        else:
            source = self.input_canvas.image
            viewport = self.input_scroll.viewport().size()
            source_scale = min(max(1, viewport.width() - 24) / source.width(),
                               max(1, viewport.height() - 24) / source.height()) * factor
            # Both views show the same processed-photo pixels at the same scale.
            result_scale = source_scale * source.width() / self.prepared.width * self.output.width / self.output_canvas.image.width()
            for (scroll, canvas), scale in zip(pairs, (source_scale, result_scale)):
                canvas.display_scale = scale
                canvas.setMinimumSize(max(280, math.ceil(canvas.image.width() * scale + 24)),
                                      max(250, math.ceil(canvas.image.height() * scale + 24)))
                canvas.update()
        QTimer.singleShot(0, self._align_preview_scroll)

    def _align_preview_scroll(self):
        for axis in ("horizontalScrollBar", "verticalScrollBar"):
            bar = getattr(self.input_scroll, axis)()
            self._sync_scroll(axis, True, bar.value())

    def _sync_scroll(self, axis, from_input, value):
        source_scroll = self.input_scroll if from_input else self.output_scroll
        target_scroll = self.output_scroll if from_input else self.input_scroll
        source_canvas = self.input_canvas if from_input else self.output_canvas
        target_canvas = self.output_canvas if from_input else self.input_canvas
        source_bar = getattr(source_scroll, axis)()
        target_bar = getattr(target_scroll, axis)()
        if self.prepared is None or self.output is None or self.preview_pendant is None:
            with QSignalBlocker(target_bar):
                target_bar.setValue(round(value / source_bar.maximum() * target_bar.maximum()) if source_bar.maximum() else 0)
            return
        horizontal = axis == "horizontalScrollBar"
        source_rect = source_canvas._image_rect()
        target_rect = target_canvas._image_rect()
        source_length = source_rect.width() if horizontal else source_rect.height()
        target_length = target_rect.width() if horizontal else target_rect.height()
        if source_length <= 0 or target_length <= 0:
            return
        viewport_length = source_scroll.viewport().width() if horizontal else source_scroll.viewport().height()
        source_start = source_rect.x() if horizontal else source_rect.y()
        fraction = (value + viewport_length / 2 - source_start) / source_length
        pw, ph = self.prepared.size
        x0, y0, x1, y1 = crop_pixel_box((pw, ph), self.preview_pendant.crop)
        cw, ch = x1 - x0, y1 - y0
        if self.preview_pendant.shape == "Circle":
            side = min(cw, ch)
            x0 += (cw - side) // 2
            y0 += (ch - side) // 2
            cw = ch = side
        content_size = cw if horizontal else ch
        full_size = pw if horizontal else ph
        crop_start = x0 if horizontal else y0
        result_size = self.output.width if horizontal else self.output.height
        border = (result_size - content_size) / 2
        if from_input:
            target_fraction = (fraction * full_size - crop_start + border) / result_size
        else:
            target_fraction = (fraction * result_size - border + crop_start) / full_size
        target_start = target_rect.x() if horizontal else target_rect.y()
        target_viewport = target_scroll.viewport().width() if horizontal else target_scroll.viewport().height()
        with QSignalBlocker(target_bar):
            target_bar.setValue(round(target_start + target_fraction * target_length - target_viewport / 2))

    def _refresh_presets(self, selected=None):
        try:
            self._presets = load_presets()
            self._preset_error = None
        except (OSError, ValueError) as exc:
            self._presets = {}
            self._preset_error = str(exc)
        with QSignalBlocker(self.preset_select):
            self.preset_select.clear()
            self.preset_select.addItem("选择预设…", None)
            for name in sorted(self._presets, key=str.casefold):
                self.preset_select.addItem(name, name)
            index = self.preset_select.findData(selected)
            self.preset_select.setCurrentIndex(max(0, index))
        self.preset_select.setToolTip("预设读取失败：" + self._preset_error if self._preset_error else "选择后应用参数，不会自动处理照片。")

    def _preset_settings(self):
        data = self._project_data()
        data.pop("source_path", None)
        return data

    def _preset_selected(self, *_):
        name = self.preset_select.currentData()
        if name is None:
            return
        try:
            presets = load_presets()
            if name not in presets:
                raise ValueError("该预设已不存在，请重新选择。")
            previous = self._project_data()
            self._apply_history({**previous, **presets[name]})
            if self.source is not None and previous != self._project_data():
                self.undo_stack.append(previous)
                self.undo_stack = self.undo_stack[-50:]
                self.redo_stack.clear()
            self._preset_pending_photo = self.source is None
            self.progress_label.setText("已应用预设，请点击开始处理")
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "无法应用预设", str(exc))
            self._refresh_presets()

    def _preset_name(self, title, current=""):
        name, ok = QInputDialog.getText(self, title, "预设名称（最多 80 个字符）：", text=current)
        name = name.strip()
        if not ok:
            return None
        if not name or len(name) > 80:
            QMessageBox.information(self, "预设名称无效", "请输入 1 到 80 个字符。")
            return None
        return name

    def _store_preset_change(self, operation):
        try:
            presets = load_presets()
            selected = operation(presets)
            if selected is None:
                return
            write_presets(presets)
            self._refresh_presets(selected)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "预设操作失败", str(exc))

    def save_new_preset(self):
        name = self._preset_name("保存新预设")
        if name is None:
            return
        def create(presets):
            if name in presets:
                raise ValueError("已有同名预设，请使用其他名称或更新所选预设。")
            presets[name] = self._preset_settings()
            return name
        self._store_preset_change(create)

    def update_preset(self):
        name = self.preset_select.currentData()
        if name is None:
            QMessageBox.information(self, "请选择预设", "请先选择需要更新的预设。")
            return
        def update(presets):
            if name not in presets:
                raise ValueError("该预设已不存在，请重新选择。")
            presets[name] = self._preset_settings()
            return name
        self._store_preset_change(update)

    def rename_preset(self):
        old = self.preset_select.currentData()
        if old is None:
            QMessageBox.information(self, "请选择预设", "请先选择需要重命名的预设。")
            return
        name = self._preset_name("重命名预设", old)
        if name is None or name == old:
            return
        def rename(presets):
            if old not in presets:
                raise ValueError("该预设已不存在，请重新选择。")
            if name in presets:
                raise ValueError("已有同名预设，请使用其他名称。")
            presets[name] = presets.pop(old)
            return name
        self._store_preset_change(rename)

    def delete_preset(self):
        name = self.preset_select.currentData()
        if name is None:
            QMessageBox.information(self, "请选择预设", "请先选择需要删除的预设。")
            return
        if QMessageBox.question(self, "删除预设", f"确定删除预设“{name}”？") != QMessageBox.StandardButton.Yes:
            return
        def delete(presets):
            if name not in presets:
                raise ValueError("该预设已不存在，请重新选择。")
            del presets[name]
            return ""
        self._store_preset_change(delete)

    def _apply_gentle_preset(self):
        widgets = (self.colors, self.color_delta, self.min_feature_mm, self.cleanup_toggle, self.protect_colors, self.protect_flat)
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.colors.setValue(128)
        self.color_delta.setValue(1)
        self.protect_colors.setChecked(True)
        self.protect_flat.setChecked(True)
        self.min_feature_mm.setValue(self.nozzle_mm.currentData())
        self.cleanup_toggle.setCurrentIndex(0)
        del blockers
        self._sync_quantization_level(self.colors.value())
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
        self._invalidate_depth()
        self.pending_start = False
        self.timer.stop()
        with QSignalBlocker(self.processing_toggle):
            self.processing_toggle.setChecked(False)
        self.processing_toggle.setText("开始处理")
        self._finish_progress("等待开始处理")
        self.generation += 1
        self.output_generation = -1
        self.prepared = self.output = self.preview_changed = None
        self.preview_quantized = self.preview_merged = None
        self.preview_pendant = None
        self.preview_shape_label.setText("尚无处理结果；设置外形和边框后点击开始处理。")
        self.output_canvas.set_image(None)
        self.source, self.source_path = image, Path(filename)
        self.last_color_export_path = None
        if not self._preset_pending_photo:
            self.crop, self.hole = (0.0, 0.0, 1.0, 1.0), (0.5, 0.12)
            self.polygon = []
            self.protected_regions = []
        self._preset_pending_photo = False
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._history_current = self._project_data()
        self.input_canvas.set_image(image)
        self.input_canvas.set_protected_regions(self.protected_regions)
        self._update_status()
        self._update_enabled()
        self._sync_geometry()
        self.status.setText("照片已打开。调整参数后点击“开始处理”。")
        return True

    def _processing_changed(self, checked):
        if checked:
            if self.source is None:
                with QSignalBlocker(self.processing_toggle):
                    self.processing_toggle.setChecked(False)
                return
            self.processing_toggle.setText("停止处理")
            self.pending_start = True
            self.timer.start(0)
        else:
            self.processing_toggle.setText("开始处理")
            self.timer.stop()
            self.pending_start = False
            self.generation += 1
            if self.active_job is not None:
                self.active_job.cancel()
            self.output_generation = -1
            self._finish_progress("处理已停止")
            self.status.setText("处理已停止；点击开始后重新生成预览。")

    def _update_progress_label(self):
        if self.progress_started is not None:
            elapsed = max(0, int(time.monotonic() - self.progress_started))
            self.progress_label.setText(f"{self.progress_stage} · 已用时 {elapsed} 秒")

    def _preview_progress(self, generation, stage):
        if generation == self.generation and self.processing_toggle.isChecked():
            self.progress_stage = stage
            self._update_progress_label()

    def _finish_progress(self, text, completed=False):
        elapsed = None if self.progress_started is None else max(0, int(time.monotonic() - self.progress_started))
        self.progress_timer.stop()
        self.progress_started = None
        self.progress_stage = ""
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if completed else 0)
        self.progress_label.setText(f"{text} · 用时 {elapsed} 秒" if completed and elapsed is not None else text)

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
        with QSignalBlocker(self.hole_x_percent), QSignalBlocker(self.hole_y_percent):
            self.hole_x_percent.setValue(self.hole[0] * 100)
            self.hole_y_percent.setValue(self.hole[1] * 100)
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
                                  self.color_delta.value(), self.protect_colors.isChecked(),
                                  self.protect_flat.isChecked(), self.flat_min_area.value(), self.distinct_delta.value(),
                                  self.smooth_edges.isChecked(), self.smooth_strength.value())
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
        if self._loading_project:
            return
        if self.source is not None:
            self._record_history()
            self._sync_geometry()
            self._update_status()
            self.generation += 1
            self._invalidate_depth()
            self.timer.stop()
            self.pending_start = False
            if self.active_job is not None:
                self.active_job.cancel()
            with QSignalBlocker(self.processing_toggle):
                self.processing_toggle.setChecked(False)
            self.processing_toggle.setText("开始处理")
            self._finish_progress("参数已更改，请点击开始处理")
            self.status.setText("参数已更改；当前预览是旧结果，请点击开始处理更新后再导出。")
            self._update_preview_shape_label()

    def _update_preview_shape_label(self):
        if self.preview_pendant is None or self.output is None:
            self.preview_shape_label.setText("尚无处理结果；设置外形和边框后点击开始处理。")
            return
        names = {"Rounded rectangle": "圆角矩形", "Rectangle": "矩形", "Circle": "圆形", "Polygon": "自定义多边形"}
        shape = names.get(self.preview_pendant.shape, self.preview_pendant.shape)
        border = "开启" if self.preview_pendant.outer_border_enabled else "关闭"
        if self.output_generation != self.generation:
            self.preview_shape_label.setText(f"旧预览：{shape}，外边框{border}；当前设置：{self.shape.currentText()}。请点击开始处理更新。")
        else:
            self.preview_shape_label.setText(f"当前结果：{shape}，外边框{border}。")

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
            self._finish_progress("等待完成多边形")
            self.pending_start = False
            with QSignalBlocker(self.processing_toggle):
                self.processing_toggle.setChecked(False)
            self.processing_toggle.setText("开始处理")
            return
        cleanup, pendant = self._settings()
        job = PreviewJob(self.generation, self.source, cleanup, pendant,
                         self.cleanup_toggle.currentIndex() == 0, self.protected_regions,
                         self.resolution.currentData())
        self.active_job = job
        self.pending_start = False
        job.signals.finished.connect(self._preview_ready)
        job.signals.failed.connect(self._preview_failed)
        job.signals.cancelled.connect(self._preview_cancelled)
        job.signals.progress.connect(self._preview_progress)
        self.preview_running = True
        self.progress_started = time.monotonic()
        self.progress_stage = "准备处理"
        self.progress_bar.setRange(0, 0)
        self._update_progress_label()
        self.progress_timer.start()
        self.pool.start(job)
        self.status.setText("正在生成预览…")

    def _preview_ready(self, generation, prepared, output, result):
        job = self.active_job
        self.preview_running = False
        self.active_job = None
        if generation != self.generation:
            if self.pending_start:
                self.timer.start(0)
            return
        self.preview_pendant = job.pendant if job else None
        with QSignalBlocker(self.processing_toggle):
            self.processing_toggle.setChecked(False)
        self.processing_toggle.setText("开始处理")
        self._finish_progress("处理完成", completed=True)
        self.prepared, self.output = prepared, output
        self.output_generation = generation
        self._update_preview_shape_label()
        self.preview_changed = result.changed_mask if result else None
        self.preview_quantized = result.quantized_mask if result else None
        self.preview_merged = result.merged_mask if result else None
        self._refresh_output_canvas()
        self._set_output_zoom()
        if result:
            total = result.visible_pixels or 1
            hole_note = " · 孔附近的轮廓或透明区域无法留出材料边距；当前不会裁孔" if self.hole_toggle.currentIndex() == 0 and not self.hole_safe else ""
            self.status.setText(
                f"预览就绪 · 调色 {result.original_colors} → {result.resulting_colors} 色 · "
                f"保留 {result.preserved_flat_colors} 种纯色 · "
                f"合并 {result.merged_islands} 个小色岛 · 最小特征约 {result.minimum_feature_px}px "
                f"· 色差保护保留 {result.protected_islands} 个小色岛（可能过小无法打印） "
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
            if self.pending_start:
                self.timer.start(0)
        else:
            with QSignalBlocker(self.processing_toggle):
                self.processing_toggle.setChecked(False)
            self.processing_toggle.setText("开始处理")
            self._finish_progress("处理失败")
            self.status.setText(f"预览失败：{message}")

    def _preview_cancelled(self, generation):
        self.preview_running = False
        self.active_job = None
        if self.source is not None and self.processing_toggle.isChecked() and self.pending_start:
            self.timer.start(0)

    def closeEvent(self, event):
        self.timer.stop()
        self.progress_timer.stop()
        self.pending_start = False
        self.generation += 1
        self._invalidate_depth()
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
        if self.preview_kind.currentText() == "高度图":
            preview = self._current_depth_heightmap() if self.depth_raw is not None and self.depth_generation == self.generation else None
            self.output_canvas.empty_message = "启用并生成 AI 深度图后可预览"
            self.output_canvas.set_image(preview)
            return
        self.output_canvas.empty_message = "打开照片，或将图片拖到这里"
        mask = (self.preview_changed, self.preview_quantized, self.preview_merged)[self.highlight_mode.currentIndex()]
        if self.highlight_toggle.isChecked() and mask is not None and self.prepared is not None and self.preview_pendant is not None:
            tint = Image.new("RGBA", self.prepared.size, (255, 74, 65, 0))
            changed_alpha = mask.point(lambda value: value * 105 // 255)
            changed_alpha = ImageChops.multiply(changed_alpha, self.prepared.getchannel("A"))
            tint.putalpha(changed_alpha)
            preview = make_pendant(Image.alpha_composite(self.prepared, tint), self.preview_pendant)
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
            self.smooth_edges.setChecked(data.get("smooth_edges", True))
            self.smooth_strength.setValue(data.get("edge_smoothing_strength", 100))
            self.protect_colors.setChecked(data.get("protect_distinct_colors", True))
            self.protect_flat.setChecked(data.get("preserve_flat_colors", data.get("protect_distinct_colors", True)))
            self.flat_min_area.setValue(data.get("flat_min_area_px", 16))
            self.distinct_delta.setValue(data.get("distinct_color_delta", 12))
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
            self.heightmap_enabled.setChecked(data.get("heightmap_enabled", False))
            self.heightmap_direction.setCurrentIndex(1 if data.get("heightmap_far_high", False) else 0)
            self.heightmap_strength.setValue(data.get("heightmap_strength_percent", 100))
            self.heightmap_border.setCurrentIndex(1 if data.get("heightmap_border_high", False) else 0)
            self._heightmap_options_changed()
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
            QMessageBox.information(self, "预览尚未更新", "参数已更改，请点击开始处理，完成后再导出。")
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
            self.last_color_export_path = Path(filename)
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

    def _invalidate_depth(self):
        if self.depth_job is not None:
            self.depth_job.cancel()
        self.depth_raw = None
        self.depth_border_mask = None
        self.depth_generation = -1
        if self.depth_view is not None:
            self.depth_view.set_image(None)
        if self.depth_dialog is not None:
            self.depth_dialog.hide()
        if hasattr(self, "heightmap_generate_button"):
            self.depth_status.setText("照片或配置已变化，请重新生成 AI 深度图。" if self.source is not None else "仅在本机估计相对深度，不上传照片。")
            self._heightmap_options_changed()

    def generate_depth(self):
        if not self.heightmap_enabled.isChecked() or self.depth_job is not None:
            return
        if (self.source is None or self.prepared is None or self.output is None or
                self.preview_pendant is None or self.output_generation != self.generation):
            QMessageBox.information(self, "请先处理照片", "请先点击开始处理并等待彩色结果完成，再生成 AI 深度图。")
            return
        job = DepthJob(self.generation, self.source, self.prepared.size, self.output, self.preview_pendant)
        self.depth_job = job
        job.signals.finished.connect(self._depth_ready)
        job.signals.failed.connect(self._depth_failed)
        job.signals.cancelled.connect(self._depth_cancelled)
        self.depth_status.setText("正在本机估计深度，可能需要数秒；照片不会上传或写入缓存。")
        self._heightmap_options_changed()
        self.pool.start(job)

    def _depth_ready(self, generation, depth):
        self.depth_job = None
        if generation != self.generation or self.output is None:
            self._heightmap_options_changed()
            return
        self.depth_raw, self.depth_border_mask = depth
        self.depth_generation = generation
        self.depth_status.setText("AI 相对深度已生成；请检查物体前后关系。")
        self._heightmap_options_changed()
        if self.heightmap_enabled.isChecked():
            self.show_depth_preview()

    def _depth_failed(self, generation, message):
        self.depth_job = None
        self._heightmap_options_changed()
        if generation == self.generation:
            self.depth_status.setText(f"深度估计失败：{message}")

    def _depth_cancelled(self, generation):
        self.depth_job = None
        self._heightmap_options_changed()

    def _current_depth_heightmap(self):
        return depth_heightmap(self.depth_raw, self.output, self.heightmap_direction.currentIndex() == 1,
                               self.heightmap_strength.value() / 100.0, self.depth_border_mask,
                               self.heightmap_border.currentIndex() == 1)

    def show_depth_preview(self):
        if self.depth_raw is None or self.depth_generation != self.generation:
            return
        if self.depth_dialog is None:
            self.depth_dialog = QDialog(self)
            self.depth_dialog.setWindowTitle("AI 深度高度图预览")
            self.depth_dialog.resize(800, 650)
            layout = QVBoxLayout(self.depth_dialog)
            text = QLabel("黑色低、白色高。相对深度由 AI 估计，请检查前后关系；实际高度在 Lumina 中设置。")
            text.setWordWrap(True)
            layout.addWidget(text)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            self.depth_view = ImageCanvas(interactive=False)
            scroll.setWidget(self.depth_view)
            layout.addWidget(scroll)
            export = QPushButton("导出深度高度图 PNG")
            export.clicked.connect(self.export_heightmap)
            layout.addWidget(export)
        self.depth_view.set_image(self._current_depth_heightmap())
        self.depth_dialog.show()
        self.depth_dialog.raise_()

    def export_heightmap(self):
        if not self.heightmap_enabled.isChecked():
            return
        if self.output is None or self.output_generation != self.generation:
            QMessageBox.information(self, "预览尚未更新", "请先点击开始处理，完成后再导出高度图。")
            return
        if self.depth_raw is None or self.depth_generation != self.generation:
            QMessageBox.information(self, "尚无深度图", "请先点击生成 AI 深度图。")
            return
        if self.hole_toggle.currentIndex() == 0 and not self.hole_safe:
            QMessageBox.information(self, "挂绳孔无法裁切", "请先调整孔位置或关闭挂绳孔，然后重新处理。")
            return
        default = (self.source_path.stem if self.source_path else "pendant") + "-heightmap.png"
        filename, _ = QFileDialog.getSaveFileName(self, "导出浮雕高度图", default, "PNG 图像 (*.png)")
        if not filename:
            return
        destination = self._png_path(filename)
        same_color = False
        if self.last_color_export_path is not None:
            try:
                same_color = (destination.resolve() == self.last_color_export_path.resolve() or
                              (destination.exists() and self.last_color_export_path.exists() and
                               destination.samefile(self.last_color_export_path)))
            except OSError:
                same_color = True
        if self._is_source_path(destination) or same_color:
            QMessageBox.critical(self, "无法导出", "高度图不能覆盖原照片或刚导出的彩色图片，请选择其他文件名。")
            return
        try:
            heightmap = self._current_depth_heightmap()
            filename = self.write_png(heightmap, filename)
            QMessageBox.information(self, "高度图已导出", f"已保存：\n{filename}\n\n"
                                    "在 Lumina 中先导入彩色 PNG 并完成普通预览，再启用浮雕、选择“根据高度图”并上传本文件。"
                                    "黑色低、白色高；最大高度在 Lumina 中设置。透明背景和孔在高度图中为黑色，轮廓依赖彩色图。")
        except (OSError, ValueError) as exc:
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
                options = {"dpi": image.info.get("dpi", (300, 300))}
                if image.mode in ("RGB", "RGBA"):
                    options["icc_profile"] = SRGB_PROFILE
                image.save(file, format="PNG", **options)
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
            "smooth_edges": self.smooth_edges.isChecked(),
            "edge_smoothing_strength": self.smooth_strength.value(),
            "protect_distinct_colors": self.protect_colors.isChecked(),
            "preserve_flat_colors": self.protect_flat.isChecked(),
            "flat_min_area_px": self.flat_min_area.value(),
            "distinct_color_delta": self.distinct_delta.value(),
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
            "heightmap_enabled": self.heightmap_enabled.isChecked(),
            "heightmap_far_high": self.heightmap_direction.currentIndex() == 1,
            "heightmap_strength_percent": self.heightmap_strength.value(),
            "heightmap_border_high": self.heightmap_border.currentIndex() == 1,
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
    smoke = len(sys.argv) == 4 and sys.argv[1] in ("--smoke-test", "--smoke-depth")
    smoke_depth = smoke and sys.argv[1] == "--smoke-depth"
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
        deadline = time.monotonic() + 110
        depth_started = [False]

        def finish_smoke():
            if window.output is not None and window.output_generation == window.generation:
                if smoke_depth:
                    if not depth_started[0]:
                        depth_started[0] = True
                        window.heightmap_enabled.setChecked(True)
                        window.generate_depth()
                    if window.depth_raw is None:
                        if time.monotonic() >= deadline or window.depth_status.text().startswith("深度估计失败"):
                            app.exit(5)
                        else:
                            QTimer.singleShot(100, finish_smoke)
                        return
                try:
                    window.write_png(window._current_depth_heightmap() if smoke_depth else window.output, destination)
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

