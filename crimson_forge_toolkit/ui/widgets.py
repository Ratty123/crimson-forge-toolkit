from __future__ import annotations

from array import array
from dataclasses import dataclass, fields as dataclass_fields
import math
from pathlib import PurePosixPath
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QImage,
    QImageReader,
    QMatrix4x4,
    QOpenGLFunctions,
    QPainter,
    QPen,
    QPixmap,
    QVector3D,
    QVector4D,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QFrame,
    QWidget,
)

from crimson_forge_toolkit.models import (
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS,
    ModelPreviewData,
    ModelPreviewMesh,
    ModelPreviewRenderSettings,
    PreparedModelPreviewBatch,
    PreparedModelPreviewData,
    clamp_model_preview_render_settings,
)
from crimson_forge_toolkit.ui.themes import get_theme

_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_DEPTH_BUFFER_BIT = 0x00000100
_GL_DEPTH_TEST = 0x0B71
_GL_CULL_FACE = 0x0B44
_GL_FLOAT = 0x1406
_GL_FALSE = 0
_GL_TRIANGLES = 0x0004


@dataclass(slots=True)
class _ModelPreviewDrawBatch:
    first_vertex: int
    vertex_count: int
    texture_key: str = ""
    normal_texture_key: str = ""
    normal_texture_strength: float = 0.0
    material_texture_key: str = ""
    material_texture_type: str = ""
    material_texture_subtype: str = ""
    material_texture_packed_channels: Tuple[str, ...] = ()
    material_decode_mode: int = 0
    height_texture_key: str = ""
    support_maps_disabled: bool = False
    has_texture_coordinates: bool = False
    texture_wrap_repeat: bool = False
    texture_flip_vertical: bool = True


