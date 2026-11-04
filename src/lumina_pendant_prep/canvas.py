from __future__ import annotations

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget


def pil_to_qimage(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    if max(rgba.size) > 2400:
        rgba = rgba.copy()
        rgba.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    data = rgba.tobytes("raw", "RGBA")
    return QImage(data, rgba.width, rgba.height, rgba.width * 4,
                  QImage.Format.Format_RGBA8888).copy()


class ImageCanvas(QWidget):
    cropChanged = Signal(object)
    holeMoved = Signal(float, float)
    polygonChanged = Signal(object)
    protectionAdded = Signal(object)
    outlineFinished = Signal()

    def __init__(self, interactive: bool = False, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 250)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.interactive = interactive
        self.mode = "crop"
        self.image: QImage | None = None
        self.display_scale: float | None = None
        self.crop = (0.0, 0.0, 1.0, 1.0)
        self.hole = (0.5, 0.12)
        self.hole_fraction = 0.07
        self.polygon: list[tuple[float, float]] = []
        self.protected_regions: list[tuple[float, float, float, float]] = []
        self.drawing_outline = False
        self.drag_start: QPointF | None = None
        self.drag_end: QPointF | None = None
        self.dragging_hole = False

    def set_image(self, image: Image.Image | None) -> None:
        self.image = pil_to_qimage(image) if image else None
        self.update()

    def set_geometry(self, crop: tuple[float, float, float, float], hole: tuple[float, float],
                     hole_fraction: float, polygon: list[tuple[float, float]] | None = None) -> None:
        self.crop, self.hole, self.hole_fraction = crop, hole, hole_fraction
        if polygon is not None:
            self.polygon = list(polygon)
        self.update()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode == "outline":
            self.drawing_outline = False
        self.setCursor(Qt.CursorShape.CrossCursor if mode in ("crop", "outline", "protect") else Qt.CursorShape.PointingHandCursor)

    def set_protected_regions(self, regions) -> None:
        self.protected_regions = list(regions)
        self.update()

    def _image_rect(self) -> QRectF:
        if self.image is None or self.image.width() == 0:
            return QRectF()
        scale = self.display_scale if self.display_scale is not None else min((self.width() - 24) / self.image.width(), (self.height() - 24) / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _point_to_image(self, point: QPointF) -> tuple[float, float] | None:
        rect = self._image_rect()
        if self.image is None or not rect.contains(point):
            return None
        return ((point.x() - rect.x()) / rect.width(), (point.y() - rect.y()) / rect.height())

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#20252b"))
        if self.image is None:
            painter.setPen(QColor("#abb4bf"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "打开照片，或将图片拖到这里")
            return
        rect = self._image_rect()
        cell = 12
        for y in range(int(rect.top()), int(rect.bottom()), cell):
            for x in range(int(rect.left()), int(rect.right()), cell):
                color = QColor("#e8e8e8") if ((x // cell + y // cell) % 2 == 0) else QColor("#c9c9c9")
                painter.fillRect(QRectF(x, y, cell, cell).intersected(rect), color)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, self.image)
        if not self.interactive:
            return
        x0, y0, x1, y1 = self.crop
        crop_rect = QRectF(rect.x() + x0 * rect.width(), rect.y() + y0 * rect.height(),
                           (x1 - x0) * rect.width(), (y1 - y0) * rect.height())
        painter.setPen(QPen(QColor("#34d399"), 2, Qt.PenStyle.DashLine))
        painter.fillRect(rect, QColor(0, 0, 0, 95))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(crop_rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.drawImage(rect, self.image)
        painter.setPen(QPen(QColor("#34d399"), 2, Qt.PenStyle.DashLine))
        painter.drawRect(crop_rect)
        painter.setPen(QPen(QColor("#f472b6"), 2, Qt.PenStyle.DashLine))
        painter.setBrush(QColor(244, 114, 182, 45))
        for left, top, right, bottom in self.protected_regions:
            painter.drawRect(QRectF(rect.x() + left * rect.width(), rect.y() + top * rect.height(),
                                    (right - left) * rect.width(), (bottom - top) * rect.height()))
        cx = rect.x() + self.hole[0] * rect.width()
        cy = rect.y() + self.hole[1] * rect.height()
        hole_r = max(3, self.hole_fraction * rect.width() / 2)
        painter.setPen(QPen(QColor("#ffcf5a"), 2))
        painter.setBrush(QColor(255, 207, 90, 100))
        painter.drawEllipse(QPointF(cx, cy), hole_r, hole_r)
        if self.polygon:
            vertices = [QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height()) for x, y in self.polygon]
            painter.setPen(QPen(QColor("#55d7ff"), 2))
            for first, second in zip(vertices, vertices[1:] + vertices[:1]):
                painter.drawLine(first, second)
            painter.setBrush(QColor("#55d7ff"))
            for point in vertices:
                painter.drawEllipse(point, 3.5, 3.5)
        if self.drag_start and self.drag_end:
            painter.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(self.drag_start, self.drag_end).normalized())

    def mousePressEvent(self, event):
        if not self.interactive or self.image is None:
            return
        if self.mode == "outline" and event.button() == Qt.MouseButton.RightButton:
            self.polygonChanged.emit(list(self.polygon))
            self.set_mode("crop")
            self.outlineFinished.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = self._point_to_image(event.position())
        if point is None:
            return
        if self.mode == "hole":
            self.dragging_hole = True
            self.holeMoved.emit(*point)
        elif self.mode == "outline":
            if not self.drawing_outline:
                self.polygon = []
                self.polygonChanged.emit([])
                self.drawing_outline = True
            self.polygon.append(point)
            self.update()
        else:
            self.drag_start = event.position()
            self.drag_end = self.drag_start
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging_hole:
            point = self._point_to_image(event.position())
            if point is not None:
                self.holeMoved.emit(*point)
            return
        if self.drag_start is not None:
            self.drag_end = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.dragging_hole:
            self.dragging_hole = False
            return
        if self.drag_start is None or self.drag_end is None:
            return
        rect = self._image_rect()
        p0 = self._point_to_image(self.drag_start)
        p1 = self._point_to_image(self.drag_end)
        self.drag_start = self.drag_end = None
        if p0 and p1 and abs(p1[0] - p0[0]) > 0.02 and abs(p1[1] - p0[1]) > 0.02:
            left, right = sorted((p0[0], p1[0]))
            top, bottom = sorted((p0[1], p1[1]))
            if self.mode == "protect":
                self.protectionAdded.emit((left, top, right, bottom))
            else:
                self.cropChanged.emit((left, top, right, bottom))
        self.update()

