from __future__ import annotations

from array import array
from ctypes import byref, c_int
from dataclasses import dataclass, fields as dataclass_fields
import math
from pathlib import PurePosixPath
import re
import time
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QImage,
    QImageReader,
    QMatrix4x4,
    QOpenGLFunctions,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QVector2D,
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
    QHeaderView,
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
    QTreeWidget,
    QVBoxLayout,
    QFrame,
    QWidget,
)

from cdmw.models import (
    MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODE_LABELS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS,
    ModelPreviewData,
    ModelPreviewMesh,
    ModelPreviewRenderSettings,
    PreparedModelPreviewBatch,
    PreparedModelPreviewData,
    RunCancelled,
    clamp_model_preview_render_settings,
)
from cdmw.ui.themes import get_theme

_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_DEPTH_BUFFER_BIT = 0x00000100
_GL_DEPTH_TEST = 0x0B71
_GL_CULL_FACE = 0x0B44
_GL_BLEND = 0x0BE2
_GL_SCISSOR_TEST = 0x0C11
_GL_FLOAT = 0x1406
_GL_FALSE = 0
_GL_TRIANGLES = 0x0004
_GL_LESS = 0x0201
_GL_TEXTURE0 = 0x84C0
_GL_MAX_TEXTURE_SIZE = 0x0D33
_GL_NO_ERROR = 0

MODEL_PREVIEW_RENDER_BUILD_ID = "2026-04-29-radical-relief-v7"
PERSISTENT_TREE_COLUMN_WIDTHS_PREFIX = "ui/tree_column_widths"
PERSISTENT_TREE_COLUMN_ORDER_PREFIX = "ui/tree_column_order"


def persistent_tree_column_widths_key(storage_name: str) -> str:
    normalized = str(storage_name or "tree").strip().strip("/")
    return f"{PERSISTENT_TREE_COLUMN_WIDTHS_PREFIX}/{normalized or 'tree'}"


def persistent_tree_column_order_key(storage_name: str) -> str:
    normalized = str(storage_name or "tree").strip().strip("/")
    return f"{PERSISTENT_TREE_COLUMN_ORDER_PREFIX}/{normalized or 'tree'}"


def parse_persistent_tree_column_widths(
    settings: QSettings,
    storage_name: str,
    column_count: int,
    *,
    minimum_width: int = 32,
) -> Tuple[int, ...]:
    if settings is None or column_count <= 0:
        return ()
    raw_value = settings.value(persistent_tree_column_widths_key(storage_name), "")
    if raw_value in (None, ""):
        return ()
    if isinstance(raw_value, str):
        parts = [part.strip() for part in raw_value.split(",") if part.strip()]
    elif isinstance(raw_value, (list, tuple)):
        parts = list(raw_value)
    else:
        return ()
    widths: List[int] = []
    for part in parts:
        try:
            width = int(part)
        except (TypeError, ValueError):
            return ()
        widths.append(max(int(minimum_width), width))
    if len(widths) != int(column_count):
        return ()
    return tuple(widths)


def has_persistent_tree_column_widths(
    settings: QSettings,
    storage_name: str,
    column_count: int,
    *,
    minimum_width: int = 32,
) -> bool:
    return bool(
        parse_persistent_tree_column_widths(
            settings,
            storage_name,
            column_count,
            minimum_width=minimum_width,
        )
    )


def parse_persistent_tree_column_order(
    settings: QSettings,
    storage_name: str,
    column_count: int,
) -> Tuple[int, ...]:
    if settings is None or column_count <= 0:
        return ()
    raw_value = settings.value(persistent_tree_column_order_key(storage_name), "")
    if raw_value in (None, ""):
        return ()
    if isinstance(raw_value, str):
        parts = [part.strip() for part in raw_value.split(",") if part.strip()]
    elif isinstance(raw_value, (list, tuple)):
        parts = list(raw_value)
    else:
        return ()
    order: List[int] = []
    for part in parts:
        try:
            logical_index = int(part)
        except (TypeError, ValueError):
            return ()
        order.append(logical_index)
    if len(order) != int(column_count) or sorted(order) != list(range(int(column_count))):
        return ()
    return tuple(order)


def persist_tree_column_order(
    tree: QTreeWidget,
    settings: QSettings,
    storage_name: str,
) -> None:
    if tree is None or settings is None:
        return
    header = tree.header()
    if header is None:
        return
    order = [str(int(header.logicalIndex(visual_index))) for visual_index in range(header.count())]
    settings.setValue(persistent_tree_column_order_key(storage_name), ",".join(order))


def restore_persistent_tree_column_order(
    tree: QTreeWidget,
    settings: QSettings,
    storage_name: str,
) -> bool:
    if tree is None or settings is None:
        return False
    header = tree.header()
    if header is None:
        return False
    order = parse_persistent_tree_column_order(settings, storage_name, header.count())
    if not order:
        return False
    tree.setUpdatesEnabled(False)
    try:
        for visual_index, logical_index in enumerate(order):
            current_visual = int(header.visualIndex(int(logical_index)))
            if current_visual >= 0 and current_visual != visual_index:
                header.moveSection(current_visual, visual_index)
    finally:
        tree.setUpdatesEnabled(True)
    return True


def persist_tree_column_widths(
    tree: QTreeWidget,
    settings: QSettings,
    storage_name: str,
    *,
    minimum_width: int = 32,
) -> None:
    if tree is None or settings is None:
        return
    header = tree.header()
    if header is None:
        return
    widths = [
        str(max(int(minimum_width), int(header.sectionSize(column))))
        for column in range(header.count())
    ]
    settings.setValue(persistent_tree_column_widths_key(storage_name), ",".join(widths))


def restore_persistent_tree_column_widths(
    tree: QTreeWidget,
    settings: QSettings,
    storage_name: str,
    *,
    minimum_width: int = 32,
) -> bool:
    if tree is None or settings is None:
        return False
    header = tree.header()
    if header is None:
        return False
    widths = parse_persistent_tree_column_widths(
        settings,
        storage_name,
        header.count(),
        minimum_width=minimum_width,
    )
    if not widths:
        return False
    tree.setUpdatesEnabled(False)
    try:
        for column, width in enumerate(widths):
            header.resizeSection(column, int(width))
    finally:
        tree.setUpdatesEnabled(True)
    return True


def make_tree_columns_persistent(
    tree: QTreeWidget,
    settings: QSettings,
    storage_name: str,
    *,
    minimum_width: int = 32,
    save_callback: Optional[Callable[..., None]] = None,
    force_interactive: bool = True,
    restore_later: bool = True,
    persist_order: bool = True,
    sections_movable: bool = True,
) -> None:
    if tree is None or settings is None:
        return
    header = tree.header()
    if header is None:
        return
    header.setSectionsClickable(True)
    header.setSectionsMovable(bool(sections_movable))
    header.setMinimumSectionSize(int(minimum_width))
    if force_interactive:
        header.setStretchLastSection(False)
        for column in range(header.count()):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
    guard_property = "_cdmw_restoring_persistent_tree_columns"
    tree.setProperty(guard_property, True)

    def _restore() -> None:
        tree.setProperty(guard_property, True)
        try:
            if persist_order:
                restore_persistent_tree_column_order(
                    tree,
                    settings,
                    storage_name,
                )
            restore_persistent_tree_column_widths(
                tree,
                settings,
                storage_name,
                minimum_width=minimum_width,
            )
        finally:
            tree.setProperty(guard_property, False)

    def _persist(*_args: object) -> None:
        if bool(tree.property(guard_property)):
            return
        persist_tree_column_widths(
            tree,
            settings,
            storage_name,
            minimum_width=minimum_width,
        )
        if persist_order:
            persist_tree_column_order(
                tree,
                settings,
                storage_name,
            )
        if save_callback is not None:
            save_callback()

    header.sectionResized.connect(_persist)
    if persist_order:
        header.sectionMoved.connect(_persist)
    if restore_later:
        QTimer.singleShot(0, _restore)
    else:
        _restore()

_RENDER_DIAGNOSTIC_MODE_CODES = {
    "lit": 0,
    "rich_lit": 22,
    "white_uniform": 1,
    "shader_marker": 2,
    "fragcoord_checker": 3,
    "vertex_color": 4,
    "normal": 5,
    "uv": 6,
    "cpu_average": 7,
    "base_direct": 8,
    "base_no_tint": 9,
    "base_alpha": 10,
    "normal_raw": 11,
    "material_raw": 12,
    "height_raw": 13,
    "height_calibrated": 23,
    "relief_control_test": 24,
    "sampler_swap_base_on_unit2": 14,
    "sampler_swap_material_on_unit0": 15,
    "base_color": 16,
    "texture_probe": 21,
    "height_depth": 17,
    "material_response": 18,
    "metal_shine": 19,
    "roughness_response": 20,
}

_ALPHA_HANDLING_MODE_CODES = {
    "default": 0,
    "ignore_discard": 1,
    "force_opaque": 2,
    "show_alpha": 3,
}

_DIFFUSE_SWIZZLE_MODE_CODES = {
    "rgba": 0,
    "bgra": 1,
    "rrr": 2,
    "ggg": 3,
    "bbb": 4,
    "aaa": 5,
    "alpha_forced_opaque": 6,
}

_BASE_TEXTURE_QUALITY_CODES = {
    "": 0,
    "resolved_base": 0,
    "low_authority_overlay": 1,
    "material_color_fallback": 2,
}


@dataclass(slots=True)
class _ModelPreviewDrawBatch:
    mesh_index: int
    material_name: str
    texture_name: str
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
    base_texture_quality: str = ""
    texture_brightness: float = 1.0
    texture_tint: Tuple[float, float, float] = ()
    texture_uv_scale: Tuple[float, float] = ()
    source_average_color: Tuple[float, float, float] = ()
    source_average_luma: float = 0.0
    normal_finite_ratio: float = 1.0
    normal_repair_count: int = 0
    tangent_finite_ratio: float = 1.0
    bitangent_finite_ratio: float = 1.0
    uv_finite_ratio: float = 1.0
    smooth_normal_ratio: float = 0.0


@dataclass(slots=True)
class _TextureUploadDiagnostic:
    texture_key: str
    image_loaded: bool = False
    image_width: int = 0
    image_height: int = 0
    prepared_width: int = 0
    prepared_height: int = 0
    gl_max_texture_size: int = 0
    upload_attempted: bool = False
    upload_success: bool = False
    texture_created: bool = False
    texture_id: int = 0
    mipmaps_generated: bool = False
    gl_error: str = ""
    failure_reason: str = ""


@dataclass(slots=True)
class _BatchRenderDiagnostic:
    batch_index: int
    mesh_index: int
    label: str
    texture_key: str = ""
    texture_path_set: bool = False
    image_loaded: bool = False
    image_size: str = "-"
    uv_valid: bool = False
    uv_count: int = 0
    position_count: int = 0
    texture_uploaded: bool = False
    texture_id: int = 0
    normal_texture_id: int = 0
    material_texture_id: int = 0
    height_texture_id: int = 0
    relief_texture_id: int = 0
    diffuse_unit: int = 0
    diffuse_sampler_location: int = -1
    render_mode_code: int = 0
    alpha_handling_mode: str = "default"
    texture_probe_source: str = "base"
    sampler_probe_mode: str = "normal"
    diffuse_swizzle_mode: str = "rgba"
    base_texture_quality: str = ""
    material_decode_mode: int = 0
    rich_material_response: bool = False
    prepared_image_size: str = "-"
    gl_error: str = ""
    alpha_discard_risk: bool = False
    use_texture: bool = False
    use_normal: bool = False
    use_material: bool = False
    use_height: bool = False
    use_relief: bool = False
    normal_uploaded: bool = False
    material_uploaded: bool = False
    height_uploaded: bool = False
    failure_bucket: str = ""
    failure_reason: str = ""
    sampled_luma: Optional[float] = None
    sampled_dark_ratio: Optional[float] = None
    sampled_alpha: Optional[float] = None
    material_sampled_luma: Optional[float] = None
    material_sampled_dark_ratio: Optional[float] = None
    material_sampled_alpha: Optional[float] = None
    height_sampled_luma: Optional[float] = None
    height_sampled_dark_ratio: Optional[float] = None
    height_sampled_alpha: Optional[float] = None
    height_sampled_min_luma: Optional[float] = None
    height_sampled_max_luma: Optional[float] = None
    height_sampled_contrast: Optional[float] = None
    derived_relief_sampled_luma: Optional[float] = None
    derived_relief_sampled_min_luma: Optional[float] = None
    derived_relief_sampled_max_luma: Optional[float] = None
    derived_relief_sampled_contrast: Optional[float] = None
    enhanced_relief_state: str = ""
    enhanced_relief_reason: str = ""
    relief_source: str = ""
    normal_average_strength: Optional[float] = None
    source_average_color: Tuple[float, float, float] = ()
    normal_finite_ratio: float = 1.0
    normal_repair_count: int = 0
    tangent_finite_ratio: float = 1.0
    bitangent_finite_ratio: float = 1.0
    uv_finite_ratio: float = 1.0
    smooth_normal_ratio: float = 0.0
    texture_flip_vertical: bool = True
    texture_wrap_repeat: bool = False
    texture_brightness: float = 1.0
    texture_tint: Tuple[float, float, float] = ()
    texture_uv_scale: Tuple[float, float] = ()
    visibility_guard: str = ""
    final_bucket: str = ""


@dataclass(slots=True)
class _TextureVisibilitySample:
    average_color: Tuple[float, float, float]
    average_luma: float
    dark_ratio: float
    average_alpha: float = 1.0
    alpha_dark_ratio: float = 0.0
    alpha_weighted_luma: float = 0.0
    min_luma: float = 0.0
    max_luma: float = 0.0
    luma_contrast: float = 0.0


@dataclass(slots=True)
class _FramebufferVisibilitySample:
    visible_pixels: int = 0
    average_luma: float = 0.0
    dark_ratio: float = 0.0
    background_ratio: float = 1.0


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


def ui_scale_for(widget: Optional[QWidget] = None) -> float:
    """Return a conservative logical-pixel scale for font/DPI-aware sizing."""
    font = widget.font() if widget is not None else QApplication.font()
    metrics = font.pixelSize()
    if metrics <= 0:
        point_size = font.pointSizeF()
        metrics = point_size if point_size > 0 else 11.0
    return max(0.85, min(1.7, float(metrics) / 11.0))


def scaled_px(value: int, widget: Optional[QWidget] = None) -> int:
    return max(1, int(round(float(value) * ui_scale_for(widget))))


def responsive_sidebar_bounds(widget: Optional[QWidget] = None, *, role: str = "normal") -> Tuple[int, int, int]:
    scale = ui_scale_for(widget)
    if role == "wide":
        values = (380, 500, 680)
    elif role == "workflow":
        values = (440, 640, 840)
    elif role == "tool":
        values = (220, 260, 340)
    elif role == "narrow":
        values = (280, 340, 460)
    else:
        values = (320, 420, 560)
    return tuple(max(1, int(round(value * scale))) for value in values)  # type: ignore[return-value]


def set_sidebar_width_policy(widget: QWidget, *, role: str = "normal") -> None:
    minimum, preferred, maximum = responsive_sidebar_bounds(widget, role=role)
    widget.setMinimumWidth(minimum)
    widget.setMaximumWidth(maximum)
    widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
    widget.resize(preferred, widget.height())


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


class EmptyStatePanel(QWidget):
    """Centered low-noise guidance for empty tables, previews, and idle panes."""

    def __init__(self, title: str, detail: str = "", *, compact: bool = False):
        super().__init__()
        self.setObjectName("EmptyStatePanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        pad_x = scaled_px(18 if compact else 28, self)
        pad_y = scaled_px(16 if compact else 24, self)
        layout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        layout.setSpacing(scaled_px(6, self))
        layout.addStretch(1)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("EmptyStateTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("EmptyStateDetail")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(bool(detail))
        layout.addWidget(self.detail_label)
        layout.addStretch(1)

    def set_text(self, title: str, detail: str = "") -> None:
        self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))