class NonIntrusiveWheelGuard(QObject):
    """Prevents accidental wheel changes on setting widgets while scrolling containers."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Wheel:
            return False
        if isinstance(watched, QComboBox):
            event.ignore()
            return True
        if isinstance(watched, QAbstractSpinBox):
            event.ignore()
            return True
        if isinstance(watched, QSlider):
            event.ignore()
            return True
        return False


_wheel_guard: Optional[NonIntrusiveWheelGuard] = None


def ensure_app_wheel_guard(app: Optional[QApplication]) -> None:
    global _wheel_guard
    if app is None or _wheel_guard is not None:
        return
    _wheel_guard = NonIntrusiveWheelGuard(app)
    app.installEventFilter(_wheel_guard)


def _rebalance_splitter_sizes(
    sizes: Sequence[int],
    minimums: Sequence[int],
    target_total: int,
    weights: Optional[Sequence[int]] = None,
) -> List[int]:
    count = min(len(sizes), len(minimums))
    if count <= 0:
        return []
    target_total = max(int(target_total), 1)
    safe_weights = [max(1, int(weights[index])) for index in range(count)] if weights else [1] * count
    normalized = [max(int(minimums[index]), int(sizes[index])) for index in range(count)]
    minimum_total = sum(int(minimums[index]) for index in range(count))
    if target_total <= minimum_total:
        return [max(1, int(minimums[index])) for index in range(count)]

    total = sum(normalized)
    if total < target_total:
        slack = target_total - total
        order = sorted(range(count), key=lambda index: (safe_weights[index], normalized[index]), reverse=True)
        cursor = 0
        while slack > 0:
            target_index = order[cursor % count]
            normalized[target_index] += 1
            slack -= 1
            cursor += 1
        return normalized

    excess = total - target_total
    if excess <= 0:
        return normalized

    while excess > 0:
        order = sorted(
            range(count),
            key=lambda index: (normalized[index] - int(minimums[index]), safe_weights[index], normalized[index]),
            reverse=True,
        )
        changed = False
        for target_index in order:
            available = normalized[target_index] - int(minimums[target_index])
            if available <= 0:
                continue
            reduction = min(available, max(1, excess // max(1, count)))
            normalized[target_index] -= reduction
            excess -= reduction
            changed = True
            if excess <= 0:
                break
        if not changed:
            break
    return normalized


def build_responsive_splitter_sizes(
    total_span: int,
    weights: Sequence[int],
    minimums: Sequence[int],
) -> List[int]:
    count = min(len(weights), len(minimums))
    if count <= 0:
        return []
    safe_weights = [max(1, int(weights[index])) for index in range(count)]
    safe_minimums = [max(1, int(minimums[index])) for index in range(count)]
    target_total = max(int(total_span), sum(safe_minimums), count)
    weight_total = max(sum(safe_weights), 1)
    sizes = [
        max(
            safe_minimums[index],
            int(round((target_total * safe_weights[index]) / weight_total)),
        )
        for index in range(count)
    ]
    return _rebalance_splitter_sizes(sizes, safe_minimums, target_total, safe_weights)


def clamp_splitter_sizes(
    total_span: int,
    sizes: Sequence[int],
    minimums: Sequence[int],
    *,
    fallback_weights: Optional[Sequence[int]] = None,
) -> List[int]:
    count = len(minimums)
    if count <= 0:
        return []
    safe_minimums = [max(1, int(value)) for value in minimums]
    target_total = max(int(total_span), sum(safe_minimums), count)
    if len(sizes) < count:
        return build_responsive_splitter_sizes(
            target_total,
            fallback_weights or [1] * count,
            safe_minimums,
        )
    candidate = []
    for index in range(count):
        try:
            value = int(sizes[index])
        except (TypeError, ValueError):
            return build_responsive_splitter_sizes(
                target_total,
                fallback_weights or [1] * count,
                safe_minimums,
            )
        if value <= 0:
            return build_responsive_splitter_sizes(
                target_total,
                fallback_weights or [1] * count,
                safe_minimums,
            )
        candidate.append(value)
    current_total = sum(candidate)
    if current_total <= 0:
        return build_responsive_splitter_sizes(
            target_total,
            fallback_weights or [1] * count,
            safe_minimums,
        )
    if current_total != target_total:
        scale = target_total / current_total
        candidate = [max(1, int(round(value * scale))) for value in candidate]
    return _rebalance_splitter_sizes(
        candidate,
        safe_minimums,
        target_total,
        fallback_weights or [1] * count,
    )


class FlatSectionPanel(QWidget):
    """Simple titled panel without QGroupBox title-over-border rendering."""

    def __init__(self, title: str, *, body_margins: Tuple[int, int, int, int] = (10, 10, 10, 10), body_spacing: int = 8):
        super().__init__()
        self.setObjectName("FlatSectionPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 4, 0, 0)
        outer_layout.setSpacing(2)

        self.header_widget = QWidget()
        self.header_widget.setObjectName("FlatSectionHeader")
        header_layout = QHBoxLayout(self.header_widget)
        header_layout.setContentsMargins(14, 0, 0, 0)
        header_layout.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("FlatSectionTitle")
        self.title_label.setWordWrap(True)
        header_layout.addWidget(self.title_label, alignment=Qt.AlignLeft | Qt.AlignTop)
        header_layout.addStretch(1)
        outer_layout.addWidget(self.header_widget)

        self.body_frame = QFrame()
        self.body_frame.setObjectName("FlatSectionBody")
        self.body_layout = QVBoxLayout(self.body_frame)
        self.body_layout.setContentsMargins(*body_margins)
        self.body_layout.setSpacing(body_spacing)
        outer_layout.addWidget(self.body_frame, stretch=1)


class PreviewLabel(QLabel):
    color_sampled = Signal(str)

    def __init__(self, title: str):
        super().__init__(title)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(280, 220)
        self.setWordWrap(True)
        self.setObjectName("PreviewLabel")
        self._source_pixmap: Optional[QPixmap] = None
        self._source_image: Optional[QImage] = None
        self._source_image_path: str = ""
        self._source_image_size = QSize()
        self._source_image_loaded_size = QSize()
        self._source_image_load_failed = False
        self._source_revision = 0
        self._scaled_pixmap_cache: Dict[Tuple[int, int, int, int], QPixmap] = {}
        self._current_render_key: Optional[Tuple[int, int, int, int]] = None
        self._current_render_size = QSize()
        self._fallback_text = title
        self._pending_render_text = title
        self._zoom_factor = 1.0
        self._fit_to_view = True
        self._fit_scale = 1.0
        self._scroll_area = None
        self._wheel_zoom_handler: Optional[Callable[[int], None]] = None
        self._color_pick_enabled = False
        self._drag_active = False
        self._drag_start_global_pos = None
        self._drag_start_h = 0
        self._drag_start_v = 0
        self._interactive_scale_timer = QTimer(self)
        self._interactive_scale_timer.setSingleShot(True)
        self._interactive_scale_timer.setInterval(20)
        self._interactive_scale_timer.timeout.connect(self._flush_interactive_scale)
        self._idle_scale_timer = QTimer(self)
        self._idle_scale_timer.setSingleShot(True)
        self._idle_scale_timer.setInterval(140)
        self._idle_scale_timer.timeout.connect(self._flush_idle_scale)

    def clear_preview(self, message: str) -> None:
        self._interactive_scale_timer.stop()
        self._idle_scale_timer.stop()
        self._source_pixmap = None
        self._source_image = None
        self._source_image_path = ""
        self._source_image_size = QSize()
        self._source_image_loaded_size = QSize()
        self._source_image_load_failed = False
        self._source_revision += 1
        self._scaled_pixmap_cache.clear()
        self._current_render_key = None
        self._current_render_size = QSize()
        self._fallback_text = message
        self._pending_render_text = message
        self._drag_active = False
        self.setPixmap(QPixmap())
        self.setText(message)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(280, 220)
        self.setMaximumSize(16777215, 16777215)
        self.unsetCursor()

    def attach_scroll_area(self, scroll_area) -> None:
        self._scroll_area = scroll_area
        scroll_area.resized.connect(self._handle_viewport_resize)

    def set_wheel_zoom_handler(self, handler: Optional[Callable[[int], None]]) -> None:
        self._wheel_zoom_handler = handler

    def set_color_pick_enabled(self, enabled: bool) -> None:
        self._color_pick_enabled = enabled
        self._update_cursor()

    def set_zoom_factor(self, zoom_factor: float) -> None:
        self._zoom_factor = max(0.1, zoom_factor)
        if self._has_source_image():
            self._interactive_scale_timer.stop()
            self._idle_scale_timer.stop()
            self._apply_scaled_pixmap(self._fallback_text)

    def set_fit_to_view(self, fit_to_view: bool) -> None:
        self._fit_to_view = fit_to_view
        if self._has_source_image():
            self._interactive_scale_timer.stop()
            self._idle_scale_timer.stop()
            self._apply_scaled_pixmap(self._fallback_text)

    def set_fit_scale(self, fit_scale: float) -> None:
        self._fit_scale = max(0.5, min(4.0, fit_scale))
        if self._has_source_image() and self._fit_to_view:
            self._interactive_scale_timer.stop()
            self._idle_scale_timer.stop()
            self._apply_scaled_pixmap(self._fallback_text)

    def set_preview_pixmap(self, pixmap: QPixmap, fallback_text: str) -> None:
        self._interactive_scale_timer.stop()
        self._idle_scale_timer.stop()
        self._source_pixmap = pixmap
        self._source_image = None
        self._source_image_path = ""
        self._source_image_size = pixmap.size()
        self._source_image_loaded_size = pixmap.size()
        self._source_image_load_failed = False
        self._source_revision += 1
        self._scaled_pixmap_cache.clear()
        self._current_render_key = None
        self._current_render_size = QSize()
        self._fallback_text = fallback_text
        self._pending_render_text = fallback_text
        self._apply_scaled_pixmap(fallback_text)

    def set_preview_image(self, image: QImage, fallback_text: str) -> None:
        self._interactive_scale_timer.stop()
        self._idle_scale_timer.stop()
        self._source_pixmap = None
        self._source_image = image
        self._source_image_path = ""
        self._source_image_size = image.size() if not image.isNull() else QSize()
        self._source_image_loaded_size = self._source_image_size
        self._source_image_load_failed = False
        self._source_revision += 1
        self._scaled_pixmap_cache.clear()
        self._current_render_key = None
        self._current_render_size = QSize()
        self._fallback_text = fallback_text
        self._pending_render_text = fallback_text
        self._apply_scaled_pixmap(fallback_text)

    def set_preview_image_path(self, image_path: str, fallback_text: str) -> None:
        self._interactive_scale_timer.stop()
        self._idle_scale_timer.stop()
        self._source_pixmap = None
        self._source_image = None
        self._source_image_path = image_path
        self._source_image_load_failed = False
        reader = QImageReader(image_path)
        size = reader.size()
        self._source_image_size = size if size.isValid() else QSize()
        self._source_image_loaded_size = QSize()
        self._source_revision += 1
        self._scaled_pixmap_cache.clear()
        self._current_render_key = None
        self._current_render_size = QSize()
        self._fallback_text = fallback_text
        self._pending_render_text = fallback_text
        self._apply_scaled_pixmap(fallback_text)

    def current_display_scale(self) -> float:
        source_width = 0
        if self._source_pixmap is not None and not self._source_pixmap.isNull():
            source_width = self._source_pixmap.width()
        elif self._source_image_size.isValid():
            source_width = self._source_image_size.width()
        if source_width <= 0:
            return 1.0
        return max(0.1, self.width() / float(source_width))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._has_source_image() and self._fit_to_view and self._scroll_area is None:
            self._schedule_fit_rescale()

    def _handle_viewport_resize(self) -> None:
        if self._has_source_image() and self._fit_to_view:
            self._schedule_fit_rescale()

    def _schedule_fit_rescale(self) -> None:
        self._pending_render_text = self._fallback_text
        self._interactive_scale_timer.start()
        self._idle_scale_timer.start()

    def _flush_interactive_scale(self) -> None:
        if self._has_source_image():
            self._apply_scaled_pixmap(self._pending_render_text, transformation_mode=Qt.FastTransformation)

    def _flush_idle_scale(self) -> None:
        if self._has_source_image():
            self._apply_scaled_pixmap(self._pending_render_text, transformation_mode=Qt.SmoothTransformation)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._color_pick_enabled:
            current_pixmap = self.pixmap()
            point = event.position().toPoint()
            if current_pixmap is not None and not current_pixmap.isNull():
                if 0 <= point.x() < current_pixmap.width() and 0 <= point.y() < current_pixmap.height():
                    color = current_pixmap.toImage().pixelColor(point)
                    self.color_sampled.emit(color.name().upper())
                    event.accept()
                    return
        if (
            event.button() == Qt.LeftButton
            and self._can_pan()
            and self._scroll_area is not None
        ):
            self._drag_active = True
            self._drag_start_global_pos = event.globalPosition().toPoint()
            self._drag_start_h = self._scroll_area.horizontalScrollBar().value()
            self._drag_start_v = self._scroll_area.verticalScrollBar().value()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_active and self._scroll_area is not None and self._drag_start_global_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_start_global_pos
            self._scroll_area.horizontalScrollBar().setValue(self._drag_start_h - delta.x())
            self._scroll_area.verticalScrollBar().setValue(self._drag_start_v - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_active and event.button() == Qt.LeftButton:
            self._drag_active = False
            self._drag_start_global_pos = None
            self._update_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta_y = event.angleDelta().y()
        if (
            self._wheel_zoom_handler is not None
            and self._has_source_image()
            and delta_y != 0
        ):
            step = 1 if delta_y > 0 else -1
            self._wheel_zoom_handler(step)
            event.accept()
            return
        super().wheelEvent(event)

    def _can_pan(self) -> bool:
        if not self._has_source_image() or self._scroll_area is None:
            return False
        viewport = self._scroll_area.viewport().size()
        return self.width() > viewport.width() or self.height() > viewport.height()

    def _has_source_image(self) -> bool:
        return (
            self._source_pixmap is not None and not self._source_pixmap.isNull()
        ) or (self._source_image is not None and not self._source_image.isNull()) or (
            bool(self._source_image_path) and not self._source_image_load_failed
        )

    def _update_cursor(self) -> None:
        if self._color_pick_enabled:
            self.setCursor(Qt.CrossCursor)
        elif self._drag_active:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._can_pan():
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()

    def _apply_scaled_pixmap(self, fallback_text: str, *, transformation_mode=Qt.SmoothTransformation) -> None:
        self._fallback_text = fallback_text
        has_source_pixmap = self._source_pixmap is not None and not self._source_pixmap.isNull()
        has_source_image = self._source_image is not None and not self._source_image.isNull()
        has_source_path = bool(self._source_image_path) and not self._source_image_load_failed
        if not has_source_pixmap and not has_source_image and not has_source_path:
            self.setPixmap(QPixmap())
            self.setText(fallback_text)
            self._update_cursor()
            return

        if self._fit_to_view and self._scroll_area is not None:
            viewport = self._scroll_area.maximumViewportSize()
            if not viewport.isValid() or viewport.isEmpty():
                viewport = self._scroll_area.viewport().size()
            width = max(1, int(round((viewport.width() - 6) * self._fit_scale)))
            height = max(1, int(round((viewport.height() - 6) * self._fit_scale)))
        else:
            if has_source_pixmap:
                source_size = self._source_pixmap.size()
            elif self._source_image is not None and not self._source_image.isNull():
                source_size = self._source_image.size()
            else:
                source_size = self._source_image_size
            width = max(1, int(round(source_size.width() * self._zoom_factor)))
            height = max(1, int(round(source_size.height() * self._zoom_factor)))

        transform_key = 0 if transformation_mode == Qt.FastTransformation else 1
        cache_key = (self._source_revision, width, height, transform_key)
        if self._current_render_key == cache_key:
            current_pixmap = self.pixmap()
            if current_pixmap is not None and not current_pixmap.isNull() and current_pixmap.size() == self._current_render_size:
                self._update_cursor()
                return
        cached = self._scaled_pixmap_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            scaled = cached
        elif has_source_pixmap:
            scaled = self._source_pixmap.scaled(
                width,
                height,
                Qt.KeepAspectRatio,
                transformation_mode,
            )
            self._cache_scaled_pixmap(cache_key, scaled)
        else:
            if not has_source_image:
                if not self._load_source_image_for_render(width, height):
                    self.setPixmap(QPixmap())
                    self.setText(fallback_text)
                    self._update_cursor()
                    return
            target_size = self._source_image.size().scaled(width, height, Qt.KeepAspectRatio)
            if not target_size.isValid():
                self.setPixmap(QPixmap())
                self.setText(fallback_text)
                self._update_cursor()
                return
            scaled_image = self._source_image.scaled(
                target_size,
                Qt.KeepAspectRatio,
                transformation_mode,
            )
            scaled = QPixmap.fromImage(scaled_image)
            self._cache_scaled_pixmap(cache_key, scaled)

        self.setText("")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(0, 0)
        self.resize(scaled.size())
        self.setFixedSize(scaled.size())
        self.setPixmap(scaled)
        self._current_render_key = cache_key
        self._current_render_size = scaled.size()
        self._update_cursor()

    def _cache_scaled_pixmap(self, cache_key: Tuple[int, int, int, int], pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self._scaled_pixmap_cache[cache_key] = pixmap
        if len(self._scaled_pixmap_cache) > 12:
            oldest_key = next(iter(self._scaled_pixmap_cache))
            self._scaled_pixmap_cache.pop(oldest_key, None)

    def _load_source_image_for_render(self, target_width: int, target_height: int) -> bool:
        if self._source_image_load_failed or not self._source_image_path:
            return False
        requested_size = QSize(max(1, target_width), max(1, target_height))
        reader = QImageReader(self._source_image_path)
        reader.setAutoTransform(True)
        if not self._source_image_size.isValid():
            size = reader.size()
            if size.isValid():
                self._source_image_size = size
        source_size = self._source_image_size if self._source_image_size.isValid() else reader.size()
        decode_target_size = (
            source_size.scaled(requested_size, Qt.KeepAspectRatio)
            if source_size.isValid()
            else requested_size
        )
        if self._source_image is not None and not self._source_image.isNull():
            loaded_size = self._source_image.size()
            if loaded_size.isValid() and (
                loaded_size.width() >= decode_target_size.width()
                and loaded_size.height() >= decode_target_size.height()
            ):
                self._source_image_loaded_size = loaded_size
                return True
        use_scaled_decode = (
            source_size.isValid()
            and source_size.width() > decode_target_size.width() * 2
            and source_size.height() > decode_target_size.height() * 2
        )
        if use_scaled_decode:
            reader.setScaledSize(decode_target_size)
        image = reader.read()
        if image.isNull() and use_scaled_decode:
            reader = QImageReader(self._source_image_path)
            reader.setAutoTransform(True)
            image = reader.read()
        if image.isNull():
            self._source_image_load_failed = True
            self._source_image = None
            self._source_image_loaded_size = QSize()
            return False
        self._source_image = image
        self._source_image_loaded_size = image.size()
        if not self._source_image_size.isValid():
            self._source_image_size = image.size()
        return True


class ModelPreviewWidget(QOpenGLWidget):
    view_state_changed = Signal(float, bool)
    debug_details_changed = Signal(str)

    _DEFAULT_YAW = -35.0
    _DEFAULT_PITCH = 20.0
    _FIT_DISTANCE = 3.25
    _VERTICAL_FOV_DEGREES = 45.0
    _ZOOM_STEPS = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0)
    _PALETTE = (
        (201 / 255.0, 111 / 255.0, 81 / 255.0),
        (94 / 255.0, 133 / 255.0, 168 / 255.0),
        (156 / 255.0, 167 / 255.0, 98 / 255.0),
        (198 / 255.0, 176 / 255.0, 92 / 255.0),
        (147 / 255.0, 112 / 255.0, 166 / 255.0),
    )
    _MATERIAL_DECODE_GENERIC = 0
    _MATERIAL_DECODE_SPECULAR = 1
    _MATERIAL_DECODE_AO = 2
    _MATERIAL_DECODE_ROUGHNESS = 3
    _MATERIAL_DECODE_METALLIC = 4
    _MATERIAL_DECODE_MATERIAL_MASK = 5
    _MATERIAL_DECODE_MATERIAL_RESPONSE = 6
    _MATERIAL_DECODE_PACKED_MASK = 7
    _MATERIAL_DECODE_ORM = 8
    _MATERIAL_DECODE_RMA = 9
    _MATERIAL_DECODE_MRA = 10
    _MATERIAL_DECODE_ARM = 11
    _MATERIAL_DECODE_OPACITY_MASK = 12

    def __init__(self, title: str, *, theme_key: str):
        super().__init__()
        self.setMinimumSize(280, 220)
        self.setMouseTracking(True)
        self._message = title
        self._theme_key = theme_key
        self._background_color = QColor(get_theme(theme_key)["preview_bg"])
        self._overlay_text_color = QColor(get_theme(theme_key)["text_muted"])
        self._debug_overlay_lines: Tuple[str, ...] = ()
        self._debug_detail_lines: Tuple[str, ...] = ()
        self._model_summary = ""
        self._vertex_blob = b""
        self._vertex_count = 0
        self._gl_ready = False
        self._program: Optional[QOpenGLShaderProgram] = None
        self._functions: Optional[QOpenGLFunctions] = None
        self._vertex_buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        self._vertex_array = QOpenGLVertexArrayObject(self)
        self._mvp_uniform_location = -1
        self._model_uniform_location = -1
        self._camera_uniform_location = -1
        self._light_uniform_location = -1
        self._ambient_uniform_location = -1
        self._texture_sampler_uniform_location = -1
        self._normal_texture_sampler_uniform_location = -1
        self._material_texture_sampler_uniform_location = -1
        self._height_texture_sampler_uniform_location = -1
        self._use_texture_uniform_location = -1
        self._use_high_quality_uniform_location = -1
        self._use_normal_texture_uniform_location = -1
        self._normal_texture_strength_uniform_location = -1
        self._use_material_texture_uniform_location = -1
        self._material_decode_mode_uniform_location = -1
        self._use_height_texture_uniform_location = -1
        self._diffuse_wrap_bias_uniform_location = -1
        self._diffuse_light_scale_uniform_location = -1
        self._normal_strength_cap_uniform_location = -1
        self._normal_strength_floor_uniform_location = -1
        self._height_effect_max_uniform_location = -1
        self._cavity_clamp_min_uniform_location = -1
        self._cavity_clamp_max_uniform_location = -1
        self._specular_base_uniform_location = -1
        self._specular_min_uniform_location = -1
        self._specular_max_uniform_location = -1
        self._shininess_base_uniform_location = -1
        self._shininess_min_uniform_location = -1
        self._shininess_max_uniform_location = -1
        self._height_shininess_boost_uniform_location = -1
        self._fit_to_view = True
        self._zoom_factor = 1.0
        self._distance = self._FIT_DISTANCE
        self._yaw = self._DEFAULT_YAW
        self._pitch = self._DEFAULT_PITCH
        self._drag_active = False
        self._pan_drag_active = False
        self._pan_drag_button = Qt.NoButton
        self._last_mouse_pos = QPointF()
        self._last_global_mouse_pos = QPoint()
        self._pan_offset = QVector3D(0.0, 0.0, 0.0)
        self._current_model = None
        self._mesh_batches: List[_ModelPreviewDrawBatch] = []
        self._texture_objects: Dict[Tuple[str, bool, bool], QOpenGLTexture] = {}
        self._use_textures = False
        self._high_quality_textures = True
        self._show_grid_overlay = False
        self._show_origin_overlay = False
        self._render_settings = clamp_model_preview_render_settings()
        self._pan_poll_timer = QTimer(self)
        self._pan_poll_timer.setInterval(16)
        self._pan_poll_timer.timeout.connect(self._poll_pan_drag)

    def set_theme(self, theme_key: str) -> None:
        self._theme_key = theme_key
        theme = get_theme(theme_key)
        self._background_color = QColor(theme["preview_bg"])
        self._overlay_text_color = QColor(theme["text_muted"])
        self.update()

    def set_alignment_guides_visible(self, visible: bool) -> None:
        self._show_grid_overlay = bool(visible)
        self._show_origin_overlay = bool(visible)
        self.update()

    def view_state_snapshot(self) -> Tuple[float, float, bool, float, float, Tuple[float, float, float]]:
        return (
            float(self._yaw),
            float(self._pitch),
            bool(self._fit_to_view),
            float(self._zoom_factor),
            float(self._distance),
            (float(self._pan_offset.x()), float(self._pan_offset.y()), float(self._pan_offset.z())),
        )

    def restore_view_state(
        self,
        state: Optional[Tuple[float, float, bool, float, float, Tuple[float, float, float]]],
    ) -> None:
        if not state:
            return
        try:
            yaw, pitch, fit_to_view, zoom_factor, distance, pan_offset = state
            self._yaw = float(yaw)
            self._pitch = float(pitch)
            self._fit_to_view = bool(fit_to_view)
            self._zoom_factor = min(max(float(zoom_factor), 0.1), 16.0)
            self._distance = max(0.1, float(distance))
            self._pan_offset = QVector3D(float(pan_offset[0]), float(pan_offset[1]), float(pan_offset[2]))
        except Exception:
            return
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()

    def _reset_model_state(self, message: str) -> bool:
        had_renderable_state = bool(
            self._current_model is not None
            or self._vertex_count > 0
            or self._mesh_batches
            or self._texture_objects
        )
        self._message = message
        self._debug_overlay_lines = ()
        self._debug_detail_lines = ()
        self.debug_details_changed.emit("")
        self._model_summary = ""
        self._vertex_blob = b""
        self._vertex_count = 0
        self._current_model = None
        self._mesh_batches = []
        self._drag_active = False
        self._pan_drag_active = False
        self._pan_drag_button = Qt.NoButton
        self._pan_poll_timer.stop()
        self._pan_offset = QVector3D(0.0, 0.0, 0.0)
        self.unsetCursor()
        return had_renderable_state

    def clear_model(self, message: str, *, release_gl: bool = False) -> None:
        had_renderable_state = self._reset_model_state(message)
        if release_gl and had_renderable_state and self._gl_ready and self.context() is not None:
            self._upload_geometry()
        self.update()

    def set_model(self, model) -> None:
        cloned_model, prepared_preview = self.prepare_model_preview(model)
        self.set_prepared_model(cloned_model, prepared_preview)

    def set_prepared_model(
        self,
        model,
        prepared_preview: Optional[PreparedModelPreviewData],
    ) -> None:
        cloned_model = self._clone_model_preview(model)
        self._initialize_preview_slot_defaults(cloned_model)
        if isinstance(prepared_preview, PreparedModelPreviewData):
            vertex_blob = b"".join(batch.vertex_blob for batch in prepared_preview.batches)
            vertex_count = sum(int(batch.index_count) for batch in prepared_preview.batches)
            mesh_batches: List[_ModelPreviewDrawBatch] = []
            first_vertex = 0
            for batch in prepared_preview.batches:
                material_texture_channels = tuple(batch.preview_material_texture_packed_channels or ())
                mesh_batches.append(
                    _ModelPreviewDrawBatch(
                        first_vertex=first_vertex,
                        vertex_count=int(batch.index_count),
                        texture_key=batch.preview_texture_path,
                        normal_texture_key=batch.preview_normal_texture_path,
                        normal_texture_strength=float(batch.preview_normal_texture_strength or 0.0),
                        material_texture_key=batch.preview_material_texture_path,
                        material_texture_type=batch.preview_material_texture_type,
                        material_texture_subtype=batch.preview_material_texture_subtype,
                        material_texture_packed_channels=material_texture_channels,
                        material_decode_mode=self._material_decode_mode_for_semantics(
                            batch.preview_material_texture_type,
                            batch.preview_material_texture_subtype,
                            material_texture_channels,
                        ),
                        height_texture_key=batch.preview_height_texture_path,
                        support_maps_disabled=bool(batch.preview_debug_disable_support_maps),
                        has_texture_coordinates=bool(batch.has_texture_coordinates),
                        texture_wrap_repeat=bool(batch.texture_wrap_repeat),
                        texture_flip_vertical=(
                            True
                            if batch.preview_texture_flip_vertical is None
                            else bool(batch.preview_texture_flip_vertical)
                        ),
                    )
                )
                first_vertex += int(batch.index_count)
        else:
            vertex_blob, vertex_count, mesh_batches = self._build_vertex_blob(cloned_model)
        self._current_model = cloned_model
        self._model_summary = getattr(cloned_model, "summary", "") or ""
        self._message = self._model_summary or "Model preview ready."
        self._vertex_blob = vertex_blob
        self._vertex_count = vertex_count
        self._mesh_batches = mesh_batches
        self._yaw = self._DEFAULT_YAW
        self._pitch = self._DEFAULT_PITCH
        self._fit_to_view = True
        self._zoom_factor = 1.0
        self._distance = self._FIT_DISTANCE
        self._pan_drag_button = Qt.NoButton
        self._pan_poll_timer.stop()
        self._pan_offset = QVector3D(0.0, 0.0, 0.0)
        self._refresh_debug_overlay_lines()
        self._upload_geometry()
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()

    @classmethod
    def prepare_model_preview(
        cls,
        model,
    ) -> Tuple[object, Optional[PreparedModelPreviewData]]:
        cloned_model = cls._clone_model_preview(model)
        if not isinstance(cloned_model, ModelPreviewData):
            return cloned_model, None
        for mesh in getattr(cloned_model, "meshes", None) or []:
            if isinstance(mesh, ModelPreviewMesh):
                cls._initialize_mesh_preview_slot_defaults(mesh)
        vertex_blob, vertex_count, mesh_batches = cls._build_vertex_blob(cloned_model)
        prepared_batches: List[PreparedModelPreviewBatch] = []
        floats_per_vertex = 17
        bytes_per_vertex = floats_per_vertex * 4
        for mesh, batch in zip(getattr(cloned_model, "meshes", ()) or (), mesh_batches):
            start = int(batch.first_vertex) * bytes_per_vertex
            end = start + (int(batch.vertex_count) * bytes_per_vertex)
            prepared_batches.append(
                PreparedModelPreviewBatch(
                    material_name=str(getattr(mesh, "material_name", "") or "").strip(),
                    texture_name=str(getattr(mesh, "texture_name", "") or "").strip(),
                    vertex_blob=vertex_blob[start:end],
                    index_count=int(batch.vertex_count),
                    preview_texture_path=batch.texture_key,
                    preview_normal_texture_path=batch.normal_texture_key,
                    preview_material_texture_path=batch.material_texture_key,
                    preview_height_texture_path=batch.height_texture_key,
                    preview_texture_flip_vertical=batch.texture_flip_vertical,
                    preview_normal_texture_strength=float(batch.normal_texture_strength or 0.0),
                    preview_material_texture_type=batch.material_texture_type,
                    preview_material_texture_subtype=batch.material_texture_subtype,
                    preview_material_texture_packed_channels=tuple(batch.material_texture_packed_channels or ()),
                    has_texture_coordinates=bool(batch.has_texture_coordinates),
                    texture_wrap_repeat=bool(batch.texture_wrap_repeat),
                    preview_debug_flip_base_v=False,
                    preview_debug_disable_support_maps=bool(batch.support_maps_disabled),
                )
            )
        return cloned_model, PreparedModelPreviewData(
            source_path=str(getattr(cloned_model, "path", "") or "").strip(),
            format=str(getattr(cloned_model, "format", "") or "").strip(),
            summary=str(getattr(cloned_model, "summary", "") or "").strip(),
            mesh_count=int(getattr(cloned_model, "mesh_count", 0) or 0),
            vertex_count=int(getattr(cloned_model, "vertex_count", vertex_count) or vertex_count),
            face_count=int(getattr(cloned_model, "face_count", 0) or 0),
            lod_index=int(getattr(cloned_model, "lod_index", -1) or -1),
            lod_count=int(getattr(cloned_model, "lod_count", 0) or 0),
            normalization_center=tuple(getattr(cloned_model, "normalization_center", (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0)),
            normalization_scale=float(getattr(cloned_model, "normalization_scale", 1.0) or 1.0),
            batches=tuple(prepared_batches),
        )

    @staticmethod
    def _clone_model_preview(model) -> Optional[ModelPreviewData]:
        if not isinstance(model, ModelPreviewData):
            return model
        cloned_meshes = []
        for mesh in getattr(model, "meshes", []) or []:
            if isinstance(mesh, ModelPreviewMesh):
                cloned_meshes.append(
                    ModelPreviewMesh(
                        **{field_info.name: getattr(mesh, field_info.name) for field_info in dataclass_fields(ModelPreviewMesh)}
                    )
                )
            else:
                cloned_meshes.append(mesh)
        return ModelPreviewData(
            **{
                field_info.name: (
                    cloned_meshes
                    if field_info.name == "meshes"
                    else getattr(model, field_info.name)
                )
                for field_info in dataclass_fields(ModelPreviewData)
            }
        )

    @staticmethod
    def _normalize_override_target(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    @staticmethod
    def _initialize_mesh_preview_slot_defaults(mesh: ModelPreviewMesh) -> None:
        if (
            not str(getattr(mesh, "preview_base_texture_default_path", "") or "").strip()
            and not str(getattr(mesh, "preview_base_texture_default_name", "") or "").strip()
        ):
            mesh.preview_base_texture_default_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
            mesh.preview_base_texture_default_name = str(getattr(mesh, "texture_name", "") or "").strip()
        if (
            not str(getattr(mesh, "preview_normal_texture_default_path", "") or "").strip()
            and not str(getattr(mesh, "preview_normal_texture_default_name", "") or "").strip()
        ):
            mesh.preview_normal_texture_default_path = str(getattr(mesh, "preview_normal_texture_path", "") or "").strip()
            mesh.preview_normal_texture_default_name = str(getattr(mesh, "preview_normal_texture_name", "") or "").strip()
            mesh.preview_normal_texture_default_strength = float(
                getattr(mesh, "preview_normal_texture_strength", 0.0) or 0.0
            )
        if (
            not str(getattr(mesh, "preview_material_texture_default_path", "") or "").strip()
            and not str(getattr(mesh, "preview_material_texture_default_name", "") or "").strip()
        ):
            mesh.preview_material_texture_default_path = str(getattr(mesh, "preview_material_texture_path", "") or "").strip()
            mesh.preview_material_texture_default_name = str(getattr(mesh, "preview_material_texture_name", "") or "").strip()
            mesh.preview_material_texture_default_type = str(getattr(mesh, "preview_material_texture_type", "") or "").strip()
            mesh.preview_material_texture_default_subtype = str(
                getattr(mesh, "preview_material_texture_subtype", "") or ""
            ).strip()
            mesh.preview_material_texture_default_packed_channels = tuple(
                str(channel or "").strip().lower()
                for channel in (getattr(mesh, "preview_material_texture_packed_channels", ()) or ())
                if str(channel or "").strip()
            )
        if (
            not str(getattr(mesh, "preview_height_texture_default_path", "") or "").strip()
            and not str(getattr(mesh, "preview_height_texture_default_name", "") or "").strip()
        ):
            mesh.preview_height_texture_default_path = str(getattr(mesh, "preview_height_texture_path", "") or "").strip()
            mesh.preview_height_texture_default_name = str(getattr(mesh, "preview_height_texture_name", "") or "").strip()

    def _initialize_preview_slot_defaults(self, model: Optional[ModelPreviewData]) -> None:
        meshes = getattr(model, "meshes", None) or []
        for mesh in meshes:
            if isinstance(mesh, ModelPreviewMesh):
                self._initialize_mesh_preview_slot_defaults(mesh)

    def set_use_textures(self, use_textures: bool) -> None:
        self._use_textures = bool(use_textures)
        self.update()

    def support_maps_available(self) -> bool:
        return any(
            batch.normal_texture_key or batch.material_texture_key or batch.height_texture_key
            for batch in self._mesh_batches
        )

    def base_flip_override_enabled(self) -> bool:
        meshes = getattr(self._current_model, "meshes", None) or []
        return any(bool(getattr(mesh, "preview_debug_flip_base_v", False)) for mesh in meshes)

    def support_maps_disabled(self) -> bool:
        meshes = getattr(self._current_model, "meshes", None) or []
        return any(bool(getattr(mesh, "preview_debug_disable_support_maps", False)) for mesh in meshes)

    def _slot_override_active(self, mesh: ModelPreviewMesh, slot: str) -> bool:
        normalized_slot = str(slot or "").strip().lower()
        if normalized_slot == "base":
            return (
                str(getattr(mesh, "preview_texture_path", "") or "").strip()
                != str(getattr(mesh, "preview_base_texture_default_path", "") or "").strip()
                or str(getattr(mesh, "texture_name", "") or "").strip()
                != str(getattr(mesh, "preview_base_texture_default_name", "") or "").strip()
            )
        if normalized_slot == "normal":
            return (
                str(getattr(mesh, "preview_normal_texture_path", "") or "").strip()
                != str(getattr(mesh, "preview_normal_texture_default_path", "") or "").strip()
                or str(getattr(mesh, "preview_normal_texture_name", "") or "").strip()
                != str(getattr(mesh, "preview_normal_texture_default_name", "") or "").strip()
                or abs(
                    float(getattr(mesh, "preview_normal_texture_strength", 0.0) or 0.0)
                    - float(getattr(mesh, "preview_normal_texture_default_strength", 0.0) or 0.0)
                ) > 1e-6
            )
        if normalized_slot == "material":
            return (
                str(getattr(mesh, "preview_material_texture_path", "") or "").strip()
                != str(getattr(mesh, "preview_material_texture_default_path", "") or "").strip()
                or str(getattr(mesh, "preview_material_texture_name", "") or "").strip()
                != str(getattr(mesh, "preview_material_texture_default_name", "") or "").strip()
                or str(getattr(mesh, "preview_material_texture_type", "") or "").strip().lower()
                != str(getattr(mesh, "preview_material_texture_default_type", "") or "").strip().lower()
                or str(getattr(mesh, "preview_material_texture_subtype", "") or "").strip().lower()
                != str(getattr(mesh, "preview_material_texture_default_subtype", "") or "").strip().lower()
                or tuple(
                    str(channel or "").strip().lower()
                    for channel in (getattr(mesh, "preview_material_texture_packed_channels", ()) or ())
                    if str(channel or "").strip()
                )
                != tuple(
                    str(channel or "").strip().lower()
                    for channel in (getattr(mesh, "preview_material_texture_default_packed_channels", ()) or ())
                    if str(channel or "").strip()
                )
            )
        if normalized_slot == "height":
            return (
                str(getattr(mesh, "preview_height_texture_path", "") or "").strip()
                != str(getattr(mesh, "preview_height_texture_default_path", "") or "").strip()
                or str(getattr(mesh, "preview_height_texture_name", "") or "").strip()
                != str(getattr(mesh, "preview_height_texture_default_name", "") or "").strip()
            )
        return False

    def texture_slot_overrides_active(self) -> bool:
        meshes = getattr(self._current_model, "meshes", None) or []
        return any(
            self._slot_override_active(mesh, slot)
            for mesh in meshes
            for slot in ("base", "normal", "material", "height")
        )

    def debug_overrides_active(self) -> bool:
        return self.base_flip_override_enabled() or self.support_maps_disabled() or self.texture_slot_overrides_active()

    def set_base_texture_flip_override_enabled(self, enabled: bool) -> None:
        self._set_mesh_debug_override("preview_debug_flip_base_v", bool(enabled))

    def set_support_maps_disabled(self, enabled: bool) -> None:
        self._set_mesh_debug_override("preview_debug_disable_support_maps", bool(enabled))

    def _iter_override_target_meshes(self, material_name: object) -> List[ModelPreviewMesh]:
        meshes = [
            mesh
            for mesh in (getattr(self._current_model, "meshes", None) or [])
            if isinstance(mesh, ModelPreviewMesh)
        ]
        if not meshes:
            return []
        normalized_material_name = self._normalize_override_target(material_name)
        if not normalized_material_name:
            return meshes
        matched_meshes = [
            mesh
            for mesh in meshes
            if self._normalize_override_target(getattr(mesh, "material_name", "")) == normalized_material_name
        ]
        return matched_meshes or meshes

    def set_texture_slot_override(
        self,
        slot: str,
        *,
        preview_path: str,
        texture_name: str,
        material_name: str = "",
        normal_strength: float = 0.0,
        material_texture_type: str = "",
        material_texture_subtype: str = "",
        material_texture_packed_channels: Sequence[str] = (),
    ) -> bool:
        meshes = self._iter_override_target_meshes(material_name)
        if not meshes:
            return False
        normalized_slot = str(slot or "").strip().lower()
        preview_path_text = str(preview_path or "").strip()
        texture_name_text = str(texture_name or "").strip()
        if normalized_slot not in {"base", "normal", "material", "height"} or not preview_path_text:
            return False

        changed = False
        for mesh in meshes:
            self._initialize_mesh_preview_slot_defaults(mesh)
            if normalized_slot == "base":
                if str(getattr(mesh, "preview_texture_path", "") or "").strip() != preview_path_text:
                    mesh.preview_texture_path = preview_path_text
                    mesh.preview_texture_image = None
                    changed = True
                if str(getattr(mesh, "texture_name", "") or "").strip() != texture_name_text:
                    mesh.texture_name = texture_name_text
                    changed = True
                continue
            if normalized_slot == "normal":
                if str(getattr(mesh, "preview_normal_texture_path", "") or "").strip() != preview_path_text:
                    mesh.preview_normal_texture_path = preview_path_text
                    mesh.preview_normal_texture_image = None
                    changed = True
                if str(getattr(mesh, "preview_normal_texture_name", "") or "").strip() != texture_name_text:
                    mesh.preview_normal_texture_name = texture_name_text
                    changed = True
                if abs(float(getattr(mesh, "preview_normal_texture_strength", 0.0) or 0.0) - float(normal_strength)) > 1e-6:
                    mesh.preview_normal_texture_strength = float(normal_strength)
                    changed = True
                continue
            if normalized_slot == "material":
                packed_channels = tuple(
                    str(channel or "").strip().lower()
                    for channel in material_texture_packed_channels
                    if str(channel or "").strip()
                )
                if str(getattr(mesh, "preview_material_texture_path", "") or "").strip() != preview_path_text:
                    mesh.preview_material_texture_path = preview_path_text
                    mesh.preview_material_texture_image = None
                    changed = True
                if str(getattr(mesh, "preview_material_texture_name", "") or "").strip() != texture_name_text:
                    mesh.preview_material_texture_name = texture_name_text
                    changed = True
                if str(getattr(mesh, "preview_material_texture_type", "") or "").strip().lower() != str(material_texture_type or "").strip().lower():
                    mesh.preview_material_texture_type = str(material_texture_type or "").strip().lower()
                    changed = True
                if str(getattr(mesh, "preview_material_texture_subtype", "") or "").strip().lower() != str(material_texture_subtype or "").strip().lower():
                    mesh.preview_material_texture_subtype = str(material_texture_subtype or "").strip().lower()
                    changed = True
                if tuple(
                    str(channel or "").strip().lower()
                    for channel in (getattr(mesh, "preview_material_texture_packed_channels", ()) or ())
                    if str(channel or "").strip()
                ) != packed_channels:
                    mesh.preview_material_texture_packed_channels = packed_channels
                    changed = True
                continue
            if str(getattr(mesh, "preview_height_texture_path", "") or "").strip() != preview_path_text:
                mesh.preview_height_texture_path = preview_path_text
                mesh.preview_height_texture_image = None
                changed = True
            if str(getattr(mesh, "preview_height_texture_name", "") or "").strip() != texture_name_text:
                mesh.preview_height_texture_name = texture_name_text
                changed = True
        if changed:
            self._rebuild_preview_batches()
        return changed

    def reset_preview_overrides(self) -> None:
        meshes = getattr(self._current_model, "meshes", None) or []
        if not meshes:
            return
        changed = False
        for mesh in meshes:
            if isinstance(mesh, ModelPreviewMesh):
                self._initialize_mesh_preview_slot_defaults(mesh)
            if bool(getattr(mesh, "preview_debug_flip_base_v", False)):
                mesh.preview_debug_flip_base_v = False
                changed = True
            if bool(getattr(mesh, "preview_debug_disable_support_maps", False)):
                mesh.preview_debug_disable_support_maps = False
                changed = True
            if isinstance(mesh, ModelPreviewMesh):
                if self._slot_override_active(mesh, "base"):
                    mesh.preview_texture_path = str(getattr(mesh, "preview_base_texture_default_path", "") or "").strip()
                    mesh.texture_name = str(getattr(mesh, "preview_base_texture_default_name", "") or "").strip()
                    mesh.preview_texture_image = None
                    changed = True
                if self._slot_override_active(mesh, "normal"):
                    mesh.preview_normal_texture_path = str(getattr(mesh, "preview_normal_texture_default_path", "") or "").strip()
                    mesh.preview_normal_texture_name = str(getattr(mesh, "preview_normal_texture_default_name", "") or "").strip()
                    mesh.preview_normal_texture_strength = float(
                        getattr(mesh, "preview_normal_texture_default_strength", 0.0) or 0.0
                    )
                    mesh.preview_normal_texture_image = None
                    changed = True
                if self._slot_override_active(mesh, "material"):
                    mesh.preview_material_texture_path = str(
                        getattr(mesh, "preview_material_texture_default_path", "") or ""
                    ).strip()
                    mesh.preview_material_texture_name = str(
                        getattr(mesh, "preview_material_texture_default_name", "") or ""
                    ).strip()
                    mesh.preview_material_texture_type = str(
                        getattr(mesh, "preview_material_texture_default_type", "") or ""
                    ).strip().lower()
                    mesh.preview_material_texture_subtype = str(
                        getattr(mesh, "preview_material_texture_default_subtype", "") or ""
                    ).strip().lower()
                    mesh.preview_material_texture_packed_channels = tuple(
                        str(channel or "").strip().lower()
                        for channel in (getattr(mesh, "preview_material_texture_default_packed_channels", ()) or ())
                        if str(channel or "").strip()
                    )
                    mesh.preview_material_texture_image = None
                    changed = True
                if self._slot_override_active(mesh, "height"):
                    mesh.preview_height_texture_path = str(getattr(mesh, "preview_height_texture_default_path", "") or "").strip()
                    mesh.preview_height_texture_name = str(getattr(mesh, "preview_height_texture_default_name", "") or "").strip()
                    mesh.preview_height_texture_image = None
                    changed = True
        if changed:
            self._rebuild_preview_batches()

    def current_model_preview(self) -> Optional[ModelPreviewData]:
        return self._clone_model_preview(self._current_model)

    def _set_mesh_debug_override(
        self,
        field_name: str,
        enabled: bool,
    ) -> None:
        meshes = getattr(self._current_model, "meshes", None) or []
        if not meshes:
            return
        changed = False
        for mesh in meshes:
            current = bool(getattr(mesh, field_name, False))
            if current == enabled:
                continue
            setattr(mesh, field_name, bool(enabled))
            changed = True
        if not changed:
            return
        self._rebuild_preview_batches()

    def _rebuild_preview_batches(self) -> None:
        self._vertex_blob, self._vertex_count, self._mesh_batches = self._build_vertex_blob(self._current_model)
        self._refresh_debug_overlay_lines()
        if self._gl_ready and self.context() is not None:
            self._upload_geometry()
        self.update()

    @staticmethod
    def _texture_display_name(explicit_name: object, texture_path: object) -> str:
        explicit_text = str(explicit_name or "").strip()
        if explicit_text:
            return PurePosixPath(explicit_text).name or explicit_text
        texture_path_text = str(texture_path or "").strip()
        if not texture_path_text:
            return ""
        return PurePosixPath(texture_path_text).name or texture_path_text

    @staticmethod
    def _summarize_overlay_values(values: Sequence[str]) -> str:
        unique_values: List[str] = []
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in unique_values:
                continue
            unique_values.append(normalized)
        if not unique_values:
            return "None"
        if len(unique_values) <= 2:
            return ", ".join(unique_values)
        return f"{', '.join(unique_values[:2])} (+{len(unique_values) - 2} more)"

    @staticmethod
    def _format_material_channel_name(channel_name: str) -> str:
        normalized = str(channel_name or "").strip().lower()
        if not normalized:
            return "Unknown"
        replacements = {
            "ao": "AO",
            "orm": "ORM",
            "rma": "RMA",
            "mra": "MRA",
            "arm": "ARM",
            "alpha": "Alpha",
        }
        if normalized in replacements:
            return replacements[normalized]
        return normalized.replace("_", " ").title()

    @classmethod
    def _material_decode_mode_for_semantics(
        cls,
        texture_type: object,
        semantic_subtype: object,
        packed_channels: Sequence[str],
    ) -> int:
        normalized_type = str(texture_type or "").strip().lower()
        normalized_subtype = str(semantic_subtype or "").strip().lower()
        normalized_channels = tuple(
            str(channel or "").strip().lower()
            for channel in packed_channels
            if str(channel or "").strip()
        )
        if normalized_subtype == "specular" or normalized_type == "specular":
            return cls._MATERIAL_DECODE_SPECULAR
        if normalized_subtype == "ao":
            return cls._MATERIAL_DECODE_AO
        if normalized_subtype in {"roughness", "gloss_or_smoothness"} or normalized_type == "roughness":
            return cls._MATERIAL_DECODE_ROUGHNESS
        if normalized_subtype == "metallic" or normalized_type == "metallic":
            return cls._MATERIAL_DECODE_METALLIC
        if normalized_subtype == "material_mask":
            return cls._MATERIAL_DECODE_MATERIAL_MASK
        if normalized_subtype == "material_response":
            return cls._MATERIAL_DECODE_MATERIAL_RESPONSE
        if normalized_subtype == "packed_mask":
            return cls._MATERIAL_DECODE_PACKED_MASK
        if normalized_subtype == "orm":
            return cls._MATERIAL_DECODE_ORM
        if normalized_subtype == "rma":
            return cls._MATERIAL_DECODE_RMA
        if normalized_subtype == "mra":
            return cls._MATERIAL_DECODE_MRA
        if normalized_subtype == "arm":
            return cls._MATERIAL_DECODE_ARM
        if normalized_subtype == "opacity_mask":
            return cls._MATERIAL_DECODE_OPACITY_MASK
        if normalized_channels[:3] == ("ao", "roughness", "metallic"):
            return cls._MATERIAL_DECODE_ORM
        if normalized_channels[:3] == ("roughness", "metallic", "ao"):
            return cls._MATERIAL_DECODE_RMA
        if normalized_channels[:3] == ("metallic", "roughness", "ao"):
            return cls._MATERIAL_DECODE_MRA
        if normalized_channels:
            return cls._MATERIAL_DECODE_PACKED_MASK
        return cls._MATERIAL_DECODE_GENERIC

    @classmethod
    def _describe_material_interpretation(
        cls,
        texture_type: object,
        semantic_subtype: object,
        packed_channels: Sequence[str],
    ) -> str:
        normalized_type = str(texture_type or "").strip().lower()
        normalized_subtype = str(semantic_subtype or "").strip().lower()
        normalized_channels = tuple(
            str(channel or "").strip().lower()
            for channel in packed_channels
            if str(channel or "").strip()
        )
        label_lookup = {
            "specular": "Specular Response",
            "ao": "Ambient Occlusion",
            "roughness": "Roughness",
            "gloss_or_smoothness": "Gloss or Smoothness",
            "metallic": "Metallic",
            "material_mask": "Material Mask",
            "material_response": "Material Response",
            "packed_mask": "Packed Mask",
            "opacity_mask": "Opacity Mask",
            "orm": "ORM",
            "rma": "RMA",
            "mra": "MRA",
            "arm": "ARM",
        }
        label = label_lookup.get(normalized_subtype)
        if label is None and normalized_subtype:
            label = cls._format_material_channel_name(normalized_subtype)
        if label is None and normalized_type:
            label = cls._format_material_channel_name(normalized_type)
        if not label:
            label = "Generic Heuristic"
        if normalized_channels:
            channel_text = " / ".join(cls._format_material_channel_name(channel) for channel in normalized_channels)
            return f"{label} ({channel_text})"
        return label

    def _refresh_debug_overlay_lines(self) -> None:
        meshes = getattr(self._current_model, "meshes", None) or []
        if not meshes:
            self._debug_overlay_lines = ()
            self._debug_detail_lines = ()
            self.debug_details_changed.emit("")
            return
        base_names: List[str] = []
        normal_names: List[str] = []
        material_names: List[str] = []
        material_interpretations: List[str] = []
        height_names: List[str] = []
        for mesh in meshes:
            base_names.append(
                self._texture_display_name(
                    getattr(mesh, "texture_name", ""),
                    getattr(mesh, "preview_texture_path", ""),
                )
            )
            normal_names.append(
                self._texture_display_name(
                    getattr(mesh, "preview_normal_texture_name", ""),
                    getattr(mesh, "preview_normal_texture_path", ""),
                )
            )
            material_names.append(
                self._texture_display_name(
                    getattr(mesh, "preview_material_texture_name", ""),
                    getattr(mesh, "preview_material_texture_path", ""),
                )
            )
            height_names.append(
                self._texture_display_name(
                    getattr(mesh, "preview_height_texture_name", ""),
                    getattr(mesh, "preview_height_texture_path", ""),
                )
            )
            if str(getattr(mesh, "preview_material_texture_path", "") or "").strip() or getattr(
                mesh,
                "preview_material_texture_image",
                None,
            ) is not None:
                material_interpretations.append(
                    self._describe_material_interpretation(
                        getattr(mesh, "preview_material_texture_type", ""),
                        getattr(mesh, "preview_material_texture_subtype", ""),
                        getattr(mesh, "preview_material_texture_packed_channels", ()) or (),
                    )
                )
        override_labels: List[str] = []
        if any(self._slot_override_active(mesh, "base") for mesh in meshes if isinstance(mesh, ModelPreviewMesh)):
            override_labels.append("Base Override")
        if any(self._slot_override_active(mesh, "normal") for mesh in meshes if isinstance(mesh, ModelPreviewMesh)):
            override_labels.append("Normal Override")
        if any(self._slot_override_active(mesh, "material") for mesh in meshes if isinstance(mesh, ModelPreviewMesh)):
            override_labels.append("Material Override")
        if any(self._slot_override_active(mesh, "height") for mesh in meshes if isinstance(mesh, ModelPreviewMesh)):
            override_labels.append("Height Override")
        if self.base_flip_override_enabled():
            override_labels.append("Flip Base V")
        if self.support_maps_disabled():
            override_labels.append("Support Maps Off")
        self._debug_overlay_lines = (
            f"Visible Mode: {self._visible_texture_mode_label()}",
        )
        self._debug_detail_lines = (
            self._debug_overlay_lines[0],
            f"Base: {self._summarize_overlay_values(base_names)}",
            f"Normal: {self._summarize_overlay_values(normal_names)}",
            f"Material: {self._summarize_overlay_values(material_names)}",
            f"Material Decode: {self._summarize_overlay_values(material_interpretations)}",
            f"Height: {self._summarize_overlay_values(height_names)}",
            f"Overrides: {', '.join(override_labels) if override_labels else 'None'}",
        )
        self.debug_details_changed.emit("\n".join(self._debug_detail_lines))

    def _visible_texture_mode_label(self) -> str:
        settings = self.render_settings()
        mode = str(getattr(settings, "visible_texture_mode", "") or "").strip().lower()
        return MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS.get(mode, mode.replace("_", " ").title() or "Mesh Base First")

    def render_settings(self) -> ModelPreviewRenderSettings:
        return clamp_model_preview_render_settings(self._render_settings)

    def debug_details_text(self) -> str:
        return "\n".join(self._debug_detail_lines)

    def set_render_settings(self, settings: Optional[ModelPreviewRenderSettings]) -> None:
        clamped = clamp_model_preview_render_settings(settings)
        previous = self._render_settings
        self._render_settings = clamped
        textures_changed = (
            previous.preview_texture_max_dimension != clamped.preview_texture_max_dimension
            or previous.low_quality_texture_max_dimension != clamped.low_quality_texture_max_dimension
            or previous.max_anisotropy != clamped.max_anisotropy
        )
        if previous.visible_texture_mode != clamped.visible_texture_mode:
            self._refresh_debug_overlay_lines()
        if textures_changed and self._gl_ready and self.context() is not None:
            self.makeCurrent()
            self._clear_gl_textures()
            self._rebuild_gl_textures()
            self.doneCurrent()
        self.update()

    def set_high_quality_textures(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._high_quality_textures == enabled:
            return
        self._high_quality_textures = enabled
        if self._gl_ready and self.context() is not None:
            self.makeCurrent()
            self._clear_gl_textures()
            self._rebuild_gl_textures()
            self.doneCurrent()
        self.update()

    def textures_available(self) -> bool:
        return any(batch.texture_key and batch.has_texture_coordinates for batch in self._mesh_batches)

    def textures_enabled(self) -> bool:
        return bool(self._use_textures)

    def high_quality_textures_enabled(self) -> bool:
        return bool(self._high_quality_textures)

    def set_zoom_factor(self, zoom_factor: float) -> None:
        self._zoom_factor = min(max(float(zoom_factor), 0.1), 16.0)
        if not self._fit_to_view:
            self._distance = self._FIT_DISTANCE / self._zoom_factor
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()

    def set_fit_to_view(self, fit_to_view: bool) -> None:
        self._fit_to_view = bool(fit_to_view)
        self._distance = self._FIT_DISTANCE if self._fit_to_view else self._FIT_DISTANCE / self._zoom_factor
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()

    def current_display_scale(self) -> float:
        return max(0.1, self._FIT_DISTANCE / max(self._distance, 0.01))

    def _world_units_per_pixel(self) -> float:
        viewport_height = max(1, self.height())
        visible_height = 2.0 * max(self._distance, 0.1) * math.tan(math.radians(self._VERTICAL_FOV_DEGREES) * 0.5)
        return visible_height / float(viewport_height)

    def _camera_orbit_basis(self) -> Tuple[QVector3D, QVector3D, QVector3D]:
        yaw_radians = math.radians(float(self._yaw))
        pitch_radians = math.radians(float(self._pitch))
        cos_yaw = math.cos(yaw_radians)
        sin_yaw = math.sin(yaw_radians)
        cos_pitch = math.cos(pitch_radians)
        sin_pitch = math.sin(pitch_radians)

        # Build a simple orbit camera basis without relying on PySide's
        # matrix/vector operator overloads, which are brittle at runtime.
        forward = QVector3D(
            sin_yaw * cos_pitch,
            sin_pitch,
            cos_yaw * cos_pitch,
        ).normalized()
        right = QVector3D.crossProduct(forward, QVector3D(0.0, 1.0, 0.0))
        if right.lengthSquared() <= 1e-8:
            right = QVector3D(1.0, 0.0, 0.0)
        else:
            right.normalize()
        up = QVector3D.crossProduct(right, forward)
        if up.lengthSquared() <= 1e-8:
            up = QVector3D(0.0, 1.0, 0.0)
        else:
            up.normalize()
        orbit_offset = QVector3D(
            forward.x() * float(self._distance),
            forward.y() * float(self._distance),
            forward.z() * float(self._distance),
        )
        return orbit_offset, right, up

    def _apply_pan_delta(self, delta_x: float, delta_y: float) -> None:
        if abs(delta_x) <= 1e-6 and abs(delta_y) <= 1e-6:
            return
        units_per_pixel = self._world_units_per_pixel()
        settings = self.render_settings()
        pan_scale = float(settings.pan_sensitivity)
        horizontal_sign = -1.0 if settings.invert_pan_x else 1.0
        vertical_sign = 1.0 if settings.invert_pan_y else -1.0
        horizontal_scale = float(delta_x) * units_per_pixel * pan_scale * horizontal_sign
        vertical_scale = float(delta_y) * units_per_pixel * pan_scale * vertical_sign
        self._pan_offset = self._pan_offset + QVector3D(horizontal_scale, vertical_scale, 0.0)
        self.update()

    def _poll_pan_drag(self) -> None:
        if not self._pan_drag_active:
            self._pan_poll_timer.stop()
            return
        if self._pan_drag_button != Qt.NoButton and not bool(QApplication.mouseButtons() & self._pan_drag_button):
            self._pan_drag_active = False
            self._pan_drag_button = Qt.NoButton
            self._pan_poll_timer.stop()
            self.releaseMouse()
            self.unsetCursor()
            return
        current_global_pos = QCursor.pos()
        delta = current_global_pos - self._last_global_mouse_pos
        if delta.x() or delta.y():
            self._last_global_mouse_pos = current_global_pos
            self._apply_pan_delta(delta.x(), delta.y())
            

    def initializeGL(self) -> None:  # type: ignore[override]
        self._functions = self.context().functions() if self.context() is not None else None
        if self._functions is None:
            return
        self._functions.glEnable(_GL_DEPTH_TEST)
        self._functions.glDisable(_GL_CULL_FACE)

        program = QOpenGLShaderProgram(self.context())
        if not program.addShaderFromSourceCode(
            QOpenGLShader.Vertex,
            """
            #version 120
            attribute vec3 position;
            attribute vec3 normal;
            attribute vec3 color;
            attribute vec2 texcoord;
            attribute vec3 tangent;
            attribute vec3 bitangent;
            uniform mat4 mvp_matrix;
            uniform mat4 model_matrix;
            varying vec3 frag_position;
            varying vec3 frag_normal;
            varying vec3 frag_color;
            varying vec2 frag_texcoord;
            varying vec3 frag_tangent;
            varying vec3 frag_bitangent;
            void main() {
                frag_position = (model_matrix * vec4(position, 1.0)).xyz;
                frag_normal = normalize((model_matrix * vec4(normal, 0.0)).xyz);
                frag_tangent = normalize((model_matrix * vec4(tangent, 0.0)).xyz);
                frag_bitangent = normalize((model_matrix * vec4(bitangent, 0.0)).xyz);
                frag_color = color;
                frag_texcoord = texcoord;
                gl_Position = mvp_matrix * vec4(position, 1.0);
            }
            """,
        ):
            raise RuntimeError(f"Model preview vertex shader failed: {program.log()}")
        if not program.addShaderFromSourceCode(
            QOpenGLShader.Fragment,
            """
            #version 120
            varying vec3 frag_position;
            varying vec3 frag_normal;
            varying vec3 frag_color;
            varying vec2 frag_texcoord;
            varying vec3 frag_tangent;
            varying vec3 frag_bitangent;
            uniform vec3 camera_position;
            uniform vec3 light_direction;
            uniform float ambient_strength;
            uniform int use_texture;
            uniform int use_high_quality;
            uniform int use_normal_texture;
            uniform float normal_texture_strength;
            uniform int use_material_texture;
            uniform int use_height_texture;
            uniform float diffuse_wrap_bias;
            uniform float diffuse_light_scale;
            uniform float normal_strength_cap;
            uniform float normal_strength_floor;
            uniform float height_effect_max;
            uniform float cavity_clamp_min;
            uniform float cavity_clamp_max;
            uniform float specular_base;
            uniform float specular_min;
            uniform float specular_max;
            uniform float shininess_base;
            uniform float shininess_min;
            uniform float shininess_max;
            uniform float height_shininess_boost;
            uniform int material_decode_mode;
            uniform sampler2D diffuse_texture;
            uniform sampler2D normal_texture;
            uniform sampler2D material_texture;
            uniform sampler2D height_texture;
            float wrapped_lambert(vec3 surface_normal, vec3 light_vector, float wrap_bias) {
                return clamp((dot(surface_normal, light_vector) + wrap_bias) / (1.0 + wrap_bias), 0.0, 1.0);
            }
            void decode_material_sample(
                vec4 sample_value,
                int decode_mode,
                out float ao_value,
                out float roughness_value,
                out float metallic_value,
                out float specular_value,
                out float cavity_value,
                out float opacity_value
            ) {
                vec4 sample = clamp(sample_value, 0.0, 1.0);
                float average = dot(sample.rgb, vec3(0.3333, 0.3333, 0.3334));
                float peak = max(max(sample.r, sample.g), max(sample.b, sample.a));
                float minimum = min(min(sample.r, sample.g), min(sample.b, sample.a));
                float variance = max(peak - minimum, 0.0);
                ao_value = 1.0;
                roughness_value = 0.58;
                metallic_value = 0.0;
                specular_value = clamp(0.12 + (variance * 0.24), 0.05, 0.42);
                cavity_value = clamp(1.0 - (variance * 0.14), 0.78, 1.0);
                opacity_value = 1.0;
                if (decode_mode == 1) {
                    specular_value = clamp(max(max(sample.r, sample.g), sample.b), 0.06, 1.0);
                    roughness_value = clamp(1.0 - max(sample.g, average), 0.08, 0.92);
                    cavity_value = clamp(1.0 - (specular_value * 0.12), 0.84, 1.0);
                } else if (decode_mode == 2) {
                    ao_value = clamp(sample.r, 0.35, 1.0);
                    roughness_value = 0.74;
                    metallic_value = 0.0;
                    specular_value = 0.08;
                    cavity_value = ao_value;
                } else if (decode_mode == 3) {
                    float rough_source = max(sample.g, average);
                    roughness_value = clamp(rough_source, 0.06, 0.98);
                    specular_value = clamp(0.42 - (roughness_value * 0.28), 0.04, 0.30);
                    cavity_value = clamp(1.0 - (roughness_value * 0.08), 0.88, 1.0);
                } else if (decode_mode == 4) {
                    metallic_value = clamp(max(sample.r, average), 0.0, 1.0);
                    roughness_value = clamp(0.18 + ((1.0 - max(sample.g, average)) * 0.62), 0.08, 0.92);
                    specular_value = clamp(0.16 + (metallic_value * 0.48), 0.06, 0.72);
                    cavity_value = clamp(1.0 - (metallic_value * 0.05), 0.90, 1.0);
                } else if (decode_mode == 5) {
                    ao_value = clamp(1.0 - (sample.r * 0.30), 0.60, 1.0);
                    roughness_value = clamp(0.28 + (sample.g * 0.56), 0.10, 0.96);
                    specular_value = clamp(0.10 + (sample.b * 0.34) + (sample.a * 0.10), 0.05, 0.46);
                    metallic_value = clamp(sample.b * 0.28, 0.0, 0.55);
                    cavity_value = clamp(min(ao_value, 1.0 - (sample.a * 0.08)), 0.58, 1.0);
                } else if (decode_mode == 6) {
                    ao_value = clamp(1.0 - (sample.r * 0.20), 0.68, 1.0);
                    roughness_value = clamp(0.16 + ((1.0 - sample.g) * 0.72), 0.08, 0.96);
                    specular_value = clamp(0.12 + (max(sample.b, sample.a) * 0.42), 0.05, 0.62);
                    metallic_value = clamp((sample.b * 0.24) + (sample.a * 0.16), 0.0, 0.58);
                    cavity_value = clamp(min(ao_value, 1.0 - (variance * 0.20)), 0.64, 1.0);
                } else if (decode_mode == 7) {
                    ao_value = clamp(1.0 - (sample.r * 0.18), 0.74, 1.0);
                    roughness_value = clamp(0.24 + (sample.g * 0.56), 0.10, 0.96);
                    specular_value = clamp(0.10 + (sample.b * 0.26) + (sample.a * 0.12), 0.04, 0.44);
                    metallic_value = clamp(sample.b * 0.35, 0.0, 0.60);
                    cavity_value = clamp(min(ao_value, 1.0 - (variance * 0.10)), 0.70, 1.0);
                } else if (decode_mode == 8 || decode_mode == 11) {
                    ao_value = clamp(sample.r, 0.20, 1.0);
                    roughness_value = clamp(sample.g, 0.05, 0.98);
                    metallic_value = clamp(sample.b, 0.0, 1.0);
                    specular_value = clamp((0.10 + (metallic_value * 0.54)) * (1.0 - (roughness_value * 0.38)), 0.05, 0.72);
                    cavity_value = ao_value;
                } else if (decode_mode == 9) {
                    roughness_value = clamp(sample.r, 0.05, 0.98);
                    metallic_value = clamp(sample.g, 0.0, 1.0);
                    ao_value = clamp(sample.b, 0.20, 1.0);
                    specular_value = clamp((0.10 + (metallic_value * 0.54)) * (1.0 - (roughness_value * 0.38)), 0.05, 0.72);
                    cavity_value = ao_value;
                } else if (decode_mode == 10) {
                    metallic_value = clamp(sample.r, 0.0, 1.0);
                    roughness_value = clamp(sample.g, 0.05, 0.98);
                    ao_value = clamp(sample.b, 0.20, 1.0);
                    specular_value = clamp((0.10 + (metallic_value * 0.54)) * (1.0 - (roughness_value * 0.38)), 0.05, 0.72);
                    cavity_value = ao_value;
                } else if (decode_mode == 12) {
                    opacity_value = clamp(sample.a > 0.01 ? sample.a : average, 0.0, 1.0);
                    roughness_value = 0.60;
                    metallic_value = 0.0;
                    specular_value = 0.10;
                    cavity_value = 1.0;
                } else {
                    ao_value = clamp(1.0 - (sample.r * 0.16), 0.78, 1.0);
                    roughness_value = clamp(0.22 + (sample.g * 0.54), 0.10, 0.96);
                    metallic_value = clamp(max(0.0, sample.b - (sample.r * 0.30)) * 0.55, 0.0, 0.72);
                    specular_value = clamp(0.10 + (sample.b * 0.22) + (sample.a * 0.18) + (variance * 0.12), 0.04, 0.55);
                    cavity_value = clamp(min(ao_value, 1.0 - (variance * 0.16)), 0.74, 1.0);
                }
                ao_value = clamp(ao_value, 0.0, 1.0);
                roughness_value = clamp(roughness_value, 0.04, 1.0);
                metallic_value = clamp(metallic_value, 0.0, 1.0);
                specular_value = clamp(specular_value, 0.0, 1.0);
                cavity_value = clamp(cavity_value, 0.0, 1.0);
                opacity_value = clamp(opacity_value, 0.0, 1.0);
            }
            void main() {
                vec3 fallback_vertex_color = max(frag_color, vec3(0.0));
                float fallback_luma = dot(fallback_vertex_color, vec3(0.2126, 0.7152, 0.0722));
                if (fallback_luma <= 0.045) {
                    fallback_vertex_color = vec3(0.74, 0.76, 0.80);
                }
                vec4 base_color = vec4(fallback_vertex_color, 1.0);
                if (use_texture != 0) {
                    base_color = texture2D(diffuse_texture, frag_texcoord);
                    if (base_color.a <= 0.01) {
                        discard;
                    }
                }

                vec3 surface_normal = normalize(frag_normal);
                vec4 material_sample = vec4(0.5, 0.5, 0.5, 1.0);
                if (use_material_texture != 0) {
                    material_sample = texture2D(material_texture, frag_texcoord);
                }
                float material_ao = 1.0;
                float material_roughness = 0.58;
                float material_metallic = 0.0;
                float material_specular = 0.12;
                float material_cavity = 1.0;
                float material_opacity = 1.0;
                if (use_material_texture != 0) {
                    decode_material_sample(
                        material_sample,
                        material_decode_mode,
                        material_ao,
                        material_roughness,
                        material_metallic,
                        material_specular,
                        material_cavity,
                        material_opacity
                    );
                    if (material_decode_mode == 12) {
                        base_color.a *= material_opacity;
                        if (base_color.a <= 0.01) {
                            discard;
                        }
                    }
                }

                float height_value = 0.5;
                if (use_height_texture != 0) {
                    height_value = texture2D(height_texture, frag_texcoord).r;
                }
                float relief = clamp((height_value - 0.5) * 2.0, -1.0, 1.0);
                float height_effect = clamp(abs(relief) * height_effect_max, 0.0, height_effect_max);

                if (use_high_quality != 0 && use_normal_texture != 0) {
                    vec3 tangent = normalize(frag_tangent);
                    vec3 bitangent = normalize(frag_bitangent);
                    vec3 sampled_normal = texture2D(normal_texture, frag_texcoord).xyz * 2.0 - 1.0;
                    sampled_normal.y = -sampled_normal.y;
                    float mapped_strength = clamp(
                        max(normal_texture_strength, normal_strength_floor),
                        0.0,
                        normal_strength_cap
                    );
                    float strength_ratio = clamp(mapped_strength / max(normal_strength_cap, 0.001), 0.0, 1.0);
                    sampled_normal.xy *= mix(0.90, 1.35, strength_ratio);
                    sampled_normal.xy *= mapped_strength;
                    sampled_normal = normalize(sampled_normal);
                    mat3 tbn = mat3(tangent, bitangent, surface_normal);
                    vec3 mapped_normal = normalize(tbn * sampled_normal);
                    surface_normal = normalize(mix(surface_normal, mapped_normal, clamp(mapped_strength * 1.08, 0.0, 1.0)));
                }

                vec3 main_light = normalize(light_direction);
                vec3 fill_light = normalize(vec3(-main_light.x * 0.35, 0.45, -main_light.z * 0.35));
                vec3 rim_light = normalize(vec3(-0.10, 0.32, -1.0));
                float wrap_bias = max(0.0, diffuse_wrap_bias);
                float primary_diffuse = wrapped_lambert(surface_normal, main_light, wrap_bias);
                float fill_diffuse = wrapped_lambert(surface_normal, fill_light, wrap_bias * 0.65);
                float back_diffuse = wrapped_lambert(surface_normal, -main_light, wrap_bias * 0.20);
                float rim_diffuse = wrapped_lambert(surface_normal, rim_light, wrap_bias * 0.20);
                float lighting = max(ambient_strength, 0.62);
                lighting += primary_diffuse * diffuse_light_scale;
                lighting += fill_diffuse * (use_high_quality != 0 ? 0.28 : 0.16);
                lighting += back_diffuse * (use_high_quality != 0 ? 0.10 : 0.04);
                lighting += rim_diffuse * (use_high_quality != 0 ? 0.12 : 0.06);

                if (use_high_quality != 0) {
                    float occlusion_drive = clamp(
                        mix(material_ao, min(material_ao, material_cavity), 0.45) - (abs(relief) * 0.10),
                        0.0,
                        1.0
                    );
                    float cavity_scale = clamp(
                        mix(cavity_clamp_min, cavity_clamp_max, occlusion_drive),
                        cavity_clamp_min,
                        cavity_clamp_max
                    );
                    lighting *= mix(1.0, cavity_scale, (use_material_texture != 0 || use_height_texture != 0) ? 0.40 : 0.12);
                    lighting += height_effect * 0.32;
                }

                vec3 view_dir = normalize(camera_position - frag_position);
                vec3 half_dir = normalize(main_light + view_dir);
                float view_facing = clamp(dot(surface_normal, view_dir), 0.0, 1.0);
                float specular = 0.0;
                float rim_specular = 0.0;
                vec3 specular_color = vec3(1.0);
                if (use_high_quality != 0) {
                    float specular_mask = clamp(
                        mix(specular_base, specular_max, material_specular),
                        specular_min,
                        specular_max
                    );
                    float shininess = clamp(
                        mix(shininess_max, shininess_min, material_roughness) + (height_effect * height_shininess_boost),
                        shininess_min,
                        shininess_max
                    );
                    float fresnel = pow(1.0 - view_facing, 4.0);
                    float rim_response = pow(1.0 - view_facing, 2.4);
                    specular_color = mix(
                        vec3(1.0),
                        clamp((base_color.rgb * 1.15) + vec3(0.08), 0.0, 1.0),
                        material_metallic * 0.68
                    );
                    specular = pow(max(dot(surface_normal, half_dir), 0.0), shininess) * specular_mask;
                    specular += fresnel * (0.03 + (material_specular * 0.18) + (material_metallic * 0.12));
                    rim_specular = rim_response * (0.02 + (material_specular * 0.07));
                }

                vec3 final_rgb = base_color.rgb * clamp(lighting, 0.60, 1.62);
                final_rgb += specular_color * specular;
                final_rgb += specular_color * rim_specular;
                gl_FragColor = vec4(final_rgb, base_color.a);
            }
            """,
        ):
            raise RuntimeError(f"Model preview fragment shader failed: {program.log()}")
        program.bindAttributeLocation("position", 0)
        program.bindAttributeLocation("normal", 1)
        program.bindAttributeLocation("color", 2)
        program.bindAttributeLocation("texcoord", 3)
        program.bindAttributeLocation("tangent", 4)
        program.bindAttributeLocation("bitangent", 5)
        if not program.link():
            raise RuntimeError(f"Model preview shader link failed: {program.log()}")

        self._program = program
        self._mvp_uniform_location = program.uniformLocation("mvp_matrix")
        self._model_uniform_location = program.uniformLocation("model_matrix")
        self._camera_uniform_location = program.uniformLocation("camera_position")
        self._light_uniform_location = program.uniformLocation("light_direction")
        self._ambient_uniform_location = program.uniformLocation("ambient_strength")
        self._texture_sampler_uniform_location = program.uniformLocation("diffuse_texture")
        self._normal_texture_sampler_uniform_location = program.uniformLocation("normal_texture")
        self._material_texture_sampler_uniform_location = program.uniformLocation("material_texture")
        self._height_texture_sampler_uniform_location = program.uniformLocation("height_texture")
        self._use_texture_uniform_location = program.uniformLocation("use_texture")
        self._use_high_quality_uniform_location = program.uniformLocation("use_high_quality")
        self._use_normal_texture_uniform_location = program.uniformLocation("use_normal_texture")
        self._normal_texture_strength_uniform_location = program.uniformLocation("normal_texture_strength")
        self._use_material_texture_uniform_location = program.uniformLocation("use_material_texture")
        self._material_decode_mode_uniform_location = program.uniformLocation("material_decode_mode")
        self._use_height_texture_uniform_location = program.uniformLocation("use_height_texture")
        self._diffuse_wrap_bias_uniform_location = program.uniformLocation("diffuse_wrap_bias")
        self._diffuse_light_scale_uniform_location = program.uniformLocation("diffuse_light_scale")
        self._normal_strength_cap_uniform_location = program.uniformLocation("normal_strength_cap")
        self._normal_strength_floor_uniform_location = program.uniformLocation("normal_strength_floor")
        self._height_effect_max_uniform_location = program.uniformLocation("height_effect_max")
        self._cavity_clamp_min_uniform_location = program.uniformLocation("cavity_clamp_min")
        self._cavity_clamp_max_uniform_location = program.uniformLocation("cavity_clamp_max")
        self._specular_base_uniform_location = program.uniformLocation("specular_base")
        self._specular_min_uniform_location = program.uniformLocation("specular_min")
        self._specular_max_uniform_location = program.uniformLocation("specular_max")
        self._shininess_base_uniform_location = program.uniformLocation("shininess_base")
        self._shininess_min_uniform_location = program.uniformLocation("shininess_min")
        self._shininess_max_uniform_location = program.uniformLocation("shininess_max")
        self._height_shininess_boost_uniform_location = program.uniformLocation("height_shininess_boost")
        self._vertex_array.create()
        self._vertex_buffer.create()
        self._vertex_buffer.setUsagePattern(QOpenGLBuffer.StaticDraw)
        self._gl_ready = True
        self._upload_geometry()

    def paintGL(self) -> None:  # type: ignore[override]
        if self._functions is None:
            return
        self._functions.glEnable(_GL_DEPTH_TEST)
        self._functions.glClearColor(
            self._background_color.redF(),
            self._background_color.greenF(),
            self._background_color.blueF(),
            1.0,
        )
        self._functions.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
        if self._program is None or self._vertex_count <= 0:
            return

        width = max(1, self.width())
        height = max(1, self.height())
        projection = QMatrix4x4()
        projection.perspective(self._VERTICAL_FOV_DEGREES, width / float(height), 0.1, 100.0)
        view = QMatrix4x4()
        view.translate(0.0, 0.0, -self._distance)
        model = QMatrix4x4()
        model.translate(self._pan_offset)
        model.rotate(self._pitch, 1.0, 0.0, 0.0)
        model.rotate(self._yaw, 0.0, 1.0, 0.0)
        mvp = projection * view * model

        settings = self.render_settings()
        self._program.bind()
        self._program.setUniformValue(self._mvp_uniform_location, mvp)
        self._program.setUniformValue(self._model_uniform_location, model)
        self._program.setUniformValue(self._camera_uniform_location, QVector3D(0.0, 0.0, self._distance))
        self._program.setUniformValue(self._light_uniform_location, QVector3D(0.20, 0.45, 1.0))
        self._program.setUniformValue(self._ambient_uniform_location, max(0.62, float(settings.ambient_strength)))
        self._program.setUniformValue(self._texture_sampler_uniform_location, 0)
        self._program.setUniformValue(self._normal_texture_sampler_uniform_location, 1)
        self._program.setUniformValue(self._material_texture_sampler_uniform_location, 2)
        self._program.setUniformValue(self._height_texture_sampler_uniform_location, 3)
        self._vertex_array.bind()
        for batch in self._mesh_batches:
            diffuse_texture = self._texture_objects.get(
                (batch.texture_key, batch.texture_wrap_repeat, batch.texture_flip_vertical)
            )
            normal_texture = self._texture_objects.get(
                (batch.normal_texture_key, batch.texture_wrap_repeat, batch.texture_flip_vertical)
            )
            material_texture = self._texture_objects.get(
                (batch.material_texture_key, batch.texture_wrap_repeat, batch.texture_flip_vertical)
            )
            height_texture = self._texture_objects.get(
                (batch.height_texture_key, batch.texture_wrap_repeat, batch.texture_flip_vertical)
            )
            use_texture = int(
                bool(
                    self._use_textures
                    and batch.has_texture_coordinates
                    and bool(batch.texture_key)
                    and diffuse_texture is not None
                )
            )
            use_high_quality_maps = bool(
                use_texture
                and self._high_quality_textures
                and batch.has_texture_coordinates
            )
            support_maps_enabled = bool(use_high_quality_maps and not batch.support_maps_disabled)
            use_normal_texture = int(
                bool(
                    support_maps_enabled
                    and batch.normal_texture_key
                    and normal_texture is not None
                    and float(batch.normal_texture_strength) > 0.0
                )
            )
            use_material_texture = int(
                bool(support_maps_enabled and batch.material_texture_key and material_texture is not None)
            )
            use_height_texture = int(
                bool(support_maps_enabled and batch.height_texture_key and height_texture is not None)
            )
            use_high_quality_shading = int(bool(use_texture and self._high_quality_textures))
            self._program.setUniformValue(self._use_texture_uniform_location, use_texture)
            self._program.setUniformValue(self._use_high_quality_uniform_location, use_high_quality_shading)
            self._program.setUniformValue(self._use_normal_texture_uniform_location, use_normal_texture)
            self._program.setUniformValue(
                self._normal_texture_strength_uniform_location,
                float(batch.normal_texture_strength if use_normal_texture else 0.0),
            )
            self._program.setUniformValue(self._use_material_texture_uniform_location, use_material_texture)
            self._program.setUniformValue(
                self._material_decode_mode_uniform_location,
                int(batch.material_decode_mode if use_material_texture else self._MATERIAL_DECODE_GENERIC),
            )
            self._program.setUniformValue(self._use_height_texture_uniform_location, use_height_texture)
            self._program.setUniformValue(self._diffuse_wrap_bias_uniform_location, float(settings.diffuse_wrap_bias))
            self._program.setUniformValue(self._diffuse_light_scale_uniform_location, float(settings.diffuse_light_scale))
            self._program.setUniformValue(self._normal_strength_cap_uniform_location, float(settings.normal_strength_cap))
            self._program.setUniformValue(self._normal_strength_floor_uniform_location, float(settings.normal_strength_floor))
            self._program.setUniformValue(self._height_effect_max_uniform_location, float(settings.height_effect_max))
            self._program.setUniformValue(self._cavity_clamp_min_uniform_location, float(settings.cavity_clamp_min))
            self._program.setUniformValue(self._cavity_clamp_max_uniform_location, float(settings.cavity_clamp_max))
            self._program.setUniformValue(self._specular_base_uniform_location, float(settings.specular_base))
            self._program.setUniformValue(self._specular_min_uniform_location, float(settings.specular_min))
            self._program.setUniformValue(self._specular_max_uniform_location, float(settings.specular_max))
            self._program.setUniformValue(self._shininess_base_uniform_location, float(settings.shininess_base))
            self._program.setUniformValue(self._shininess_min_uniform_location, float(settings.shininess_min))
            self._program.setUniformValue(self._shininess_max_uniform_location, float(settings.shininess_max))
            self._program.setUniformValue(self._height_shininess_boost_uniform_location, float(settings.height_shininess_boost))
            if use_texture and diffuse_texture is not None:
                diffuse_texture.bind(0)
            if use_normal_texture and normal_texture is not None:
                normal_texture.bind(1)
            if use_material_texture and material_texture is not None:
                material_texture.bind(2)
            if use_height_texture and height_texture is not None:
                height_texture.bind(3)
            self._functions.glDrawArrays(_GL_TRIANGLES, batch.first_vertex, batch.vertex_count)
            if use_texture and diffuse_texture is not None:
                diffuse_texture.release()
            if use_normal_texture and normal_texture is not None:
                normal_texture.release()
            if use_material_texture and material_texture is not None:
                material_texture.release()
            if use_height_texture and height_texture is not None:
                height_texture.release()
        self._vertex_array.release()
        self._program.release()

    def _preview_mvp_matrix(self) -> QMatrix4x4:
        width = max(1, self.width())
        height = max(1, self.height())
        projection = QMatrix4x4()
        projection.perspective(self._VERTICAL_FOV_DEGREES, width / float(height), 0.1, 100.0)
        view = QMatrix4x4()
        view.translate(0.0, 0.0, -self._distance)
        model = QMatrix4x4()
        model.translate(self._pan_offset)
        model.rotate(self._pitch, 1.0, 0.0, 0.0)
        model.rotate(self._yaw, 0.0, 1.0, 0.0)
        return projection * view * model

    def _project_preview_point(
        self,
        mvp: QMatrix4x4,
        point: tuple[float, float, float],
    ) -> Optional[QPointF]:
        clip = mvp.map(QVector4D(float(point[0]), float(point[1]), float(point[2]), 1.0))
        w = float(clip.w())
        if abs(w) < 1e-8:
            return None
        ndc_x = float(clip.x()) / w
        ndc_y = float(clip.y()) / w
        if ndc_x < -4.0 or ndc_x > 4.0 or ndc_y < -4.0 or ndc_y > 4.0:
            return None
        return QPointF(
            (ndc_x * 0.5 + 0.5) * float(max(1, self.width())),
            (1.0 - (ndc_y * 0.5 + 0.5)) * float(max(1, self.height())),
        )

    def _draw_preview_line(
        self,
        painter: QPainter,
        mvp: QMatrix4x4,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        color: QColor,
        *,
        width: float = 1.0,
    ) -> None:
        start_point = self._project_preview_point(mvp, start)
        end_point = self._project_preview_point(mvp, end)
        if start_point is None or end_point is None:
            return
        painter.setPen(QPen(color, width))
        painter.drawLine(start_point, end_point)

    def _draw_alignment_guides(self, painter: QPainter) -> None:
        if self._vertex_count <= 0 or not (self._show_grid_overlay or self._show_origin_overlay):
            return
        mvp = self._preview_mvp_matrix()
        grid_color = QColor(148, 163, 184, 42)
        major_grid_color = QColor(203, 213, 225, 72)
        extent = 2.5
        step = 0.5
        if self._show_grid_overlay:
            line_count = int(round((extent * 2.0) / step))
            for index in range(line_count + 1):
                value = -extent + index * step
                major = abs(value) < 1e-6 or abs(value - round(value)) < 1e-6
                color = major_grid_color if major else grid_color
                self._draw_preview_line(painter, mvp, (value, 0.0, -extent), (value, 0.0, extent), color)
                self._draw_preview_line(painter, mvp, (-extent, 0.0, value), (extent, 0.0, value), color)

        axis_extent = 2.85
        self._draw_preview_line(painter, mvp, (-axis_extent, 0.0, 0.0), (axis_extent, 0.0, 0.0), QColor(239, 68, 68, 145), width=1.35)
        self._draw_preview_line(painter, mvp, (0.0, -axis_extent, 0.0), (0.0, axis_extent, 0.0), QColor(59, 130, 246, 145), width=1.35)
        self._draw_preview_line(painter, mvp, (0.0, 0.0, -axis_extent), (0.0, 0.0, axis_extent), QColor(34, 197, 94, 145), width=1.35)
        for label, point, color in (
            ("X", (axis_extent, 0.0, 0.0), QColor(239, 68, 68, 150)),
            ("Y", (0.0, axis_extent, 0.0), QColor(59, 130, 246, 150)),
            ("Z", (0.0, 0.0, axis_extent), QColor(34, 197, 94, 150)),
        ):
            label_point = self._project_preview_point(mvp, point)
            if label_point is not None:
                painter.setPen(color)
                painter.drawText(QRect(int(label_point.x()) + 4, int(label_point.y()) + 4, 18, 18), Qt.AlignLeft, label)

        if self._show_origin_overlay:
            origin = self._project_preview_point(mvp, (0.0, 0.0, 0.0))
            if origin is not None:
                painter.setPen(QPen(QColor(255, 255, 255, 170), 1.2))
                radius = 6.0
                painter.drawEllipse(origin, radius, radius)
                painter.drawLine(QPointF(origin.x() - 8.0, origin.y()), QPointF(origin.x() + 8.0, origin.y()))
                painter.drawLine(QPointF(origin.x(), origin.y() - 8.0), QPointF(origin.x(), origin.y() + 8.0))
                painter.setPen(QColor(226, 232, 240, 120))
                painter.drawText(QRect(int(origin.x()) + 10, int(origin.y()) + 8, 80, 18), Qt.AlignLeft, "origin")
            center = QPointF(float(self.width()) * 0.5, float(self.height()) * 0.5)
            painter.setPen(QPen(QColor(255, 255, 255, 55), 0.8))
            painter.drawLine(QPointF(center.x() - 12.0, center.y()), QPointF(center.x() + 12.0, center.y()))
            painter.drawLine(QPointF(center.x(), center.y() - 12.0), QPointF(center.x(), center.y() + 12.0))
            painter.setPen(QPen(QColor(255, 255, 255, 105), 1.0))
            painter.drawEllipse(center, 2.0, 2.0)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._draw_alignment_guides(painter)
        painter.setPen(self._overlay_text_color)
        if self._vertex_count <= 0:
            painter.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self._message)
        else:
            painter.drawText(
                QRect(12, 10, max(120, self.width() - 24), 22),
                Qt.AlignLeft | Qt.AlignVCenter,
                "Drag: orbit | Middle/Right-drag or Shift+Drag: pan | Wheel: zoom | Double-click: reset",
            )
            if self._debug_overlay_lines:
                metrics = painter.fontMetrics()
                painter.setPen(self._overlay_text_color)
                overlay_text = metrics.elidedText(
                    self._debug_overlay_lines[0],
                    Qt.ElideRight,
                    max(120, self.width() - 24),
                )
                painter.drawText(
                    QRect(12, 34, max(120, self.width() - 24), 20),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    overlay_text,
                )
        painter.end()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        pan_requested = (
            event.button() == Qt.MiddleButton
            or event.button() == Qt.RightButton
            or (event.button() == Qt.LeftButton and bool(event.modifiers() & Qt.ShiftModifier))
        )
        if self._vertex_count > 0 and pan_requested:
            self._pan_drag_active = True
            self._pan_drag_button = event.button()
            self._last_mouse_pos = event.position()
            self._last_global_mouse_pos = event.globalPosition().toPoint()
            self.setCursor(Qt.SizeAllCursor)
            self.grabMouse()
            self._pan_poll_timer.start()
            event.accept()
            return
        if self._vertex_count > 0 and event.button() == Qt.LeftButton:
            self._drag_active = True
            self._last_mouse_pos = event.position()
            self._last_global_mouse_pos = event.globalPosition().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_active:
            current_pos = event.position()
            delta = current_pos - self._last_mouse_pos
            self._last_mouse_pos = current_pos
            # Orbit should feel like dragging the model rather than steering a
            # camera rig, so both axes follow the more common DCC-style signs.
            settings = self.render_settings()
            orbit_sign_x = -1.0 if settings.invert_orbit_x else 1.0
            orbit_sign_y = -1.0 if settings.invert_orbit_y else 1.0
            orbit_scale = float(settings.orbit_sensitivity)
            self._yaw += delta.x() * orbit_scale * orbit_sign_x
            self._pitch = min(max(self._pitch + delta.y() * orbit_scale * orbit_sign_y, -89.0), 89.0)
            self.update()
            event.accept()
            return
        if self._pan_drag_active:
            current_pos = event.position()
            delta = current_pos - self._last_mouse_pos
            self._last_mouse_pos = current_pos
            self._last_global_mouse_pos = event.globalPosition().toPoint()
            self._apply_pan_delta(delta.x(), delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_active and event.button() == Qt.LeftButton:
            self._drag_active = False
            self.releaseMouse()
            self.unsetCursor()
            event.accept()
            return
        if self._pan_drag_active and event.button() == self._pan_drag_button:
            self._pan_drag_active = False
            self._pan_drag_button = Qt.NoButton
            self._pan_poll_timer.stop()
            self.releaseMouse()
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self._vertex_count > 0 and event.button() == Qt.LeftButton:
            self._yaw = self._DEFAULT_YAW
            self._pitch = self._DEFAULT_PITCH
            self._fit_to_view = True
            self._zoom_factor = 1.0
            self._distance = self._FIT_DISTANCE
            self._pan_drag_button = Qt.NoButton
            self._pan_poll_timer.stop()
            self._pan_offset = QVector3D(0.0, 0.0, 0.0)
            self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if self._vertex_count <= 0 or event.angleDelta().y() == 0:
            super().wheelEvent(event)
            return
        step = 1 if event.angleDelta().y() > 0 else -1
        current_zoom = self.current_display_scale() if self._fit_to_view else self._zoom_factor
        closest_index = min(
            range(len(self._ZOOM_STEPS)),
            key=lambda index: abs(self._ZOOM_STEPS[index] - current_zoom),
        )
        next_index = min(max(closest_index + step, 0), len(self._ZOOM_STEPS) - 1)
        self._fit_to_view = False
        self._zoom_factor = self._ZOOM_STEPS[next_index]
        self._distance = self._FIT_DISTANCE / self._zoom_factor
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()
        event.accept()

    def _upload_geometry(self) -> None:
        if not self._gl_ready or self.context() is None or self._program is None:
            return
        self.makeCurrent()
        self._clear_gl_textures()
        self._program.bind()
        self._vertex_array.bind()
        self._vertex_buffer.bind()
        self._vertex_buffer.allocate(self._vertex_blob, len(self._vertex_blob))
        stride = 17 * 4
        self._program.enableAttributeArray(0)
        self._program.setAttributeBuffer(0, _GL_FLOAT, 0, 3, stride)
        self._program.enableAttributeArray(1)
        self._program.setAttributeBuffer(1, _GL_FLOAT, 3 * 4, 3, stride)
        self._program.enableAttributeArray(2)
        self._program.setAttributeBuffer(2, _GL_FLOAT, 6 * 4, 3, stride)
        self._program.enableAttributeArray(3)
        self._program.setAttributeBuffer(3, _GL_FLOAT, 9 * 4, 2, stride)
        self._program.enableAttributeArray(4)
        self._program.setAttributeBuffer(4, _GL_FLOAT, 11 * 4, 3, stride)
        self._program.enableAttributeArray(5)
        self._program.setAttributeBuffer(5, _GL_FLOAT, 14 * 4, 3, stride)
        self._vertex_buffer.release()
        self._vertex_array.release()
        self._program.release()
        self._rebuild_gl_textures()
        self.doneCurrent()

    @staticmethod
    def _orthogonal_tangent_frame(normal: Tuple[float, float, float]) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        nx, ny, nz = normal
        if abs(nz) < 0.999:
            tangent = (0.0, 0.0, 1.0)
        else:
            tangent = (1.0, 0.0, 0.0)
        tx = tangent[1] * nz - tangent[2] * ny
        ty = tangent[2] * nx - tangent[0] * nz
        tz = tangent[0] * ny - tangent[1] * nx
        tangent_length = max((tx * tx + ty * ty + tz * tz) ** 0.5, 1e-6)
        tx /= tangent_length
        ty /= tangent_length
        tz /= tangent_length
        bx = ny * tz - nz * ty
        by = nz * tx - nx * tz
        bz = nx * ty - ny * tx
        bitangent_length = max((bx * bx + by * by + bz * bz) ** 0.5, 1e-6)
        bx /= bitangent_length
        by /= bitangent_length
        bz /= bitangent_length
        return (tx, ty, tz), (bx, by, bz)

    @classmethod
    def _build_tangent_frames(
        cls,
        positions: Sequence[Tuple[float, float, float]],
        texture_coordinates: Sequence[Tuple[float, float]],
        normals: Sequence[Tuple[float, float, float]],
        indices: Sequence[int],
    ) -> Tuple[List[Tuple[float, float, float]], List[Tuple[float, float, float]]]:
        vertex_count = len(positions)
        if (
            vertex_count <= 0
            or len(texture_coordinates) != vertex_count
            or len(normals) != vertex_count
        ):
            tangents: List[Tuple[float, float, float]] = []
            bitangents: List[Tuple[float, float, float]] = []
            for normal in normals or [(0.0, 0.0, 1.0)] * max(vertex_count, 1):
                tangent, bitangent = cls._orthogonal_tangent_frame(normal)
                tangents.append(tangent)
                bitangents.append(bitangent)
            return tangents[:vertex_count], bitangents[:vertex_count]

        tangent_accum = [[0.0, 0.0, 0.0] for _ in range(vertex_count)]
        bitangent_accum = [[0.0, 0.0, 0.0] for _ in range(vertex_count)]
        for triangle_index in range(0, len(indices) - 2, 3):
            a = indices[triangle_index]
            b = indices[triangle_index + 1]
            c = indices[triangle_index + 2]
            if (
                a < 0
                or b < 0
                or c < 0
                or a >= vertex_count
                or b >= vertex_count
                or c >= vertex_count
            ):
                continue
            p0 = positions[a]
            p1 = positions[b]
            p2 = positions[c]
            uv0 = texture_coordinates[a]
            uv1 = texture_coordinates[b]
            uv2 = texture_coordinates[c]
            edge1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            edge2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
            delta_uv1 = (uv1[0] - uv0[0], uv1[1] - uv0[1])
            delta_uv2 = (uv2[0] - uv0[0], uv2[1] - uv0[1])
            determinant = (delta_uv1[0] * delta_uv2[1]) - (delta_uv1[1] * delta_uv2[0])
            if abs(determinant) <= 1e-8:
                continue
            reciprocal = 1.0 / determinant
            tangent = (
                reciprocal * ((delta_uv2[1] * edge1[0]) - (delta_uv1[1] * edge2[0])),
                reciprocal * ((delta_uv2[1] * edge1[1]) - (delta_uv1[1] * edge2[1])),
                reciprocal * ((delta_uv2[1] * edge1[2]) - (delta_uv1[1] * edge2[2])),
            )
            bitangent = (
                reciprocal * ((-delta_uv2[0] * edge1[0]) + (delta_uv1[0] * edge2[0])),
                reciprocal * ((-delta_uv2[0] * edge1[1]) + (delta_uv1[0] * edge2[1])),
                reciprocal * ((-delta_uv2[0] * edge1[2]) + (delta_uv1[0] * edge2[2])),
            )
            for vertex_index in (a, b, c):
                tangent_accum[vertex_index][0] += tangent[0]
                tangent_accum[vertex_index][1] += tangent[1]
                tangent_accum[vertex_index][2] += tangent[2]
                bitangent_accum[vertex_index][0] += bitangent[0]
                bitangent_accum[vertex_index][1] += bitangent[1]
                bitangent_accum[vertex_index][2] += bitangent[2]

        tangents = []
        bitangents = []
        for vertex_index in range(vertex_count):
            nx, ny, nz = normals[vertex_index]
            tx, ty, tz = tangent_accum[vertex_index]
            tangent_length = (tx * tx + ty * ty + tz * tz) ** 0.5
            if tangent_length <= 1e-6:
                tangent, bitangent = cls._orthogonal_tangent_frame(normals[vertex_index])
                tangents.append(tangent)
                bitangents.append(bitangent)
                continue
            tx /= tangent_length
            ty /= tangent_length
            tz /= tangent_length
            normal_dot_tangent = (nx * tx) + (ny * ty) + (nz * tz)
            tx -= nx * normal_dot_tangent
            ty -= ny * normal_dot_tangent
            tz -= nz * normal_dot_tangent
            tangent_length = max((tx * tx + ty * ty + tz * tz) ** 0.5, 1e-6)
            tx /= tangent_length
            ty /= tangent_length
            tz /= tangent_length
            bx, by, bz = bitangent_accum[vertex_index]
            if (bx * bx + by * by + bz * bz) <= 1e-6:
                bx = (ny * tz) - (nz * ty)
                by = (nz * tx) - (nx * tz)
                bz = (nx * ty) - (ny * tx)
            bitangent_length = max((bx * bx + by * by + bz * bz) ** 0.5, 1e-6)
            bx /= bitangent_length
            by /= bitangent_length
            bz /= bitangent_length
            tangents.append((tx, ty, tz))
            bitangents.append((bx, by, bz))
        return tangents, bitangents

    @classmethod
    def _build_vertex_blob(cls, model) -> Tuple[bytes, int, List[_ModelPreviewDrawBatch]]:
        meshes = getattr(model, "meshes", None)
        if not meshes:
            return b"", 0, []
        vertex_data = array("f")
        vertex_count = 0
        batches: List[_ModelPreviewDrawBatch] = []
        for mesh_index, mesh in enumerate(meshes):
            positions = list(getattr(mesh, "positions", []) or [])
            normals = list(getattr(mesh, "normals", []) or [])
            indices = list(getattr(mesh, "indices", []) or [])
            if not positions or not indices:
                continue
            if len(normals) != len(positions):
                normals = [(0.0, 0.0, 1.0)] * len(positions)
            texture_coordinates = list(getattr(mesh, "texture_coordinates", []) or [])
            has_texture_coordinates = len(texture_coordinates) == len(positions)
            tangents, bitangents = cls._build_tangent_frames(
                positions,
                texture_coordinates,
                normals,
                indices,
            )
            texture_wrap_repeat = False
            if has_texture_coordinates:
                us = [uv[0] for uv in texture_coordinates]
                vs = [uv[1] for uv in texture_coordinates]
                texture_wrap_repeat = (
                    min(us) < -0.05
                    or max(us) > 1.05
                    or min(vs) < -0.05
                    or max(vs) > 1.05
                )
            preview_color = tuple(getattr(mesh, "preview_color", ()) or ())
            if len(preview_color) >= 3:
                color = (
                    max(0.0, min(1.0, float(preview_color[0]))),
                    max(0.0, min(1.0, float(preview_color[1]))),
                    max(0.0, min(1.0, float(preview_color[2]))),
                )
            else:
                color = cls._PALETTE[mesh_index % len(cls._PALETTE)]
            batch_first_vertex = vertex_count
            for triangle_index in range(0, len(indices) - 2, 3):
                a = indices[triangle_index]
                b = indices[triangle_index + 1]
                c = indices[triangle_index + 2]
                if (
                    a < 0
                    or b < 0
                    or c < 0
                    or a >= len(positions)
                    or b >= len(positions)
                    or c >= len(positions)
                ):
                    continue
                for vertex_index in (a, b, c):
                    px, py, pz = positions[vertex_index]
                    nx, ny, nz = normals[vertex_index]
                    if has_texture_coordinates:
                        tu, tv = texture_coordinates[vertex_index]
                    else:
                        tu, tv = 0.0, 0.0
                    tx, ty, tz = tangents[vertex_index] if vertex_index < len(tangents) else (1.0, 0.0, 0.0)
                    bx, by, bz = bitangents[vertex_index] if vertex_index < len(bitangents) else (0.0, 1.0, 0.0)
                    vertex_data.extend(
                        (
                            px,
                            py,
                            pz,
                            nx,
                            ny,
                            nz,
                            color[0],
                            color[1],
                            color[2],
                            tu,
                            tv,
                            tx,
                            ty,
                            tz,
                            bx,
                            by,
                            bz,
                        )
                    )
                vertex_count += 3
            batch_vertex_count = vertex_count - batch_first_vertex
            if batch_vertex_count <= 0:
                continue
            texture_key = str(getattr(mesh, "preview_texture_path", "") or "").strip()
            if not texture_key and getattr(mesh, "preview_texture_image", None) is not None:
                texture_key = f"in_memory:{mesh_index}"
            normal_texture_key = str(getattr(mesh, "preview_normal_texture_path", "") or "").strip()
            if not normal_texture_key and getattr(mesh, "preview_normal_texture_image", None) is not None:
                normal_texture_key = f"in_memory_normal:{mesh_index}"
            material_texture_key = str(getattr(mesh, "preview_material_texture_path", "") or "").strip()
            if not material_texture_key and getattr(mesh, "preview_material_texture_image", None) is not None:
                material_texture_key = f"in_memory_material:{mesh_index}"
            height_texture_key = str(getattr(mesh, "preview_height_texture_path", "") or "").strip()
            if not height_texture_key and getattr(mesh, "preview_height_texture_image", None) is not None:
                height_texture_key = f"in_memory_height:{mesh_index}"
            texture_flip_vertical = cls._should_flip_texture_vertically(mesh)
            if bool(getattr(mesh, "preview_debug_flip_base_v", False)):
                texture_flip_vertical = not texture_flip_vertical
            material_texture_type = str(getattr(mesh, "preview_material_texture_type", "") or "").strip().lower()
            material_texture_subtype = str(getattr(mesh, "preview_material_texture_subtype", "") or "").strip().lower()
            material_texture_packed_channels = tuple(
                str(channel or "").strip().lower()
                for channel in (getattr(mesh, "preview_material_texture_packed_channels", ()) or ())
                if str(channel or "").strip()
            )
            batches.append(
                _ModelPreviewDrawBatch(
                    first_vertex=batch_first_vertex,
                    vertex_count=batch_vertex_count,
                    texture_key=texture_key,
                    normal_texture_key=normal_texture_key,
                    normal_texture_strength=float(getattr(mesh, "preview_normal_texture_strength", 0.0) or 0.0),
                    material_texture_key=material_texture_key,
                    material_texture_type=material_texture_type,
                    material_texture_subtype=material_texture_subtype,
                    material_texture_packed_channels=material_texture_packed_channels,
                    material_decode_mode=cls._material_decode_mode_for_semantics(
                        material_texture_type,
                        material_texture_subtype,
                        material_texture_packed_channels,
                    ),
                    height_texture_key=height_texture_key,
                    support_maps_disabled=bool(getattr(mesh, "preview_debug_disable_support_maps", False)),
                    has_texture_coordinates=has_texture_coordinates,
                    texture_wrap_repeat=texture_wrap_repeat,
                    texture_flip_vertical=texture_flip_vertical,
                )
            )
        return vertex_data.tobytes(), vertex_count, batches

    @staticmethod
    def _sample_texture_orientation_metrics(
        texture_image: QImage,
        texture_coordinates: Sequence[Tuple[float, float]],
        *,
        flip_vertical: bool,
        max_samples: int = 384,
    ) -> Tuple[int, int, int, int]:
        if texture_image.isNull() or not texture_coordinates:
            return 0, 0, 0, 0
        image = texture_image
        if image.format() != QImage.Format_RGBA8888:
            image = image.convertToFormat(QImage.Format_RGBA8888)
        width = image.width()
        height = image.height()
        if width <= 0 or height <= 0:
            return 0, 0, 0, 0
        total_coordinates = len(texture_coordinates)
        sample_step = max(1, total_coordinates // max_samples)
        opaque_black = 0
        transparent = 0
        colored = 0
        total = 0
        for index in range(0, total_coordinates, sample_step):
            try:
                u, v = texture_coordinates[index]
                uu = max(0.0, min(1.0, float(u)))
                vv = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
            if flip_vertical:
                vv = 1.0 - vv
            x = min(width - 1, max(0, int(round(uu * (width - 1)))))
            y = min(height - 1, max(0, int(round(vv * (height - 1)))))
            color = image.pixelColor(x, y)
            alpha = color.alpha()
            total += 1
            if alpha <= 8:
                transparent += 1
                continue
            if color.red() <= 12 and color.green() <= 12 and color.blue() <= 12:
                opaque_black += 1
                continue
            colored += 1
        return opaque_black, transparent, colored, total

    @classmethod
    def _should_flip_texture_vertically(cls, mesh) -> bool:
        flip_override = getattr(mesh, "preview_texture_flip_vertical", None)
        if flip_override is not None:
            return bool(flip_override)
        texture_image = getattr(mesh, "preview_texture_image", None)
        if not isinstance(texture_image, QImage) or texture_image.isNull():
            texture_image = cls._load_gl_texture_image(str(getattr(mesh, "preview_texture_path", "") or "").strip())
        if not isinstance(texture_image, QImage) or texture_image.isNull():
            return True
        texture_coordinates = list(getattr(mesh, "texture_coordinates", []) or [])
        positions = list(getattr(mesh, "positions", []) or [])
        if not texture_coordinates or len(texture_coordinates) != len(positions):
            return True
        flipped_black, flipped_transparent, flipped_colored, flipped_total = cls._sample_texture_orientation_metrics(
            texture_image,
            texture_coordinates,
            flip_vertical=True,
        )
        unflipped_black, unflipped_transparent, unflipped_colored, unflipped_total = cls._sample_texture_orientation_metrics(
            texture_image,
            texture_coordinates,
            flip_vertical=False,
        )
        if flipped_total <= 0 or unflipped_total <= 0:
            return True
        black_improvement = flipped_black - unflipped_black
        meaningful_black_delta = max(24, flipped_total // 10)
        if black_improvement >= meaningful_black_delta and unflipped_colored >= flipped_colored:
            return False
        transparent_improvement = flipped_transparent - unflipped_transparent
        meaningful_transparent_delta = max(48, flipped_total // 6)
        if (
            flipped_black == 0
            and unflipped_black == 0
            and transparent_improvement >= meaningful_transparent_delta
            and unflipped_colored >= flipped_colored
        ):
            return False
        return True

    def _clear_gl_textures(self) -> None:
        for texture in self._texture_objects.values():
            try:
                texture.destroy()
            except Exception:
                continue
        self._texture_objects.clear()

    def _prepare_gl_texture_image(self, texture_image: QImage) -> QImage:
        prepared = texture_image.convertToFormat(QImage.Format_RGBA8888)
        if prepared.isNull():
            return prepared
        longest_edge = max(prepared.width(), prepared.height())
        settings = self.render_settings()
        if self._high_quality_textures:
            max_dimension = int(settings.preview_texture_max_dimension)
            if longest_edge <= 0 or max_dimension <= 0 or longest_edge <= max_dimension:
                return prepared
            target_longest_edge = max_dimension
        else:
            max_dimension = int(settings.low_quality_texture_max_dimension)
            if longest_edge <= 0 or max_dimension <= 0:
                return prepared
            target_longest_edge = min(max_dimension, max(256, longest_edge // 2))
        if longest_edge <= 0 or max_dimension <= 0:
            return prepared
        if target_longest_edge >= longest_edge:
            return prepared
        target_size = prepared.size().scaled(target_longest_edge, target_longest_edge, Qt.KeepAspectRatio)
        if target_size.width() <= 0 or target_size.height() <= 0:
            return prepared
        return prepared.scaled(target_size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    @staticmethod
    def _load_gl_texture_image(texture_key: str) -> Optional[QImage]:
        normalized_key = str(texture_key or "").strip()
        if not normalized_key or normalized_key.lower().startswith("in_memory"):
            return None
        reader = QImageReader(normalized_key)
        image = reader.read()
        if image.isNull():
            return None
        return image

    def _rebuild_gl_textures(self) -> None:
        if not self._gl_ready:
            return
        source_images: Dict[str, QImage] = {}
        current_meshes = getattr(getattr(self, "_current_model", None), "meshes", None)
        if current_meshes:
            for mesh_index, mesh in enumerate(current_meshes):
                texture_slots = (
                    ("preview_texture_path", "preview_texture_image", f"in_memory:{mesh_index}"),
                    ("preview_normal_texture_path", "preview_normal_texture_image", f"in_memory_normal:{mesh_index}"),
                    ("preview_material_texture_path", "preview_material_texture_image", f"in_memory_material:{mesh_index}"),
                    ("preview_height_texture_path", "preview_height_texture_image", f"in_memory_height:{mesh_index}"),
                )
                for path_attr, image_attr, fallback_key in texture_slots:
                    texture_key = str(getattr(mesh, path_attr, "") or "").strip()
                    texture_image = getattr(mesh, image_attr, None)
                    if not texture_key and texture_image is not None:
                        texture_key = fallback_key
                    if not texture_key or texture_key in source_images or texture_image is None:
                        continue
                    if not isinstance(texture_image, QImage) or texture_image.isNull():
                        continue
                    source_images[texture_key] = texture_image
        for batch in self._mesh_batches:
            batch_texture_keys = (
                batch.texture_key,
                batch.normal_texture_key,
                batch.material_texture_key,
                batch.height_texture_key,
            )
            for texture_key in batch_texture_keys:
                if not texture_key:
                    continue
                texture_image = source_images.get(texture_key)
                if texture_image is None:
                    texture_image = self._load_gl_texture_image(texture_key)
                    if texture_image is not None:
                        source_images[texture_key] = texture_image
                if texture_image is None:
                    continue
                cache_key = (texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
                if cache_key in self._texture_objects:
                    continue
                prepared_image = self._prepare_gl_texture_image(texture_image)
                if batch.texture_flip_vertical:
                    prepared_image = prepared_image.mirrored(False, True)
                if prepared_image.isNull():
                    continue
                texture = QOpenGLTexture(prepared_image)
                if self._high_quality_textures:
                    try:
                        texture.generateMipMaps()
                    except Exception:
                        pass
                    texture.setMinMagFilters(QOpenGLTexture.LinearMipMapLinear, QOpenGLTexture.Linear)
                    if hasattr(texture, "setMaximumAnisotropy"):
                        try:
                            texture.setMaximumAnisotropy(float(self.render_settings().max_anisotropy))
                        except Exception:
                            pass
                else:
                    texture.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
                texture.setWrapMode(QOpenGLTexture.Repeat if batch.texture_wrap_repeat else QOpenGLTexture.ClampToEdge)
                self._texture_objects[cache_key] = texture


class PreviewScrollArea(QScrollArea):
    resized = Signal()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.resized.emit()


def _format_media_preview_time(value_ms: int) -> str:
    total_seconds = max(0, int(value_ms // 1000))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


class MediaPreviewWidget(QWidget):
    def __init__(self, message: str, *, theme_key: str):
        super().__init__()
        self._message = message
        self._theme_key = theme_key
        self._media_path = ""
        self._media_kind = ""
        self._ignore_slider_update = False
        self._media_supported = bool(QMediaPlayer is not None and QAudioOutput is not None and QVideoWidget is not None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.info_label = QLabel(message)
        self.info_label.setWordWrap(True)
        self.info_label.setObjectName("HintLabel")
        layout.addWidget(self.info_label)

        if self._media_supported:
            self.video_widget = QVideoWidget()
            self.video_widget.setMinimumHeight(220)
            layout.addWidget(self.video_widget, stretch=1)

            controls_row = QHBoxLayout()
            controls_row.setSpacing(8)
            self.play_button = QPushButton("Play")
            self.stop_button = QPushButton("Stop")
            self.position_slider = QSlider(Qt.Horizontal)
            self.position_slider.setRange(0, 0)
            self.time_label = QLabel("0:00 / 0:00")
            self.time_label.setObjectName("HintLabel")
            controls_row.addWidget(self.play_button)
            controls_row.addWidget(self.stop_button)
            controls_row.addWidget(self.position_slider, stretch=1)
            controls_row.addWidget(self.time_label)
            layout.addLayout(controls_row)

            self.audio_output = QAudioOutput(self)
            self.audio_output.setVolume(1.0)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio_output)
            self.player.setVideoOutput(self.video_widget)
            self.player.positionChanged.connect(self._handle_position_changed)
            self.player.durationChanged.connect(self._handle_duration_changed)
            self.player.playbackStateChanged.connect(self._handle_playback_state_changed)
            self.player.mediaStatusChanged.connect(self._handle_media_status_changed)
            self.player.errorOccurred.connect(self._handle_error)

            self.play_button.clicked.connect(self._toggle_play_pause)
            self.stop_button.clicked.connect(self._stop_playback)
            self.position_slider.sliderPressed.connect(self._handle_slider_pressed)
            self.position_slider.sliderReleased.connect(self._handle_slider_released)
            self.position_slider.sliderMoved.connect(self._handle_slider_moved)
        else:
            self.video_widget = None
            self.play_button = QPushButton("Play")
            self.stop_button = QPushButton("Stop")
            self.position_slider = QSlider(Qt.Horizontal)
            self.time_label = QLabel("0:00 / 0:00")
            self.audio_output = None
            self.player = None

        self.clear_media(message)

    def set_theme(self, theme_key: str) -> None:
        self._theme_key = theme_key

    def clear_media(self, message: str) -> None:
        self._message = message
        self._media_path = ""
        self._media_kind = ""
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.video_widget is not None:
            self.video_widget.setVisible(False)
        self.info_label.setText(message)
        self.play_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.position_slider.setEnabled(False)
        self.position_slider.setRange(0, 0)
        self.position_slider.setValue(0)
        self.time_label.setText("0:00 / 0:00")

    def shutdown(self) -> None:
        self.clear_media(self._message)

    def set_media(self, media_path: str, *, media_kind: str, detail_text: str = "") -> None:
        normalized_path = str(media_path or "").strip()
        normalized_kind = str(media_kind or "").strip().lower()
        if not normalized_path:
            self.clear_media(detail_text or "No media preview available.")
            return

        self._media_path = normalized_path
        self._media_kind = normalized_kind

        if not self._media_supported:
            self.info_label.setText(
                "Qt Multimedia is not available in this build.\n\n"
                + (detail_text or normalized_path)
            )
            self.play_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.position_slider.setEnabled(False)
            return

        self.info_label.setText(detail_text or normalized_path)
        if self.video_widget is not None:
            self.video_widget.setVisible(normalized_kind == "video")
        self.play_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.position_slider.setEnabled(True)
        self.position_slider.setRange(0, 0)
        self.position_slider.setValue(0)
        self.time_label.setText("0:00 / 0:00")
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(normalized_path))
        self.player.play()

    def _toggle_play_pause(self) -> None:
        if self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _stop_playback(self) -> None:
        if self.player is None:
            return
        self.player.stop()

    def _handle_slider_pressed(self) -> None:
        self._ignore_slider_update = True

    def _handle_slider_released(self) -> None:
        if self.player is not None:
            self.player.setPosition(int(self.position_slider.value()))
        self._ignore_slider_update = False

    def _handle_slider_moved(self, value: int) -> None:
        duration = self.position_slider.maximum()
        self.time_label.setText(f"{_format_media_preview_time(value)} / {_format_media_preview_time(duration)}")

    def _handle_position_changed(self, position: int) -> None:
        if not self._ignore_slider_update:
            self.position_slider.setValue(int(position))
        duration = self.position_slider.maximum()
        self.time_label.setText(f"{_format_media_preview_time(position)} / {_format_media_preview_time(duration)}")

    def _handle_duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(0, int(duration)))
        position = self.position_slider.value()
        self.time_label.setText(f"{_format_media_preview_time(position)} / {_format_media_preview_time(duration)}")

    def _handle_playback_state_changed(self, state) -> None:
        if QMediaPlayer is None:
            return
        self.play_button.setText("Pause" if state == QMediaPlayer.PlayingState else "Play")

    def _handle_media_status_changed(self, status) -> None:
        if QMediaPlayer is None:
            return
        if status == QMediaPlayer.EndOfMedia:
            self.play_button.setText("Play")

    def _handle_error(self, _error, error_text: str) -> None:
        message = str(error_text or "").strip() or "The multimedia backend could not open this file."
        if self._media_kind == "audio":
            message += "\n\nSome Wwise `.wem` variants are not supported by the local Qt Multimedia backend."
        self.info_label.setText(message + (f"\n\nSource: {self._media_path}" if self._media_path else ""))


def _theme_is_light(theme_key: str) -> bool:
    theme = get_theme(theme_key)
    color = QColor(theme["window"])
    return color.lightnessF() >= 0.55


class PreviewSyntaxHighlighter(QSyntaxHighlighter):
    CSS_TEXT_EXTENSIONS = {".css"}
    XML_TEXT_EXTENSIONS = {".xml", ".html", ".thtml", ".material", ".shader"}
    JSON_TEXT_EXTENSIONS = {".json", ".yaml", ".yml"}
    INI_TEXT_EXTENSIONS = {".ini", ".cfg"}
    PALOC_TEXT_EXTENSIONS = {".paloc"}
    LUA_TEXT_EXTENSIONS = {".lua"}

    LUA_KEYWORDS = {
        "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
        "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
    }

    def __init__(self, document, theme_key: str):
        super().__init__(document)
        self.language = "plain"
        self.comment_format = QTextCharFormat()
        self.keyword_format = QTextCharFormat()
        self.string_format = QTextCharFormat()
        self.number_format = QTextCharFormat()
        self.tag_format = QTextCharFormat()
        self.attribute_format = QTextCharFormat()
        self.section_format = QTextCharFormat()
        self.key_format = QTextCharFormat()
        self.entity_format = QTextCharFormat()
        self.bracket_format = QTextCharFormat()
        self.set_theme(theme_key)

    def set_theme(self, theme_key: str) -> None:
        light = _theme_is_light(theme_key)

        def make(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            return fmt

        if light:
            self.comment_format = make("#008000", italic=True)
            self.keyword_format = make("#af00db", bold=True)
            self.string_format = make("#a31515")
            self.number_format = make("#098658")
            self.tag_format = make("#0451a5", bold=True)
            self.attribute_format = make("#001080")
            self.section_format = make("#795e26", bold=True)
            self.key_format = make("#001080")
            self.entity_format = make("#795e26")
            self.bracket_format = make("#333333")
        else:
            self.comment_format = make("#6a9955", italic=True)
            self.keyword_format = make("#c586c0", bold=True)
            self.string_format = make("#ce9178")
            self.number_format = make("#b5cea8")
            self.tag_format = make("#569cd6", bold=True)
            self.attribute_format = make("#9cdcfe")
            self.section_format = make("#4ec9b0", bold=True)
            self.key_format = make("#9cdcfe")
            self.entity_format = make("#d7ba7d")
            self.bracket_format = make("#d4d4d4")
        self.rehighlight()

    def set_language_for_extension(self, extension: str) -> None:
        suffix = (extension or "").lower()
        if suffix in self.CSS_TEXT_EXTENSIONS:
            self.language = "css"
        elif suffix in self.XML_TEXT_EXTENSIONS:
            self.language = "xml"
        elif suffix in self.JSON_TEXT_EXTENSIONS:
            self.language = "json"
        elif suffix in self.INI_TEXT_EXTENSIONS or suffix in self.PALOC_TEXT_EXTENSIONS:
            self.language = "ini"
        elif suffix in self.LUA_TEXT_EXTENSIONS:
            self.language = "lua"
        else:
            self.language = "plain"
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if self.language == "css":
            self._highlight_css(text)
        elif self.language == "xml":
            self._highlight_xml(text)
        elif self.language == "json":
            self._highlight_json(text)
        elif self.language == "ini":
            self._highlight_ini(text)
        elif self.language == "lua":
            self._highlight_lua(text)

    def _highlight_xml(self, text: str) -> None:
        self.setCurrentBlockState(0)
        for match in re.finditer(r"</?[\w:.-]+", text):
            self.setFormat(match.start(), match.end() - match.start(), self.tag_format)
        for match in re.finditer(r"</?|/?>", text):
            self.setFormat(match.start(), match.end() - match.start(), self.bracket_format)
        for match in re.finditer(r"\b[\w:.-]+(?=\s*=)", text):
            self.setFormat(match.start(), match.end() - match.start(), self.attribute_format)
        for match in re.finditer(r"\"[^\"\n]*\"|'[^'\n]*'", text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        for match in re.finditer(r"&[#\w]+;", text):
            self.setFormat(match.start(), match.end() - match.start(), self.entity_format)

        start_index = 0 if self.previousBlockState() == 1 else text.find("<!--")
        while start_index >= 0:
            end_index = text.find("-->", start_index)
            if end_index == -1:
                self.setCurrentBlockState(1)
                self.setFormat(start_index, len(text) - start_index, self.comment_format)
                break
            length = end_index - start_index + 3
            self.setFormat(start_index, length, self.comment_format)
            start_index = text.find("<!--", end_index + 3)

    def _highlight_css(self, text: str) -> None:
        self.setCurrentBlockState(0)

        start_index = 0 if self.previousBlockState() == 1 else text.find("/*")
        while start_index >= 0:
            end_index = text.find("*/", start_index + 2)
            if end_index == -1:
                self.setCurrentBlockState(1)
                self.setFormat(start_index, len(text) - start_index, self.comment_format)
                break
            length = end_index - start_index + 2
            self.setFormat(start_index, length, self.comment_format)
            start_index = text.find("/*", end_index + 2)

        selector_match = re.match(r"\s*([^{]+?)(?=\s*\{)", text)
        if selector_match:
            self.setFormat(selector_match.start(1), selector_match.end(1) - selector_match.start(1), self.tag_format)
        for match in re.finditer(r"(?<=\{|;)\s*([-\w]+)(?=\s*:)", text):
            self.setFormat(match.start(1), match.end(1) - match.start(1), self.attribute_format)
        for match in re.finditer(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        for match in re.finditer(r"#[0-9A-Fa-f]{3,8}\b|(?<![\w.])-?\b\d+(?:\.\d+)?(?:px|em|rem|vh|vw|%)?\b", text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

    def _highlight_json(self, text: str) -> None:
        for match in re.finditer(r'"(?:\\.|[^"\\])*"(?=\s*:)', text):
            self.setFormat(match.start(), match.end() - match.start(), self.key_format)
        for match in re.finditer(r'"(?:\\.|[^"\\])*"', text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        for match in re.finditer(r"\b(true|false|null)\b", text):
            self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)
        for match in re.finditer(r"(?<![\w.])-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

    def _highlight_ini(self, text: str) -> None:
        comment_match = re.match(r"\s*[;#].*$", text)
        if comment_match:
            self.setFormat(comment_match.start(), comment_match.end() - comment_match.start(), self.comment_format)
            return
        section_match = re.match(r"\s*\[[^\]]+\]", text)
        if section_match:
            self.setFormat(section_match.start(), section_match.end() - section_match.start(), self.section_format)
            return
        key_match = re.match(r"\s*[^=:#\s][^=:#]*?(?=\s*[=:])", text)
        if key_match:
            self.setFormat(key_match.start(), key_match.end() - key_match.start(), self.key_format)
        for match in re.finditer(r"\"[^\"\n]*\"|'[^'\n]*'", text):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        for match in re.finditer(r"(?<![\w.])-?\b\d+(?:\.\d+)?\b", text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

    def _highlight_lua(self, text: str) -> None:
        comment_match = re.search(r"--.*$", text)
        text_no_comment = text[: comment_match.start()] if comment_match else text
        for match in re.finditer(r"\b(" + "|".join(sorted(self.LUA_KEYWORDS)) + r")\b", text_no_comment):
            self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)
        for match in re.finditer(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", text_no_comment):
            self.setFormat(match.start(), match.end() - match.start(), self.string_format)
        for match in re.finditer(r"(?<![\w.])-?\b\d+(?:\.\d+)?\b", text_no_comment):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)
        if comment_match:
            self.setFormat(comment_match.start(), comment_match.end() - comment_match.start(), self.comment_format)


class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodePreviewEditor"):
        super().__init__(editor)
        self.code_editor = editor

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(self.code_editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        self.code_editor.line_number_area_paint_event(event)


class CodePreviewEditor(QPlainTextEdit):
    def __init__(self, *, theme_key: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme_key = theme_key
        self._match_selections: list[QTextEdit.ExtraSelection] = []
        self._editor_font_size = max(8, self.font().pointSize())
        self.line_number_area = _LineNumberArea(self)
        self.syntax_highlighter = PreviewSyntaxHighlighter(self.document(), theme_key)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Consolas")
        if not font.exactMatch():
            font = QFont("Courier New")
        font.setPointSize(self._editor_font_size)
        self._apply_editor_font(font)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self._apply_combined_selections)
        self.update_line_number_area_width(0)
        self.set_theme(theme_key)

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), self._gutter_background)

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current_block_number = self.textCursor().blockNumber()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                if block_number == current_block_number:
                    painter.setPen(self._line_number_active_color)
                    font = painter.font()
                    font.setBold(True)
                    painter.setFont(font)
                else:
                    painter.setPen(self._line_number_color)
                    font = painter.font()
                    font.setBold(False)
                    painter.setFont(font)
                painter.drawText(
                    0,
                    top,
                    self.line_number_area.width() - 6,
                    self.fontMetrics().height(),
                    Qt.AlignRight | Qt.AlignVCenter,
                    number,
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    def set_match_selections(self, selections: list[QTextEdit.ExtraSelection]) -> None:
        self._match_selections = list(selections)
        self._apply_combined_selections()

    def _apply_combined_selections(self) -> None:
        selections = []
        if not self.isReadOnly():
            super().setExtraSelections(self._match_selections)
            return
        current_line = QTextEdit.ExtraSelection()
        current_line.format.setBackground(self._current_line_color)
        current_line.format.setProperty(QTextFormat.FullWidthSelection, True)
        current_line.cursor = self.textCursor()
        current_line.cursor.clearSelection()
        selections.append(current_line)
        selections.extend(self._match_selections)
        super().setExtraSelections(selections)
        self.line_number_area.update()

    def set_theme(self, theme_key: str) -> None:
        self.theme_key = theme_key
        theme = get_theme(theme_key)
        self._gutter_background = QColor(theme["surface_alt"])
        self._line_number_color = QColor(theme["text_muted"])
        self._line_number_active_color = QColor(theme["accent"])
        self._current_line_color = QColor(theme["accent_soft"])
        self.syntax_highlighter.set_theme(theme_key)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme['preview_bg']}; color: {theme['text']}; border: 1px solid {theme['border_strong']}; border-radius: 4px; selection-background-color: {theme['accent']}; selection-color: #ffffff; }}"
        )
        self.viewport().update()
        self.line_number_area.update()
        self._apply_combined_selections()

    def set_language_for_extension(self, extension: str) -> None:
        self.syntax_highlighter.set_language_for_extension(extension)

    def set_wrap_enabled(self, enabled: bool) -> None:
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if enabled else QPlainTextEdit.NoWrap)

    def adjust_font_size(self, delta: int) -> int:
        self._editor_font_size = max(8, min(22, self._editor_font_size + delta))
        font = self.font()
        font.setPointSize(self._editor_font_size)
        self._apply_editor_font(font)
        return self._editor_font_size

    def set_font_size(self, size: int) -> int:
        self._editor_font_size = max(8, min(22, size))
        font = self.font()
        font.setPointSize(self._editor_font_size)
        self._apply_editor_font(font)
        return self._editor_font_size

    def apply_font_preferences(self, font: QFont, *, preserve_size: bool = False) -> None:
        updated_font = QFont(font)
        if preserve_size:
            updated_font.setPointSize(self._editor_font_size)
        else:
            self._editor_font_size = max(8, min(22, updated_font.pointSize()))
        self._apply_editor_font(updated_font)

    def center_on_span(self, start: int, end: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(max(0, start))
        cursor.setPosition(max(start, end), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()

    def _apply_editor_font(self, font: QFont) -> None:
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.update_line_number_area_width(0)
        self.viewport().update()
        self.line_number_area.update()
        self.syntax_highlighter.rehighlight()


class LogHighlighter(QSyntaxHighlighter):
    _timestamp_re = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]")
    _error_re = re.compile(r"\b(ERROR|Traceback|Exception|FAILED|failure|fatal)\b", re.IGNORECASE)
    _warning_re = re.compile(r"\b(warning|preflight|skip|skipped)\b", re.IGNORECASE)
    _success_re = re.compile(r"\b(complete|completed|finished|ready|successfully|correct)\b", re.IGNORECASE)
    _phase_re = re.compile(r"\bPhase\s+\d+/\d+\b", re.IGNORECASE)
    _windows_path_re = re.compile(r"[A-Za-z]:\\[^\r\n<>|\"*?]+")
    _relative_path_re = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/]){2,}[\w.-]+")
    _progress_re = re.compile(r"\[\d+/\d+\]|\b\d+(?:[.,]\d+)?%")
    _action_re = re.compile(
        r"\b(UPSCALE|BUILD|COPY|DRYRUN|SYNCING|INDEXING|SCANNING|STARTING|RUNNING|LOADING|REFRESHING|EXTRACTING|CONVERTING|VALIDATING|RETRYING|FOUND)\b",
        re.IGNORECASE,
    )
    _backend_re = re.compile(r"\b(Real-ESRGAN NCNN|chaiNNer|texconv(?:\.exe)?)\b", re.IGNORECASE)
    _correction_mode_re = re.compile(
        r"\b(Match Mean Luma|Match Levels|Match Histogram|Source Match Balanced|Source Match Extended|Source Match Experimental)\b",
        re.IGNORECASE,
    )
    _texture_type_re = re.compile(r"\[(color|ui|emissive|impostor|normal|height|vector|roughness|mask|unknown)\]")
    _key_value_re = re.compile(r"\b([a-z_]+)=([^\s,;()]+)", re.IGNORECASE)
    _label_re = re.compile(
        r"\b(scale|tile|preset|model|format|mips|output|png|backend|correction|mean|range|source|providers?|folder|executable|input|root)\b",
        re.IGNORECASE,
    )
    _dimension_re = re.compile(r"\b\d+x\d+\b")
    _number_re = re.compile(r"(?<![\w./\\-])\d+(?:[.,]\d+)?\b")
    _arrow_re = re.compile(r"->")

    def __init__(self, document, theme_key: str):
        super().__init__(document)
        self.current_theme_key = theme_key
        self._bold_enabled = True
        self.timestamp_format = QTextCharFormat()
        self.error_format = QTextCharFormat()
        self.warning_format = QTextCharFormat()
        self.success_format = QTextCharFormat()
        self.phase_format = QTextCharFormat()
        self.path_format = QTextCharFormat()
        self.progress_format = QTextCharFormat()
        self.action_format = QTextCharFormat()
        self.backend_format = QTextCharFormat()
        self.key_format = QTextCharFormat()
        self.value_format = QTextCharFormat()
        self.number_format = QTextCharFormat()
        self.separator_format = QTextCharFormat()
        self.error_line_format = QTextCharFormat()
        self.warning_line_format = QTextCharFormat()
        self.success_line_format = QTextCharFormat()
        self.texture_type_formats: dict[str, QTextCharFormat] = {}
        self.set_theme(theme_key)

    def set_theme(self, theme_key: str) -> None:
        self.current_theme_key = theme_key
        theme = get_theme(theme_key)
        light = _theme_is_light(theme_key)

        def make_format(
            color: str,
            *,
            bold: bool = False,
            italic: bool = False,
            background: Optional[QColor] = None,
        ) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold and self._bold_enabled:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            if background is not None:
                fmt.setBackground(background)
            return fmt

        self.timestamp_format = make_format(theme["text_muted"])
        self.error_format = make_format(theme["error"], bold=True)
        self.warning_format = make_format(theme["warning_text"], bold=True)
        self.success_format = make_format("#098658" if light else "#6a9955", bold=True)
        self.phase_format = make_format(theme["accent"], bold=True)
        self.path_format = make_format(theme["text_strong"], bold=True)
        self.progress_format = make_format(theme["accent"], bold=True)
        self.action_format = make_format("#0451a5" if light else "#569cd6", bold=True)
        self.backend_format = make_format(theme["accent"], bold=True)
        self.key_format = make_format("#795e26" if light else "#d7ba7d", bold=True)
        self.value_format = make_format("#a31515" if light else "#ce9178")
        self.number_format = make_format("#098658" if light else "#b5cea8")
        self.separator_format = make_format(theme["text_muted"], bold=True)

        warning_bg = QColor(theme["warning_bg"])
        warning_bg.setAlpha(70 if light else 48)
        error_bg = QColor(theme["error"])
        error_bg.setAlpha(42 if light else 34)
        success_bg = QColor(theme["accent_soft"])
        success_bg.setAlpha(120 if light else 90)
        self.error_line_format = make_format(theme["text_strong"], background=error_bg)
        self.warning_line_format = make_format(theme["text"], background=warning_bg)
        self.success_line_format = make_format(theme["text"], background=success_bg)

        texture_palette = {
            "color": "#a31515" if light else "#ce9178",
            "ui": "#795e26" if light else "#d7ba7d",
            "emissive": "#b58900" if light else "#ffd166",
            "impostor": "#8a5a00" if light else "#f4a261",
            "normal": "#0451a5" if light else "#569cd6",
            "height": "#098658" if light else "#4ec9b0",
            "vector": "#0b7a75" if light else "#4ec9b0",
            "roughness": "#af00db" if light else "#c586c0",
            "mask": "#7c3aed" if light else "#c586c0",
            "unknown": theme["text_muted"],
        }
        self.texture_type_formats = {
            texture_type: make_format(color, bold=True)
            for texture_type, color in texture_palette.items()
        }
        self.rehighlight()

    def set_bold_enabled(self, enabled: bool) -> None:
        self._bold_enabled = bool(enabled)
        self.set_theme(self.current_theme_key)

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        lowered = text.lower()
        if self._error_re.search(text):
            self.setFormat(0, len(text), self.error_line_format)
        elif self._warning_re.search(text):
            self.setFormat(0, len(text), self.warning_line_format)
        elif "completed successfully" in lowered:
            self.setFormat(0, len(text), self.success_line_format)

        timestamp_match = self._timestamp_re.match(text)
        if timestamp_match:
            self.setFormat(timestamp_match.start(), timestamp_match.end() - timestamp_match.start(), self.timestamp_format)

        for match in self._windows_path_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.path_format)
        for match in self._relative_path_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.path_format)

        for match in self._progress_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.progress_format)

        for match in self._phase_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.phase_format)

        for match in self._backend_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.backend_format)

        for match in self._correction_mode_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.success_format)

        for match in self._key_value_re.finditer(text):
            key_start, key_end = match.span(1)
            value_start, value_end = match.span(2)
            self.setFormat(key_start, key_end - key_start, self.key_format)
            self.setFormat(value_start, value_end - value_start, self.value_format)

        for match in self._label_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.key_format)

        for match in self._dimension_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

        for match in self._number_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

        for match in self._arrow_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.separator_format)

        for match in self._texture_type_re.finditer(text):
            texture_type = match.group(1).lower()
            fmt = self.texture_type_formats.get(texture_type, self.path_format)
            self.setFormat(match.start(), match.end() - match.start(), fmt)

        for match in self._action_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.action_format)

        for match in self._warning_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.warning_format)

        for match in self._error_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.error_format)

        for match in self._success_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.success_format)


class ArchiveDetailsHighlighter(QSyntaxHighlighter):
    _section_re = re.compile(
        r"^(Entry Metadata|Import Summary|Preview / Texture Notes|Preview Diagnostics|Readable Strings|Binary Header Preview)\s*$"
    )
    _label_re = re.compile(r"^([A-Za-z][A-Za-z0-9 /()_-]+:)")
    _warning_re = re.compile(r"\b(warning|failed|missing|truncated|unsupported|fallback|skipped|unavailable|error)\b", re.IGNORECASE)
    _windows_path_re = re.compile(r"[A-Za-z]:\\[^\r\n<>|\"*?]+")
    _relative_path_re = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/]){2,}[\w./\\-]+")
    _number_re = re.compile(r"(?<![\w./\\-])\d[\d,]*(?:\.\d+)?\b")
    _hex_offset_re = re.compile(r"^\s*([0-9A-F]{4})(?=\s)")
    _hex_byte_re = re.compile(r"\b[0-9A-F]{2}\b")

    def __init__(self, document, theme_key: str):
        super().__init__(document)
        self.current_theme_key = theme_key
        self.section_format = QTextCharFormat()
        self.label_format = QTextCharFormat()
        self.path_format = QTextCharFormat()
        self.number_format = QTextCharFormat()
        self.warning_format = QTextCharFormat()
        self.hex_offset_format = QTextCharFormat()
        self.hex_byte_format = QTextCharFormat()
        self.muted_format = QTextCharFormat()
        self.set_theme(theme_key)

    def set_theme(self, theme_key: str) -> None:
        self.current_theme_key = theme_key
        theme = get_theme(theme_key)
        light = _theme_is_light(theme_key)

        def make_format(
            color: str,
            *,
            bold: bool = False,
            italic: bool = False,
        ) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            return fmt

        self.section_format = make_format(theme["accent"], bold=True)
        self.label_format = make_format("#795e26" if light else "#d7ba7d", bold=True)
        self.path_format = make_format(theme["text_strong"], bold=True)
        self.number_format = make_format("#098658" if light else "#b5cea8")
        self.warning_format = make_format(theme["warning_text"], bold=True)
        self.hex_offset_format = make_format("#0451a5" if light else "#569cd6", bold=True)
        self.hex_byte_format = make_format("#ce9178" if light else "#d7ba7d")
        self.muted_format = make_format(theme["text_muted"], italic=True)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if not text.strip():
            return

        section_match = self._section_re.match(text.strip())
        if section_match:
            self.setFormat(0, len(text), self.section_format)
            return

        if text.lstrip().startswith("String scan truncated") or text.lstrip().startswith("No details available."):
            self.setFormat(0, len(text), self.muted_format)
            return

        hex_offset_match = self._hex_offset_re.match(text)
        if hex_offset_match:
            offset_start, offset_end = hex_offset_match.span(1)
            self.setFormat(offset_start, offset_end - offset_start, self.hex_offset_format)
            remainder = text[offset_end:]
            ascii_separator = remainder.find("  ")
            hex_region_end = len(text) if ascii_separator < 0 else offset_end + ascii_separator
            for match in self._hex_byte_re.finditer(text[offset_end:hex_region_end]):
                start = offset_end + match.start()
                self.setFormat(start, match.end() - match.start(), self.hex_byte_format)

        label_match = self._label_re.match(text)
        if label_match:
            start, end = label_match.span(1)
            self.setFormat(start, end - start, self.label_format)

        for match in self._windows_path_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.path_format)
        for match in self._relative_path_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.path_format)
        for match in self._number_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)
        for match in self._warning_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.warning_format)


class ArchiveDetailsEditor(CodePreviewEditor):
    def __init__(self, *, theme_key: str, parent: Optional[QWidget] = None):
        super().__init__(theme_key=theme_key, parent=parent)
        self.syntax_highlighter = ArchiveDetailsHighlighter(self.document(), theme_key)
        self.set_theme(theme_key)

    def set_language_for_extension(self, extension: str) -> None:
        _ = extension


class CollapsibleSection(QWidget):
    toggled = Signal(bool)

    def __init__(self, title: str, *, expanded: bool = False):
        super().__init__()
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(6)

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("SectionToggle")
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle_button.clicked.connect(self.set_expanded)
        outer_layout.addWidget(self.toggle_button)

        self.body_frame = QFrame()
        self.body_frame.setObjectName("SectionBody")
        self.body_layout = QVBoxLayout(self.body_frame)
        self.body_layout.setContentsMargins(12, 10, 12, 12)
        self.body_layout.setSpacing(8)
        outer_layout.addWidget(self.body_frame)

        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.blockSignals(False)
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.body_frame.setVisible(expanded)
        self.toggled.emit(expanded)


_QUICK_START_HTML_ES = """
<h3>Que cubre esta app</h3>
<p><b>Crimson Forge Toolkit</b> es una herramienta de archivos y archivos sueltos para Crimson Desert. Cubre extraccion, investigacion, edicion, reconstruccion DDS, escalado opcional, comparacion y exportacion suelta lista para mods.</p>
<ul>
  <li><b>Explorador de archivos</b>: escanear .pamt/.paz, previsualizar recursos compatibles, filtrar, clasificar y extraer a carpetas sueltas.</li>
  <li><b>Flujo de texturas</b>: escanear DDS sueltos, convertir DDS a PNG si hace falta, escalar opcionalmente, reconstruir DDS, comparar resultados y exportar salida mod-ready.</li>
  <li><b>Editor de texturas</b>: abrir imagenes para edicion visible por capas y enviar la salida plana al flujo de reconstruccion.</li>
  <li><b>Asistente de reemplazo</b>: tomar PNG/DDS editados, asociarlos con el DDS original del juego, reconstruir la salida corregida y preparar carpetas mod-ready.</li>
  <li><b>Investigacion</b>: inspeccionar familias de texturas, clasificaciones desconocidas, referencias, analisis DDS, informes y notas locales.</li>
  <li><b>Busqueda de texto</b>: buscar archivos de texto de archivo o sueltos, como .xml, .json, .cfg y .lua.</li>
  <li><b>Configuracion</b>: guardar tema, densidad, cache, estado de layout, confirmaciones y preferencias de inicio.</li>
</ul>
<h3>Configuracion inicial recomendada</h3>
<ol>
  <li>Abre <b>Configuracion</b>. <b>Configuracion inicial</b> esta a la izquierda y <b>Rutas</b> a la derecha.</li>
  <li>Haz clic en <b>Inicializar espacio</b>.</li>
  <li>Configura <b>texconv.exe</b>. Las vistas DDS, conversion DDS-a-PNG, comparacion y reconstruccion DDS dependen de el.</li>
  <li>Define <b>Raiz DDS original</b>, <b>Raiz PNG</b> y <b>Raiz de salida</b>. Activa staging DDS solo si quieres una carpeta PNG previa al escalado.</li>
  <li>Elige un backend de escalado: desactivado, <b>Real-ESRGAN NCNN</b> directo o <b>chaiNNer</b>.</li>
  <li>Empieza con una politica de texturas segura y deja las reglas automaticas activadas para preservar mapas tecnicos riesgosos.</li>
  <li>Revisa perfiles, reglas y coincidencias antes de ejecutar un lote.</li>
  <li>Usa <b>Vista de politica</b> antes de <b>Iniciar</b> para revisar la accion planeada por textura.</li>
  <li>Ejecuta un subconjunto pequeno primero y revisa el resultado en <b>Comparar</b>.</li>
  <li>Si ya editaste una textura fuera de la app, usa <b>Asistente de reemplazo</b>.</li>