class EmptyStateTreeWidget(QTreeWidget):
    """QTreeWidget with quiet placeholder copy when the model has no rows."""

    def __init__(self, title: str = "", detail: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.empty_title = title
        self.empty_detail = detail

    def set_empty_state(self, title: str, detail: str = "") -> None:
        self.empty_title = title
        self.empty_detail = detail
        self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.topLevelItemCount() > 0 or not (self.empty_title or self.empty_detail):
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = self.viewport().rect().adjusted(scaled_px(24, self), scaled_px(24, self), -scaled_px(24, self), -scaled_px(24, self))
        palette = self.palette()
        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(palette.color(QPalette.Text))
        metrics = painter.fontMetrics()
        title_height = metrics.boundingRect(rect, Qt.AlignCenter | Qt.TextWordWrap, self.empty_title).height()
        detail_height = 0
        if self.empty_detail:
            detail_font = QFont(self.font())
            detail_font.setBold(False)
            painter.setFont(detail_font)
            detail_height = painter.fontMetrics().boundingRect(rect, Qt.AlignCenter | Qt.TextWordWrap, self.empty_detail).height()
        gap = scaled_px(8, self) if self.empty_title and self.empty_detail else 0
        total_height = title_height + detail_height + gap
        y = rect.center().y() - total_height // 2
        if self.empty_title:
            title_rect = QRect(rect.left(), y, rect.width(), title_height)
            painter.setFont(title_font)
            painter.setPen(palette.color(QPalette.Text))
            painter.drawText(title_rect, Qt.AlignCenter | Qt.TextWordWrap, self.empty_title)
            y += title_height + gap
        if self.empty_detail:
            detail_rect = QRect(rect.left(), y, rect.width(), detail_height)
            painter.setFont(self.font())
            painter.setPen(palette.color(QPalette.PlaceholderText))
            painter.drawText(detail_rect, Qt.AlignCenter | Qt.TextWordWrap, self.empty_detail)


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
    alignment_translate_requested = Signal(float, float, float)
    alignment_drag_started = Signal()
    alignment_drag_changed = Signal(float, float, float)
    alignment_drag_finished = Signal(float, float, float)
    alignment_rotation_changed = Signal(float, float, float)
    alignment_rotation_finished = Signal(float, float, float)

    _DEFAULT_YAW = -35.0
    _DEFAULT_PITCH = 20.0
    _FIT_DISTANCE = 3.25
    _VERTICAL_FOV_DEGREES = 45.0
    _OVERLAY_CLIP_EPSILON = 1e-5
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
        self._dark_background_enabled = True
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
        self._relief_texture_sampler_uniform_location = -1
        self._use_texture_uniform_location = -1
        self._render_diagnostic_mode_uniform_location = -1
        self._alpha_handling_mode_uniform_location = -1
        self._diffuse_swizzle_mode_uniform_location = -1
        self._disable_tint_uniform_location = -1
        self._disable_brightness_uniform_location = -1
        self._disable_uv_scale_uniform_location = -1
        self._disable_lighting_uniform_location = -1
        self._render_build_marker_uniform_location = -1
        self._base_texture_tint_uniform_location = -1
        self._base_texture_brightness_uniform_location = -1
        self._base_texture_uv_scale_uniform_location = -1
        self._base_texture_average_color_uniform_location = -1
        self._base_texture_average_luma_uniform_location = -1
        self._base_texture_quality_uniform_location = -1
        self._use_high_quality_uniform_location = -1
        self._use_normal_texture_uniform_location = -1
        self._normal_texture_strength_uniform_location = -1
        self._use_material_texture_uniform_location = -1
        self._material_decode_mode_uniform_location = -1
        self._use_height_texture_uniform_location = -1
        self._use_relief_texture_uniform_location = -1
        self._diffuse_wrap_bias_uniform_location = -1
        self._diffuse_light_scale_uniform_location = -1
        self._normal_strength_cap_uniform_location = -1
        self._normal_strength_floor_uniform_location = -1
        self._height_effect_max_uniform_location = -1
        self._height_sample_min_uniform_location = -1
        self._height_sample_max_uniform_location = -1
        self._height_sample_contrast_uniform_location = -1
        self._height_relief_usable_uniform_location = -1
        self._relief_source_code_uniform_location = -1
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
        self._alignment_editing_enabled = False
        self._alignment_drag_axis = ""
        self._alignment_hover_axis = ""
        self._alignment_translation_units_per_pixel = 0.001
        self._alignment_live_translation = QVector3D(0.0, 0.0, 0.0)
        self._alignment_drag_total = QVector3D(0.0, 0.0, 0.0)
        self._alignment_rotation_degrees_per_pixel = 0.35
        self._alignment_rotation_drag_active = False
        self._alignment_rotation_drag_roll = False
        self._alignment_live_rotation = QVector3D(0.0, 0.0, 0.0)
        self._alignment_rotation_drag_total = QVector3D(0.0, 0.0, 0.0)
        self._alignment_editable_mesh_start = 0
        self._alignment_editable_mesh_count = -1
        self._alignment_editable_mesh_indices: Optional[set[int]] = None
        self._current_model = None
        self._mesh_batches: List[_ModelPreviewDrawBatch] = []
        self._texture_objects: Dict[Tuple[str, bool, bool], QOpenGLTexture] = {}
        self._texture_upload_diagnostics: Dict[Tuple[str, bool, bool], _TextureUploadDiagnostic] = {}
        self._batch_render_diagnostics: Dict[int, _BatchRenderDiagnostic] = {}
        self._batch_luma_diagnostics: Dict[int, _TextureVisibilitySample] = {}
        self._batch_material_luma_diagnostics: Dict[int, _TextureVisibilitySample] = {}
        self._batch_height_luma_diagnostics: Dict[int, _TextureVisibilitySample] = {}
        self._batch_derived_relief_luma_diagnostics: Dict[int, _TextureVisibilitySample] = {}
        self._batch_derived_relief_keys: Dict[int, str] = {}
        self._batch_normal_strength_diagnostics: Dict[int, float] = {}
        self._framebuffer_visibility_diagnostic = _FramebufferVisibilitySample()
        self._framebuffer_visibility_sampled_at = 0.0
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
        self._overlay_text_color = QColor(theme["text_muted"])
        self._apply_preview_background()
        self.update()

    def _apply_preview_background(self) -> None:
        if self._dark_background_enabled:
            self._background_color = QColor(get_theme(self._theme_key)["preview_bg"])
        else:
            self._background_color = QColor("#f4f6f8")

    def set_dark_background_enabled(self, enabled: bool) -> None:
        self._dark_background_enabled = bool(enabled)
        self._apply_preview_background()
        self.update()

    def dark_background_enabled(self) -> bool:
        return bool(self._dark_background_enabled)

    def set_alignment_guides_visible(self, visible: bool) -> None:
        self._show_grid_overlay = bool(visible)
        self._show_origin_overlay = bool(visible)
        self.update()

    def set_alignment_editing_enabled(self, enabled: bool) -> None:
        self._alignment_editing_enabled = bool(enabled)
        if not self._alignment_editing_enabled:
            self._alignment_drag_axis = ""
            self._alignment_hover_axis = ""
            self._alignment_rotation_drag_active = False
            self._alignment_rotation_drag_roll = False
            self._alignment_rotation_drag_total = QVector3D(0.0, 0.0, 0.0)
            self._alignment_live_rotation = QVector3D(0.0, 0.0, 0.0)
        self.update()

    def set_alignment_translation_units_per_pixel(self, value: float) -> None:
        try:
            self._alignment_translation_units_per_pixel = max(0.00001, abs(float(value)))
        except Exception:
            self._alignment_translation_units_per_pixel = 0.001

    def set_alignment_rotation_degrees_per_pixel(self, value: float) -> None:
        try:
            self._alignment_rotation_degrees_per_pixel = max(0.001, abs(float(value)))
        except Exception:
            self._alignment_rotation_degrees_per_pixel = 0.35

    def set_alignment_live_translation(self, x: float, y: float, z: float) -> None:
        self._alignment_live_translation = QVector3D(float(x), float(y), float(z))
        self.update()

    def clear_alignment_live_translation(self) -> None:
        self.set_alignment_live_translation(0.0, 0.0, 0.0)

    def set_alignment_live_rotation(self, x: float, y: float, z: float) -> None:
        self._alignment_live_rotation = QVector3D(float(x), float(y), float(z))
        self.update()

    def clear_alignment_live_rotation(self) -> None:
        self.set_alignment_live_rotation(0.0, 0.0, 0.0)

    def set_alignment_editable_mesh_range(self, start: int = 0, count: int = -1) -> None:
        self._alignment_editable_mesh_start = max(0, int(start))
        self._alignment_editable_mesh_count = int(count)
        self._alignment_editable_mesh_indices = None
        self.update()

    def set_alignment_editable_mesh_indices(self, indices: Sequence[int] | None) -> None:
        if indices is None:
            self._alignment_editable_mesh_indices = None
        else:
            editable_indices: set[int] = set()
            for index in indices:
                try:
                    editable_index = int(index)
                except (TypeError, ValueError):
                    continue
                if editable_index >= 0:
                    editable_indices.add(editable_index)
            self._alignment_editable_mesh_indices = editable_indices
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
        self._texture_upload_diagnostics.clear()
        self._batch_render_diagnostics.clear()
        self._batch_luma_diagnostics.clear()
        self._batch_material_luma_diagnostics.clear()
        self._batch_height_luma_diagnostics.clear()
        self._batch_derived_relief_luma_diagnostics.clear()
        self._batch_derived_relief_keys.clear()
        self._batch_normal_strength_diagnostics.clear()
        self._framebuffer_visibility_diagnostic = _FramebufferVisibilitySample()
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
                        mesh_index=len(mesh_batches),
                        material_name=str(batch.material_name or "").strip(),
                        texture_name=str(batch.texture_name or "").strip(),
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
                        base_texture_quality=str(batch.preview_base_texture_quality or "").strip().lower(),
                        texture_brightness=float(batch.preview_texture_brightness or 1.0),
                        texture_tint=tuple(batch.preview_texture_tint or ()),
                        texture_uv_scale=tuple(batch.preview_texture_uv_scale or ()),
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
        if not self._alignment_drag_axis:
            self._alignment_live_translation = QVector3D(0.0, 0.0, 0.0)
        if not self._alignment_rotation_drag_active:
            self._alignment_live_rotation = QVector3D(0.0, 0.0, 0.0)
        self._refresh_debug_overlay_lines()
        self._upload_geometry()
        self.view_state_changed.emit(self._zoom_factor, self._fit_to_view)
        self.update()

    @classmethod
    def prepare_model_preview(
        cls,
        model,
        *,
        stop_event=None,
    ) -> Tuple[object, Optional[PreparedModelPreviewData]]:
        if stop_event is not None and stop_event.is_set():
            raise RunCancelled("Model preview preparation cancelled.")
        cloned_model = cls._clone_model_preview(model)
        if not isinstance(cloned_model, ModelPreviewData):
            return cloned_model, None
        for mesh in getattr(cloned_model, "meshes", None) or []:
            if stop_event is not None and stop_event.is_set():
                raise RunCancelled("Model preview preparation cancelled.")
            if isinstance(mesh, ModelPreviewMesh):
                cls._initialize_mesh_preview_slot_defaults(mesh)
        vertex_blob, vertex_count, mesh_batches = cls._build_vertex_blob(cloned_model)
        prepared_batches: List[PreparedModelPreviewBatch] = []
        floats_per_vertex = 20
        bytes_per_vertex = floats_per_vertex * 4
        for mesh, batch in zip(getattr(cloned_model, "meshes", ()) or (), mesh_batches):
            if stop_event is not None and stop_event.is_set():
                raise RunCancelled("Model preview preparation cancelled.")
            start = int(batch.first_vertex) * bytes_per_vertex
            end = start + (int(batch.vertex_count) * bytes_per_vertex)
            prepared_batches.append(
                PreparedModelPreviewBatch(
                    material_name=str(getattr(mesh, "material_name", "") or "").strip(),
                    texture_name=str(getattr(mesh, "texture_name", "") or "").strip(),
                    vertex_blob=vertex_blob[start:end],
                    index_count=int(batch.vertex_count),
                    preview_texture_path=batch.texture_key,
                    preview_base_texture_quality=batch.base_texture_quality,
                    preview_normal_texture_path=batch.normal_texture_key,
                    preview_material_texture_path=batch.material_texture_key,
                    preview_height_texture_path=batch.height_texture_key,
                    preview_texture_flip_vertical=batch.texture_flip_vertical,
                    preview_texture_brightness=float(batch.texture_brightness or 1.0),
                    preview_texture_tint=tuple(batch.texture_tint or ()),
                    preview_texture_uv_scale=tuple(batch.texture_uv_scale or ()),
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
        previous = bool(self._use_textures)
        self._use_textures = bool(use_textures)
        if (
            previous != self._use_textures
            and self._use_textures
            and self._render_mode_uses_derived_relief(self._render_settings)
            and self._gl_ready
            and self.context() is not None
        ):
            self.makeCurrent()
            self._clear_gl_textures()
            self._rebuild_gl_textures()
            self.doneCurrent()
        self.update()

    def support_maps_available(self) -> bool:
        return any(
            batch.normal_texture_key or batch.material_texture_key or batch.height_texture_key
            for batch in self._mesh_batches
        )

    @staticmethod
    def _support_map_slot_counts_from_batches(
        batches: Sequence[_ModelPreviewDrawBatch],
    ) -> Dict[str, int]:
        counts = {"normal": 0, "material": 0, "height": 0}
        for batch in batches:
            if batch.normal_texture_key:
                counts["normal"] += 1
            if batch.material_texture_key:
                counts["material"] += 1
            if batch.height_texture_key:
                counts["height"] += 1
        return counts

    @staticmethod
    def _support_map_active_counts_from_diagnostics(
        diagnostics: Mapping[int, _BatchRenderDiagnostic],
    ) -> Dict[str, int]:
        counts = {"normal": 0, "material": 0, "height": 0}
        for item in diagnostics.values():
            if item.use_normal:
                counts["normal"] += 1
            if item.use_material:
                counts["material"] += 1
            if item.use_height:
                counts["height"] += 1
        return counts

    @staticmethod
    def _format_support_map_counts(counts: Mapping[str, int]) -> str:
        return (
            f"n:{int(counts.get('normal', 0))} "
            f"m:{int(counts.get('material', 0))} "
            f"h:{int(counts.get('height', 0))}"
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

    @staticmethod
    def _yes_no(value: bool) -> str:
        return "yes" if bool(value) else "no"

    @staticmethod
    def _texture_id(texture: Optional[QOpenGLTexture]) -> int:
        if texture is None:
            return 0
        try:
            return int(texture.textureId())
        except Exception:
            return 0

    @staticmethod
    def _texture_created(texture: Optional[QOpenGLTexture]) -> bool:
        if texture is None:
            return False
        try:
            return bool(texture.isCreated())
        except Exception:
            return False

    def _gl_error_text(self) -> str:
        functions = self._functions
        if functions is None or not hasattr(functions, "glGetError"):
            return ""
        errors: List[str] = []
        for _ in range(8):
            try:
                error_code = int(functions.glGetError())
            except Exception:
                break
            if error_code == _GL_NO_ERROR:
                break
            errors.append(f"0x{error_code:04X}")
        return ",".join(errors)

    def _read_gl_max_texture_size(self) -> int:
        functions = self._functions
        if functions is None or not hasattr(functions, "glGetIntegerv"):
            return 0
        value = c_int(0)
        try:
            functions.glGetIntegerv(_GL_MAX_TEXTURE_SIZE, byref(value))
        except Exception:
            return 0
        return max(0, int(value.value))

    @staticmethod
    def _forced_sampler_unit(settings: ModelPreviewRenderSettings, render_mode: str) -> int:
        if render_mode == "sampler_swap_base_on_unit2":
            return 2
        sampler_probe = str(getattr(settings, "sampler_probe_mode", "normal") or "normal").strip().lower()
        if sampler_probe.startswith("force_unit"):
            try:
                return max(0, min(4, int(sampler_probe.replace("force_unit", ""))))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _diffuse_probe_source_for_render_mode(settings: ModelPreviewRenderSettings, render_mode: str) -> str:
        normalized_mode = str(render_mode or "").strip().lower()
        if normalized_mode in {"base_direct", "base_no_tint", "base_alpha", "base_color", "sampler_swap_base_on_unit2"}:
            return "base"
        if normalized_mode == "texture_probe":
            source = str(getattr(settings, "texture_probe_source", "base") or "base").strip().lower()
            return source if source in {"base", "normal", "material", "height"} else "base"
        if normalized_mode == "sampler_swap_material_on_unit0":
            return "material"
        return "base"

    @staticmethod
    def _diagnostic_texture_for_source(
        source: str,
        *,
        base: Optional[QOpenGLTexture],
        normal: Optional[QOpenGLTexture],
        material: Optional[QOpenGLTexture],
        height: Optional[QOpenGLTexture],
    ) -> Optional[QOpenGLTexture]:
        normalized = str(source or "").strip().lower()
        if normalized == "normal":
            return normal
        if normalized == "material":
            return material
        if normalized == "height":
            return height
        return base

    @staticmethod
    def _release_texture_unit(texture: Optional[QOpenGLTexture], unit: int) -> None:
        if texture is None:
            return
        try:
            texture.release(int(unit))
        except TypeError:
            texture.release()
        except Exception:
            pass

    @staticmethod
    def _format_float_tuple(values: Sequence[object], *, default: str) -> str:
        normalized: List[str] = []
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            normalized.append(f"{number:.2f}".rstrip("0").rstrip("."))
        return "/".join(normalized) if normalized else default

    @staticmethod
    def _batch_label(batch: _ModelPreviewDrawBatch) -> str:
        label = str(batch.material_name or batch.texture_name or "").strip()
        if not label:
            label = str(batch.texture_key or "").strip()
        if not label:
            label = f"mesh {batch.mesh_index}"
        return label

    @staticmethod
    def _summarize_texture_key(texture_key: str) -> str:
        key = str(texture_key or "").strip()
        if not key:
            return "-"
        if key.lower().startswith("in_memory"):
            return key
        path = PurePosixPath(key.replace("\\", "/"))
        return path.name or key

    @staticmethod
    def _sample_base_texture_visibility(
        texture_image: QImage,
        texture_coordinates: Sequence[object],
        *,
        flip_vertical: bool,
        max_samples: int = 384,
    ) -> Optional[_TextureVisibilitySample]:
        if texture_image.isNull() or not texture_coordinates:
            return None
        width = int(texture_image.width())
        height = int(texture_image.height())
        if width <= 0 or height <= 0:
            return None
        count = len(texture_coordinates)
        if count <= 0:
            return None
        step = max(1, count // max(1, int(max_samples)))
        red_total = 0.0
        green_total = 0.0
        blue_total = 0.0
        alpha_total = 0.0
        luma_total = 0.0
        alpha_weighted_luma_total = 0.0
        min_luma = 1.0
        max_luma = 0.0
        dark_count = 0
        alpha_dark_count = 0
        sample_count = 0
        for coord in texture_coordinates[::step]:
            try:
                u = float(coord[0])  # type: ignore[index]
                v = float(coord[1])  # type: ignore[index]
            except Exception:
                continue
            if not math.isfinite(u) or not math.isfinite(v):
                continue
            u = u - math.floor(u)
            v = v - math.floor(v)
            if flip_vertical:
                v = 1.0 - v
            x = max(0, min(width - 1, int(round(u * float(width - 1)))))
            y = max(0, min(height - 1, int(round(v * float(height - 1)))))
            color = texture_image.pixelColor(x, y)
            red = float(color.redF())
            green = float(color.greenF())
            blue = float(color.blueF())
            alpha = float(color.alphaF())
            luma = (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
            red_total += red
            green_total += green
            blue_total += blue
            alpha_total += alpha
            luma_total += luma
            alpha_weighted_luma_total += luma * alpha
            min_luma = min(min_luma, luma)
            max_luma = max(max_luma, luma)
            if luma < 0.035:
                dark_count += 1
            if alpha <= 0.01:
                alpha_dark_count += 1
            sample_count += 1
            if sample_count >= max_samples:
                break
        if sample_count <= 0:
            return None
        divisor = float(sample_count)
        return _TextureVisibilitySample(
            average_color=(red_total / divisor, green_total / divisor, blue_total / divisor),
            average_luma=luma_total / divisor,
            dark_ratio=dark_count / divisor,
            average_alpha=alpha_total / divisor,
            alpha_dark_ratio=alpha_dark_count / divisor,
            alpha_weighted_luma=alpha_weighted_luma_total / divisor,
            min_luma=min_luma,
            max_luma=max_luma,
            luma_contrast=max_luma - min_luma,
        )

    @staticmethod
    def _sample_normal_map_average_strength(
        texture_image: QImage,
        texture_coordinates: Sequence[object],
        *,
        flip_vertical: bool,
        max_samples: int = 384,
    ) -> Optional[float]:
        if texture_image.isNull() or not texture_coordinates:
            return None
        width = int(texture_image.width())
        height = int(texture_image.height())
        if width <= 0 or height <= 0:
            return None
        step = max(1, len(texture_coordinates) // max(1, int(max_samples)))
        total = 0.0
        count = 0
        for coord in texture_coordinates[::step]:
            try:
                u = float(coord[0])  # type: ignore[index]
                v = float(coord[1])  # type: ignore[index]
            except Exception:
                continue
            if not math.isfinite(u) or not math.isfinite(v):
                continue
            u = u - math.floor(u)
            v = v - math.floor(v)
            if flip_vertical:
                v = 1.0 - v
            x = max(0, min(width - 1, int(round(u * float(width - 1)))))
            y = max(0, min(height - 1, int(round(v * float(height - 1)))))
            color = texture_image.pixelColor(x, y)
            nx = (float(color.redF()) * 2.0) - 1.0
            ny = (float(color.greenF()) * 2.0) - 1.0
            nz = (float(color.blueF()) * 2.0) - 1.0
            total += min(4.0, math.sqrt((nx * nx) + (ny * ny) + (nz * nz)))
            count += 1
            if count >= max_samples:
                break
        if count <= 0:
            return None
        return total / float(count)

    @staticmethod
    def _derived_relief_texture_key(batch_index: int, batch: _ModelPreviewDrawBatch) -> str:
        texture_key = str(batch.texture_key or "").strip()
        if not texture_key:
            return ""
        return (
            f"derived_relief:{int(batch_index)}:"
            f"{'repeat' if batch.texture_wrap_repeat else 'clamp'}:"
            f"{'flip' if batch.texture_flip_vertical else 'noflip'}:"
            f"{texture_key}"
        )

    @staticmethod
    def _derive_relief_image_from_base(texture_image: QImage, *, max_dimension: int = 512) -> Optional[QImage]:
        if texture_image.isNull():
            return None
        image = texture_image.convertToFormat(QImage.Format_RGBA8888)
        if image.isNull():
            return None
        width = int(image.width())
        height = int(image.height())
        if width <= 1 or height <= 1:
            return None
        longest = max(width, height)
        if longest > max_dimension:
            target = image.size().scaled(int(max_dimension), int(max_dimension), Qt.KeepAspectRatio)
            if target.width() > 1 and target.height() > 1:
                image = image.scaled(target, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                width = int(image.width())
                height = int(image.height())
        luma_values: List[float] = []
        luma_grid: List[List[float]] = []
        for y in range(height):
            row: List[float] = []
            for x in range(width):
                color = image.pixelColor(x, y)
                luma = (0.2126 * float(color.redF())) + (0.7152 * float(color.greenF())) + (0.0722 * float(color.blueF()))
                row.append(luma)
                luma_values.append(luma)
            luma_grid.append(row)
        if not luma_values:
            return None
        sorted_luma = sorted(luma_values)
        low = sorted_luma[int(max(0, min(len(sorted_luma) - 1, round((len(sorted_luma) - 1) * 0.05))))]
        high = sorted_luma[int(max(0, min(len(sorted_luma) - 1, round((len(sorted_luma) - 1) * 0.95))))]
        contrast = max(high - low, 0.0)
        if contrast < 0.018:
            return None
        relief = QImage(width, height, QImage.Format_RGBA8888)
        contrast_gain = max(1.0, min(4.0, 0.42 / max(contrast, 0.001)))
        for y in range(height):
            ym = max(0, y - 1)
            yp = min(height - 1, y + 1)
            for x in range(width):
                xm = max(0, x - 1)
                xp = min(width - 1, x + 1)
                center = luma_grid[y][x]
                local_average = (
                    luma_grid[ym][xm] + luma_grid[ym][x] + luma_grid[ym][xp]
                    + luma_grid[y][xm] + center + luma_grid[y][xp]
                    + luma_grid[yp][xm] + luma_grid[yp][x] + luma_grid[yp][xp]
                ) / 9.0
                sobel_x = (
                    -luma_grid[ym][xm] + luma_grid[ym][xp]
                    - (2.0 * luma_grid[y][xm]) + (2.0 * luma_grid[y][xp])
                    - luma_grid[yp][xm] + luma_grid[yp][xp]
                )
                sobel_y = (
                    -luma_grid[ym][xm] - (2.0 * luma_grid[ym][x]) - luma_grid[ym][xp]
                    + luma_grid[yp][xm] + (2.0 * luma_grid[yp][x]) + luma_grid[yp][xp]
                )
                edge = min(1.0, math.sqrt((sobel_x * sobel_x) + (sobel_y * sobel_y)) * 1.35)
                normalized = ((center - low) / max(contrast, 0.001)) - 0.5
                local_detail = (center - local_average) * 2.5
                relief_value = 0.5 + (normalized * 0.42 * contrast_gain) + (local_detail * 0.28) + ((edge - 0.20) * 0.16)
                grey = max(0, min(255, int(round(max(0.0, min(1.0, relief_value)) * 255.0))))
                relief.setPixelColor(x, y, QColor(grey, grey, grey, 255))
        return relief

    @staticmethod
    def _enhanced_relief_status(
        *,
        render_mode_code: int,
        high_quality_enabled: bool,
        support_maps_enabled: bool,
        support_maps_disabled: bool,
        height_key: str,
        height_texture_available: bool,
        height_luma: Optional[_TextureVisibilitySample],
        derived_relief_key: str = "",
        derived_relief_texture_available: bool = False,
        derived_relief_luma: Optional[_TextureVisibilitySample] = None,
        height_map_disabled: bool,
        height_effect_max: float,
    ) -> Tuple[str, str, bool, str]:
        if render_mode_code == 24:
            return "control-test", "Relief Control Test is visualizing slider values directly.", False, "control-test"
        if render_mode_code not in {22, 23}:
            return "inactive", "Enhanced Relief Preview mode is not selected.", False, "inactive"
        if not high_quality_enabled:
            return "inactive", "Support-map preview shading is disabled.", False, "inactive"
        if float(height_effect_max) <= 0.001:
            return "inactive", "Relief depth is set to zero.", False, "inactive"
        true_height_usable = bool(
            support_maps_enabled
            and not support_maps_disabled
            and not height_map_disabled
            and str(height_key or "").strip()
            and height_texture_available
            and height_luma is not None
            and float(height_luma.luma_contrast) >= 0.010
        )
        derived_usable = bool(
            str(derived_relief_key or "").strip()
            and derived_relief_texture_available
            and derived_relief_luma is not None
            and float(derived_relief_luma.luma_contrast) >= 0.018
        )
        if true_height_usable and derived_usable:
            source = "height+derived-detail"
            reason = "Calibrated height relief with derived base micro-detail is active."
        elif true_height_usable:
            source = "height-map"
            reason = "Calibrated height relief is active."
        elif derived_usable:
            source = "derived-base"
            reason = "Derived base-texture relief is active."
        else:
            if str(height_key or "").strip() and not height_texture_available and not derived_usable:
                reason = "Height texture is not uploaded and no usable base-derived relief was generated."
            elif height_luma is not None and float(height_luma.luma_contrast) < 0.010 and not derived_usable:
                reason = "Height texture is nearly flat and base-derived relief is unavailable."
            elif not str(derived_relief_key or "").strip():
                reason = "No base texture is available for derived relief."
            else:
                reason = "No usable height or base-derived relief texture is available."
            return "inactive", reason, False, "inactive"
        if render_mode_code == 22:
            return "active", reason, True, source
        return "inactive", "Relief texture diagnostic is selected.", True, source

    def _diagnostic_for_unpainted_batch(
        self,
        batch_index: int,
        batch: _ModelPreviewDrawBatch,
    ) -> _BatchRenderDiagnostic:
        meshes = getattr(self._current_model, "meshes", None) or []
        mesh = meshes[batch.mesh_index] if 0 <= batch.mesh_index < len(meshes) else None
        position_count = len(getattr(mesh, "positions", ()) or ()) if mesh is not None else 0
        uv_count = len(getattr(mesh, "texture_coordinates", ()) or ()) if mesh is not None else 0
        upload_key = (batch.texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
        upload_diagnostic = self._texture_upload_diagnostics.get(upload_key)
        image_loaded = bool(upload_diagnostic.image_loaded) if upload_diagnostic else False
        image_size = (
            f"{upload_diagnostic.image_width}x{upload_diagnostic.image_height}"
            if upload_diagnostic and upload_diagnostic.image_width > 0 and upload_diagnostic.image_height > 0
            else "-"
        )
        uploaded = bool(upload_diagnostic.upload_success) if upload_diagnostic else False
        prepared_size = (
            f"{upload_diagnostic.prepared_width}x{upload_diagnostic.prepared_height}"
            if upload_diagnostic and upload_diagnostic.prepared_width > 0 and upload_diagnostic.prepared_height > 0
            else "-"
        )
        luma = self._batch_luma_diagnostics.get(batch_index)
        material_luma = self._batch_material_luma_diagnostics.get(batch_index)
        height_luma = self._batch_height_luma_diagnostics.get(batch_index)
        settings = self.render_settings()
        derived_relief_key = self._batch_derived_relief_keys.get(batch_index, "")
        if not derived_relief_key and self._render_mode_uses_derived_relief(settings):
            derived_relief_key = self._derived_relief_texture_key(batch_index, batch)
        derived_relief_luma = self._batch_derived_relief_luma_diagnostics.get(batch_index)
        texture_objects = getattr(self, "_texture_objects", {}) or {}
        height_available = bool(
            texture_objects.get((batch.height_texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical)))
        )
        derived_relief_available = bool(
            texture_objects.get((derived_relief_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical)))
        )
        render_mode_code = int(_RENDER_DIAGNOSTIC_MODE_CODES.get(str(getattr(settings, "render_diagnostic_mode", "lit")), 0))
        enhanced_state, enhanced_reason, enhanced_usable, relief_source = self._enhanced_relief_status(
            render_mode_code=render_mode_code,
            high_quality_enabled=bool(self._high_quality_textures and batch.has_texture_coordinates),
            support_maps_enabled=bool(
                self._high_quality_textures
                and batch.has_texture_coordinates
                and not batch.support_maps_disabled
                and not bool(getattr(settings, "disable_all_support_maps", False))
            ),
            support_maps_disabled=bool(batch.support_maps_disabled),
            height_key=str(batch.height_texture_key or ""),
            height_texture_available=height_available,
            height_luma=height_luma,
            derived_relief_key=derived_relief_key,
            derived_relief_texture_available=derived_relief_available,
            derived_relief_luma=derived_relief_luma,
            height_map_disabled=bool(getattr(settings, "disable_height_map", False)),
            height_effect_max=float(getattr(settings, "height_effect_max", 0.0) or 0.0),
        )
        diagnostic = _BatchRenderDiagnostic(
            batch_index=batch_index,
            mesh_index=batch.mesh_index,
            label=self._batch_label(batch),
            texture_key=str(batch.texture_key or ""),
            texture_path_set=bool(batch.texture_key),
            image_loaded=image_loaded,
            image_size=image_size,
            uv_valid=bool(batch.has_texture_coordinates),
            uv_count=uv_count,
            position_count=position_count,
            texture_uploaded=uploaded,
            texture_id=int(upload_diagnostic.texture_id if upload_diagnostic else 0),
            diffuse_sampler_location=int(getattr(self, "_texture_sampler_uniform_location", -1)),
            render_mode_code=render_mode_code,
            alpha_handling_mode=str(getattr(settings, "alpha_handling_mode", "default")),
            texture_probe_source=str(getattr(settings, "texture_probe_source", "base")),
            sampler_probe_mode=str(getattr(settings, "sampler_probe_mode", "normal")),
            diffuse_swizzle_mode=str(getattr(settings, "diffuse_swizzle_mode", "rgba")),
            base_texture_quality=str(getattr(batch, "base_texture_quality", "") or ""),
            material_decode_mode=int(getattr(batch, "material_decode_mode", 0) or 0),
            rich_material_response=bool(
                str(getattr(settings, "render_diagnostic_mode", "lit") or "").strip().lower()
                == "rich_lit"
            ),
            prepared_image_size=prepared_size,
            gl_error=str(upload_diagnostic.gl_error if upload_diagnostic else ""),
            alpha_discard_risk=bool(luma and luma.alpha_dark_ratio >= 0.50),
            use_texture=False,
            use_normal=False,
            use_material=False,
            use_height=False,
            use_relief=bool(enhanced_usable),
            sampled_luma=luma.average_luma if luma else None,
            sampled_dark_ratio=luma.dark_ratio if luma else None,
            sampled_alpha=luma.average_alpha if luma else None,
            material_sampled_luma=material_luma.average_luma if material_luma else None,
            material_sampled_dark_ratio=material_luma.dark_ratio if material_luma else None,
            material_sampled_alpha=material_luma.average_alpha if material_luma else None,
            height_sampled_luma=height_luma.average_luma if height_luma else None,
            height_sampled_dark_ratio=height_luma.dark_ratio if height_luma else None,
            height_sampled_alpha=height_luma.average_alpha if height_luma else None,
            height_sampled_min_luma=height_luma.min_luma if height_luma else None,
            height_sampled_max_luma=height_luma.max_luma if height_luma else None,
            height_sampled_contrast=height_luma.luma_contrast if height_luma else None,
            derived_relief_sampled_luma=derived_relief_luma.average_luma if derived_relief_luma else None,
            derived_relief_sampled_min_luma=derived_relief_luma.min_luma if derived_relief_luma else None,
            derived_relief_sampled_max_luma=derived_relief_luma.max_luma if derived_relief_luma else None,
            derived_relief_sampled_contrast=derived_relief_luma.luma_contrast if derived_relief_luma else None,
            enhanced_relief_state=enhanced_state,
            enhanced_relief_reason=enhanced_reason,
            relief_source=relief_source,
            normal_average_strength=self._batch_normal_strength_diagnostics.get(batch_index),
            source_average_color=luma.average_color if luma else (),
            normal_finite_ratio=float(batch.normal_finite_ratio),
            normal_repair_count=int(batch.normal_repair_count),
            tangent_finite_ratio=float(batch.tangent_finite_ratio),
            bitangent_finite_ratio=float(batch.bitangent_finite_ratio),
            uv_finite_ratio=float(batch.uv_finite_ratio),
            smooth_normal_ratio=float(getattr(batch, "smooth_normal_ratio", 0.0) or 0.0),
            texture_flip_vertical=bool(batch.texture_flip_vertical),
            texture_wrap_repeat=bool(batch.texture_wrap_repeat),
            texture_brightness=float(batch.texture_brightness or 1.0),
            texture_tint=tuple(batch.texture_tint or ()),
            texture_uv_scale=tuple(batch.texture_uv_scale or ()),
        )
        if not batch.texture_key:
            diagnostic.failure_bucket = "image"
            diagnostic.failure_reason = "No base preview texture path is assigned to this batch."
        elif not self._use_textures:
            diagnostic.failure_bucket = "use_texture"
            diagnostic.failure_reason = "Use textures when available is disabled."
        elif not batch.has_texture_coordinates:
            diagnostic.failure_bucket = "uv"
            diagnostic.failure_reason = "Recovered mesh UV count does not match position count."
        elif upload_diagnostic and not upload_diagnostic.image_loaded:
            diagnostic.failure_bucket = "image"
            diagnostic.failure_reason = upload_diagnostic.failure_reason or f"DDS preview PNG missing or unreadable: {batch.texture_key}"
        elif upload_diagnostic and not upload_diagnostic.upload_success:
            diagnostic.failure_bucket = "upload"
            diagnostic.failure_reason = upload_diagnostic.failure_reason or "Texture image loaded but no GL texture object was uploaded."
        else:
            diagnostic.failure_bucket = "pending"
            diagnostic.failure_reason = "Renderer has not painted this batch yet."
        return diagnostic

    @staticmethod
    def _sample_framebuffer_visibility(
        framebuffer: QImage,
        background_color: QColor,
        *,
        max_samples: int = 4096,
    ) -> _FramebufferVisibilitySample:
        if framebuffer.isNull():
            return _FramebufferVisibilitySample()
        image = framebuffer.convertToFormat(QImage.Format_RGBA8888)
        width = int(image.width())
        height = int(image.height())
        if width <= 0 or height <= 0:
            return _FramebufferVisibilitySample()
        total_pixels = width * height
        step = max(1, int(math.sqrt(max(1, total_pixels // max(1, int(max_samples))))))
        bg_r = float(background_color.redF())
        bg_g = float(background_color.greenF())
        bg_b = float(background_color.blueF())
        visible = 0
        background = 0
        dark = 0
        luma_total = 0.0
        sampled = 0
        for y in range(0, height, step):
            for x in range(0, width, step):
                color = image.pixelColor(x, y)
                red = float(color.redF())
                green = float(color.greenF())
                blue = float(color.blueF())
                distance = abs(red - bg_r) + abs(green - bg_g) + abs(blue - bg_b)
                sampled += 1
                if distance <= 0.035:
                    background += 1
                    continue
                luma = (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
                visible += 1
                luma_total += luma
                if luma < 0.055:
                    dark += 1
        if sampled <= 0 or visible <= 0:
            return _FramebufferVisibilitySample(
                visible_pixels=0,
                background_ratio=1.0 if sampled <= 0 else background / float(sampled),
            )
        return _FramebufferVisibilitySample(
            visible_pixels=visible,
            average_luma=luma_total / float(visible),
            dark_ratio=dark / float(visible),
            background_ratio=background / float(sampled),
        )

    @staticmethod
    def _black_output_triage_lines(
        diagnostics: Sequence[_BatchRenderDiagnostic],
        framebuffer: _FramebufferVisibilitySample,
    ) -> Tuple[str, ...]:
        dark_output = bool(
            framebuffer.visible_pixels > 0
            and framebuffer.average_luma < 0.075
            and framebuffer.dark_ratio > 0.60
        )
        if not dark_output:
            return ()
        missing_base = sum(1 for item in diagnostics if not item.texture_path_set)
        alpha_hidden = sum(
            1
            for item in diagnostics
            if item.alpha_discard_risk and item.alpha_handling_mode == "default"
        )
        image_failed = sum(1 for item in diagnostics if item.failure_bucket == "image" and item.texture_path_set)
        upload_failed = sum(1 for item in diagnostics if item.failure_bucket == "upload")
        shader_dark = sum(
            1
            for item in diagnostics
            if item.use_texture and item.sampled_luma is not None and item.sampled_luma >= 0.075
        )
        support_only = sum(
            1
            for item in diagnostics
            if not item.use_texture and (item.use_normal or item.use_material or item.use_height)
        )
        support_dark = sum(
            1
            for item in diagnostics
            if item.use_texture
            and (item.use_normal or item.use_material or item.use_height)
            and (
                (item.material_sampled_luma is not None and item.material_sampled_luma < 0.035)
                or (item.height_sampled_luma is not None and item.height_sampled_luma < 0.035)
            )
        )
        invalid_normals = sum(1 for item in diagnostics if item.normal_repair_count > 0 or item.normal_finite_ratio < 0.80)

        lines = ["Black Output Triage:"]
        if missing_base:
            lines.append(
                f"- Missing base/color texture on {missing_base:,} batch(es). Assign a Base / Color map; support maps cannot provide visible color."
            )
        if alpha_hidden:
            lines.append(
                f"- Alpha/discard can hide {alpha_hidden:,} batch(es). Try Force Opaque or Show Alpha to verify the source texture."
            )
        if image_failed:
            lines.append(
                f"- Base texture decode failed on {image_failed:,} batch(es). Check DDS conversion/cache output for those paths."
            )
        if upload_failed:
            lines.append(
                f"- Texture upload failed on {upload_failed:,} batch(es). Try a smaller preview texture limit or nearest/no-mipmap diagnostics."
            )
        if support_only:
            lines.append(
                f"- {support_only:,} batch(es) have only normal/material/height support maps active. Add a Base / Color map to avoid grey or black output."
            )
        if support_dark:
            lines.append(
                f"- Support-map samples are very dark on {support_dark:,} batch(es). Disable Material/Height maps or inspect Material Mask/Height diagnostics."
            )
        if invalid_normals:
            lines.append(
                f"- Normal data was repaired or appears invalid on {invalid_normals:,} batch(es). Inspect Normal mode before trusting lighting."
            )
        if shader_dark and not any((missing_base, alpha_hidden, image_failed, upload_failed, support_only, support_dark)):
            lines.append(
                "- Base texture samples are visible but shaded output is dark. Use Base Color Guarded, Material Mask Response, and Metal / Shine Response modes to isolate shader response."
            )
        if len(lines) == 1:
            lines.append(
                "- No single texture failure was obvious. Compare Lit against Base Texture Raw, Base Color Guarded, and UV diagnostics."
            )
        return tuple(lines)

    def _render_sampling_diagnostic_lines(self) -> Tuple[str, ...]:
        if not self._mesh_batches:
            return ()
        diagnostics = [
            self._batch_render_diagnostics.get(index) or self._diagnostic_for_unpainted_batch(index, batch)
            for index, batch in enumerate(self._mesh_batches)
        ]
        current_mode_code = int(_RENDER_DIAGNOSTIC_MODE_CODES.get(str(self.render_settings().render_diagnostic_mode), 0))
        stale_mode_count = sum(
            1
            for item in diagnostics
            if int(getattr(item, "render_mode_code", current_mode_code)) != current_mode_code
        )
        sampled = sum(1 for item in diagnostics if item.use_texture)
        blocked_image = sum(1 for item in diagnostics if item.failure_bucket == "image")
        blocked_uv = sum(1 for item in diagnostics if item.failure_bucket == "uv")
        blocked_upload = sum(1 for item in diagnostics if item.failure_bucket == "upload")
        blocked_use = sum(1 for item in diagnostics if item.failure_bucket == "use_texture")
        shader_dark = sum(
            1
            for item in diagnostics
            if item.use_texture and item.sampled_luma is not None and item.sampled_luma < 0.035
        )
        shader_guarded = sum(
            1
            for item in diagnostics
            if item.use_texture and item.texture_uploaded and item.uv_valid and item.image_loaded
        )
        relief_active = sum(1 for item in diagnostics if item.enhanced_relief_state == "active")
        relief_reasons = sorted(
            {
                item.enhanced_relief_reason
                for item in diagnostics
                if item.enhanced_relief_state != "active" and item.enhanced_relief_reason
            }
        )
        relief_sources = sorted({item.relief_source for item in diagnostics if item.relief_source})
        relief_reason_text = relief_reasons[0] if relief_reasons else "All eligible batches have calibrated relief."
        framebuffer = self._framebuffer_visibility_diagnostic
        dark_output = bool(framebuffer.visible_pixels > 0 and framebuffer.average_luma < 0.075 and framebuffer.dark_ratio > 0.60)
        settings = self.render_settings()
        lines: List[str] = [
            "",
            "Render Sampling Diagnostics",
            f"Renderer Build: {MODEL_PREVIEW_RENDER_BUILD_ID}",
            f"Diagnostic Render Mode: {self._render_diagnostic_mode_label()}",
            (
                "Render Settings Snapshot: "
                f"mode_code={current_mode_code}, "
                f"alpha={settings.alpha_handling_mode}, source={settings.texture_probe_source}, "
                f"sampler={settings.sampler_probe_mode}, swizzle={settings.diffuse_swizzle_mode}, "
                f"disable_tint={self._yes_no(settings.disable_tint)}, "
                f"disable_brightness={self._yes_no(settings.disable_brightness)}, "
                f"disable_uv_scale={self._yes_no(settings.disable_uv_scale)}, "
                f"nearest_no_mips={self._yes_no(settings.force_nearest_no_mipmaps)}, "
                f"disable_lighting={self._yes_no(settings.disable_lighting)}, "
                f"disable_depth={self._yes_no(settings.disable_depth_test)}, "
                f"enhanced_relief_mode={self._yes_no(str(settings.render_diagnostic_mode) == 'rich_lit')}, "
                f"relief_depth={float(settings.height_effect_max):.2f}, "
                f"specular_response={float(settings.specular_max):.2f}, "
                f"surface_contrast={float(settings.shininess_max):.0f}, "
                f"solo_batch={settings.solo_batch_index}, "
                f"shader_mode_uniform_loc={int(getattr(self, '_render_diagnostic_mode_uniform_location', -1))}"
            ),
            f"Sampled base textures: {sampled:,} / {len(diagnostics):,}",
            f"Blocked by image load: {blocked_image:,}",
            f"Blocked by UVs: {blocked_uv:,}",
            f"Blocked by GL upload: {blocked_upload:,}",
            f"Blocked by preview settings/control logic: {blocked_use:,}",
            f"Shader sampled but source luma is very dark: {shader_dark:,}",
            f"Shader/material luma guard eligible: {shader_guarded:,}",
            "Shader/material guard: preserves visible base color when texture sampling is enabled",
            (
                f"Diagnostics pending repaint: {stale_mode_count:,} stale batch mode(s)"
                if stale_mode_count
                else "Diagnostics pending repaint: no"
            ),
            (
                "Relief Capability: "
                f"enhanced_relief={'active' if relief_active else 'inactive'} "
                f"({relief_active:,}/{len(diagnostics):,} batch(es)); "
                f"source={','.join(relief_sources) if relief_sources else 'inactive'}; "
                f"reason={relief_reason_text}; "
                f"relief_depth={float(settings.height_effect_max):.2f}, "
                f"specular_response={float(settings.specular_max):.2f}, "
                f"surface_contrast={float(settings.shininess_max):.0f}"
            ),
            (
                "Framebuffer probe: "
                f"visible_px={framebuffer.visible_pixels:,}, "
                f"output_luma={framebuffer.average_luma:.3f}, "
                f"dark_px={framebuffer.dark_ratio:.0%}, "
                f"background_px={framebuffer.background_ratio:.0%}"
            ),
        ]
        lines.extend(self._black_output_triage_lines(diagnostics, framebuffer))
        for item in diagnostics:
            luma_text = ""
            if item.sampled_luma is not None:
                color_text = ""
                if len(item.source_average_color) >= 3:
                    color_text = (
                        f", source_average_color="
                        f"{item.source_average_color[0]:.2f}/"
                        f"{item.source_average_color[1]:.2f}/"
                        f"{item.source_average_color[2]:.2f}"
                )
                guard_text = f", visibility_guard={item.visibility_guard}" if item.visibility_guard else ""
                luma_text = (
                    f", source_luma={item.sampled_luma:.3f}, "
                    f"dark_px={item.sampled_dark_ratio or 0.0:.0%}, "
                    f"alpha={item.sampled_alpha if item.sampled_alpha is not None else 1.0:.2f}"
                    f"{color_text}{guard_text}"
                )
                if item.alpha_discard_risk:
                    luma_text = f"{luma_text}, alpha_discard_risk=yes"
            support_text = ""
            if item.material_sampled_luma is not None or item.height_sampled_luma is not None or item.normal_average_strength is not None:
                support_parts: List[str] = []
                if item.normal_average_strength is not None:
                    support_parts.append(f"normal_strength={item.normal_average_strength:.2f}")
                if item.material_sampled_luma is not None:
                    support_parts.append(
                        f"material_luma={item.material_sampled_luma:.3f}/dark={item.material_sampled_dark_ratio or 0.0:.0%}"
                    )
                if item.height_sampled_luma is not None:
                    support_parts.append(
                        f"height_luma={item.height_sampled_luma:.3f}/dark={item.height_sampled_dark_ratio or 0.0:.0%}"
                    )
                    if item.height_sampled_min_luma is not None and item.height_sampled_max_luma is not None:
                        support_parts.append(
                            f"height_range={item.height_sampled_min_luma:.3f}-{item.height_sampled_max_luma:.3f}"
                        )
                if item.height_sampled_contrast is not None:
                    support_parts.append(f"height_contrast={item.height_sampled_contrast:.3f}")
                if item.derived_relief_sampled_contrast is not None:
                    support_parts.append(
                        f"derived_relief_luma={item.derived_relief_sampled_luma or 0.0:.3f}"
                    )
                    support_parts.append(
                        f"derived_relief_range={item.derived_relief_sampled_min_luma or 0.0:.3f}-{item.derived_relief_sampled_max_luma or 0.0:.3f}"
                    )
                    support_parts.append(f"derived_relief_contrast={item.derived_relief_sampled_contrast:.3f}")
                support_text = f", support_samples={' '.join(support_parts)}"
            final_bucket = item.final_bucket
            if item.alpha_discard_risk and item.alpha_handling_mode == "default":
                final_bucket = "alpha discard hid base"
            elif item.texture_path_set and not item.texture_uploaded:
                final_bucket = "mipmap incomplete or invalid texture object"
            elif str(item.sampler_probe_mode).startswith("force_unit") and dark_output:
                final_bucket = "diffuse sampler transparent/black"
            elif dark_output and item.use_texture and (item.sampled_luma or 0.0) >= 0.075:
                final_bucket = "base sampled but shader output dark"
            elif item.normal_repair_count > 0:
                final_bucket = "invalid normals repaired"
            elif item.use_texture and (item.use_normal or item.use_material or item.use_height) and dark_output:
                final_bucket = "support maps darkened output"
            line = (
                f"Batch {item.batch_index} {item.label}: "
                f"base={'set' if item.texture_path_set else 'none'}"
                f" ({self._summarize_texture_key(item.texture_key)}), "
                f"image={self._yes_no(item.image_loaded)} {item.image_size}, "
                f"prepared={item.prepared_image_size}, "
                f"uv={self._yes_no(item.uv_valid)} {item.uv_count:,}/{item.position_count:,}, "
                f"upload={self._yes_no(item.texture_uploaded)} tex_id={item.texture_id}, "
                f"support_tex_ids=n:{item.normal_texture_id} m:{item.material_texture_id} h:{item.height_texture_id} r:{item.relief_texture_id}, "
                f"diffuse_unit={item.diffuse_unit}, sampler_loc={item.diffuse_sampler_location}, "
                f"mode_code={item.render_mode_code}, alpha_mode={item.alpha_handling_mode}, "
                f"base_quality={item.base_texture_quality or '-'}, material_decode={item.material_decode_mode}, "
                f"rich_material={self._yes_no(item.rich_material_response)}, "
                f"enhanced_relief={item.enhanced_relief_state or '-'}, relief_source={item.relief_source or '-'}, "
                f"probe_source={item.texture_probe_source}, sampler_probe={item.sampler_probe_mode}, "
                f"swizzle={item.diffuse_swizzle_mode}, "
                f"use_texture={self._yes_no(item.use_texture)}, "
                f"support_maps=n:{self._yes_no(item.use_normal)} "
                f"m:{self._yes_no(item.use_material)} h:{self._yes_no(item.use_height)} "
                f"r:{self._yes_no(item.use_relief)}, "
                f"support_uploads=n:{self._yes_no(item.normal_uploaded)} "
                f"m:{self._yes_no(item.material_uploaded)} h:{self._yes_no(item.height_uploaded)}, "
                f"normals={item.normal_finite_ratio:.0%} repaired={item.normal_repair_count:,}, "
                f"smooth_normals={item.smooth_normal_ratio:.0%}, "
                f"tangent={item.tangent_finite_ratio:.0%}, bitangent={item.bitangent_finite_ratio:.0%}, "
                f"uv_finite={item.uv_finite_ratio:.0%}, flip_v={self._yes_no(item.texture_flip_vertical)}, "
                f"wrap={self._yes_no(item.texture_wrap_repeat)}, brightness={item.texture_brightness:.2f}, "
                f"tint={self._format_float_tuple(item.texture_tint, default='1/1/1')}, "
                f"uv_scale={self._format_float_tuple(item.texture_uv_scale, default='1/1')}"
                f"{luma_text}{support_text}"
            )
            if final_bucket:
                line = f"{line}, final_bucket={final_bucket}"
            if item.enhanced_relief_reason:
                line = f"{line}, relief_reason={item.enhanced_relief_reason}"
            if item.gl_error:
                line = f"{line}, gl_error={item.gl_error}"
            if item.failure_bucket:
                line = f"{line} -> {item.failure_bucket}: {item.failure_reason}"
            lines.append(line)
        return tuple(lines)

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
        base_sources: List[str] = []
        base_qualities: List[str] = []
        sidecar_approximation_count = 0
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
            base_source = str(getattr(mesh, "preview_base_texture_source", "") or "").strip()
            if base_source:
                base_sources.append(base_source)
            base_quality = str(getattr(mesh, "preview_base_texture_quality", "") or "").strip()
            if base_quality:
                base_qualities.append(base_quality.replace("_", " ").title())
            if str(getattr(mesh, "preview_texture_approximation_note", "") or "").strip():
                sidecar_approximation_count += 1
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
        support_available_counts = self._support_map_slot_counts_from_batches(self._mesh_batches)
        support_active_counts = self._support_map_active_counts_from_diagnostics(self._batch_render_diagnostics)
        settings = self.render_settings()
        support_gate_reasons: List[str] = []
        if sum(support_available_counts.values()) <= 0:
            support_gate_reasons.append("no support maps resolved")
        if not self._high_quality_textures:
            support_gate_reasons.append("support-map preview shading off")
        if bool(getattr(settings, "disable_all_support_maps", False)) or self.support_maps_disabled():
            support_gate_reasons.append("support maps disabled")
        disabled_slots = [
            label
            for enabled, label in (
                (getattr(settings, "disable_normal_map", False), "normal"),
                (getattr(settings, "disable_material_map", False), "material"),
                (getattr(settings, "disable_height_map", False), "height"),
            )
            if bool(enabled)
        ]
        if disabled_slots:
            support_gate_reasons.append("disabled slots: " + "/".join(disabled_slots))
        self._debug_overlay_lines = (
            f"Visible Mode: {self._visible_texture_mode_label()}",
        )
        render_diagnostic_lines = self._render_sampling_diagnostic_lines()
        self._debug_detail_lines = (
            self._debug_overlay_lines[0],
            f"Base: {self._summarize_overlay_values(base_names)}",
            f"Normal: {self._summarize_overlay_values(normal_names)}",
            f"Material: {self._summarize_overlay_values(material_names)}",
            f"Material Decode: {self._summarize_overlay_values(material_interpretations)}",
            f"Height: {self._summarize_overlay_values(height_names)}",
            (
                "Support Maps: "
                f"available {self._format_support_map_counts(support_available_counts)}; "
                f"active {self._format_support_map_counts(support_active_counts)}"
                f"{'; ' + ', '.join(support_gate_reasons) if support_gate_reasons else ''}"
            ),
            f"Base Source: {self._summarize_overlay_values(base_sources)}",
            f"Base Quality: {self._summarize_overlay_values(base_qualities)}",
            (
                f"Preview Approximation: {sidecar_approximation_count:,} sidecar material parameter set(s)"
                if sidecar_approximation_count
                else "Preview Approximation: None"
            ),
            f"Overrides: {', '.join(override_labels) if override_labels else 'None'}",
            *render_diagnostic_lines,
        )
        self.debug_details_changed.emit("\n".join(self._debug_detail_lines))

    def _visible_texture_mode_label(self) -> str:
        settings = self.render_settings()
        mode = str(getattr(settings, "visible_texture_mode", "") or "").strip().lower()
        return MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS.get(mode, mode.replace("_", " ").title() or "Mesh Base First")

    def _render_diagnostic_mode_label(self) -> str:
        settings = self.render_settings()
        mode = str(getattr(settings, "render_diagnostic_mode", "") or "").strip().lower()
        return MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODE_LABELS.get(mode, mode.replace("_", " ").title() or "Lit")

    @staticmethod
    def _render_mode_uses_derived_relief(settings: Optional[ModelPreviewRenderSettings]) -> bool:
        mode = str(getattr(settings, "render_diagnostic_mode", "lit") or "lit").strip().lower()
        return mode in {"rich_lit", "height_calibrated"}

    def render_settings(self) -> ModelPreviewRenderSettings:
        return clamp_model_preview_render_settings(self._render_settings)

    def debug_details_text(self) -> str:
        return "\n".join(self._debug_detail_lines)

    def set_render_settings(self, settings: Optional[ModelPreviewRenderSettings]) -> None:
        clamped = clamp_model_preview_render_settings(settings)
        previous = self._render_settings
        self._render_settings = clamped
        render_response_changed = (
            previous.render_diagnostic_mode != clamped.render_diagnostic_mode
            or previous.height_effect_max != clamped.height_effect_max
            or previous.specular_max != clamped.specular_max
            or previous.shininess_max != clamped.shininess_max
        )
        derived_relief_need_changed = (
            self._render_mode_uses_derived_relief(previous)
            != self._render_mode_uses_derived_relief(clamped)
        )
        textures_changed = (
            previous.preview_texture_max_dimension != clamped.preview_texture_max_dimension
            or previous.low_quality_texture_max_dimension != clamped.low_quality_texture_max_dimension
            or previous.max_anisotropy != clamped.max_anisotropy
            or previous.force_nearest_no_mipmaps != clamped.force_nearest_no_mipmaps
            or derived_relief_need_changed
        )
        if render_response_changed:
            self._batch_render_diagnostics = {}
            self._framebuffer_visibility_diagnostic = _FramebufferVisibilitySample()
        if previous.visible_texture_mode != clamped.visible_texture_mode or render_response_changed:
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

    def _alignment_translation_display_vector(self) -> QVector3D:
        scale = 1.0
        try:
            scale = float(getattr(self._current_model, "normalization_scale", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        return QVector3D(
            float(self._alignment_live_translation.x()) * scale,
            float(self._alignment_live_translation.y()) * scale,
            float(self._alignment_live_translation.z()) * scale,
        )

    def _apply_alignment_live_transform(
        self,
        base_model: QMatrix4x4,
        live_translation: QVector3D,
        live_rotation: QVector3D,
        rotation_origin: tuple[float, float, float],
    ) -> QMatrix4x4:
        matrix = QMatrix4x4(base_model)
        if live_translation.lengthSquared() > 1e-12:
            matrix.translate(live_translation)
        if live_rotation.lengthSquared() > 1e-12:
            matrix.translate(float(rotation_origin[0]), float(rotation_origin[1]), float(rotation_origin[2]))
            matrix.rotate(float(live_rotation.x()), 1.0, 0.0, 0.0)
            matrix.rotate(float(live_rotation.y()), 0.0, 1.0, 0.0)
            matrix.rotate(float(live_rotation.z()), 0.0, 0.0, 1.0)
            matrix.translate(-float(rotation_origin[0]), -float(rotation_origin[1]), -float(rotation_origin[2]))
        return matrix

    def _alignment_batch_is_editable(self, batch_index: int) -> bool:
        if self._alignment_editable_mesh_indices is not None:
            return int(batch_index) in self._alignment_editable_mesh_indices
        if self._alignment_editable_mesh_count < 0:
            return batch_index >= self._alignment_editable_mesh_start
        return (
            batch_index >= self._alignment_editable_mesh_start
            and batch_index < self._alignment_editable_mesh_start + self._alignment_editable_mesh_count
        )

    def _preview_base_model_matrix(self) -> QMatrix4x4:
        model = QMatrix4x4()
        model.translate(self._pan_offset)
        model.rotate(self._pitch, 1.0, 0.0, 0.0)
        model.rotate(self._yaw, 0.0, 1.0, 0.0)
        return model
            

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
            attribute vec3 smooth_normal;
            uniform mat4 mvp_matrix;
            uniform mat4 model_matrix;
            varying vec3 frag_position;
            varying vec3 frag_normal;
            varying vec3 frag_smooth_normal;
            varying vec3 frag_color;
            varying vec2 frag_texcoord;
            varying vec3 frag_tangent;
            varying vec3 frag_bitangent;
            vec3 safe_normalize(vec3 value, vec3 fallback) {
                float len_sq = dot(value, value);
                if (len_sq <= 0.00000001) {
                    return fallback;
                }
                vec3 normalized = value * inversesqrt(len_sq);
                if (dot(normalized, normalized) <= 0.5) {
                    return fallback;
                }
                return normalized;
            }
            void main() {
                frag_position = (model_matrix * vec4(position, 1.0)).xyz;
                frag_normal = safe_normalize((model_matrix * vec4(normal, 0.0)).xyz, vec3(0.0, 0.0, 1.0));
                frag_smooth_normal = safe_normalize((model_matrix * vec4(smooth_normal, 0.0)).xyz, frag_normal);
                frag_tangent = safe_normalize((model_matrix * vec4(tangent, 0.0)).xyz, vec3(1.0, 0.0, 0.0));
                frag_bitangent = safe_normalize((model_matrix * vec4(bitangent, 0.0)).xyz, vec3(0.0, 1.0, 0.0));
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
            varying vec3 frag_smooth_normal;
            varying vec3 frag_color;
            varying vec2 frag_texcoord;
            varying vec3 frag_tangent;
            varying vec3 frag_bitangent;
            uniform vec3 camera_position;
            uniform vec3 light_direction;
            uniform float ambient_strength;
            uniform int use_texture;
            uniform int render_diagnostic_mode;
            uniform int alpha_handling_mode;
            uniform int diffuse_swizzle_mode;
            uniform int disable_tint;
            uniform int disable_brightness;
            uniform int disable_uv_scale;
            uniform int disable_lighting;
            uniform float render_build_marker;
            uniform vec3 base_texture_tint;
            uniform float base_texture_brightness;
            uniform vec2 base_texture_uv_scale;
            uniform vec3 base_texture_average_color;
            uniform float base_texture_average_luma;
            uniform int base_texture_quality;
            uniform int use_high_quality;
            uniform int use_normal_texture;
            uniform float normal_texture_strength;
            uniform int use_material_texture;
            uniform int use_height_texture;
            uniform int use_relief_texture;
            uniform float diffuse_wrap_bias;
            uniform float diffuse_light_scale;
            uniform float normal_strength_cap;
            uniform float normal_strength_floor;
            uniform float height_effect_max;
            uniform float height_sample_min;
            uniform float height_sample_max;
            uniform float height_sample_contrast;
            uniform int height_relief_usable;
            uniform int relief_source_code;
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
            uniform sampler2D relief_texture;
            vec4 apply_diffuse_swizzle(vec4 value, int mode) {
                vec4 sample = clamp(value, 0.0, 1.0);
                if (mode == 1) {
                    sample = sample.bgra;
                } else if (mode == 2) {
                    sample = vec4(sample.rrr, sample.a);
                } else if (mode == 3) {
                    sample = vec4(sample.ggg, sample.a);
                } else if (mode == 4) {
                    sample = vec4(sample.bbb, sample.a);
                } else if (mode == 5) {
                    sample = vec4(sample.aaa, 1.0);
                } else if (mode == 6) {
                    sample.a = 1.0;
                }
                return sample;
            }
            vec3 safe_normalize(vec3 value, vec3 fallback) {
                float len_sq = dot(value, value);
                if (len_sq <= 0.00000001) {
                    return fallback;
                }
                vec3 normalized = value * inversesqrt(len_sq);
                if (dot(normalized, normalized) <= 0.5) {
                    return fallback;
                }
                return normalized;
            }
            float wrapped_lambert(vec3 surface_normal, vec3 light_vector, float wrap_bias) {
                return clamp((dot(surface_normal, light_vector) + wrap_bias) / (1.0 + wrap_bias), 0.0, 1.0);
            }
            float color_saturation(vec3 value) {
                float high = max(max(value.r, value.g), value.b);
                float low = min(min(value.r, value.g), value.b);
                return max(high - low, 0.0);
            }
            vec3 luma_preserving_colorize(vec3 base, vec3 hint, float amount) {
                float base_luma = dot(base, vec3(0.2126, 0.7152, 0.0722));
                float hint_luma = max(dot(hint, vec3(0.2126, 0.7152, 0.0722)), 0.08);
                vec3 hue = clamp(hint / hint_luma, vec3(0.35), vec3(1.85));
                vec3 colored = clamp(max(base * 0.72, hue * max(base_luma, 0.10)), 0.0, 1.35);
                return mix(base, colored, clamp(amount, 0.0, 1.0));
            }
            float calibrated_height_value(float raw_height) {
                float range = max(height_sample_max - height_sample_min, 0.001);
                float normalized = clamp((raw_height - height_sample_min) / range, 0.0, 1.0);
                float contrast = max(height_sample_contrast, range);
                float auto_gain = clamp(0.24 / max(contrast, 0.018), 1.0, 5.5);
                float centered = (normalized - 0.5) * auto_gain;
                return clamp(0.5 + centered, 0.0, 1.0);
            }
            float sample_relief_height(vec2 uv) {
                float raw_height = 0.5;
                if (relief_source_code == 1) {
                    raw_height = texture2D(height_texture, uv).r;
                } else if (relief_source_code == 2) {
                    raw_height = texture2D(relief_texture, uv).r;
                } else if (relief_source_code == 3) {
                    float true_height = texture2D(height_texture, uv).r;
                    float derived_detail = texture2D(relief_texture, uv).r - 0.5;
                    raw_height = clamp(true_height + (derived_detail * 0.28), 0.0, 1.0);
                }
                if (height_relief_usable == 0) {
                    return raw_height;
                }
                return calibrated_height_value(raw_height);
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
                bool rich_lit = render_diagnostic_mode == 22;
                if (render_diagnostic_mode == 1) {
                    gl_FragColor = vec4(1.0, 1.0, 1.0, 1.0);
                    return;
                }
                if (render_diagnostic_mode == 2) {
                    float marker = fract(render_build_marker);
                    gl_FragColor = vec4(0.12 + marker, 0.82, 0.24, 1.0);
                    return;
                }
                if (render_diagnostic_mode == 3) {
                    vec2 cell = floor(gl_FragCoord.xy / vec2(18.0, 18.0));
                    float checker = mod(cell.x + cell.y, 2.0);
                    gl_FragColor = vec4(mix(vec3(0.08, 0.22, 0.72), vec3(0.95, 0.95, 0.18), checker), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 24) {
                    float relief_bar = clamp(height_effect_max, 0.0, 1.0);
                    float shine_bar = clamp(specular_max, 0.0, 1.0);
                    float contrast_bar = clamp((shininess_max - 1.0) / 255.0, 0.0, 1.0);
                    vec2 cell = floor(gl_FragCoord.xy / vec2(18.0, 18.0));
                    float checker = mod(cell.x + cell.y, 2.0) * 0.10;
                    float scan = step(0.50, fract(gl_FragCoord.y / 54.0));
                    vec3 control_color = vec3(
                        mix(0.18, 1.00, relief_bar),
                        mix(0.18, 1.00, shine_bar),
                        mix(0.18, 1.00, contrast_bar)
                    );
                    control_color = max(control_color, vec3(0.22, 0.22, 0.22));
                    control_color += checker + (scan * vec3(0.06, 0.03, 0.08));
                    gl_FragColor = vec4(clamp(control_color, 0.0, 1.0), 1.0);
                    return;
                }
                vec3 fallback_vertex_color = max(frag_color, vec3(0.0));
                float fallback_luma = dot(fallback_vertex_color, vec3(0.2126, 0.7152, 0.0722));
                if (fallback_luma <= 0.045) {
                    fallback_vertex_color = vec3(0.74, 0.76, 0.80);
                }
                vec4 base_color = vec4(fallback_vertex_color, 1.0);
                vec2 sample_uv = disable_uv_scale != 0 ? frag_texcoord : frag_texcoord * base_texture_uv_scale;
                float relief_self_shadow = 0.0;
                float relief_parallax_amount = 0.0;
                float sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                vec3 protected_average = clamp(base_texture_average_color, 0.0, 1.0);
                if (disable_tint == 0) {
                    protected_average *= base_texture_tint;
                }
                if (disable_brightness == 0) {
                    protected_average *= base_texture_brightness;
                }
                protected_average = clamp(protected_average, 0.0, 1.0);
                if (render_diagnostic_mode == 4) {
                    gl_FragColor = vec4(clamp(fallback_vertex_color, 0.0, 1.0), 1.0);
                    return;
                }
                vec3 surface_normal = safe_normalize(frag_normal, vec3(0.0, 0.0, 1.0));
                if (render_diagnostic_mode == 5) {
                    gl_FragColor = vec4(clamp((surface_normal * 0.5) + vec3(0.5), 0.0, 1.0), 1.0);
                    return;
                }
                vec3 preview_tangent = safe_normalize(frag_tangent, vec3(1.0, 0.0, 0.0));
                vec3 preview_bitangent = safe_normalize(frag_bitangent, vec3(0.0, 1.0, 0.0));
                if (rich_lit) {
                    vec3 smoothed_preview_normal = safe_normalize(frag_smooth_normal, surface_normal);
                    if (dot(surface_normal, smoothed_preview_normal) > 0.05) {
                        surface_normal = safe_normalize(mix(surface_normal, smoothed_preview_normal, 0.72), surface_normal);
                    }
                }
                if (rich_lit && use_high_quality != 0 && use_relief_texture != 0 && height_relief_usable != 0 && height_effect_max > 0.001) {
                    vec3 preview_view_dir = safe_normalize(camera_position - frag_position, vec3(0.0, 0.0, 1.0));
                    vec3 tangent_view = vec3(
                        dot(preview_view_dir, preview_tangent),
                        dot(preview_view_dir, preview_bitangent),
                        max(dot(preview_view_dir, surface_normal), 0.18)
                    );
                    vec2 parallax_dir = tangent_view.xy / max(abs(tangent_view.z), 0.22);
                    float parallax_scale = height_effect_max * mix(0.075, 0.220, clamp(height_effect_max, 0.0, 1.0));
                    float layer_depth = 0.0;
                    float layer_step = 1.0 / 8.0;
                    vec2 uv_step = parallax_dir * parallax_scale * layer_step;
                    vec2 relief_uv = sample_uv;
                    float relief_height = sample_relief_height(relief_uv);
                    float previous_height = relief_height;
                    float parallax_valley = relief_height;
                    for (int relief_step = 0; relief_step < 8; ++relief_step) {
                        if (relief_height <= layer_depth) {
                            break;
                        }
                        previous_height = relief_height;
                        relief_uv -= uv_step;
                        layer_depth += layer_step;
                        relief_height = sample_relief_height(relief_uv);
                        parallax_valley = min(parallax_valley, relief_height);
                    }
                    float after_depth = relief_height - layer_depth;
                    float before_depth = previous_height - max(layer_depth - layer_step, 0.0);
                    float relief_weight = before_depth / max(before_depth - after_depth, 0.001);
                    vec2 parallax_uv = mix(relief_uv, relief_uv + uv_step, clamp(relief_weight, 0.0, 1.0));
                    float parallax_height = sample_relief_height(parallax_uv);
                    relief_parallax_amount = clamp(length(parallax_uv - sample_uv) / max(parallax_scale, 0.001), 0.0, 1.0);
                    relief_self_shadow = clamp(
                        ((parallax_height - parallax_valley) * 4.80 + abs(parallax_height - relief_height) * 2.25) * height_effect_max,
                        0.0,
                        0.88
                    );
                    sample_uv = parallax_uv;
                }
                if (render_diagnostic_mode == 6) {
                    gl_FragColor = vec4(fract(abs(sample_uv.x)), fract(abs(sample_uv.y)), 0.35, 1.0);
                    return;
                }
                if (render_diagnostic_mode == 7) {
                    gl_FragColor = vec4(max(protected_average, vec3(0.018)), 1.0);
                    return;
                }
                if (use_texture != 0) {
                    base_color = apply_diffuse_swizzle(texture2D(diffuse_texture, sample_uv), diffuse_swizzle_mode);
                    if (alpha_handling_mode == 2) {
                        base_color.a = 1.0;
                    }
                    if (
                        base_color.a <= 0.01
                        && alpha_handling_mode == 0
                        && (render_diagnostic_mode == 0 || render_diagnostic_mode == 16 || rich_lit)
                    ) {
                        discard;
                    }
                    if (render_diagnostic_mode == 10 || alpha_handling_mode == 3) {
                        gl_FragColor = vec4(vec3(base_color.a), 1.0);
                        return;
                    }
                    if (render_diagnostic_mode == 8 || render_diagnostic_mode == 14 || render_diagnostic_mode == 15 || render_diagnostic_mode == 21) {
                        gl_FragColor = vec4(clamp(base_color.rgb, 0.0, 1.0), 1.0);
                        return;
                    }
                    if (render_diagnostic_mode == 9) {
                        gl_FragColor = vec4(clamp(base_color.rgb, 0.0, 1.0), 1.0);
                        return;
                    }
                    vec3 tint = disable_tint != 0 ? vec3(1.0) : base_texture_tint;
                    float brightness = disable_brightness != 0 ? 1.0 : base_texture_brightness;
                    base_color.rgb = clamp(base_color.rgb * tint * brightness, 0.0, 1.5);
                    base_color.rgb = max(base_color.rgb, vec3(0.018));
                    sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                    if (base_texture_average_luma > 0.075 && sampled_base_luma < base_texture_average_luma * 0.28) {
                        base_color.rgb = mix(base_color.rgb, protected_average, 0.82);
                        sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                    }
                    if (base_texture_average_luma > 0.075) {
                        vec3 average_visibility_floor = clamp((protected_average * 0.88) + vec3(0.055), 0.0, 1.0);
                        base_color.rgb = max(base_color.rgb, average_visibility_floor);
                        sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                    }
                    float material_hint_luma = dot(fallback_vertex_color, vec3(0.2126, 0.7152, 0.0722));
                    float material_hint_sat = color_saturation(fallback_vertex_color);
                    float base_sat = color_saturation(base_color.rgb);
                    if (
                        (render_diagnostic_mode == 0 || render_diagnostic_mode == 16 || rich_lit)
                        && material_hint_luma > 0.055
                        && material_hint_sat > 0.075
                        && sampled_base_luma > 0.045
                        && base_sat < material_hint_sat * 0.65
                    ) {
                        float colorize_amount = clamp((material_hint_sat - base_sat) * 1.25, 0.0, 0.48);
                        base_color.rgb = luma_preserving_colorize(base_color.rgb, fallback_vertex_color, colorize_amount);
                        sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                    }
                    if (
                        rich_lit
                        && material_hint_luma > 0.045
                        && material_hint_sat > 0.040
                        && sampled_base_luma > 0.030
                    ) {
                        float low_authority_drive = base_texture_quality == 1 ? 1.0 : 0.0;
                        float fallback_drive = base_texture_quality == 2 ? 1.0 : 0.0;
                        float flat_base_drive = clamp((sampled_base_luma - 0.78) * 2.2, 0.0, 1.0);
                        flat_base_drive = max(flat_base_drive, clamp((0.13 - base_sat) * 4.0, 0.0, 0.75));
                        float rich_colorize = clamp(
                            (low_authority_drive * 0.58)
                            + (fallback_drive * 0.72)
                            + (flat_base_drive * 0.36)
                            + max(0.0, material_hint_sat - base_sat) * 0.50,
                            0.0,
                            0.78
                        );
                        base_color.rgb = luma_preserving_colorize(base_color.rgb, fallback_vertex_color, rich_colorize);
                        sampled_base_luma = dot(base_color.rgb, vec3(0.2126, 0.7152, 0.0722));
                    }
                } else if (render_diagnostic_mode == 8 || render_diagnostic_mode == 9 || render_diagnostic_mode == 10 || render_diagnostic_mode == 14 || render_diagnostic_mode == 15 || render_diagnostic_mode == 21) {
                    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
                    return;
                }
                vec3 base_visibility_floor = base_color.rgb;
                if (use_texture != 0 && sampled_base_luma > 0.025) {
                    vec3 source_floor = base_color.rgb;
                    if (base_texture_average_luma > sampled_base_luma) {
                        source_floor = max(source_floor, clamp(base_texture_average_color, 0.0, 1.0));
                    }
                    base_visibility_floor = clamp((source_floor * 0.72) + vec3(0.035), 0.0, 1.0);
                }
                if (render_diagnostic_mode == 16) {
                    vec3 diagnostic_base_rgb = max(base_color.rgb, base_visibility_floor);
                    if (use_texture != 0 && base_texture_average_luma > 0.075) {
                        diagnostic_base_rgb = max(
                            diagnostic_base_rgb,
                            protected_average
                        );
                    }
                    gl_FragColor = vec4(clamp(diagnostic_base_rgb, 0.0, 1.0), base_color.a);
                    return;
                }
                vec4 material_sample = vec4(0.5, 0.5, 0.5, 1.0);
                if (use_material_texture != 0) {
                    material_sample = texture2D(material_texture, sample_uv);
                }
                if (render_diagnostic_mode == 11) {
                    vec3 normal_sample = use_normal_texture != 0 ? texture2D(normal_texture, sample_uv).rgb : vec3(0.0);
                    gl_FragColor = vec4(clamp(normal_sample, 0.0, 1.0), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 12) {
                    gl_FragColor = vec4(clamp(material_sample.rgb, 0.0, 1.0), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 13) {
                    float height_sample = use_height_texture != 0 ? texture2D(height_texture, sample_uv).r : 0.0;
                    gl_FragColor = vec4(vec3(clamp(height_sample, 0.0, 1.0)), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 23) {
                    float height_sample = use_height_texture != 0 ? texture2D(height_texture, sample_uv).r : 0.0;
                    float calibrated_sample = use_relief_texture != 0 && height_relief_usable != 0
                        ? sample_relief_height(sample_uv)
                        : height_sample;
                    vec3 inactive_tint = height_relief_usable != 0 ? vec3(1.0) : vec3(0.70, 0.55, 0.92);
                    gl_FragColor = vec4(clamp(vec3(calibrated_sample) * inactive_tint, 0.0, 1.0), 1.0);
                    return;
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
                            if (alpha_handling_mode == 0 && render_diagnostic_mode == 0) {
                                base_color.a = 1.0;
                            } else {
                                discard;
                            }
                        }
                    }
                }
                float material_channel_variance = max(
                    max(abs(material_sample.r - material_sample.g), abs(material_sample.g - material_sample.b)),
                    abs(material_sample.r - material_sample.b)
                );
                float effective_height_effect_max = rich_lit ? height_effect_max : 0.35;
                float effective_specular_max = rich_lit ? specular_max : 0.18;
                float effective_shininess_max = rich_lit ? shininess_max : 72.0;
                float support_depth_drive = clamp(effective_height_effect_max, 0.0, 1.0);
                float support_shine_drive = clamp(effective_specular_max, 0.0, 1.0);
                float support_rough_drive = clamp((effective_shininess_max - 32.0) / 224.0, 0.0, 1.0);
                if (rich_lit && use_high_quality != 0) {
                    float rough_center = material_roughness - 0.5;
                    float rough_contrast_gain = mix(0.45, 4.20, support_rough_drive);
                    material_roughness = clamp(0.5 + (rough_center * rough_contrast_gain), 0.035, 1.0);
                    material_specular = clamp(material_specular * mix(0.55, 3.60, support_shine_drive), 0.0, 1.0);
                    material_metallic = clamp(material_metallic * mix(0.72, 2.10, support_shine_drive), 0.0, 1.0);
                    material_cavity = clamp(material_cavity - (material_channel_variance * support_depth_drive * 0.48), 0.0, 1.0);
                }
                float material_raw_metal_hint = use_material_texture != 0
                    ? clamp(max(material_metallic, max(material_sample.b * 0.72, material_sample.r * 0.28)), 0.0, 1.0)
                    : material_metallic;
                float material_raw_specular_hint = use_material_texture != 0
                    ? clamp(max(material_specular, (material_sample.a * 0.26) + (material_sample.b * 0.22) + (material_channel_variance * 0.40)), 0.0, 1.0)
                    : material_specular;

                float height_value = 0.5;
                if (use_relief_texture != 0) {
                    height_value = (rich_lit && height_relief_usable != 0)
                        ? sample_relief_height(sample_uv)
                        : texture2D(relief_texture, sample_uv).r;
                }
                float relief = clamp((height_value - 0.5) * 2.0, -1.0, 1.0);
                float height_effect = clamp(abs(relief) * effective_height_effect_max, 0.0, effective_height_effect_max);
                float height_edge = 0.0;
                float height_ridge = 0.5;
                if (use_relief_texture != 0) {
                    vec2 height_gradient = vec2(dFdx(height_value), dFdy(height_value));
                    float relief_gradient_gain = rich_lit && height_relief_usable != 0 ? 520.0 : 115.0;
                    height_edge = clamp(length(height_gradient) * mix(20.0, relief_gradient_gain, support_depth_drive), 0.0, 1.0);
                    height_ridge = clamp(0.5 + (relief * mix(0.32, rich_lit ? 5.10 : 1.95, support_depth_drive)), 0.0, 1.0);
                }
                float normal_detail_strength = 0.0;
                float normal_light_delta = 0.0;
                float normal_grazing_detail = 0.0;

                if (render_diagnostic_mode == 17) {
                    float depth_drive = mix(0.12, 1.0, support_depth_drive);
                    float relief_drive = use_relief_texture != 0 ? relief : ((sampled_base_luma - 0.5) * 0.45);
                    float ridge = use_relief_texture != 0
                        ? height_ridge
                        : clamp(0.5 + (relief_drive * mix(0.22, 2.10, depth_drive)), 0.0, 1.0);
                    float relief_edge = max(
                        height_edge,
                        clamp(abs(relief_drive) * mix(0.65, 8.0, depth_drive), 0.0, 1.0)
                    );
                    vec3 depth_low = mix(vec3(0.26, 0.30, 0.36), vec3(0.05, 0.10, 0.20), depth_drive);
                    vec3 depth_high = mix(vec3(0.52, 0.55, 0.58), vec3(1.00, 0.78, 0.18), depth_drive);
                    vec3 depth_color = mix(depth_low, depth_high, ridge);
                    gl_FragColor = vec4(clamp(depth_color + vec3(relief_edge * mix(0.05, 0.36, depth_drive)), 0.0, 1.0), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 18) {
                    float rough_contrast = support_rough_drive;
                    float rough_signal = clamp(mix(material_roughness, material_sample.g, use_material_texture != 0 ? 0.55 : 0.0), 0.0, 1.0);
                    float metal_signal = clamp(max(material_raw_metal_hint, max(material_sample.b, material_sample.r * 0.42)), 0.0, 1.0);
                    float shine_signal = clamp(material_raw_specular_hint * (0.90 + support_shine_drive * 2.40), 0.0, 1.0);
                    float cavity_signal = clamp(min(material_ao, material_cavity) - (material_channel_variance * 0.18), 0.0, 1.0);
                    vec3 raw_mask_color = use_material_texture != 0
                        ? clamp(vec3(material_sample.r, material_sample.g, material_sample.b), 0.0, 1.0)
                        : vec3(0.28, 0.30, 0.34);
                    vec3 decoded_color = vec3(
                        mix(0.16, cavity_signal, 0.72),
                        mix(0.18, 1.0 - rough_signal, 0.60 + (rough_contrast * 0.26)),
                        clamp((metal_signal * (0.58 + support_shine_drive * 0.92)) + (shine_signal * 0.58), 0.0, 1.0)
                    );
                    vec3 response_color = mix(decoded_color, raw_mask_color, use_material_texture != 0 ? 0.52 : 0.0);
                    response_color += vec3(material_channel_variance * (0.16 + rough_contrast * 0.42));
                    response_color.r += metal_signal * 0.16;
                    response_color.b += shine_signal * 0.14;
                    gl_FragColor = vec4(clamp(response_color, 0.0, 1.0), 1.0);
                    return;
                }
                if (render_diagnostic_mode == 20) {
                    float rough_contrast = support_rough_drive;
                    float raw_rough_hint = use_material_texture != 0
                        ? clamp(mix(material_roughness, material_sample.g, 0.60) + ((material_channel_variance - 0.10) * 0.82), 0.0, 1.0)
                        : material_roughness;
                    float centered_rough = (raw_rough_hint - 0.5) * mix(0.95, 4.80, rough_contrast);
                    float rough_response = smoothstep(0.0, 1.0, clamp(0.5 + centered_rough, 0.0, 1.0));
                    float rough_detail = clamp(material_channel_variance * mix(0.28, 1.20, rough_contrast), 0.0, 1.0);
                    vec3 rough_low = mix(vec3(0.20, 0.24, 0.30), vec3(0.05, 0.14, 0.45), rough_contrast);
                    vec3 rough_high = mix(vec3(0.62, 0.62, 0.56), vec3(1.00, 0.84, 0.22), rough_contrast);
                    vec3 rough_color = mix(rough_low, rough_high, rough_response);
                    rough_color += vec3(rough_detail * 0.18, rough_detail * 0.14, rough_detail * 0.05);
                    gl_FragColor = vec4(clamp(rough_color, 0.0, 1.0), 1.0);
                    return;
                }

                if (use_high_quality != 0 && use_normal_texture != 0) {
                    vec3 tangent = safe_normalize(frag_tangent, vec3(1.0, 0.0, 0.0));
                    vec3 bitangent = safe_normalize(frag_bitangent, vec3(0.0, 1.0, 0.0));
                    vec3 unperturbed_normal = surface_normal;
                    vec3 sampled_normal = texture2D(normal_texture, sample_uv).xyz * 2.0 - 1.0;
                    sampled_normal.y = -sampled_normal.y;
                    float mapped_strength = clamp(
                        max(normal_texture_strength, normal_strength_floor),
                        0.0,
                        normal_strength_cap
                    );
                    float strength_ratio = clamp(mapped_strength / max(normal_strength_cap, 0.001), 0.0, 1.0);
                    float rich_normal_gain = rich_lit ? mix(1.05, 2.25, support_depth_drive) : 1.0;
                    sampled_normal.xy *= mix(0.75, 1.35, strength_ratio) * rich_normal_gain;
                    sampled_normal.xy *= mapped_strength;
                    vec2 normal_xy = sampled_normal.xy;
                    sampled_normal = safe_normalize(sampled_normal, vec3(0.0, 0.0, 1.0));
                    mat3 tbn = mat3(tangent, bitangent, surface_normal);
                    vec3 mapped_normal = safe_normalize(tbn * sampled_normal, surface_normal);
                    vec3 normal_probe_light = safe_normalize(vec3(0.20, 0.45, 1.0), vec3(0.20, 0.45, 1.0));
                    normal_detail_strength = clamp(length(normal_xy) * mix(0.55, rich_lit ? 1.45 : 1.05, strength_ratio), 0.0, 1.0);
                    normal_light_delta = clamp(dot(mapped_normal, normal_probe_light) - dot(unperturbed_normal, normal_probe_light), -1.0, 1.0);
                    normal_grazing_detail = clamp(length(mapped_normal - unperturbed_normal) * mix(0.45, rich_lit ? 1.35 : 0.95, strength_ratio), 0.0, 1.0);
                    surface_normal = safe_normalize(
                        mix(surface_normal, mapped_normal, clamp(mapped_strength * (rich_lit ? mix(1.05, 1.45, support_depth_drive) : 0.92), 0.0, 0.98)),
                        surface_normal
                    );
                }
                if (rich_lit && use_high_quality != 0 && use_relief_texture != 0 && height_relief_usable != 0) {
                    vec2 relief_gradient = vec2(dFdx(height_value), dFdy(height_value));
                    vec2 relief_texel = vec2(0.0025, 0.0025);
                    vec2 relief_uv_gradient = vec2(
                        sample_relief_height(sample_uv + vec2(relief_texel.x, 0.0)) - sample_relief_height(sample_uv - vec2(relief_texel.x, 0.0)),
                        sample_relief_height(sample_uv + vec2(0.0, relief_texel.y)) - sample_relief_height(sample_uv - vec2(0.0, relief_texel.y))
                    );
                    relief_gradient += relief_uv_gradient * mix(5.0, 30.0, support_depth_drive);
                    vec3 relief_normal = safe_normalize(
                        surface_normal + vec3(-relief_gradient.x, -relief_gradient.y, height_edge * 0.32) * clamp(effective_height_effect_max * 1.90, 0.0, 1.90),
                        surface_normal
                    );
                    surface_normal = safe_normalize(mix(surface_normal, relief_normal, clamp(effective_height_effect_max * 1.45, 0.0, 0.98)), surface_normal);
                }

                vec3 main_light = safe_normalize(light_direction, vec3(0.20, 0.45, 1.0));
                vec3 fill_light = safe_normalize(vec3(-main_light.x * 0.35, 0.45, -main_light.z * 0.35), vec3(-0.25, 0.55, -0.25));
                vec3 rim_light = safe_normalize(vec3(-0.10, 0.32, -1.0), vec3(-0.10, 0.32, -1.0));
                if (rich_lit && use_high_quality != 0 && use_relief_texture != 0 && height_relief_usable != 0 && height_effect_max > 0.001) {
                    vec2 light_relief_dir = vec2(dot(main_light, preview_tangent), dot(main_light, preview_bitangent));
                    float light_relief_len = length(light_relief_dir);
                    if (light_relief_len > 0.001) {
                        light_relief_dir /= light_relief_len;
                        float light_probe_height = sample_relief_height(sample_uv + light_relief_dir * height_effect_max * 0.120);
                        relief_self_shadow = max(
                            relief_self_shadow,
                            clamp((height_value - light_probe_height) * effective_height_effect_max * 7.25, 0.0, 0.88)
                        );
                    }
                }
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
                        mix(material_ao, min(material_ao, material_cavity), rich_lit ? 0.68 : 0.45)
                        - (abs(relief) * (0.10 + (effective_height_effect_max * (rich_lit ? 0.34 : 0.16)))),
                        0.0,
                        1.0
                    );
                    float cavity_scale = clamp(
                        mix(cavity_clamp_min, cavity_clamp_max, occlusion_drive),
                        cavity_clamp_min,
                        cavity_clamp_max
                    );
                    lighting *= mix(1.0, cavity_scale, (use_material_texture != 0 || use_relief_texture != 0) ? (rich_lit ? 0.58 : 0.22) : 0.06);
                    lighting *= 1.0 - (relief_self_shadow * (rich_lit ? mix(0.70, 1.10, support_depth_drive) : 0.0));
                    lighting += height_effect * (rich_lit ? 1.12 : 0.24) + height_effect * primary_diffuse * (rich_lit ? 0.92 : 0.18);
                    lighting += relief_parallax_amount * support_depth_drive * (rich_lit ? 0.48 : 0.0);
                    lighting += height_edge * ((rich_lit ? 0.34 : 0.05) + (support_depth_drive * (rich_lit ? 1.08 : 0.22)));
                    if (use_normal_texture != 0) {
                        float normal_shadow = max(0.0, -normal_light_delta);
                        float normal_highlight = max(0.0, normal_light_delta);
                        lighting *= clamp(1.0 - (normal_shadow * (rich_lit ? 0.56 : 0.34)), 0.42, 1.0);
                        lighting += normal_highlight * (rich_lit ? 0.48 : 0.30);
                        lighting += normal_detail_strength * (rich_lit ? 0.18 : 0.10);
                    }
                }
                if (disable_lighting != 0) {
                    gl_FragColor = vec4(clamp(base_color.rgb, 0.0, 1.0), base_color.a);
                    return;
                }

                vec3 view_dir = safe_normalize(camera_position - frag_position, vec3(0.0, 0.0, 1.0));
                vec3 half_dir = safe_normalize(main_light + view_dir, main_light);
                float view_facing = clamp(dot(surface_normal, view_dir), 0.0, 1.0);
                float specular = 0.0;
                float rim_specular = 0.0;
                vec3 specular_color = vec3(1.0);
                if (use_high_quality != 0) {
                    float material_shine = max(max(material_specular, material_metallic * 0.78), (1.0 - material_roughness) * 0.34);
                    if (rich_lit) {
                        float mask_detail = clamp(material_channel_variance * 0.78 + height_edge * 0.52, 0.0, 0.84);
                        material_shine = clamp(
                            material_shine
                            + (material_raw_specular_hint * mix(0.08, 0.68, support_shine_drive))
                            + mask_detail * mix(0.26, 1.05, support_shine_drive),
                            0.0,
                            1.0
                        );
                    }
                    float specular_mask = clamp(
                        mix(specular_base, effective_specular_max, material_shine) * (rich_lit ? mix(0.85, 3.30, support_shine_drive) : 1.0),
                        specular_min,
                        rich_lit ? min(1.0, max(effective_specular_max, 0.08) * 3.20) : effective_specular_max
                    );
                    float shininess = clamp(
                        mix(effective_shininess_max, shininess_min, material_roughness) + (height_effect * height_shininess_boost * (rich_lit ? 1.25 : 1.0)),
                        shininess_min,
                        effective_shininess_max
                    );
                    float fresnel = pow(1.0 - view_facing, rich_lit ? 3.2 : 4.0);
                    float rim_response = pow(1.0 - view_facing, rich_lit ? 2.0 : 2.4);
                    specular_color = mix(
                        vec3(1.0),
                        clamp((base_color.rgb * 1.15) + vec3(0.08), 0.0, 1.0),
                        material_metallic * 0.68
                    );
                    specular = pow(max(dot(surface_normal, half_dir), 0.0), shininess) * specular_mask;
                    specular += fresnel * (0.035 + (material_specular * (rich_lit ? mix(0.10, 0.58, support_shine_drive) : 0.22)) + (material_metallic * (rich_lit ? mix(0.08, 0.42, support_shine_drive) : 0.18)));
                    specular += pow(max(dot(surface_normal, view_dir), 0.0), max(4.0, shininess * 0.35)) * material_metallic * specular_mask * (rich_lit ? mix(0.12, 0.62, support_shine_drive) : 0.24);
                    specular += height_edge * material_shine * (rich_lit ? mix(0.015, 0.12, support_shine_drive) : 0.0);
                    rim_specular = rim_response * (0.025 + (material_specular * (rich_lit ? mix(0.04, 0.22, support_shine_drive) : 0.08)) + (material_metallic * (rich_lit ? mix(0.03, 0.13, support_shine_drive) : 0.05)));
                }
                if (render_diagnostic_mode == 19) {
                    float shine_drive = support_shine_drive;
                    vec3 shine_base = clamp((base_color.rgb * 0.46) + vec3(0.28, 0.31, 0.36), 0.0, 1.0);
                    vec3 shine_color = mix(vec3(0.24, 0.28, 0.34), shine_base, 0.70 + (material_raw_metal_hint * (0.20 + shine_drive * 0.24)));
                    float material_response = clamp(
                        (material_raw_specular_hint * (0.62 + shine_drive * 2.20))
                        + (material_raw_metal_hint * (0.36 + shine_drive * 1.35))
                        + ((1.0 - material_roughness) * shine_drive * 0.55),
                        0.0,
                        1.0
                    );
                    float shine_response = clamp(
                        ((specular + rim_specular) * (0.30 + (shine_drive * 4.80)))
                        + (material_raw_specular_hint * shine_drive * 1.55)
                        + (material_raw_metal_hint * shine_drive * 0.95)
                        + (material_channel_variance * shine_drive * 0.62)
                        + (height_edge * shine_drive * 0.22),
                        0.0,
                        1.0
                    );
                    shine_response = max(shine_response, material_response);
                    float floor_light = mix(0.58, 0.76, shine_drive);
                    gl_FragColor = vec4(
                        clamp(
                            (shine_color * (floor_light + (shine_response * 0.92)))
                            + vec3(shine_response * mix(0.22, 0.96, shine_drive)),
                            0.0,
                            1.0
                        ),
                        1.0
                    );
                    return;
                }

                float smoothness = clamp(1.0 - material_roughness, 0.0, 1.0);
                float broad_sheen = 0.0;
                if (use_high_quality != 0) {
                    broad_sheen = clamp(
                        (material_raw_specular_hint * 0.42)
                        + (material_raw_metal_hint * 0.46)
                        + (smoothness * 0.26)
                        + (material_channel_variance * 0.18),
                        0.0,
                        1.0
                    ) * ((rich_lit ? 0.34 : 0.22) + (support_shine_drive * (rich_lit ? 1.55 : 1.20)));
                    broad_sheen = clamp(broad_sheen, 0.0, 1.0);
                }
                vec3 final_rgb = base_color.rgb * clamp(lighting, 0.72, 1.58);
                if (use_high_quality != 0) {
                    float relief_tone = (height_ridge - 0.5) * support_depth_drive;
                    final_rgb *= clamp(
                        1.0
                        + (relief_tone * (rich_lit ? 0.58 : 0.26))
                        + ((smoothness - 0.5) * support_rough_drive * (rich_lit ? 0.18 : 0.12)),
                        0.72,
                        rich_lit ? 1.52 : 1.30
                    );
                    final_rgb += vec3(height_edge * support_depth_drive * (rich_lit ? 0.165 : 0.075));
                    if (rich_lit) {
                        float relief_detail = use_relief_texture != 0 && height_relief_usable != 0
                            ? ((height_ridge - 0.5) * 2.0)
                            : ((sampled_base_luma - base_texture_average_luma) * 1.35);
                        float relief_visibility_gain = clamp(0.18 + (support_depth_drive * 0.76), 0.0, 0.94);
                        vec3 relief_visual_rgb = final_rgb
                            * clamp(1.0 + (relief_detail * mix(0.42, 1.12, support_depth_drive)), 0.42, 1.72);
                        relief_visual_rgb -= vec3(relief_self_shadow * mix(0.20, 0.62, support_depth_drive));
                        relief_visual_rgb += vec3(height_edge * mix(0.16, 0.46, support_depth_drive));
                        relief_visual_rgb += vec3(relief_parallax_amount * support_depth_drive * 0.18);
                        final_rgb = mix(final_rgb, relief_visual_rgb, relief_visibility_gain);
                        final_rgb *= clamp(1.0 - (relief_self_shadow * mix(0.22, 0.58, support_depth_drive)), 0.42, 1.0);
                        final_rgb += vec3(relief_parallax_amount * support_depth_drive * 0.12);
                    }
                    if (use_normal_texture != 0) {
                        float normal_texture_contrast = normal_detail_strength * (rich_lit ? 0.36 : 0.22);
                        float normal_ridge_light = max(0.0, normal_light_delta) * (rich_lit ? 0.34 : 0.22);
                        float normal_micro_shadow = max(0.0, -normal_light_delta) * (rich_lit ? 0.42 : 0.26);
                        vec3 normal_detail_rgb = final_rgb;
                        normal_detail_rgb *= clamp(1.0 + (normal_light_delta * (rich_lit ? 0.72 : 0.46)), 0.48, 1.62);
                        normal_detail_rgb *= clamp(1.0 - normal_micro_shadow, 0.52, 1.0);
                        normal_detail_rgb += vec3((normal_texture_contrast * 0.10) + normal_ridge_light);
                        final_rgb = mix(final_rgb, normal_detail_rgb, clamp(0.35 + normal_grazing_detail, 0.0, rich_lit ? 0.88 : 0.62));
                    }
                    final_rgb += specular_color * broad_sheen * (
                        (rich_lit ? 0.045 : 0.030)
                        + (primary_diffuse * (rich_lit ? 0.050 : 0.035))
                        + (pow(1.0 - view_facing, 1.55) * (rich_lit ? 0.26 : 0.18))
                    );
                    final_rgb = mix(
                        final_rgb,
                        luma_preserving_colorize(final_rgb, specular_color, broad_sheen * material_metallic * (rich_lit ? 0.32 : 0.22)),
                        support_shine_drive
                    );
                }
                final_rgb += specular_color * specular;
                final_rgb += specular_color * rim_specular;
                if (use_texture != 0 && sampled_base_luma > 0.025) {
                    final_rgb = max(final_rgb, base_visibility_floor);
                }
                if (use_texture != 0 && sampled_base_luma > 0.055) {
                    final_rgb = max(final_rgb, base_visibility_floor);
                    float output_luma = dot(final_rgb, vec3(0.2126, 0.7152, 0.0722));
                    float protected_luma = max(0.070, max(sampled_base_luma, base_texture_average_luma) * 0.66);
                    if (output_luma < protected_luma) {
                        final_rgb += vec3(protected_luma - output_luma);
                    }
                }
                if (
                    rich_lit
                    && use_high_quality != 0
                    && use_relief_texture != 0
                    && height_relief_usable != 0
                    && support_depth_drive > 0.001
                ) {
                    float relief_depth_drive = clamp(support_depth_drive, 0.0, 1.0);
                    float relief_shine_drive = clamp(support_shine_drive, 0.0, 1.0);
                    float relief_contrast_drive = clamp(support_rough_drive, 0.0, 1.0);
                    vec2 fine_texel = vec2(mix(0.0014, 0.0048, relief_depth_drive));
                    vec2 broad_texel = vec2(mix(0.0048, 0.0150, relief_depth_drive));
                    float h_center = height_value;
                    float h_left = sample_relief_height(sample_uv - vec2(fine_texel.x, 0.0));
                    float h_right = sample_relief_height(sample_uv + vec2(fine_texel.x, 0.0));
                    float h_down = sample_relief_height(sample_uv - vec2(0.0, fine_texel.y));
                    float h_up = sample_relief_height(sample_uv + vec2(0.0, fine_texel.y));
                    float h_left_b = sample_relief_height(sample_uv - vec2(broad_texel.x, 0.0));
                    float h_right_b = sample_relief_height(sample_uv + vec2(broad_texel.x, 0.0));
                    float h_down_b = sample_relief_height(sample_uv - vec2(0.0, broad_texel.y));
                    float h_up_b = sample_relief_height(sample_uv + vec2(0.0, broad_texel.y));
                    vec2 fine_gradient = vec2(h_right - h_left, h_up - h_down);
                    vec2 broad_gradient = vec2(h_right_b - h_left_b, h_up_b - h_down_b);
                    float fine_slope = length(fine_gradient);
                    float broad_slope = length(broad_gradient);
                    float relief_slope = clamp(
                        (fine_slope * mix(5.0, 42.0, relief_depth_drive))
                        + (broad_slope * mix(3.0, 24.0, relief_depth_drive)),
                        0.0,
                        1.0
                    );
                    float fine_curve = ((h_left + h_right + h_down + h_up) * 0.25) - h_center;
                    float broad_curve = ((h_left_b + h_right_b + h_down_b + h_up_b) * 0.25) - h_center;
                    vec2 relief_light_dir = vec2(dot(main_light, preview_tangent), dot(main_light, preview_bitangent));
                    if (length(relief_light_dir) <= 0.001) {
                        relief_light_dir = vec2(0.45, 0.70);
                    }
                    relief_light_dir = normalize(relief_light_dir);
                    float directional_relief = dot(normalize((fine_gradient * 2.4) + broad_gradient + vec2(0.0001)), relief_light_dir);
                    float relief_cavity = clamp(
                        max(0.0, fine_curve * mix(4.0, 18.0, relief_depth_drive))
                        + max(0.0, broad_curve * mix(3.0, 14.0, relief_depth_drive))
                        + (relief_slope * mix(0.06, 0.32, relief_contrast_drive)),
                        0.0,
                        1.0
                    );
                    float relief_ridge = clamp(
                        max(0.0, -fine_curve * mix(3.0, 15.0, relief_depth_drive))
                        + max(0.0, directional_relief * mix(0.20, 1.05, relief_depth_drive))
                        + (relief_slope * mix(0.03, 0.22, relief_shine_drive)),
                        0.0,
                        1.0
                    );
                    float relief_local_contrast = clamp(
                        (h_center - 0.5) * mix(0.35, 1.55, relief_depth_drive) * mix(0.65, 1.55, relief_contrast_drive),
                        -0.85,
                        0.85
                    );
                    vec3 relief_emboss_rgb = final_rgb;
                    relief_emboss_rgb *= clamp(1.0 + relief_local_contrast, 0.34, 1.92);
                    relief_emboss_rgb *= clamp(1.0 - (relief_cavity * mix(0.26, 0.78, relief_depth_drive)), 0.26, 1.0);
                    relief_emboss_rgb += vec3(relief_ridge * mix(0.10, 0.42, relief_depth_drive));
                    relief_emboss_rgb += specular_color * relief_ridge * relief_shine_drive * mix(0.08, 0.56, relief_shine_drive);
                    relief_emboss_rgb += specular_color * pow(clamp(relief_slope, 0.0, 1.0), mix(2.2, 0.65, relief_shine_drive)) * relief_shine_drive * 0.22;
                    final_rgb = mix(final_rgb, relief_emboss_rgb, mix(0.32, 0.92, relief_depth_drive));
                    final_rgb = max(final_rgb, base_visibility_floor * mix(0.82, 0.48, relief_depth_drive));
                }
                if (rich_lit && use_high_quality != 0) {
                    float radical_depth = clamp(support_depth_drive, 0.0, 1.0);
                    float radical_shine = clamp(support_shine_drive, 0.0, 1.0);
                    float radical_contrast = clamp(support_rough_drive, 0.0, 1.0);
                    float radical_center = use_relief_texture != 0 && height_relief_usable != 0
                        ? height_value
                        : clamp(sampled_base_luma, 0.0, 1.0);
                    vec2 radical_texel = vec2(mix(0.0020, 0.0180, radical_depth));
                    float radical_l = use_relief_texture != 0 && height_relief_usable != 0
                        ? sample_relief_height(sample_uv - vec2(radical_texel.x, 0.0))
                        : clamp(radical_center - dFdx(sampled_base_luma) * 3.0, 0.0, 1.0);
                    float radical_r = use_relief_texture != 0 && height_relief_usable != 0
                        ? sample_relief_height(sample_uv + vec2(radical_texel.x, 0.0))
                        : clamp(radical_center + dFdx(sampled_base_luma) * 3.0, 0.0, 1.0);
                    float radical_d = use_relief_texture != 0 && height_relief_usable != 0
                        ? sample_relief_height(sample_uv - vec2(0.0, radical_texel.y))
                        : clamp(radical_center - dFdy(sampled_base_luma) * 3.0, 0.0, 1.0);
                    float radical_u = use_relief_texture != 0 && height_relief_usable != 0
                        ? sample_relief_height(sample_uv + vec2(0.0, radical_texel.y))
                        : clamp(radical_center + dFdy(sampled_base_luma) * 3.0, 0.0, 1.0);
                    float radical_average = (radical_l + radical_r + radical_d + radical_u) * 0.25;
                    vec2 radical_gradient = vec2(radical_r - radical_l, radical_u - radical_d);
                    vec2 radical_light = vec2(dot(main_light, preview_tangent), dot(main_light, preview_bitangent));
                    if (length(radical_light) <= 0.001) {
                        radical_light = vec2(0.55, 0.80);
                    }
                    radical_light = normalize(radical_light);
                    float radical_slope = clamp(length(radical_gradient) * mix(18.0, 125.0, radical_depth), 0.0, 1.0);
                    float radical_direction = dot(normalize(radical_gradient + vec2(0.0001)), radical_light);
                    float radical_cavity = clamp(
                        max(0.0, radical_average - radical_center) * mix(10.0, 48.0, radical_depth)
                        + radical_slope * mix(0.12, 0.62, radical_contrast),
                        0.0,
                        1.0
                    );
                    float radical_ridge = clamp(
                        max(0.0, radical_center - radical_average) * mix(8.0, 38.0, radical_depth)
                        + max(0.0, radical_direction) * mix(0.20, 1.20, radical_depth)
                        + radical_slope * mix(0.10, 0.70, radical_shine),
                        0.0,
                        1.0
                    );
                    float radical_cut = smoothstep(
                        mix(0.05, 0.28, radical_contrast),
                        mix(0.18, 0.74, radical_contrast),
                        radical_slope + abs(radical_center - 0.5) * radical_depth
                    );
                    float radical_luma = dot(final_rgb, vec3(0.2126, 0.7152, 0.0722));
                    vec3 radical_chiseled = final_rgb;
                    radical_chiseled *= clamp(1.0 - radical_cavity * mix(0.55, 1.55, radical_depth), 0.08, 1.0);
                    radical_chiseled += vec3(radical_ridge * mix(0.28, 1.15, radical_depth));
                    radical_chiseled = mix(
                        vec3(radical_luma),
                        radical_chiseled,
                        mix(1.0, 1.85, radical_contrast)
                    );
                    radical_chiseled += specular_color * pow(max(radical_ridge, radical_slope), mix(2.2, 0.38, radical_shine)) * radical_shine * 0.95;
                    radical_chiseled += vec3(radical_cut * radical_depth * mix(0.00, 0.42, radical_shine));
                    float radical_mix = clamp(0.18 + radical_depth * 0.92, 0.0, 1.0);
                    final_rgb = mix(final_rgb, radical_chiseled, radical_mix);
                    final_rgb *= clamp(1.0 + (radical_center - 0.5) * mix(0.25, 1.65, radical_depth) * mix(0.8, 1.8, radical_contrast), 0.16, 2.25);
                }
                gl_FragColor = vec4(clamp(final_rgb, 0.0, 1.65), base_color.a);
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
        program.bindAttributeLocation("smooth_normal", 6)
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
        self._relief_texture_sampler_uniform_location = program.uniformLocation("relief_texture")
        self._use_texture_uniform_location = program.uniformLocation("use_texture")
        self._render_diagnostic_mode_uniform_location = program.uniformLocation("render_diagnostic_mode")
        self._alpha_handling_mode_uniform_location = program.uniformLocation("alpha_handling_mode")
        self._diffuse_swizzle_mode_uniform_location = program.uniformLocation("diffuse_swizzle_mode")
        self._disable_tint_uniform_location = program.uniformLocation("disable_tint")
        self._disable_brightness_uniform_location = program.uniformLocation("disable_brightness")
        self._disable_uv_scale_uniform_location = program.uniformLocation("disable_uv_scale")
        self._disable_lighting_uniform_location = program.uniformLocation("disable_lighting")
        self._render_build_marker_uniform_location = program.uniformLocation("render_build_marker")
        self._base_texture_tint_uniform_location = program.uniformLocation("base_texture_tint")
        self._base_texture_brightness_uniform_location = program.uniformLocation("base_texture_brightness")
        self._base_texture_uv_scale_uniform_location = program.uniformLocation("base_texture_uv_scale")
        self._base_texture_average_color_uniform_location = program.uniformLocation("base_texture_average_color")
        self._base_texture_average_luma_uniform_location = program.uniformLocation("base_texture_average_luma")
        self._base_texture_quality_uniform_location = program.uniformLocation("base_texture_quality")
        self._use_high_quality_uniform_location = program.uniformLocation("use_high_quality")
        self._use_normal_texture_uniform_location = program.uniformLocation("use_normal_texture")
        self._normal_texture_strength_uniform_location = program.uniformLocation("normal_texture_strength")
        self._use_material_texture_uniform_location = program.uniformLocation("use_material_texture")
        self._material_decode_mode_uniform_location = program.uniformLocation("material_decode_mode")
        self._use_height_texture_uniform_location = program.uniformLocation("use_height_texture")
        self._use_relief_texture_uniform_location = program.uniformLocation("use_relief_texture")
        self._diffuse_wrap_bias_uniform_location = program.uniformLocation("diffuse_wrap_bias")
        self._diffuse_light_scale_uniform_location = program.uniformLocation("diffuse_light_scale")
        self._normal_strength_cap_uniform_location = program.uniformLocation("normal_strength_cap")
        self._normal_strength_floor_uniform_location = program.uniformLocation("normal_strength_floor")
        self._height_effect_max_uniform_location = program.uniformLocation("height_effect_max")
        self._height_sample_min_uniform_location = program.uniformLocation("height_sample_min")
        self._height_sample_max_uniform_location = program.uniformLocation("height_sample_max")
        self._height_sample_contrast_uniform_location = program.uniformLocation("height_sample_contrast")
        self._height_relief_usable_uniform_location = program.uniformLocation("height_relief_usable")
        self._relief_source_code_uniform_location = program.uniformLocation("relief_source_code")
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
        settings = self.render_settings()
        if hasattr(self._functions, "glViewport"):
            try:
                device_pixel_ratio = float(self.devicePixelRatioF()) if hasattr(self, "devicePixelRatioF") else 1.0
                self._functions.glViewport(
                    0,
                    0,
                    max(1, int(round(self.width() * device_pixel_ratio))),
                    max(1, int(round(self.height() * device_pixel_ratio))),
                )
            except Exception:
                pass
        if bool(getattr(settings, "disable_depth_test", False)):
            self._functions.glDisable(_GL_DEPTH_TEST)
        else:
            self._functions.glEnable(_GL_DEPTH_TEST)
        self._functions.glDisable(_GL_CULL_FACE)
        if hasattr(self._functions, "glDisable"):
            self._functions.glDisable(_GL_BLEND)
            self._functions.glDisable(_GL_SCISSOR_TEST)
        if hasattr(self._functions, "glColorMask"):
            try:
                self._functions.glColorMask(True, True, True, True)
            except Exception:
                pass
        if hasattr(self._functions, "glDepthMask"):
            try:
                self._functions.glDepthMask(True)
            except Exception:
                pass
        if hasattr(self._functions, "glDepthFunc"):
            try:
                self._functions.glDepthFunc(_GL_LESS)
            except Exception:
                pass
        if hasattr(self._functions, "glActiveTexture"):
            try:
                self._functions.glActiveTexture(_GL_TEXTURE0)
            except Exception:
                pass
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
        base_model = self._preview_base_model_matrix()
        base_mvp = projection * view * base_model
        live_translation = self._alignment_translation_display_vector()
        has_live_translation = live_translation.lengthSquared() > 1e-12
        live_rotation = QVector3D(self._alignment_live_rotation)
        has_live_rotation = live_rotation.lengthSquared() > 1e-12
        rotation_origin = self._alignment_handle_origin(include_live_translation=False) if has_live_rotation else (0.0, 0.0, 0.0)

        self._program.bind()
        self._program.setUniformValue(self._camera_uniform_location, QVector3D(0.0, 0.0, self._distance))
        self._program.setUniformValue(self._light_uniform_location, QVector3D(0.20, 0.45, 1.0))
        self._program.setUniformValue(self._ambient_uniform_location, max(0.62, float(settings.ambient_strength)))
        self._program.setUniformValue(self._texture_sampler_uniform_location, 0)
        self._program.setUniformValue(self._normal_texture_sampler_uniform_location, 1)
        self._program.setUniformValue(self._material_texture_sampler_uniform_location, 2)
        self._program.setUniformValue(self._height_texture_sampler_uniform_location, 3)
        self._program.setUniformValue(self._relief_texture_sampler_uniform_location, 4)
        render_mode = str(getattr(settings, "render_diagnostic_mode", "lit") or "lit").strip().lower()
        render_mode_code = int(_RENDER_DIAGNOSTIC_MODE_CODES.get(render_mode, 0))
        self._program.setUniformValue(
            self._render_diagnostic_mode_uniform_location,
            render_mode_code,
        )
        self._program.setUniformValue(
            self._alpha_handling_mode_uniform_location,
            int(_ALPHA_HANDLING_MODE_CODES.get(str(settings.alpha_handling_mode), 0)),
        )
        self._program.setUniformValue(
            self._diffuse_swizzle_mode_uniform_location,
            int(_DIFFUSE_SWIZZLE_MODE_CODES.get(str(settings.diffuse_swizzle_mode), 0)),
        )
        self._program.setUniformValue(self._disable_tint_uniform_location, int(bool(settings.disable_tint)))
        self._program.setUniformValue(self._disable_brightness_uniform_location, int(bool(settings.disable_brightness)))
        self._program.setUniformValue(self._disable_uv_scale_uniform_location, int(bool(settings.disable_uv_scale)))
        self._program.setUniformValue(self._disable_lighting_uniform_location, int(bool(settings.disable_lighting)))
        self._program.setUniformValue(self._render_build_marker_uniform_location, 0.4252)
        self._gl_error_text()
        self._vertex_array.bind()
        active_transformed_state: Optional[bool] = None
        current_meshes = getattr(getattr(self, "_current_model", None), "meshes", None) or []
        runtime_diagnostics: Dict[int, _BatchRenderDiagnostic] = {}
        solo_batch_index = int(getattr(settings, "solo_batch_index", -1) or -1)
        for batch_index, batch in enumerate(self._mesh_batches):
            if solo_batch_index >= 0 and batch_index != solo_batch_index:
                continue
            transformed_batch = bool((has_live_translation or has_live_rotation) and self._alignment_batch_is_editable(batch_index))
            if transformed_batch:
                batch_model = self._apply_alignment_live_transform(base_model, live_translation, live_rotation, rotation_origin)
            else:
                batch_model = base_model
            if active_transformed_state is None or transformed_batch != active_transformed_state:
                self._program.setUniformValue(self._model_uniform_location, batch_model)
                self._program.setUniformValue(self._mvp_uniform_location, projection * view * batch_model if transformed_batch else base_mvp)
                active_transformed_state = transformed_batch
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
            derived_relief_key = self._batch_derived_relief_keys.get(batch_index, "")
            derived_relief_texture = self._texture_objects.get(
                (derived_relief_key, batch.texture_wrap_repeat, batch.texture_flip_vertical)
            )
            diagnostic_source = self._diffuse_probe_source_for_render_mode(settings, render_mode)
            diagnostic_texture = self._diagnostic_texture_for_source(
                diagnostic_source,
                base=diffuse_texture,
                normal=normal_texture,
                material=material_texture,
                height=height_texture,
            )
            diffuse_draw_texture = diagnostic_texture if render_mode_code in {14, 15, 21} else diffuse_texture
            diffuse_unit = self._forced_sampler_unit(settings, render_mode)
            use_texture = int(
                bool(
                    self._use_textures
                    and batch.has_texture_coordinates
                    and bool(batch.texture_key or diagnostic_source != "base")
                    and diffuse_draw_texture is not None
                    and self._texture_created(diffuse_draw_texture)
                    and self._texture_id(diffuse_draw_texture) > 0
                )
            )
            use_high_quality_maps = bool(
                self._high_quality_textures
                and batch.has_texture_coordinates
            )
            support_maps_enabled = bool(
                use_high_quality_maps
                and not batch.support_maps_disabled
                and not bool(getattr(settings, "disable_all_support_maps", False))
            )
            use_normal_texture = int(
                bool(
                    support_maps_enabled
                    and not bool(getattr(settings, "disable_normal_map", False))
                    and batch.normal_texture_key
                    and normal_texture is not None
                    and float(batch.normal_texture_strength) > 0.0
                )
            )
            use_material_texture = int(
                bool(
                    support_maps_enabled
                    and not bool(getattr(settings, "disable_material_map", False))
                    and batch.material_texture_key
                    and material_texture is not None
                )
            )
            use_height_texture = int(
                bool(
                    support_maps_enabled
                    and not bool(getattr(settings, "disable_height_map", False))
                    and batch.height_texture_key
                    and height_texture is not None
                )
            )
            upload_key = (batch.texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
            upload_diagnostic = self._texture_upload_diagnostics.get(upload_key)
            normal_upload = self._texture_upload_diagnostics.get(
                (batch.normal_texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
            )
            material_upload = self._texture_upload_diagnostics.get(
                (batch.material_texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
            )
            height_upload = self._texture_upload_diagnostics.get(
                (batch.height_texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
            )
            mesh = current_meshes[batch.mesh_index] if 0 <= batch.mesh_index < len(current_meshes) else None
            position_count = len(getattr(mesh, "positions", ()) or ()) if mesh is not None else 0
            uv_count = len(getattr(mesh, "texture_coordinates", ()) or ()) if mesh is not None else 0
            luma = self._batch_luma_diagnostics.get(batch_index)
            material_luma = self._batch_material_luma_diagnostics.get(batch_index)
            height_luma = self._batch_height_luma_diagnostics.get(batch_index)
            derived_relief_luma = self._batch_derived_relief_luma_diagnostics.get(batch_index)
            normal_strength = self._batch_normal_strength_diagnostics.get(batch_index)
            enhanced_state, enhanced_reason, enhanced_height_usable, relief_source = self._enhanced_relief_status(
                render_mode_code=render_mode_code,
                high_quality_enabled=bool(use_high_quality_maps),
                support_maps_enabled=bool(support_maps_enabled),
                support_maps_disabled=bool(batch.support_maps_disabled),
                height_key=str(batch.height_texture_key or ""),
                height_texture_available=bool(height_texture is not None),
                height_luma=height_luma,
                derived_relief_key=derived_relief_key,
                derived_relief_texture_available=bool(derived_relief_texture is not None),
                derived_relief_luma=derived_relief_luma,
                height_map_disabled=bool(getattr(settings, "disable_height_map", False)),
                height_effect_max=float(getattr(settings, "height_effect_max", 0.0) or 0.0),
            )
            relief_source_code = {"height-map": 1, "derived-base": 2, "height+derived-detail": 3}.get(relief_source, 0)
            use_relief_texture = int(bool(enhanced_height_usable and relief_source_code > 0))
            use_high_quality_shading = int(
                bool(
                    self._high_quality_textures
                    and batch.has_texture_coordinates
                    and (use_texture or use_normal_texture or use_material_texture or use_height_texture or use_relief_texture)
                )
            )
            image_loaded = bool(upload_diagnostic.image_loaded) if upload_diagnostic else False
            image_size = (
                f"{upload_diagnostic.image_width}x{upload_diagnostic.image_height}"
                if upload_diagnostic and upload_diagnostic.image_width > 0 and upload_diagnostic.image_height > 0
                else "-"
            )
            texture_uploaded = bool(diffuse_texture is not None and self._texture_created(diffuse_texture) and self._texture_id(diffuse_texture) > 0)
            diffuse_texture_id = self._texture_id(diffuse_texture)
            normal_texture_id = self._texture_id(normal_texture)
            material_texture_id = self._texture_id(material_texture)
            height_texture_id = self._texture_id(height_texture)
            relief_texture_id = self._texture_id(derived_relief_texture if relief_source_code in {2, 3} else height_texture)
            prepared_size = (
                f"{upload_diagnostic.prepared_width}x{upload_diagnostic.prepared_height}"
                if upload_diagnostic and upload_diagnostic.prepared_width > 0 and upload_diagnostic.prepared_height > 0
                else "-"
            )
            failure_bucket = ""
            failure_reason = ""
            if not batch.texture_key:
                failure_bucket = "image"
                failure_reason = "No base preview texture path is assigned to this batch."
            elif not self._use_textures:
                failure_bucket = "use_texture"
                failure_reason = "Use textures when available is disabled."
            elif not batch.has_texture_coordinates:
                failure_bucket = "uv"
                failure_reason = "Recovered mesh UV count does not match position count."
            elif upload_diagnostic and not upload_diagnostic.image_loaded:
                failure_bucket = "image"
                failure_reason = upload_diagnostic.failure_reason or f"DDS preview PNG missing or unreadable: {batch.texture_key}"
            elif not image_loaded:
                failure_bucket = "image"
                failure_reason = f"No decoded preview image was available for this texture key: {batch.texture_key}"
            elif diffuse_texture is None:
                failure_bucket = "upload"
                failure_reason = (
                    upload_diagnostic.failure_reason
                    if upload_diagnostic and upload_diagnostic.failure_reason
                    else "Texture image loaded but no GL texture object was uploaded."
                )
            elif not use_texture:
                failure_bucket = "use_texture"
                failure_reason = "Texture sampling disabled by preview control logic."
            runtime_diagnostics[batch_index] = _BatchRenderDiagnostic(
                batch_index=batch_index,
                mesh_index=batch.mesh_index,
                label=self._batch_label(batch),
                texture_key=str(batch.texture_key or ""),
                texture_path_set=bool(batch.texture_key),
                image_loaded=image_loaded,
                image_size=image_size,
                uv_valid=bool(batch.has_texture_coordinates),
                uv_count=uv_count,
                position_count=position_count,
                texture_uploaded=texture_uploaded,
                texture_id=diffuse_texture_id,
                normal_texture_id=normal_texture_id,
                material_texture_id=material_texture_id,
                height_texture_id=height_texture_id,
                relief_texture_id=relief_texture_id,
                diffuse_unit=int(diffuse_unit),
                diffuse_sampler_location=int(self._texture_sampler_uniform_location),
                render_mode_code=int(render_mode_code),
                alpha_handling_mode=str(settings.alpha_handling_mode),
                texture_probe_source=diagnostic_source,
                sampler_probe_mode=str(settings.sampler_probe_mode),
                diffuse_swizzle_mode=str(settings.diffuse_swizzle_mode),
                base_texture_quality=str(batch.base_texture_quality or ""),
                material_decode_mode=int(batch.material_decode_mode or 0),
                rich_material_response=bool(render_mode_code == 22),
                prepared_image_size=prepared_size,
                gl_error=str(upload_diagnostic.gl_error if upload_diagnostic else ""),
                alpha_discard_risk=bool(luma and luma.alpha_dark_ratio >= 0.50),
                use_texture=bool(use_texture),
                use_normal=bool(use_normal_texture),
                use_material=bool(use_material_texture),
                use_height=bool(use_height_texture),
                use_relief=bool(use_relief_texture),
                normal_uploaded=bool(normal_texture is not None or (normal_upload and normal_upload.upload_success)),
                material_uploaded=bool(material_texture is not None or (material_upload and material_upload.upload_success)),
                height_uploaded=bool(height_texture is not None or (height_upload and height_upload.upload_success)),
                failure_bucket=failure_bucket,
                failure_reason=failure_reason,
                sampled_luma=luma.average_luma if luma else None,
                sampled_dark_ratio=luma.dark_ratio if luma else None,
                sampled_alpha=luma.average_alpha if luma else None,
                material_sampled_luma=material_luma.average_luma if material_luma else None,
                material_sampled_dark_ratio=material_luma.dark_ratio if material_luma else None,
                material_sampled_alpha=material_luma.average_alpha if material_luma else None,
                height_sampled_luma=height_luma.average_luma if height_luma else None,
                height_sampled_dark_ratio=height_luma.dark_ratio if height_luma else None,
                height_sampled_alpha=height_luma.average_alpha if height_luma else None,
                height_sampled_min_luma=height_luma.min_luma if height_luma else None,
                height_sampled_max_luma=height_luma.max_luma if height_luma else None,
                height_sampled_contrast=height_luma.luma_contrast if height_luma else None,
                derived_relief_sampled_luma=derived_relief_luma.average_luma if derived_relief_luma else None,
                derived_relief_sampled_min_luma=derived_relief_luma.min_luma if derived_relief_luma else None,
                derived_relief_sampled_max_luma=derived_relief_luma.max_luma if derived_relief_luma else None,
                derived_relief_sampled_contrast=derived_relief_luma.luma_contrast if derived_relief_luma else None,
                enhanced_relief_state=enhanced_state,
                enhanced_relief_reason=enhanced_reason,
                relief_source=relief_source,
                normal_average_strength=normal_strength,
                source_average_color=luma.average_color if luma else (),
                normal_finite_ratio=float(batch.normal_finite_ratio),
                normal_repair_count=int(batch.normal_repair_count),
                tangent_finite_ratio=float(batch.tangent_finite_ratio),
                bitangent_finite_ratio=float(batch.bitangent_finite_ratio),
                uv_finite_ratio=float(batch.uv_finite_ratio),
                smooth_normal_ratio=float(getattr(batch, "smooth_normal_ratio", 0.0) or 0.0),
                texture_flip_vertical=bool(batch.texture_flip_vertical),
                texture_wrap_repeat=bool(batch.texture_wrap_repeat),
                texture_brightness=float(batch.texture_brightness or 1.0),
                texture_tint=tuple(batch.texture_tint or ()),
                texture_uv_scale=tuple(batch.texture_uv_scale or ()),
                visibility_guard=(
                    "active"
                    if bool(use_texture) and luma is not None and luma.average_luma >= 0.075
                    else ("eligible" if bool(use_texture) and luma is not None else "")
                ),
                final_bucket=(
                    "shader visibility guard"
                    if bool(use_texture) and luma is not None and luma.average_luma >= 0.075
                    else ""
                ),
            )
            tint_values = tuple(batch.texture_tint or ())
            if len(tint_values) >= 3:
                texture_tint = QVector3D(
                    max(0.0, min(2.0, float(tint_values[0]))),
                    max(0.0, min(2.0, float(tint_values[1]))),
                    max(0.0, min(2.0, float(tint_values[2]))),
                )
            else:
                texture_tint = QVector3D(1.0, 1.0, 1.0)
            uv_scale_values = tuple(batch.texture_uv_scale or ())
            if len(uv_scale_values) >= 2:
                texture_uv_scale = QVector3D(
                    max(0.05, min(64.0, float(uv_scale_values[0]))),
                    max(0.05, min(64.0, float(uv_scale_values[1]))),
                    0.0,
                )
            else:
                texture_uv_scale = QVector3D(1.0, 1.0, 0.0)
            source_average_values = tuple(batch.source_average_color or ())
            if len(source_average_values) >= 3:
                source_average_color = QVector3D(
                    max(0.0, min(1.5, float(source_average_values[0]))),
                    max(0.0, min(1.5, float(source_average_values[1]))),
                    max(0.0, min(1.5, float(source_average_values[2]))),
                )
            else:
                source_average_color = QVector3D(0.0, 0.0, 0.0)
            source_average_luma = max(0.0, min(1.5, float(batch.source_average_luma or 0.0)))
            base_texture_quality_code = int(_BASE_TEXTURE_QUALITY_CODES.get(str(batch.base_texture_quality or ""), 0))
            relief_luma = height_luma if relief_source_code in {1, 3} else derived_relief_luma
            self._program.setUniformValue(self._use_texture_uniform_location, use_texture)
            self._program.setUniformValue(self._base_texture_tint_uniform_location, texture_tint)
            self._program.setUniformValue(
                self._base_texture_brightness_uniform_location,
                max(0.1, min(3.0, float(batch.texture_brightness or 1.0))),
            )
            self._program.setUniformValue(
                self._base_texture_uv_scale_uniform_location,
                QVector2D(float(texture_uv_scale.x()), float(texture_uv_scale.y())),
            )
            self._program.setUniformValue(self._base_texture_average_color_uniform_location, source_average_color)
            self._program.setUniformValue(self._base_texture_average_luma_uniform_location, source_average_luma)
            self._program.setUniformValue(self._base_texture_quality_uniform_location, base_texture_quality_code)
            self._program.setUniformValue(self._texture_sampler_uniform_location, int(diffuse_unit))
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
            self._program.setUniformValue(self._use_relief_texture_uniform_location, use_relief_texture)
            self._program.setUniformValue(self._diffuse_wrap_bias_uniform_location, float(settings.diffuse_wrap_bias))
            self._program.setUniformValue(self._diffuse_light_scale_uniform_location, float(settings.diffuse_light_scale))
            self._program.setUniformValue(self._normal_strength_cap_uniform_location, float(settings.normal_strength_cap))
            self._program.setUniformValue(self._normal_strength_floor_uniform_location, float(settings.normal_strength_floor))
            self._program.setUniformValue(self._height_effect_max_uniform_location, float(settings.height_effect_max))
            self._program.setUniformValue(
                self._height_sample_min_uniform_location,
                float(relief_luma.min_luma if relief_luma is not None else 0.0),
            )
            self._program.setUniformValue(
                self._height_sample_max_uniform_location,
                float(relief_luma.max_luma if relief_luma is not None else 1.0),
            )
            self._program.setUniformValue(
                self._height_sample_contrast_uniform_location,
                float(relief_luma.luma_contrast if relief_luma is not None else 1.0),
            )
            self._program.setUniformValue(self._height_relief_usable_uniform_location, int(bool(enhanced_height_usable)))
            self._program.setUniformValue(self._relief_source_code_uniform_location, int(relief_source_code))
            self._program.setUniformValue(self._cavity_clamp_min_uniform_location, float(settings.cavity_clamp_min))
            self._program.setUniformValue(self._cavity_clamp_max_uniform_location, float(settings.cavity_clamp_max))
            self._program.setUniformValue(self._specular_base_uniform_location, float(settings.specular_base))
            self._program.setUniformValue(self._specular_min_uniform_location, float(settings.specular_min))
            self._program.setUniformValue(self._specular_max_uniform_location, float(settings.specular_max))
            self._program.setUniformValue(self._shininess_base_uniform_location, float(settings.shininess_base))
            self._program.setUniformValue(self._shininess_min_uniform_location, float(settings.shininess_min))
            self._program.setUniformValue(self._shininess_max_uniform_location, float(settings.shininess_max))
            self._program.setUniformValue(self._height_shininess_boost_uniform_location, float(settings.height_shininess_boost))
            self._program.setUniformValue(
                self._render_diagnostic_mode_uniform_location,
                int(render_mode_code),
            )
            bind_error = ""
            if use_texture and diffuse_draw_texture is not None:
                try:
                    diffuse_draw_texture.bind(int(diffuse_unit))
                finally:
                    bind_error = self._gl_error_text()
            if use_normal_texture and normal_texture is not None:
                normal_texture.bind(1)
            if use_material_texture and material_texture is not None and int(diffuse_unit) != 2:
                material_texture.bind(2)
            if use_height_texture and height_texture is not None:
                height_texture.bind(3)
            if use_relief_texture:
                if relief_source_code in {2, 3} and derived_relief_texture is not None:
                    derived_relief_texture.bind(4)
                elif relief_source_code == 1 and height_texture is not None:
                    height_texture.bind(4)
            self._functions.glDrawArrays(_GL_TRIANGLES, batch.first_vertex, batch.vertex_count)
            draw_error = self._gl_error_text()
            if bind_error or draw_error:
                runtime_diagnostics[batch_index].gl_error = ",".join(part for part in (bind_error, draw_error) if part)
            if use_texture and diffuse_draw_texture is not None:
                self._release_texture_unit(diffuse_draw_texture, int(diffuse_unit))
            if use_normal_texture and normal_texture is not None:
                self._release_texture_unit(normal_texture, 1)
            if use_material_texture and material_texture is not None and int(diffuse_unit) != 2:
                self._release_texture_unit(material_texture, 2)
            if use_height_texture and height_texture is not None:
                self._release_texture_unit(height_texture, 3)
            if use_relief_texture:
                if relief_source_code in {2, 3} and derived_relief_texture is not None:
                    self._release_texture_unit(derived_relief_texture, 4)
                elif relief_source_code == 1 and height_texture is not None:
                    self._release_texture_unit(height_texture, 4)
        self._vertex_array.release()
        self._program.release()
        previous_framebuffer_diagnostic = self._framebuffer_visibility_diagnostic
        now = time.monotonic()
        if now - self._framebuffer_visibility_sampled_at >= 0.50:
            self._framebuffer_visibility_sampled_at = now
            try:
                self._framebuffer_visibility_diagnostic = self._sample_framebuffer_visibility(
                    self.grabFramebuffer(),
                    self._background_color,
                )
            except Exception:
                self._framebuffer_visibility_diagnostic = _FramebufferVisibilitySample()
        if (
            runtime_diagnostics != self._batch_render_diagnostics
            or previous_framebuffer_diagnostic != self._framebuffer_visibility_diagnostic
        ):
            self._batch_render_diagnostics = runtime_diagnostics
            self._refresh_debug_overlay_lines()

    def _preview_mvp_matrix(self) -> QMatrix4x4:
        width = max(1, self.width())
        height = max(1, self.height())
        projection = QMatrix4x4()
        projection.perspective(self._VERTICAL_FOV_DEGREES, width / float(height), 0.1, 100.0)
        view = QMatrix4x4()
        view.translate(0.0, 0.0, -self._distance)
        model = self._preview_base_model_matrix()
        return projection * view * model

    @classmethod
    def _clip_preview_line(
        cls,
        start_clip: tuple[float, float, float, float],
        end_clip: tuple[float, float, float, float],
    ) -> Optional[tuple[tuple[float, float, float, float], tuple[float, float, float, float]]]:
        """Clip an overlay helper line to the OpenGL homogeneous clip volume."""
        planes = (
            (1.0, 0.0, 0.0, 1.0, 0.0),
            (-1.0, 0.0, 0.0, 1.0, 0.0),
            (0.0, 1.0, 0.0, 1.0, 0.0),
            (0.0, -1.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0, 1.0, 0.0),
            (0.0, 0.0, -1.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0, -cls._OVERLAY_CLIP_EPSILON),
        )
        t_min = 0.0
        t_max = 1.0
        delta = tuple(float(end_clip[index]) - float(start_clip[index]) for index in range(4))
        for a, b, c, d, offset in planes:
            start_value = (
                a * float(start_clip[0])
                + b * float(start_clip[1])
                + c * float(start_clip[2])
                + d * float(start_clip[3])
                + offset
            )
            end_value = (
                a * float(end_clip[0])
                + b * float(end_clip[1])
                + c * float(end_clip[2])
                + d * float(end_clip[3])
                + offset
            )
            if start_value < 0.0 and end_value < 0.0:
                return None
            if start_value >= 0.0 and end_value >= 0.0:
                continue
            denominator = start_value - end_value
            if abs(denominator) <= 1e-12:
                return None
            intersection = start_value / denominator
            if start_value < 0.0:
                t_min = max(t_min, intersection)
            else:
                t_max = min(t_max, intersection)
            if t_min > t_max:
                return None

        def at(t: float) -> tuple[float, float, float, float]:
            return tuple(float(start_clip[index]) + delta[index] * t for index in range(4))  # type: ignore[return-value]

        return at(t_min), at(t_max)

    @classmethod
    def _clip_point_is_visible(cls, clip: tuple[float, float, float, float]) -> bool:
        w = float(clip[3])
        if w <= cls._OVERLAY_CLIP_EPSILON:
            return False
        return (
            float(clip[0]) >= -w
            and float(clip[0]) <= w
            and float(clip[1]) >= -w
            and float(clip[1]) <= w
            and float(clip[2]) >= -w
            and float(clip[2]) <= w
        )

    def _clip_to_screen_point(self, clip: tuple[float, float, float, float]) -> Optional[QPointF]:
        w = float(clip[3])
        if w <= self._OVERLAY_CLIP_EPSILON:
            return None
        ndc_x = float(clip[0]) / w
        ndc_y = float(clip[1]) / w
        return QPointF(
            (ndc_x * 0.5 + 0.5) * float(max(1, self.width())),
            (1.0 - (ndc_y * 0.5 + 0.5)) * float(max(1, self.height())),
        )

    def _project_preview_point(
        self,
        mvp: QMatrix4x4,
        point: tuple[float, float, float],
    ) -> Optional[QPointF]:
        clip = mvp.map(QVector4D(float(point[0]), float(point[1]), float(point[2]), 1.0))
        clip_tuple = (float(clip.x()), float(clip.y()), float(clip.z()), float(clip.w()))
        if not self._clip_point_is_visible(clip_tuple):
            return None
        return self._clip_to_screen_point(clip_tuple)

    def _project_preview_line(
        self,
        mvp: QMatrix4x4,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
    ) -> Optional[tuple[QPointF, QPointF]]:
        start_clip = mvp.map(QVector4D(float(start[0]), float(start[1]), float(start[2]), 1.0))
        end_clip = mvp.map(QVector4D(float(end[0]), float(end[1]), float(end[2]), 1.0))
        clipped = self._clip_preview_line(
            (float(start_clip.x()), float(start_clip.y()), float(start_clip.z()), float(start_clip.w())),
            (float(end_clip.x()), float(end_clip.y()), float(end_clip.z()), float(end_clip.w())),
        )
        if clipped is None:
            return None
        start_point = self._clip_to_screen_point(clipped[0])
        end_point = self._clip_to_screen_point(clipped[1])
        if start_point is None or end_point is None:
            return None
        return start_point, end_point

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
        line_points = self._project_preview_line(mvp, start, end)
        if line_points is None:
            return
        start_point, end_point = line_points
        painter.setPen(QPen(color, width))
        painter.drawLine(start_point, end_point)

    def _clamped_overlay_point(self, point: QPointF, *, margin: float = 12.0) -> QPointF:
        return QPointF(
            min(max(float(point.x()), margin), max(margin, float(self.width()) - margin)),
            min(max(float(point.y()), margin), max(margin, float(self.height()) - margin)),
        )

    def _draw_alignment_corner_axis_gizmo(self, painter: QPainter) -> None:
        if self.width() < 120 or self.height() < 120:
            return
        anchor = QPointF(float(self.width()) - 58.0, float(self.height()) - 54.0)
        endpoints = {
            "X": (QPointF(anchor.x() + 34.0, anchor.y() + 8.0), QColor(239, 68, 68, 210)),
            "Y": (QPointF(anchor.x() + 2.0, anchor.y() - 34.0), QColor(59, 130, 246, 210)),
            "Z": (QPointF(anchor.x() - 29.0, anchor.y() + 22.0), QColor(34, 197, 94, 210)),
        }
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 70)))
        painter.drawRoundedRect(QRect(int(anchor.x()) - 40, int(anchor.y()) - 44, 88, 78), 6, 6)
        for label, (endpoint, color) in endpoints.items():
            painter.setPen(QPen(color, 2.0))
            painter.drawLine(anchor, endpoint)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(endpoint, 3.3, 3.3)
            painter.setPen(color)
            painter.drawText(QRect(int(endpoint.x()) + 4, int(endpoint.y()) - 9, 18, 18), Qt.AlignLeft | Qt.AlignVCenter, label)
        painter.restore()

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
            if label_point is None:
                line_points = self._project_preview_line(mvp, (0.0, 0.0, 0.0), point)
                if line_points is not None:
                    label_point = line_points[1]
            if label_point is not None:
                label_point = self._clamped_overlay_point(label_point, margin=18.0)
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
        self._draw_alignment_corner_axis_gizmo(painter)

    def _alignment_handle_origin(self, *, include_live_translation: bool = True) -> tuple[float, float, float]:
        positions = []
        for mesh_index, mesh in enumerate(getattr(self._current_model, "meshes", None) or []):
            if not self._alignment_batch_is_editable(mesh_index):
                continue
            positions.extend(getattr(mesh, "positions", None) or [])
        if not positions:
            return (0.0, 0.0, 0.0)
        live_translation = self._alignment_translation_display_vector() if include_live_translation else QVector3D(0.0, 0.0, 0.0)
        min_x = min(float(position[0]) for position in positions)
        min_y = min(float(position[1]) for position in positions)
        min_z = min(float(position[2]) for position in positions)
        max_x = max(float(position[0]) for position in positions)
        max_y = max(float(position[1]) for position in positions)
        max_z = max(float(position[2]) for position in positions)
        return (
            ((min_x + max_x) * 0.5) + float(live_translation.x()),
            ((min_y + max_y) * 0.5) + float(live_translation.y()),
            ((min_z + max_z) * 0.5) + float(live_translation.z()),
        )

    def _alignment_axis_points(self) -> Dict[str, Tuple[QPointF, QPointF]]:
        if not self._alignment_editing_enabled or self._vertex_count <= 0:
            return {}
        mvp = self._preview_mvp_matrix()
        origin_world = self._alignment_handle_origin()
        origin = self._project_preview_point(mvp, origin_world)
        axis_extent = 0.72
        points: Dict[str, Tuple[QPointF, QPointF]] = {}
        for axis_name, endpoint in (
            ("x", (origin_world[0] + axis_extent, origin_world[1], origin_world[2])),
            ("y", (origin_world[0], origin_world[1] + axis_extent, origin_world[2])),
            ("z", (origin_world[0], origin_world[1], origin_world[2] + axis_extent)),
        ):
            projected = self._project_preview_point(mvp, endpoint)
            if origin is not None and projected is not None:
                points[axis_name] = (origin, projected)
                continue
            clipped = self._project_preview_line(mvp, origin_world, endpoint)
            if clipped is not None:
                points[axis_name] = clipped
        return points

    @staticmethod
    def _distance_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
        vx = float(end.x() - start.x())
        vy = float(end.y() - start.y())
        length_sq = vx * vx + vy * vy
        if length_sq <= 1e-8:
            dx = float(point.x() - start.x())
            dy = float(point.y() - start.y())
            return math.sqrt(dx * dx + dy * dy)
        t = max(0.0, min(1.0, ((float(point.x() - start.x()) * vx) + (float(point.y() - start.y()) * vy)) / length_sq))
        closest_x = float(start.x()) + vx * t
        closest_y = float(start.y()) + vy * t
        dx = float(point.x()) - closest_x
        dy = float(point.y()) - closest_y
        return math.sqrt(dx * dx + dy * dy)

    def _alignment_axis_at(self, point: QPointF) -> str:
        best_axis = ""
        best_distance = 14.0
        for axis_name, (start, end) in self._alignment_axis_points().items():
            distance = self._distance_to_segment(point, start, end)
            if distance < best_distance:
                best_axis = axis_name
                best_distance = distance
        return best_axis

    def _draw_alignment_edit_handles(self, painter: QPainter) -> None:
        if not self._alignment_editing_enabled or self._vertex_count <= 0:
            return
        axis_points = self._alignment_axis_points()
        if not axis_points:
            return
        colors = {
            "x": QColor(239, 68, 68, 220),
            "y": QColor(59, 130, 246, 220),
            "z": QColor(34, 197, 94, 220),
        }
        labels = {"x": "X", "y": "Y", "z": "Z"}
        for axis_name, (start, end) in axis_points.items():
            active = axis_name == self._alignment_drag_axis or axis_name == self._alignment_hover_axis
            color = colors[axis_name]
            width = 3.2 if active else 2.0
            painter.setPen(QPen(color, width))
            painter.drawLine(start, end)
            painter.setPen(QPen(color, 1.2))
            painter.drawEllipse(end, 7.0 if active else 5.5, 7.0 if active else 5.5)
            label_point = self._clamped_overlay_point(end, margin=18.0)
            painter.drawText(QRect(int(label_point.x()) + 8, int(label_point.y()) - 9, 18, 18), Qt.AlignLeft | Qt.AlignVCenter, labels[axis_name])

    def _draw_texture_debug_strip(self, painter: QPainter) -> None:
        settings = self.render_settings()
        if not bool(getattr(settings, "show_texture_debug_strip", False)) or not self._batch_render_diagnostics:
            return
        swatch_size = 28
        gap = 6
        x = 12
        y = max(64, self.height() - swatch_size - 14)
        painter.save()
        painter.setPen(QPen(self._overlay_text_color, 1))
        for batch_index in sorted(self._batch_render_diagnostics)[:8]:
            diagnostic = self._batch_render_diagnostics[batch_index]
            color_values = tuple(diagnostic.source_average_color or ())
            if len(color_values) >= 3:
                color = QColor.fromRgbF(
                    max(0.0, min(1.0, float(color_values[0]))),
                    max(0.0, min(1.0, float(color_values[1]))),
                    max(0.0, min(1.0, float(color_values[2]))),
                    1.0,
                )
            else:
                color = QColor(32, 32, 32)
            rect = QRect(x, y, swatch_size, swatch_size)
            painter.fillRect(rect, color)
            painter.drawRect(rect)
            painter.drawText(QRect(x, y - 16, swatch_size + 8, 14), Qt.AlignLeft | Qt.AlignVCenter, str(batch_index))
            x += swatch_size + gap
        painter.restore()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._draw_alignment_guides(painter)
        self._draw_alignment_edit_handles(painter)
        self._draw_texture_debug_strip(painter)
        painter.setPen(self._overlay_text_color)
        if self._vertex_count <= 0:
            painter.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self._message)
        else:
            help_text = "Drag: orbit | Middle/Right-drag or Shift+Drag: pan | Wheel: zoom | Double-click: reset"
            if self._alignment_editing_enabled:
                help_text = "Drag axes: move (Shift fine/Ctrl coarse) | Alt+Drag: rotate X/Y | Alt+Shift: roll | Wheel: zoom"
            painter.drawText(
                QRect(12, 10, max(120, self.width() - 24), 22),
                Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(help_text, Qt.ElideRight, max(120, self.width() - 24)),
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
        if (
            self._alignment_editing_enabled
            and self._vertex_count > 0
            and event.button() == Qt.LeftButton
            and bool(event.modifiers() & Qt.AltModifier)
        ):
            self._alignment_rotation_drag_active = True
            self._alignment_rotation_drag_roll = bool(event.modifiers() & Qt.ShiftModifier)
            self._alignment_rotation_drag_total = QVector3D(0.0, 0.0, 0.0)
            self.clear_alignment_live_rotation()
            self._last_mouse_pos = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            self.grabMouse()
            self.update()
            self.alignment_drag_started.emit()
            event.accept()
            return
        if self._alignment_editing_enabled and self._vertex_count > 0 and event.button() == Qt.LeftButton:
            axis = self._alignment_axis_at(event.position())
            if axis:
                self._alignment_drag_axis = axis
                self._alignment_hover_axis = axis
                self._alignment_drag_total = QVector3D(0.0, 0.0, 0.0)
                self.clear_alignment_live_translation()
                self._last_mouse_pos = event.position()
                self.setCursor(Qt.SizeAllCursor)
                self.grabMouse()
                self.update()
                self.alignment_drag_started.emit()
                event.accept()
                return
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
        if self._alignment_rotation_drag_active:
            current_pos = event.position()
            delta = current_pos - self._last_mouse_pos
            self._last_mouse_pos = current_pos
            degrees_per_pixel = float(self._alignment_rotation_degrees_per_pixel)
            if bool(event.modifiers() & Qt.ControlModifier):
                degrees_per_pixel *= 4.0
            elif bool(event.modifiers() & Qt.ShiftModifier) and not self._alignment_rotation_drag_roll:
                degrees_per_pixel *= 0.25
            if self._alignment_rotation_drag_roll:
                rotation_delta = QVector3D(0.0, 0.0, float(delta.x()) * degrees_per_pixel)
            else:
                rotation_delta = QVector3D(float(delta.y()) * degrees_per_pixel, float(delta.x()) * degrees_per_pixel, 0.0)
            if rotation_delta.lengthSquared() > 1e-12:
                self._alignment_rotation_drag_total = self._alignment_rotation_drag_total + rotation_delta
                self.set_alignment_live_rotation(
                    float(self._alignment_rotation_drag_total.x()),
                    float(self._alignment_rotation_drag_total.y()),
                    float(self._alignment_rotation_drag_total.z()),
                )
                self.alignment_rotation_changed.emit(
                    float(self._alignment_rotation_drag_total.x()),
                    float(self._alignment_rotation_drag_total.y()),
                    float(self._alignment_rotation_drag_total.z()),
                )
            event.accept()
            return
        if self._alignment_drag_axis:
            current_pos = event.position()
            delta = current_pos - self._last_mouse_pos
            self._last_mouse_pos = current_pos
            axis_points = self._alignment_axis_points()
            start_end = axis_points.get(self._alignment_drag_axis)
            if start_end is not None:
                start, end = start_end
                axis_dx = float(end.x() - start.x())
                axis_dy = float(end.y() - start.y())
                axis_length = max(math.sqrt(axis_dx * axis_dx + axis_dy * axis_dy), 1.0)
                projected_pixels = ((float(delta.x()) * axis_dx) + (float(delta.y()) * axis_dy)) / axis_length
                movement = projected_pixels * float(self._alignment_translation_units_per_pixel)
                if bool(event.modifiers() & Qt.ShiftModifier):
                    movement *= 0.10
                elif bool(event.modifiers() & Qt.ControlModifier):
                    movement *= 4.0
                dx = movement if self._alignment_drag_axis == "x" else 0.0
                dy = movement if self._alignment_drag_axis == "y" else 0.0
                dz = movement if self._alignment_drag_axis == "z" else 0.0
                if abs(dx) > 1e-9 or abs(dy) > 1e-9 or abs(dz) > 1e-9:
                    self._alignment_drag_total = self._alignment_drag_total + QVector3D(float(dx), float(dy), float(dz))
                    self.set_alignment_live_translation(
                        float(self._alignment_drag_total.x()),
                        float(self._alignment_drag_total.y()),
                        float(self._alignment_drag_total.z()),
                    )
                    self.alignment_drag_changed.emit(
                        float(self._alignment_drag_total.x()),
                        float(self._alignment_drag_total.y()),
                        float(self._alignment_drag_total.z()),
                    )
            event.accept()
            return
        if self._alignment_editing_enabled and self._vertex_count > 0:
            axis = self._alignment_axis_at(event.position())
            if axis != self._alignment_hover_axis:
                self._alignment_hover_axis = axis
                self.setCursor(Qt.SizeAllCursor if axis else Qt.ArrowCursor)
                self.update()
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
        if self._alignment_rotation_drag_active and event.button() == Qt.LeftButton:
            total = QVector3D(self._alignment_rotation_drag_total)
            self._alignment_rotation_drag_active = False
            self._alignment_rotation_drag_roll = False
            self._alignment_rotation_drag_total = QVector3D(0.0, 0.0, 0.0)
            self.releaseMouse()
            self.unsetCursor()
            self.alignment_rotation_finished.emit(float(total.x()), float(total.y()), float(total.z()))
            self.clear_alignment_live_rotation()
            self.update()
            event.accept()
            return
        if self._alignment_drag_axis and event.button() == Qt.LeftButton:
            total = QVector3D(self._alignment_drag_total)
            self._alignment_drag_axis = ""
            self._alignment_drag_total = QVector3D(0.0, 0.0, 0.0)
            self.releaseMouse()
            self.unsetCursor()
            self.alignment_drag_finished.emit(float(total.x()), float(total.y()), float(total.z()))
            self.clear_alignment_live_translation()
            self.update()
            event.accept()
            return
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
            self._alignment_rotation_drag_active = False
            self._alignment_rotation_drag_roll = False
            self._alignment_rotation_drag_total = QVector3D(0.0, 0.0, 0.0)
            self._alignment_live_rotation = QVector3D(0.0, 0.0, 0.0)
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
        stride = 20 * 4
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
        self._program.enableAttributeArray(6)
        self._program.setAttributeBuffer(6, _GL_FLOAT, 17 * 4, 3, stride)
        self._vertex_buffer.release()
        self._vertex_array.release()
        self._program.release()
        self._rebuild_gl_textures()
        self.doneCurrent()

    @staticmethod
    def _finite_float(value: object, fallback: float = 0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return float(fallback)
        return result if math.isfinite(result) else float(fallback)

    @classmethod
    def _sanitize_vector3(
        cls,
        vector: object,
        *,
        fallback: Tuple[float, float, float],
        normalize: bool = False,
    ) -> Tuple[Tuple[float, float, float], bool]:
        repaired = False
        try:
            x = cls._finite_float(vector[0], fallback[0])  # type: ignore[index]
            y = cls._finite_float(vector[1], fallback[1])  # type: ignore[index]
            z = cls._finite_float(vector[2], fallback[2])  # type: ignore[index]
        except Exception:
            x, y, z = fallback
            repaired = True
        if normalize:
            length = math.sqrt((x * x) + (y * y) + (z * z))
            if length <= 1e-8 or not math.isfinite(length):
                return fallback, True
            x /= length
            y /= length
            z /= length
        return (x, y, z), repaired

    @classmethod
    def _sanitize_uv(cls, uv: object) -> Tuple[Tuple[float, float], bool]:
        try:
            u = cls._finite_float(uv[0], 0.0)  # type: ignore[index]
            v = cls._finite_float(uv[1], 0.0)  # type: ignore[index]
        except Exception:
            return (0.0, 0.0), True
        return (u, v), False

    @classmethod
    def _sanitize_color3(
        cls,
        color: Sequence[object],
        *,
        fallback: Tuple[float, float, float],
    ) -> Tuple[float, float, float]:
        if len(color) < 3:
            return fallback
        return (
            max(0.0, min(1.0, cls._finite_float(color[0], fallback[0]))),
            max(0.0, min(1.0, cls._finite_float(color[1], fallback[1]))),
            max(0.0, min(1.0, cls._finite_float(color[2], fallback[2]))),
        )

    @staticmethod
    def _orthogonal_tangent_frame(normal: Tuple[float, float, float]) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        nx, ny, nz = normal
        length = math.sqrt((nx * nx) + (ny * ny) + (nz * nz))
        if length <= 1e-8 or not math.isfinite(length):
            nx, ny, nz = (0.0, 0.0, 1.0)
        else:
            nx /= length
            ny /= length
            nz /= length
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

    @staticmethod
    def _smooth_normal_position_key(position: Tuple[float, float, float]) -> Tuple[int, int, int]:
        return (
            int(round(float(position[0]) * 100000.0)),
            int(round(float(position[1]) * 100000.0)),
            int(round(float(position[2]) * 100000.0)),
        )

    @classmethod
    def _build_preview_smoothed_normals(
        cls,
        positions: Sequence[Tuple[float, float, float]],
        normals: Sequence[Tuple[float, float, float]],
        indices: Sequence[int],
    ) -> Tuple[List[Tuple[float, float, float]], float]:
        vertex_count = len(positions)
        if vertex_count <= 0 or len(normals) != vertex_count:
            return list(normals), 0.0
        accum_by_position: Dict[Tuple[int, int, int], List[float]] = {}
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
            ax, ay, az = positions[a]
            bx, by, bz = positions[b]
            cx, cy, cz = positions[c]
            ab = (bx - ax, by - ay, bz - az)
            ac = (cx - ax, cy - ay, cz - az)
            face = (
                (ab[1] * ac[2]) - (ab[2] * ac[1]),
                (ab[2] * ac[0]) - (ab[0] * ac[2]),
                (ab[0] * ac[1]) - (ab[1] * ac[0]),
            )
            face_length = math.sqrt((face[0] * face[0]) + (face[1] * face[1]) + (face[2] * face[2]))
            if face_length <= 1e-12 or not math.isfinite(face_length):
                continue
            for vertex_index in (a, b, c):
                key = cls._smooth_normal_position_key(positions[vertex_index])
                accum = accum_by_position.setdefault(key, [0.0, 0.0, 0.0])
                accum[0] += face[0]
                accum[1] += face[1]
                accum[2] += face[2]

        smoothed: List[Tuple[float, float, float]] = []
        changed = 0
        for vertex_index, original in enumerate(normals):
            key = cls._smooth_normal_position_key(positions[vertex_index])
            accum = accum_by_position.get(key)
            if accum is None:
                smoothed.append(original)
                continue
            candidate, repaired = cls._sanitize_vector3(
                accum,
                fallback=original,
                normalize=True,
            )
            if repaired:
                smoothed.append(original)
                continue
            dot = (original[0] * candidate[0]) + (original[1] * candidate[1]) + (original[2] * candidate[2])
            if dot <= 0.05:
                smoothed.append(original)
                continue
            if dot < 0.995:
                changed += 1
            smoothed.append(candidate)
        return smoothed, changed / float(max(1, vertex_count))

    @classmethod
    def _build_vertex_blob(cls, model) -> Tuple[bytes, int, List[_ModelPreviewDrawBatch]]:
        meshes = getattr(model, "meshes", None)
        if not meshes:
            return b"", 0, []
        vertex_data = array("f")
        vertex_count = 0
        batches: List[_ModelPreviewDrawBatch] = []
        for mesh_index, mesh in enumerate(meshes):
            raw_positions = list(getattr(mesh, "positions", []) or [])
            positions: List[Tuple[float, float, float]] = []
            position_repair_count = 0
            for raw_position in raw_positions:
                position, repaired = cls._sanitize_vector3(raw_position, fallback=(0.0, 0.0, 0.0))
                positions.append(position)
                if repaired:
                    position_repair_count += 1
            normals = list(getattr(mesh, "normals", []) or [])
            indices = list(getattr(mesh, "indices", []) or [])
            if not positions or not indices:
                continue
            if len(normals) != len(positions):
                normals = [(0.0, 0.0, 1.0)] * len(positions)
            sanitized_normals: List[Tuple[float, float, float]] = []
            normal_repair_count = 0
            for normal in normals:
                sanitized_normal, repaired = cls._sanitize_vector3(
                    normal,
                    fallback=(0.0, 0.0, 1.0),
                    normalize=True,
                )
                sanitized_normals.append(sanitized_normal)
                if repaired:
                    normal_repair_count += 1
            normals = sanitized_normals
            smoothed_normals, smooth_normal_ratio = cls._build_preview_smoothed_normals(
                positions,
                normals,
                indices,
            )
            raw_texture_coordinates = list(getattr(mesh, "texture_coordinates", []) or [])
            texture_coordinates: List[Tuple[float, float]] = []
            uv_repair_count = 0
            if len(raw_texture_coordinates) == len(positions):
                for raw_uv in raw_texture_coordinates:
                    uv, repaired = cls._sanitize_uv(raw_uv)
                    texture_coordinates.append(uv)
                    if repaired:
                        uv_repair_count += 1
            has_texture_coordinates = len(texture_coordinates) == len(positions)
            tangents, bitangents = cls._build_tangent_frames(
                positions,
                texture_coordinates,
                normals,
                indices,
            )
            sanitized_tangents: List[Tuple[float, float, float]] = []
            tangent_repair_count = 0
            sanitized_bitangents: List[Tuple[float, float, float]] = []
            bitangent_repair_count = 0
            for vertex_index in range(len(positions)):
                tangent_fallback, bitangent_fallback = cls._orthogonal_tangent_frame(normals[vertex_index])
                tangent_source = tangents[vertex_index] if vertex_index < len(tangents) else tangent_fallback
                bitangent_source = bitangents[vertex_index] if vertex_index < len(bitangents) else bitangent_fallback
                tangent, tangent_repaired = cls._sanitize_vector3(
                    tangent_source,
                    fallback=tangent_fallback,
                    normalize=True,
                )
                bitangent, bitangent_repaired = cls._sanitize_vector3(
                    bitangent_source,
                    fallback=bitangent_fallback,
                    normalize=True,
                )
                sanitized_tangents.append(tangent)
                sanitized_bitangents.append(bitangent)
                if tangent_repaired:
                    tangent_repair_count += 1
                if bitangent_repaired:
                    bitangent_repair_count += 1
            tangents = sanitized_tangents
            bitangents = sanitized_bitangents
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
            color = cls._sanitize_color3(
                preview_color,
                fallback=cls._PALETTE[mesh_index % len(cls._PALETTE)],
            )
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
                    sx, sy, sz = (
                        smoothed_normals[vertex_index]
                        if vertex_index < len(smoothed_normals)
                        else (nx, ny, nz)
                    )
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
                            sx,
                            sy,
                            sz,
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
            base_texture_quality = str(getattr(mesh, "preview_base_texture_quality", "") or "").strip().lower()
            texture_tint_values = tuple(getattr(mesh, "preview_texture_tint", ()) or ())[:3]
            texture_tint = tuple(
                max(0.0, min(2.0, cls._finite_float(value, 1.0)))
                for value in texture_tint_values
            )
            texture_uv_scale_values = tuple(getattr(mesh, "preview_texture_uv_scale", ()) or ())[:2]
            texture_uv_scale = tuple(
                max(0.05, min(64.0, cls._finite_float(value, 1.0)))
                for value in texture_uv_scale_values
            )
            if len(texture_uv_scale) >= 2 and (
                abs(float(texture_uv_scale[0]) - 1.0) > 1e-6
                or abs(float(texture_uv_scale[1]) - 1.0) > 1e-6
            ):
                texture_wrap_repeat = True
            vertex_total = max(1, len(positions))
            batches.append(
                _ModelPreviewDrawBatch(
                    mesh_index=mesh_index,
                    material_name=str(getattr(mesh, "material_name", "") or "").strip(),
                    texture_name=str(getattr(mesh, "texture_name", "") or "").strip(),
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
                    base_texture_quality=base_texture_quality,
                    texture_brightness=max(
                        0.1,
                        min(3.0, cls._finite_float(getattr(mesh, "preview_texture_brightness", 1.0), 1.0)),
                    ),
                    texture_tint=texture_tint,
                    texture_uv_scale=texture_uv_scale,
                    normal_finite_ratio=max(0.0, 1.0 - (float(normal_repair_count + position_repair_count) / float(vertex_total))),
                    normal_repair_count=normal_repair_count,
                    tangent_finite_ratio=max(0.0, 1.0 - (float(tangent_repair_count) / float(vertex_total))),
                    bitangent_finite_ratio=max(0.0, 1.0 - (float(bitangent_repair_count) / float(vertex_total))),
                    uv_finite_ratio=max(0.0, 1.0 - (float(uv_repair_count) / float(vertex_total))) if has_texture_coordinates else 0.0,
                    smooth_normal_ratio=max(0.0, min(1.0, float(smooth_normal_ratio))),
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
                uu = float(u)
                vv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(uu) or not math.isfinite(vv):
                continue
            uu = max(0.0, min(1.0, uu))
            vv = max(0.0, min(1.0, vv))
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
        gl_max_texture_size = self._read_gl_max_texture_size()
        if self._high_quality_textures:
            max_dimension = int(settings.preview_texture_max_dimension)
            if longest_edge <= 0 or max_dimension <= 0 or longest_edge <= max_dimension:
                if gl_max_texture_size <= 0 or longest_edge <= gl_max_texture_size:
                    return prepared
            target_longest_edge = max_dimension
        else:
            max_dimension = int(settings.low_quality_texture_max_dimension)
            if longest_edge <= 0 or max_dimension <= 0:
                return prepared
            target_longest_edge = min(max_dimension, max(256, longest_edge // 2))
        if gl_max_texture_size > 0:
            target_longest_edge = min(int(target_longest_edge), int(gl_max_texture_size))
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
        self._texture_upload_diagnostics.clear()
        self._batch_luma_diagnostics.clear()
        self._batch_material_luma_diagnostics.clear()
        self._batch_height_luma_diagnostics.clear()
        self._batch_normal_strength_diagnostics.clear()
        self._batch_derived_relief_luma_diagnostics.clear()
        self._batch_derived_relief_keys.clear()
        settings = self.render_settings()
        build_derived_relief = bool(
            self._render_mode_uses_derived_relief(settings)
            and self._use_textures
            and self._high_quality_textures
        )
        for batch in self._mesh_batches:
            batch.source_average_color = ()
            batch.source_average_luma = 0.0
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
        for batch_index, batch in enumerate(self._mesh_batches):
            derived_relief_key = (
                self._derived_relief_texture_key(batch_index, batch)
                if build_derived_relief
                else ""
            )
            if derived_relief_key:
                self._batch_derived_relief_keys[batch_index] = derived_relief_key
            batch_texture_keys = (
                batch.texture_key,
                batch.normal_texture_key,
                batch.material_texture_key,
                batch.height_texture_key,
                derived_relief_key,
            )
            for texture_key in batch_texture_keys:
                if not texture_key:
                    continue
                cache_key = (texture_key, bool(batch.texture_wrap_repeat), bool(batch.texture_flip_vertical))
                upload_diagnostic = self._texture_upload_diagnostics.get(cache_key)
                if upload_diagnostic is None:
                    upload_diagnostic = _TextureUploadDiagnostic(texture_key=texture_key)
                    self._texture_upload_diagnostics[cache_key] = upload_diagnostic
                texture_image = source_images.get(texture_key)
                if texture_image is None:
                    texture_image = self._load_gl_texture_image(texture_key)
                    if texture_image is not None:
                        source_images[texture_key] = texture_image
                if texture_image is None and texture_key == derived_relief_key and batch.texture_key:
                    base_image = source_images.get(batch.texture_key)
                    if base_image is None:
                        base_image = self._load_gl_texture_image(batch.texture_key)
                        if base_image is not None:
                            source_images[batch.texture_key] = base_image
                    if base_image is not None:
                        texture_image = self._derive_relief_image_from_base(base_image)
                        if texture_image is not None:
                            source_images[texture_key] = texture_image
                if texture_image is None:
                    upload_diagnostic.image_loaded = False
                    upload_diagnostic.failure_reason = f"DDS preview PNG missing or unreadable: {texture_key}"
                    continue
                upload_diagnostic.image_loaded = True
                upload_diagnostic.image_width = int(texture_image.width())
                upload_diagnostic.image_height = int(texture_image.height())
                upload_diagnostic.gl_max_texture_size = self._read_gl_max_texture_size()
                if texture_key == batch.texture_key and current_meshes and 0 <= batch.mesh_index < len(current_meshes):
                    mesh = current_meshes[batch.mesh_index]
                    luma = self._sample_base_texture_visibility(
                        texture_image,
                        getattr(mesh, "texture_coordinates", ()) or (),
                        flip_vertical=bool(batch.texture_flip_vertical),
                    )
                    if luma is not None:
                        self._batch_luma_diagnostics[batch_index] = luma
                        batch.source_average_color = luma.average_color
                        batch.source_average_luma = float(luma.average_luma)
                if current_meshes and 0 <= batch.mesh_index < len(current_meshes):
                    mesh = current_meshes[batch.mesh_index]
                    if texture_key == batch.material_texture_key:
                        luma = self._sample_base_texture_visibility(
                            texture_image,
                            getattr(mesh, "texture_coordinates", ()) or (),
                            flip_vertical=bool(batch.texture_flip_vertical),
                        )
                        if luma is not None:
                            self._batch_material_luma_diagnostics[batch_index] = luma
                    elif texture_key == batch.height_texture_key:
                        luma = self._sample_base_texture_visibility(
                            texture_image,
                            getattr(mesh, "texture_coordinates", ()) or (),
                            flip_vertical=bool(batch.texture_flip_vertical),
                        )
                        if luma is not None:
                            self._batch_height_luma_diagnostics[batch_index] = luma
                    elif texture_key == derived_relief_key:
                        luma = self._sample_base_texture_visibility(
                            texture_image,
                            getattr(mesh, "texture_coordinates", ()) or (),
                            flip_vertical=bool(batch.texture_flip_vertical),
                        )
                        if luma is not None:
                            self._batch_derived_relief_luma_diagnostics[batch_index] = luma
                    elif texture_key == batch.normal_texture_key:
                        normal_strength = self._sample_normal_map_average_strength(
                            texture_image,
                            getattr(mesh, "texture_coordinates", ()) or (),
                            flip_vertical=bool(batch.texture_flip_vertical),
                        )
                        if normal_strength is not None:
                            self._batch_normal_strength_diagnostics[batch_index] = float(normal_strength)
                if cache_key in self._texture_objects:
                    upload_diagnostic.upload_attempted = True
                    upload_diagnostic.upload_success = True
                    upload_diagnostic.failure_reason = ""
                    continue
                prepared_image = self._prepare_gl_texture_image(texture_image)
                if batch.texture_flip_vertical:
                    prepared_image = prepared_image.mirrored(False, True)
                if prepared_image.isNull():
                    upload_diagnostic.upload_attempted = True
                    upload_diagnostic.upload_success = False
                    upload_diagnostic.failure_reason = "Texture image decoded but could not be prepared for GL upload."
                    continue
                upload_diagnostic.prepared_width = int(prepared_image.width())
                upload_diagnostic.prepared_height = int(prepared_image.height())
                upload_diagnostic.upload_attempted = True
                try:
                    texture = QOpenGLTexture(prepared_image)
                except Exception as exc:
                    upload_diagnostic.upload_success = False
                    upload_diagnostic.failure_reason = f"QOpenGLTexture upload failed: {exc}"
                    continue
                upload_diagnostic.texture_created = self._texture_created(texture)
                upload_diagnostic.texture_id = self._texture_id(texture)
                upload_diagnostic.gl_error = self._gl_error_text()
                if not upload_diagnostic.texture_created or upload_diagnostic.texture_id <= 0:
                    try:
                        texture.destroy()
                    except Exception:
                        pass
                    upload_diagnostic.upload_success = False
                    upload_diagnostic.failure_reason = "QOpenGLTexture object was not created or returned texture id 0."
                    continue
                use_mipmaps = bool(self._high_quality_textures and not self.render_settings().force_nearest_no_mipmaps)
                if use_mipmaps:
                    try:
                        texture.generateMipMaps()
                        upload_diagnostic.mipmaps_generated = True
                    except Exception:
                        upload_diagnostic.mipmaps_generated = False
                    if upload_diagnostic.mipmaps_generated:
                        texture.setMinMagFilters(QOpenGLTexture.LinearMipMapLinear, QOpenGLTexture.Linear)
                    else:
                        texture.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
                    if hasattr(texture, "setMaximumAnisotropy"):
                        try:
                            texture.setMaximumAnisotropy(float(self.render_settings().max_anisotropy))
                        except Exception:
                            pass
                else:
                    filter_mode = QOpenGLTexture.Nearest if self.render_settings().force_nearest_no_mipmaps else QOpenGLTexture.Linear
                    texture.setMinMagFilters(filter_mode, filter_mode)
                texture.setWrapMode(QOpenGLTexture.Repeat if batch.texture_wrap_repeat else QOpenGLTexture.ClampToEdge)
                self._texture_objects[cache_key] = texture
                upload_diagnostic.gl_error = ",".join(
                    part for part in (upload_diagnostic.gl_error, self._gl_error_text()) if part
                )
                upload_diagnostic.upload_success = upload_diagnostic.texture_created and upload_diagnostic.texture_id > 0
                upload_diagnostic.failure_reason = ""
        self._refresh_debug_overlay_lines()


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


_TEXT_HIGHLIGHT_STYLES = {"rich", "calm", "plain"}
_TEXT_COLOR_SCHEMES = {"theme", "vscode", "terminal", "accessible", "solarized"}


def _normalize_text_highlight_style(style: object) -> str:
    value = str(style or "rich").strip().lower()
    return value if value in _TEXT_HIGHLIGHT_STYLES else "rich"


def _normalize_text_color_scheme(scheme: object) -> str:
    value = str(scheme or "theme").strip().lower()
    return value if value in _TEXT_COLOR_SCHEMES else "theme"


def _scheme_palette(theme_key: str, scheme: object) -> Optional[Dict[str, str]]:
    normalized = _normalize_text_color_scheme(scheme)
    if normalized == "theme":
        return None
    light = _theme_is_light(theme_key)
    if normalized == "terminal":
        return {
            "comment": "#6b7280" if light else "#7dd3fc",
            "keyword": "#7c3aed" if light else "#f0abfc",
            "string": "#047857" if light else "#86efac",
            "number": "#b45309" if light else "#fbbf24",
            "tag": "#0369a1" if light else "#93c5fd",
            "attribute": "#be123c" if light else "#fda4af",
            "section": "#0f766e" if light else "#5eead4",
            "key": "#b45309" if light else "#fde68a",
            "entity": "#9333ea" if light else "#d8b4fe",
            "bracket": "#4b5563" if light else "#d1d5db",
            "success": "#047857" if light else "#22c55e",
            "warning": "#a16207" if light else "#facc15",
            "error": "#b91c1c" if light else "#f87171",
        }
    if normalized == "accessible":
        return {
            "comment": "#525252" if light else "#bdbdbd",
            "keyword": "#0000aa" if light else "#8ab4ff",
            "string": "#006400" if light else "#b7f7c1",
            "number": "#7a3e00" if light else "#ffd27d",
            "tag": "#003f8c" if light else "#9bd1ff",
            "attribute": "#6f1d8f" if light else "#e3b5ff",
            "section": "#004d40" if light else "#9ff7e8",
            "key": "#5f3700" if light else "#ffe08a",
            "entity": "#7a3e00" if light else "#ffd27d",
            "bracket": "#333333" if light else "#eeeeee",
            "success": "#006400" if light else "#76ff7a",
            "warning": "#8a5a00" if light else "#ffdd57",
            "error": "#a00000" if light else "#ff8a80",
        }
    if normalized == "solarized":
        return {
            "comment": "#657b83",
            "keyword": "#6c71c4",
            "string": "#2aa198",
            "number": "#d33682",
            "tag": "#268bd2",
            "attribute": "#b58900",
            "section": "#859900",
            "key": "#b58900",
            "entity": "#cb4b16",
            "bracket": "#839496",
            "success": "#859900",
            "warning": "#b58900",
            "error": "#dc322f",
        }
    return {
        "comment": "#008000" if light else "#6a9955",
        "keyword": "#af00db" if light else "#c586c0",
        "string": "#a31515" if light else "#ce9178",
        "number": "#098658" if light else "#b5cea8",
        "tag": "#0451a5" if light else "#569cd6",
        "attribute": "#001080" if light else "#9cdcfe",
        "section": "#795e26" if light else "#4ec9b0",
        "key": "#001080" if light else "#9cdcfe",
        "entity": "#795e26" if light else "#d7ba7d",
        "bracket": "#333333" if light else "#d4d4d4",
        "success": "#098658" if light else "#6a9955",
        "warning": "#b45309" if light else "#fbbf24",
        "error": "#c0362c" if light else "#f48771",
    }


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

    def __init__(self, document, theme_key: str, highlight_style: str = "rich", color_scheme: str = "theme"):
        super().__init__(document)
        self.language = "plain"
        self.highlight_style = _normalize_text_highlight_style(highlight_style)
        self.color_scheme = _normalize_text_color_scheme(color_scheme)
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
        self.current_theme_key = theme_key
        light = _theme_is_light(theme_key)
        theme = get_theme(theme_key)
        calm = self.highlight_style == "calm"

        def make(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold and not calm:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            return fmt

        if self.highlight_style == "plain":
            base_color = theme["text"]
            self.comment_format = make(base_color)
            self.keyword_format = make(base_color)
            self.string_format = make(base_color)
            self.number_format = make(base_color)
            self.tag_format = make(base_color)
            self.attribute_format = make(base_color)
            self.section_format = make(base_color)
            self.key_format = make(base_color)
            self.entity_format = make(base_color)
            self.bracket_format = make(base_color)
        else:
            scheme = _scheme_palette(theme_key, self.color_scheme)
        if self.highlight_style == "plain":
            pass
        elif scheme is not None:
            self.comment_format = make(scheme["comment"], italic=True)
            self.keyword_format = make(scheme["keyword"], bold=True)
            self.string_format = make(scheme["string"])
            self.number_format = make(scheme["number"])
            self.tag_format = make(scheme["tag"], bold=True)
            self.attribute_format = make(scheme["attribute"])
            self.section_format = make(scheme["section"], bold=True)
            self.key_format = make(scheme["key"])
            self.entity_format = make(scheme["entity"])
            self.bracket_format = make(scheme["bracket"])
        elif calm:
            self.comment_format = make(theme["text_muted"], italic=True)
            self.keyword_format = make(theme["accent"])
            self.string_format = make("#8a4b32" if light else "#c49a8b")
            self.number_format = make("#3f7f5f" if light else "#9bbf9d")
            self.tag_format = make(theme["accent"])
            self.attribute_format = make(theme["text_strong"])
            self.section_format = make(theme["accent"])
            self.key_format = make(theme["text_strong"])
            self.entity_format = make(theme["warning_text"])
            self.bracket_format = make(theme["text_muted"])
        elif light:
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

    def set_highlight_style(self, style: str) -> None:
        normalized = _normalize_text_highlight_style(style)
        if normalized == self.highlight_style:
            return
        self.highlight_style = normalized
        self.set_theme(getattr(self, "current_theme_key", "") or "graphite")

    def set_color_scheme(self, scheme: str) -> None:
        normalized = _normalize_text_color_scheme(scheme)
        if normalized == self.color_scheme:
            return
        self.color_scheme = normalized
        self.set_theme(getattr(self, "current_theme_key", "") or "graphite")

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
        if self.highlight_style == "plain":
            return
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
    def __init__(
        self,
        *,
        theme_key: str,
        parent: Optional[QWidget] = None,
        highlight_style: str = "rich",
        color_scheme: str = "theme",
    ):
        super().__init__(parent)
        self.theme_key = theme_key
        self._highlight_style = _normalize_text_highlight_style(highlight_style)
        self._color_scheme = _normalize_text_color_scheme(color_scheme)
        self._match_selections: list[QTextEdit.ExtraSelection] = []
        self._search_query = ""
        self._search_matches: list[Tuple[int, int]] = []
        self._current_search_index = -1
        self._editor_font_size = max(8, self.font().pointSize())
        self.line_number_area = _LineNumberArea(self)
        self.syntax_highlighter = PreviewSyntaxHighlighter(
            self.document(),
            theme_key,
            self._highlight_style,
            self._color_scheme,
        )
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
        self._search_match_color = QColor(theme["warning_text"])
        self._search_match_color.setAlpha(100)
        self._search_current_match_color = QColor(theme["accent"])
        self._search_current_match_color.setAlpha(150)
        self.syntax_highlighter.set_theme(theme_key)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme['preview_bg']}; color: {theme['text']}; border: 1px solid {theme['border_strong']}; border-radius: 4px; selection-background-color: {theme['accent']}; selection-color: #ffffff; }}"
        )
        self.viewport().update()
        self.line_number_area.update()
        self._apply_combined_selections()

    def set_highlight_style(self, style: str) -> None:
        self._highlight_style = _normalize_text_highlight_style(style)
        if hasattr(self.syntax_highlighter, "set_highlight_style"):
            self.syntax_highlighter.set_highlight_style(self._highlight_style)

    def set_color_scheme(self, scheme: str) -> None:
        self._color_scheme = _normalize_text_color_scheme(scheme)
        if hasattr(self.syntax_highlighter, "set_color_scheme"):
            self.syntax_highlighter.set_color_scheme(self._color_scheme)
        else:
            self.syntax_highlighter.rehighlight()

    def set_language_for_extension(self, extension: str) -> None:
        self.syntax_highlighter.set_language_for_extension(extension)

    def set_wrap_enabled(self, enabled: bool) -> None:
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if enabled else QPlainTextEdit.NoWrap)

    def search_text(self, query: str, *, jump: bool = True) -> Tuple[int, int]:
        self._search_query = str(query or "")
        self._rebuild_search_matches(jump=jump)
        return self.search_result()

    def find_next_match(self) -> Tuple[int, int]:
        if not self._search_matches:
            return self.search_result()
        self._current_search_index = (self._current_search_index + 1) % len(self._search_matches)
        self._apply_search_selection(jump=True)
        return self.search_result()

    def find_previous_match(self) -> Tuple[int, int]:
        if not self._search_matches:
            return self.search_result()
        self._current_search_index = (self._current_search_index - 1) % len(self._search_matches)
        self._apply_search_selection(jump=True)
        return self.search_result()

    def clear_search(self) -> None:
        self._search_query = ""
        self._search_matches = []
        self._current_search_index = -1
        self.set_match_selections([])

    def search_result(self) -> Tuple[int, int]:
        if not self._search_matches:
            return (0, 0)
        return (self._current_search_index + 1, len(self._search_matches))

    def _rebuild_search_matches(self, *, jump: bool) -> None:
        query = self._search_query
        if not query:
            self.clear_search()
            return
        haystack = self.toPlainText()
        lowered_haystack = haystack.lower()
        lowered_query = query.lower()
        matches: list[Tuple[int, int]] = []
        start = 0
        while True:
            index = lowered_haystack.find(lowered_query, start)
            if index < 0:
                break
            end = index + len(query)
            matches.append((index, end))
            start = max(index + len(query), index + 1)
        self._search_matches = matches
        self._current_search_index = 0 if matches else -1
        self._apply_search_selection(jump=jump and bool(matches))

    def _apply_search_selection(self, *, jump: bool) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        for match_index, (start, end) in enumerate(self._search_matches):
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(
                self._search_current_match_color
                if match_index == self._current_search_index
                else self._search_match_color
            )
            cursor = self.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            selection.cursor = cursor
            selections.append(selection)
        self.set_match_selections(selections)
        if jump and 0 <= self._current_search_index < len(self._search_matches):
            start, end = self._search_matches[self._current_search_index]
            self.center_on_span(start, end)

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

    def __init__(self, document, theme_key: str, highlight_style: str = "rich", color_scheme: str = "theme"):
        super().__init__(document)
        self.current_theme_key = theme_key
        self._bold_enabled = True
        self.highlight_style = _normalize_text_highlight_style(highlight_style)
        self.color_scheme = _normalize_text_color_scheme(color_scheme)
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
        calm = self.highlight_style == "calm"
        scheme = _scheme_palette(theme_key, self.color_scheme)

        def make_format(
            color: str,
            *,
            bold: bool = False,
            italic: bool = False,
            background: Optional[QColor] = None,
        ) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold and self._bold_enabled and not calm:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            if background is not None:
                fmt.setBackground(background)
            return fmt

        self.timestamp_format = make_format(theme["text_muted"])
        self.error_format = make_format((scheme or {}).get("error", theme["error"] if not calm else theme["warning_text"]), bold=True)
        self.warning_format = make_format((scheme or {}).get("warning", theme["warning_text"]), bold=True)
        self.success_format = make_format((scheme or {}).get("success", "#098658" if light else "#6a9955"), bold=True)
        self.phase_format = make_format((scheme or {}).get("tag", theme["accent"]), bold=True)
        self.path_format = make_format(theme["text_strong"], bold=True)
        self.progress_format = make_format((scheme or {}).get("number", theme["accent"]), bold=True)
        self.action_format = make_format((scheme or {}).get("keyword", "#0451a5" if light else "#569cd6"), bold=True)
        self.backend_format = make_format((scheme or {}).get("tag", theme["accent"]), bold=True)
        self.key_format = make_format((scheme or {}).get("key", "#795e26" if light else "#d7ba7d"), bold=True)
        self.value_format = make_format((scheme or {}).get("string", "#a31515" if light else "#ce9178"))
        self.number_format = make_format((scheme or {}).get("number", "#098658" if light else "#b5cea8"))
        self.separator_format = make_format(theme["text_muted"], bold=True)

        warning_bg = QColor(theme["warning_bg"])
        warning_bg.setAlpha(36 if calm else (70 if light else 48))
        error_bg = QColor(theme["error"])
        error_bg.setAlpha(22 if calm else (42 if light else 34))
        success_bg = QColor(theme["accent_soft"])
        success_bg.setAlpha(46 if calm else (120 if light else 90))
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

    def set_highlight_style(self, style: str) -> None:
        self.highlight_style = _normalize_text_highlight_style(style)
        self.set_theme(self.current_theme_key)

    def set_color_scheme(self, scheme: str) -> None:
        self.color_scheme = _normalize_text_color_scheme(scheme)
        self.set_theme(self.current_theme_key)

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if self.highlight_style == "plain":
            return
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
        r"^(Entry Metadata|Import Summary|Preview / Texture Notes|Preview Diagnostics|Render Sampling Diagnostics|Readable Strings|Binary Header Preview)\s*$"
    )
    _label_re = re.compile(r"^([A-Za-z][A-Za-z0-9 /()_-]+:)")
    _warning_re = re.compile(r"\b(warning|failed|missing|truncated|unsupported|fallback|skipped|unavailable|error)\b", re.IGNORECASE)
    _windows_path_re = re.compile(r"[A-Za-z]:\\[^\r\n<>|\"*?]+")
    _relative_path_re = re.compile(r"(?<![\w.-])(?:[\w.-]+[\\/]){2,}[\w./\\-]+")
    _number_re = re.compile(r"(?<![\w./\\-])\d[\d,]*(?:\.\d+)?\b")
    _hex_offset_re = re.compile(r"^\s*([0-9A-F]{4})(?=\s)")
    _hex_byte_re = re.compile(r"\b[0-9A-F]{2}\b")

    def __init__(self, document, theme_key: str, highlight_style: str = "rich", color_scheme: str = "theme"):
        super().__init__(document)
        self.current_theme_key = theme_key
        self.highlight_style = _normalize_text_highlight_style(highlight_style)
        self.color_scheme = _normalize_text_color_scheme(color_scheme)
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
        calm = self.highlight_style == "calm"
        scheme = _scheme_palette(theme_key, self.color_scheme)

        def make_format(
            color: str,
            *,
            bold: bool = False,
            italic: bool = False,
        ) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold and not calm:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            return fmt

        self.section_format = make_format((scheme or {}).get("section", theme["accent"] if not calm else theme["text_strong"]), bold=True)
        self.label_format = make_format((scheme or {}).get("key", "#795e26" if light else "#d7ba7d"), bold=True)
        self.path_format = make_format(theme["text_strong"], bold=True)
        self.number_format = make_format((scheme or {}).get("number", "#098658" if light else "#b5cea8"))
        self.warning_format = make_format((scheme or {}).get("warning", theme["warning_text"]), bold=True)
        self.hex_offset_format = make_format((scheme or {}).get("tag", "#0451a5" if light else "#569cd6"), bold=True)
        self.hex_byte_format = make_format((scheme or {}).get("string", "#ce9178" if light else "#d7ba7d"))
        self.muted_format = make_format(theme["text_muted"], italic=True)
        self.rehighlight()

    def set_highlight_style(self, style: str) -> None:
        self.highlight_style = _normalize_text_highlight_style(style)
        self.set_theme(self.current_theme_key)

    def set_color_scheme(self, scheme: str) -> None:
        self.color_scheme = _normalize_text_color_scheme(scheme)
        self.set_theme(self.current_theme_key)

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if self.highlight_style == "plain":
            return
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
    def __init__(
        self,
        *,
        theme_key: str,
        parent: Optional[QWidget] = None,
        highlight_style: str = "rich",
        color_scheme: str = "theme",
    ):
        super().__init__(theme_key=theme_key, parent=parent, highlight_style=highlight_style, color_scheme=color_scheme)
        self.syntax_highlighter = ArchiveDetailsHighlighter(
            self.document(),
            theme_key,
            self._highlight_style,
            self._color_scheme,
        )
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
<p><b>Crimson Desert Mod Workbench</b> es una herramienta de archivos y archivos sueltos para Crimson Desert. Cubre extraccion, investigacion, edicion, reconstruccion DDS, escalado opcional, comparacion y exportacion suelta lista para mods.</p>
<ul>
  <li><b>Explorador de archivos</b>: escanear .pamt/.paz, previsualizar recursos compatibles, filtrar, clasificar y extraer a carpetas sueltas.</li>
  <li><b>Acciones de malla</b>: exportar OBJ/FBX, probar <b>Importar vista de malla</b>, probar texturas con <b>Vista previa de importar DDS</b>, ejecutar <b>Importar malla</b>, alinear reemplazos estaticos y usar <b>Intercambiar con malla del juego</b> cuando otra malla del archivo deba ser el origen.</li>
  <li><b>Flujo de texturas</b>: escanear DDS sueltos, convertir DDS a PNG si hace falta, escalar opcionalmente, reconstruir DDS, comparar resultados y exportar salida mod-ready.</li>
  <li><b>Editor de texturas</b>: abrir imagenes para edicion visible por capas y enviar la salida plana al flujo de reconstruccion.</li>
  <li><b>Asistente de reemplazo</b>: tomar PNG/DDS editados, asociarlos con el DDS original del juego, reconstruir la salida corregida y preparar carpetas mod-ready.</li>
  <li><b>Investigacion</b>: inspeccionar familias de texturas, clasificaciones desconocidas, referencias, analisis DDS, informes y notas locales.</li>
  <li><b>Busqueda de texto</b>: buscar archivos de texto de archivo o sueltos, como .xml, .json, .cfg y .lua.</li>
  <li><b>Configuracion</b>: guardar tema, densidad, cache, estado de layout, confirmaciones y preferencias de inicio.</li>
</ul>
<h3>Configuracion inicial recomendada</h3>
<ol>
  <li>Crea una carpeta dedicada para la app y coloca alli el <b>.exe</b> portable para mantener juntos configuracion, cache, herramientas y workspace.</li>
  <li>Abre <b>Configuracion &gt; Ubicaciones de archivo</b> y define la ruta del juego/paquete de Crimson Desert. Usa deteccion automatica si aplica.</li>
  <li>Abre <b>Configuracion &gt; Setup</b> y haz clic en <b>Inicializar espacio</b>.</li>
  <li>Descarga <b>texconv</b>, coloca <b>texconv.exe</b> bajo la carpeta tools del workspace y configura la ruta de texconv.</li>
  <li>Define <b>Raiz DDS original</b>, <b>Raiz PNG</b> y <b>Raiz de salida</b>. Activa staging DDS solo si quieres una carpeta PNG previa al escalado.</li>
  <li>Elige un backend de escalado: desactivado, <b>Real-ESRGAN NCNN</b> directo o <b>chaiNNer</b>.</li>
  <li>Empieza con una politica de texturas segura y deja las reglas automaticas activadas para preservar mapas tecnicos riesgosos.</li>
  <li>Revisa perfiles, reglas y coincidencias antes de ejecutar un lote.</li>
  <li>Usa <b>Vista de politica</b> antes de <b>Iniciar</b> para revisar la accion planeada por textura.</li>
  <li>Ejecuta un subconjunto pequeno primero y revisa el resultado en <b>Comparar</b>.</li>
  <li>Si ya editaste una textura fuera de la app, usa <b>Asistente de reemplazo</b>.</li>
  <li>Para mallas, empieza en <b>Explorador de archivos</b>: selecciona una malla .pam/.pamlod/.pac, usa <b>Importar vista de malla</b> para probar sin escribir y usa <b>Importar malla</b> solo cuando la alineacion y las texturas se vean correctas.</li>
</ol>
<h3>Guia rapida de mallas</h3>
<ul>
  <li><b>Exportar OBJ/FBX</b>: util para inspeccionar o editar externamente. OBJ es la base de round-trip cuando la app puede escribir los metadatos necesarios.</li>
  <li><b>Importar vista de malla</b>: abre la revision y <b>Alineacion de reemplazo de malla</b> sin escribir salida.</li>
  <li><b>Vista previa de importar DDS</b>: prueba una textura DDS en el modelo seleccionado sin escribir salida.</li>
  <li><b>Importar malla</b>: despues de revisar, permite exportar salida suelta mod-ready o parchear archivos donde sea compatible.</li>
  <li><b>Intercambiar con malla del juego</b>: primero marca la malla seleccionada como destino, luego selecciona otra malla del archivo como origen. La app abre la misma alineacion de reemplazo y puede incluir texturas, sidecars, esqueletos o animaciones relacionadas cuando corresponda.</li>
  <li><b>GLB/glTF/DAE</b>: se tratan como fuentes estaticas. No convierten skins, huesos, animaciones ni grafos PBR complejos a datos nativos del juego.</li>
</ul>
<h3>Areas principales</h3>
<ul>
  <li><b>Configuracion / Setup</b>: creacion de workspace, herramientas externas, enlaces de ayuda e importadores.</li>
  <li><b>Configuracion / Rutas</b>: origen, staging, PNG, salida y raices de exportacion mod-ready.</li>
  <li><b>Salida DDS</b>: formato, tamano, mips y staging globales.</li>
  <li><b>Perfiles, reglas y coincidencias</b>: planificacion reutilizable por archivo.</li>
  <li><b>Escalado</b>: backend, politica, controles NCNN y notas.</li>
  <li><b>Comparar</b>: revision lado a lado antes de lotes grandes.</li>
</ul>
<h3>Nota sobre cache de sidecars</h3>
<p>Crear el cache global de sidecars puede tardar mucho en archivos grandes. Mejora referencias inversas DDS, conexiones de texturas de modelos y busqueda de sidecars/materiales. Si lo activas, deja que termine; se configura en <b>Configuracion &gt; Rendimiento del explorador de archivos</b>.</p>
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
<p><b>Crimson Desert Mod Workbench</b> ist ein Archiv- und Loose-File-Werkzeug fuer Crimson Desert. Es deckt Extraktion, Research, Bearbeitung, DDS-Neuaufbau, optionales Upscaling, Vergleich und mod-fertigen Loose-Export ab.</p>
<ul>
  <li><b>Archiv-Browser</b>: .pamt/.paz scannen, unterstuetzte Assets anzeigen, filtern, klassifizieren und in lose Ordner extrahieren.</li>
  <li><b>Mesh-Aktionen</b>: OBJ/FBX exportieren, <b>Mesh-Importvorschau</b> testen, Texturen mit <b>DDS-Importvorschau</b> pruefen, <b>Mesh importieren</b> ausfuehren, statische Ersetzungen ausrichten und <b>Mit Ingame-Mesh tauschen</b> nutzen, wenn eine andere Archiv-Mesh als Quelle dienen soll.</li>
  <li><b>Textur-Workflow</b>: lose DDS scannen, DDS bei Bedarf zu PNG konvertieren, optional hochskalieren, DDS neu erstellen, Ergebnisse vergleichen und mod-fertige Ausgabe exportieren.</li>
  <li><b>Textur-Editor</b>: Bilder fuer sichtbare Ebenenbearbeitung oeffnen und die flache Ausgabe zurueck in den Neuaufbau senden.</li>
  <li><b>Ersetzungsassistent</b>: bearbeitete PNG/DDS mit dem Original-DDS abgleichen, korrigierte Ausgabe neu erstellen und mod-fertige Ordner vorbereiten.</li>
  <li><b>Recherche</b>: Texturfamilien, unbekannte Klassifizierungen, Referenzen, DDS-Analyse, Berichte und lokale Notizen pruefen.</li>
  <li><b>Textsuche</b>: Archiv- oder lose Textdateien wie .xml, .json, .cfg und .lua durchsuchen.</li>
  <li><b>Einstellungen</b>: Theme, Dichte, Cache, Layoutstatus, Bestaetigungen und Startpraeferenzen speichern.</li>
</ul>
<h3>Empfohlene Starteinrichtung</h3>
<ol>
  <li>Erstelle einen eigenen Ordner fuer die App und lege die portable <b>.exe</b> dort ab, damit Konfiguration, Cache, Tools und Workspace zusammen bleiben.</li>
  <li>Oeffne <b>Einstellungen &gt; Archiv-Orte</b> und setze den Crimson-Desert-Spiel-/Paketpfad. Nutze Auto-Erkennung, wenn moeglich.</li>
  <li>Oeffne <b>Einstellungen &gt; Einrichtung</b> und klicke auf <b>Arbeitsbereich einrichten</b>.</li>
  <li>Lade <b>texconv</b> herunter, lege <b>texconv.exe</b> im tools-Ordner des Workspace ab und konfiguriere den texconv-Pfad.</li>
  <li>Setze <b>Original-DDS-Stamm</b>, <b>PNG-Stamm</b> und <b>Ausgabe-Stamm</b>. Aktiviere DDS-Staging nur fuer einen separaten PNG-Staging-Ordner.</li>
  <li>Waehle ein Upscaling-Backend: deaktiviert, direktes <b>Real-ESRGAN NCNN</b> oder <b>chaiNNer</b>.</li>
  <li>Starte mit einer sicheren Textur-Richtlinie und lasse automatische Regeln aktiv, damit riskante technische Maps erhalten bleiben.</li>
  <li>Pruefe Profile, Regeln und Treffer, bevor du einen Stapellauf startest.</li>
  <li>Nutze <b>Richtlinienvorschau</b> vor <b>Start</b>, um die geplante Aktion pro Textur zu pruefen.</li>
  <li>Fuehre zuerst eine kleine Auswahl aus und pruefe das Ergebnis in <b>Vergleichen</b>.</li>
  <li>Wenn du eine Textur bereits extern bearbeitet hast, nutze den <b>Ersetzungsassistent</b>.</li>
  <li>Fuer Meshes im <b>Archiv-Browser</b> starten: .pam/.pamlod/.pac waehlen, mit <b>Mesh-Importvorschau</b> ohne Schreiben testen und <b>Mesh importieren</b> erst nutzen, wenn Ausrichtung und Texturen korrekt aussehen.</li>
</ol>
<h3>Schnellguide fuer Meshes</h3>
<ul>
  <li><b>OBJ/FBX exportieren</b>: nuetzlich fuer Inspektion oder externe Bearbeitung. OBJ ist die Roundtrip-Basis, wenn die App die noetigen Metadaten schreiben kann.</li>
  <li><b>Mesh-Importvorschau</b>: oeffnet Review und <b>Mesh-Ersetzungsausrichtung</b>, ohne Ausgabe zu schreiben.</li>
  <li><b>DDS-Importvorschau</b>: testet eine DDS-Textur am gewaehlten Modell, ohne Ausgabe zu schreiben.</li>
  <li><b>Mesh importieren</b>: nach der Pruefung mod-fertige Loose-Ausgabe oder Patch schreiben, wo kompatibel.</li>
  <li><b>Mit Ingame-Mesh tauschen</b>: zuerst die ausgewaehlte Mesh als Ziel markieren, dann eine andere Archiv-Mesh als Quelle waehlen. Die App oeffnet dieselbe Ersetzungsausrichtung und kann passende Texturen, Sidecars, Skelette oder Animationen einschliessen.</li>
  <li><b>GLB/glTF/DAE</b>: werden als statische Quellen behandelt. Skins, Knochen, Animationen und komplexe PBR-Graphen werden nicht in native Spieldaten konvertiert.</li>
</ul>
<h3>Hauptbereiche</h3>
<ul>
  <li><b>Einstellungen / Einrichtung</b>: Workspace-Erstellung, externe Tools, Hilfelinks und Importhelfer.</li>
  <li><b>Einstellungen / Pfade</b>: Quelle, Staging, PNG, Ausgabe und mod-fertige Exportstaemme.</li>
  <li><b>DDS-Ausgabe</b>: globale Format-, Groessen-, Mip- und Staging-Regeln.</li>
  <li><b>Profile, Regeln und Treffer</b>: wiederverwendbare Planung pro Datei.</li>
  <li><b>Upscaling</b>: Backend, Richtlinie, NCNN-Steuerung und Notizen.</li>
  <li><b>Vergleichen</b>: Seit-an-Seit-Pruefung vor groesseren Laeufen.</li>
</ul>
<h3>Hinweis zum Sidecar-Cache</h3>
<p>Der globale Sidecar-Cache kann bei grossen Archiven lange dauern. Er verbessert DDS-Rueckreferenzen, Modell-Textur-Verbindungen und Material-Sidecar-Suche. Wenn du ihn aktivierst, lass den ersten Lauf fertig werden; die Optionen findest du unter <b>Einstellungen &gt; Archiv-Browser-Leistung</b>.</p>
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
            "Start by putting the portable EXE in its own app folder, setting the Crimson Desert game/package path in Settings > Archive Locations, then clicking Init Workspace. Configure texconv before judging DDS preview or rebuild failures."
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
            <p><b>Crimson Desert Mod Workbench</b> is a read-only archive and loose-file workflow tool for Crimson Desert. It is built around extraction, research, editing, DDS rebuild, optional upscaling, comparison, and mod-ready loose export.</p>
            <ul>
              <li><b>Archive Browser</b>: scan <b>.pamt/.paz</b>, preview supported assets, filter, classify, and extract to loose folders.</li>
              <li><b>Mesh Actions</b>: export OBJ/FBX, test <b>Import Mesh Preview</b>, preview texture overrides with <b>Import DDS Preview</b>, run <b>Import Mesh</b>, align static replacements, and use <b>Swap With In-Game Mesh</b> when another loaded archive mesh should become the source.</li>
              <li><b>Texture Workflow</b>: scan loose DDS files, convert DDS to PNG when needed, optionally upscale, rebuild DDS, compare results, and export loose mod output.</li>
              <li><b>Texture Editor</b>: open images directly for layered visible-texture editing and send flattened output back into the rebuild flow.</li>
              <li><b>Replace Assistant</b>: take edited PNG/DDS files, match them to the original game DDS, rebuild corrected output, and prepare mod-ready folders.</li>
              <li><b>Research</b>: inspect grouped texture families, unknown classifications, references, DDS analysis, reports, and local notes.</li>
              <li><b>Text Search</b>: search archive or loose text-like files such as <b>.xml</b>, <b>.json</b>, <b>.cfg</b>, and <b>.lua</b>.</li>
              <li><b>Settings</b>: store theme, density, cache behavior, remembered layout state, confirmations, and startup preferences beside the EXE.</li>
            </ul>
            <h3>Recommended Startup Setup</h3>
            <ol>
              <li>Create or choose a dedicated folder for the app, then place the portable <b>.exe</b> there so config, cache, tools, and workspace folders stay together.</li>
              <li>Open <b>Settings &gt; Archive Locations</b> and set the Crimson Desert game/package path. Use <b>Auto-detect</b> if the game is in a common install location.</li>
              <li>Open <b>Settings &gt; Setup</b> and click <b>Init Workspace</b>.</li>
              <li>Download <b>texconv</b> from the DirectXTex releases page, place <b>texconv.exe</b> under the workspace tools folder, then set <b>texconv.exe path</b>.</li>
              <li>Confirm <b>Original DDS root</b>, <b>PNG root</b>, and <b>Output root</b>. Enable DDS staging only if you want a separate pre-upscale PNG staging folder.</li>
              <li>Choose an upscaling backend in <b>Upscaling</b>: disabled, direct <b>Real-ESRGAN NCNN</b>, or <b>chaiNNer</b>.</li>
              <li>Keep a safer <b>Texture Policy</b> preset first and leave automatic rules enabled so risky technical DDS files are preserved instead of pushed through the visible PNG path.</li>
              <li>Open <b>Profiles, Rules &amp; Matches</b> and review the starter workflow assignments before running a batch.</li>
              <li>Use <b>Preview Policy</b> before <b>Start</b> if you want to inspect the planned per-texture action.</li>
              <li>Click <b>Scan</b> in the Texture Workflow tab.</li>
              <li>Run a small subset first, then review the output in <b>Compare</b> before trying a larger batch.</li>
              <li>If you already edited a texture outside the app, use <b>Replace Assistant</b> instead of the batch workflow.</li>
              <li>If you want to edit visible textures inside the app, open them in <b>Texture Editor</b> and then send the flattened result back into <b>Replace Assistant</b> or <b>Texture Workflow</b>.</li>
              <li>For mesh work, start in <b>Archive Browser</b>: select a <b>.pam</b>, <b>.pamlod</b>, or <b>.pac</b>, use <b>Import Mesh Preview</b> to test without writing, and use <b>Import Mesh</b> only after alignment and texture choices look correct.</li>
            </ol>
            <h3>Mesh Quick Guide</h3>
            <ul>
              <li><b>Export OBJ/FBX</b>: use this for inspection or external editing. OBJ is the round-trip baseline when the app can write the companion metadata needed for import.</li>
              <li><b>Import Mesh Preview</b>: opens review and <b>Mesh Replacement Alignment</b> without writing archive or loose output.</li>
              <li><b>Import DDS Preview</b>: tests a DDS texture override on the selected model without writing output.</li>
              <li><b>Import Mesh</b>: after review, writes a supported replacement as mod-ready loose output or an archive patch where that workflow is available.</li>
              <li><b>Swap With In-Game Mesh</b>: first mark the selected archive mesh as the target, then choose another loaded archive mesh as the source. The app opens the same replacement alignment flow and can carry related textures, sidecars, skeletons, or animations when appropriate.</li>
              <li><b>GLB/glTF/DAE</b>: treated as static replacement sources. Skins, bones, animations, and complex PBR material graphs are not converted into native game material data.</li>
            </ul>
            <h3>Pick The Right Starting Path</h3>
            <ul>
              <li><b>I want to look inside the game files</b>: open <b>Archive Browser</b>, choose a package root, scan, filter, preview, and extract selected files.</li>
              <li><b>I want to replace a model</b>: use <b>Archive Browser</b> mesh actions, start with <b>Import Mesh Preview</b>, then continue to <b>Import Mesh</b> or <b>Swap With In-Game Mesh</b> after checking alignment.</li>
              <li><b>I want to batch-process loose DDS files</b>: use <b>Texture Workflow</b> with a small folder first, then review in <b>Compare</b>.</li>
              <li><b>I already edited one texture</b>: use <b>Replace Assistant</b> so the original DDS controls format, dimensions, mips, and output path.</li>
              <li><b>I want to edit inside the app</b>: use <b>Texture Editor</b>, save a project if you need layers later, then export or send the flattened PNG onward.</li>
              <li><b>I need to understand what a texture family is</b>: use <b>Research</b> for grouped sets, classifications, references, analysis, and notes.</li>
              <li><b>I am searching for XML, JSON, Lua, or config strings</b>: use <b>Text Search</b> against archives or loose folders.</li>
            </ul>
            <h3>Sidecar Cache Note</h3>
            <p>Building the global sidecar cache is intentionally optional because it can be expensive on large archives. It improves DDS related-file discovery, reverse references, mesh texture connections, and material-sidecar lookup. If you enable it, let the first run finish even when it takes a long time. Configure sidecar indexing and worker count in <b>Settings &gt; Archive Browser Performance</b>.</p>
            <h3>Safety Reminders</h3>
            <p>Visible color textures are not the same as technical maps. Height, displacement, normals, masks, vectors, and other precision-sensitive DDS files are riskier to push through PNG intermediates.</p>
            <ul>
              <li>Start with a safer preset.</li>
              <li>Keep automatic rules enabled.</li>
              <li>Use preview-only paths before writing mesh or archive output.</li>
              <li>Open Documentation for detailed field references, recipes, troubleshooting, and FAQs.</li>
            </ul>
            <h3>Where Details Live</h3>
            <p>The <b>Documentation</b> menu is topic-based and searchable. Use it for mesh import/swap steps, archive guides, Texture Workflow profiles and rules, Texture Editor tools, Replace Assistant packaging, Research, Text Search, settings, troubleshooting, and FAQs.</p>
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
        self.open_archive_locations_button = QPushButton("Open Archive Locations")
        self.open_setup_button = QPushButton("Open Setup && Paths")
        self.open_chainner_button = QPushButton("Open chaiNNer Setup")
        self.open_docs_button = QPushButton("Open Documentation")
        self.close_button = QPushButton("Close")
        button_row.addWidget(self.open_archive_locations_button)
        button_row.addWidget(self.open_setup_button)
        button_row.addWidget(self.open_chainner_button)
        button_row.addWidget(self.open_docs_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.open_archive_locations_button.clicked.connect(self._open_archive_locations)
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

    def _open_archive_locations(self) -> None:
        self.parent_window.focus_archive_locations()
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

        self.intro_html = intro_html
        guide_label = QLabel(
            "Search or choose a topic on the left. The reader shows one topic at a time so longer documentation stays navigable."
        )
        guide_label.setObjectName("HintLabel")
        guide_label.setWordWrap(True)
        layout.addWidget(guide_label)

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
        self.browser.setFont(self.font())
        self.browser.document().setDefaultFont(self.font())
        self.browser.setProperty("_i18n_source_html", "")
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
            self._select_first_topic()

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

    def _build_section_html(self, section: Dict[str, str]) -> str:
        section_id = str(section.get("id", "") or "").strip()
        section_title = str(section.get("title", "") or "").strip() or "Documentation"
        section_summary = str(section.get("summary", "") or "").strip()
        section_body = str(section.get("html", "") or "")
        category = self._section_category(section)
        summary_html = f"<p><i>{section_summary}</i></p>" if section_summary else ""
        category_html = f"<p><b>{category}</b></p>" if category else ""
        css = """
        <style>
        h2 { margin-top: 0; }
        h4 { margin-bottom: 4px; }
        table { border-collapse: collapse; width: 100%; margin: 8px 0 12px 0; }
        th, td { border: 1px solid #6b7280; padding: 5px 7px; vertical-align: top; }
        th { background: rgba(127, 127, 127, 0.18); font-weight: 600; }
        .doc-callout { border-left: 4px solid #3b82f6; padding: 7px 10px; margin: 8px 0; background: rgba(59, 130, 246, 0.10); }
        .doc-warning { border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.12); }
        .doc-danger { border-left-color: #ef4444; background: rgba(239, 68, 68, 0.10); }
        .doc-ok { border-left-color: #22c55e; background: rgba(34, 197, 94, 0.10); }
        .pill { border: 1px solid #6b7280; border-radius: 4px; padding: 1px 4px; white-space: nowrap; }
        </style>
        """
        if section_id == "overview":
            return f"{css}<h2>{section_title}</h2>{category_html}{summary_html}{self.intro_html}<hr/>{section_body}"
        return f"{css}<h2>{section_title}</h2>{category_html}{summary_html}{section_body}"

    @staticmethod
    def _topic_search_text(section: Dict[str, str]) -> str:
        title = str(section.get("title", "") or "")
        keywords = str(section.get("keywords", "") or "")
        body = str(section.get("html", "") or "")
        plain_body = re.sub(r"<[^>]+>", " ", body)
        return f"{title}\n{keywords}\n{plain_body}".lower()

    @staticmethod
    def _section_category(section: Dict[str, str]) -> str:
        category = str(section.get("category", "") or "").strip()
        if category:
            return category
        section_id = str(section.get("id", "") or "").strip()
        if section_id in {"overview", "quick_start", "first_run_checklist", "faq"}:
            return "Start Here"
        if section_id.startswith("workflow_") or section_id in {"dds_output", "upscaling_backends", "texture_workflow_guides", "compare_review"}:
            return "Texture Workflow"
        if section_id in {"archive_browser", "archive_guides", "mesh_media_guides"}:
            return "Archive Browser"
        if section_id in {"texture_editor", "replace_assistant", "research", "text_search"}:
            return "Tools"
        if section_id in {"mod_packaging", "safety", "settings_files", "troubleshooting"}:
            return "Reference"
        return "Other"

    @staticmethod
    def _category_sort_key(category: str) -> Tuple[int, str]:
        order = {
            "Start Here": 0,
            "Texture Workflow": 1,
            "Archive Browser": 2,
            "Tools": 3,
            "Reference": 4,
            "Other": 99,
        }
        return (order.get(category, 50), category.lower())

    def _localized_category_label(self, category: str) -> str:
        language_code = self._current_language_code()
        labels = {
            "es": {
                "Start Here": "Primeros pasos",
                "Texture Workflow": "Flujo de texturas",
                "Archive Browser": "Explorador de archivos",
                "Tools": "Herramientas",
                "Reference": "Referencia",
                "Other": "Otros",
            },
            "de": {
                "Start Here": "Start",
                "Texture Workflow": "Textur-Workflow",
                "Archive Browser": "Archiv-Browser",
                "Tools": "Werkzeuge",
                "Reference": "Referenz",
                "Other": "Weitere Themen",
            },
        }
        return labels.get(language_code, {}).get(category, category)

    def _add_topic_group_header(self, category: str) -> None:
        item = QListWidgetItem("")
        item.setFlags(Qt.NoItemFlags)
        item.setData(Qt.UserRole, "")
        item.setSizeHint(QSize(0, 30))
        self.topic_list.addItem(item)

        header_widget = QWidget()
        header_widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(8, 5, 8, 3)
        header_layout.setSpacing(8)

        label = QLabel(self._localized_category_label(category).upper())
        label_font = QFont(self.topic_list.font())
        label_font.setBold(True)
        label_font.setPointSize(max(8, label_font.pointSize() - 1))
        label.setFont(label_font)
        label.setAttribute(Qt.WA_TransparentForMouseEvents)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Plain)
        divider.setAttribute(Qt.WA_TransparentForMouseEvents)
        divider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        palette = self.topic_list.palette()
        muted = palette.color(QPalette.Disabled, QPalette.Text)
        if not muted.isValid():
            muted = palette.color(QPalette.Text)
        divider_color = palette.color(QPalette.Mid)
        if not divider_color.isValid():
            divider_color = muted
        label.setStyleSheet(f"color: {muted.name()};")
        divider.setStyleSheet(f"color: {divider_color.name()}; background: {divider_color.name()}; max-height: 1px;")

        header_layout.addWidget(label, stretch=0)
        header_layout.addWidget(divider, stretch=1)
        self.topic_list.setItemWidget(item, header_widget)

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
        grouped_sections: Dict[str, List[Dict[str, str]]] = {}
        for section in self._filtered_sections:
            grouped_sections.setdefault(self._section_category(section), []).append(section)
        for category in sorted(grouped_sections, key=self._category_sort_key):
            self._add_topic_group_header(category)
            for section in grouped_sections[category]:
                item = QListWidgetItem(str(section.get("title", "") or "Untitled"))
                item.setData(Qt.UserRole, str(section.get("id", "") or ""))
                item.setForeground(QBrush(self.topic_list.palette().color(QPalette.Text)))
                summary = str(section.get("summary", "") or "")
                if summary:
                    item.setToolTip(summary)
                self.topic_list.addItem(item)
        self.topic_list.blockSignals(False)
        self.topic_count_label.setText(self._format_topic_count(len(self._filtered_sections)))
        if not self._filtered_sections:
            self.browser.setHtml(
                "<h2>No Matching Topics</h2><p>Try a broader search term such as <b>DDS</b>, <b>archive</b>, <b>profile</b>, <b>replace</b>, or <b>FAQ</b>.</p>"
            )
            return
        if current_section_id:
            for index in range(self.topic_list.count()):
                item = self.topic_list.item(index)
                if str(item.data(Qt.UserRole) or "") == current_section_id:
                    self.topic_list.setCurrentItem(item)
                    return
        self._select_first_topic()

    def _select_first_topic(self) -> None:
        for index in range(self.topic_list.count()):
            item = self.topic_list.item(index)
            if str(item.data(Qt.UserRole) or ""):
                self.topic_list.setCurrentItem(item)
                return

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
                self._render_section(target_id)
                return
        self.search_edit.clear()
        for index in range(self.topic_list.count()):
            item = self.topic_list.item(index)
            if str(item.data(Qt.UserRole) or "") == target_id:
                self.topic_list.setCurrentItem(item)
                self._render_section(target_id)
                return

    def _handle_topic_changed(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        if current is None:
            return
        self._render_section(str(current.data(Qt.UserRole) or ""))

    def _scroll_to_section(self, section_id: str) -> None:
        if not section_id:
            return
        QTimer.singleShot(0, lambda: self.browser.scrollToAnchor(section_id))

    def _render_section(self, section_id: str) -> None:
        if not section_id:
            return
        for section in self._sections:
            if str(section.get("id", "") or "") == section_id:
                html = self._build_section_html(section)
                self.browser.setProperty("_i18n_source_html", html)
                self.browser.setHtml(html)
                QTimer.singleShot(0, lambda: self.browser.moveCursor(QTextCursor.Start))
                return

    def _handle_anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)
            return
        target_id = url.fragment().strip()
        if not target_id and url.scheme() == "topic":
            target_id = url.path().strip("/").strip()
        if target_id:
            self.select_section(target_id)