</ol>
<h3>Areas principales</h3>
<ul>
  <li><b>Configuracion / Setup</b>: creacion de workspace, herramientas externas, enlaces de ayuda e importadores.</li>
  <li><b>Configuracion / Rutas</b>: origen, staging, PNG, salida y raices de exportacion mod-ready.</li>
  <li><b>Salida DDS</b>: formato, tamano, mips y staging globales.</li>
  <li><b>Perfiles, reglas y coincidencias</b>: planificacion reutilizable por archivo.</li>
  <li><b>Escalado</b>: backend, politica, controles NCNN y notas.</li>
  <li><b>Comparar</b>: revision lado a lado antes de lotes grandes.</li>
</ul>
<h3>Advertencia sobre texturas tecnicas</h3>
<p>Las texturas visibles de color no son iguales que mapas tecnicos. Altura, desplazamiento, normales, mascaras, vectores y otros DDS sensibles son mas riesgosos al pasar por PNG.</p>
<ul>
  <li>Empieza con un preajuste seguro.</li>
  <li>Manten las reglas automaticas activadas.</li>
  <li>Revisa perfiles y rutas del planificador antes de forzar mapas tecnicos por la ruta PNG visible.</li>
</ul>
<h3>Documentacion</h3>
<p>El menu <b>Documentacion</b> abre un navegador de documentacion con busqueda y temas de flujo, perfiles y rutas del planificador.</p>
"""


_QUICK_START_HTML_DE = """
<h3>Was diese App abdeckt</h3>
<p><b>Crimson Forge Toolkit</b> ist ein Archiv- und Loose-File-Werkzeug fuer Crimson Desert. Es deckt Extraktion, Research, Bearbeitung, DDS-Neuaufbau, optionales Upscaling, Vergleich und mod-fertigen Loose-Export ab.</p>
<ul>
  <li><b>Archiv-Browser</b>: .pamt/.paz scannen, unterstuetzte Assets anzeigen, filtern, klassifizieren und in lose Ordner extrahieren.</li>
  <li><b>Textur-Workflow</b>: lose DDS scannen, DDS bei Bedarf zu PNG konvertieren, optional hochskalieren, DDS neu erstellen, Ergebnisse vergleichen und mod-fertige Ausgabe exportieren.</li>
  <li><b>Textur-Editor</b>: Bilder fuer sichtbare Ebenenbearbeitung oeffnen und die flache Ausgabe zurueck in den Neuaufbau senden.</li>
  <li><b>Ersetzungsassistent</b>: bearbeitete PNG/DDS mit dem Original-DDS abgleichen, korrigierte Ausgabe neu erstellen und mod-fertige Ordner vorbereiten.</li>
  <li><b>Recherche</b>: Texturfamilien, unbekannte Klassifizierungen, Referenzen, DDS-Analyse, Berichte und lokale Notizen pruefen.</li>
  <li><b>Textsuche</b>: Archiv- oder lose Textdateien wie .xml, .json, .cfg und .lua durchsuchen.</li>
  <li><b>Einstellungen</b>: Theme, Dichte, Cache, Layoutstatus, Bestaetigungen und Startpraeferenzen speichern.</li>
</ul>
<h3>Empfohlene Starteinrichtung</h3>
<ol>
  <li>Oeffne <b>Einstellungen</b>. <b>Einrichtung</b> ist links und <b>Pfade</b> rechts.</li>
  <li>Klicke auf <b>Arbeitsbereich einrichten</b>.</li>
  <li>Konfiguriere <b>texconv.exe</b>. DDS-Vorschau, DDS-zu-PNG, Vergleich und DDS-Neuaufbau haengen davon ab.</li>
  <li>Setze <b>Original-DDS-Stamm</b>, <b>PNG-Stamm</b> und <b>Ausgabe-Stamm</b>. Aktiviere DDS-Staging nur fuer einen separaten PNG-Staging-Ordner.</li>
  <li>Waehle ein Upscaling-Backend: deaktiviert, direktes <b>Real-ESRGAN NCNN</b> oder <b>chaiNNer</b>.</li>
  <li>Starte mit einer sicheren Textur-Richtlinie und lasse automatische Regeln aktiv, damit riskante technische Maps erhalten bleiben.</li>
  <li>Pruefe Profile, Regeln und Treffer, bevor du einen Stapellauf startest.</li>
  <li>Nutze <b>Richtlinienvorschau</b> vor <b>Start</b>, um die geplante Aktion pro Textur zu pruefen.</li>
  <li>Fuehre zuerst eine kleine Auswahl aus und pruefe das Ergebnis in <b>Vergleichen</b>.</li>
  <li>Wenn du eine Textur bereits extern bearbeitet hast, nutze den <b>Ersetzungsassistent</b>.</li>
</ol>
<h3>Hauptbereiche</h3>
<ul>
  <li><b>Einstellungen / Einrichtung</b>: Workspace-Erstellung, externe Tools, Hilfelinks und Importhelfer.</li>
  <li><b>Einstellungen / Pfade</b>: Quelle, Staging, PNG, Ausgabe und mod-fertige Exportstaemme.</li>
  <li><b>DDS-Ausgabe</b>: globale Format-, Groessen-, Mip- und Staging-Regeln.</li>
  <li><b>Profile, Regeln und Treffer</b>: wiederverwendbare Planung pro Datei.</li>
  <li><b>Upscaling</b>: Backend, Richtlinie, NCNN-Steuerung und Notizen.</li>
  <li><b>Vergleichen</b>: Seit-an-Seit-Pruefung vor groesseren Laeufen.</li>
</ul>
<h3>Warnung zu technischen Texturen</h3>
<p>Sichtbare Farbtexturen sind nicht dasselbe wie technische Maps. Hoehe, Displacement, Normalen, Masken, Vektoren und andere empfindliche DDS-Dateien sind riskanter, wenn sie ueber PNG laufen.</p>
<ul>
  <li>Starte mit einem sicheren Preset.</li>
  <li>Lasse automatische Regeln aktiv.</li>
  <li>Pruefe Planerprofile und Planerpfade, bevor technische Maps in den sichtbaren PNG-Pfad gezwungen werden.</li>
</ul>
<h3>Dokumentation</h3>
<p>Das Menue <b>Dokumentation</b> oeffnet einen durchsuchbaren Dokumentationsbrowser mit Workflow-Themen, Profilen und Planerpfaden.</p>
"""


class QuickStartDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("Startup Setup")
        self.setMinimumSize(560, 460)
        self.resize(720, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel("Startup setup guide")
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        intro_label = QLabel(
            "Before running archive previews or texture workflows, configure Setup and Paths under Settings. The same controls are used by Texture Workflow, Archive Browser previews, Replace Assistant, and Texture Editor handoffs."
        )
        intro_label.setObjectName("HintLabel")
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setReadOnly(True)
        quick_start_html = (
            """
            <h3>What This App Covers</h3>
            <p><b>Crimson Forge Toolkit</b> is a read-only archive and loose-file workflow tool for Crimson Desert. It is built around extraction, research, editing, DDS rebuild, optional upscaling, comparison, and mod-ready loose export.</p>
            <ul>
              <li><b>Archive Browser</b>: scan <b>.pamt/.paz</b>, preview supported assets, filter, classify, and extract to loose folders.</li>
              <li><b>Texture Workflow</b>: scan loose DDS files, convert DDS to PNG when needed, optionally upscale, rebuild DDS, compare results, and export loose mod output.</li>
              <li><b>Texture Editor</b>: open images directly for layered visible-texture editing and send flattened output back into the rebuild flow.</li>
              <li><b>Replace Assistant</b>: take edited PNG/DDS files, match them to the original game DDS, rebuild corrected output, and prepare mod-ready folders.</li>
              <li><b>Research</b>: inspect grouped texture families, unknown classifications, references, DDS analysis, reports, and local notes.</li>
              <li><b>Text Search</b>: search archive or loose text-like files such as <b>.xml</b>, <b>.json</b>, <b>.cfg</b>, and <b>.lua</b>.</li>
              <li><b>Settings</b>: store theme, density, cache behavior, remembered layout state, confirmations, and startup preferences beside the EXE.</li>
            </ul>
            <h3>Recommended Startup Setup</h3>
            <ol>
              <li>Open <b>Settings</b>. <b>Setup</b> is on the left and <b>Paths</b> is on the right.</li>
              <li>Click <b>Init Workspace</b>.</li>
              <li>Configure <b>texconv.exe</b>. DDS preview, DDS-to-PNG conversion, compare previews, and DDS rebuild all depend on it.</li>
              <li>Set <b>Original DDS root</b>, <b>PNG root</b>, and <b>Output root</b>. Enable DDS staging only if you want a separate pre-upscale PNG staging folder.</li>
              <li>Choose an upscaling backend in <b>Upscaling</b>: disabled, direct <b>Real-ESRGAN NCNN</b>, or <b>chaiNNer</b>.</li>
              <li>Keep a safer <b>Texture Policy</b> preset first and leave automatic rules enabled so risky technical DDS files are preserved instead of pushed through the visible PNG path.</li>
              <li>Open <b>Profiles, Rules &amp; Matches</b> and review the starter workflow assignments before running a batch.</li>
              <li>Use <b>Preview Policy</b> before <b>Start</b> if you want to inspect the planned per-texture action.</li>
              <li>Click <b>Scan</b> in the Texture Workflow tab.</li>
              <li>Run a small subset first, then review the output in <b>Compare</b> before trying a larger batch.</li>
              <li>If you already edited a texture outside the app, use <b>Replace Assistant</b> instead of the batch workflow.</li>
              <li>If you want to edit visible textures inside the app, open them in <b>Texture Editor</b> and then send the flattened result back into <b>Replace Assistant</b> or <b>Texture Workflow</b>.</li>
            </ol>
            <h3>Main Workflow Areas</h3>
            <ul>
              <li><b>Settings / Setup</b>: workspace creation, external tools, app links, and optional downloads/import helpers.</li>
              <li><b>Settings / Paths</b>: source, staging, PNG, output, and mod-ready export roots.</li>
              <li><b>DDS Output</b>: global format, size, mip, and staging behavior used unless a workflow profile overrides them.</li>
              <li><b>Profiles, Rules &amp; Matches</b>: reusable per-file workflow profiles, ordered matching rules, and a live matched DDS table.</li>
              <li><b>Upscaling</b>: backend choice, policy preset, direct NCNN controls, and backend-specific notes.</li>
              <li><b>Compare</b>: side-by-side original/output review for the current loose output set.</li>
            </ul>
            <h3>Profiles, Rules &amp; Matches</h3>
            <p>This area controls per-file planning inside Texture Workflow.</p>
            <ul>
              <li><b>Workflow Profiles</b>: reusable named override sets for DDS output and direct NCNN behavior.</li>
              <li><b>Ordered Rules</b>: top-to-bottom match list with last-match-wins behavior. Rules can assign a workflow profile and also override semantic, planner profile, colorspace, alpha policy, and planner path.</li>
              <li><b>Matched Files</b>: live list of files under the current Original DDS root and folder/file filter. You can multi-select rows and create exact-path rules with <b>Assign Profile</b>.</li>
              <li>Starter profiles are meant as sensible baselines, not universal best answers. Technical maps often need preserve-first handling.</li>
            </ul>
            <h3>Backend Choice</h3>
            <p><b>Run Summary</b> gives you a read-only overview of the current sources, backend, texture policy, direct-backend settings, and export behavior before you start.</p>
            <ul>
              <li><b>Disabled</b>: rebuild DDS from existing PNGs or test DDS output settings without upscaling.</li>
              <li><b>Real-ESRGAN NCNN</b>: direct in-app route if you want scale, tile, retry, and optional post correction controlled inside the app.</li>
              <li><b>chaiNNer</b>: use only with a tested chain. The chain remains the source of truth; direct NCNN controls do not override it.</li>
            </ul>
            <h3>Technical Texture Warning</h3>
            <p>Visible color textures are not the same as technical maps. Height, displacement, normals, masks, vectors, and other precision-sensitive DDS files are riskier to push through PNG intermediates.</p>
            <ul>
              <li>Start with a safer preset.</li>
              <li>Keep automatic rules enabled.</li>
              <li>Review planner profiles and planner paths before forcing technical maps through the visible PNG path.</li>
              <li>Source Match correction only applies to direct NCNN runs and only where the app decides it is appropriate.</li>
            </ul>
            <h3>Other App Areas</h3>
            <ul>
              <li><b>Archive Browser</b>: read-only scan, filter, preview, extract, send DDS to workflow, or open matching files in Texture Editor/Research.</li>
              <li><b>Texture Editor</b>: layered visible-texture editing with selections, masks, channels, brushes, gradients, clone/heal/smudge, patch, dodge/burn, and compare handoff.</li>
              <li><b>Replace Assistant</b>: best route for one-off edited replacements and mod-ready folder output.</li>
              <li><b>Research</b>: grouped texture families, DDS QA and metadata, unknown resolver, reports, references, and notes.</li>
              <li><b>Text Search</b>: archive or loose text search with preview and export.</li>
            </ul>
            <h3>Compare and Review</h3>
            <p><b>Compare</b> is the review step before larger runs.</p>
            <ul>
              <li>Use <b>Preview size</b> to scale both panes together.</li>
              <li>Use the mouse wheel while hovering a preview to zoom.</li>
              <li>Drag to pan when a preview is larger than the viewport.</li>
              <li>Use <b>Sync Pan</b> to keep both previews aligned.</li>
            </ul>
            <h3>Common Failure Causes</h3>
            <ul>
              <li><b>Missing texconv</b>: previews, DDS-to-PNG conversion, compare previews, and DDS rebuild all depend on <b>texconv.exe</b>.</li>
              <li><b>Missing NCNN models</b>: the direct NCNN backend needs a working executable plus compatible models.</li>
              <li><b>No matching PNG outputs</b>: if a chain or backend produces no usable PNG output, DDS rebuild has nothing to convert.</li>
              <li><b>Wrong chaiNNer paths</b>: hardcoded chain folders can make chaiNNer read from or write to the wrong place.</li>
              <li><b>Brightness drift</b>: review in <b>Compare</b>, try a different model, or test a Source Match correction mode.</li>
            </ul>
            <h3>Documentation</h3>
            <p>The top-level <b>Documentation</b> menu now opens a searchable in-app documentation browser with deeper workflow topics, including planner profiles and planner paths.</p>
            <h3>Local State</h3>
            <p>The app auto-saves its settings beside the EXE and also stores archive scan cache beside it.</p>
            """
        )
        self.browser.setFont(self.font())
        self.browser.document().setDefaultFont(self.font())
        self.browser.setProperty("_i18n_source_html", quick_start_html)
        self.browser.setProperty("_i18n_html_es", _QUICK_START_HTML_ES)
        self.browser.setProperty("_i18n_html_de", _QUICK_START_HTML_DE)
        self.browser.setHtml(quick_start_html)
        layout.addWidget(self.browser, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.open_setup_button = QPushButton("Open Setup && Paths")
        self.open_chainner_button = QPushButton("Open chaiNNer Setup")
        self.open_docs_button = QPushButton("Open Documentation")
        self.close_button = QPushButton("Close")
        button_row.addWidget(self.open_setup_button)
        button_row.addWidget(self.open_chainner_button)
        button_row.addWidget(self.open_docs_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.open_setup_button.clicked.connect(self._open_setup)
        self.open_chainner_button.clicked.connect(self._open_chainner_setup)
        self.open_docs_button.clicked.connect(self._open_docs)
        self.close_button.clicked.connect(self.accept)

    def _open_setup(self) -> None:
        self.parent_window.focus_quick_start_sections(include_chainner=False)
        self.accept()

    def _open_chainner_setup(self) -> None:
        self.parent_window.focus_quick_start_sections(include_chainner=True)
        self.accept()

    def _open_docs(self) -> None:
        parent_window = self.parent_window
        self.accept()
        if parent_window is not None and hasattr(parent_window, "show_about_dialog"):
            QTimer.singleShot(0, lambda: parent_window.show_about_dialog(topic_id="overview"))


class AboutDialog(QDialog):
    def __init__(
        self,
        parent,
        *,
        title: str,
        intro_html: str,
        sections: Sequence[Dict[str, str]],
        initial_section_id: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(840, 560)
        self.resize(1080, 720)
        self._sections: List[Dict[str, str]] = [dict(section) for section in sections]
        self._filtered_sections: List[Dict[str, str]] = list(self._sections)
        self._initial_section_id = initial_section_id.strip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_font = QFont(self.font())
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_label = QLabel("Search")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search topics, fields, tabs, planner paths, planner profiles...")
        self.topic_count_label = QLabel("")
        self.topic_count_label.setObjectName("HintLabel")
        search_row.addWidget(search_label)
        search_row.addWidget(self.search_edit, stretch=1)
        search_row.addWidget(self.topic_count_label)
        layout.addLayout(search_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        topic_panel = QWidget()
        topic_layout = QVBoxLayout(topic_panel)
        topic_layout.setContentsMargins(0, 0, 0, 0)
        topic_layout.setSpacing(8)
        topic_hint = QLabel("Choose a documentation topic or search by feature name.")
        topic_hint.setObjectName("HintLabel")
        topic_hint.setWordWrap(True)
        topic_layout.addWidget(topic_hint)
        self.topic_list = QListWidget()
        self.topic_list.setAlternatingRowColors(True)
        self.topic_list.setProperty("_i18n_translate_items", True)
        topic_layout.addWidget(self.topic_list, stretch=1)
        splitter.addWidget(topic_panel)

        self.browser = QTextBrowser()
        self.browser.setReadOnly(True)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        document_html = self._build_document_html(title, intro_html)
        self.browser.setFont(self.font())
        self.browser.document().setDefaultFont(self.font())
        self.browser.setProperty("_i18n_source_html", document_html)
        self.browser.setHtml(document_html)
        splitter.addWidget(self.browser)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 760])

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self.search_edit.textChanged.connect(self._refresh_topic_list)
        self.topic_list.currentItemChanged.connect(self._handle_topic_changed)
        self.browser.anchorClicked.connect(self._handle_anchor_clicked)

        self._refresh_topic_list()
        if self._initial_section_id:
            self.select_section(self._initial_section_id)
        elif self.topic_list.count() > 0:
            self.topic_list.setCurrentRow(0)

    def _build_document_html(self, title: str, intro_html: str) -> str:
        section_html: List[str] = []
        for section in self._sections:
            section_id = str(section.get("id", "") or "").strip()
            section_title = str(section.get("title", "") or "").strip()
            section_body = str(section.get("html", "") or "")
            if not section_id or not section_title:
                continue
            section_html.append(
                f"<a name=\"{section_id}\"></a><h2>{section_title}</h2>{section_body}"
            )
        return (
            f"<h3>{title}</h3>{intro_html}"
            "<hr/>"
            + "<hr/>".join(section_html)
        )

    @staticmethod
    def _topic_search_text(section: Dict[str, str]) -> str:
        title = str(section.get("title", "") or "")
        keywords = str(section.get("keywords", "") or "")
        body = str(section.get("html", "") or "")
        plain_body = re.sub(r"<[^>]+>", " ", body)
        return f"{title}\n{keywords}\n{plain_body}".lower()

    def _refresh_topic_list(self) -> None:
        query = self.search_edit.text().strip().lower()
        current_section_id = self.current_section_id()
        self._filtered_sections = [
            section
            for section in self._sections
            if not query or query in self._topic_search_text(section)
        ]
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        for section in self._filtered_sections:
            item = QListWidgetItem(str(section.get("title", "") or "Untitled"))
            item.setData(Qt.UserRole, str(section.get("id", "") or ""))
            summary = str(section.get("summary", "") or "")
            if summary:
                item.setToolTip(summary)
            self.topic_list.addItem(item)
        self.topic_list.blockSignals(False)
        self.topic_count_label.setText(self._format_topic_count(len(self._filtered_sections)))
        if not self._filtered_sections:
            return
        if current_section_id:
            for index in range(self.topic_list.count()):
                item = self.topic_list.item(index)
                if str(item.data(Qt.UserRole) or "") == current_section_id:
                    self.topic_list.setCurrentItem(item)
                    return
        self.topic_list.setCurrentRow(0)

    def _current_language_code(self) -> str:
        parent = self.parent()
        localizer = getattr(parent, "ui_localizer", None)
        return str(getattr(localizer, "language_code", "en") or "en").strip().lower()

    def _format_topic_count(self, count: int) -> str:
        language_code = self._current_language_code()
        if language_code == "es":
            return f"{count} tema" if count == 1 else f"{count} temas"
        if language_code == "de":
            return f"{count} Thema" if count == 1 else f"{count} Themen"
        return f"{count} topic" if count == 1 else f"{count} topics"

    def current_section_id(self) -> str:
        item = self.topic_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.UserRole) or "")

    def select_section(self, section_id: str) -> None:
        target_id = section_id.strip()
        if not target_id:
            return
        for index in range(self.topic_list.count()):
            item = self.topic_list.item(index)
            if str(item.data(Qt.UserRole) or "") == target_id:
                self.topic_list.setCurrentItem(item)
                self._scroll_to_section(target_id)
                return
        self.search_edit.clear()
        for index in range(self.topic_list.count()):
            item = self.topic_list.item(index)
            if str(item.data(Qt.UserRole) or "") == target_id:
                self.topic_list.setCurrentItem(item)
                self._scroll_to_section(target_id)
                return

    def _handle_topic_changed(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        if current is None:
            return
        self._scroll_to_section(str(current.data(Qt.UserRole) or ""))

    def _scroll_to_section(self, section_id: str) -> None:
        if not section_id:
            return
        QTimer.singleShot(0, lambda: self.browser.scrollToAnchor(section_id))

    def _handle_anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)
            return
        target_id = url.fragment().strip()
        if not target_id and url.scheme() == "topic":
            target_id = url.path().strip("/").strip()
        if target_id:
            self.select_section(target_id)
