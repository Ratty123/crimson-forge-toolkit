from __future__ import annotations

import dataclasses
import html
import json
import math
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPalette,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QPushButton,
    QKeySequenceEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSlider,
    QSplitter,
    QTabBar,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from crimson_forge_toolkit.constants import APP_TITLE
from crimson_forge_toolkit.core.research import summarize_ui_reference_constraints
from crimson_forge_toolkit.core.texture_editor import (
    _blend_layer_region,
    add_texture_editor_layer,
    apply_texture_editor_lasso_selection,
    apply_texture_editor_recolor,
    apply_texture_editor_rect_selection,
    apply_texture_editor_fill,
    apply_texture_editor_gradient,
    apply_texture_editor_patch,
    apply_texture_editor_selection_fill,
    apply_texture_editor_selection_stroke,
    apply_texture_editor_selection_to_layer_mask,
    apply_texture_editor_stroke,
    bump_texture_editor_layer_revision,
    build_texture_editor_selection_mask,
    capture_texture_editor_snapshot,
    clear_texture_editor_selection,
    copy_texture_editor_layer_channel,
    create_texture_editor_document_from_source,
    create_texture_editor_layer_mask,
    crop_texture_editor_document_to_selection,
    delete_texture_editor_layer_mask,
    duplicate_texture_editor_layer,
    extract_texture_editor_selection,
    extract_texture_editor_layer_channel_to_rgba,
    export_texture_editor_flattened_png,
    export_texture_editor_grid_slices,
    export_texture_editor_region_png,
    flatten_texture_editor_layers,
    flatten_texture_editor_layers_region,
    flip_texture_editor_document,
    grow_texture_editor_selection,
    invert_texture_editor_layer_mask,
    load_texture_editor_layer_mask_as_selection,
    load_texture_editor_layer_channel_as_selection,
    load_texture_editor_project,
    make_texture_editor_workspace_root,
    merge_texture_editor_layer_down,
    move_texture_editor_layer,
    remove_texture_editor_layer,
    remove_texture_editor_adjustment_layer,
    reorder_texture_editor_layer,
    resize_texture_editor_document_canvas,
    resize_texture_editor_document_image,
    restore_texture_editor_snapshot,
    rotate_texture_editor_document_90,
    set_texture_editor_layer_mask_enabled,
    save_texture_editor_project,
    select_all_texture_editor,
    shrink_texture_editor_selection,
    snap_lasso_points_to_edges,
    swap_texture_editor_layer_channels,
    trim_texture_editor_document_transparent_bounds,
    add_texture_editor_adjustment_layer,
    update_texture_editor_adjustment_layer,
    update_texture_editor_selection_settings,
    update_texture_editor_layer,
    paste_texture_editor_channel_into_layer,
    write_texture_editor_selection_to_layer_channel,
    write_texture_editor_layer_luma_to_channel,
)
from crimson_forge_toolkit.models import (
    TextureEditorAdjustmentLayer,
    TextureEditorCommand,
    TextureEditorDocument,
    TextureEditorFloatingSelection,
    TextureEditorHistoryEntry,
    TextureEditorSelection,
    TextureEditorSourceBinding,
    TextureEditorToolSettings,
    TextureEditorWorkspace,
)
from crimson_forge_toolkit.ui.widgets import build_responsive_splitter_sizes


def _rgba_array_to_qimage(array: np.ndarray) -> QImage:
    rgba = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = rgba.shape[:2]
    image = QImage(rgba.data, width, height, width * 4, QImage.Format_RGBA8888)
    return image.copy()


def _create_tool_icon(tool_key: str, palette: Optional[QPalette] = None) -> QIcon:
    size = 20
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    app = QApplication.instance()
    resolved_palette = QPalette(palette or (app.palette() if app is not None else QPalette()))
    accent = QColor(resolved_palette.color(QPalette.Highlight))
    primary = QColor(resolved_palette.color(QPalette.ButtonText))
    subtle = QColor(resolved_palette.color(QPalette.PlaceholderText))
    if not subtle.isValid() or subtle.alpha() == 0:
        subtle = QColor(primary)
        subtle.setAlpha(170)
    accent.setAlpha(230)

    def _pen(color: QColor, width: float = 1.8, *, dashed: bool = False) -> QPen:
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        if dashed:
            pen.setStyle(Qt.DashLine)
        return pen

    painter.setBrush(Qt.NoBrush)
    if tool_key == "paint":
        painter.setPen(_pen(primary, 2.0))
        painter.drawLine(4, 15, 12, 7)
        painter.setBrush(QBrush(accent))
        painter.setPen(_pen(accent, 1.5))
        painter.drawEllipse(12, 4, 4, 4)
    elif tool_key == "erase":
        painter.setBrush(QBrush(accent))
        painter.setPen(_pen(primary, 1.6))
        path = QPainterPath()
        path.moveTo(5, 13)
        path.lineTo(10, 6)
        path.lineTo(16, 10)
        path.lineTo(11, 16)
        path.closeSubpath()
        painter.drawPath(path)
    elif tool_key == "sharpen":
        painter.setPen(_pen(primary, 1.8))
        painter.drawLine(10, 3, 10, 17)
        painter.drawLine(3, 10, 17, 10)
        painter.drawLine(5, 5, 15, 15)
        painter.drawLine(15, 5, 5, 15)
    elif tool_key == "soften":
        painter.setPen(_pen(primary, 1.2))
        painter.setBrush(QBrush(accent))
        painter.drawEllipse(5, 5, 10, 10)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_pen(subtle, 1.2))
        painter.drawEllipse(3, 3, 14, 14)
    elif tool_key == "clone":
        painter.setPen(_pen(primary, 1.6))
        painter.drawEllipse(4, 6, 7, 7)
        painter.drawEllipse(9, 6, 7, 7)
        painter.drawLine(8, 13, 8, 17)
        painter.drawLine(12, 13, 12, 17)
    elif tool_key == "heal":
        painter.setPen(_pen(primary, 1.8))
        painter.drawEllipse(4, 4, 12, 12)
        painter.drawLine(10, 6, 10, 14)
        painter.drawLine(6, 10, 14, 10)
    elif tool_key == "move":
        painter.setPen(_pen(primary, 1.8))
        painter.drawLine(10, 3, 10, 17)
        painter.drawLine(3, 10, 17, 10)
        painter.drawLine(10, 3, 8, 5)
        painter.drawLine(10, 3, 12, 5)
        painter.drawLine(10, 17, 8, 15)
        painter.drawLine(10, 17, 12, 15)
        painter.drawLine(3, 10, 5, 8)
        painter.drawLine(3, 10, 5, 12)
        painter.drawLine(17, 10, 15, 8)
        painter.drawLine(17, 10, 15, 12)
    elif tool_key == "fill":
        painter.setPen(_pen(primary, 1.6))
        bucket = QPainterPath()
        bucket.moveTo(5, 8)
        bucket.lineTo(9, 4)
        bucket.lineTo(15, 10)
        bucket.lineTo(11, 14)
        bucket.closeSubpath()
        painter.setBrush(QBrush(accent))
        painter.drawPath(bucket)
        painter.setBrush(QBrush(QColor("#C6E4FF")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(11, 13, 5, 3)
    elif tool_key == "gradient":
        painter.setPen(Qt.NoPen)
        gradient_path = QPainterPath()
        gradient_path.moveTo(4, 14)
        gradient_path.lineTo(16, 6)
        painter.setPen(_pen(primary, 1.4))
        painter.drawLine(4, 14, 16, 6)
        painter.setBrush(QBrush(accent))
        painter.drawEllipse(3, 13, 3, 3)
        warm = QColor(accent)
        warm = warm.lighter(130 if primary.lightness() < 160 else 85)
        warm.setAlpha(220)
        painter.setBrush(QBrush(warm))
        painter.drawEllipse(14, 5, 3, 3)
    elif tool_key == "smudge":
        painter.setPen(_pen(primary, 1.5))
        painter.drawLine(4, 15, 9, 10)
        painter.drawLine(9, 10, 14, 12)
        painter.setBrush(QBrush(accent))
        painter.setPen(_pen(accent, 1.3))
        painter.drawEllipse(12, 4, 4, 7)
    elif tool_key == "dodge_burn":
        painter.setPen(_pen(primary, 1.5))
        warm = QColor(accent)
        warm = warm.lighter(140 if primary.lightness() < 160 else 90)
        warm.setAlpha(220)
        painter.setBrush(QBrush(warm))
        painter.drawEllipse(3, 8, 6, 6)
        cool = QColor(subtle)
        cool.setAlpha(220)
        painter.setBrush(QBrush(cool))
        painter.drawEllipse(11, 6, 6, 8)
        painter.setPen(_pen(accent, 1.3))
        painter.drawLine(8, 11, 12, 10)
    elif tool_key == "patch":
        painter.setPen(_pen(primary, 1.4, dashed=True))
        painter.drawRect(3, 4, 7, 7)
        painter.setPen(_pen(accent, 1.4))
        painter.drawLine(10, 7, 15, 12)
        painter.drawRect(11, 11, 5, 5)
    elif tool_key == "select_rect":
        painter.setPen(_pen(primary, 1.5, dashed=True))
        painter.drawRect(4, 4, 12, 10)
    elif tool_key == "lasso":
        painter.setPen(_pen(primary, 1.8))
        path = QPainterPath()
        path.moveTo(5, 8)
        path.cubicTo(4, 2, 16, 2, 15, 9)
        path.cubicTo(14, 15, 7, 16, 7, 12)
        painter.drawPath(path)
        painter.drawLine(7, 12, 10, 17)
    elif tool_key == "recolor":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#FF8C7A")))
        painter.drawEllipse(3, 8, 6, 6)
        painter.setBrush(QBrush(QColor("#7CCB7A")))
        painter.drawEllipse(7, 4, 6, 6)
        painter.setBrush(QBrush(QColor("#74C1FF")))
        painter.drawEllipse(11, 8, 6, 6)
    painter.end()
    return QIcon(pixmap)


@dataclasses.dataclass
class _TextureEditorSession:
    label: str
    document: Optional[TextureEditorDocument]
    layer_pixels: Dict[str, np.ndarray]
    history_snapshots: List[Dict[str, object]]
    history_index: int
    original_flattened: Optional[np.ndarray] = None
    layer_property_dirty: bool = False
    floating_pixels: Optional[np.ndarray] = None
    floating_mask: Optional[np.ndarray] = None
    composite_cache: Optional[np.ndarray] = None
    composite_cache_revision: int = -1
    composite_dirty_bounds: Optional[Tuple[int, int, int, int]] = None
    thumbnail_cache: Dict[Tuple[str, int], QIcon] = dataclasses.field(default_factory=dict)


class CollapsibleSection(QWidget):
    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        content_widget: QWidget,
        *,
        expanded: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._content_widget = content_widget
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.toggle_button = QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle_button.setMinimumHeight(26)
        self.toggle_button.setObjectName("SectionToggle")
        self.toggle_button.clicked.connect(self.set_expanded)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self._content_widget)
        self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self.toggle_button.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.blockSignals(False)
        self.toggle_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._content_widget.setVisible(expanded)
        self.toggled.emit(expanded)


class ShortcutEditorDialog(QDialog):
    def __init__(
        self,
        *,
        shortcuts: Dict[str, str],
        labels: Dict[str, str],
        defaults: Dict[str, str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Texture Editor Shortcuts")
        self._defaults = defaults
        self._edits: Dict[str, QKeySequenceEdit] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        hint = QLabel("Set the shortcuts you want for common Texture Editor actions.")
        hint.setWordWrap(True)
        hint.setObjectName("HintLabel")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        for key, label in labels.items():
            edit = QKeySequenceEdit()
            edit.setKeySequence(QKeySequence(shortcuts.get(key, defaults.get(key, ""))))
            self._edits[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        defaults_button = buttons.addButton("Defaults", QDialogButtonBox.ResetRole)
        defaults_button.clicked.connect(self.reset_to_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reset_to_defaults(self) -> None:
        for key, edit in self._edits.items():
            edit.setKeySequence(QKeySequence(self._defaults.get(key, "")))

    def shortcut_map(self) -> Dict[str, str]:
        return {key: edit.keySequence().toString(QKeySequence.NativeText) for key, edit in self._edits.items()}


class TextureEditorNavigator(QWidget):
    center_requested = Signal(float, float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._image_width = 0
        self._image_height = 0
        self._viewport_rect: Optional[Tuple[float, float, float, float]] = None
        self._dragging = False
        self.setMinimumSize(170, 120)
        self.setMaximumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(
        self,
        image: Optional[QImage],
        *,
        image_width: int,
        image_height: int,
        viewport_rect: Optional[Tuple[float, float, float, float]],
    ) -> None:
        self._image = image.copy() if image is not None else None
        self._image_width = max(0, int(image_width))
        self._image_height = max(0, int(image_height))
        self._viewport_rect = viewport_rect
        self.update()

    def _target_rect(self) -> QRectF:
        if self._image_width <= 0 or self._image_height <= 0:
            return QRectF()
        inner = QRectF(8.0, 8.0, max(1.0, float(self.width()) - 16.0), max(1.0, float(self.height()) - 16.0))
        scale = min(inner.width() / float(self._image_width), inner.height() / float(self._image_height))
        width = float(self._image_width) * scale
        height = float(self._image_height) * scale
        x = inner.x() + ((inner.width() - width) / 2.0)
        y = inner.y() + ((inner.height() - height) / 2.0)
        return QRectF(x, y, width, height)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1B202A"))
        target = self._target_rect()
        if target.isEmpty():
            painter.setPen(QColor("#8B97AA"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Navigator")
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
        painter.setBrush(QColor(255, 255, 255, 6))
        painter.drawRoundedRect(target, 6, 6)
        if self._image is not None:
            painter.drawImage(target, self._image)
        else:
            painter.setPen(QColor("#8B97AA"))
            painter.drawText(target.toRect(), Qt.AlignCenter, "No preview")
        if self._viewport_rect is not None and self._image_width > 0 and self._image_height > 0:
            vx, vy, vw, vh = self._viewport_rect
            view = QRectF(
                target.x() + ((vx / float(self._image_width)) * target.width()),
                target.y() + ((vy / float(self._image_height)) * target.height()),
                max(6.0, (vw / float(self._image_width)) * target.width()),
                max(6.0, (vh / float(self._image_height)) * target.height()),
            )
            view = view.intersected(target)
            painter.setBrush(QColor(116, 193, 255, 30))
            painter.setPen(QPen(QColor("#74C1FF"), 1.4))
            painter.drawRoundedRect(view, 4, 4)

    def _emit_center_request(self, pos) -> None:
        target = self._target_rect()
        if target.isEmpty() or self._image_width <= 0 or self._image_height <= 0 or not target.contains(pos):
            return
        ratio_x = (float(pos.x()) - target.x()) / max(1.0, target.width())
        ratio_y = (float(pos.y()) - target.y()) / max(1.0, target.height())
        image_x = max(0.0, min(float(self._image_width), ratio_x * float(self._image_width)))
        image_y = max(0.0, min(float(self._image_height), ratio_y * float(self._image_height)))
        self.center_requested.emit(image_x, image_y)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._emit_center_request(event.position())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._dragging:
            self._emit_center_request(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._dragging = False


class TextureEditorRuler(QWidget):
    def __init__(self, orientation: Qt.Orientation, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._orientation = orientation
        self._image_length = 0
        self._other_length = 0
        self._display_scale = 1.0
        self._scroll_value = 0
        self._viewport_offset = 0
        self._hover_position: Optional[int] = None
        self._guides: Tuple[int, ...] = ()
        if orientation == Qt.Horizontal:
            self.setFixedHeight(22)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setFixedWidth(22)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_state(
        self,
        *,
        image_length: int,
        other_length: int,
        display_scale: float,
        scroll_value: int,
        viewport_offset: int,
        hover_position: Optional[int],
        guides: Sequence[int],
    ) -> None:
        self._image_length = max(0, int(image_length))
        self._other_length = max(0, int(other_length))
        self._display_scale = max(0.0001, float(display_scale))
        self._scroll_value = max(0, int(scroll_value))
        self._viewport_offset = int(viewport_offset)
        self._hover_position = None if hover_position is None else int(hover_position)
        self._guides = tuple(int(value) for value in guides)
        self.update()

    def _tick_step(self) -> int:
        if self._display_scale <= 0:
            return 100
        desired = 80.0 / self._display_scale
        magnitude = 1
        while magnitude * 10 <= desired:
            magnitude *= 10
        for factor in (1, 2, 5, 10):
            candidate = magnitude * factor
            if candidate >= desired:
                return max(1, int(candidate))
        return max(1, int(magnitude * 10))

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1C212B"))
        if self._image_length <= 0:
            return
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
        if self._orientation == Qt.Horizontal:
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
            visible_pixels = self.width() / self._display_scale
        else:
            painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
            visible_pixels = self.height() / self._display_scale
        start_pixel = -float(self._viewport_offset) / self._display_scale
        end_pixel = min(float(self._image_length), start_pixel + visible_pixels)
        step = self._tick_step()
        first_tick = int(math.floor(start_pixel / float(step)) * step)
        painter.setPen(QPen(QColor("#A9B6CB"), 1))
        value = first_tick
        while value <= end_pixel + step:
            widget_pos = (float(value) - start_pixel) * self._display_scale
            if self._orientation == Qt.Horizontal:
                x = int(round(widget_pos))
                painter.drawLine(x, self.height() - 8, x, self.height())
                painter.drawText(x + 2, 12, str(value))
            else:
                y = int(round(widget_pos))
                painter.drawLine(self.width() - 8, y, self.width(), y)
                painter.save()
                painter.translate(8, y + 16)
                painter.rotate(-90)
                painter.drawText(0, 0, str(value))
                painter.restore()
            value += step
        guide_pen = QPen(QColor(116, 193, 255, 140), 1)
        hover_pen = QPen(QColor("#F2C14E"), 1)
        for guide in self._guides:
            pos = self._viewport_offset + (float(guide) * self._display_scale)
            painter.setPen(guide_pen)
            if self._orientation == Qt.Horizontal:
                x = int(round(pos))
                painter.drawLine(x, 0, x, self.height())
            else:
                y = int(round(pos))
                painter.drawLine(0, y, self.width(), y)
        if self._hover_position is not None:
            pos = self._viewport_offset + (float(self._hover_position) * self._display_scale)
            painter.setPen(hover_pen)
            if self._orientation == Qt.Horizontal:
                x = int(round(pos))
                painter.drawLine(x, 0, x, self.height())
            else:
                y = int(round(pos))
                painter.drawLine(0, y, self.width(), y)


class TextureEditorTaskWorker(QObject):
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, task: Callable[[], object]) -> None:
        super().__init__()
        self._task = task
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            result = self._task()
            if not self.stop_event.is_set():
                self.completed.emit(result)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(str(exc))
        finally:
            self.finished.emit()


class TextureEditorUIConstraintWorker(QObject):
    completed = Signal(str, str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, archive_entries: Sequence[object], target_path: str) -> None:
        super().__init__()
        self._archive_entries = list(archive_entries)
        self._target_path = str(target_path or "").strip()
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set() or not self._target_path:
                return
            warning_text = ""
            summary = summarize_ui_reference_constraints(
                self._archive_entries,
                self._target_path,
                stop_event=self.stop_event,
            )
            if not self.stop_event.is_set():
                warning_text = str(summary.get("warning_text", "") or "")
                self.completed.emit(self._target_path, warning_text)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(str(exc))
        finally:
            self.finished.emit()


class TextureEditorCanvas(QWidget):
    stroke_committed = Signal(object)
    selection_committed = Signal(object)
    clone_source_picked = Signal(object)
    color_sampled = Signal(str)
    hover_info_changed = Signal(object)
    wheel_zoom_requested = Signal(int, int, int)
    floating_transform_requested = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._edited_rgba: Optional[np.ndarray] = None
        self._original_rgba: Optional[np.ndarray] = None
        self._edited_image: Optional[QImage] = None
        self._original_image: Optional[QImage] = None
        self._display_image: Optional[QImage] = None
        self._scroll_area: Optional[QScrollArea] = None
        self._fit_to_view = True
        self._zoom_factor = 1.0
        self._display_scale = 1.0
        self._tool = "paint"
        self._brush_size = 32.0
        self._brush_hardness = 80
        self._brush_tip = "round"
        self._brush_roundness = 100
        self._brush_angle = 0
        self._brush_pattern = "solid"
        self._symmetry_mode = "off"
        self._view_mode = "edited"
        self._split_percent = 50
        self._grid_enabled = False
        self._grid_size = 64
        self._grid_color = QColor("#74C1FF")
        self._grid_opacity = 42
        self._guides_enabled = False
        self._vertical_guides: Tuple[int, ...] = ()
        self._horizontal_guides: Tuple[int, ...] = ()
        self._selection = TextureEditorSelection()
        self._floating_bounds: Optional[Tuple[int, int, int, int]] = None
        self._floating_origin_bounds: Optional[Tuple[int, int, int, int]] = None
        self._floating_offset_x = 0
        self._floating_offset_y = 0
        self._floating_scale_x = 1.0
        self._floating_scale_y = 1.0
        self._floating_rotation_degrees = 0.0
        self._quick_mask_image: Optional[QImage] = None
        self._clone_source_point: Optional[Tuple[int, int]] = None
        self._hover_point: Optional[Tuple[int, int]] = None
        self._sample_target = ""
        self._dragging = False
        self._drag_points: List[Tuple[int, int]] = []
        self._rect_origin: Optional[Tuple[int, int]] = None
        self._lasso_points: List[Tuple[float, float]] = []
        self._pan_start = None
        self._last_stroke_point: Optional[Tuple[int, int]] = None
        self._transform_drag_mode = ""
        self._transform_drag_start_point: Optional[Tuple[float, float]] = None
        self._transform_drag_start_bounds: Optional[Tuple[int, int, int, int]] = None
        self._transform_drag_start_origin_bounds: Optional[Tuple[int, int, int, int]] = None
        self._transform_drag_start_offset = (0, 0)
        self._transform_drag_start_scale = (1.0, 1.0)
        self._transform_drag_start_rotation = 0.0
        self.setMouseTracking(True)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _brush_tools(self) -> set[str]:
        return {"paint", "erase", "sharpen", "soften", "clone", "heal", "smudge", "dodge_burn"}

    def attach_scroll_area(self, scroll_area: QScrollArea) -> None:
        self._scroll_area = scroll_area
        scroll_area.viewport().installEventFilter(self)
        self._update_display_geometry()

    def eventFilter(self, watched, event):  # type: ignore[override]
        if (
            self._scroll_area is not None
            and watched is self._scroll_area.viewport()
        ):
            if event.type() == QEvent.Type.Resize:
                self._update_display_geometry()
            elif event.type() == QEvent.Type.Wheel and self._image is not None:
                delta = int(event.angleDelta().y())
                if delta == 0:
                    delta = int(event.pixelDelta().y())
                if delta == 0:
                    return super().eventFilter(watched, event)
                viewport_pos = event.position().toPoint()
                canvas_pos = self.mapFrom(self._scroll_area.viewport(), viewport_pos)
                clamped = self._clamp_widget_point_to_image(canvas_pos)
                if clamped is not None:
                    self.wheel_zoom_requested.emit(int(delta), int(clamped.x()), int(clamped.y()))
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _display_target_rect(self) -> QRect:
        if self._image is None:
            return QRect()
        width = max(1, int(round(self._image.width() * self._display_scale)))
        height = max(1, int(round(self._image.height() * self._display_scale)))
        return QRect(0, 0, width, height)

    def set_image(self, image: Optional[QImage]) -> None:
        self._edited_rgba = None
        self._original_rgba = None
        self._edited_image = image.copy() if image is not None else None
        self._original_image = None
        self._image = image.copy() if image is not None else None
        self._display_image = self._image.copy() if self._image is not None else None
        self._update_display_geometry()
        self.update()

    def set_rgba_images(
        self,
        edited_rgba: Optional[np.ndarray],
        *,
        original_rgba: Optional[np.ndarray] = None,
    ) -> None:
        self._edited_rgba = edited_rgba.copy() if edited_rgba is not None else None
        self._original_rgba = original_rgba.copy() if original_rgba is not None else None
        self._edited_image = _rgba_array_to_qimage(self._edited_rgba) if self._edited_rgba is not None else None
        self._original_image = _rgba_array_to_qimage(self._original_rgba) if self._original_rgba is not None else None
        self._refresh_display_image()

    def set_view_mode(self, mode: str) -> None:
        normalized = (mode or "edited").strip().lower()
        if normalized == self._view_mode:
            return
        self._view_mode = normalized
        self._refresh_display_image()

    def set_compare_split_percent(self, percent: int) -> None:
        percent = max(5, min(95, int(percent)))
        if percent == self._split_percent:
            return
        self._split_percent = percent
        self.update()

    def set_grid_state(
        self,
        *,
        enabled: bool,
        grid_size: int,
        grid_color: Optional[QColor] = None,
        grid_opacity: int = 42,
    ) -> None:
        self._grid_enabled = bool(enabled)
        self._grid_size = max(2, int(grid_size))
        if grid_color is not None and grid_color.isValid():
            self._grid_color = QColor(grid_color)
        self._grid_opacity = max(5, min(100, int(grid_opacity)))
        self.update()

    def set_guide_state(
        self,
        *,
        enabled: bool,
        vertical_guides: Sequence[int],
        horizontal_guides: Sequence[int],
    ) -> None:
        self._guides_enabled = bool(enabled)
        self._vertical_guides = tuple(max(0, int(value)) for value in vertical_guides)
        self._horizontal_guides = tuple(max(0, int(value)) for value in horizontal_guides)
        self.update()

    def set_symmetry_mode(self, mode: str) -> None:
        normalized = (mode or "off").strip().lower()
        if normalized == self._symmetry_mode:
            return
        self._symmetry_mode = normalized
        self.update()

    def _build_channel_qimage(self, channel_key: str) -> Optional[QImage]:
        if self._edited_rgba is None:
            return None
        rgba = self._edited_rgba
        if channel_key == "red":
            channel = rgba[..., 0]
        elif channel_key == "green":
            channel = rgba[..., 1]
        elif channel_key == "blue":
            channel = rgba[..., 2]
        else:
            channel = rgba[..., 3]
        gray_rgba = np.stack([channel, channel, channel, np.full_like(channel, 255)], axis=-1)
        return _rgba_array_to_qimage(gray_rgba)

    def _refresh_display_image(self) -> None:
        self._image = self._edited_image.copy() if self._edited_image is not None else None
        if self._edited_image is None:
            self._display_image = None
            self._update_display_geometry()
            self.update()
            return
        mode = self._view_mode
        if mode == "original" and self._original_image is not None:
            self._display_image = self._original_image.copy()
        elif mode in {"red", "green", "blue", "alpha"}:
            self._display_image = self._build_channel_qimage(mode)
        else:
            self._display_image = self._edited_image.copy()
        self._update_display_geometry()
        self.update()

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        if tool not in self._brush_tools():
            self._last_stroke_point = None
        self.update()

    def set_brush_size(self, size: float) -> None:
        self._brush_size = max(0.25, float(size))
        self.update()

    def set_brush_visual_state(
        self,
        *,
        hardness: int,
        tip: str,
        roundness: int,
        angle_degrees: int,
        pattern: str,
    ) -> None:
        self._brush_hardness = max(0, min(100, int(hardness)))
        self._brush_tip = str(tip or "round")
        self._brush_roundness = max(10, min(100, int(roundness)))
        self._brush_angle = int(angle_degrees)
        self._brush_pattern = str(pattern or "solid")
        self.update()

    def _draw_brush_outline(self, painter: QPainter, center_x: float, center_y: float) -> None:
        diameter = max(1.0, float(self._brush_size) * self._display_scale)
        radius = diameter / 2.0
        roundness_ratio = max(0.15, min(1.0, float(self._brush_roundness) / 100.0))
        width = diameter * roundness_ratio
        height = diameter
        painter.save()
        painter.translate(center_x, center_y)
        painter.rotate(float(self._brush_angle))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 210), 1.1))
        tip_key = (self._brush_tip or "round").strip().lower()
        if tip_key == "image_stamp":
            painter.drawRect(QRect(int(round(-width / 2.0)), int(round(-height / 2.0)), max(1, int(round(width))), max(1, int(round(height)))))
            painter.drawLine(int(round(-width / 2.0)), int(round(-height / 2.0)), int(round(width / 2.0)), int(round(height / 2.0)))
            painter.drawLine(int(round(width / 2.0)), int(round(-height / 2.0)), int(round(-width / 2.0)), int(round(height / 2.0)))
        elif tip_key == "square":
            painter.drawRect(QRect(int(round(-width / 2.0)), int(round(-height / 2.0)), max(1, int(round(width))), max(1, int(round(height)))))
        elif tip_key == "diamond":
            path = QPainterPath()
            path.moveTo(0.0, -height / 2.0)
            path.lineTo(width / 2.0, 0.0)
            path.lineTo(0.0, height / 2.0)
            path.lineTo(-width / 2.0, 0.0)
            path.closeSubpath()
            painter.drawPath(path)
        else:
            painter.drawEllipse(
                QRect(
                    int(round(-width / 2.0)),
                    int(round(-height / 2.0)),
                    max(1, int(round(width))),
                    max(1, int(round(height))),
                )
            )
        painter.setPen(QPen(QColor("#74C1FF"), 0.9))
        if tip_key == "image_stamp":
            painter.drawRect(QRect(int(round(-(width + 2.0) / 2.0)), int(round(-(height + 2.0) / 2.0)), max(1, int(round(width + 2.0))), max(1, int(round(height + 2.0)))))
        elif tip_key == "square":
            painter.drawRect(QRect(int(round(-(width + 2.0) / 2.0)), int(round(-(height + 2.0) / 2.0)), max(1, int(round(width + 2.0))), max(1, int(round(height + 2.0)))))
        elif tip_key == "diamond":
            path = QPainterPath()
            path.moveTo(0.0, -(height + 2.0) / 2.0)
            path.lineTo((width + 2.0) / 2.0, 0.0)
            path.lineTo(0.0, (height + 2.0) / 2.0)
            path.lineTo(-(width + 2.0) / 2.0, 0.0)
            path.closeSubpath()
            painter.drawPath(path)
        else:
            painter.drawEllipse(
                QRect(
                    int(round(-(width + 2.0) / 2.0)),
                    int(round(-(height + 2.0) / 2.0)),
                    max(1, int(round(width + 2.0))),
                    max(1, int(round(height + 2.0))),
                )
            )
        painter.restore()

    def _draw_brush_hud(self, painter: QPainter, center_x: float, center_y: float) -> None:
        tip_label = "Stamp" if (self._brush_tip or "").strip().lower() == "image_stamp" else self._brush_tip.title()
        hud_text = f"{max(0.25, self._brush_size):.2f}px  H{self._brush_hardness}%  {tip_label}  R{self._brush_roundness}%  A{self._brush_angle}°"
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(hud_text) + 12
        text_height = metrics.height() + 8
        hud_x = int(round(center_x + 18))
        hud_y = int(round(center_y + 18))
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(18, 22, 30, 210))
        painter.drawRoundedRect(hud_x, hud_y, text_width, text_height, 6, 6)
        painter.setPen(QColor("#E7EDF7"))
        painter.drawText(hud_x + 6, hud_y + text_height - 5, hud_text)
        painter.restore()

    def _emit_hover_info(self, point: Optional[Tuple[int, int]]) -> None:
        if point is None or self._image is None:
            self.hover_info_changed.emit(None)
            return
        if point[0] < 0 or point[1] < 0 or point[0] >= self._image.width() or point[1] >= self._image.height():
            self.hover_info_changed.emit(None)
            return
        pixel = QColor(self._image.pixel(point[0], point[1]))
        self.hover_info_changed.emit(
            {
                "x": int(point[0]),
                "y": int(point[1]),
                "rgba": (
                    int(pixel.red()),
                    int(pixel.green()),
                    int(pixel.blue()),
                    int(pixel.alpha()),
                ),
            }
        )

    def _append_lasso_point(self, point: Tuple[float, float]) -> None:
        if not self._lasso_points:
            self._lasso_points = [point]
            return
        last_x, last_y = self._lasso_points[-1]
        dx = float(point[0] - last_x)
        dy = float(point[1] - last_y)
        if (dx * dx + dy * dy) < 0.16:
            return
        self._lasso_points.append(point)

    def set_selection(self, selection: TextureEditorSelection) -> None:
        self._selection = selection
        self.update()

    def set_floating_bounds(self, bounds: Optional[Tuple[int, int, int, int]]) -> None:
        self._floating_bounds = bounds
        if bounds is None:
            self._floating_origin_bounds = None
            self._floating_offset_x = 0
            self._floating_offset_y = 0
            self._floating_scale_x = 1.0
            self._floating_scale_y = 1.0
            self._floating_rotation_degrees = 0.0
            self._transform_drag_mode = ""
        self.update()

    def set_floating_transform_state(
        self,
        *,
        current_bounds: Optional[Tuple[int, int, int, int]],
        origin_bounds: Optional[Tuple[int, int, int, int]],
        offset_x: int,
        offset_y: int,
        scale_x: float,
        scale_y: float,
        rotation_degrees: float,
    ) -> None:
        self._floating_bounds = current_bounds
        self._floating_origin_bounds = origin_bounds
        self._floating_offset_x = int(offset_x)
        self._floating_offset_y = int(offset_y)
        self._floating_scale_x = float(scale_x)
        self._floating_scale_y = float(scale_y)
        self._floating_rotation_degrees = float(rotation_degrees)
        self.update()

    def set_quick_mask_overlay(self, overlay: Optional[QImage]) -> None:
        self._quick_mask_image = overlay.copy() if overlay is not None else None
        self.update()

    def set_clone_source_point(self, point: Optional[Tuple[int, int]]) -> None:
        self._clone_source_point = point
        self.update()

    def set_color_sample_target(self, target: str) -> None:
        self._sample_target = target
        self.setCursor(Qt.CrossCursor if target else Qt.ArrowCursor)

    def current_display_scale(self) -> float:
        return self._display_scale

    def is_fit_to_view(self) -> bool:
        return self._fit_to_view

    def set_fit_to_view(self, fit_to_view: bool) -> None:
        self._fit_to_view = bool(fit_to_view)
        self._update_display_geometry()

    def set_zoom_factor(self, factor: float) -> None:
        self._fit_to_view = False
        self._zoom_factor = max(0.05, min(32.0, float(factor)))
        self._update_display_geometry()

    def _update_display_geometry(self) -> None:
        if self._image is None:
            if self._scroll_area is not None:
                viewport = self._scroll_area.viewport().size()
                self.resize(max(280, viewport.width() - 12), max(220, viewport.height() - 12))
            else:
                self.resize(420, 300)
            self._display_scale = 1.0
            self.update()
            return
        width = max(1, self._image.width())
        height = max(1, self._image.height())
        if self._fit_to_view and self._scroll_area is not None:
            viewport = self._scroll_area.viewport().size()
            usable_w = max(1, viewport.width() - 12)
            usable_h = max(1, viewport.height() - 12)
            scale = min(usable_w / width, usable_h / height)
            self._display_scale = max(0.05, min(32.0, scale))
        else:
            self._display_scale = max(0.05, min(32.0, self._zoom_factor))
        target = self._display_target_rect()
        self.resize(max(1, target.width()), max(1, target.height()))
        self.update()

    def _widget_to_image_point(self, pos) -> Optional[Tuple[int, int]]:
        if self._image is None:
            return None
        scale = max(0.0001, self._display_scale)
        x = int(pos.x() / scale)
        y = int(pos.y() / scale)
        if x < 0 or y < 0 or x >= self._image.width() or y >= self._image.height():
            return None
        return (x, y)

    def _widget_to_image_point_float(self, pos) -> Optional[Tuple[float, float]]:
        if self._image is None:
            return None
        scale = max(0.0001, self._display_scale)
        x = float(pos.x()) / scale
        y = float(pos.y()) / scale
        if x < 0.0 or y < 0.0 or x >= float(self._image.width()) or y >= float(self._image.height()):
            return None
        max_x = max(0.0, float(self._image.width()) - 0.001)
        max_y = max(0.0, float(self._image.height()) - 0.001)
        return (min(max_x, x), min(max_y, y))

    def _clamp_widget_point_to_image(self, pos) -> Optional[QPoint]:
        if self._image is None:
            return None
        if self.width() <= 0 or self.height() <= 0:
            return None
        clamped_x = min(max(int(round(pos.x())), 0), max(0, self.width() - 1))
        clamped_y = min(max(int(round(pos.y())), 0), max(0, self.height() - 1))
        return QPoint(clamped_x, clamped_y)

    def _sample_color(self, point: Tuple[int, int]) -> str:
        if self._image is None:
            return "#000000"
        pixel = QColor(self._image.pixel(point[0], point[1]))
        return pixel.name().upper()

    def _floating_handle_rects(self) -> Dict[str, QRectF]:
        if self._floating_bounds is None:
            return {}
        x, y, width, height = self._floating_bounds
        scale = max(0.0001, self._display_scale)
        widget_x = float(x) * scale
        widget_y = float(y) * scale
        widget_w = max(1.0, float(width) * scale)
        widget_h = max(1.0, float(height) * scale)
        handle_size = max(10.0, min(16.0, max(10.0, scale * 0.75)))
        half = handle_size / 2.0
        left = widget_x
        right = widget_x + widget_w
        top = widget_y
        bottom = widget_y + widget_h
        center_x = widget_x + (widget_w / 2.0)
        rotate_y = top - max(18.0, handle_size * 1.6)
        return {
            "scale_nw": QRectF(left - half, top - half, handle_size, handle_size),
            "scale_ne": QRectF(right - half, top - half, handle_size, handle_size),
            "scale_sw": QRectF(left - half, bottom - half, handle_size, handle_size),
            "scale_se": QRectF(right - half, bottom - half, handle_size, handle_size),
            "rotate": QRectF(center_x - half, rotate_y - half, handle_size, handle_size),
        }

    def _floating_transform_hit(self, pos) -> Optional[str]:
        if self._floating_bounds is None:
            return None
        point = QPoint(int(round(pos.x())), int(round(pos.y())))
        for name, rect in self._floating_handle_rects().items():
            if rect.contains(pos):
                return name
        scale = max(0.0001, self._display_scale)
        x, y, width, height = self._floating_bounds
        widget_rect = QRectF(float(x) * scale, float(y) * scale, max(1.0, float(width) * scale), max(1.0, float(height) * scale))
        if widget_rect.contains(pos):
            return "move"
        return None

    def _cursor_for_floating_hit(self, hit: Optional[str]):
        if hit == "move":
            return Qt.SizeAllCursor
        if hit in {"scale_nw", "scale_se"}:
            return Qt.SizeFDiagCursor
        if hit in {"scale_ne", "scale_sw"}:
            return Qt.SizeBDiagCursor
        if hit == "rotate":
            return Qt.CrossCursor
        return Qt.ArrowCursor

    def _clamped_image_point_float(self, pos) -> Optional[Tuple[float, float]]:
        if self._image is None:
            return None
        precise = self._widget_to_image_point_float(pos)
        if precise is not None:
            return precise
        clamped = self._clamp_widget_point_to_image(pos.toPoint())
        if clamped is None:
            return None
        scale = max(0.0001, self._display_scale)
        max_x = max(0.0, float(self._image.width()) - 0.001)
        max_y = max(0.0, float(self._image.height()) - 0.001)
        return (
            min(max_x, max(0.0, float(clamped.x()) / scale)),
            min(max_y, max(0.0, float(clamped.y()) / scale)),
        )

    def _build_floating_transform_payload(self, current_point: Tuple[float, float], *, commit: bool) -> Optional[Dict[str, object]]:
        if (
            not self._transform_drag_mode
            or self._transform_drag_start_point is None
            or self._transform_drag_start_bounds is None
            or self._transform_drag_start_origin_bounds is None
        ):
            return None
        mode = self._transform_drag_mode
        start_point = self._transform_drag_start_point
        start_x, start_y, start_w, start_h = self._transform_drag_start_bounds
        origin_x, origin_y, _origin_w, _origin_h = self._transform_drag_start_origin_bounds
        payload: Dict[str, object] = {
            "mode": mode,
            "commit": bool(commit),
            "offset_x": int(self._transform_drag_start_offset[0]),
            "offset_y": int(self._transform_drag_start_offset[1]),
            "scale_x": float(self._transform_drag_start_scale[0]),
            "scale_y": float(self._transform_drag_start_scale[1]),
            "rotation_degrees": float(self._transform_drag_start_rotation),
        }
        if mode == "move":
            payload["offset_x"] = int(round(self._transform_drag_start_offset[0] + (current_point[0] - start_point[0])))
            payload["offset_y"] = int(round(self._transform_drag_start_offset[1] + (current_point[1] - start_point[1])))
            return payload
        if mode.startswith("scale_"):
            anchor_x = float(start_x)
            anchor_y = float(start_y)
            if mode == "scale_nw":
                anchor_x = float(start_x + start_w)
                anchor_y = float(start_y + start_h)
            elif mode == "scale_ne":
                anchor_x = float(start_x)
                anchor_y = float(start_y + start_h)
            elif mode == "scale_sw":
                anchor_x = float(start_x + start_w)
                anchor_y = float(start_y)
            current_width = max(1.0, abs(anchor_x - current_point[0]))
            current_height = max(1.0, abs(anchor_y - current_point[1]))
            factor = max(current_width / max(1.0, float(start_w)), current_height / max(1.0, float(start_h)))
            factor = max(0.05, min(8.0, factor))
            next_w = max(1.0, float(start_w) * factor)
            next_h = max(1.0, float(start_h) * factor)
            if mode == "scale_nw":
                next_x = anchor_x - next_w
                next_y = anchor_y - next_h
            elif mode == "scale_ne":
                next_x = anchor_x
                next_y = anchor_y - next_h
            elif mode == "scale_sw":
                next_x = anchor_x - next_w
                next_y = anchor_y
            else:
                next_x = anchor_x
                next_y = anchor_y
            payload["offset_x"] = int(round(next_x - float(origin_x)))
            payload["offset_y"] = int(round(next_y - float(origin_y)))
            payload["scale_x"] = float(self._transform_drag_start_scale[0] * factor)
            payload["scale_y"] = float(self._transform_drag_start_scale[1] * factor)
            return payload
        if mode == "rotate":
            center_x = float(start_x) + (float(start_w) / 2.0)
            center_y = float(start_y) + (float(start_h) / 2.0)
            start_angle = math.degrees(math.atan2(start_point[1] - center_y, start_point[0] - center_x))
            current_angle = math.degrees(math.atan2(current_point[1] - center_y, current_point[0] - center_x))
            payload["rotation_degrees"] = float(self._transform_drag_start_rotation + (current_angle - start_angle))
            return payload
        return None

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#222733"))
        if self._image is None or self._display_image is None:
            painter.setPen(QColor("#9CA6B8"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Open a texture to start editing.")
            return
        target_rect = self._display_target_rect()
        if target_rect.isEmpty():
            return
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if self._view_mode == "split" and self._original_image is not None and self._edited_image is not None:
            split_ratio = max(0.05, min(0.95, float(self._split_percent) / 100.0))
            split_x = int(round(target_rect.width() * split_ratio))
            source_split_x = int(round(self._edited_image.width() * split_ratio))
            if split_x > 0 and source_split_x > 0:
                painter.drawImage(
                    target_rect.adjusted(0, 0, -(target_rect.width() - split_x), 0),
                    self._original_image,
                    self._original_image.rect().adjusted(0, 0, -(self._original_image.width() - source_split_x), 0),
                )
            if split_x < target_rect.width() and source_split_x < self._edited_image.width():
                painter.drawImage(
                    target_rect.adjusted(split_x, 0, 0, 0),
                    self._edited_image,
                    self._edited_image.rect().adjusted(source_split_x, 0, 0, 0),
                )
            painter.setPen(QPen(QColor("#8ED0FF"), 2))
            painter.drawLine(split_x, 0, split_x, target_rect.height())
        else:
            painter.drawImage(target_rect, self._display_image)
        if self._quick_mask_image is not None:
            painter.drawImage(target_rect, self._quick_mask_image)
        if self._grid_enabled and self._grid_size > 1:
            grid_step = float(self._grid_size) * max(0.01, self._display_scale)
            if grid_step >= 6.0:
                base_color = QColor(self._grid_color if self._grid_color.isValid() else QColor("#74C1FF"))
                opacity_ratio = max(0.05, min(1.0, float(self._grid_opacity) / 100.0))
                minor_color = QColor(base_color)
                major_color = QColor(base_color)
                minor_color.setAlpha(max(8, int(round(255.0 * opacity_ratio * 0.38))))
                major_color.setAlpha(max(minor_color.alpha() + 12, int(round(255.0 * opacity_ratio * 0.72))))
                minor_pen = QPen(minor_color, 1)
                major_pen = QPen(major_color, 1)
                x = grid_step
                line_index = 1
                while x < target_rect.width():
                    painter.setPen(major_pen if (line_index % 4 == 0) else minor_pen)
                    painter.drawLine(int(round(x)), 0, int(round(x)), target_rect.height())
                    x += grid_step
                    line_index += 1
                y = grid_step
                line_index = 1
                while y < target_rect.height():
                    painter.setPen(major_pen if (line_index % 4 == 0) else minor_pen)
                    painter.drawLine(0, int(round(y)), target_rect.width(), int(round(y)))
                    y += grid_step
                    line_index += 1
        if self._guides_enabled:
            guide_pen = QPen(QColor(116, 193, 255, 165), 1)
            guide_pen.setStyle(Qt.DashLine)
            painter.setPen(guide_pen)
            for guide_x in self._vertical_guides:
                x = int(round(float(guide_x) * max(0.01, self._display_scale)))
                painter.drawLine(x, 0, x, target_rect.height())
            for guide_y in self._horizontal_guides:
                y = int(round(float(guide_y) * max(0.01, self._display_scale)))
                painter.drawLine(0, y, target_rect.width(), y)
        if self._symmetry_mode != "off":
            symmetry_pen = QPen(QColor(116, 193, 255, 110), 1)
            symmetry_pen.setStyle(Qt.DashLine)
            painter.setPen(symmetry_pen)
            if self._symmetry_mode in {"horizontal", "both"}:
                guide_x = int(round((self._image.width() * 0.5) * max(0.01, self._display_scale)))
                painter.drawLine(guide_x, 0, guide_x, target_rect.height())
            if self._symmetry_mode in {"vertical", "both"}:
                guide_y = int(round((self._image.height() * 0.5) * max(0.01, self._display_scale)))
                painter.drawLine(0, guide_y, target_rect.width(), guide_y)
        scale = self._display_scale
        painter.setRenderHint(QPainter.Antialiasing, True)
        selection_pen = QPen(QColor("#69B8FF"))
        selection_pen.setStyle(Qt.DashLine)
        selection_pen.setWidth(2)
        painter.setPen(selection_pen)
        if self._selection.mask_polygons:
            for polygon_points in self._selection.mask_polygons:
                if len(polygon_points) < 3:
                    continue
                path = QPainterPath()
                first = polygon_points[0]
                path.moveTo(first[0] * scale, first[1] * scale)
                for point in polygon_points[1:]:
                    path.lineTo(point[0] * scale, point[1] * scale)
                path.closeSubpath()
                painter.drawPath(path)
        elif self._selection.mode == "rect" and self._selection.rect is not None:
            x, y, w, h = self._selection.rect
            painter.drawRect(int(x * scale), int(y * scale), int(w * scale), int(h * scale))
        elif self._selection.mode == "lasso" and self._selection.polygon_points:
            path = QPainterPath()
            first = self._selection.polygon_points[0]
            path.moveTo(first[0] * scale, first[1] * scale)
            for point in self._selection.polygon_points[1:]:
                path.lineTo(point[0] * scale, point[1] * scale)
            path.closeSubpath()
            painter.drawPath(path)
        overlay_pen = QPen(QColor("#F8D25C"))
        overlay_pen.setWidth(2)
        painter.setPen(overlay_pen)
        if self._drag_points and self._tool in {"paint", "erase", "sharpen", "soften", "clone", "heal"}:
            overlay_path = QPainterPath()
            first = self._drag_points[0]
            overlay_path.moveTo(first[0] * scale, first[1] * scale)
            for point in self._drag_points[1:]:
                overlay_path.lineTo(point[0] * scale, point[1] * scale)
            brush_width = max(1.0, float(self._brush_size) * scale)
            fill_color = {
                "paint": QColor(116, 193, 255, 58),
                "erase": QColor(255, 118, 118, 52),
                "sharpen": QColor(255, 212, 92, 56),
                "soften": QColor(134, 196, 255, 52),
                "clone": QColor(104, 236, 194, 48),
                "heal": QColor(104, 236, 194, 48),
            }.get(self._tool, QColor(255, 212, 92, 52))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(fill_color, brush_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if len(self._drag_points) == 1:
                painter.drawEllipse(
                    int(round((first[0] * scale) - (brush_width / 2.0))),
                    int(round((first[1] * scale) - (brush_width / 2.0))),
                    int(round(brush_width)),
                    int(round(brush_width)),
                )
            else:
                painter.drawPath(overlay_path)
        elif self._drag_points and self._tool == "move":
            move_pen = QPen(QColor("#8BD0FF"), 2.0)
            move_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(move_pen)
            start = self._drag_points[0]
            end = self._drag_points[-1]
            painter.drawLine(
                int(round(start[0] * scale)),
                int(round(start[1] * scale)),
                int(round(end[0] * scale)),
                int(round(end[1] * scale)),
            )
            handle_radius = 5
            painter.setPen(QPen(QColor(255, 255, 255, 160), 1.2))
            painter.setBrush(QColor(139, 208, 255, 84))
            painter.drawEllipse(
                int(round(end[0] * scale)) - handle_radius,
                int(round(end[1] * scale)) - handle_radius,
                handle_radius * 2,
                handle_radius * 2,
            )
        if self._lasso_points and self._tool == "lasso":
            path = QPainterPath()
            first = self._lasso_points[0]
            path.moveTo(first[0] * scale, first[1] * scale)
            for point in self._lasso_points[1:]:
                path.lineTo(point[0] * scale, point[1] * scale)
            painter.drawPath(path)
        if self._rect_origin is not None and self._drag_points and self._tool == "select_rect":
            start = self._rect_origin
            end = self._drag_points[-1]
            x = min(start[0], end[0])
            y = min(start[1], end[1])
            w = abs(end[0] - start[0])
            h = abs(end[1] - start[1])
            painter.drawRect(int(x * scale), int(y * scale), int(w * scale), int(h * scale))
        if self._clone_source_point is not None:
            x = int(self._clone_source_point[0] * scale)
            y = int(self._clone_source_point[1] * scale)
            painter.setPen(QPen(QColor("#FF7A7A"), 2))
            painter.drawLine(x - 10, y, x + 10, y)
            painter.drawLine(x, y - 10, x, y + 10)
        if self._floating_bounds is not None:
            x, y, w, h = self._floating_bounds
            painter.setPen(QPen(QColor("#F2C14E"), 2, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(int(round(x * scale)), int(round(y * scale)), int(round(w * scale)), int(round(h * scale)))
            if self._tool == "move":
                handle_pen = QPen(QColor("#FFD97A"), 1.2)
                painter.setPen(handle_pen)
                painter.setBrush(QColor(38, 45, 58, 220))
                handle_rects = self._floating_handle_rects()
                rotate_rect = handle_rects.get("rotate")
                if rotate_rect is not None:
                    center_x = int(round((x + (w / 2.0)) * scale))
                    center_y = int(round(y * scale))
                    rotate_center = rotate_rect.center()
                    painter.drawLine(center_x, center_y, int(round(rotate_center.x())), int(round(rotate_center.y())))
                for rect in handle_rects.values():
                    painter.drawEllipse(rect)
        hover_point = self._drag_points[-1] if self._drag_points else self._hover_point
        if hover_point is not None and self._tool in self._brush_tools():
            center_x = float(hover_point[0]) * scale
            center_y = float(hover_point[1]) * scale
            self._draw_brush_outline(painter, center_x, center_y)
            self._draw_brush_hud(painter, center_x, center_y)
        if self._tool in {"clone", "heal"} and self._clone_source_point is not None and hover_point is not None:
            source_x = float(self._clone_source_point[0]) * scale
            source_y = float(self._clone_source_point[1]) * scale
            if self._drag_points:
                dx = hover_point[0] - self._drag_points[0][0]
                dy = hover_point[1] - self._drag_points[0][1]
                source_x = float(self._clone_source_point[0] + dx) * scale
                source_y = float(self._clone_source_point[1] + dy) * scale
            painter.setPen(QPen(QColor("#8ED0FF"), 1.0, Qt.DashLine))
            painter.drawLine(
                int(round(hover_point[0] * scale)),
                int(round(hover_point[1] * scale)),
                int(round(source_x)),
                int(round(source_y)),
            )
            painter.setPen(QPen(QColor("#FFDA79"), 1.2))
            painter.drawEllipse(int(round(source_x - 4)), int(round(source_y - 4)), 8, 8)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._image is None:
            return
        point = self._widget_to_image_point(event.position())
        self._hover_point = point
        self._emit_hover_info(point)
        if self._sample_target:
            if point is None:
                return
            self.color_sampled.emit(f"{self._sample_target}|{self._sample_color(point)}")
            self.set_color_sample_target("")
            return
        if event.button() in {Qt.MiddleButton, Qt.RightButton} and self._scroll_area is not None:
            if (
                event.button() == Qt.RightButton
                and self._tool in {"clone", "heal"}
                and (event.modifiers() & Qt.ControlModifier)
            ):
                self.clone_source_picked.emit(point)
                return
            self._pan_start = (event.globalPosition().toPoint(), self._scroll_area.horizontalScrollBar().value(), self._scroll_area.verticalScrollBar().value())
            self.setCursor(Qt.ClosedHandCursor)
            return
        if event.button() != Qt.LeftButton:
            return
        if self._tool == "move" and self._floating_bounds is not None:
            transform_hit = self._floating_transform_hit(event.position())
            if transform_hit is not None:
                precise_point = self._clamped_image_point_float(event.position())
                if precise_point is None:
                    return
                self._transform_drag_mode = transform_hit
                self._transform_drag_start_point = precise_point
                self._transform_drag_start_bounds = self._floating_bounds
                self._transform_drag_start_origin_bounds = self._floating_origin_bounds or self._floating_bounds
                self._transform_drag_start_offset = (int(self._floating_offset_x), int(self._floating_offset_y))
                self._transform_drag_start_scale = (float(self._floating_scale_x), float(self._floating_scale_y))
                self._transform_drag_start_rotation = float(self._floating_rotation_degrees)
                self.setCursor(self._cursor_for_floating_hit(transform_hit))
                self.update()
                return
        if point is None:
            return
        if (event.modifiers() & Qt.AltModifier) and self._tool in {"paint", "fill"}:
            self.color_sampled.emit(f"paint|{self._sample_color(point)}")
            return
        if (
            (event.modifiers() & Qt.ShiftModifier)
            and self._tool in self._brush_tools()
            and self._last_stroke_point is not None
        ):
            self.stroke_committed.emit({"tool": self._tool, "points": [self._last_stroke_point, point]})
            self._last_stroke_point = point
            self.update()
            return
        self._dragging = True
        self._drag_points = [point]
        if self._tool == "select_rect":
            self._rect_origin = point
        elif self._tool == "lasso":
            precise_point = self._widget_to_image_point_float(event.position())
            if precise_point is not None:
                self._lasso_points = [precise_point]
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._pan_start is not None and self._scroll_area is not None:
            current = event.globalPosition().toPoint()
            start_point, start_x, start_y = self._pan_start
            delta = current - start_point
            self._scroll_area.horizontalScrollBar().setValue(start_x - delta.x())
            self._scroll_area.verticalScrollBar().setValue(start_y - delta.y())
            return
        if self._transform_drag_mode:
            precise_point = self._clamped_image_point_float(event.position())
            if precise_point is None:
                return
            payload = self._build_floating_transform_payload(precise_point, commit=False)
            if payload is not None:
                self.floating_transform_requested.emit(payload)
            return
        point = self._widget_to_image_point(event.position())
        self._hover_point = point
        self._emit_hover_info(point)
        if not self._dragging:
            if self._tool == "move" and self._sample_target == "":
                self.setCursor(self._cursor_for_floating_hit(self._floating_transform_hit(event.position())))
            self.update()
            return
        if self._tool == "lasso":
            precise_point = self._widget_to_image_point_float(event.position())
            if precise_point is None:
                return
            self._append_lasso_point(precise_point)
        else:
            if point is None:
                return
            self._drag_points.append(point)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._pan_start is not None and event.button() in {Qt.MiddleButton, Qt.RightButton}:
            self._pan_start = None
            self.setCursor(Qt.ArrowCursor)
            return
        if self._transform_drag_mode and event.button() == Qt.LeftButton:
            precise_point = self._clamped_image_point_float(event.position())
            payload = None if precise_point is None else self._build_floating_transform_payload(precise_point, commit=True)
            self._transform_drag_mode = ""
            self._transform_drag_start_point = None
            self._transform_drag_start_bounds = None
            self._transform_drag_start_origin_bounds = None
            self.setCursor(Qt.ArrowCursor)
            if payload is not None:
                self.floating_transform_requested.emit(payload)
            self.update()
            return
        if not self._dragging or event.button() != Qt.LeftButton:
            return
        self._dragging = False
        if self._tool == "select_rect" and self._rect_origin is not None and self._drag_points:
            start = self._rect_origin
            end = self._drag_points[-1]
            rect = (min(start[0], end[0]), min(start[1], end[1]), abs(end[0] - start[0]), abs(end[1] - start[1]))
            self.selection_committed.emit({"mode": "rect", "rect": rect})
        elif self._tool == "lasso" and len(self._lasso_points) >= 3:
            self.selection_committed.emit({"mode": "lasso", "points": list(self._lasso_points)})
        elif self._drag_points:
            self.stroke_committed.emit({"tool": self._tool, "points": list(self._drag_points)})
            if self._tool in self._brush_tools():
                self._last_stroke_point = self._drag_points[-1]
        self._drag_points = []
        self._lasso_points = []
        self._rect_origin = None
        self.update()

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hover_point = None
        self._emit_hover_info(None)
        if self._pan_start is None and not self._transform_drag_mode and not self._sample_target:
            self.setCursor(Qt.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if self._image is None:
            event.ignore()
            return
        delta = int(event.angleDelta().y())
        if delta == 0:
            delta = int(event.pixelDelta().y())
        if delta == 0:
            event.ignore()
            return
        pos = self._clamp_widget_point_to_image(event.position().toPoint())
        if pos is None:
            event.ignore()
            return
        self.wheel_zoom_requested.emit(int(delta), int(pos.x()), int(pos.y()))
        event.accept()


class TextureEditorTab(QWidget):
    status_message_requested = Signal(str, bool)
    send_to_replace_assistant_requested = Signal(str, object)
    send_to_texture_workflow_requested = Signal(str, object)
    browse_archive_requested = Signal(str)
    open_in_compare_requested = Signal(str, object)

    def __init__(
        self,
        *,
        settings: QSettings,
        base_dir: Path,
        get_texconv_path,
        get_png_root,
        get_original_dds_root=None,
        get_archive_entries=None,
        get_current_config=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.base_dir = base_dir
        self.get_texconv_path = get_texconv_path
        self.get_png_root = get_png_root
        self.get_original_dds_root = get_original_dds_root or (lambda: "")
        self.get_archive_entries = get_archive_entries or (lambda: [])
        self.get_current_config = get_current_config or (lambda: None)
        self.workspace_root = make_texture_editor_workspace_root(base_dir)
        self.document: Optional[TextureEditorDocument] = None
        self.layer_pixels: Dict[str, np.ndarray] = {}
        self.history_snapshots: List[Dict[str, object]] = []
        self.history_index = -1
        self._layer_property_dirty = False
        self._floating_pixels: Optional[np.ndarray] = None
        self._floating_mask: Optional[np.ndarray] = None
        self._composite_cache: Optional[np.ndarray] = None
        self._composite_cache_revision = -1
        self._composite_dirty_bounds: Optional[Tuple[int, int, int, int]] = None
        self._thumbnail_cache: Dict[Tuple[str, int], QIcon] = {}
        self._pending_layer_property_before_document: Optional[TextureEditorDocument] = None
        self._pending_layer_property_before_pixels: Dict[str, np.ndarray] = {}
        self._adjustment_property_dirty = False
        self._pending_adjustment_before_document: Optional[TextureEditorDocument] = None
        self._refreshing_adjustments = False
        self._refreshing_layers_list = False
        self._editing_mask_target = False
        self._floating_transform_before_document: Optional[TextureEditorDocument] = None
        self._floating_transform_before_floating_pixels: Optional[np.ndarray] = None
        self._floating_transform_label = ""
        self.layer_clipboard: Optional[Tuple[np.ndarray, str, int, int, str]] = None
        self.selection_clipboard: Optional[Tuple[np.ndarray, str, int, int]] = None
        self.channel_clipboard: Optional[Tuple[np.ndarray, str]] = None
        self._sessions: List[_TextureEditorSession] = []
        self._active_session_index = -1
        self._switching_session = False
        self.workspace = TextureEditorWorkspace()
        self._shortcut_objects: List[QShortcut] = []
        self._task_thread: Optional[QThread] = None
        self._task_worker: Optional[TextureEditorTaskWorker] = None
        self._ui_constraint_thread: Optional[QThread] = None
        self._ui_constraint_worker: Optional[TextureEditorUIConstraintWorker] = None
        self._pending_ui_constraint_key = ""
        self._task_success_callback: Optional[Callable[[object], None]] = None
        self._busy_task_label = ""
        self._adjustment_preview_timer = QTimer(self)
        self._adjustment_preview_timer.setSingleShot(True)
        self._adjustment_preview_timer.setInterval(40)
        self._adjustment_preview_timer.timeout.connect(self.preview_selected_adjustment_properties)
        self._applying_brush_preset = False
        self._custom_brush_presets: Dict[str, Dict[str, object]] = self._load_custom_brush_presets()
        self.current_tool_settings = TextureEditorToolSettings()
        self._settings_ready = False
        self._last_open_dir = str(base_dir)
        self._last_save_dir = str(base_dir)
        self._grid_color = QColor("#74C1FF")
        self._hover_pixel_info: Optional[Dict[str, object]] = None
        self._show_rulers = True
        self._show_guides = False
        self._vertical_guides: Tuple[int, ...] = ()
        self._horizontal_guides: Tuple[int, ...] = ()
        self._tool_setting_rows: Dict[str, Tuple[Optional[QWidget], QWidget]] = {}
        self._ui_constraint_warning_cache: Dict[str, str] = {}
        self.setStyleSheet(
            """
            QGroupBox {
                border: 1px solid rgba(128, 146, 179, 0.08);
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: 600;
                background-color: rgba(255, 255, 255, 0.01);
            }
            QPushButton#EditorPanelButton {
                background-color: rgba(255, 255, 255, 0.02);
                border: 1px solid rgba(128, 146, 179, 0.12);
                color: #E7EDF7;
            }
            QPushButton#EditorPanelButton:hover {
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(128, 146, 179, 0.2);
            }
            QPushButton#EditorPrimaryButton {
                background-color: rgba(116, 193, 255, 0.14);
                border: 1px solid rgba(116, 193, 255, 0.34);
                color: #EAF5FF;
            }
            QPushButton#EditorPrimaryButton:hover {
                background-color: rgba(116, 193, 255, 0.2);
                border: 1px solid rgba(116, 193, 255, 0.5);
            }
            QLineEdit, QComboBox, QTextBrowser, QListWidget {
                border-radius: 6px;
                border: 1px solid rgba(128, 146, 179, 0.12);
                background-color: rgba(255, 255, 255, 0.02);
            }
            QToolButton#EditorToolButton {
                background-color: rgba(255, 255, 255, 0.012);
                border: 1px solid rgba(255, 255, 255, 0.04);
            }
            QToolButton#EditorToolButton:checked {
                background-color: rgba(116, 193, 255, 0.18);
                border: 1px solid rgba(116, 193, 255, 0.45);
            }
            QFrame#EditorSectionBody {
                border: 1px solid rgba(128, 146, 179, 0.08);
                border-radius: 10px;
                background-color: rgba(255, 255, 255, 0.016);
            }
            QFrame#EditorActionPane {
                border: 1px solid rgba(128, 146, 179, 0.08);
                border-radius: 10px;
                background-color: rgba(255, 255, 255, 0.012);
            }
            QWidget#EditorLeftSidebar, QWidget#EditorInspectorSidebar {
                background-color: rgba(255, 255, 255, 0.012);
                border: 1px solid rgba(128, 146, 179, 0.08);
                border-radius: 12px;
            }
            QWidget#EditorCanvasPane {
                background-color: rgba(255, 255, 255, 0.008);
                border: 1px solid rgba(128, 146, 179, 0.06);
                border-radius: 12px;
            }
            QScrollArea#EditorSidebarScroll {
                border: none;
                background: transparent;
            }
            QSplitter::handle {
                background-color: transparent;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(0)

        self.document_tab_bar = QTabBar()
        self.document_tab_bar.setDocumentMode(True)
        self.document_tab_bar.setMovable(True)
        self.document_tab_bar.setTabsClosable(True)
        self.document_tab_bar.setDrawBase(False)
        self.document_tab_bar.setExpanding(False)
        self.document_tab_bar.hide()
        self._apply_document_tab_bar_style(self.font())
        self.open_file_button = QPushButton("Open Image...")
        self.open_archive_button = QPushButton("Browse Archive")
        self.open_compare_button = QPushButton("Open In Compare")
        self.open_project_button = QPushButton("Open Project...")
        self.save_project_button = QPushButton("Save Project")
        self.save_png_button = QPushButton("Export PNG")
        self.send_replace_button = QPushButton("To Replace")
        self.send_workflow_button = QPushButton("To Workflow")
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.shortcuts_button = QPushButton("Shortcuts")
        self.save_png_button.setObjectName("EditorPrimaryButton")
        self.send_replace_button.setObjectName("EditorPrimaryButton")
        self.send_workflow_button.setObjectName("EditorPrimaryButton")
        self.open_archive_button.setObjectName("EditorPanelButton")
        self.open_compare_button.setObjectName("EditorPanelButton")
        self.open_project_button.setObjectName("EditorPanelButton")
        self.shortcuts_button.setObjectName("EditorPanelButton")
        self.open_compare_button.hide()

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("WarningText")
        self.warning_label.setWordWrap(True)
        self.warning_label.setVisible(False)

        self.status_label = QLabel("Open a PNG, DDS, or project to start editing.")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.status_label.setMinimumHeight(36)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(10)
        root_layout.addWidget(self.main_splitter, stretch=1)

        self.tool_panel = QWidget()
        self.tool_panel.setObjectName("EditorLeftSidebar")
        self.tool_panel.setMinimumWidth(200)
        self.tool_panel.setMaximumWidth(220)
        tool_layout = QVBoxLayout(self.tool_panel)
        tool_layout.setContentsMargins(12, 12, 12, 12)
        tool_layout.setSpacing(8)
        left_actions_body = QFrame()
        left_actions_body.setObjectName("EditorActionPane")
        left_actions_layout = QVBoxLayout(left_actions_body)
        left_actions_layout.setContentsMargins(8, 8, 8, 8)
        left_actions_layout.setSpacing(6)
        self.actions_menu_button = QToolButton()
        self.actions_menu_button.setText("Actions")
        self.actions_menu_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.actions_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.actions_menu_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.actions_menu_button.setObjectName("EditorPanelButton")
        self.actions_menu = QMenu(self.actions_menu_button)
        self.action_open_file = QAction("Open Image...", self)
        self.action_open_archive = QAction("Browse Archive", self)
        self.action_open_project = QAction("Open Project...", self)
        self.action_save_project = QAction("Save Project", self)
        self.action_export_png = QAction("Export PNG", self)
        self.action_send_replace = QAction("Send To Replace Assistant", self)
        self.action_send_workflow = QAction("Send To Texture Workflow", self)
        for action in (
            self.action_open_file,
            self.action_open_archive,
            self.action_open_project,
        ):
            self.actions_menu.addAction(action)
        self.actions_menu.addSeparator()
        for action in (
            self.action_save_project,
            self.action_export_png,
            self.action_send_replace,
            self.action_send_workflow,
        ):
            self.actions_menu.addAction(action)
        self.actions_menu_button.setMenu(self.actions_menu)
        left_actions_layout.addWidget(self.actions_menu_button)
        edit_actions = QGridLayout()
        edit_actions.setHorizontalSpacing(8)
        edit_actions.setVerticalSpacing(6)
        edit_label = QLabel("Edit")
        edit_label.setObjectName("HintLabel")
        self.shortcuts_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        edit_actions.addWidget(edit_label, 0, 0, 1, 2)
        edit_actions.addWidget(self.undo_button, 1, 0)
        edit_actions.addWidget(self.redo_button, 1, 1)
        edit_actions.addWidget(self.shortcuts_button, 2, 0, 1, 2)
        left_actions_layout.addLayout(edit_actions)
        left_actions_layout.addWidget(self.warning_label)
        left_actions_layout.addWidget(self.status_label)
        tool_layout.addWidget(left_actions_body)
        self.tool_buttons: Dict[str, QToolButton] = {}
        tool_group = QGroupBox("Tools")
        tool_group.setObjectName("EditorToolGroup")
        tool_group_layout = QVBoxLayout(tool_group)
        tool_group_layout.setContentsMargins(8, 12, 8, 8)
        tool_group_layout.setSpacing(4)
        for tool_key, label in (
            ("paint", "Paint"),
            ("erase", "Erase"),
            ("fill", "Fill"),
            ("gradient", "Gradient"),
            ("sharpen", "Sharpen"),
            ("soften", "Soften"),
            ("smudge", "Smudge"),
            ("dodge_burn", "Dodge/Burn"),
            ("clone", "Clone"),
            ("heal", "Heal"),
            ("patch", "Patch"),
            ("move", "Move"),
            ("select_rect", "Rect Select"),
            ("lasso", "Lasso"),
            ("recolor", "Recolor"),
        ):
            button = QToolButton()
            button.setText(label)
            button.setIcon(_create_tool_icon(tool_key))
            button.setCheckable(True)
            button.setObjectName("EditorToolButton")
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setIconSize(QSize(16, 16))
            button.setMinimumHeight(22)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setAutoRaise(False)
            button.setToolTip(label)
            self.tool_buttons[tool_key] = button
            tool_group_layout.addWidget(button)
        tool_group_layout.addStretch(1)
        tool_layout.addWidget(tool_group)
        tool_layout.addStretch(1)
        self.left_scroll = QScrollArea()
        self.left_scroll.setObjectName("EditorSidebarScroll")
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.NoFrame)
        self.left_scroll.setMinimumWidth(218)
        self.left_scroll.setMaximumWidth(246)
        self.left_scroll.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.left_scroll.setWidget(self.tool_panel)
        self.main_splitter.addWidget(self.left_scroll)

        self.canvas_panel = QWidget()
        self.canvas_panel.setObjectName("EditorCanvasPane")
        self.canvas_panel.setMinimumWidth(480)
        canvas_layout = QVBoxLayout(self.canvas_panel)
        canvas_layout.setContentsMargins(12, 12, 12, 12)
        canvas_layout.setSpacing(10)
        canvas_layout.addWidget(self.document_tab_bar)
        self.canvas_toolbar = QFrame()
        self.canvas_toolbar.setObjectName("EditorActionPane")
        zoom_row = QHBoxLayout(self.canvas_toolbar)
        zoom_row.setContentsMargins(8, 6, 8, 4)
        zoom_row.setSpacing(6)
        self.zoom_out_button = QPushButton("-")
        self.zoom_fit_button = QPushButton("Fit")
        self.zoom_100_button = QPushButton("100%")
        self.zoom_in_button = QPushButton("+")
        self.zoom_label = QLabel("Fit")
        self.zoom_label.setObjectName("HintLabel")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItem("Edited", "edited")
        self.view_mode_combo.addItem("Original", "original")
        self.view_mode_combo.addItem("Split", "split")
        self.view_mode_combo.addItem("Red", "red")
        self.view_mode_combo.addItem("Green", "green")
        self.view_mode_combo.addItem("Blue", "blue")
        self.view_mode_combo.addItem("Alpha", "alpha")
        self.view_mode_combo.setMinimumWidth(96)
        self.view_mode_label = QLabel("View")
        self.view_mode_label.setObjectName("HintLabel")
        self.compare_split_slider = QSlider(Qt.Horizontal)
        self.compare_split_slider.setRange(5, 95)
        self.compare_split_slider.setValue(50)
        self.compare_split_slider.setMinimumWidth(72)
        self.compare_split_slider.setMaximumWidth(110)
        self.compare_split_slider.setVisible(False)
        self.grid_checkbox = QCheckBox("Grid")
        self.grid_size_spin = QSpinBox()
        self.grid_size_spin.setRange(4, 1024)
        self.grid_size_spin.setSingleStep(4)
        self.grid_size_spin.setValue(64)
        self.grid_size_spin.setMinimumWidth(60)
        self.grid_size_spin.setMaximumWidth(80)
        self.grid_color_button = QToolButton()
        self.grid_color_button.setFixedSize(24, 24)
        self.grid_color_button.setToolTip("Grid color")
        self.grid_color_button.setAutoRaise(False)
        self.grid_opacity_spin = QSpinBox()
        self.grid_opacity_spin.setRange(5, 100)
        self.grid_opacity_spin.setSuffix("%")
        self.grid_opacity_spin.setValue(42)
        self.grid_opacity_spin.setToolTip("Grid opacity")
        self.grid_opacity_spin.setMinimumWidth(64)
        self.grid_opacity_spin.setMaximumWidth(72)
        self.zoom_out_button.setMinimumSize(32, 28)
        self.zoom_fit_button.setMinimumSize(42, 28)
        self.zoom_100_button.setMinimumSize(52, 28)
        self.zoom_in_button.setMinimumSize(32, 28)
        zoom_row.addWidget(self.zoom_out_button)
        zoom_row.addWidget(self.zoom_fit_button)
        zoom_row.addWidget(self.zoom_100_button)
        zoom_row.addWidget(self.zoom_in_button)
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addSpacing(8)
        zoom_row.addWidget(self.view_mode_label)
        zoom_row.addWidget(self.view_mode_combo)
        zoom_row.addWidget(self.compare_split_slider)
        zoom_row.addSpacing(6)
        zoom_row.addWidget(self.grid_checkbox)
        zoom_row.addWidget(self.grid_size_spin)
        zoom_row.addWidget(self.grid_color_button)
        zoom_row.addWidget(self.grid_opacity_spin)
        zoom_row.addStretch(1)
        canvas_layout.addWidget(self.canvas_toolbar)
        self.canvas = TextureEditorCanvas()
        self.canvas_scroll = QScrollArea()
        self.canvas_scroll.setWidgetResizable(False)
        self.canvas_scroll.setAlignment(Qt.AlignCenter)
        self.canvas_scroll.setWidget(self.canvas)
        self.canvas.attach_scroll_area(self.canvas_scroll)
        self.ruler_corner = QFrame()
        self.ruler_corner.setFixedSize(22, 22)
        self.ruler_corner.setObjectName("EditorRulerCorner")
        self.top_ruler = TextureEditorRuler(Qt.Horizontal)
        self.left_ruler = TextureEditorRuler(Qt.Vertical)
        canvas_view_grid = QGridLayout()
        canvas_view_grid.setContentsMargins(0, 0, 0, 0)
        canvas_view_grid.setHorizontalSpacing(0)
        canvas_view_grid.setVerticalSpacing(0)
        canvas_view_grid.addWidget(self.ruler_corner, 0, 0)
        canvas_view_grid.addWidget(self.top_ruler, 0, 1)
        canvas_view_grid.addWidget(self.left_ruler, 1, 0)
        canvas_view_grid.addWidget(self.canvas_scroll, 1, 1)
        canvas_view_grid.setColumnStretch(1, 1)
        canvas_view_grid.setRowStretch(1, 1)
        canvas_layout.addLayout(canvas_view_grid, stretch=1)
        self.canvas_status_strip = QFrame()
        self.canvas_status_strip.setObjectName("EditorActionPane")
        canvas_status_layout = QHBoxLayout(self.canvas_status_strip)
        canvas_status_layout.setContentsMargins(8, 4, 8, 4)
        canvas_status_layout.setSpacing(10)
        self.canvas_status_zoom_label = QLabel("100%")
        self.canvas_status_tool_label = QLabel("Paint")
        self.canvas_status_layer_label = QLabel("Layer")
        self.canvas_status_selection_label = QLabel("No selection")
        self.canvas_status_state_label = QLabel("Ready")
        self.canvas_status_document_label = QLabel("No document")
        self.canvas_status_pixel_label = QLabel("XY -, -  RGBA -")
        self.canvas_status_source_label = QLabel("")
        self.canvas_status_source_label.setObjectName("HintLabel")
        for label_widget in (
            self.canvas_status_zoom_label,
            self.canvas_status_tool_label,
            self.canvas_status_layer_label,
            self.canvas_status_selection_label,
            self.canvas_status_state_label,
            self.canvas_status_document_label,
            self.canvas_status_pixel_label,
        ):
            label_widget.setObjectName("HintLabel")
            canvas_status_layout.addWidget(label_widget)
        canvas_status_layout.addWidget(self.canvas_status_source_label, stretch=1)
        canvas_layout.addWidget(self.canvas_status_strip)
        self.main_splitter.addWidget(self.canvas_panel)

        self.right_panel = QWidget()
        self.right_panel.setObjectName("EditorInspectorSidebar")
        self.right_panel.setMinimumWidth(210)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(10)
        settings_title = QLabel("Settings")
        settings_title.setStyleSheet("font-weight: 600;")
        right_layout.addWidget(settings_title)
        self.metadata_browser = QTextBrowser()
        self.metadata_browser.setOpenExternalLinks(False)
        self.metadata_browser.setMinimumHeight(120)
        self.metadata_browser.setFrameShape(QFrame.NoFrame)
        self.metadata_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.metadata_browser.setStyleSheet("background: transparent; border: none;")
        metadata_body = QFrame()
        metadata_body.setObjectName("EditorSectionBody")
        metadata_layout = QVBoxLayout(metadata_body)
        metadata_layout.setContentsMargins(10, 10, 10, 10)
        metadata_layout.addWidget(self.metadata_browser)
        self.metadata_section = CollapsibleSection("Document", metadata_body, expanded=False)
        right_layout.addWidget(self.metadata_section)

        navigator_body = QFrame()
        navigator_body.setObjectName("EditorSectionBody")
        navigator_layout = QVBoxLayout(navigator_body)
        navigator_layout.setContentsMargins(10, 10, 10, 10)
        navigator_layout.setSpacing(8)
        self.navigator_widget = TextureEditorNavigator()
        navigator_layout.addWidget(self.navigator_widget)
        self.show_rulers_checkbox = QCheckBox("Show rulers")
        self.show_rulers_checkbox.setChecked(True)
        self.show_guides_checkbox = QCheckBox("Show guides")
        self.show_guides_checkbox.setChecked(False)
        navigator_layout.addWidget(self.show_rulers_checkbox)
        navigator_layout.addWidget(self.show_guides_checkbox)
        navigator_layout.addWidget(QLabel("Vertical guides"))
        self.vertical_guides_edit = QLineEdit()
        self.vertical_guides_edit.setPlaceholderText("e.g. 128, 256, 512")
        navigator_layout.addWidget(self.vertical_guides_edit)
        navigator_layout.addWidget(QLabel("Horizontal guides"))
        self.horizontal_guides_edit = QLineEdit()
        self.horizontal_guides_edit.setPlaceholderText("e.g. 64, 128")
        navigator_layout.addWidget(self.horizontal_guides_edit)
        guide_actions = QHBoxLayout()
        self.apply_guides_button = QPushButton("Apply Guides")
        self.clear_guides_button = QPushButton("Clear Guides")
        for button in (self.apply_guides_button, self.clear_guides_button):
            button.setObjectName("EditorPanelButton")
        guide_actions.addWidget(self.apply_guides_button)
        guide_actions.addWidget(self.clear_guides_button)
        navigator_layout.addLayout(guide_actions)
        self.navigator_section = CollapsibleSection("Navigator", navigator_body, expanded=True)
        right_layout.addWidget(self.navigator_section)

        tool_settings_body = QFrame()
        tool_settings_body.setObjectName("EditorSectionBody")
        self.tool_settings_layout = QFormLayout(tool_settings_body)
        self.tool_settings_layout.setContentsMargins(10, 10, 10, 10)
        self.tool_settings_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.tool_settings_layout.setRowWrapPolicy(QFormLayout.DontWrapRows)
        self.tool_settings_layout.setHorizontalSpacing(12)
        self.tool_settings_layout.setVerticalSpacing(8)
        self.tool_settings_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.paint_color_edit = QLineEdit("#C85A30")
        self.paint_color_button = QPushButton("Pick")
        self.paint_color_sample_button = QPushButton("Sample")
        self.paint_color_row = QWidget()
        paint_color_row_layout = QHBoxLayout(self.paint_color_row)
        paint_color_row_layout.setContentsMargins(0, 0, 0, 0)
        paint_color_row_layout.setSpacing(6)
        paint_color_row_layout.addWidget(self.paint_color_edit, stretch=1)
        paint_color_row_layout.addWidget(self.paint_color_button)
        paint_color_row_layout.addWidget(self.paint_color_sample_button)
        self.secondary_color_edit = QLineEdit("#FFFFFF")
        self.secondary_color_button = QPushButton("Pick")
        self.secondary_color_sample_button = QPushButton("Sample")
        self.secondary_color_row = QWidget()
        secondary_color_row_layout = QHBoxLayout(self.secondary_color_row)
        secondary_color_row_layout.setContentsMargins(0, 0, 0, 0)
        secondary_color_row_layout.setSpacing(6)
        secondary_color_row_layout.addWidget(self.secondary_color_edit, stretch=1)
        secondary_color_row_layout.addWidget(self.secondary_color_button)
        secondary_color_row_layout.addWidget(self.secondary_color_sample_button)
        self.brush_preset_combo = QComboBox()
        self.brush_preset_combo.addItem("Custom", "custom")
        self.brush_preset_combo.addItem("Detail", "detail")
        self.brush_preset_combo.addItem("Soft Paint", "soft_paint")
        self.brush_preset_combo.addItem("Hard Block", "hard_block")
        self.brush_preset_combo.addItem("Texture", "texture")
        self.brush_preset_combo.addItem("Speckle", "speckle")
        self.brush_preset_combo.addItem("Retouch", "retouch")
        self.save_brush_preset_button = QPushButton("Save Preset")
        self.save_brush_preset_button.setObjectName("EditorPanelButton")
        self.brush_preset_row = QWidget()
        brush_preset_row_layout = QHBoxLayout(self.brush_preset_row)
        brush_preset_row_layout.setContentsMargins(0, 0, 0, 0)
        brush_preset_row_layout.setSpacing(6)
        brush_preset_row_layout.addWidget(self.brush_preset_combo, stretch=1)
        brush_preset_row_layout.addWidget(self.save_brush_preset_button)
        self.brush_tip_combo = QComboBox()
        self.brush_tip_combo.addItem("Round", "round")
        self.brush_tip_combo.addItem("Square", "square")
        self.brush_tip_combo.addItem("Diamond", "diamond")
        self.brush_tip_combo.addItem("Flat", "flat")
        self.brush_tip_combo.addItem("Image Stamp", "image_stamp")
        self.brush_pattern_combo = QComboBox()
        self.brush_pattern_combo.addItem("Solid", "solid")
        self.brush_pattern_combo.addItem("Speckle", "speckle")
        self.brush_pattern_combo.addItem("Hatch", "hatch")
        self.brush_pattern_combo.addItem("Crosshatch", "crosshatch")
        self.brush_pattern_combo.addItem("Grain", "grain")
        self.custom_brush_tip_path_edit = QLineEdit()
        self.custom_brush_tip_path_edit.setReadOnly(True)
        self.custom_brush_tip_path_edit.setPlaceholderText("No image stamp loaded")
        self.load_custom_brush_tip_button = QPushButton("Load...")
        self.clear_custom_brush_tip_button = QPushButton("Clear")
        self.custom_brush_tip_row = QWidget()
        custom_brush_tip_row_layout = QHBoxLayout(self.custom_brush_tip_row)
        custom_brush_tip_row_layout.setContentsMargins(0, 0, 0, 0)
        custom_brush_tip_row_layout.setSpacing(6)
        custom_brush_tip_row_layout.addWidget(self.custom_brush_tip_path_edit, stretch=1)
        custom_brush_tip_row_layout.addWidget(self.load_custom_brush_tip_button)
        custom_brush_tip_row_layout.addWidget(self.clear_custom_brush_tip_button)
        self.symmetry_mode_combo = QComboBox()
        self.symmetry_mode_combo.addItem("Off", "off")
        self.symmetry_mode_combo.addItem("Horizontal mirror", "horizontal")
        self.symmetry_mode_combo.addItem("Vertical mirror", "vertical")
        self.symmetry_mode_combo.addItem("Both axes", "both")
        self.brush_size_slider = QSlider(Qt.Horizontal)
        self.brush_size_slider.setRange(1, 256)
        self.brush_size_slider.setValue(32)
        self.size_step_mode_combo = QComboBox()
        self.size_step_mode_combo.addItem("Normal", "normal")
        self.size_step_mode_combo.addItem("Fine detail", "fine")
        self.hardness_slider = QSlider(Qt.Horizontal)
        self.hardness_slider.setRange(0, 100)
        self.hardness_slider.setValue(80)
        self.roundness_slider = QSlider(Qt.Horizontal)
        self.roundness_slider.setRange(10, 100)
        self.roundness_slider.setValue(100)
        self.angle_slider = QSlider(Qt.Horizontal)
        self.angle_slider.setRange(-180, 180)
        self.angle_slider.setValue(0)
        self.smoothing_slider = QSlider(Qt.Horizontal)
        self.smoothing_slider.setRange(0, 100)
        self.smoothing_slider.setValue(0)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(1, 100)
        self.opacity_slider.setValue(100)
        self.flow_slider = QSlider(Qt.Horizontal)
        self.flow_slider.setRange(1, 100)
        self.flow_slider.setValue(100)
        self.spacing_slider = QSlider(Qt.Horizontal)
        self.spacing_slider.setRange(1, 100)
        self.spacing_slider.setValue(20)
        self.fill_tolerance_slider = QSlider(Qt.Horizontal)
        self.fill_tolerance_slider.setRange(0, 255)
        self.fill_tolerance_slider.setValue(24)
        self.fill_contiguous_checkbox = QCheckBox("Contiguous fill only")
        self.fill_contiguous_checkbox.setChecked(True)
        self.strength_slider = QSlider(Qt.Horizontal)
        self.strength_slider.setRange(0, 100)
        self.strength_slider.setValue(25)
        self.paint_blend_mode_combo = QComboBox()
        self.paint_blend_mode_combo.addItem("Normal", "normal")
        self.paint_blend_mode_combo.addItem("Multiply", "multiply")
        self.paint_blend_mode_combo.addItem("Screen", "screen")
        self.paint_blend_mode_combo.addItem("Overlay", "overlay")
        self.sharpen_mode_combo = QComboBox()
        self.sharpen_mode_combo.addItem("Unsharp Mask", "unsharp_mask")
        self.sharpen_mode_combo.addItem("Local Contrast", "local_contrast")
        self.sharpen_mode_combo.addItem("High Pass", "high_pass")
        self.soften_mode_combo = QComboBox()
        self.soften_mode_combo.addItem("Gaussian Blur", "gaussian")
        self.soften_mode_combo.addItem("Median Blur", "median")
        self.soften_mode_combo.addItem("Surface Blur", "surface")
        self.sample_visible_layers_checkbox = QCheckBox("Sample visible layers")
        self.sample_visible_layers_checkbox.setChecked(True)
        self.clone_aligned_checkbox = QCheckBox("Aligned sampling")
        self.clone_aligned_checkbox.setChecked(True)
        self.clear_clone_source_button = QPushButton("Clear Source")
        self.clear_clone_source_button.setObjectName("EditorPanelButton")
        self.lasso_snap_checkbox = QCheckBox("Snap lasso to edges")
        self.lasso_snap_checkbox.setChecked(False)
        self.lasso_snap_radius_slider = QSlider(Qt.Horizontal)
        self.lasso_snap_radius_slider.setRange(2, 24)
        self.lasso_snap_radius_slider.setValue(10)
        self.lasso_snap_sensitivity_slider = QSlider(Qt.Horizontal)
        self.lasso_snap_sensitivity_slider.setRange(1, 100)
        self.lasso_snap_sensitivity_slider.setValue(55)
        self.clone_source_label = QLabel("Ctrl+right-click sets the source point. Right-drag pans the canvas. Turn off aligned sampling to keep stamping from one fixed source.")
        self.clone_source_label.setWordWrap(True)
        self.smudge_strength_slider = QSlider(Qt.Horizontal)
        self.smudge_strength_slider.setRange(1, 100)
        self.smudge_strength_slider.setValue(45)
        self.dodge_burn_mode_combo = QComboBox()
        self.dodge_burn_mode_combo.addItem("Dodge Midtones", "dodge_midtones")
        self.dodge_burn_mode_combo.addItem("Dodge Highlights", "dodge_highlights")
        self.dodge_burn_mode_combo.addItem("Dodge Shadows", "dodge_shadows")
        self.dodge_burn_mode_combo.addItem("Burn Midtones", "burn_midtones")
        self.dodge_burn_mode_combo.addItem("Burn Highlights", "burn_highlights")
        self.dodge_burn_mode_combo.addItem("Burn Shadows", "burn_shadows")
        self.dodge_burn_exposure_slider = QSlider(Qt.Horizontal)
        self.dodge_burn_exposure_slider.setRange(1, 100)
        self.dodge_burn_exposure_slider.setValue(20)
        self.patch_blend_slider = QSlider(Qt.Horizontal)
        self.patch_blend_slider.setRange(1, 100)
        self.patch_blend_slider.setValue(70)
        self.gradient_type_combo = QComboBox()
        self.gradient_type_combo.addItem("Linear", "linear")
        self.gradient_type_combo.addItem("Radial", "radial")
        self.recolor_mode_combo = QComboBox()
        self.recolor_mode_combo.addItem("Tint whole texture", "tint")
        self.recolor_mode_combo.addItem("Replace selected color", "replace_color")
        self.recolor_source_edit = QLineEdit("#808080")
        self.recolor_source_pick_button = QPushButton("Pick")
        self.recolor_source_sample_button = QPushButton("Sample")
        self.recolor_target_edit = QLineEdit("#C85A30")
        self.recolor_target_pick_button = QPushButton("Pick")
        self.recolor_target_sample_button = QPushButton("Sample")
        self.recolor_tolerance_slider = QSlider(Qt.Horizontal)
        self.recolor_tolerance_slider.setRange(0, 255)
        self.recolor_tolerance_slider.setValue(48)
        self.recolor_strength_slider = QSlider(Qt.Horizontal)
        self.recolor_strength_slider.setRange(1, 100)
        self.recolor_strength_slider.setValue(100)
        self.recolor_preserve_luma_checkbox = QCheckBox("Preserve shading / luminance")
        self.recolor_preserve_luma_checkbox.setChecked(True)
        self.apply_recolor_button = QPushButton("Apply Recolor To Active Layer")
        recolor_source_row = QWidget()
        recolor_source_row_layout = QHBoxLayout(recolor_source_row)
        recolor_source_row_layout.setContentsMargins(0, 0, 0, 0)
        recolor_source_row_layout.setSpacing(6)
        recolor_source_row_layout.addWidget(self.recolor_source_edit, stretch=1)
        recolor_source_row_layout.addWidget(self.recolor_source_pick_button)
        recolor_source_row_layout.addWidget(self.recolor_source_sample_button)
        recolor_target_row = QWidget()
        recolor_target_row_layout = QHBoxLayout(recolor_target_row)
        recolor_target_row_layout.setContentsMargins(0, 0, 0, 0)
        recolor_target_row_layout.setSpacing(6)
        recolor_target_row_layout.addWidget(self.recolor_target_edit, stretch=1)
        recolor_target_row_layout.addWidget(self.recolor_target_pick_button)
        recolor_target_row_layout.addWidget(self.recolor_target_sample_button)
        self._add_tool_setting_row("brush_preset", "Preset", self.brush_preset_row)
        self._add_tool_setting_row("brush_tip", "Brush tip", self.brush_tip_combo)
        self._add_tool_setting_row("custom_brush_tip", "Stamp", self.custom_brush_tip_row)
        self._add_tool_setting_row("brush_pattern", "Pattern", self.brush_pattern_combo)
        self._add_tool_setting_row("symmetry_mode", "Symmetry", self.symmetry_mode_combo)
        self._add_tool_setting_row("paint_color", "Color", self.paint_color_row)
        self._add_tool_setting_row("secondary_color", "Secondary", self.secondary_color_row)
        self._add_tool_setting_row("brush_size", "Brush size", self.brush_size_slider)
        self._add_tool_setting_row("size_step_mode", "Size mode", self.size_step_mode_combo)
        self._add_tool_setting_row("hardness", "Hardness", self.hardness_slider)
        self._add_tool_setting_row("roundness", "Roundness", self.roundness_slider)
        self._add_tool_setting_row("angle_degrees", "Angle", self.angle_slider)
        self._add_tool_setting_row("smoothing", "Smoothing", self.smoothing_slider)
        self._add_tool_setting_row("opacity", "Opacity", self.opacity_slider)
        self._add_tool_setting_row("flow", "Flow", self.flow_slider)
        self._add_tool_setting_row("spacing", "Spacing", self.spacing_slider)
        self._add_tool_setting_row("paint_blend_mode", "Blend mode", self.paint_blend_mode_combo)
        self._add_tool_setting_row("fill_tolerance", "Fill tolerance", self.fill_tolerance_slider)
        self._add_tool_setting_row("fill_contiguous", "", self.fill_contiguous_checkbox)
        self._add_tool_setting_row("strength", "Strength", self.strength_slider)
        self._add_tool_setting_row("smudge_strength", "Smudge", self.smudge_strength_slider)
        self._add_tool_setting_row("dodge_burn_mode", "Tone mode", self.dodge_burn_mode_combo)
        self._add_tool_setting_row("dodge_burn_exposure", "Exposure", self.dodge_burn_exposure_slider)
        self._add_tool_setting_row("patch_blend", "Patch blend", self.patch_blend_slider)
        self._add_tool_setting_row("gradient_type", "Gradient", self.gradient_type_combo)
        self._add_tool_setting_row("sharpen_mode", "Sharpen mode", self.sharpen_mode_combo)
        self._add_tool_setting_row("soften_mode", "Soften mode", self.soften_mode_combo)
        self._add_tool_setting_row("sample_visible_layers", "", self.sample_visible_layers_checkbox)
        self._add_tool_setting_row("clone_aligned", "", self.clone_aligned_checkbox)
        self._add_tool_setting_row("clone_clear_source", "", self.clear_clone_source_button)
        self._add_tool_setting_row("lasso_snap_to_edges", "", self.lasso_snap_checkbox)
        self._add_tool_setting_row("lasso_snap_radius", "Snap radius", self.lasso_snap_radius_slider)
        self._add_tool_setting_row("lasso_snap_sensitivity", "Edge sensitivity", self.lasso_snap_sensitivity_slider)
        self._add_tool_setting_row("clone_hint", "Clone / Heal", self.clone_source_label)
        self._add_tool_setting_row("recolor_mode", "Recolor mode", self.recolor_mode_combo)
        self._add_tool_setting_row("recolor_source", "Recolor source", recolor_source_row)
        self._add_tool_setting_row("recolor_target", "Recolor target", recolor_target_row)
        self._add_tool_setting_row("recolor_tolerance", "Recolor tolerance", self.recolor_tolerance_slider)
        self._add_tool_setting_row("recolor_strength", "Recolor strength", self.recolor_strength_slider)
        self._add_tool_setting_row("recolor_preserve_luma", "", self.recolor_preserve_luma_checkbox)
        self._add_tool_setting_row("recolor_apply", "", self.apply_recolor_button)
        self.tool_settings_section = CollapsibleSection("Tool Settings", tool_settings_body, expanded=True)
        right_layout.addWidget(self.tool_settings_section)

        selection_body = QFrame()
        selection_body.setObjectName("EditorSectionBody")
        selection_layout = QVBoxLayout(selection_body)
        selection_layout.setContentsMargins(10, 10, 10, 10)
        selection_layout.setSpacing(8)
        self.selection_help_label = QLabel(
            "Selections limit paint, erase, fill, clone, heal, sharpen, soften, and recolor to the selected area. Quick Mask now lets Paint, Erase, and Fill edit the selection directly."
        )
        self.selection_help_label.setWordWrap(True)
        selection_layout.addWidget(self.selection_help_label)
        selection_form = QFormLayout()
        selection_form.setContentsMargins(0, 0, 0, 0)
        selection_form.setHorizontalSpacing(10)
        selection_form.setVerticalSpacing(8)
        self.selection_mode_combo = QComboBox()
        self.selection_mode_combo.addItem("Replace", "replace")
        self.selection_mode_combo.addItem("Add", "add")
        self.selection_mode_combo.addItem("Subtract", "subtract")
        self.selection_mode_combo.addItem("Intersect", "intersect")
        selection_form.addRow("Mode", self.selection_mode_combo)
        self.selection_feather_slider = QSlider(Qt.Horizontal)
        self.selection_feather_slider.setRange(0, 32)
        self.selection_feather_slider.setValue(0)
        selection_form.addRow("Feather", self.selection_feather_slider)
        self.selection_refine_spin = QSpinBox()
        self.selection_refine_spin.setRange(1, 64)
        self.selection_refine_spin.setValue(4)
        selection_form.addRow("Grow/Shrink", self.selection_refine_spin)
        selection_layout.addLayout(selection_form)
        self.selection_invert_checkbox = QCheckBox("Invert current selection")
        self.selection_quick_mask_checkbox = QCheckBox("Quick mask overlay")
        selection_layout.addWidget(self.selection_invert_checkbox)
        selection_layout.addWidget(self.selection_quick_mask_checkbox)
        selection_actions = QGridLayout()
        selection_actions.setHorizontalSpacing(8)
        selection_actions.setVerticalSpacing(8)
        self.selection_copy_layer_button = QPushButton("Copy To New Layer")
        self.selection_select_all_button = QPushButton("Select All")
        self.selection_clear_button = QPushButton("Clear Selection")
        self.selection_grow_button = QPushButton("Grow +4")
        self.selection_shrink_button = QPushButton("Shrink -4")
        self.selection_to_mask_button = QPushButton("Selection To Mask")
        self.selection_from_mask_button = QPushButton("Mask To Selection")
        for button in (
            self.selection_copy_layer_button,
            self.selection_select_all_button,
            self.selection_clear_button,
            self.selection_grow_button,
            self.selection_shrink_button,
            self.selection_to_mask_button,
            self.selection_from_mask_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        selection_actions.addWidget(self.selection_copy_layer_button, 0, 0, 1, 2)
        selection_actions.addWidget(self.selection_select_all_button, 1, 0)
        selection_actions.addWidget(self.selection_clear_button, 1, 1)
        selection_actions.addWidget(self.selection_grow_button, 2, 0)
        selection_actions.addWidget(self.selection_shrink_button, 2, 1)
        selection_actions.addWidget(self.selection_to_mask_button, 3, 0)
        selection_actions.addWidget(self.selection_from_mask_button, 3, 1)
        selection_layout.addLayout(selection_actions)
        self.selection_section = CollapsibleSection("Selection", selection_body, expanded=False)
        right_layout.addWidget(self.selection_section)

        channels_body = QFrame()
        channels_body.setObjectName("EditorSectionBody")
        channels_layout = QVBoxLayout(channels_body)
        channels_layout.setContentsMargins(10, 10, 10, 10)
        channels_layout.setSpacing(8)
        self.channel_help_label = QLabel("Choose which channels paint, fill, gradient, recolor, and retouch tools are allowed to modify.")
        self.channel_help_label.setWordWrap(True)
        channels_layout.addWidget(self.channel_help_label)
        channel_grid = QGridLayout()
        channel_grid.setHorizontalSpacing(8)
        channel_grid.setVerticalSpacing(6)
        self.channel_red_checkbox = QCheckBox("R")
        self.channel_green_checkbox = QCheckBox("G")
        self.channel_blue_checkbox = QCheckBox("B")
        self.channel_alpha_checkbox = QCheckBox("A")
        self.channel_red_checkbox.setChecked(True)
        self.channel_green_checkbox.setChecked(True)
        self.channel_blue_checkbox.setChecked(True)
        self.channel_alpha_checkbox.setChecked(True)
        channel_grid.addWidget(self.channel_red_checkbox, 0, 0)
        channel_grid.addWidget(self.channel_green_checkbox, 0, 1)
        channel_grid.addWidget(self.channel_blue_checkbox, 0, 2)
        channel_grid.addWidget(self.channel_alpha_checkbox, 0, 3)
        channels_layout.addLayout(channel_grid)
        channel_actions = QGridLayout()
        channel_actions.setHorizontalSpacing(8)
        channel_actions.setVerticalSpacing(8)
        self.channel_all_button = QPushButton("All")
        self.channel_rgb_button = QPushButton("RGB")
        self.channel_alpha_only_button = QPushButton("Alpha Only")
        for button in (
            self.channel_all_button,
            self.channel_rgb_button,
            self.channel_alpha_only_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        channel_actions.addWidget(self.channel_all_button, 0, 0)
        channel_actions.addWidget(self.channel_rgb_button, 0, 1)
        channel_actions.addWidget(self.channel_alpha_only_button, 0, 2)
        channels_layout.addLayout(channel_actions)
        packed_actions = QGridLayout()
        packed_actions.setHorizontalSpacing(8)
        packed_actions.setVerticalSpacing(8)
        self.channel_extract_combo = QComboBox()
        self.channel_extract_combo.addItem("Extract Red", "red")
        self.channel_extract_combo.addItem("Extract Green", "green")
        self.channel_extract_combo.addItem("Extract Blue", "blue")
        self.channel_extract_combo.addItem("Extract Alpha", "alpha")
        self.channel_extract_button = QPushButton("Extract To Layer")
        self.channel_pack_combo = QComboBox()
        self.channel_pack_combo.addItem("Pack Luma To Red", "red")
        self.channel_pack_combo.addItem("Pack Luma To Green", "green")
        self.channel_pack_combo.addItem("Pack Luma To Blue", "blue")
        self.channel_pack_combo.addItem("Pack Luma To Alpha", "alpha")
        self.channel_pack_button = QPushButton("Apply")
        self.channel_selection_combo = QComboBox()
        self.channel_selection_combo.addItem("Load Red As Selection", "red")
        self.channel_selection_combo.addItem("Load Green As Selection", "green")
        self.channel_selection_combo.addItem("Load Blue As Selection", "blue")
        self.channel_selection_combo.addItem("Load Alpha As Selection", "alpha")
        self.channel_selection_from_button = QPushButton("From Channel")
        self.channel_selection_to_combo = QComboBox()
        self.channel_selection_to_combo.addItem("Write Selection To Red", "red")
        self.channel_selection_to_combo.addItem("Write Selection To Green", "green")
        self.channel_selection_to_combo.addItem("Write Selection To Blue", "blue")
        self.channel_selection_to_combo.addItem("Write Selection To Alpha", "alpha")
        self.channel_selection_to_button = QPushButton("To Channel")
        self.channel_copy_combo = QComboBox()
        self.channel_copy_combo.addItem("Copy Red", "red")
        self.channel_copy_combo.addItem("Copy Green", "green")
        self.channel_copy_combo.addItem("Copy Blue", "blue")
        self.channel_copy_combo.addItem("Copy Alpha", "alpha")
        self.channel_copy_button = QPushButton("Copy")
        self.channel_paste_combo = QComboBox()
        self.channel_paste_combo.addItem("Paste To Red", "red")
        self.channel_paste_combo.addItem("Paste To Green", "green")
        self.channel_paste_combo.addItem("Paste To Blue", "blue")
        self.channel_paste_combo.addItem("Paste To Alpha", "alpha")
        self.channel_paste_button = QPushButton("Paste")
        self.channel_swap_a_combo = QComboBox()
        self.channel_swap_b_combo = QComboBox()
        for combo in (self.channel_swap_a_combo, self.channel_swap_b_combo):
            combo.addItem("Red", "red")
            combo.addItem("Green", "green")
            combo.addItem("Blue", "blue")
            combo.addItem("Alpha", "alpha")
        self.channel_swap_b_combo.setCurrentIndex(2)
        self.channel_swap_button = QPushButton("Swap")
        for button in (
            self.channel_extract_button,
            self.channel_pack_button,
            self.channel_selection_from_button,
            self.channel_selection_to_button,
            self.channel_copy_button,
            self.channel_paste_button,
            self.channel_swap_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        packed_actions.addWidget(self.channel_extract_combo, 0, 0)
        packed_actions.addWidget(self.channel_extract_button, 0, 1)
        packed_actions.addWidget(self.channel_pack_combo, 1, 0)
        packed_actions.addWidget(self.channel_pack_button, 1, 1)
        packed_actions.addWidget(self.channel_selection_combo, 2, 0)
        packed_actions.addWidget(self.channel_selection_from_button, 2, 1)
        packed_actions.addWidget(self.channel_selection_to_combo, 3, 0)
        packed_actions.addWidget(self.channel_selection_to_button, 3, 1)
        packed_actions.addWidget(self.channel_copy_combo, 4, 0)
        packed_actions.addWidget(self.channel_copy_button, 4, 1)
        packed_actions.addWidget(self.channel_paste_combo, 5, 0)
        packed_actions.addWidget(self.channel_paste_button, 5, 1)
        swap_row = QWidget()
        swap_row_layout = QHBoxLayout(swap_row)
        swap_row_layout.setContentsMargins(0, 0, 0, 0)
        swap_row_layout.setSpacing(6)
        swap_row_layout.addWidget(self.channel_swap_a_combo, stretch=1)
        swap_row_layout.addWidget(QLabel("↔"))
        swap_row_layout.addWidget(self.channel_swap_b_combo, stretch=1)
        packed_actions.addWidget(swap_row, 6, 0)
        packed_actions.addWidget(self.channel_swap_button, 6, 1)
        channels_layout.addLayout(packed_actions)
        self.channels_section = CollapsibleSection("Channels", channels_body, expanded=False)
        right_layout.addWidget(self.channels_section)

        transform_body = QFrame()
        transform_body.setObjectName("EditorSectionBody")
        transform_layout = QVBoxLayout(transform_body)
        transform_layout.setContentsMargins(10, 10, 10, 10)
        transform_layout.setSpacing(8)
        self.transform_help_label = QLabel("Float the active layer or a copied selection, then move, scale, rotate, flip, and commit it as an isolated layer.")
        self.transform_help_label.setWordWrap(True)
        transform_layout.addWidget(self.transform_help_label)
        transform_grid = QGridLayout()
        transform_grid.setHorizontalSpacing(8)
        transform_grid.setVerticalSpacing(8)
        self.transform_scale_spin = QSpinBox()
        self.transform_scale_spin.setRange(10, 400)
        self.transform_scale_spin.setValue(100)
        self.transform_rotation_spin = QSpinBox()
        self.transform_rotation_spin.setRange(-180, 180)
        self.transform_rotation_spin.setValue(0)
        self.transform_float_layer_button = QPushButton("Float Active Layer Copy")
        self.transform_apply_button = QPushButton("Apply")
        self.transform_flip_h_button = QPushButton("Flip H")
        self.transform_flip_v_button = QPushButton("Flip V")
        self.transform_rotate_left_button = QPushButton("Rotate -90")
        self.transform_rotate_right_button = QPushButton("Rotate +90")
        self.transform_commit_button = QPushButton("Commit")
        self.transform_cancel_button = QPushButton("Cancel")
        transform_grid.addWidget(QLabel("Scale %"), 0, 0)
        transform_grid.addWidget(self.transform_scale_spin, 0, 1)
        transform_grid.addWidget(QLabel("Rotation"), 1, 0)
        transform_grid.addWidget(self.transform_rotation_spin, 1, 1)
        transform_grid.addWidget(self.transform_float_layer_button, 2, 0, 1, 2)
        transform_grid.addWidget(self.transform_apply_button, 3, 0, 1, 2)
        transform_grid.addWidget(self.transform_flip_h_button, 4, 0)
        transform_grid.addWidget(self.transform_flip_v_button, 4, 1)
        transform_grid.addWidget(self.transform_rotate_left_button, 5, 0)
        transform_grid.addWidget(self.transform_rotate_right_button, 5, 1)
        transform_grid.addWidget(self.transform_commit_button, 6, 0)
        transform_grid.addWidget(self.transform_cancel_button, 6, 1)
        transform_layout.addLayout(transform_grid)
        self.transform_section = CollapsibleSection("Transform", transform_body, expanded=False)
        right_layout.addWidget(self.transform_section)

        image_body = QFrame()
        image_body.setObjectName("EditorSectionBody")
        image_layout = QVBoxLayout(image_body)
        image_layout.setContentsMargins(10, 10, 10, 10)
        image_layout.setSpacing(8)
        self.image_help_label = QLabel("Crop, resize, trim, flip, or rotate the current document while keeping layer positions aligned.")
        self.image_help_label.setWordWrap(True)
        image_layout.addWidget(self.image_help_label)
        image_actions = QVBoxLayout()
        image_actions.setContentsMargins(0, 0, 0, 0)
        image_actions.setSpacing(8)
        self.image_crop_selection_button = QPushButton("Crop To Selection")
        self.image_trim_button = QPushButton("Trim Transparent")
        self.image_resize_button = QPushButton("Image Size...")
        self.canvas_resize_button = QPushButton("Canvas Size...")
        self.image_flip_h_button = QPushButton("Flip H")
        self.image_flip_v_button = QPushButton("Flip V")
        self.image_rotate_left_button = QPushButton("Rotate -90")
        self.image_rotate_right_button = QPushButton("Rotate +90")
        for button in (
            self.image_crop_selection_button,
            self.image_trim_button,
            self.image_resize_button,
            self.canvas_resize_button,
            self.image_flip_h_button,
            self.image_flip_v_button,
            self.image_rotate_left_button,
            self.image_rotate_right_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        image_actions.addWidget(self.image_crop_selection_button)
        image_actions.addWidget(self.image_trim_button)
        image_grid = QGridLayout()
        image_grid.setContentsMargins(0, 0, 0, 0)
        image_grid.setHorizontalSpacing(8)
        image_grid.setVerticalSpacing(8)
        image_grid.addWidget(self.image_resize_button, 0, 0)
        image_grid.addWidget(self.canvas_resize_button, 0, 1)
        image_grid.addWidget(self.image_flip_h_button, 1, 0)
        image_grid.addWidget(self.image_flip_v_button, 1, 1)
        image_grid.addWidget(self.image_rotate_left_button, 2, 0)
        image_grid.addWidget(self.image_rotate_right_button, 2, 1)
        image_actions.addLayout(image_grid)
        image_layout.addLayout(image_actions)
        self.image_section = CollapsibleSection("Image", image_body, expanded=False)
        right_layout.addWidget(self.image_section)

        atlas_body = QFrame()
        atlas_body.setObjectName("EditorSectionBody")
        atlas_layout = QVBoxLayout(atlas_body)
        atlas_layout.setContentsMargins(10, 10, 10, 10)
        atlas_layout.setSpacing(8)
        self.atlas_help_label = QLabel("Use the current grid size for atlas slicing, or export the current selection as a padded region.")
        self.atlas_help_label.setWordWrap(True)
        self.atlas_help_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.atlas_help_label.setMinimumHeight((self.atlas_help_label.fontMetrics().lineSpacing() * 3) + 6)
        atlas_layout.addWidget(self.atlas_help_label)
        atlas_form = QFormLayout()
        atlas_form.setContentsMargins(0, 0, 0, 0)
        atlas_form.setHorizontalSpacing(10)
        atlas_form.setVerticalSpacing(8)
        self.atlas_padding_spin = QSpinBox()
        self.atlas_padding_spin.setRange(0, 256)
        self.atlas_padding_spin.setValue(0)
        atlas_form.addRow("Padding", self.atlas_padding_spin)
        atlas_layout.addLayout(atlas_form)
        self.atlas_trim_checkbox = QCheckBox("Trim transparent bounds on export")
        self.atlas_skip_empty_checkbox = QCheckBox("Skip empty atlas slices")
        self.atlas_skip_empty_checkbox.setChecked(True)
        atlas_layout.addWidget(self.atlas_trim_checkbox)
        atlas_layout.addWidget(self.atlas_skip_empty_checkbox)
        atlas_actions = QVBoxLayout()
        atlas_actions.setContentsMargins(0, 0, 0, 0)
        atlas_actions.setSpacing(8)
        self.atlas_export_selection_button = QPushButton("Export Selection...")
        self.atlas_export_grid_button = QPushButton("Export Grid Slices...")
        for button in (self.atlas_export_selection_button, self.atlas_export_grid_button):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        atlas_actions.addWidget(self.atlas_export_selection_button)
        atlas_actions.addWidget(self.atlas_export_grid_button)
        atlas_layout.addLayout(atlas_actions)
        self.atlas_section = CollapsibleSection("Atlas", atlas_body, expanded=False)
        right_layout.addWidget(self.atlas_section)

        layers_body = QFrame()
        layers_body.setObjectName("EditorSectionBody")
        layers_layout = QVBoxLayout(layers_body)
        layers_layout.setContentsMargins(10, 10, 10, 10)
        self.layers_list = QListWidget()
        self.layers_list.setMinimumHeight(140)
        self.layers_list.setFrameShape(QFrame.NoFrame)
        self.layers_list.setIconSize(QSize(28, 28))
        self.layers_list.setSelectionMode(QListWidget.SingleSelection)
        self.layers_list.setDragDropMode(QListWidget.InternalMove)
        self.layers_list.setDefaultDropAction(Qt.MoveAction)
        self.layers_list.setDragEnabled(True)
        self.layers_list.setAcceptDrops(True)
        self.layers_list.setDropIndicatorShown(True)
        layers_layout.addWidget(self.layers_list)
        layer_actions = QGridLayout()
        layer_actions.setHorizontalSpacing(8)
        layer_actions.setVerticalSpacing(8)
        self.add_layer_button = QPushButton("Add")
        self.duplicate_layer_button = QPushButton("Duplicate")
        self.remove_layer_button = QPushButton("Remove")
        self.merge_layer_button = QPushButton("Merge Down")
        self.layer_up_button = QPushButton("Up")
        self.layer_down_button = QPushButton("Down")
        for button in (
            self.add_layer_button,
            self.duplicate_layer_button,
            self.remove_layer_button,
            self.merge_layer_button,
            self.layer_up_button,
            self.layer_down_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        layer_actions.addWidget(self.add_layer_button, 0, 0)
        layer_actions.addWidget(self.duplicate_layer_button, 0, 1)
        layer_actions.addWidget(self.remove_layer_button, 1, 0)
        layer_actions.addWidget(self.merge_layer_button, 1, 1)
        layer_actions.addWidget(self.layer_up_button, 2, 0)
        layer_actions.addWidget(self.layer_down_button, 2, 1)
        layers_layout.addLayout(layer_actions)
        self.layer_name_edit = QLineEdit()
        self.layer_visible_checkbox = QCheckBox("Visible")
        self.layer_visible_checkbox.setChecked(True)
        self.layer_locked_checkbox = QCheckBox("Lock layer")
        self.layer_alpha_locked_checkbox = QCheckBox("Lock alpha")
        self.layer_mask_enabled_checkbox = QCheckBox("Enable mask")
        self.layer_edit_mask_checkbox = QCheckBox("Edit mask")
        self.layer_blend_mode_combo = QComboBox()
        self.layer_blend_mode_combo.addItem("Normal", "normal")
        self.layer_blend_mode_combo.addItem("Multiply", "multiply")
        self.layer_blend_mode_combo.addItem("Screen", "screen")
        self.layer_blend_mode_combo.addItem("Overlay", "overlay")
        self.layer_opacity_slider = QSlider(Qt.Horizontal)
        self.layer_opacity_slider.setRange(0, 100)
        self.layer_opacity_slider.setValue(100)
        layers_layout.addWidget(QLabel("Layer name"))
        layers_layout.addWidget(self.layer_name_edit)
        layers_layout.addWidget(QLabel("Blend mode"))
        layers_layout.addWidget(self.layer_blend_mode_combo)
        layers_layout.addWidget(self.layer_visible_checkbox)
        layers_layout.addWidget(self.layer_locked_checkbox)
        layers_layout.addWidget(self.layer_alpha_locked_checkbox)
        layers_layout.addWidget(self.layer_mask_enabled_checkbox)
        layers_layout.addWidget(self.layer_edit_mask_checkbox)
        mask_actions = QGridLayout()
        mask_actions.setHorizontalSpacing(8)
        mask_actions.setVerticalSpacing(8)
        self.layer_add_mask_button = QPushButton("Add Mask")
        self.layer_invert_mask_button = QPushButton("Invert Mask")
        self.layer_delete_mask_button = QPushButton("Delete Mask")
        mask_actions.addWidget(self.layer_add_mask_button, 0, 0)
        mask_actions.addWidget(self.layer_invert_mask_button, 0, 1)
        mask_actions.addWidget(self.layer_delete_mask_button, 1, 0, 1, 2)
        layers_layout.addLayout(mask_actions)
        layers_layout.addWidget(QLabel("Layer opacity"))
        layers_layout.addWidget(self.layer_opacity_slider)
        self.layers_section = CollapsibleSection("Layers", layers_body, expanded=False)
        right_layout.addWidget(self.layers_section)

        adjustments_body = QFrame()
        adjustments_body.setObjectName("EditorSectionBody")
        adjustments_layout = QVBoxLayout(adjustments_body)
        adjustments_layout.setContentsMargins(10, 10, 10, 10)
        adjustments_layout.setSpacing(8)
        self.adjustments_list = QListWidget()
        self.adjustments_list.setMinimumHeight(100)
        self.adjustments_list.setFrameShape(QFrame.NoFrame)
        adjustments_layout.addWidget(self.adjustments_list)
        adjustments_actions = QGridLayout()
        adjustments_actions.setHorizontalSpacing(8)
        adjustments_actions.setVerticalSpacing(8)
        self.adjustment_add_combo = QComboBox()
        self.adjustment_add_combo.addItem("Hue / Saturation", "hue_saturation")
        self.adjustment_add_combo.addItem("Brightness / Contrast", "brightness_contrast")
        self.adjustment_add_combo.addItem("Exposure", "exposure")
        self.adjustment_add_combo.addItem("Vibrance", "vibrance")
        self.adjustment_add_combo.addItem("Color Balance", "color_balance")
        self.adjustment_add_combo.addItem("Selective Color", "selective_color")
        self.adjustment_add_combo.addItem("Levels", "levels")
        self.adjustment_add_combo.addItem("Curves", "curves")
        self.adjustment_add_button = QPushButton("Add")
        self.adjustment_duplicate_button = QPushButton("Duplicate")
        self.adjustment_remove_button = QPushButton("Remove")
        self.adjustment_reset_button = QPushButton("Reset")
        self.adjustment_up_button = QPushButton("Up")
        self.adjustment_down_button = QPushButton("Down")
        self.adjustment_solo_button = QPushButton("Solo")
        for button in (
            self.adjustment_add_button,
            self.adjustment_duplicate_button,
            self.adjustment_remove_button,
            self.adjustment_reset_button,
            self.adjustment_up_button,
            self.adjustment_down_button,
            self.adjustment_solo_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        adjustments_actions.addWidget(self.adjustment_add_combo, 0, 0)
        adjustments_actions.addWidget(self.adjustment_add_button, 0, 1)
        adjustments_actions.addWidget(self.adjustment_duplicate_button, 1, 0)
        adjustments_actions.addWidget(self.adjustment_solo_button, 1, 1)
        adjustments_actions.addWidget(self.adjustment_remove_button, 2, 0)
        adjustments_actions.addWidget(self.adjustment_reset_button, 2, 1)
        adjustments_actions.addWidget(self.adjustment_up_button, 3, 0)
        adjustments_actions.addWidget(self.adjustment_down_button, 3, 1)
        adjustments_layout.addLayout(adjustments_actions)
        self.adjustment_enabled_checkbox = QCheckBox("Enabled")
        adjustments_layout.addWidget(self.adjustment_enabled_checkbox)
        self.adjustment_opacity_slider = QSlider(Qt.Horizontal)
        self.adjustment_opacity_slider.setRange(0, 100)
        self.adjustment_opacity_slider.setValue(100)
        adjustments_layout.addWidget(QLabel("Adjustment opacity"))
        adjustments_layout.addWidget(self.adjustment_opacity_slider)
        self.adjustment_mode_label = QLabel("Target")
        self.adjustment_mode_combo = QComboBox()
        self.adjustment_mode_combo.addItem("Reds", "reds")
        self.adjustment_mode_combo.addItem("Greens", "greens")
        self.adjustment_mode_combo.addItem("Blues", "blues")
        self.adjustment_mode_combo.addItem("Cyans", "cyans")
        self.adjustment_mode_combo.addItem("Magentas", "magentas")
        self.adjustment_mode_combo.addItem("Yellows", "yellows")
        self.adjustment_mode_combo.addItem("Neutrals", "neutrals")
        self.adjustment_mode_combo.addItem("Whites", "whites")
        self.adjustment_mode_combo.addItem("Blacks", "blacks")
        adjustments_layout.addWidget(self.adjustment_mode_label)
        adjustments_layout.addWidget(self.adjustment_mode_combo)
        self.adjustment_param_a_label = QLabel("Param A")
        self.adjustment_param_a_slider = QSlider(Qt.Horizontal)
        self.adjustment_param_a_slider.setRange(-100, 100)
        self.adjustment_param_b_label = QLabel("Param B")
        self.adjustment_param_b_slider = QSlider(Qt.Horizontal)
        self.adjustment_param_b_slider.setRange(-100, 100)
        self.adjustment_param_c_label = QLabel("Param C")
        self.adjustment_param_c_slider = QSlider(Qt.Horizontal)
        self.adjustment_param_c_slider.setRange(-100, 100)
        adjustments_layout.addWidget(self.adjustment_param_a_label)
        adjustments_layout.addWidget(self.adjustment_param_a_slider)
        adjustments_layout.addWidget(self.adjustment_param_b_label)
        adjustments_layout.addWidget(self.adjustment_param_b_slider)
        adjustments_layout.addWidget(self.adjustment_param_c_label)
        adjustments_layout.addWidget(self.adjustment_param_c_slider)
        adjustment_mask_actions = QGridLayout()
        adjustment_mask_actions.setHorizontalSpacing(8)
        adjustment_mask_actions.setVerticalSpacing(8)
        self.adjustment_use_active_mask_button = QPushButton("Mask Active Layer")
        self.adjustment_clear_mask_button = QPushButton("Clear Mask")
        for button in (
            self.adjustment_use_active_mask_button,
            self.adjustment_clear_mask_button,
        ):
            button.setObjectName("EditorPanelButton")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(0)
        adjustment_mask_actions.addWidget(self.adjustment_use_active_mask_button, 0, 0)
        adjustment_mask_actions.addWidget(self.adjustment_clear_mask_button, 0, 1)
        adjustments_layout.addLayout(adjustment_mask_actions)
        self.adjustments_section = CollapsibleSection("Adjustments", adjustments_body, expanded=False)
        right_layout.addWidget(self.adjustments_section)

        history_body = QFrame()
        history_body.setObjectName("EditorSectionBody")
        history_layout = QVBoxLayout(history_body)
        history_layout.setContentsMargins(10, 10, 10, 10)
        self.history_list = QListWidget()
        self.history_list.setMinimumHeight(120)
        self.history_list.setFrameShape(QFrame.NoFrame)
        history_layout.addWidget(self.history_list)
        history_actions = QHBoxLayout()
        self.history_restore_button = QPushButton("Restore Selected")
        self.history_clear_button = QPushButton("Clear History")
        history_actions.addWidget(self.history_restore_button)
        history_actions.addWidget(self.history_clear_button)
        history_actions.addStretch(1)
        history_layout.addLayout(history_actions)
        self.history_section = CollapsibleSection("History", history_body, expanded=False)
        right_layout.addWidget(self.history_section)

        right_layout.addStretch(1)
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.NoFrame)
        self.right_scroll.setMinimumWidth(210)
        self.right_scroll.setMaximumWidth(290)
        self.right_scroll.setWidget(self.right_panel)
        self.main_splitter.addWidget(self.right_scroll)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 8)
        self.main_splitter.setStretchFactor(2, 2)
        self.main_splitter.setSizes(build_responsive_splitter_sizes(2040, [11, 74, 15], [218, 480, 210]))

        self._connect_signals()
        self._rebuild_brush_preset_combo(preserve_key="custom")
        self._set_active_tool("paint")
        self._load_settings()
        self._settings_ready = True
        self._rebuild_shortcuts()
        self._refresh_ui()
        self._apply_font_sensitive_metrics(self.font())
        QTimer.singleShot(0, self._apply_responsive_splitter_defaults)

    def _apply_responsive_splitter_defaults(self) -> None:
        total_width = max(self.width() - 32, sum([218, 480, 210]))
        if self.document is None:
            left_width = 226
            self.main_splitter.setSizes([left_width, max(520, total_width - left_width), 0])
            return
        self.main_splitter.setSizes(
            build_responsive_splitter_sizes(total_width, [11, 74, 15], [218, 480, 210])
        )

    def _apply_document_tab_bar_style(self, font: QFont) -> None:
        metrics = QFontMetrics(font)
        pad_y = max(3, metrics.height() // 5)
        pad_x = max(8, metrics.averageCharWidth())
        min_width = max(88, metrics.horizontalAdvance("Document") + 22)
        max_width = max(min_width + 40, metrics.horizontalAdvance("VeryLongDocumentName.png") + 20)
        self.document_tab_bar.setStyleSheet(
            f"""
            QTabBar::tab {{
                background-color: rgba(255, 255, 255, 0.018);
                border: 1px solid rgba(128, 146, 179, 0.10);
                border-bottom: none;
                padding: {pad_y}px {pad_x}px;
                min-width: {min_width}px;
                max-width: {max_width}px;
                margin-right: 2px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                color: #C9D6EA;
            }}
            QTabBar::tab:selected {{
                background-color: rgba(255, 255, 255, 0.05);
                border-color: rgba(116, 193, 255, 0.22);
                color: #F2F6FF;
            }}
            """
        )

    def _apply_font_sensitive_metrics(self, font: QFont) -> None:
        metrics = QFontMetrics(font)
        standard_button_height = max(24, metrics.height() + 8)
        compact_button_height = max(22, metrics.height() + 6)
        tool_button_height = max(22, metrics.height() + 6)
        tool_icon_size = max(14, min(18, metrics.height() + 2))

        self._apply_document_tab_bar_style(font)
        self.actions_menu_button.setMinimumHeight(standard_button_height)
        for button in (
            self.open_file_button,
            self.open_archive_button,
            self.open_compare_button,
            self.open_project_button,
            self.save_project_button,
            self.save_png_button,
            self.send_replace_button,
            self.send_workflow_button,
            self.shortcuts_button,
        ):
            button.setMinimumHeight(standard_button_height)
        for button in (
            self.undo_button,
            self.redo_button,
            self.history_restore_button,
            self.history_clear_button,
        ):
            button.setMinimumHeight(compact_button_height)
        self._refresh_tool_button_icons()
        for button in self.tool_buttons.values():
            button.setMinimumHeight(tool_button_height)
            button.setIconSize(QSize(tool_icon_size, tool_icon_size))

    def _refresh_tool_button_icons(self) -> None:
        palette = QApplication.palette()
        for tool_key, button in self.tool_buttons.items():
            button.setIcon(_create_tool_icon(tool_key, palette))

    def sync_ui_font(self, font: QFont) -> None:
        applied_font = QFont(font)
        self.setFont(applied_font)
        for widget in (
            self.document_tab_bar,
            self.left_scroll,
            self.tool_panel,
            self.canvas_panel,
            self.right_panel,
            self.right_scroll,
            self.actions_menu_button,
        ):
            widget.setFont(applied_font)
        self.actions_menu.setFont(applied_font)
        self.metadata_browser.document().setDefaultFont(applied_font)
        self._apply_font_sensitive_metrics(applied_font)
        self._refresh_metadata()

    def sync_ui_font_from_application(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        self.sync_ui_font(app.font())

    def _apply_empty_state_layout(self, has_doc: bool) -> None:
        self.canvas_toolbar.setVisible(has_doc)
        self.canvas_status_strip.setVisible(has_doc)
        right_sidebar_was_visible = self.right_scroll.isVisible()
        right_handle = self.main_splitter.handle(2)
        self.right_panel.setVisible(has_doc)
        self.right_scroll.setVisible(has_doc)
        if has_doc:
            self.right_scroll.setMinimumWidth(210)
            self.right_scroll.setMaximumWidth(290)
            right_handle.setVisible(True)
            right_handle.setEnabled(True)
        else:
            self.right_scroll.setMinimumWidth(0)
            self.right_scroll.setMaximumWidth(0)
            right_handle.setVisible(False)
            right_handle.setEnabled(False)
            total_width = max(self.width() - 32, 760)
            left_width = 226
            self.main_splitter.setSizes([left_width, max(520, total_width - left_width), 0])
        if has_doc and not right_sidebar_was_visible:
            QTimer.singleShot(0, self._apply_responsive_splitter_defaults)

    def _connect_signals(self) -> None:
        self.document_tab_bar.currentChanged.connect(self._handle_document_tab_changed)
        self.document_tab_bar.tabCloseRequested.connect(self._close_document_tab)
        self.open_file_button.clicked.connect(self.open_file_dialog)
        self.open_archive_button.clicked.connect(self.request_browse_archive)
        self.open_compare_button.clicked.connect(self.request_open_compare)
        self.open_project_button.clicked.connect(self.open_project_dialog)
        self.save_project_button.clicked.connect(self.save_project_dialog)
        self.save_png_button.clicked.connect(self.save_flattened_png_dialog)
        self.send_replace_button.clicked.connect(self.send_to_replace_assistant)
        self.send_workflow_button.clicked.connect(self.send_to_texture_workflow)
        self.action_open_file.triggered.connect(self.open_file_dialog)
        self.action_open_archive.triggered.connect(self.request_browse_archive)
        self.action_open_project.triggered.connect(self.open_project_dialog)
        self.action_save_project.triggered.connect(self.save_project_dialog)
        self.action_export_png.triggered.connect(self.save_flattened_png_dialog)
        self.action_send_replace.triggered.connect(self.send_to_replace_assistant)
        self.action_send_workflow.triggered.connect(self.send_to_texture_workflow)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.shortcuts_button.clicked.connect(self.open_shortcuts_dialog)
        self.zoom_out_button.clicked.connect(lambda: self._adjust_zoom(-1))
        self.zoom_fit_button.clicked.connect(lambda: self._set_fit_mode(True))
        self.zoom_100_button.clicked.connect(lambda: self._set_zoom(1.0))
        self.zoom_in_button.clicked.connect(lambda: self._adjust_zoom(1))
        self.view_mode_combo.currentIndexChanged.connect(self._handle_view_mode_changed)
        self.compare_split_slider.valueChanged.connect(self._handle_compare_split_changed)
        self.grid_checkbox.toggled.connect(self._handle_grid_state_changed)
        self.grid_size_spin.valueChanged.connect(self._handle_grid_state_changed)
        self.grid_color_button.clicked.connect(self._pick_grid_color)
        self.grid_opacity_spin.valueChanged.connect(self._handle_grid_state_changed)
        for tool_key, button in self.tool_buttons.items():
            button.clicked.connect(lambda checked=False, key=tool_key: self._set_active_tool(key))
        self.canvas.stroke_committed.connect(self._handle_canvas_stroke)
        self.canvas.selection_committed.connect(self._handle_canvas_selection)
        self.canvas.clone_source_picked.connect(self._handle_clone_source_picked)
        self.canvas.color_sampled.connect(self._handle_canvas_color_sampled)
        self.canvas.hover_info_changed.connect(self._handle_canvas_hover_changed)
        self.canvas.wheel_zoom_requested.connect(self._handle_canvas_wheel_zoom)
        self.canvas.floating_transform_requested.connect(self._handle_canvas_floating_transform)
        self.canvas_scroll.horizontalScrollBar().valueChanged.connect(self._handle_canvas_viewport_changed)
        self.canvas_scroll.verticalScrollBar().valueChanged.connect(self._handle_canvas_viewport_changed)
        self.navigator_widget.center_requested.connect(self._handle_navigator_center_requested)
        self.show_rulers_checkbox.toggled.connect(self._handle_navigation_overlay_changed)
        self.show_guides_checkbox.toggled.connect(self._handle_navigation_overlay_changed)
        self.apply_guides_button.clicked.connect(self._handle_navigation_overlay_changed)
        self.clear_guides_button.clicked.connect(self.clear_guides)
        self.vertical_guides_edit.textChanged.connect(lambda *_args: self._refresh_ui())
        self.horizontal_guides_edit.textChanged.connect(lambda *_args: self._refresh_ui())
        self.vertical_guides_edit.editingFinished.connect(self._handle_navigation_overlay_changed)
        self.horizontal_guides_edit.editingFinished.connect(self._handle_navigation_overlay_changed)
        self.paint_color_button.clicked.connect(lambda: self._pick_color_into(self.paint_color_edit))
        self.paint_color_sample_button.clicked.connect(lambda: self.canvas.set_color_sample_target("paint"))
        self.secondary_color_button.clicked.connect(lambda: self._pick_color_into(self.secondary_color_edit))
        self.secondary_color_sample_button.clicked.connect(lambda: self.canvas.set_color_sample_target("secondary"))
        self.recolor_source_pick_button.clicked.connect(lambda: self._pick_color_into(self.recolor_source_edit))
        self.recolor_target_pick_button.clicked.connect(lambda: self._pick_color_into(self.recolor_target_edit))
        self.recolor_source_sample_button.clicked.connect(lambda: self.canvas.set_color_sample_target("recolor_source"))
        self.recolor_target_sample_button.clicked.connect(lambda: self.canvas.set_color_sample_target("recolor_target"))
        self.apply_recolor_button.clicked.connect(self.apply_recolor_to_active_layer)
        self.save_brush_preset_button.clicked.connect(self.save_current_brush_preset)
        self.layers_list.currentItemChanged.connect(lambda *_args: self._handle_layer_selection_changed())
        self.layers_list.model().rowsMoved.connect(self._handle_layers_reordered_by_drag)
        self.add_layer_button.clicked.connect(self.add_layer)
        self.duplicate_layer_button.clicked.connect(self.duplicate_layer)
        self.remove_layer_button.clicked.connect(self.remove_layer)
        self.merge_layer_button.clicked.connect(self.merge_layer_down)
        self.layer_up_button.clicked.connect(lambda: self.reorder_layer(-1))
        self.layer_down_button.clicked.connect(lambda: self.reorder_layer(1))
        self.layer_name_edit.editingFinished.connect(self.rename_selected_layer)
        self.layer_visible_checkbox.toggled.connect(self.toggle_selected_layer_visibility)
        self.layer_opacity_slider.valueChanged.connect(self.preview_selected_layer_properties)
        self.layer_opacity_slider.sliderReleased.connect(self.commit_selected_layer_opacity)
        self.layer_add_mask_button.clicked.connect(self.add_mask_to_selected_layer)
        self.layer_invert_mask_button.clicked.connect(self.invert_selected_layer_mask)
        self.layer_delete_mask_button.clicked.connect(self.delete_selected_layer_mask)
        self.layer_mask_enabled_checkbox.toggled.connect(self.toggle_selected_layer_mask_enabled)
        self.layer_edit_mask_checkbox.toggled.connect(self.toggle_edit_mask_target)
        self.history_list.currentRowChanged.connect(self._handle_history_row_changed)
        self.history_list.itemDoubleClicked.connect(lambda *_args: self.restore_selected_history())
        self.history_restore_button.clicked.connect(self.restore_selected_history)
        self.history_clear_button.clicked.connect(self.clear_history)
        self.selection_copy_layer_button.clicked.connect(self.copy_selection_to_new_layer)
        self.selection_clear_button.clicked.connect(self.clear_selection)
        self.selection_select_all_button.clicked.connect(self.select_all_image)
        self.selection_grow_button.clicked.connect(lambda: self.adjust_selection_size(self.selection_refine_spin.value()))
        self.selection_shrink_button.clicked.connect(lambda: self.adjust_selection_size(-self.selection_refine_spin.value()))
        self.selection_to_mask_button.clicked.connect(self.apply_selection_to_selected_layer_mask)
        self.selection_from_mask_button.clicked.connect(self.load_selected_layer_mask_as_selection)
        self.selection_refine_spin.valueChanged.connect(self._refresh_selection_button_labels)
        self.selection_invert_checkbox.toggled.connect(self.toggle_selection_invert)
        self.selection_quick_mask_checkbox.toggled.connect(self.toggle_quick_mask)
        self.load_custom_brush_tip_button.clicked.connect(self.load_custom_brush_tip)
        self.clear_custom_brush_tip_button.clicked.connect(self.clear_custom_brush_tip)
        self.selection_feather_slider.valueChanged.connect(self.preview_selection_settings)
        self.selection_feather_slider.sliderReleased.connect(self.commit_selection_settings)
        self.channel_red_checkbox.toggled.connect(self._handle_channel_lock_changed)
        self.channel_green_checkbox.toggled.connect(self._handle_channel_lock_changed)
        self.channel_blue_checkbox.toggled.connect(self._handle_channel_lock_changed)
        self.channel_alpha_checkbox.toggled.connect(self._handle_channel_lock_changed)
        self.channel_all_button.clicked.connect(lambda: self._set_channel_lock_state(True, True, True, True))
        self.channel_rgb_button.clicked.connect(lambda: self._set_channel_lock_state(True, True, True, False))
        self.channel_alpha_only_button.clicked.connect(lambda: self._set_channel_lock_state(False, False, False, True))
        self.channel_extract_button.clicked.connect(self.extract_active_channel_to_new_layer)
        self.channel_pack_button.clicked.connect(self.write_active_layer_luma_to_selected_channel)
        self.channel_selection_from_button.clicked.connect(self.load_selected_channel_as_selection)
        self.channel_selection_to_button.clicked.connect(self.write_selection_to_selected_channel)
        self.channel_copy_button.clicked.connect(self.copy_selected_channel)
        self.channel_paste_button.clicked.connect(self.paste_channel_clipboard)
        self.channel_swap_button.clicked.connect(self.swap_selected_channels)
        self.transform_float_layer_button.clicked.connect(self.float_active_layer_copy)
        self.transform_apply_button.clicked.connect(self.apply_floating_transform)
        self.transform_flip_h_button.clicked.connect(lambda: self.flip_floating_selection(True, False))
        self.transform_flip_v_button.clicked.connect(lambda: self.flip_floating_selection(False, True))
        self.transform_rotate_left_button.clicked.connect(lambda: self.rotate_floating_selection(-90))
        self.transform_rotate_right_button.clicked.connect(lambda: self.rotate_floating_selection(90))
        self.transform_commit_button.clicked.connect(self.commit_floating_selection)
        self.transform_cancel_button.clicked.connect(self.cancel_floating_selection)
        self.image_crop_selection_button.clicked.connect(self.crop_document_to_selection)
        self.image_trim_button.clicked.connect(self.trim_document_transparent)
        self.image_resize_button.clicked.connect(self.resize_document_image)
        self.canvas_resize_button.clicked.connect(self.resize_document_canvas)
        self.image_flip_h_button.clicked.connect(lambda: self.flip_document(True, False))
        self.image_flip_v_button.clicked.connect(lambda: self.flip_document(False, True))
        self.image_rotate_left_button.clicked.connect(lambda: self.rotate_document_90(False))
        self.image_rotate_right_button.clicked.connect(lambda: self.rotate_document_90(True))
        self.layer_blend_mode_combo.currentIndexChanged.connect(self.preview_selected_layer_properties)
        self.layer_locked_checkbox.toggled.connect(self.commit_selected_layer_flags)
        self.layer_alpha_locked_checkbox.toggled.connect(self.commit_selected_layer_flags)
        self.adjustment_add_button.clicked.connect(self.add_adjustment_layer)
        self.adjustment_duplicate_button.clicked.connect(self.duplicate_selected_adjustment)
        self.adjustment_remove_button.clicked.connect(self.remove_selected_adjustment)
        self.adjustment_reset_button.clicked.connect(self.reset_selected_adjustment)
        self.adjustment_up_button.clicked.connect(lambda: self.move_selected_adjustment(-1))
        self.adjustment_down_button.clicked.connect(lambda: self.move_selected_adjustment(1))
        self.adjustment_solo_button.clicked.connect(self.solo_selected_adjustment)
        self.adjustment_use_active_mask_button.clicked.connect(self.use_active_layer_as_adjustment_mask)
        self.adjustment_clear_mask_button.clicked.connect(self.clear_selected_adjustment_mask)
        self.adjustments_list.currentItemChanged.connect(lambda *_args: self._handle_adjustment_selection_changed())
        self.adjustment_enabled_checkbox.toggled.connect(self.commit_selected_adjustment_enabled)
        self.adjustment_mode_combo.currentIndexChanged.connect(self._schedule_adjustment_preview)
        self.adjustment_opacity_slider.valueChanged.connect(self._schedule_adjustment_preview)
        self.adjustment_opacity_slider.sliderReleased.connect(self.commit_selected_adjustment_properties)
        self.adjustment_param_a_slider.valueChanged.connect(self._schedule_adjustment_preview)
        self.adjustment_param_a_slider.sliderReleased.connect(self.commit_selected_adjustment_properties)
        self.adjustment_param_b_slider.valueChanged.connect(self._schedule_adjustment_preview)
        self.adjustment_param_b_slider.sliderReleased.connect(self.commit_selected_adjustment_properties)
        self.adjustment_param_c_slider.valueChanged.connect(self._schedule_adjustment_preview)
        self.adjustment_param_c_slider.sliderReleased.connect(self.commit_selected_adjustment_properties)
        self.atlas_export_selection_button.clicked.connect(self.export_selection_region)
        self.atlas_export_grid_button.clicked.connect(self.export_grid_slices)
        for widget in (
            self.paint_color_edit,
            self.secondary_color_edit,
            self.brush_preset_combo,
            self.brush_tip_combo,
            self.brush_pattern_combo,
            self.symmetry_mode_combo,
            self.brush_size_slider,
            self.size_step_mode_combo,
            self.hardness_slider,
            self.roundness_slider,
            self.angle_slider,
            self.smoothing_slider,
            self.opacity_slider,
            self.flow_slider,
            self.spacing_slider,
            self.fill_tolerance_slider,
            self.fill_contiguous_checkbox,
            self.paint_blend_mode_combo,
            self.strength_slider,
            self.smudge_strength_slider,
            self.dodge_burn_mode_combo,
            self.dodge_burn_exposure_slider,
            self.patch_blend_slider,
            self.gradient_type_combo,
            self.sharpen_mode_combo,
            self.soften_mode_combo,
            self.sample_visible_layers_checkbox,
            self.clone_aligned_checkbox,
            self.selection_mode_combo,
            self.lasso_snap_checkbox,
            self.lasso_snap_radius_slider,
            self.lasso_snap_sensitivity_slider,
            self.recolor_mode_combo,
            self.recolor_source_edit,
            self.recolor_target_edit,
            self.recolor_tolerance_slider,
            self.recolor_strength_slider,
            self.recolor_preserve_luma_checkbox,
        ):
            if hasattr(widget, "textChanged"):
                widget.textChanged.connect(self._handle_tool_settings_changed)  # type: ignore[attr-defined]
            elif hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._handle_tool_settings_changed)  # type: ignore[attr-defined]
            elif hasattr(widget, "currentIndexChanged"):
                widget.currentIndexChanged.connect(self._handle_tool_settings_changed)  # type: ignore[attr-defined]
            elif hasattr(widget, "toggled"):
                widget.toggled.connect(self._handle_tool_settings_changed)  # type: ignore[attr-defined]
        self.clear_clone_source_button.clicked.connect(self.clear_clone_source_point)

    def _add_tool_setting_row(self, key: str, label_text: str, field_widget: QWidget) -> None:
        label_widget: Optional[QLabel]
        if label_text:
            label_widget = QLabel(label_text)
            self.tool_settings_layout.addRow(label_widget, field_widget)
        else:
            label_widget = None
            self.tool_settings_layout.addRow("", field_widget)
        self._tool_setting_rows[key] = (label_widget, field_widget)

    def _brush_preset_definitions(self) -> Dict[str, Dict[str, object]]:
        return {
            "detail": {"size": 4, "hardness": 90, "opacity": 100, "flow": 100, "spacing": 8, "tip": "round", "pattern": "solid", "roundness": 100, "angle": 0, "smoothing": 0, "size_step_mode": "fine"},
            "soft_paint": {"size": 28, "hardness": 35, "opacity": 70, "flow": 55, "spacing": 16, "tip": "round", "pattern": "solid", "roundness": 100, "angle": 0, "smoothing": 28, "size_step_mode": "normal"},
            "hard_block": {"size": 20, "hardness": 100, "opacity": 100, "flow": 100, "spacing": 12, "tip": "square", "pattern": "solid", "roundness": 100, "angle": 0, "smoothing": 0, "size_step_mode": "normal"},
            "texture": {"size": 18, "hardness": 75, "opacity": 78, "flow": 72, "spacing": 28, "tip": "round", "pattern": "grain", "roundness": 84, "angle": 0, "smoothing": 8, "size_step_mode": "normal"},
            "speckle": {"size": 14, "hardness": 58, "opacity": 82, "flow": 62, "spacing": 34, "tip": "round", "pattern": "speckle", "roundness": 100, "angle": 0, "smoothing": 0, "size_step_mode": "fine"},
            "retouch": {"size": 12, "hardness": 65, "opacity": 82, "flow": 48, "spacing": 14, "tip": "flat", "pattern": "solid", "roundness": 42, "angle": -32, "smoothing": 14, "size_step_mode": "fine"},
        }

    def _load_custom_brush_presets(self) -> Dict[str, Dict[str, object]]:
        raw = str(self.settings.value("texture_editor/custom_brush_presets", "{}") or "{}").strip() or "{}"
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        output: Dict[str, Dict[str, object]] = {}
        if not isinstance(parsed, dict):
            return output
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            preset_key = key.strip().lower()
            if not preset_key or preset_key in {"custom"}:
                continue
            output[preset_key] = value
        return output

    def _store_custom_brush_presets(self) -> None:
        self.settings.setValue("texture_editor/custom_brush_presets", json.dumps(self._custom_brush_presets, indent=2, sort_keys=True))

    def _all_brush_preset_definitions(self) -> Dict[str, Dict[str, object]]:
        presets = dict(self._brush_preset_definitions())
        presets.update(self._custom_brush_presets)
        return presets

    def _rebuild_brush_preset_combo(self, *, preserve_key: Optional[str] = None) -> None:
        selected_key = preserve_key or str(self.brush_preset_combo.currentData() or "custom")
        self.brush_preset_combo.blockSignals(True)
        self.brush_preset_combo.clear()
        self.brush_preset_combo.addItem("Custom", "custom")
        built_in = self._brush_preset_definitions()
        for key in ("detail", "soft_paint", "hard_block", "texture", "speckle", "retouch"):
            label = key.replace("_", " ").title()
            self.brush_preset_combo.addItem(label, key)
        for key in sorted(self._custom_brush_presets.keys()):
            label = key.replace("_", " ").title()
            self.brush_preset_combo.addItem(f"{label} *", key)
        index = self.brush_preset_combo.findData(selected_key)
        self.brush_preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.brush_preset_combo.blockSignals(False)

    def _apply_brush_preset(self, preset_key: str) -> None:
        values = self._all_brush_preset_definitions().get((preset_key or "").strip().lower())
        if not values:
            return
        self._applying_brush_preset = True
        try:
            self.brush_size_slider.setValue(int(values["size"]))
            self.hardness_slider.setValue(int(values["hardness"]))
            self.opacity_slider.setValue(int(values["opacity"]))
            self.flow_slider.setValue(int(values["flow"]))
            self.spacing_slider.setValue(int(values["spacing"]))
            self.roundness_slider.setValue(int(values.get("roundness", 100)))
            self.angle_slider.setValue(int(values.get("angle", 0)))
            self.smoothing_slider.setValue(int(values.get("smoothing", 0)))
            tip_index = self.brush_tip_combo.findData(str(values["tip"]))
            if tip_index >= 0:
                self.brush_tip_combo.setCurrentIndex(tip_index)
            pattern_index = self.brush_pattern_combo.findData(str(values["pattern"]))
            if pattern_index >= 0:
                self.brush_pattern_combo.setCurrentIndex(pattern_index)
            self.custom_brush_tip_path_edit.setText(str(values.get("custom_tip_path", "") or ""))
            size_mode_index = self.size_step_mode_combo.findData(str(values.get("size_step_mode", "normal")))
            if size_mode_index >= 0:
                self.size_step_mode_combo.setCurrentIndex(size_mode_index)
        finally:
            self._applying_brush_preset = False

    def save_current_brush_preset(self) -> None:
        name, accepted = QInputDialog.getText(self, APP_TITLE, "Brush preset name")
        if not accepted:
            return
        preset_name = "_".join(part for part in name.strip().lower().split() if part)
        if not preset_name:
            self._set_status("Enter a preset name first.", True)
            return
        self._custom_brush_presets[preset_name] = {
            "size": int(self.brush_size_slider.value()),
            "hardness": int(self.hardness_slider.value()),
            "opacity": int(self.opacity_slider.value()),
            "flow": int(self.flow_slider.value()),
            "spacing": int(self.spacing_slider.value()),
            "tip": str(self.brush_tip_combo.currentData() or "round"),
            "pattern": str(self.brush_pattern_combo.currentData() or "solid"),
            "custom_tip_path": self.custom_brush_tip_path_edit.text().strip(),
            "roundness": int(self.roundness_slider.value()),
            "angle": int(self.angle_slider.value()),
            "smoothing": int(self.smoothing_slider.value()),
            "size_step_mode": str(self.size_step_mode_combo.currentData() or "normal"),
        }
        self._store_custom_brush_presets()
        self._rebuild_brush_preset_combo(preserve_key=preset_name)
        self._set_status(f"Saved brush preset '{preset_name}'.", False)

    def load_custom_brush_tip(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Load brush image stamp",
            self._last_open_dir,
            "Image files (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
        )
        if not path_text:
            return
        resolved = str(Path(path_text).expanduser().resolve())
        self.custom_brush_tip_path_edit.setText(resolved)
        image_stamp_index = self.brush_tip_combo.findData("image_stamp")
        if image_stamp_index >= 0:
            self.brush_tip_combo.setCurrentIndex(image_stamp_index)
        self._mark_brush_preset_custom()
        self._handle_tool_settings_changed()
        self._set_status("Loaded custom brush image stamp.", False)

    def clear_custom_brush_tip(self) -> None:
        if not self.custom_brush_tip_path_edit.text().strip():
            return
        self.custom_brush_tip_path_edit.clear()
        if self.brush_tip_combo.currentData() == "image_stamp":
            round_index = self.brush_tip_combo.findData("round")
            if round_index >= 0:
                self.brush_tip_combo.setCurrentIndex(round_index)
        self._mark_brush_preset_custom()
        self._handle_tool_settings_changed()
        self._set_status("Cleared custom brush image stamp.", False)

    def _mark_brush_preset_custom(self) -> None:
        if self.brush_preset_combo.currentData() == "custom":
            return
        self.brush_preset_combo.blockSignals(True)
        custom_index = self.brush_preset_combo.findData("custom")
        if custom_index >= 0:
            self.brush_preset_combo.setCurrentIndex(custom_index)
        self.brush_preset_combo.blockSignals(False)

    def _load_settings(self) -> None:
        self.paint_color_edit.setText(str(self.settings.value("texture_editor/paint_color", "#C85A30")))
        self.secondary_color_edit.setText(str(self.settings.value("texture_editor/secondary_color", "#FFFFFF")))
        brush_preset = str(self.settings.value("texture_editor/brush_preset", "custom"))
        self._rebuild_brush_preset_combo(preserve_key=brush_preset)
        brush_preset_index = self.brush_preset_combo.findData(brush_preset)
        if brush_preset_index >= 0:
            self.brush_preset_combo.setCurrentIndex(brush_preset_index)
        brush_tip = str(self.settings.value("texture_editor/brush_tip", "round"))
        brush_tip_index = self.brush_tip_combo.findData(brush_tip)
        if brush_tip_index >= 0:
            self.brush_tip_combo.setCurrentIndex(brush_tip_index)
        brush_pattern = str(self.settings.value("texture_editor/brush_pattern", "solid"))
        brush_pattern_index = self.brush_pattern_combo.findData(brush_pattern)
        if brush_pattern_index >= 0:
            self.brush_pattern_combo.setCurrentIndex(brush_pattern_index)
        self.custom_brush_tip_path_edit.setText(str(self.settings.value("texture_editor/custom_brush_tip_path", "")))
        symmetry_mode = str(self.settings.value("texture_editor/symmetry_mode", "off"))
        symmetry_mode_index = self.symmetry_mode_combo.findData(symmetry_mode)
        if symmetry_mode_index >= 0:
            self.symmetry_mode_combo.setCurrentIndex(symmetry_mode_index)
        self.brush_size_slider.setValue(int(self.settings.value("texture_editor/brush_size", 32)))
        size_step_mode = str(self.settings.value("texture_editor/size_step_mode", "normal"))
        size_step_index = self.size_step_mode_combo.findData(size_step_mode)
        if size_step_index >= 0:
            self.size_step_mode_combo.setCurrentIndex(size_step_index)
        self.hardness_slider.setValue(int(self.settings.value("texture_editor/hardness", 80)))
        self.roundness_slider.setValue(int(self.settings.value("texture_editor/roundness", 100)))
        self.angle_slider.setValue(int(self.settings.value("texture_editor/angle_degrees", 0)))
        self.smoothing_slider.setValue(int(self.settings.value("texture_editor/smoothing", 0)))
        self.opacity_slider.setValue(int(self.settings.value("texture_editor/opacity", 100)))
        self.flow_slider.setValue(int(self.settings.value("texture_editor/flow", 100)))
        self.spacing_slider.setValue(int(self.settings.value("texture_editor/spacing", 20)))
        self.strength_slider.setValue(int(self.settings.value("texture_editor/strength", 25)))
        self.smudge_strength_slider.setValue(int(self.settings.value("texture_editor/smudge_strength", 45)))
        dodge_burn_mode = str(self.settings.value("texture_editor/dodge_burn_mode", "dodge_midtones"))
        dodge_burn_mode_index = self.dodge_burn_mode_combo.findData(dodge_burn_mode)
        if dodge_burn_mode_index >= 0:
            self.dodge_burn_mode_combo.setCurrentIndex(dodge_burn_mode_index)
        self.dodge_burn_exposure_slider.setValue(int(self.settings.value("texture_editor/dodge_burn_exposure", 20)))
        self.patch_blend_slider.setValue(int(self.settings.value("texture_editor/patch_blend", 70)))
        gradient_type = str(self.settings.value("texture_editor/gradient_type", "linear"))
        gradient_index = self.gradient_type_combo.findData(gradient_type)
        if gradient_index >= 0:
            self.gradient_type_combo.setCurrentIndex(gradient_index)
        paint_blend_mode = str(self.settings.value("texture_editor/paint_blend_mode", "normal"))
        paint_blend_index = self.paint_blend_mode_combo.findData(paint_blend_mode)
        if paint_blend_index >= 0:
            self.paint_blend_mode_combo.setCurrentIndex(paint_blend_index)
        selection_mode = str(self.settings.value("texture_editor/selection_combine_mode", "replace"))
        selection_mode_index = self.selection_mode_combo.findData(selection_mode)
        if selection_mode_index >= 0:
            self.selection_mode_combo.setCurrentIndex(selection_mode_index)
        sharpen_mode = str(self.settings.value("texture_editor/sharpen_mode", "unsharp_mask"))
        soften_mode = str(self.settings.value("texture_editor/soften_mode", "gaussian"))
        sharpen_index = self.sharpen_mode_combo.findData(sharpen_mode)
        if sharpen_index >= 0:
            self.sharpen_mode_combo.setCurrentIndex(sharpen_index)
        soften_index = self.soften_mode_combo.findData(soften_mode)
        if soften_index >= 0:
            self.soften_mode_combo.setCurrentIndex(soften_index)
        self.sample_visible_layers_checkbox.setChecked(bool(self.settings.value("texture_editor/sample_visible_layers", True)))
        self.clone_aligned_checkbox.setChecked(bool(self.settings.value("texture_editor/clone_aligned", True)))
        self.fill_tolerance_slider.setValue(int(self.settings.value("texture_editor/fill_tolerance", 24)))
        self.fill_contiguous_checkbox.setChecked(bool(self.settings.value("texture_editor/fill_contiguous", True)))
        self.lasso_snap_checkbox.setChecked(bool(self.settings.value("texture_editor/lasso_snap_to_edges", False)))
        self.lasso_snap_radius_slider.setValue(int(self.settings.value("texture_editor/lasso_snap_radius", 10)))
        self.lasso_snap_sensitivity_slider.setValue(int(self.settings.value("texture_editor/lasso_edge_sensitivity", 55)))
        self.selection_refine_spin.setValue(int(self.settings.value("texture_editor/selection_refine_amount", 4)))
        self.recolor_source_edit.setText(str(self.settings.value("texture_editor/recolor_source", "#808080")))
        self.recolor_target_edit.setText(str(self.settings.value("texture_editor/recolor_target", "#C85A30")))
        self.recolor_tolerance_slider.setValue(int(self.settings.value("texture_editor/recolor_tolerance", 48)))
        self.recolor_strength_slider.setValue(int(self.settings.value("texture_editor/recolor_strength", 100)))
        self.recolor_preserve_luma_checkbox.setChecked(bool(self.settings.value("texture_editor/recolor_preserve_luma", True)))
        view_mode = str(self.settings.value("texture_editor/view_mode", "edited"))
        view_mode_index = self.view_mode_combo.findData(view_mode)
        if view_mode_index >= 0:
            self.view_mode_combo.setCurrentIndex(view_mode_index)
        self.compare_split_slider.setValue(int(self.settings.value("texture_editor/compare_split", 50)))
        self.grid_checkbox.setChecked(bool(self.settings.value("texture_editor/grid_enabled", False)))
        self.grid_size_spin.setValue(int(self.settings.value("texture_editor/grid_size", 64)))
        self._set_grid_color(
            QColor(str(self.settings.value("texture_editor/grid_color", "#74C1FF"))),
            save=False,
            apply=False,
        )
        self.grid_opacity_spin.setValue(int(self.settings.value("texture_editor/grid_opacity", 42)))
        self._last_open_dir = str(self.settings.value("texture_editor/last_open_dir", str(self.base_dir)))
        self._last_save_dir = str(self.settings.value("texture_editor/last_save_dir", str(self.base_dir)))
        mode = str(self.settings.value("texture_editor/recolor_mode", "tint"))
        index = self.recolor_mode_combo.findData(mode)
        if index >= 0:
            self.recolor_mode_combo.setCurrentIndex(index)
        self._handle_tool_settings_changed()

    def _save_settings(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("texture_editor/paint_color", self.paint_color_edit.text())
        self.settings.setValue("texture_editor/secondary_color", self.secondary_color_edit.text())
        self.settings.setValue("texture_editor/brush_preset", self.brush_preset_combo.currentData())
        self.settings.setValue("texture_editor/brush_tip", self.brush_tip_combo.currentData())
        self.settings.setValue("texture_editor/brush_pattern", self.brush_pattern_combo.currentData())
        self.settings.setValue("texture_editor/custom_brush_tip_path", self.custom_brush_tip_path_edit.text())
        self.settings.setValue("texture_editor/symmetry_mode", self.symmetry_mode_combo.currentData())
        self.settings.setValue("texture_editor/brush_size", self.brush_size_slider.value())
        self.settings.setValue("texture_editor/size_step_mode", self.size_step_mode_combo.currentData())
        self.settings.setValue("texture_editor/hardness", self.hardness_slider.value())
        self.settings.setValue("texture_editor/roundness", self.roundness_slider.value())
        self.settings.setValue("texture_editor/angle_degrees", self.angle_slider.value())
        self.settings.setValue("texture_editor/smoothing", self.smoothing_slider.value())
        self.settings.setValue("texture_editor/opacity", self.opacity_slider.value())
        self.settings.setValue("texture_editor/flow", self.flow_slider.value())
        self.settings.setValue("texture_editor/spacing", self.spacing_slider.value())
        self.settings.setValue("texture_editor/strength", self.strength_slider.value())
        self.settings.setValue("texture_editor/smudge_strength", self.smudge_strength_slider.value())
        self.settings.setValue("texture_editor/dodge_burn_mode", self.dodge_burn_mode_combo.currentData())
        self.settings.setValue("texture_editor/dodge_burn_exposure", self.dodge_burn_exposure_slider.value())
        self.settings.setValue("texture_editor/patch_blend", self.patch_blend_slider.value())
        self.settings.setValue("texture_editor/gradient_type", self.gradient_type_combo.currentData())
        self.settings.setValue("texture_editor/paint_blend_mode", self.paint_blend_mode_combo.currentData())
        self.settings.setValue("texture_editor/selection_combine_mode", self.selection_mode_combo.currentData())
        self.settings.setValue("texture_editor/sharpen_mode", self.sharpen_mode_combo.currentData())
        self.settings.setValue("texture_editor/soften_mode", self.soften_mode_combo.currentData())
        self.settings.setValue("texture_editor/sample_visible_layers", self.sample_visible_layers_checkbox.isChecked())
        self.settings.setValue("texture_editor/clone_aligned", self.clone_aligned_checkbox.isChecked())
        self.settings.setValue("texture_editor/fill_tolerance", self.fill_tolerance_slider.value())
        self.settings.setValue("texture_editor/fill_contiguous", self.fill_contiguous_checkbox.isChecked())
        self.settings.setValue("texture_editor/lasso_snap_to_edges", self.lasso_snap_checkbox.isChecked())
        self.settings.setValue("texture_editor/lasso_snap_radius", self.lasso_snap_radius_slider.value())
        self.settings.setValue("texture_editor/lasso_edge_sensitivity", self.lasso_snap_sensitivity_slider.value())
        self.settings.setValue("texture_editor/selection_refine_amount", self.selection_refine_spin.value())
        self.settings.setValue("texture_editor/recolor_mode", self.recolor_mode_combo.currentData())
        self.settings.setValue("texture_editor/recolor_source", self.recolor_source_edit.text())
        self.settings.setValue("texture_editor/recolor_target", self.recolor_target_edit.text())
        self.settings.setValue("texture_editor/recolor_tolerance", self.recolor_tolerance_slider.value())
        self.settings.setValue("texture_editor/recolor_strength", self.recolor_strength_slider.value())
        self.settings.setValue("texture_editor/recolor_preserve_luma", self.recolor_preserve_luma_checkbox.isChecked())
        self.settings.setValue("texture_editor/view_mode", self.view_mode_combo.currentData())
        self.settings.setValue("texture_editor/compare_split", self.compare_split_slider.value())
        self.settings.setValue("texture_editor/grid_enabled", self.grid_checkbox.isChecked())
        self.settings.setValue("texture_editor/grid_size", self.grid_size_spin.value())
        self.settings.setValue("texture_editor/grid_color", self._grid_color.name(QColor.HexRgb))
        self.settings.setValue("texture_editor/grid_opacity", self.grid_opacity_spin.value())
        self.settings.setValue("texture_editor/last_open_dir", self._last_open_dir)
        self.settings.setValue("texture_editor/last_save_dir", self._last_save_dir)
        

    def flush_settings_save(self) -> None:
        self._store_active_session()
        self._save_settings()

    def shutdown(self) -> None:
        if self._task_worker is not None:
            self._task_worker.stop()
        if self._task_thread is not None:
            self._task_thread.quit()
            self._task_thread.wait(5000)
        if self._ui_constraint_worker is not None:
            self._ui_constraint_worker.stop()
        if self._ui_constraint_thread is not None:
            self._ui_constraint_thread.quit()
            self._ui_constraint_thread.wait(2000)
        self.flush_settings_save()

    def _default_shortcuts(self) -> Dict[str, str]:
        return {
            "open_file": "Ctrl+O",
            "open_archive": "Ctrl+Shift+O",
            "open_compare": "Ctrl+Shift+C",
            "open_project": "Ctrl+Alt+O",
            "save_project": "Ctrl+S",
            "save_png": "Ctrl+Shift+S",
            "send_replace": "Ctrl+Alt+R",
            "send_workflow": "Ctrl+Alt+W",
            "undo": "Ctrl+Z",
            "redo": "Ctrl+Y",
            "clear_selection": "Ctrl+D",
            "clear_selection_alt": "Escape",
            "copy_selection_layer": "Ctrl+J",
            "new_layer": "Ctrl+Shift+N",
            "copy_layer": "Ctrl+C",
            "cut_selection": "Ctrl+X",
            "paste_layer": "Ctrl+V",
            "paste_centered": "Ctrl+Shift+V",
            "transform_float_layer": "Ctrl+T",
            "fit_view": "F",
            "actual_size": "1",
            "tool_paint": "B",
            "tool_erase": "E",
            "tool_fill": "G",
            "tool_gradient": "Shift+G",
            "tool_smudge": "S",
            "tool_dodge_burn": "O",
            "tool_move": "M",
            "tool_rect": "R",
            "tool_lasso": "L",
            "tool_clone": "C",
            "tool_heal": "H",
            "tool_patch": "P",
            "brush_smaller": "[",
            "brush_larger": "]",
            "hardness_softer": "Shift+[",
            "hardness_harder": "Shift+]",
            "toggle_quick_mask": "Q",
        }

    def _shortcut_labels(self) -> Dict[str, str]:
        return {
            "open_file": "Open file",
            "open_archive": "Show in Archive Browser",
            "open_compare": "Open current source in Compare",
            "open_project": "Open project",
            "save_project": "Save project",
            "save_png": "Save flattened PNG",
            "send_replace": "Send to Replace Assistant",
            "send_workflow": "Send to Texture Workflow",
            "undo": "Undo",
            "redo": "Redo",
            "clear_selection": "Clear selection",
            "clear_selection_alt": "Clear selection",
            "copy_selection_layer": "Copy selection to new layer",
            "new_layer": "New layer",
            "copy_layer": "Copy layer or selection",
            "cut_selection": "Cut selection",
            "paste_layer": "Paste layer or selection",
            "paste_centered": "Paste centered",
            "transform_float_layer": "Float active layer copy",
            "fit_view": "Fit view",
            "actual_size": "Actual size (100%)",
            "tool_paint": "Paint tool",
            "tool_erase": "Erase tool",
            "tool_fill": "Fill tool",
            "tool_gradient": "Gradient tool",
            "tool_smudge": "Smudge tool",
            "tool_dodge_burn": "Dodge/Burn tool",
            "tool_move": "Move tool",
            "tool_rect": "Rect select tool",
            "tool_lasso": "Lasso tool",
            "tool_clone": "Clone tool",
            "tool_heal": "Heal tool",
            "tool_patch": "Patch tool",
            "brush_smaller": "Brush smaller",
            "brush_larger": "Brush larger",
            "hardness_softer": "Hardness softer",
            "hardness_harder": "Hardness harder",
            "toggle_quick_mask": "Toggle quick mask overlay",
        }

    def _load_shortcuts_map(self) -> Dict[str, str]:
        shortcuts = self._default_shortcuts()
        for key, default in shortcuts.items():
            shortcuts[key] = str(self.settings.value(f"texture_editor/shortcuts/{key}", default))
        return shortcuts

    def _register_shortcut(self, sequence_text: str, callback) -> None:
        text = (sequence_text or "").strip()
        if not text:
            return
        shortcut = QShortcut(QKeySequence(text), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self._shortcut_objects.append(shortcut)

    def _rebuild_shortcuts(self) -> None:
        for shortcut in self._shortcut_objects:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcut_objects = []
        shortcut_map = self._load_shortcuts_map()
        bindings = {
            "open_file": self.open_file_dialog,
            "open_archive": self.request_browse_archive,
            "open_compare": self.request_open_compare,
            "open_project": self.open_project_dialog,
            "save_project": self.save_project_dialog,
            "save_png": self.save_flattened_png_dialog,
            "send_replace": self.send_to_replace_assistant,
            "send_workflow": self.send_to_texture_workflow,
            "undo": self.undo,
            "redo": self.redo,
            "clear_selection": self.clear_selection,
            "clear_selection_alt": self.clear_selection,
            "copy_selection_layer": self.copy_selection_to_new_layer,
            "new_layer": self.add_layer,
            "copy_layer": self.copy_content,
            "cut_selection": self.cut_selection_to_floating,
            "paste_layer": self.paste_content,
            "paste_centered": self.paste_content_centered,
            "transform_float_layer": self.float_active_layer_copy,
            "fit_view": lambda: self._set_fit_mode(True),
            "actual_size": lambda: self._set_zoom(1.0),
            "tool_paint": lambda: self._set_active_tool("paint"),
            "tool_erase": lambda: self._set_active_tool("erase"),
            "tool_fill": lambda: self._set_active_tool("fill"),
            "tool_gradient": lambda: self._set_active_tool("gradient"),
            "tool_smudge": lambda: self._set_active_tool("smudge"),
            "tool_dodge_burn": lambda: self._set_active_tool("dodge_burn"),
            "tool_move": lambda: self._set_active_tool("move"),
            "tool_rect": lambda: self._set_active_tool("select_rect"),
            "tool_lasso": lambda: self._set_active_tool("lasso"),
            "tool_clone": lambda: self._set_active_tool("clone"),
            "tool_heal": lambda: self._set_active_tool("heal"),
            "tool_patch": lambda: self._set_active_tool("patch"),
            "brush_smaller": lambda: self._nudge_brush_size(-1),
            "brush_larger": lambda: self._nudge_brush_size(1),
            "hardness_softer": lambda: self._nudge_brush_hardness(-1),
            "hardness_harder": lambda: self._nudge_brush_hardness(1),
            "toggle_quick_mask": self.toggle_quick_mask_shortcut,
        }
        for key, callback in bindings.items():
            self._register_shortcut(shortcut_map.get(key, ""), callback)

    def open_shortcuts_dialog(self) -> None:
        dialog = ShortcutEditorDialog(
            shortcuts=self._load_shortcuts_map(),
            labels=self._shortcut_labels(),
            defaults=self._default_shortcuts(),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        for key, sequence_text in dialog.shortcut_map().items():
            self.settings.setValue(f"texture_editor/shortcuts/{key}", sequence_text)
        self._rebuild_shortcuts()
        self._set_status("Texture Editor shortcuts updated.", False)

    def _handle_tool_settings_changed(self) -> None:
        sender = self.sender()
        if sender is self.brush_preset_combo:
            preset_key = str(self.brush_preset_combo.currentData() or "custom")
            if preset_key != "custom":
                self._apply_brush_preset(preset_key)
        elif (
            not self._applying_brush_preset
            and self._settings_ready
            and sender in {
                self.brush_size_slider,
                self.size_step_mode_combo,
                self.hardness_slider,
                self.roundness_slider,
                self.angle_slider,
                self.smoothing_slider,
                self.opacity_slider,
                self.flow_slider,
                self.spacing_slider,
                self.brush_tip_combo,
                self.brush_pattern_combo,
            }
        ):
            self._mark_brush_preset_custom()
        self.current_tool_settings = TextureEditorToolSettings(
            tool=self.current_tool_settings.tool,
            color_hex=self.paint_color_edit.text().strip() or "#C85A30",
            secondary_color_hex=self.secondary_color_edit.text().strip() or "#FFFFFF",
            brush_preset=str(self.brush_preset_combo.currentData() or "custom"),
            brush_tip=str(self.brush_tip_combo.currentData() or "round"),
            brush_pattern=str(self.brush_pattern_combo.currentData() or "solid"),
            custom_brush_tip_path=self.custom_brush_tip_path_edit.text().strip(),
            symmetry_mode=str(self.symmetry_mode_combo.currentData() or "off"),
            size=float(self.brush_size_slider.value()),
            size_step_mode=str(self.size_step_mode_combo.currentData() or "normal"),
            hardness=self.hardness_slider.value(),
            roundness=self.roundness_slider.value(),
            angle_degrees=self.angle_slider.value(),
            smoothing=self.smoothing_slider.value(),
            opacity=self.opacity_slider.value(),
            flow=self.flow_slider.value(),
            spacing=self.spacing_slider.value(),
            strength=self.strength_slider.value(),
            smudge_strength=self.smudge_strength_slider.value(),
            dodge_burn_mode=str(self.dodge_burn_mode_combo.currentData() or "dodge_midtones"),
            dodge_burn_exposure=self.dodge_burn_exposure_slider.value(),
            patch_blend=self.patch_blend_slider.value(),
            gradient_type=str(self.gradient_type_combo.currentData() or "linear"),
            paint_blend_mode=str(self.paint_blend_mode_combo.currentData() or "normal"),
            fill_tolerance=self.fill_tolerance_slider.value(),
            fill_contiguous=self.fill_contiguous_checkbox.isChecked(),
            sharpen_mode=str(self.sharpen_mode_combo.currentData() or "unsharp_mask"),
            soften_mode=str(self.soften_mode_combo.currentData() or "gaussian"),
            sample_visible_layers=self.sample_visible_layers_checkbox.isChecked(),
            clone_aligned=self.clone_aligned_checkbox.isChecked(),
            clone_source_point=self.current_tool_settings.clone_source_point,
            selection_combine_mode=str(self.selection_mode_combo.currentData() or "replace"),
            lasso_snap_to_edges=self.lasso_snap_checkbox.isChecked(),
            lasso_snap_radius=self.lasso_snap_radius_slider.value(),
            lasso_edge_sensitivity=self.lasso_snap_sensitivity_slider.value(),
            recolor_mode=str(self.recolor_mode_combo.currentData() or "tint"),
            recolor_source_hex=self.recolor_source_edit.text().strip() or "#808080",
            recolor_target_hex=self.recolor_target_edit.text().strip() or "#C85A30",
            recolor_tolerance=self.recolor_tolerance_slider.value(),
            recolor_strength=self.recolor_strength_slider.value(),
            recolor_preserve_luminance=self.recolor_preserve_luma_checkbox.isChecked(),
        )
        self.canvas.set_brush_size(self.current_tool_settings.size)
        self.canvas.set_brush_visual_state(
            hardness=self.current_tool_settings.hardness,
            tip=self.current_tool_settings.brush_tip,
            roundness=self.current_tool_settings.roundness,
            angle_degrees=self.current_tool_settings.angle_degrees,
            pattern=self.current_tool_settings.brush_pattern,
        )
        self.canvas.set_symmetry_mode(self.current_tool_settings.symmetry_mode)
        self._save_settings()
        self._refresh_tool_visibility()

    def _set_active_tool(self, tool_key: str) -> None:
        previous_tool = self.current_tool_settings.tool
        next_clone_source = self.current_tool_settings.clone_source_point
        if previous_tool in {"clone", "heal"} and tool_key not in {"clone", "heal"}:
            next_clone_source = None
        self.current_tool_settings = dataclasses.replace(
            self.current_tool_settings,
            tool=tool_key,
            clone_source_point=next_clone_source,
        )
        for key, button in self.tool_buttons.items():
            button.setChecked(key == tool_key)
        self.canvas.set_tool(tool_key)
        self.canvas.set_clone_source_point(self.current_tool_settings.clone_source_point)
        self._refresh_tool_visibility()
        self._set_status(self._tool_status_text(tool_key), False)

    def _tool_status_text(self, tool_key: str) -> str:
        if tool_key in {"clone", "heal"}:
            return "Ctrl+right-click sets the clone/heal source point. Use aligned sampling for classic retouching, or turn it off to stamp from a fixed source."
        if tool_key == "smudge":
            return "Smudge tool active. Drag to pull nearby texture detail for seam cleanup and blending."
        if tool_key == "dodge_burn":
            return "Dodge/Burn tool active. Use exposure and tonal mode to lighten or darken local texture detail."
        if tool_key == "patch":
            return "Patch tool active. Make a selection first, then drag to define the repair source offset for that selected region."
        if tool_key == "gradient":
            return "Gradient tool active. Drag to paint a linear or radial gradient using the primary and secondary colors."
        if tool_key == "recolor":
            return "Adjust recolor settings and use 'Apply Recolor To Active Layer'."
        if tool_key == "sharpen":
            return "Sharpen tool active. Adjust brush preset, brush tip, brush size, strength, sharpen mode, and whether to sample visible layers."
        if tool_key == "soften":
            return "Soften tool active. Adjust brush preset, brush tip, brush size, strength, soften mode, and whether to sample visible layers."
        if tool_key == "select_rect":
            return "Drag on the canvas to create a rectangular selection. Use the Selection panel to replace, add, subtract, intersect, feather, grow, or shrink."
        if tool_key == "lasso":
            return "Drag freely on the canvas to create a lasso selection. Optional edge snapping can pull it toward nearby texture edges, and the Selection panel controls how it combines."
        if tool_key == "move":
            return "Move tool active. Drag to reposition the active layer non-destructively."
        if tool_key == "fill":
            return "Fill tool active. Click to flood-fill the active layer using the current color, tolerance, and blend mode. Alt+click samples a color into the paint swatch."
        if tool_key == "paint":
            return "Paint tool active. Brush presets, image stamps, patterns, and symmetry are available here. Alt+click samples a color into the paint swatch."
        return f"{tool_key.replace('_', ' ').title()} tool active."

    def _refresh_tool_visibility(self) -> None:
        tool = self.current_tool_settings.tool
        visible_keys = {
            "brush_preset": tool in {"paint", "erase", "clone", "heal", "sharpen", "soften", "smudge", "dodge_burn"},
            "brush_tip": tool in {"paint", "erase", "clone", "heal", "sharpen", "soften", "smudge", "dodge_burn"},
            "custom_brush_tip": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"} and str(self.brush_tip_combo.currentData() or "round") == "image_stamp",
            "brush_pattern": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "symmetry_mode": tool in {"paint", "erase", "sharpen", "soften", "smudge", "dodge_burn"},
            "paint_color": tool in {"paint", "fill", "gradient"},
            "secondary_color": tool == "gradient",
            "brush_size": tool in {"paint", "erase", "clone", "heal", "sharpen", "soften", "smudge", "dodge_burn"},
            "size_step_mode": tool in {"paint", "erase", "clone", "heal", "sharpen", "soften", "smudge", "dodge_burn"},
            "hardness": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "roundness": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "angle_degrees": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "smoothing": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "opacity": tool in {"paint", "erase", "clone", "heal", "fill", "gradient", "smudge", "dodge_burn"},
            "flow": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "spacing": tool in {"paint", "erase", "clone", "heal", "smudge", "dodge_burn"},
            "paint_blend_mode": tool in {"paint", "fill", "gradient"},
            "fill_tolerance": tool == "fill",
            "fill_contiguous": tool == "fill",
            "strength": tool in {"sharpen", "soften"},
            "smudge_strength": tool == "smudge",
            "dodge_burn_mode": tool == "dodge_burn",
            "dodge_burn_exposure": tool == "dodge_burn",
            "patch_blend": tool == "patch",
            "gradient_type": tool == "gradient",
            "sharpen_mode": tool == "sharpen",
            "soften_mode": tool == "soften",
            "sample_visible_layers": tool in {"clone", "heal", "sharpen", "soften", "smudge", "patch"},
            "clone_aligned": tool in {"clone", "heal"},
            "clone_clear_source": tool in {"clone", "heal"},
            "lasso_snap_to_edges": tool == "lasso",
            "lasso_snap_radius": tool == "lasso" and self.lasso_snap_checkbox.isChecked(),
            "lasso_snap_sensitivity": tool == "lasso" and self.lasso_snap_checkbox.isChecked(),
            "clone_hint": tool in {"clone", "heal"},
            "recolor_mode": tool == "recolor",
            "recolor_source": tool == "recolor",
            "recolor_target": tool == "recolor",
            "recolor_tolerance": tool == "recolor",
            "recolor_strength": tool == "recolor",
            "recolor_preserve_luma": tool == "recolor",
            "recolor_apply": tool == "recolor",
        }
        for key, (label_widget, field_widget) in self._tool_setting_rows.items():
            visible = visible_keys.get(key, True)
            if label_widget is not None:
                label_widget.setVisible(visible)
            field_widget.setVisible(visible)
        selection_visible = tool in {"select_rect", "lasso"} or (
            self.document is not None and (self.document.selection.mode != "none" or self.document.quick_mask_enabled)
        )
        self.selection_section.setVisible(selection_visible)

    def _restore_left_sidebar_scroll(self, scroll_x: int, scroll_y: int) -> None:
        if self.left_scroll is None:
            return
        hbar = self.left_scroll.horizontalScrollBar()
        vbar = self.left_scroll.verticalScrollBar()
        hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), int(scroll_x))))
        vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), int(scroll_y))))

    def _capture_left_sidebar_scroll(self) -> Tuple[int, int]:
        if self.left_scroll is None:
            return (0, 0)
        return (
            int(self.left_scroll.horizontalScrollBar().value()),
            int(self.left_scroll.verticalScrollBar().value()),
        )

    def _schedule_left_sidebar_scroll_restore(self, scroll_x: int, scroll_y: int) -> None:
        QTimer.singleShot(0, lambda sx=scroll_x, sy=scroll_y: self._restore_left_sidebar_scroll(sx, sy))

    def _set_status(self, message: str, error: bool) -> None:
        sidebar_scroll = self._capture_left_sidebar_scroll()
        self.status_label.setText(message)
        self._schedule_left_sidebar_scroll_restore(*sidebar_scroll)
        self.status_message_requested.emit(message, error)

    def _document_composite_revision(self) -> int:
        if self.document is None:
            return -1
        revision = int(self.document.composite_revision)
        revision += sum(int(layer.revision) for layer in self.document.layers)
        revision += sum(int(layer.revision) for layer in self.document.adjustment_layers)
        if self.document.floating_selection is not None and self._floating_pixels is not None:
            revision += 1000003
            revision += int(self.document.floating_selection.offset_x)
            revision += int(self.document.floating_selection.offset_y)
            revision += int(round(self.document.floating_selection.rotation_degrees * 10.0))
            revision += int(round(self.document.floating_selection.scale_x * 100.0))
            revision += int(round(self.document.floating_selection.scale_y * 100.0))
            revision += 97 if self.document.floating_selection.flip_x else 0
            revision += 193 if self.document.floating_selection.flip_y else 0
        return revision

    def _invalidate_composite_cache(self, dirty_bounds: Optional[Tuple[int, int, int, int]] = None) -> None:
        self._composite_cache_revision = -1
        if dirty_bounds is None:
            self._composite_dirty_bounds = None
            self._composite_cache = None
            return
        if self._composite_dirty_bounds is None:
            self._composite_dirty_bounds = dirty_bounds
            return
        x0 = min(self._composite_dirty_bounds[0], dirty_bounds[0])
        y0 = min(self._composite_dirty_bounds[1], dirty_bounds[1])
        x1 = max(self._composite_dirty_bounds[0] + self._composite_dirty_bounds[2], dirty_bounds[0] + dirty_bounds[2])
        y1 = max(self._composite_dirty_bounds[1] + self._composite_dirty_bounds[3], dirty_bounds[1] + dirty_bounds[3])
        self._composite_dirty_bounds = (x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def _invalidate_layer_thumbnail(self, layer_id: str) -> None:
        keys = [key for key in self._thumbnail_cache.keys() if key[0] == layer_id]
        for key in keys:
            self._thumbnail_cache.pop(key, None)

    def _busy(self) -> bool:
        return self._task_thread is not None

    def _active_document_key(self) -> str:
        if self.document is None:
            return ""
        return self.document.project_path.as_posix() if self.document.project_path is not None else self.document.title

    def _parse_guides_text(self, text: str) -> Tuple[int, ...]:
        values: List[int] = []
        for raw in (text or "").replace(";", ",").split(","):
            token = raw.strip()
            if not token:
                continue
            try:
                values.append(max(0, int(round(float(token)))))
            except Exception:
                continue
        return tuple(sorted(dict.fromkeys(values)))

    def _guides_text(self, values: Sequence[int]) -> str:
        return ", ".join(str(max(0, int(value))) for value in values)

    def _refresh_navigation_overlays(self) -> None:
        has_doc = self.document is not None
        show_rulers = bool(self._show_rulers and has_doc)
        self.top_ruler.setVisible(show_rulers)
        self.left_ruler.setVisible(show_rulers)
        self.ruler_corner.setVisible(show_rulers)
        vertical_guides = self._vertical_guides if has_doc else ()
        horizontal_guides = self._horizontal_guides if has_doc else ()
        self.canvas.set_guide_state(
            enabled=bool(self._show_guides and has_doc),
            vertical_guides=vertical_guides,
            horizontal_guides=horizontal_guides,
        )
        if not has_doc:
            self.top_ruler.set_state(
                image_length=0,
                other_length=0,
                display_scale=1.0,
                scroll_value=0,
                viewport_offset=0,
                hover_position=None,
                guides=(),
            )
            self.left_ruler.set_state(
                image_length=0,
                other_length=0,
                display_scale=1.0,
                scroll_value=0,
                viewport_offset=0,
                hover_position=None,
                guides=(),
            )
            self.navigator_widget.set_state(None, image_width=0, image_height=0, viewport_rect=None)
            return
        scale = max(0.0001, self.canvas.current_display_scale())
        scroll_x = int(self.canvas_scroll.horizontalScrollBar().value())
        scroll_y = int(self.canvas_scroll.verticalScrollBar().value())
        canvas_origin = self.canvas.mapTo(self.canvas_scroll.viewport(), QPoint(0, 0))
        viewport_offset_x = int(canvas_origin.x())
        viewport_offset_y = int(canvas_origin.y())
        hover_x = None if self._hover_pixel_info is None else int(self._hover_pixel_info.get("x", 0))
        hover_y = None if self._hover_pixel_info is None else int(self._hover_pixel_info.get("y", 0))
        self.top_ruler.set_state(
            image_length=int(self.document.width),
            other_length=int(self.document.height),
            display_scale=scale,
            scroll_value=scroll_x,
            viewport_offset=viewport_offset_x,
            hover_position=hover_x,
            guides=vertical_guides,
        )
        self.left_ruler.set_state(
            image_length=int(self.document.height),
            other_length=int(self.document.width),
            display_scale=scale,
            scroll_value=scroll_y,
            viewport_offset=viewport_offset_y,
            hover_position=hover_y,
            guides=horizontal_guides,
        )
        display_image = getattr(self.canvas, "_display_image", None) or getattr(self.canvas, "_image", None)
        viewport = self.canvas_scroll.viewport().size()
        visible_w = min(float(self.document.width), max(1.0, float(viewport.width()) / scale))
        visible_h = min(float(self.document.height), max(1.0, float(viewport.height()) / scale))
        viewport_rect = (
            max(0.0, float(scroll_x) / scale),
            max(0.0, float(scroll_y) / scale),
            visible_w,
            visible_h,
        )
        self.navigator_widget.set_state(
            display_image,
            image_width=int(self.document.width),
            image_height=int(self.document.height),
            viewport_rect=viewport_rect,
        )

    def _handle_navigation_overlay_changed(self, *_args) -> None:
        self._show_rulers = bool(self.show_rulers_checkbox.isChecked())
        self._show_guides = bool(self.show_guides_checkbox.isChecked())
        self._vertical_guides = self._parse_guides_text(self.vertical_guides_edit.text())
        self._horizontal_guides = self._parse_guides_text(self.horizontal_guides_edit.text())
        self.vertical_guides_edit.blockSignals(True)
        self.horizontal_guides_edit.blockSignals(True)
        self.vertical_guides_edit.setText(self._guides_text(self._vertical_guides))
        self.horizontal_guides_edit.setText(self._guides_text(self._horizontal_guides))
        self.vertical_guides_edit.blockSignals(False)
        self.horizontal_guides_edit.blockSignals(False)
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def clear_guides(self) -> None:
        self._vertical_guides = ()
        self._horizontal_guides = ()
        self.vertical_guides_edit.blockSignals(True)
        self.horizontal_guides_edit.blockSignals(True)
        self.vertical_guides_edit.setText("")
        self.horizontal_guides_edit.setText("")
        self.vertical_guides_edit.blockSignals(False)
        self.horizontal_guides_edit.blockSignals(False)
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()
        self._refresh_ui()
        self._set_status("Texture Editor guides cleared.", False)

    def _handle_canvas_hover_changed(self, payload: object) -> None:
        self._hover_pixel_info = payload if isinstance(payload, dict) else None
        self._refresh_canvas_status_strip()
        self._refresh_navigation_overlays()

    def _handle_navigator_center_requested(self, image_x: float, image_y: float) -> None:
        if self.document is None:
            return
        scale = max(0.0001, self.canvas.current_display_scale())
        viewport = self.canvas_scroll.viewport().size()
        target_x = int(round((float(image_x) * scale) - (float(viewport.width()) / 2.0)))
        target_y = int(round((float(image_y) * scale) - (float(viewport.height()) / 2.0)))
        hbar = self.canvas_scroll.horizontalScrollBar()
        vbar = self.canvas_scroll.verticalScrollBar()
        hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), target_x)))
        vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), target_y)))
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def _capture_view_state(self) -> Dict[str, object]:
        return {
            "zoom_factor": float(self.canvas.current_display_scale()),
            "fit_to_view": bool(self.canvas.is_fit_to_view()),
            "view_mode": str(self.view_mode_combo.currentData() or "edited"),
            "compare_split": int(self.compare_split_slider.value()),
            "grid_enabled": bool(self.grid_checkbox.isChecked()),
            "grid_size": int(self.grid_size_spin.value()),
            "grid_color": self._grid_color.name(QColor.HexRgb),
            "grid_opacity": int(self.grid_opacity_spin.value()),
            "show_rulers": bool(self._show_rulers),
            "show_guides": bool(self._show_guides),
            "vertical_guides": list(self._vertical_guides),
            "horizontal_guides": list(self._horizontal_guides),
            "scroll_x": int(self.canvas_scroll.horizontalScrollBar().value()),
            "scroll_y": int(self.canvas_scroll.verticalScrollBar().value()),
        }

    def _apply_view_state(self, state: Optional[Dict[str, object]]) -> None:
        if not state:
            self.show_rulers_checkbox.blockSignals(True)
            self.show_guides_checkbox.blockSignals(True)
            self.show_rulers_checkbox.setChecked(True)
            self.show_guides_checkbox.setChecked(False)
            self.show_rulers_checkbox.blockSignals(False)
            self.show_guides_checkbox.blockSignals(False)
            self._show_rulers = True
            self._show_guides = False
            self._vertical_guides = ()
            self._horizontal_guides = ()
            self.vertical_guides_edit.setText("")
            self.horizontal_guides_edit.setText("")
            self.canvas.set_zoom_factor(1.0)
            self.canvas_scroll.horizontalScrollBar().setValue(0)
            self.canvas_scroll.verticalScrollBar().setValue(0)
            self._refresh_zoom_indicators()
            self._refresh_navigation_overlays()
            return
        view_mode = str(state.get("view_mode", "edited"))
        index = self.view_mode_combo.findData(view_mode)
        if index >= 0:
            self.view_mode_combo.blockSignals(True)
            self.view_mode_combo.setCurrentIndex(index)
            self.view_mode_combo.blockSignals(False)
        self.compare_split_slider.blockSignals(True)
        self.compare_split_slider.setValue(int(state.get("compare_split", self.compare_split_slider.value())))
        self.compare_split_slider.blockSignals(False)
        self.grid_checkbox.blockSignals(True)
        self.grid_checkbox.setChecked(bool(state.get("grid_enabled", self.grid_checkbox.isChecked())))
        self.grid_checkbox.blockSignals(False)
        self.grid_size_spin.blockSignals(True)
        self.grid_size_spin.setValue(int(state.get("grid_size", self.grid_size_spin.value())))
        self.grid_size_spin.blockSignals(False)
        self._set_grid_color(QColor(str(state.get("grid_color", self._grid_color.name(QColor.HexRgb)))), save=False, apply=False)
        self.grid_opacity_spin.blockSignals(True)
        self.grid_opacity_spin.setValue(int(state.get("grid_opacity", self.grid_opacity_spin.value())))
        self.grid_opacity_spin.blockSignals(False)
        self.show_rulers_checkbox.blockSignals(True)
        self.show_guides_checkbox.blockSignals(True)
        self.show_rulers_checkbox.setChecked(bool(state.get("show_rulers", True)))
        self.show_guides_checkbox.setChecked(bool(state.get("show_guides", False)))
        self.show_rulers_checkbox.blockSignals(False)
        self.show_guides_checkbox.blockSignals(False)
        vertical_guides = state.get("vertical_guides") or []
        horizontal_guides = state.get("horizontal_guides") or []
        self._show_rulers = bool(self.show_rulers_checkbox.isChecked())
        self._show_guides = bool(self.show_guides_checkbox.isChecked())
        self._vertical_guides = tuple(int(value) for value in vertical_guides if isinstance(value, (int, float)))
        self._horizontal_guides = tuple(int(value) for value in horizontal_guides if isinstance(value, (int, float)))
        self.vertical_guides_edit.setText(self._guides_text(self._vertical_guides))
        self.horizontal_guides_edit.setText(self._guides_text(self._horizontal_guides))
        if bool(state.get("fit_to_view", True)):
            self.canvas.set_fit_to_view(True)
        else:
            self.canvas.set_zoom_factor(float(state.get("zoom_factor", 1.0)))
            self.canvas_scroll.horizontalScrollBar().setValue(int(state.get("scroll_x", 0)))
            self.canvas_scroll.verticalScrollBar().setValue(int(state.get("scroll_y", 0)))
        self._refresh_zoom_indicators()
        self._refresh_navigation_overlays()

    def _run_async_task(
        self,
        *,
        label: str,
        task: Callable[[], object],
        on_success: Callable[[object], None],
    ) -> bool:
        if self._busy():
            self._set_status("Texture Editor is already busy. Wait for the current task to finish.", True)
            return False
        self._busy_task_label = label
        self._task_success_callback = on_success
        self._set_status(label, False)
        thread = QThread(self)
        worker = TextureEditorTaskWorker(task)
        worker.moveToThread(thread)
        worker.completed.connect(self._handle_async_task_completed)
        worker.error.connect(self._handle_async_task_error)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._handle_async_task_finished)
        thread.started.connect(worker.run)
        self._task_thread = thread
        self._task_worker = worker
        self._refresh_ui()
        thread.start()
        return True

    @Slot(object)
    def _handle_async_task_completed(self, result: object) -> None:
        callback = self._task_success_callback
        if callback is not None:
            callback(result)

    @Slot(str)
    def _handle_async_task_error(self, message: str) -> None:
        QMessageBox.warning(self, APP_TITLE, message)
        self._set_status(f"{self._busy_task_label or 'Texture Editor task'} failed.", True)

    @Slot()
    def _handle_async_task_finished(self) -> None:
        thread = self._task_thread
        worker = self._task_worker
        self._task_thread = None
        self._task_worker = None
        self._task_success_callback = None
        self._busy_task_label = ""
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._refresh_ui()

    def _store_active_session(self) -> None:
        if self._switching_session:
            return
        if not (0 <= self._active_session_index < len(self._sessions)):
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        session = self._sessions[self._active_session_index]
        session.document = self.document
        session.layer_pixels = self.layer_pixels
        session.history_snapshots = self.history_snapshots
        session.history_index = self.history_index
        session.layer_property_dirty = self._layer_property_dirty
        session.floating_pixels = None if self._floating_pixels is None else self._floating_pixels.copy()
        session.floating_mask = None if self._floating_mask is None else self._floating_mask.copy()
        session.composite_cache = None if self._composite_cache is None else self._composite_cache.copy()
        session.composite_cache_revision = self._composite_cache_revision
        session.composite_dirty_bounds = self._composite_dirty_bounds
        session.thumbnail_cache = dict(self._thumbnail_cache)
        session.label = self.document.title if self.document is not None else session.label
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()
        self._sync_document_tab_label(self._active_session_index)

    def _sync_document_tab_label(self, index: int) -> None:
        if not (0 <= index < len(self._sessions)):
            return
        label = self._sessions[index].label or f"Document {index + 1}"
        self.document_tab_bar.setTabText(index, label)
        document = self._sessions[index].document
        tooltip = ""
        if document is not None:
            tooltip = document.source_binding.source_path or str(document.project_path or "")
        self.document_tab_bar.setTabToolTip(index, tooltip)

    def _load_session_index(self, index: int) -> None:
        self._switching_session = True
        try:
            if not (0 <= index < len(self._sessions)):
                self._active_session_index = -1
                self.document = None
                self.layer_pixels = {}
                self.history_snapshots = []
                self.history_index = -1
                self._layer_property_dirty = False
                self._floating_pixels = None
                self._floating_mask = None
                self._composite_cache = None
                self._composite_cache_revision = -1
                self._composite_dirty_bounds = None
                self._thumbnail_cache = {}
                self.document_tab_bar.blockSignals(True)
                self.document_tab_bar.setCurrentIndex(-1)
                self.document_tab_bar.blockSignals(False)
                self._refresh_ui()
                return
            self._active_session_index = index
            session = self._sessions[index]
            self.document = session.document
            self.layer_pixels = session.layer_pixels
            self.history_snapshots = session.history_snapshots
            self.history_index = session.history_index
            self._layer_property_dirty = session.layer_property_dirty
            self._floating_pixels = None if session.floating_pixels is None else session.floating_pixels.copy()
            self._floating_mask = None if session.floating_mask is None else session.floating_mask.copy()
            self._composite_cache = None if session.composite_cache is None else session.composite_cache.copy()
            self._composite_cache_revision = session.composite_cache_revision
            self._composite_dirty_bounds = session.composite_dirty_bounds
            self._thumbnail_cache = dict(session.thumbnail_cache)
            self.document_tab_bar.blockSignals(True)
            self.document_tab_bar.setCurrentIndex(index)
            self.document_tab_bar.blockSignals(False)
            self._sync_document_tab_label(index)
            self.workspace = dataclasses.replace(
                self.workspace,
                open_document_ids=tuple(
                    candidate.document.project_path.as_posix() if candidate.document and candidate.document.project_path is not None else candidate.label
                    for candidate in self._sessions
                ),
                active_document_id=self._active_document_key(),
            )
            self._apply_view_state(self.workspace.document_view_state.get(self._active_document_key()))
            self._refresh_ui()
        finally:
            self._switching_session = False

    def _create_session(self, document: TextureEditorDocument, layer_pixels: Dict[str, np.ndarray], *, label: str) -> None:
        self._store_active_session()
        session = _TextureEditorSession(
            label=label,
            document=document,
            layer_pixels=layer_pixels,
            history_snapshots=[],
            history_index=-1,
            original_flattened=flatten_texture_editor_layers(document, layer_pixels),
            layer_property_dirty=False,
            floating_pixels=None,
            floating_mask=None,
            composite_cache=None,
            composite_cache_revision=-1,
            composite_dirty_bounds=None,
            thumbnail_cache={},
        )
        self._sessions.append(session)
        self.document_tab_bar.addTab(label)
        self.document_tab_bar.show()
        self.workspace = dataclasses.replace(
            self.workspace,
            open_document_ids=tuple(
                candidate.document.project_path.as_posix() if candidate.document and candidate.document.project_path is not None else candidate.label
                for candidate in self._sessions
            ),
        )
        self._load_session_index(len(self._sessions) - 1)

    def _handle_document_tab_changed(self, index: int) -> None:
        if self._switching_session or index == self._active_session_index:
            return
        self._store_active_session()
        self._load_session_index(index)

    def _close_document_tab(self, index: int) -> None:
        if not (0 <= index < len(self._sessions)):
            return
        self._store_active_session()
        closing_current = index == self._active_session_index
        self.document_tab_bar.blockSignals(True)
        self.document_tab_bar.removeTab(index)
        self.document_tab_bar.blockSignals(False)
        self._sessions.pop(index)
        self.workspace = dataclasses.replace(
            self.workspace,
            open_document_ids=tuple(
                candidate.document.project_path.as_posix() if candidate.document and candidate.document.project_path is not None else candidate.label
                for candidate in self._sessions
            ),
        )
        if not self._sessions:
            self.document_tab_bar.hide()
            self._load_session_index(-1)
            self._set_status("Closed the last Texture Editor document.", False)
            return
        next_index = index
        if closing_current:
            next_index = min(index, len(self._sessions) - 1)
        elif index < self._active_session_index:
            self._active_session_index -= 1
            next_index = self._active_session_index
        self._load_session_index(next_index)
        self.document_tab_bar.show()

    def _configured_root_path(self, getter) -> Optional[Path]:
        try:
            raw = str(getter()).strip()
        except Exception:
            return None
        if not raw:
            return None
        try:
            return Path(raw).expanduser().resolve()
        except Exception:
            return None

    def _build_binding_for_source(
        self,
        source_path: Path,
        *,
        launch_origin: str,
        binding: Optional[TextureEditorSourceBinding] = None,
    ) -> TextureEditorSourceBinding:
        resolved = source_path.expanduser().resolve()
        source_binding = dataclasses.replace(binding) if binding is not None else TextureEditorSourceBinding()
        if not source_binding.launch_origin:
            source_binding.launch_origin = launch_origin
        if not source_binding.display_name:
            source_binding.display_name = resolved.name
        if not source_binding.source_path:
            source_binding.source_path = str(resolved)
        if not source_binding.source_identity_path:
            source_binding.source_identity_path = source_binding.source_path

        if not source_binding.relative_path or not source_binding.package_root:
            png_root = self._configured_root_path(self.get_png_root)
            original_root = self._configured_root_path(self.get_original_dds_root)
            inferred_relative = ""
            inferred_package = ""
            inferred_archive_relative = ""
            for root in (png_root, original_root):
                if root is None:
                    continue
                try:
                    relative = resolved.relative_to(root)
                except Exception:
                    continue
                inferred_relative = PurePosixPath(relative.as_posix()).as_posix()
                parts = [part for part in PurePosixPath(inferred_relative).parts if part]
                if parts:
                    inferred_package = parts[0]
                    inferred_archive_relative = (
                        PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else ""
                    )
                break
            if not inferred_relative:
                parts = list(PurePosixPath(resolved.as_posix()).parts)
                package_index = next((idx for idx, part in enumerate(parts) if len(part) == 4 and part.isdigit()), -1)
                if package_index >= 0 and package_index + 1 < len(parts):
                    inferred_relative = PurePosixPath(*parts[package_index:]).as_posix()
                    inferred_package = parts[package_index]
                    inferred_archive_relative = (
                        PurePosixPath(*parts[package_index + 1:]).as_posix()
                        if package_index + 1 < len(parts)
                        else ""
                    )
            if inferred_relative and not source_binding.relative_path:
                source_binding.relative_path = inferred_relative
            if inferred_package and not source_binding.package_root:
                source_binding.package_root = inferred_package
            if inferred_archive_relative and not source_binding.archive_relative_path:
                source_binding.archive_relative_path = inferred_archive_relative

        if not source_binding.original_dds_path:
            original_root = self._configured_root_path(self.get_original_dds_root)
            if resolved.suffix.lower() == ".dds":
                source_binding.original_dds_path = str(resolved)
            elif original_root is not None and source_binding.relative_path:
                candidate = (original_root / Path(PurePosixPath(source_binding.relative_path))).with_suffix(".dds")
                if candidate.exists():
                    source_binding.original_dds_path = str(candidate)
            else:
                sibling_dds = resolved.with_suffix(".dds")
                if sibling_dds.exists():
                    source_binding.original_dds_path = str(sibling_dds)
        return source_binding

    def _pick_color_into(self, line_edit: QLineEdit) -> None:
        color = QColorDialog.getColor(QColor(line_edit.text() or "#C85A30"), self, "Choose color")
        if color.isValid():
            line_edit.setText(color.name().upper())

    def _handle_canvas_color_sampled(self, payload: str) -> None:
        try:
            target, color_hex = payload.split("|", 1)
        except ValueError:
            return
        if target == "paint":
            self.paint_color_edit.setText(color_hex)
        elif target == "secondary":
            self.secondary_color_edit.setText(color_hex)
        elif target == "recolor_source":
            self.recolor_source_edit.setText(color_hex)
        elif target == "recolor_target":
            self.recolor_target_edit.setText(color_hex)
        self._set_status(f"Sampled color {color_hex}.", False)

    def _nudge_brush_size(self, direction: int) -> None:
        current = float(self.brush_size_slider.value())
        size_mode = str(self.size_step_mode_combo.currentData() or "normal")
        step = 1 if size_mode == "fine" else 4
        new_value = int(max(self.brush_size_slider.minimum(), min(self.brush_size_slider.maximum(), current + (step * direction))))
        self.brush_size_slider.setValue(new_value)

    def _nudge_brush_hardness(self, direction: int) -> None:
        current = int(self.hardness_slider.value())
        new_value = max(self.hardness_slider.minimum(), min(self.hardness_slider.maximum(), current + (5 * direction)))
        self.hardness_slider.setValue(new_value)

    def _set_channel_lock_state(self, red: bool, green: bool, blue: bool, alpha: bool) -> None:
        self.channel_red_checkbox.setChecked(red)
        self.channel_green_checkbox.setChecked(green)
        self.channel_blue_checkbox.setChecked(blue)
        self.channel_alpha_checkbox.setChecked(alpha)

    def _handle_channel_lock_changed(self) -> None:
        if self.document is None:
            return
        self.document = dataclasses.replace(
            self.document,
            edit_red_channel=self.channel_red_checkbox.isChecked(),
            edit_green_channel=self.channel_green_checkbox.isChecked(),
            edit_blue_channel=self.channel_blue_checkbox.isChecked(),
            edit_alpha_channel=self.channel_alpha_checkbox.isChecked(),
        )
        self._refresh_canvas_status_strip()
        self._set_status(
            f"Channel edit locks: {'R' if self.document.edit_red_channel else '-'}{'G' if self.document.edit_green_channel else '-'}{'B' if self.document.edit_blue_channel else '-'}{'A' if self.document.edit_alpha_channel else '-'}",
            False,
        )

    def extract_active_channel_to_new_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_key = str(self.channel_extract_combo.currentData() or "alpha")
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        extracted = extract_texture_editor_layer_channel_to_rgba(self.layer_pixels[layer_id], channel_key)
        self.document, self.layer_pixels, new_id = add_texture_editor_layer(
            self.document,
            self.layer_pixels,
            name=f"{layer.name} {channel_key.title()}",
            initial_pixels=extracted,
            offset_x=int(layer.offset_x),
            offset_y=int(layer.offset_y),
        )
        self._record_history_change(
            f"Extract {channel_key.title()} Channel",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="channel_extract",
            tracked_layer_ids=[layer_id, new_id],
            force_checkpoint=True,
        )
        self._refresh_ui()
        for row in range(self.layers_list.count()):
            item = self.layers_list.item(row)
            if item is not None and item.data(Qt.UserRole) == new_id:
                self.layers_list.setCurrentItem(item)
                break
        self._set_status(f"Extracted the {channel_key.title()} channel into a new layer.", False)

    def write_active_layer_luma_to_selected_channel(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_key = str(self.channel_pack_combo.currentData() or "alpha")
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        updated = write_texture_editor_layer_luma_to_channel(self.layer_pixels[layer_id], channel_key)
        if layer.alpha_locked and channel_key == "alpha":
            self._set_status("Unlock alpha before packing luminance into the alpha channel.", True)
            return
        self.layer_pixels[layer_id] = updated
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            f"Pack Luma To {channel_key.title()}",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="channel_pack",
            tracked_layer_ids=[layer_id],
        )
        self._refresh_ui()
        self._set_status(f"Packed active-layer luminance into the {channel_key.title()} channel.", False)

    def load_selected_channel_as_selection(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_key = str(self.channel_selection_combo.currentData() or "alpha")
        before_document = dataclasses.replace(self.document)
        self.document = load_texture_editor_layer_channel_as_selection(
            self.document,
            layer,
            self.layer_pixels[layer_id],
            channel_key,
            mask_pixels=self.layer_pixels.get(layer.mask_layer_id) if layer.mask_layer_id else None,
            combine_mode=str(self.selection_mode_combo.currentData() or "replace"),
        )
        self._record_history_change(
            f"Load {channel_key.title()} Channel As Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()
        self._set_status(f"Loaded the {channel_key.title()} channel as a selection.", False)

    def write_selection_to_selected_channel(self) -> None:
        if self.document is None or self.document.selection.mode == "none":
            self._set_status("Create a selection first, then write it to a channel.", True)
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_key = str(self.channel_selection_to_combo.currentData() or "alpha")
        if layer.alpha_locked and channel_key == "alpha":
            self._set_status("Unlock alpha before writing the selection into the alpha channel.", True)
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        updated = write_texture_editor_selection_to_layer_channel(
            self.document,
            layer,
            self.layer_pixels[layer_id],
            channel_key,
        )
        self.layer_pixels[layer_id] = updated
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            f"Write Selection To {channel_key.title()}",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="channel_pack",
            tracked_layer_ids=[layer_id],
        )
        self._refresh_ui()
        self._set_status(f"Wrote the current selection into the {channel_key.title()} channel.", False)

    def copy_selected_channel(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        channel_key = str(self.channel_copy_combo.currentData() or "alpha")
        self.channel_clipboard = (
            copy_texture_editor_layer_channel(self.layer_pixels[layer_id], channel_key),
            channel_key,
        )
        self._refresh_channel_controls()
        self._set_status(f"Copied the {channel_key.title()} channel to the editor clipboard.", False)

    def paste_channel_clipboard(self) -> None:
        if self.document is None or self.channel_clipboard is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_key = str(self.channel_paste_combo.currentData() or "alpha")
        if layer.alpha_locked and channel_key == "alpha":
            self._set_status("Unlock alpha before pasting into the alpha channel.", True)
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        channel_data, _source_key = self.channel_clipboard
        self.layer_pixels[layer_id] = paste_texture_editor_channel_into_layer(
            self.layer_pixels[layer_id],
            channel_key,
            channel_data,
        )
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            f"Paste Channel To {channel_key.title()}",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="channel_pack",
            tracked_layer_ids=[layer_id],
        )
        self._refresh_ui()
        self._set_status(f"Pasted the channel clipboard into the {channel_key.title()} channel.", False)

    def swap_selected_channels(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        channel_a = str(self.channel_swap_a_combo.currentData() or "red")
        channel_b = str(self.channel_swap_b_combo.currentData() or "blue")
        if channel_a == channel_b:
            self._set_status("Choose two different channels to swap.", True)
            return
        if layer.alpha_locked and "alpha" in {channel_a, channel_b}:
            self._set_status("Unlock alpha before swapping with the alpha channel.", True)
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        self.layer_pixels[layer_id] = swap_texture_editor_layer_channels(
            self.layer_pixels[layer_id],
            channel_a,
            channel_b,
        )
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            f"Swap {channel_a.title()} / {channel_b.title()}",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="channel_pack",
            tracked_layer_ids=[layer_id],
        )
        self._refresh_ui()
        self._set_status(f"Swapped the {channel_a.title()} and {channel_b.title()} channels.", False)

    def _prompt_document_dimensions(
        self,
        *,
        title: str,
        width: int,
        height: int,
        allow_anchor: bool = False,
        keep_aspect_default: bool = False,
    ) -> Optional[Tuple[int, int, str]]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        width_spin = QSpinBox(dialog)
        width_spin.setRange(1, 16384)
        width_spin.setValue(max(1, int(width)))
        height_spin = QSpinBox(dialog)
        height_spin.setRange(1, 16384)
        height_spin.setValue(max(1, int(height)))
        form.addRow("Width", width_spin)
        form.addRow("Height", height_spin)
        keep_aspect_checkbox: Optional[QCheckBox] = None
        if not allow_anchor:
            keep_aspect_checkbox = QCheckBox("Keep aspect ratio", dialog)
            keep_aspect_checkbox.setChecked(bool(keep_aspect_default))
            form.addRow("", keep_aspect_checkbox)
            base_ratio = float(max(1, int(width))) / float(max(1, int(height)))
            updating = {"active": False}

            def _sync_from_width(value: int) -> None:
                if keep_aspect_checkbox is None or not keep_aspect_checkbox.isChecked() or updating["active"]:
                    return
                updating["active"] = True
                try:
                    height_spin.setValue(max(1, int(round(float(value) / max(base_ratio, 1e-6)))))
                finally:
                    updating["active"] = False

            def _sync_from_height(value: int) -> None:
                if keep_aspect_checkbox is None or not keep_aspect_checkbox.isChecked() or updating["active"]:
                    return
                updating["active"] = True
                try:
                    width_spin.setValue(max(1, int(round(float(value) * base_ratio))))
                finally:
                    updating["active"] = False

            width_spin.valueChanged.connect(_sync_from_width)
            height_spin.valueChanged.connect(_sync_from_height)
        anchor_combo: Optional[QComboBox] = None
        if allow_anchor:
            anchor_combo = QComboBox(dialog)
            anchor_combo.addItem("Top Left", "top_left")
            anchor_combo.addItem("Center", "center")
            form.addRow("Anchor", anchor_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        anchor_value = str(anchor_combo.currentData() or "top_left") if anchor_combo is not None else "top_left"
        return (int(width_spin.value()), int(height_spin.value()), anchor_value)

    def resize_document_image(self) -> None:
        if self.document is None:
            return
        result = self._prompt_document_dimensions(
            title="Image Size",
            width=self.document.width,
            height=self.document.height,
            allow_anchor=False,
            keep_aspect_default=True,
        )
        if result is None:
            return
        new_width, new_height, _anchor = result
        self._apply_document_pixels_change(
            "Image Size",
            lambda: resize_texture_editor_document_image(self.document, self.layer_pixels, new_width, new_height),
        )

    def resize_document_canvas(self) -> None:
        if self.document is None:
            return
        result = self._prompt_document_dimensions(
            title="Canvas Size",
            width=self.document.width,
            height=self.document.height,
            allow_anchor=True,
        )
        if result is None:
            return
        new_width, new_height, anchor = result
        self._apply_document_pixels_change(
            "Canvas Size",
            lambda: resize_texture_editor_document_canvas(
                self.document,
                self.layer_pixels,
                new_width,
                new_height,
                anchor=anchor,
            ),
        )

    def _adjust_zoom(self, step: int) -> None:
        current = self.canvas.current_display_scale()
        factor = current * (1.15 if step > 0 else 0.87)
        self._set_zoom(factor)

    def _handle_canvas_wheel_zoom(self, delta: int, widget_x: int, widget_y: int) -> None:
        if self.document is None:
            return
        old_scale = max(0.0001, self.canvas.current_display_scale())
        viewport_pos = self.canvas.mapTo(self.canvas_scroll.viewport(), QPoint(int(widget_x), int(widget_y)))
        viewport_x = viewport_pos.x()
        viewport_y = viewport_pos.y()
        image_x = widget_x / old_scale
        image_y = widget_y / old_scale
        if abs(delta) >= 60:
            factor = 1.15 ** max(-4.0, min(4.0, delta / 120.0))
        else:
            factor = 1.0025 ** max(-480.0, min(480.0, float(delta)))
        self._set_zoom(old_scale * factor)
        new_scale = max(0.0001, self.canvas.current_display_scale())
        self.canvas_scroll.horizontalScrollBar().setValue(int(round((image_x * new_scale) - viewport_x)))
        self.canvas_scroll.verticalScrollBar().setValue(int(round((image_y * new_scale) - viewport_y)))
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def _refresh_zoom_indicators(self) -> None:
        scale_text = f"{self.canvas.current_display_scale():.0%}"
        if self.canvas.is_fit_to_view():
            self.zoom_label.setText(f"Fit {scale_text}")
        else:
            self.zoom_label.setText(scale_text)
        if self.document is None:
            self.canvas_status_zoom_label.setText("No zoom")
        elif self.canvas.is_fit_to_view():
            self.canvas_status_zoom_label.setText(f"Zoom Fit {scale_text}")
        else:
            self.canvas_status_zoom_label.setText(f"Zoom {scale_text}")

    def _set_fit_mode(self, fit_to_view: bool) -> None:
        self.canvas.set_fit_to_view(fit_to_view)
        self._refresh_zoom_indicators()
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def _set_zoom(self, factor: float) -> None:
        self.canvas.set_zoom_factor(factor)
        self._refresh_zoom_indicators()
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def _handle_view_mode_changed(self) -> None:
        mode = str(self.view_mode_combo.currentData() or "edited")
        self.compare_split_slider.setVisible(mode == "split")
        self.canvas.set_view_mode(mode)
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()
        self._save_settings()

    def _handle_compare_split_changed(self, value: int) -> None:
        self.canvas.set_compare_split_percent(value)
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()
        self._save_settings()

    def _handle_grid_state_changed(self, *_args) -> None:
        grid_color = QColor(getattr(self, "_grid_color", QColor("#74C1FF")))
        self.canvas.set_grid_state(
            enabled=self.grid_checkbox.isChecked(),
            grid_size=self.grid_size_spin.value(),
            grid_color=grid_color,
            grid_opacity=self.grid_opacity_spin.value(),
        )
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()
        self._save_settings()

    def _set_grid_color(self, color: QColor, *, save: bool = True, apply: bool = True) -> None:
        resolved = QColor(color) if color.isValid() else QColor("#74C1FF")
        self._grid_color = resolved
        self._update_grid_color_button()
        if apply:
            self._handle_grid_state_changed()
        elif save:
            self._save_settings()

    def _update_grid_color_button(self) -> None:
        color = QColor(self._grid_color if self._grid_color.isValid() else QColor("#74C1FF"))
        self.grid_color_button.setStyleSheet(
            "QToolButton {"
            f"background-color: {color.name(QColor.HexRgb)};"
            "border: 1px solid rgba(220, 230, 245, 0.35);"
            "border-radius: 4px;"
            "}"
        )
        self.grid_color_button.setText("")
        self.grid_color_button.setToolTip(f"Grid color: {color.name(QColor.HexRgb).upper()}")

    def _pick_grid_color(self) -> None:
        chosen = QColorDialog.getColor(self._grid_color, self, "Select grid color")
        if not chosen.isValid():
            return
        self._set_grid_color(chosen)

    def _handle_canvas_viewport_changed(self, *_args) -> None:
        self._refresh_zoom_indicators()
        self._refresh_navigation_overlays()
        document_key = self._active_document_key()
        if document_key:
            self.workspace.document_view_state[document_key] = self._capture_view_state()

    def _encode_rgba_blob(self, pixels: np.ndarray) -> bytes:
        encoded = cv2.imencode(".png", cv2.cvtColor(np.asarray(pixels, dtype=np.uint8), cv2.COLOR_RGBA2BGRA))[1]
        return bytes(encoded)

    def _decode_rgba_blob(self, blob: Optional[bytes]) -> Optional[np.ndarray]:
        if not blob:
            return None
        decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if decoded is None:
            return None
        if decoded.ndim == 2:
            decoded = cv2.cvtColor(decoded, cv2.COLOR_GRAY2BGRA)
        elif decoded.shape[2] == 3:
            decoded = cv2.cvtColor(decoded, cv2.COLOR_BGR2BGRA)
        return np.asarray(cv2.cvtColor(decoded, cv2.COLOR_BGRA2RGBA), dtype=np.uint8).copy()

    def _history_layer_canvas_offset(self, document: TextureEditorDocument, layer_id: str) -> Tuple[int, int]:
        for layer in document.layers:
            if layer.layer_id == layer_id or layer.mask_layer_id == layer_id:
                return (int(layer.offset_x), int(layer.offset_y))
        return (0, 0)

    def _encode_history_layer_state(
        self,
        document: TextureEditorDocument,
        layer_id: str,
        pixels: Optional[np.ndarray],
        *,
        dirty_bounds: Optional[Tuple[int, int, int, int]],
        previous_pixels: Optional[np.ndarray] = None,
    ) -> Optional[object]:
        if pixels is None:
            return None
        if dirty_bounds is None or previous_pixels is None or previous_pixels.shape != pixels.shape:
            return self._encode_rgba_blob(pixels)
        offset_x, offset_y = self._history_layer_canvas_offset(document, layer_id)
        dirty_x, dirty_y, dirty_w, dirty_h = dirty_bounds
        gx0 = max(int(offset_x), int(dirty_x))
        gy0 = max(int(offset_y), int(dirty_y))
        gx1 = min(int(offset_x + pixels.shape[1]), int(dirty_x + dirty_w))
        gy1 = min(int(offset_y + pixels.shape[0]), int(dirty_y + dirty_h))
        if gx1 <= gx0 or gy1 <= gy0:
            return None
        lx0 = int(gx0 - offset_x)
        ly0 = int(gy0 - offset_y)
        lw = int(gx1 - gx0)
        lh = int(gy1 - gy0)
        if lw <= 0 or lh <= 0:
            return None
        patch_area = lw * lh
        full_area = max(1, int(pixels.shape[0]) * int(pixels.shape[1]))
        if patch_area >= int(full_area * 0.6):
            return self._encode_rgba_blob(pixels)
        patch = pixels[ly0:ly0 + lh, lx0:lx0 + lw]
        return {
            "mode": "patch",
            "shape": [int(pixels.shape[0]), int(pixels.shape[1])],
            "local_bounds": [lx0, ly0, lw, lh],
            "blob": self._encode_rgba_blob(patch),
        }

    def _decode_history_layer_state(
        self,
        current_pixels: Optional[np.ndarray],
        payload: object,
    ) -> Optional[np.ndarray]:
        if payload is None:
            return None
        if isinstance(payload, (bytes, bytearray)):
            return self._decode_rgba_blob(bytes(payload))
        if not isinstance(payload, dict):
            return None
        mode = str(payload.get("mode", "") or "")
        if mode != "patch":
            blob = payload.get("blob")
            return self._decode_rgba_blob(blob if isinstance(blob, (bytes, bytearray)) else None)
        shape_raw = payload.get("shape")
        bounds_raw = payload.get("local_bounds")
        blob = payload.get("blob")
        if not (
            isinstance(shape_raw, list)
            and len(shape_raw) == 2
            and isinstance(bounds_raw, list)
            and len(bounds_raw) == 4
            and isinstance(blob, (bytes, bytearray))
        ):
            return None
        target_h = max(1, int(shape_raw[0]))
        target_w = max(1, int(shape_raw[1]))
        lx0, ly0, lw, lh = (max(0, int(value)) for value in bounds_raw)
        patch = self._decode_rgba_blob(bytes(blob))
        if patch is None:
            return None
        if current_pixels is not None and current_pixels.shape == (target_h, target_w, 4):
            restored = current_pixels.copy()
        else:
            restored = np.zeros((target_h, target_w, 4), dtype=np.uint8)
        restored[ly0:ly0 + min(lh, patch.shape[0]), lx0:lx0 + min(lw, patch.shape[1])] = patch[:lh, :lw]
        return restored

    def _history_auxiliary_layer_ids(self, document: TextureEditorDocument) -> set[str]:
        aux_ids: set[str] = set()
        for layer in document.layers:
            if layer.mask_layer_id:
                aux_ids.add(layer.mask_layer_id)
        for adjustment in document.adjustment_layers:
            if adjustment.mask_layer_id:
                aux_ids.add(adjustment.mask_layer_id)
        return aux_ids

    def _build_checkpoint_record(self, label: str) -> Dict[str, object]:
        snapshot = capture_texture_editor_snapshot(self.document, self.layer_pixels, label)
        return {
            "entry": snapshot["entry"],
            "command": dataclasses.asdict(TextureEditorCommand(kind="checkpoint", label=label, timestamp=time.time(), checkpoint=True)),
            "checkpoint": snapshot,
            "floating_pixels": None if self._floating_pixels is None else self._encode_rgba_blob(self._floating_pixels),
        }

    def _record_history_change(
        self,
        label: str,
        *,
        before_document: TextureEditorDocument,
        before_layer_pixels: Dict[str, np.ndarray],
        kind: str,
        dirty_bounds: Optional[Tuple[int, int, int, int]] = None,
        tracked_layer_ids: Optional[Sequence[str]] = None,
        force_checkpoint: bool = False,
        before_floating_pixels: Optional[np.ndarray] = None,
    ) -> None:
        if self.document is None:
            return
        if self.history_index < len(self.history_snapshots) - 1:
            self.history_snapshots = self.history_snapshots[: self.history_index + 1]
        checkpoint = force_checkpoint or not self.history_snapshots or ((len(self.history_snapshots) + 1) % 20 == 0)
        if checkpoint:
            record = self._build_checkpoint_record(label)
        else:
            tracked_ids = set(tracked_layer_ids) if tracked_layer_ids is not None else set()
            if tracked_layer_ids is None:
                tracked_ids = {layer.layer_id for layer in before_document.layers}
                tracked_ids.update(layer.layer_id for layer in self.document.layers)
                tracked_ids.update(self._history_auxiliary_layer_ids(before_document))
                tracked_ids.update(self._history_auxiliary_layer_ids(self.document))
            before_blobs: Dict[str, object] = {}
            after_blobs: Dict[str, object] = {}
            for layer_id in tracked_ids:
                before_pixels = before_layer_pixels.get(layer_id)
                after_pixels = self.layer_pixels.get(layer_id)
                if before_pixels is not None and after_pixels is not None and before_pixels.shape == after_pixels.shape and np.array_equal(before_pixels, after_pixels):
                    continue
                before_payload = self._encode_history_layer_state(
                    before_document,
                    layer_id,
                    before_pixels,
                    dirty_bounds=dirty_bounds,
                    previous_pixels=after_pixels,
                )
                after_payload = self._encode_history_layer_state(
                    self.document,
                    layer_id,
                    after_pixels,
                    dirty_bounds=dirty_bounds,
                    previous_pixels=before_pixels,
                )
                if before_payload is None and after_payload is None:
                    continue
                before_blobs[layer_id] = before_payload
                after_blobs[layer_id] = after_payload
            command = TextureEditorCommand(
                kind=kind,
                label=label,
                timestamp=time.time(),
                dirty_bounds=dirty_bounds,
                checkpoint=False,
            )
            record = {
                "entry": TextureEditorHistoryEntry(label=label, timestamp=command.timestamp),
                "command": dataclasses.asdict(command),
                "before_document": dataclasses.replace(before_document),
                "after_document": dataclasses.replace(self.document),
                "before_layers": before_blobs,
                "after_layers": after_blobs,
                "before_floating_pixels": None if before_floating_pixels is None else self._encode_rgba_blob(before_floating_pixels),
                "after_floating_pixels": None if self._floating_pixels is None else self._encode_rgba_blob(self._floating_pixels),
            }
        self.history_snapshots.append(record)
        if len(self.history_snapshots) > 100:
            self.history_snapshots.pop(0)
        self.history_index = len(self.history_snapshots) - 1
        self._refresh_history_list()

    def _push_history(self, label: str) -> None:
        if self.document is None:
            return
        if self.history_index < len(self.history_snapshots) - 1:
            self.history_snapshots = self.history_snapshots[: self.history_index + 1]
        self.history_snapshots.append(self._build_checkpoint_record(label))
        if len(self.history_snapshots) > 100:
            self.history_snapshots.pop(0)
        self.history_index = len(self.history_snapshots) - 1
        self._refresh_history_list()

    def _apply_history_document_state(
        self,
        document: TextureEditorDocument,
        layer_blobs: Dict[str, object],
    ) -> None:
        current_pixels = dict(self.layer_pixels)
        target_ids = {layer.layer_id for layer in document.layers}
        target_ids.update(self._history_auxiliary_layer_ids(document))
        new_pixels: Dict[str, np.ndarray] = {}
        for layer_id in target_ids:
            if layer_id in current_pixels:
                new_pixels[layer_id] = current_pixels[layer_id]
        for layer_id, blob in layer_blobs.items():
            if blob is None:
                new_pixels.pop(layer_id, None)
                continue
            decoded = self._decode_history_layer_state(new_pixels.get(layer_id), blob)
            if decoded is not None:
                new_pixels[layer_id] = decoded
        self.document = document
        self.layer_pixels = new_pixels
        self._floating_pixels = None
        self._floating_mask = None
        self._invalidate_composite_cache()

    def _apply_history_record(self, record: Dict[str, object], *, direction: str) -> None:
        checkpoint = record.get("checkpoint")
        if checkpoint is not None:
            document, layer_pixels, _entry = restore_texture_editor_snapshot(checkpoint)
            self.document = document
            self.layer_pixels = layer_pixels
            floating_blob = record.get("floating_pixels")
            self._floating_pixels = self._decode_rgba_blob(floating_blob) if floating_blob else None
            self._floating_mask = None if self._floating_pixels is None else self._floating_pixels[..., 3].copy()
            return
        if direction == "before":
            document = dataclasses.replace(record["before_document"])  # type: ignore[arg-type]
            layer_blobs = record.get("before_layers") or {}
            floating_blob = record.get("before_floating_pixels")
        else:
            document = dataclasses.replace(record["after_document"])  # type: ignore[arg-type]
            layer_blobs = record.get("after_layers") or {}
            floating_blob = record.get("after_floating_pixels")
        self._apply_history_document_state(document, layer_blobs)
        self._floating_pixels = self._decode_rgba_blob(floating_blob) if floating_blob else None
        self._floating_mask = None if self._floating_pixels is None else self._floating_pixels[..., 3].copy()

    def _restore_history_index(self, index: int) -> None:
        if index < 0 or index >= len(self.history_snapshots):
            return
        checkpoint_index = index
        while checkpoint_index >= 0 and "checkpoint" not in self.history_snapshots[checkpoint_index]:
            checkpoint_index -= 1
        if checkpoint_index >= 0:
            self._apply_history_record(self.history_snapshots[checkpoint_index], direction="after")
            replay_start = checkpoint_index + 1
        else:
            replay_start = 0
        for replay_index in range(replay_start, index + 1):
            record = self.history_snapshots[replay_index]
            if "checkpoint" in record:
                if replay_index != checkpoint_index:
                    self._apply_history_record(record, direction="after")
                continue
            self._apply_history_record(record, direction="after")
        self.history_index = index
        self._layer_property_dirty = False
        self._adjustment_property_dirty = False
        self._pending_adjustment_before_document = None
        self._invalidate_composite_cache()
        self._refresh_ui()
        self._set_status(f"Restored history step: {self.history_snapshots[index]['entry'].label}.", False)

    def undo(self) -> None:
        if self.history_index <= 0:
            return
        self._restore_history_index(self.history_index - 1)

    def redo(self) -> None:
        if self.history_index >= len(self.history_snapshots) - 1:
            return
        self._restore_history_index(self.history_index + 1)

    def _refresh_history_list(self) -> None:
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for index, snapshot in enumerate(self.history_snapshots):
            entry = snapshot["entry"]
            item = QListWidgetItem(entry.label)
            item.setData(Qt.UserRole, index)
            if index == self.history_index:
                item.setText(f"{entry.label} (current)")
            self.history_list.addItem(item)
        if 0 <= self.history_index < self.history_list.count():
            self.history_list.setCurrentRow(self.history_index)
        self.history_list.blockSignals(False)
        self._update_history_action_state()

    def _handle_history_row_changed(self, row: int) -> None:
        self._update_history_action_state()
        if row < 0 or row == self.history_index:
            return
        snapshot = self.history_snapshots[row]
        entry = snapshot["entry"]
        self._set_status(
            f"Selected history step '{entry.label}'. Double-click or use Restore Selected to jump to it.",
            False,
        )

    def _update_history_action_state(self) -> None:
        selected_row = self.history_list.currentRow()
        self.history_restore_button.setEnabled(
            self.document is not None
            and not self._busy()
            and 0 <= selected_row < len(self.history_snapshots)
            and selected_row != self.history_index
        )

    def restore_selected_history(self) -> None:
        row = self.history_list.currentRow()
        if row < 0 or row == self.history_index:
            return
        self._restore_history_index(row)

    def clear_history(self) -> None:
        if self.document is None:
            return
        current_label = "Current State"
        self.history_snapshots = [self._build_checkpoint_record(current_label)]
        self.history_index = 0
        self._adjustment_property_dirty = False
        self._pending_adjustment_before_document = None
        self._refresh_history_list()
        self._set_status("Texture Editor history cleared. Current state kept as the new baseline.", False)

    def _current_layer_id(self) -> Optional[str]:
        item = self.layers_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.UserRole)
        return str(value) if value else None

    def _current_layer_pixels(self) -> Optional[np.ndarray]:
        layer_id = self._current_layer_id()
        if not layer_id:
            return None
        return self.layer_pixels.get(layer_id)

    def _current_edit_target_layer_id(self) -> Optional[str]:
        if self.document is None:
            return None
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id:
            return None
        if not self._editing_mask_target:
            return layer_id
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None or not layer.mask_layer_id:
            return layer_id
        return layer.mask_layer_id

    def _layer_canvas_bounds(self, layer_id: str) -> Optional[Tuple[int, int, int, int]]:
        if self.document is None or layer_id not in self.layer_pixels:
            return None
        pixels = self.layer_pixels[layer_id]
        offset_x, offset_y = self._history_layer_canvas_offset(self.document, layer_id)
        return (int(offset_x), int(offset_y), int(pixels.shape[1]), int(pixels.shape[0]))

    def _estimated_brush_dirty_bounds(
        self,
        points: Sequence[Tuple[int, int]],
        *,
        padding: Optional[int] = None,
    ) -> Optional[Tuple[int, int, int, int]]:
        if self.document is None or not points:
            return None
        brush_padding = padding if padding is not None else int(math.ceil(max(1.0, float(self.current_tool_settings.size)) * 0.75)) + 4
        xs = [int(point[0]) for point in points]
        ys = [int(point[1]) for point in points]
        x0 = max(0, min(xs) - brush_padding)
        y0 = max(0, min(ys) - brush_padding)
        x1 = min(int(self.document.width), max(xs) + brush_padding + 1)
        y1 = min(int(self.document.height), max(ys) + brush_padding + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def float_active_layer_copy(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        pixels = self.layer_pixels[layer_id]
        alpha = pixels[..., 3]
        ys, xs = np.where(alpha > 0)
        if xs.size > 0 and ys.size > 0:
            x0 = int(xs.min())
            y0 = int(ys.min())
            x1 = int(xs.max()) + 1
            y1 = int(ys.max()) + 1
        else:
            x0 = 0
            y0 = 0
            x1 = int(pixels.shape[1])
            y1 = int(pixels.shape[0])
        extracted = pixels[y0:y1, x0:x1].copy()
        if extracted.size == 0:
            self._set_status("The active layer does not contain any pixels to float.", True)
            return
        before_document = dataclasses.replace(self.document)
        before_floating_pixels = self._snapshot_floating_pixels()
        target_bounds = (int(layer.offset_x + x0), int(layer.offset_y + y0), int(extracted.shape[1]), int(extracted.shape[0]))
        self._set_floating_selection(
            extracted,
            label=f"{layer.name} Copy",
            bounds=target_bounds,
            source_layer_id=layer_id,
            paste_mode="in_place",
        )
        self._record_history_change(
            "Float Active Layer Copy",
            before_document=before_document,
            before_layer_pixels={},
            kind="floating_create",
            tracked_layer_ids=[],
            dirty_bounds=target_bounds,
            before_floating_pixels=before_floating_pixels,
        )
        self._set_active_tool("move")
        self._refresh_ui()
        self._set_status(f"Floating copy created from '{layer.name}'.", False)

    def apply_floating_transform(self) -> None:
        if self.document is None or self.document.floating_selection is None:
            return
        before_document = dataclasses.replace(self.document)
        before_floating_pixels = self._snapshot_floating_pixels()
        floating = self.document.floating_selection
        self.document = dataclasses.replace(
            self.document,
            floating_selection=dataclasses.replace(
                floating,
                scale_x=max(0.1, self.transform_scale_spin.value() / 100.0),
                scale_y=max(0.1, self.transform_scale_spin.value() / 100.0),
                rotation_degrees=float(self.transform_rotation_spin.value()),
                committed=False,
            ),
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Transform Floating Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="floating_transform",
            tracked_layer_ids=[],
            before_floating_pixels=before_floating_pixels,
        )
        self._refresh_ui()

    def flip_floating_selection(self, flip_x: bool, flip_y: bool) -> None:
        if self.document is None or self.document.floating_selection is None:
            return
        before_document = dataclasses.replace(self.document)
        before_floating_pixels = self._snapshot_floating_pixels()
        floating = self.document.floating_selection
        self.document = dataclasses.replace(
            self.document,
            floating_selection=dataclasses.replace(
                floating,
                flip_x=(not floating.flip_x) if flip_x else floating.flip_x,
                flip_y=(not floating.flip_y) if flip_y else floating.flip_y,
                committed=False,
            ),
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Flip Floating Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="floating_transform",
            tracked_layer_ids=[],
            before_floating_pixels=before_floating_pixels,
        )
        self._refresh_ui()

    def rotate_floating_selection(self, degrees: int) -> None:
        if self.document is None or self.document.floating_selection is None:
            return
        before_document = dataclasses.replace(self.document)
        before_floating_pixels = self._snapshot_floating_pixels()
        floating = self.document.floating_selection
        self.document = dataclasses.replace(
            self.document,
            floating_selection=dataclasses.replace(
                floating,
                rotation_degrees=float(((floating.rotation_degrees + degrees + 180) % 360) - 180),
                committed=False,
            ),
        )
        self.transform_rotation_spin.blockSignals(True)
        self.transform_rotation_spin.setValue(int(round(self.document.floating_selection.rotation_degrees)))
        self.transform_rotation_spin.blockSignals(False)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Rotate Floating Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="floating_transform",
            tracked_layer_ids=[],
            before_floating_pixels=before_floating_pixels,
        )
        self._refresh_ui()

    def _current_floating_canvas_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        if self.document is None or self.document.floating_selection is None or self._floating_pixels is None:
            return None
        transformed = self._transformed_floating_pixels()
        if transformed is None:
            return None
        floating = self.document.floating_selection
        return (
            int(floating.bounds[0] + floating.offset_x),
            int(floating.bounds[1] + floating.offset_y),
            int(transformed.shape[1]),
            int(transformed.shape[0]),
        )

    def _handle_canvas_floating_transform(self, payload: object) -> None:
        if self.document is None or self.document.floating_selection is None or not isinstance(payload, dict):
            return
        floating = self.document.floating_selection
        next_offset_x = int(payload.get("offset_x", floating.offset_x))
        next_offset_y = int(payload.get("offset_y", floating.offset_y))
        next_scale_x = max(0.05, float(payload.get("scale_x", floating.scale_x)))
        next_scale_y = max(0.05, float(payload.get("scale_y", floating.scale_y)))
        next_rotation = float(payload.get("rotation_degrees", floating.rotation_degrees))
        commit = bool(payload.get("commit", False))
        mode = str(payload.get("mode", "move") or "move")
        if (
            next_offset_x == int(floating.offset_x)
            and next_offset_y == int(floating.offset_y)
            and abs(next_scale_x - float(floating.scale_x)) < 1e-6
            and abs(next_scale_y - float(floating.scale_y)) < 1e-6
            and abs(next_rotation - float(floating.rotation_degrees)) < 1e-6
        ):
            if commit:
                self._floating_transform_before_document = None
                self._floating_transform_before_floating_pixels = None
                self._floating_transform_label = ""
            return
        if self._floating_transform_before_document is None:
            self._floating_transform_before_document = dataclasses.replace(self.document)
            self._floating_transform_before_floating_pixels = self._snapshot_floating_pixels()
            self._floating_transform_label = {
                "move": "Move Floating Selection",
                "rotate": "Rotate Floating Selection",
                "scale_nw": "Scale Floating Selection",
                "scale_ne": "Scale Floating Selection",
                "scale_sw": "Scale Floating Selection",
                "scale_se": "Scale Floating Selection",
            }.get(mode, "Transform Floating Selection")
        before_bounds = self._current_floating_canvas_bounds()
        self.document = dataclasses.replace(
            self.document,
            floating_selection=dataclasses.replace(
                floating,
                offset_x=next_offset_x,
                offset_y=next_offset_y,
                scale_x=next_scale_x,
                scale_y=next_scale_y,
                rotation_degrees=next_rotation,
                committed=False,
            ),
        )
        after_bounds = self._current_floating_canvas_bounds()
        dirty_bounds = None
        if before_bounds is not None and after_bounds is not None:
            x0 = min(before_bounds[0], after_bounds[0])
            y0 = min(before_bounds[1], after_bounds[1])
            x1 = max(before_bounds[0] + before_bounds[2], after_bounds[0] + after_bounds[2])
            y1 = max(before_bounds[1] + before_bounds[3], after_bounds[1] + after_bounds[3])
            dirty_bounds = (int(x0), int(y0), max(1, int(x1 - x0)), max(1, int(y1 - y0)))
        self._invalidate_composite_cache(dirty_bounds)
        if commit and self._floating_transform_before_document is not None:
            self._record_history_change(
                self._floating_transform_label or "Transform Floating Selection",
                before_document=self._floating_transform_before_document,
                before_layer_pixels={},
                kind="floating_transform",
                tracked_layer_ids=[],
                dirty_bounds=dirty_bounds,
                before_floating_pixels=self._floating_transform_before_floating_pixels,
            )
            self._floating_transform_before_document = None
            self._floating_transform_before_floating_pixels = None
            self._floating_transform_label = ""
            self._set_status("Updated floating selection on the canvas.", False)
            self._refresh_editor_views(
                canvas=True,
                history=True,
                transform=True,
                status=True,
                tool_visibility=False,
            )
        else:
            self._refresh_editor_views(
                canvas=True,
                transform=True,
                status=True,
                tool_visibility=False,
            )

    def _apply_document_pixels_change(
        self,
        label: str,
        apply_change: Callable[[], Tuple[TextureEditorDocument, Dict[str, np.ndarray]]],
    ) -> None:
        if self.document is None:
            return
        if self.document.floating_selection is not None:
            self._set_status("Commit or cancel the floating selection before changing the whole document.", True)
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {key: value.copy() for key, value in self.layer_pixels.items()}
        updated_document, updated_pixels = apply_change()
        if updated_document is self.document and updated_pixels is self.layer_pixels:
            return
        self.document = updated_document
        self.layer_pixels = updated_pixels
        self._thumbnail_cache = {}
        self._invalidate_composite_cache()
        self._record_history_change(
            label,
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="document_transform",
            tracked_layer_ids=[],
            force_checkpoint=True,
        )
        self._refresh_ui()
        self._set_status(f"{label} applied.", False)

    def crop_document_to_selection(self) -> None:
        if self.document is None or self.document.selection.mode == "none":
            self._set_status("Create a selection first, then use Crop To Selection.", True)
            return
        self._apply_document_pixels_change(
            "Crop To Selection",
            lambda: crop_texture_editor_document_to_selection(self.document, self.layer_pixels),
        )

    def trim_document_transparent(self) -> None:
        self._apply_document_pixels_change(
            "Trim Transparent",
            lambda: trim_texture_editor_document_transparent_bounds(self.document, self.layer_pixels),
        )

    def flip_document(self, horizontal: bool, vertical: bool) -> None:
        if not horizontal and not vertical:
            return
        label = "Flip Horizontal" if horizontal else "Flip Vertical"
        self._apply_document_pixels_change(
            label,
            lambda: flip_texture_editor_document(self.document, self.layer_pixels, horizontal=horizontal, vertical=vertical),
        )

    def rotate_document_90(self, clockwise: bool) -> None:
        label = "Rotate 90 CW" if clockwise else "Rotate 90 CCW"
        self._apply_document_pixels_change(
            label,
            lambda: rotate_texture_editor_document_90(self.document, self.layer_pixels, clockwise=clockwise),
        )

    def add_mask_to_selected_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels, _mask_id = create_texture_editor_layer_mask(self.document, self.layer_pixels, layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Add Layer Mask",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="mask_update",
            force_checkpoint=True,
        )
        self._refresh_ui()

    def invert_selected_layer_mask(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None or not layer.mask_layer_id or layer.mask_layer_id not in self.layer_pixels:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer.mask_layer_id: self.layer_pixels[layer.mask_layer_id].copy()}
        self.layer_pixels = invert_texture_editor_layer_mask(self.document, self.layer_pixels, layer_id)
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Invert Layer Mask",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="mask_update",
            tracked_layer_ids=[layer.mask_layer_id],
        )
        self._refresh_ui()

    def delete_selected_layer_mask(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels = delete_texture_editor_layer_mask(self.document, self.layer_pixels, layer_id)
        self._editing_mask_target = False
        self._invalidate_composite_cache()
        self._record_history_change(
            "Delete Layer Mask",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="mask_update",
            force_checkpoint=True,
        )
        self._refresh_ui()

    def toggle_selected_layer_mask_enabled(self, checked: bool) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        self.document = set_texture_editor_layer_mask_enabled(self.document, layer_id, checked)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Toggle Layer Mask",
            before_document=before_document,
            before_layer_pixels={},
            kind="mask_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def toggle_edit_mask_target(self, checked: bool) -> None:
        if checked:
            layer_id = self._current_layer_id()
            layer = None if self.document is None or not layer_id else next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
            if layer is None or not layer.mask_layer_id or layer.mask_layer_id not in self.layer_pixels:
                self._editing_mask_target = False
                self.layer_edit_mask_checkbox.blockSignals(True)
                self.layer_edit_mask_checkbox.setChecked(False)
                self.layer_edit_mask_checkbox.blockSignals(False)
                self._set_status("Add a layer mask before switching the editor into mask paint mode.", True)
                return
        self._editing_mask_target = bool(checked)
        if self._editing_mask_target:
            self._set_status("Editing active layer mask. Paint/erase and other brush tools will target the mask.", False)
        else:
            self._set_status(self._tool_status_text(self.current_tool_settings.tool), False)
        self._refresh_ui()

    def add_adjustment_layer(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment_type = str(self.adjustment_add_combo.currentData() or "levels")
        before_document = dataclasses.replace(self.document)
        display_names = {
            "hue_saturation": "Hue / Saturation",
            "vibrance": "Vibrance",
            "selective_color": "Selective Color",
            "brightness_contrast": "Brightness / Contrast",
            "exposure": "Exposure",
            "color_balance": "Color Balance",
            "levels": "Levels",
            "curves": "Curves",
        }
        self.document = add_texture_editor_adjustment_layer(
            self.document,
            adjustment_type=adjustment_type,
            name=display_names.get(adjustment_type, "Adjustment"),
            parameters=self._default_adjustment_parameters(adjustment_type),
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Add Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()
        if self.document.adjustment_layers:
            self._refresh_adjustments(preserve_selection_id=self.document.adjustment_layers[-1].layer_id)

    def _default_adjustment_parameters(self, adjustment_type: str) -> Dict[str, object]:
        adjustment_key = (adjustment_type or "levels").strip().lower()
        if adjustment_key == "hue_saturation":
            return {"hue": 0.0, "saturation": 0.0, "lightness": 0.0}
        if adjustment_key == "vibrance":
            return {"vibrance": 0.0, "saturation": 0.0, "lightness": 0.0}
        if adjustment_key == "selective_color":
            return {
                "target_range": "neutrals",
                "red_cyan": 0.0,
                "green_magenta": 0.0,
                "blue_yellow": 0.0,
            }
        if adjustment_key == "brightness_contrast":
            return {"brightness": 0.0, "contrast": 0.0, "saturation": 0.0}
        if adjustment_key == "exposure":
            return {"exposure": 0.0, "offset": 0.0, "gamma": 1.0}
        if adjustment_key == "color_balance":
            return {"red_cyan": 0.0, "green_magenta": 0.0, "blue_yellow": 0.0}
        if adjustment_key == "curves":
            return {"shadows": 0.0, "midtones": 0.0, "highlights": 0.0}
        return {"black": 0.0, "gamma": 1.0, "white": 255.0, "output_black": 0.0, "output_white": 255.0}

    def _adjustment_list_label(self, adjustment: TextureEditorAdjustmentLayer) -> str:
        prefix = "[On]" if adjustment.enabled else "[Off]"
        mask_suffix = "  Mask" if adjustment.mask_layer_id else ""
        if adjustment.adjustment_type == "hue_saturation":
            hue = int(round(float(adjustment.parameters.get("hue", 0.0))))
            sat = int(round(float(adjustment.parameters.get("saturation", 0.0))))
            light = int(round(float(adjustment.parameters.get("lightness", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  H:{hue:+d} S:{sat:+d} L:{light:+d}"
        if adjustment.adjustment_type == "vibrance":
            vibrance = int(round(float(adjustment.parameters.get("vibrance", 0.0))))
            sat = int(round(float(adjustment.parameters.get("saturation", 0.0))))
            light = int(round(float(adjustment.parameters.get("lightness", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  Vib:{vibrance:+d} S:{sat:+d} L:{light:+d}"
        if adjustment.adjustment_type == "selective_color":
            target = str(adjustment.parameters.get("target_range", "neutrals") or "neutrals").title()
            red = int(round(float(adjustment.parameters.get("red_cyan", 0.0))))
            green = int(round(float(adjustment.parameters.get("green_magenta", 0.0))))
            blue = int(round(float(adjustment.parameters.get("blue_yellow", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  {target}  R:{red:+d} G:{green:+d} B:{blue:+d}"
        if adjustment.adjustment_type == "brightness_contrast":
            brightness = int(round(float(adjustment.parameters.get("brightness", 0.0))))
            contrast = int(round(float(adjustment.parameters.get("contrast", 0.0))))
            saturation = int(round(float(adjustment.parameters.get("saturation", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  Br:{brightness:+d} Ct:{contrast:+d} Sat:{saturation:+d}"
        if adjustment.adjustment_type == "exposure":
            exposure = int(round(float(adjustment.parameters.get("exposure", 0.0))))
            offset = int(round(float(adjustment.parameters.get("offset", 0.0))))
            gamma = float(adjustment.parameters.get("gamma", 1.0))
            return f"{prefix} {adjustment.name}{mask_suffix}  Exp:{exposure:+d} Off:{offset:+d} G:{gamma:.2f}"
        if adjustment.adjustment_type == "color_balance":
            red = int(round(float(adjustment.parameters.get("red_cyan", 0.0))))
            green = int(round(float(adjustment.parameters.get("green_magenta", 0.0))))
            blue = int(round(float(adjustment.parameters.get("blue_yellow", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  R:{red:+d} G:{green:+d} B:{blue:+d}"
        if adjustment.adjustment_type == "curves":
            shadows = int(round(float(adjustment.parameters.get("shadows", 0.0))))
            mids = int(round(float(adjustment.parameters.get("midtones", 0.0))))
            highs = int(round(float(adjustment.parameters.get("highlights", 0.0))))
            return f"{prefix} {adjustment.name}{mask_suffix}  Sh:{shadows:+d} Mid:{mids:+d} Hi:{highs:+d}"
        black = int(round(float(adjustment.parameters.get("black", 0.0))))
        gamma = float(adjustment.parameters.get("gamma", 1.0))
        white = int(round(float(adjustment.parameters.get("white", 255.0))))
        return f"{prefix} {adjustment.name}{mask_suffix}  B:{black} G:{gamma:.2f} W:{white}"

    def _current_adjustment_id(self) -> Optional[str]:
        item = self.adjustments_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.UserRole)
        return str(value) if value else None

    def remove_selected_adjustment(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment_id = self._current_adjustment_id()
        if not adjustment_id:
            return
        before_document = dataclasses.replace(self.document)
        self.document = remove_texture_editor_adjustment_layer(self.document, adjustment_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Remove Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def duplicate_selected_adjustment(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment = self._selected_adjustment()
        if adjustment is None:
            return
        before_document = dataclasses.replace(self.document)
        duplicated_document = add_texture_editor_adjustment_layer(
            self.document,
            adjustment_type=adjustment.adjustment_type,
            name=f"{adjustment.name} Copy",
            parameters=dict(adjustment.parameters),
        )
        duplicated_adjustment = duplicated_document.adjustment_layers[-1]
        self.document = update_texture_editor_adjustment_layer(
            duplicated_document,
            duplicated_adjustment.layer_id,
            enabled=adjustment.enabled,
            opacity=adjustment.opacity,
            parameters=dict(adjustment.parameters),
            mask_layer_id=adjustment.mask_layer_id,
            name=f"{adjustment.name} Copy",
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Duplicate Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=duplicated_adjustment.layer_id)

    def move_selected_adjustment(self, direction: int) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment_id = self._current_adjustment_id()
        if not adjustment_id:
            return
        layers = list(self.document.adjustment_layers)
        current_index = next((index for index, layer in enumerate(layers) if layer.layer_id == adjustment_id), -1)
        if current_index < 0:
            return
        target_index = max(0, min(len(layers) - 1, current_index + int(direction)))
        if target_index == current_index:
            return
        before_document = dataclasses.replace(self.document)
        layer = layers.pop(current_index)
        layers.insert(target_index, dataclasses.replace(layer, revision=int(layer.revision) + 1))
        self.document = dataclasses.replace(
            self.document,
            adjustment_layers=tuple(layers),
            composite_revision=int(self.document.composite_revision) + 1,
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Move Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=adjustment_id)

    def solo_selected_adjustment(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment_id = self._current_adjustment_id()
        if not adjustment_id:
            return
        before_document = dataclasses.replace(self.document)
        updated_layers: List[TextureEditorAdjustmentLayer] = []
        for adjustment in self.document.adjustment_layers:
            updated_layers.append(
                dataclasses.replace(
                    adjustment,
                    enabled=(adjustment.layer_id == adjustment_id),
                    revision=int(adjustment.revision) + (1 if adjustment.enabled != (adjustment.layer_id == adjustment_id) else 0),
                )
            )
        self.document = dataclasses.replace(
            self.document,
            adjustment_layers=tuple(updated_layers),
            composite_revision=int(self.document.composite_revision) + 1,
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Solo Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=adjustment_id)
        self._set_status("Soloed the selected adjustment. Re-enable others manually if needed.", False)

    def use_active_layer_as_adjustment_mask(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment = self._selected_adjustment()
        active_layer_id = self._current_layer_id()
        if adjustment is None or not active_layer_id:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_adjustment_layer(
            self.document,
            adjustment.layer_id,
            mask_layer_id=active_layer_id,
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Assign Adjustment Mask",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=adjustment.layer_id)
        self._set_status("Assigned the active raster layer as the selected adjustment mask.", False)

    def clear_selected_adjustment_mask(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment = self._selected_adjustment()
        if adjustment is None or not adjustment.mask_layer_id:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_adjustment_layer(
            self.document,
            adjustment.layer_id,
            mask_layer_id="",
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Clear Adjustment Mask",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=adjustment.layer_id)

    def _selected_adjustment(self) -> Optional[TextureEditorAdjustmentLayer]:
        if self.document is None:
            return None
        adjustment_id = self._current_adjustment_id()
        if not adjustment_id:
            return None
        return next((layer for layer in self.document.adjustment_layers if layer.layer_id == adjustment_id), None)

    def _handle_adjustment_selection_changed(self) -> None:
        if self._refreshing_adjustments:
            return
        if self._adjustment_preview_timer.isActive():
            self._adjustment_preview_timer.stop()
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment = self._selected_adjustment()
        has_adjustment = adjustment is not None
        self.adjustment_enabled_checkbox.blockSignals(True)
        self.adjustment_opacity_slider.blockSignals(True)
        self.adjustment_mode_combo.blockSignals(True)
        self.adjustment_param_a_slider.blockSignals(True)
        self.adjustment_param_b_slider.blockSignals(True)
        self.adjustment_param_c_slider.blockSignals(True)
        if adjustment is None:
            self.adjustment_mode_label.setVisible(False)
            self.adjustment_mode_combo.setVisible(False)
            self.adjustment_enabled_checkbox.setChecked(False)
            self.adjustment_opacity_slider.setValue(100)
            self.adjustment_mode_combo.setCurrentIndex(max(0, self.adjustment_mode_combo.findData("neutrals")))
            self.adjustment_param_a_slider.setValue(0)
            self.adjustment_param_b_slider.setValue(0)
            self.adjustment_param_c_slider.setValue(0)
        else:
            self.adjustment_enabled_checkbox.setChecked(adjustment.enabled)
            self.adjustment_opacity_slider.setValue(adjustment.opacity)
            if adjustment.adjustment_type == "hue_saturation":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Hue")
                self.adjustment_param_b_label.setText("Saturation")
                self.adjustment_param_c_label.setText("Lightness")
                self.adjustment_param_a_slider.setRange(-180, 180)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("hue", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("saturation", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("lightness", 0.0))))
            elif adjustment.adjustment_type == "vibrance":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Vibrance")
                self.adjustment_param_b_label.setText("Saturation")
                self.adjustment_param_c_label.setText("Lightness")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("vibrance", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("saturation", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("lightness", 0.0))))
            elif adjustment.adjustment_type == "selective_color":
                self.adjustment_mode_label.setVisible(True)
                self.adjustment_mode_combo.setVisible(True)
                target_range = str(adjustment.parameters.get("target_range", "neutrals") or "neutrals")
                mode_index = self.adjustment_mode_combo.findData(target_range)
                if mode_index >= 0:
                    self.adjustment_mode_combo.setCurrentIndex(mode_index)
                self.adjustment_param_a_label.setText("Red / Cyan")
                self.adjustment_param_b_label.setText("Green / Magenta")
                self.adjustment_param_c_label.setText("Blue / Yellow")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("red_cyan", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("green_magenta", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("blue_yellow", 0.0))))
            elif adjustment.adjustment_type == "brightness_contrast":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Brightness")
                self.adjustment_param_b_label.setText("Contrast")
                self.adjustment_param_c_label.setText("Saturation")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("brightness", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("contrast", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("saturation", 0.0))))
            elif adjustment.adjustment_type == "exposure":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Exposure")
                self.adjustment_param_b_label.setText("Offset")
                self.adjustment_param_c_label.setText("Gamma x100")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(10, 300)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("exposure", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("offset", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("gamma", 1.0) * 100.0)))
            elif adjustment.adjustment_type == "color_balance":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Red / Cyan")
                self.adjustment_param_b_label.setText("Green / Magenta")
                self.adjustment_param_c_label.setText("Blue / Yellow")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("red_cyan", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("green_magenta", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("blue_yellow", 0.0))))
            elif adjustment.adjustment_type == "curves":
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Shadows")
                self.adjustment_param_b_label.setText("Midtones")
                self.adjustment_param_c_label.setText("Highlights")
                self.adjustment_param_a_slider.setRange(-100, 100)
                self.adjustment_param_b_slider.setRange(-100, 100)
                self.adjustment_param_c_slider.setRange(-100, 100)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("shadows", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("midtones", 0.0))))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("highlights", 0.0))))
            else:
                self.adjustment_mode_label.setVisible(False)
                self.adjustment_mode_combo.setVisible(False)
                self.adjustment_param_a_label.setText("Black")
                self.adjustment_param_b_label.setText("Gamma x100")
                self.adjustment_param_c_label.setText("White")
                self.adjustment_param_a_slider.setRange(0, 254)
                self.adjustment_param_b_slider.setRange(10, 400)
                self.adjustment_param_c_slider.setRange(1, 255)
                self.adjustment_param_a_slider.setValue(int(round(adjustment.parameters.get("black", 0.0))))
                self.adjustment_param_b_slider.setValue(int(round(adjustment.parameters.get("gamma", 1.0) * 100.0)))
                self.adjustment_param_c_slider.setValue(int(round(adjustment.parameters.get("white", 255.0))))
        self.adjustment_enabled_checkbox.blockSignals(False)
        self.adjustment_opacity_slider.blockSignals(False)
        self.adjustment_mode_combo.blockSignals(False)
        self.adjustment_param_a_slider.blockSignals(False)
        self.adjustment_param_b_slider.blockSignals(False)
        self.adjustment_param_c_slider.blockSignals(False)
        self.adjustment_enabled_checkbox.setEnabled(has_adjustment)
        self.adjustment_opacity_slider.setEnabled(has_adjustment)
        self.adjustment_mode_label.setEnabled(has_adjustment)
        self.adjustment_mode_combo.setEnabled(has_adjustment and adjustment is not None and adjustment.adjustment_type == "selective_color")
        self.adjustment_param_a_slider.setEnabled(has_adjustment)
        self.adjustment_param_b_slider.setEnabled(has_adjustment)
        self.adjustment_param_c_slider.setEnabled(has_adjustment)

    def _schedule_adjustment_preview(self) -> None:
        if self.document is None:
            return
        self._adjustment_preview_timer.start()

    def preview_selected_adjustment_properties(self) -> None:
        if self.document is None:
            return
        adjustment = self._selected_adjustment()
        if adjustment is None:
            return
        if not self._adjustment_property_dirty:
            self._pending_adjustment_before_document = dataclasses.replace(self.document)
        before_document = self._pending_adjustment_before_document or dataclasses.replace(self.document)
        if adjustment.adjustment_type == "hue_saturation":
            parameters = {
                "hue": float(self.adjustment_param_a_slider.value()),
                "saturation": float(self.adjustment_param_b_slider.value()),
                "lightness": float(self.adjustment_param_c_slider.value()),
            }
        elif adjustment.adjustment_type == "vibrance":
            parameters = {
                "vibrance": float(self.adjustment_param_a_slider.value()),
                "saturation": float(self.adjustment_param_b_slider.value()),
                "lightness": float(self.adjustment_param_c_slider.value()),
            }
        elif adjustment.adjustment_type == "selective_color":
            parameters = {
                "target_range": str(self.adjustment_mode_combo.currentData() or "neutrals"),
                "red_cyan": float(self.adjustment_param_a_slider.value()),
                "green_magenta": float(self.adjustment_param_b_slider.value()),
                "blue_yellow": float(self.adjustment_param_c_slider.value()),
            }
        elif adjustment.adjustment_type == "brightness_contrast":
            parameters = {
                "brightness": float(self.adjustment_param_a_slider.value()),
                "contrast": float(self.adjustment_param_b_slider.value()),
                "saturation": float(self.adjustment_param_c_slider.value()),
            }
        elif adjustment.adjustment_type == "exposure":
            parameters = {
                "exposure": float(self.adjustment_param_a_slider.value()),
                "offset": float(self.adjustment_param_b_slider.value()),
                "gamma": float(self.adjustment_param_c_slider.value()) / 100.0,
            }
        elif adjustment.adjustment_type == "color_balance":
            parameters = {
                "red_cyan": float(self.adjustment_param_a_slider.value()),
                "green_magenta": float(self.adjustment_param_b_slider.value()),
                "blue_yellow": float(self.adjustment_param_c_slider.value()),
            }
        elif adjustment.adjustment_type == "curves":
            parameters = {
                "shadows": float(self.adjustment_param_a_slider.value()),
                "midtones": float(self.adjustment_param_b_slider.value()),
                "highlights": float(self.adjustment_param_c_slider.value()),
            }
        else:
            parameters = {
                "black": float(self.adjustment_param_a_slider.value()),
                "gamma": float(self.adjustment_param_b_slider.value()) / 100.0,
                "white": float(self.adjustment_param_c_slider.value()),
            }
        self.document = update_texture_editor_adjustment_layer(
            self.document,
            adjustment.layer_id,
            enabled=self.adjustment_enabled_checkbox.isChecked(),
            opacity=self.adjustment_opacity_slider.value(),
            parameters=parameters,
        )
        self._invalidate_composite_cache()
        self._adjustment_property_dirty = before_document != self.document
        self._refresh_canvas()
        self._refresh_canvas_status_strip()

    def commit_selected_adjustment_enabled(self) -> None:
        if self._adjustment_preview_timer.isActive():
            self._adjustment_preview_timer.stop()
        self.preview_selected_adjustment_properties()
        self.commit_selected_adjustment_properties()

    def reset_selected_adjustment(self) -> None:
        if self.document is None:
            return
        if self._adjustment_property_dirty:
            self.commit_selected_adjustment_properties()
        adjustment = self._selected_adjustment()
        if adjustment is None:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_adjustment_layer(
            self.document,
            adjustment.layer_id,
            enabled=adjustment.enabled,
            opacity=adjustment.opacity,
            parameters=self._default_adjustment_parameters(adjustment.adjustment_type),
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Reset Adjustment",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_adjustments(preserve_selection_id=adjustment.layer_id)

    def commit_selected_adjustment_properties(self) -> None:
        if self._adjustment_preview_timer.isActive():
            self._adjustment_preview_timer.stop()
            self.preview_selected_adjustment_properties()
        if self.document is None or not self._adjustment_property_dirty:
            return
        before_document = self._pending_adjustment_before_document or dataclasses.replace(self.document)
        self._record_history_change(
            "Adjustment Update",
            before_document=before_document,
            before_layer_pixels={},
            kind="adjustment_update",
            tracked_layer_ids=[],
        )
        self._pending_adjustment_before_document = None
        self._adjustment_property_dirty = False
        current_adjustment = self._current_adjustment_id()
        self._refresh_editor_views(
            canvas=True,
            history=True,
            adjustments=True,
            status=True,
            tool_visibility=False,
        )
        if current_adjustment:
            self._refresh_adjustments(preserve_selection_id=current_adjustment)

    def _shift_pixels(
        self,
        pixels: np.ndarray,
        dx: int,
        dy: int,
        *,
        selection_mask: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        if dx == 0 and dy == 0:
            return pixels.copy()
        height, width = pixels.shape[:2]
        shifted = np.zeros_like(pixels)
        src_x0 = max(0, -dx)
        src_y0 = max(0, -dy)
        src_x1 = min(width, width - dx) if dx >= 0 else width
        src_y1 = min(height, height - dy) if dy >= 0 else height
        dst_x0 = max(0, dx)
        dst_y0 = max(0, dy)
        dst_x1 = dst_x0 + max(0, src_x1 - src_x0)
        dst_y1 = dst_y0 + max(0, src_y1 - src_y0)
        if src_x1 > src_x0 and src_y1 > src_y0:
            shifted[dst_y0:dst_y1, dst_x0:dst_x1] = pixels[src_y0:src_y1, src_x0:src_x1]
        if selection_mask is None:
            return shifted
        selection_alpha = np.clip(selection_mask.astype(np.float32) / 255.0, 0.0, 1.0)[..., None]
        selected = np.clip(np.round(pixels.astype(np.float32) * selection_alpha), 0, 255).astype(np.uint8)
        remainder = np.clip(np.round(pixels.astype(np.float32) * (1.0 - selection_alpha)), 0, 255).astype(np.uint8)
        shifted_selected = self._shift_pixels(selected, dx, dy, selection_mask=None)
        return np.clip(remainder.astype(np.uint16) + shifted_selected.astype(np.uint16), 0, 255).astype(np.uint8)

    def _transformed_floating_pixels(self) -> Optional[np.ndarray]:
        if self.document is None or self.document.floating_selection is None or self._floating_pixels is None:
            return None
        floating = self.document.floating_selection
        pixels = self._floating_pixels.copy()
        if floating.flip_x:
            pixels = np.ascontiguousarray(np.flip(pixels, axis=1))
        if floating.flip_y:
            pixels = np.ascontiguousarray(np.flip(pixels, axis=0))
        scale_x = max(0.05, float(floating.scale_x))
        scale_y = max(0.05, float(floating.scale_y))
        if abs(scale_x - 1.0) > 1e-3 or abs(scale_y - 1.0) > 1e-3:
            new_w = max(1, int(round(pixels.shape[1] * scale_x)))
            new_h = max(1, int(round(pixels.shape[0] * scale_y)))
            pixels = cv2.resize(pixels, (new_w, new_h), interpolation=cv2.INTER_CUBIC if (new_w >= pixels.shape[1] and new_h >= pixels.shape[0]) else cv2.INTER_AREA)
        angle = float(floating.rotation_degrees)
        if abs(angle) > 1e-3:
            height, width = pixels.shape[:2]
            center = (width / 2.0, height / 2.0)
            matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
            cos = abs(matrix[0, 0])
            sin = abs(matrix[0, 1])
            bound_w = max(1, int((height * sin) + (width * cos)))
            bound_h = max(1, int((height * cos) + (width * sin)))
            matrix[0, 2] += (bound_w / 2.0) - center[0]
            matrix[1, 2] += (bound_h / 2.0) - center[1]
            pixels = cv2.warpAffine(
                pixels,
                matrix,
                (bound_w, bound_h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(0, 0, 0, 0),
            )
        return np.ascontiguousarray(pixels, dtype=np.uint8)

    def _compose_floating_selection(self, base: np.ndarray) -> np.ndarray:
        if self.document is None or self.document.floating_selection is None:
            return base
        floating_pixels = self._transformed_floating_pixels()
        if floating_pixels is None or floating_pixels.size == 0:
            return base
        floating = self.document.floating_selection
        x = int(floating.bounds[0] + floating.offset_x)
        y = int(floating.bounds[1] + floating.offset_y)
        h, w = floating_pixels.shape[:2]
        dx0 = max(0, x)
        dy0 = max(0, y)
        dx1 = min(base.shape[1], x + w)
        dy1 = min(base.shape[0], y + h)
        if dx1 <= dx0 or dy1 <= dy0:
            return base
        sx0 = dx0 - x
        sy0 = dy0 - y
        sx1 = sx0 + (dx1 - dx0)
        sy1 = sy0 + (dy1 - dy0)
        composed = base.copy()
        composed[dy0:dy1, dx0:dx1] = _blend_layer_region(
            composed[dy0:dy1, dx0:dx1],
            floating_pixels[sy0:sy1, sx0:sx1],
            opacity=100,
            mode="normal",
        )
        return composed

    def _compose_floating_selection_region(
        self,
        base_region: np.ndarray,
        bounds: Tuple[int, int, int, int],
    ) -> np.ndarray:
        if self.document is None or self.document.floating_selection is None:
            return base_region
        floating_pixels = self._transformed_floating_pixels()
        if floating_pixels is None or floating_pixels.size == 0:
            return base_region
        region_x, region_y, region_w, region_h = bounds
        floating = self.document.floating_selection
        x = int(floating.bounds[0] + floating.offset_x)
        y = int(floating.bounds[1] + floating.offset_y)
        h, w = floating_pixels.shape[:2]
        dx0 = max(region_x, x)
        dy0 = max(region_y, y)
        dx1 = min(region_x + region_w, x + w)
        dy1 = min(region_y + region_h, y + h)
        if dx1 <= dx0 or dy1 <= dy0:
            return base_region
        sx0 = dx0 - x
        sy0 = dy0 - y
        sx1 = sx0 + (dx1 - dx0)
        sy1 = sy0 + (dy1 - dy0)
        local_x0 = dx0 - region_x
        local_y0 = dy0 - region_y
        composed = base_region.copy()
        composed[local_y0:local_y0 + (dy1 - dy0), local_x0:local_x0 + (dx1 - dx0)] = _blend_layer_region(
            composed[local_y0:local_y0 + (dy1 - dy0), local_x0:local_x0 + (dx1 - dx0)],
            floating_pixels[sy0:sy1, sx0:sx1],
            opacity=100,
            mode="normal",
        )
        return composed

    def _current_composite_rgba(self) -> Optional[np.ndarray]:
        if self.document is None:
            return None
        revision = self._document_composite_revision()
        if self._composite_cache is not None and revision == self._composite_cache_revision:
            return self._composite_cache
        if self._composite_cache is not None and self._composite_dirty_bounds is not None:
            dirty_x, dirty_y, dirty_w, dirty_h = self._composite_dirty_bounds
            x0 = max(0, int(dirty_x))
            y0 = max(0, int(dirty_y))
            x1 = min(int(self.document.width), x0 + max(0, int(dirty_w)))
            y1 = min(int(self.document.height), y0 + max(0, int(dirty_h)))
            if x1 > x0 and y1 > y0:
                bounds = (x0, y0, x1 - x0, y1 - y0)
                base_region = flatten_texture_editor_layers_region(self.document, self.layer_pixels, bounds)
                composed_region = self._compose_floating_selection_region(base_region, bounds)
                composed = self._composite_cache.copy()
                composed[y0:y1, x0:x1] = composed_region
                self._composite_cache = composed
                self._composite_cache_revision = revision
                self._composite_dirty_bounds = None
                return composed
        base = flatten_texture_editor_layers(self.document, self.layer_pixels)
        composed = self._compose_floating_selection(base)
        self._composite_cache = composed
        self._composite_cache_revision = revision
        self._composite_dirty_bounds = None
        return composed

    def _refresh_canvas(self) -> None:
        if self.document is None:
            self.canvas.set_image(None)
            self.canvas.set_quick_mask_overlay(None)
            self.canvas.set_symmetry_mode("off")
            self._refresh_zoom_indicators()
            self._refresh_navigation_overlays()
            return
        flattened = self._current_composite_rgba()
        original_flattened = None
        if 0 <= self._active_session_index < len(self._sessions):
            original_flattened = self._sessions[self._active_session_index].original_flattened
        self.canvas.set_rgba_images(flattened, original_rgba=original_flattened)
        self.canvas.set_selection(self.document.selection)
        if self.document.quick_mask_enabled:
            quick_mask = build_texture_editor_selection_mask(self.document.width, self.document.height, self.document.selection)
            if quick_mask is not None and np.any(quick_mask > 0):
                overlay = np.zeros((self.document.height, self.document.width, 4), dtype=np.uint8)
                overlay[..., 0] = 235
                overlay[..., 1] = 70
                overlay[..., 2] = 90
                overlay[..., 3] = np.clip(np.round(quick_mask.astype(np.float32) * 0.32), 0.0, 255.0).astype(np.uint8)
                self.canvas.set_quick_mask_overlay(_rgba_array_to_qimage(overlay))
            else:
                self.canvas.set_quick_mask_overlay(None)
        else:
            self.canvas.set_quick_mask_overlay(None)
        if self.document.floating_selection is not None and self._floating_pixels is not None:
            transformed = self._transformed_floating_pixels()
            if transformed is not None:
                floating = self.document.floating_selection
                current_bounds = (
                    int(floating.bounds[0] + floating.offset_x),
                    int(floating.bounds[1] + floating.offset_y),
                    int(transformed.shape[1]),
                    int(transformed.shape[0]),
                )
                self.canvas.set_floating_transform_state(
                    current_bounds=current_bounds,
                    origin_bounds=(
                        int(floating.bounds[0]),
                        int(floating.bounds[1]),
                        int(floating.bounds[2]),
                        int(floating.bounds[3]),
                    ),
                    offset_x=int(floating.offset_x),
                    offset_y=int(floating.offset_y),
                    scale_x=float(floating.scale_x),
                    scale_y=float(floating.scale_y),
                    rotation_degrees=float(floating.rotation_degrees),
                )
            else:
                self.canvas.set_floating_transform_state(
                    current_bounds=None,
                    origin_bounds=None,
                    offset_x=0,
                    offset_y=0,
                    scale_x=1.0,
                    scale_y=1.0,
                    rotation_degrees=0.0,
                )
        else:
            self.canvas.set_floating_transform_state(
                current_bounds=None,
                origin_bounds=None,
                offset_x=0,
                offset_y=0,
                scale_x=1.0,
                scale_y=1.0,
                rotation_degrees=0.0,
            )
        self.canvas.set_clone_source_point(self.current_tool_settings.clone_source_point)
        self.canvas.set_symmetry_mode(self.current_tool_settings.symmetry_mode)
        self.canvas.set_view_mode(str(self.view_mode_combo.currentData() or "edited"))
        self.canvas.set_compare_split_percent(self.compare_split_slider.value())
        self.canvas.set_grid_state(
            enabled=self.grid_checkbox.isChecked(),
            grid_size=self.grid_size_spin.value(),
            grid_color=self._grid_color,
            grid_opacity=self.grid_opacity_spin.value(),
        )
        self._refresh_zoom_indicators()
        self._refresh_navigation_overlays()

    def _refresh_metadata(self) -> None:
        if self.document is None:
            self.metadata_browser.setHtml("<p>No document open.</p>")
            self.warning_label.setVisible(False)
            return
        binding = self.document.source_binding
        combined_warning = "\n".join(
            part for part in [self.document.technical_warning, self._current_ui_constraint_warning()] if part
        ).strip()
        self.warning_label.setVisible(bool(combined_warning))
        self.warning_label.setText(combined_warning)

        def _cell_text(value: str) -> str:
            text = value.strip() or "-"
            return (
                "<div style='white-space:pre-wrap; word-break:break-word; color:#D8E1EE; line-height:1.25;'>"
                f"{html.escape(text)}</div>"
            )

        semantics_text = f"{binding.texture_type}/{binding.semantic_subtype}"
        if semantics_text == "unknown/unknown":
            semantics_text = "Unknown"
        refined_html = [
            f"<div style='font-weight:600; color:#E7EDF7; margin-bottom:2px; line-height:1.2;'>{html.escape(self.document.title)}</div>",
            f"<div style='margin-bottom:8px; color:#B4C0D4; line-height:1.2;'>{self.document.width}x{self.document.height} px</div>",
            "<table style='width:100%; border-collapse:separate; border-spacing:0 6px;'>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Origin</td><td>{_cell_text(binding.launch_origin or 'file')}</td></tr>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Source</td><td>{_cell_text(binding.source_path)}</td></tr>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Relative path</td><td>{_cell_text(binding.relative_path or binding.archive_relative_path)}</td></tr>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Package</td><td>{_cell_text(binding.package_root)}</td></tr>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Original DDS</td><td>{_cell_text(binding.original_dds_path)}</td></tr>",
            f"<tr><td style='width:96px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Semantics</td><td>{_cell_text(semantics_text)}</td></tr>",
            "</table>",
        ]
        self.metadata_browser.setHtml("".join(refined_html))
        return
        def cell(value: str) -> str:
            text = value.strip() or "—"
            return f"<div style='white-space:pre-wrap; word-break:break-all;'>{html.escape(text)}</div>"

        semantics = f"{binding.texture_type}/{binding.semantic_subtype}"
        html_parts = [
            f"<div style='font-size:15px; font-weight:600; color:#E7EDF7;'>{html.escape(self.document.title)}</div>",
            f"<div style='margin-top:2px; margin-bottom:10px; color:#B4C0D4;'>{self.document.width}x{self.document.height} px</div>",
            "<table style='width:100%; border-collapse:separate; border-spacing:0 6px;'>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Origin</td><td>{cell(binding.launch_origin or 'file')}</td></tr>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Source</td><td>{cell(binding.source_path)}</td></tr>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Relative path</td><td>{cell(binding.relative_path or binding.archive_relative_path)}</td></tr>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Package</td><td>{cell(binding.package_root)}</td></tr>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Original DDS</td><td>{cell(binding.original_dds_path)}</td></tr>",
            f"<tr><td style='width:108px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Semantics</td><td>{cell(semantics)}</td></tr>",
            "</table>",
        ]
        self.metadata_browser.setHtml("".join(html_parts))
        self.warning_label.setVisible(bool(self.document.technical_warning))
        self.warning_label.setText(self.document.technical_warning)

        def _cell_override(value: str) -> str:
            text = value.strip() or "-"
            return (
                "<div style='white-space:pre-wrap; word-break:break-word; color:#D8E1EE;'>"
                f"{html.escape(text)}</div>"
            )

        semantics_override = f"{binding.texture_type}/{binding.semantic_subtype}"
        if semantics_override == "unknown/unknown":
            semantics_override = "Unknown"
        refined_html = [
            f"<div style='font-size:15px; font-weight:600; color:#E7EDF7; margin-bottom:2px;'>{html.escape(self.document.title)}</div>",
            f"<div style='margin-bottom:10px; color:#B4C0D4;'>{self.document.width}x{self.document.height} px</div>",
            "<table style='width:100%; border-collapse:separate; border-spacing:0 8px;'>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Origin</td><td>{_cell_override(binding.launch_origin or 'file')}</td></tr>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Source</td><td>{_cell_override(binding.source_path)}</td></tr>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Relative path</td><td>{_cell_override(binding.relative_path or binding.archive_relative_path)}</td></tr>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Package</td><td>{_cell_override(binding.package_root)}</td></tr>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Original DDS</td><td>{_cell_override(binding.original_dds_path)}</td></tr>",
            f"<tr><td style='width:110px; vertical-align:top; color:#E7EDF7; font-weight:600;'>Semantics</td><td>{_cell_override(semantics_override)}</td></tr>",
            "</table>",
        ]
        self.metadata_browser.setHtml("".join(refined_html))

    def _current_ui_constraint_warning(self) -> str:
        if self.document is None:
            return ""
        binding = self.document.source_binding
        target_path = (binding.archive_relative_path or binding.relative_path or "").strip()
        if not target_path:
            return ""
        cache_key = target_path.casefold()
        cached = self._ui_constraint_warning_cache.get(cache_key)
        if cached is not None:
            return cached
        if not self._looks_like_ui_constraint_candidate(target_path):
            self._ui_constraint_warning_cache[cache_key] = ""
            return ""
        self._start_ui_constraint_lookup(target_path)
        return ""

    def _looks_like_ui_constraint_candidate(self, target_path: str) -> bool:
        normalized = str(target_path or "").replace("\\", "/").lower()
        if not normalized:
            return False
        ui_tokens = ("/ui/", "/icon/", "/hud/", "/menu/", "/widget/")
        name_tokens = ("itemicon", "ui_", "icon_", "hud_", "menu_")
        return any(token in normalized for token in ui_tokens) or any(token in PurePosixPath(normalized).name for token in name_tokens)

    def _start_ui_constraint_lookup(self, target_path: str) -> None:
        cache_key = str(target_path or "").strip().casefold()
        if not cache_key:
            return
        if cache_key in self._ui_constraint_warning_cache:
            return
        if self._pending_ui_constraint_key == cache_key:
            return
        if self._ui_constraint_thread is not None:
            return
        worker = TextureEditorUIConstraintWorker(self.get_archive_entries(), target_path)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_ui_constraint_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_ui_constraint_refs)
        self._ui_constraint_worker = worker
        self._ui_constraint_thread = thread
        self._pending_ui_constraint_key = cache_key
        thread.start()

    def _handle_ui_constraint_ready(self, target_path: str, warning_text: str) -> None:
        cache_key = str(target_path or "").strip().casefold()
        if cache_key:
            self._ui_constraint_warning_cache[cache_key] = str(warning_text or "")
        if self.document is None:
            return
        binding = self.document.source_binding
        current_key = (binding.archive_relative_path or binding.relative_path or "").strip().casefold()
        if current_key and current_key == cache_key:
            self._refresh_metadata()

    def _cleanup_ui_constraint_refs(self) -> None:
        self._ui_constraint_thread = None
        self._ui_constraint_worker = None
        self._pending_ui_constraint_key = ""

    def _layer_thumbnail_icon(self, layer_id: str) -> QIcon:
        if self.document is None:
            return QIcon()
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return QIcon()
        cache_key = (layer_id, int(layer.revision))
        cached = self._thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached
        pixels = self.layer_pixels.get(layer_id)
        if pixels is None or pixels.size == 0:
            return QIcon()
        alpha = pixels[..., 3]
        ys, xs = np.where(alpha > 0)
        if xs.size > 0 and ys.size > 0:
            x0 = max(0, int(xs.min()))
            y0 = max(0, int(ys.min()))
            x1 = int(xs.max()) + 1
            y1 = int(ys.max()) + 1
            preview = pixels[y0:y1, x0:x1]
        else:
            preview = pixels
        qimage = _rgba_array_to_qimage(preview)
        pixmap = QPixmap.fromImage(qimage).scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        icon = QIcon(pixmap)
        self._thumbnail_cache[cache_key] = icon
        return icon

    def _refresh_layers(self) -> None:
        current_layer_id = self._current_layer_id()
        self._refreshing_layers_list = True
        self.layers_list.clear()
        if self.document is None:
            self._refreshing_layers_list = False
            return
        for layer in reversed(self.document.layers):
            prefix = "[Visible]" if layer.visible else "[Hidden]"
            lock_suffix = "  Lock" if layer.locked else ""
            alpha_suffix = "  Alpha" if layer.alpha_locked else ""
            mask_suffix = "  Mask" if layer.mask_layer_id and layer.mask_enabled else ""
            offset_suffix = f"  @{layer.offset_x},{layer.offset_y}" if (layer.offset_x or layer.offset_y) else ""
            item = QListWidgetItem(f"{prefix} {layer.name}  {layer.blend_mode.title()}{offset_suffix}{mask_suffix}{lock_suffix}{alpha_suffix}")
            item.setIcon(self._layer_thumbnail_icon(layer.layer_id))
            item.setData(Qt.UserRole, layer.layer_id)
            self.layers_list.addItem(item)
            if layer.layer_id == current_layer_id or (current_layer_id is None and layer.layer_id == self.document.active_layer_id):
                self.layers_list.setCurrentItem(item)
        self._refreshing_layers_list = False
        self._handle_layer_selection_changed()

    def _refresh_selection_controls(self) -> None:
        has_doc = self.document is not None
        busy = self._busy()
        has_selection = has_doc and self.document.selection.mode != "none"
        self.selection_help_label.setText(
            "Selections limit paint, erase, fill, gradient, clone, heal, patch, smudge, sharpen, soften, dodge/burn, and recolor to the selected area."
            if has_selection or (has_doc and self.current_tool_settings.tool in {"select_rect", "lasso"})
            else "Selections limit editing to the selected area. Use Rect Select or Lasso to create one."
        )
        self.selection_invert_checkbox.blockSignals(True)
        self.selection_feather_slider.blockSignals(True)
        self.selection_mode_combo.blockSignals(True)
        self.selection_quick_mask_checkbox.blockSignals(True)
        if has_doc:
            self.selection_invert_checkbox.setChecked(bool(self.document.selection.inverted))
            self.selection_feather_slider.setValue(max(0, int(self.document.selection.feather_radius)))
            self.selection_quick_mask_checkbox.setChecked(bool(self.document.quick_mask_enabled))
        else:
            self.selection_invert_checkbox.setChecked(False)
            self.selection_feather_slider.setValue(0)
            self.selection_quick_mask_checkbox.setChecked(False)
        self.selection_mode_combo.setCurrentIndex(max(0, self.selection_mode_combo.findData(self.current_tool_settings.selection_combine_mode)))
        self.selection_invert_checkbox.blockSignals(False)
        self.selection_feather_slider.blockSignals(False)
        self.selection_mode_combo.blockSignals(False)
        self.selection_quick_mask_checkbox.blockSignals(False)
        self._refresh_selection_button_labels()
        self.selection_copy_layer_button.setEnabled(bool(has_selection and not busy))
        self.selection_select_all_button.setEnabled(bool(has_doc and not busy))
        self.selection_clear_button.setEnabled(bool(has_doc and not busy and (has_selection or self.document.quick_mask_enabled)))
        self.selection_grow_button.setEnabled(bool(has_selection and not busy))
        self.selection_shrink_button.setEnabled(bool(has_selection and not busy))
        selected_layer = None if not has_doc else next((candidate for candidate in self.document.layers if candidate.layer_id == (self._current_layer_id() or self.document.active_layer_id)), None)
        has_mask = bool(selected_layer and selected_layer.mask_layer_id and selected_layer.mask_layer_id in self.layer_pixels)
        self.selection_to_mask_button.setEnabled(bool(has_selection and selected_layer and not busy))
        self.selection_from_mask_button.setEnabled(bool(has_doc and has_mask and not busy))
        self.selection_invert_checkbox.setEnabled(bool(has_selection and not busy))
        self.selection_feather_slider.setEnabled(bool(has_selection and not busy))
        self.selection_mode_combo.setEnabled(bool(has_doc and not busy))
        self.selection_refine_spin.setEnabled(bool(has_selection and not busy))
        self.selection_quick_mask_checkbox.setEnabled(bool(has_doc and not busy))

    def _refresh_adjustments(
        self,
        *,
        preserve_selection_id: Optional[str] = None,
        refresh_controls: bool = True,
    ) -> None:
        target_adjustment_id = preserve_selection_id or self._current_adjustment_id()
        selected_item: Optional[QListWidgetItem] = None
        self._refreshing_adjustments = True
        self.adjustments_list.blockSignals(True)
        self.adjustments_list.clear()
        if self.document is not None:
            for adjustment in self.document.adjustment_layers:
                item = QListWidgetItem(self._adjustment_list_label(adjustment))
                item.setData(Qt.UserRole, adjustment.layer_id)
                self.adjustments_list.addItem(item)
                if adjustment.layer_id == target_adjustment_id:
                    selected_item = item
            if selected_item is None and self.adjustments_list.count() > 0:
                selected_item = self.adjustments_list.item(max(0, self.adjustments_list.count() - 1))
            if selected_item is not None:
                self.adjustments_list.setCurrentItem(selected_item)
        self.adjustments_list.blockSignals(False)
        self._refreshing_adjustments = False
        if refresh_controls:
            self._handle_adjustment_selection_changed()

    def _refresh_transform_controls(self) -> None:
        has_floating = self.document is not None and self.document.floating_selection is not None
        has_active_layer = self.document is not None and bool(self.document.active_layer_id)
        for widget in (
            self.transform_scale_spin,
            self.transform_rotation_spin,
            self.transform_apply_button,
            self.transform_flip_h_button,
            self.transform_flip_v_button,
            self.transform_rotate_left_button,
            self.transform_rotate_right_button,
            self.transform_commit_button,
            self.transform_cancel_button,
        ):
            widget.setEnabled(bool(has_floating))
        self.transform_float_layer_button.setEnabled(bool(has_active_layer))
        if not has_floating:
            self.transform_scale_spin.blockSignals(True)
            self.transform_rotation_spin.blockSignals(True)
            self.transform_scale_spin.setValue(100)
            self.transform_rotation_spin.setValue(0)
            self.transform_scale_spin.blockSignals(False)
            self.transform_rotation_spin.blockSignals(False)
            return
        floating = self.document.floating_selection
        self.transform_scale_spin.blockSignals(True)
        self.transform_rotation_spin.blockSignals(True)
        self.transform_scale_spin.setValue(int(round(max(floating.scale_x, floating.scale_y) * 100.0)))
        self.transform_rotation_spin.setValue(int(round(floating.rotation_degrees)))
        self.transform_scale_spin.blockSignals(False)
        self.transform_rotation_spin.blockSignals(False)

    def _refresh_canvas_status_strip(self) -> None:
        if self.document is None:
            self.canvas_status_zoom_label.setText("No zoom")
            self.canvas_status_tool_label.setText("No tool")
            self.canvas_status_layer_label.setText("No layer")
            self.canvas_status_selection_label.setText("No selection")
            self.canvas_status_state_label.setText("No state")
            self.canvas_status_document_label.setText("No document")
            self.canvas_status_pixel_label.setText("XY -, -  RGBA -")
            self.canvas_status_source_label.setText("")
            return
        active_layer = next(
            (candidate for candidate in self.document.layers if candidate.layer_id == self.document.active_layer_id),
            None,
        )
        selection_text = "No selection"
        if self.document.floating_selection is not None and self._floating_pixels is not None:
            selection_text = "Floating selection active"
        elif self.document.selection.mode != "none":
            selection_text = f"Selection: {self.document.selection.mode}"
        if self.document.quick_mask_enabled:
            selection_text = f"{selection_text} | Quick Mask"
        state_bits: List[str] = []
        if self._editing_mask_target:
            state_bits.append("Edit Mask")
        if self._layer_property_dirty:
            state_bits.append("Layer Pending")
        if self._adjustment_property_dirty:
            state_bits.append("Adjustment Pending")
        selected_adjustment = self._selected_adjustment()
        if selected_adjustment is not None:
            state_bits.append(f"Adj {selected_adjustment.name}")
        channel_bits = "".join(
            marker
            for enabled, marker in (
                (self.document.edit_red_channel, "R"),
                (self.document.edit_green_channel, "G"),
                (self.document.edit_blue_channel, "B"),
                (self.document.edit_alpha_channel, "A"),
            )
            if enabled
        ) or "None"
        state_bits.append(f"Ch {channel_bits}")
        if self.current_tool_settings.symmetry_mode != "off":
            state_bits.append(f"Sym {self.current_tool_settings.symmetry_mode.title()}")
        state_text = " | ".join(state_bits) if state_bits else "Ready"
        self._refresh_zoom_indicators()
        self.canvas_status_tool_label.setText(f"Tool {self.current_tool_settings.tool.replace('_', ' ').title()}")
        self.canvas_status_layer_label.setText(f"Layer {active_layer.name if active_layer is not None else '-'}")
        self.canvas_status_selection_label.setText(selection_text)
        self.canvas_status_state_label.setText(state_text)
        self.canvas_status_document_label.setText(f"{self.document.width}x{self.document.height}")
        if self._hover_pixel_info is None:
            self.canvas_status_pixel_label.setText("XY -, -  RGBA -")
        else:
            rgba = self._hover_pixel_info.get("rgba", ())
            if isinstance(rgba, tuple) and len(rgba) == 4:
                self.canvas_status_pixel_label.setText(
                    f"XY {int(self._hover_pixel_info.get('x', 0))}, {int(self._hover_pixel_info.get('y', 0))}  RGBA {int(rgba[0])}, {int(rgba[1])}, {int(rgba[2])}, {int(rgba[3])}"
                )
            else:
                self.canvas_status_pixel_label.setText("XY -, -  RGBA -")
        source_summary = self.document.source_binding.relative_path or self.document.source_binding.archive_relative_path or self.document.source_binding.source_path
        self.canvas_status_source_label.setText(source_summary)

    def _refresh_editor_views(
        self,
        *,
        canvas: bool = True,
        metadata: bool = False,
        layers: bool = False,
        history: bool = False,
        selection: bool = False,
        adjustments: bool = False,
        transform: bool = False,
        status: bool = True,
        tool_visibility: bool = True,
    ) -> None:
        if canvas:
            self._refresh_canvas()
        if metadata:
            self._refresh_metadata()
        if layers:
            self._refresh_layers()
        if history:
            self._refresh_history_list()
        if selection:
            self._refresh_selection_controls()
        if adjustments:
            self._refresh_adjustments()
        if transform:
            self._refresh_transform_controls()
        if status:
            self._refresh_canvas_status_strip()
        if tool_visibility:
            self._refresh_tool_visibility()

    def _refresh_ui(self) -> None:
        sidebar_scroll = self._capture_left_sidebar_scroll()
        self._refresh_editor_views(
            canvas=True,
            metadata=True,
            layers=True,
            history=True,
            selection=True,
            adjustments=True,
            transform=True,
            status=True,
            tool_visibility=False,
        )
        self._refresh_channel_controls()
        has_doc = self.document is not None
        busy = self._busy()
        for button in (
            self.open_file_button,
            self.open_archive_button,
            self.open_compare_button,
            self.open_project_button,
            self.save_project_button,
            self.save_png_button,
            self.send_replace_button,
            self.send_workflow_button,
            self.add_layer_button,
            self.duplicate_layer_button,
            self.remove_layer_button,
            self.merge_layer_button,
            self.layer_up_button,
            self.layer_down_button,
            self.history_clear_button,
            self.image_crop_selection_button,
            self.image_trim_button,
            self.image_resize_button,
            self.canvas_resize_button,
            self.image_flip_h_button,
            self.image_flip_v_button,
            self.image_rotate_left_button,
            self.image_rotate_right_button,
        ):
            button.setEnabled((has_doc if button not in {self.open_file_button, self.open_archive_button, self.open_project_button} else True) and not busy)
        self.actions_menu_button.setEnabled(not busy)
        for button, action in (
            (self.open_file_button, self.action_open_file),
            (self.open_archive_button, self.action_open_archive),
            (self.open_project_button, self.action_open_project),
            (self.save_project_button, self.action_save_project),
            (self.save_png_button, self.action_export_png),
            (self.send_replace_button, self.action_send_replace),
            (self.send_workflow_button, self.action_send_workflow),
        ):
            action.setEnabled(button.isEnabled())
        self.image_crop_selection_button.setEnabled(bool(has_doc and not busy and self.document.selection.mode != "none" and self.document.floating_selection is None))
        self.image_trim_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.image_resize_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.canvas_resize_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.image_flip_h_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.image_flip_v_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.image_rotate_left_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.image_rotate_right_button.setEnabled(bool(has_doc and not busy and self.document.floating_selection is None))
        self.undo_button.setEnabled(has_doc and not busy and self.history_index > 0)
        self.redo_button.setEnabled(has_doc and not busy and self.history_index < len(self.history_snapshots) - 1)
        self.shortcuts_button.setEnabled(not busy)
        self.layer_name_edit.setEnabled(has_doc and not busy)
        self.layer_visible_checkbox.setEnabled(has_doc and not busy)
        self.layer_locked_checkbox.setEnabled(has_doc and not busy)
        self.layer_alpha_locked_checkbox.setEnabled(has_doc and not busy)
        self.layer_mask_enabled_checkbox.setEnabled(has_doc and not busy)
        self.layer_edit_mask_checkbox.setEnabled(has_doc and not busy)
        self.layer_add_mask_button.setEnabled(has_doc and not busy)
        self.layer_invert_mask_button.setEnabled(has_doc and not busy)
        self.layer_delete_mask_button.setEnabled(has_doc and not busy)
        self.layer_blend_mode_combo.setEnabled(has_doc and not busy)
        self.layer_opacity_slider.setEnabled(has_doc and not busy)
        self.view_mode_combo.setEnabled(has_doc and not busy)
        self.compare_split_slider.setEnabled(has_doc and not busy and str(self.view_mode_combo.currentData() or "edited") == "split")
        self.grid_checkbox.setEnabled(has_doc and not busy)
        self.grid_size_spin.setEnabled(has_doc and not busy and self.grid_checkbox.isChecked())
        self.grid_color_button.setEnabled(has_doc and not busy and self.grid_checkbox.isChecked())
        self.grid_opacity_spin.setEnabled(has_doc and not busy and self.grid_checkbox.isChecked())
        self.navigator_widget.setEnabled(has_doc and not busy)
        self.show_rulers_checkbox.setEnabled(has_doc and not busy)
        self.show_guides_checkbox.setEnabled(has_doc and not busy)
        self.vertical_guides_edit.setEnabled(has_doc and not busy)
        self.horizontal_guides_edit.setEnabled(has_doc and not busy)
        self.apply_guides_button.setEnabled(has_doc and not busy)
        self.clear_guides_button.setEnabled(
            has_doc
            and not busy
            and (
                bool(self._vertical_guides)
                or bool(self._horizontal_guides)
                or bool(self.vertical_guides_edit.text().strip())
                or bool(self.horizontal_guides_edit.text().strip())
            )
        )
        self.canvas.setEnabled(has_doc and not busy)
        self.document_tab_bar.setEnabled(not busy)
        for button in self.tool_buttons.values():
            button.setEnabled(has_doc and not busy)
        for widget in (
            self.paint_color_edit,
            self.paint_color_button,
            self.paint_color_sample_button,
            self.secondary_color_edit,
            self.secondary_color_button,
            self.secondary_color_sample_button,
            self.brush_preset_combo,
            self.save_brush_preset_button,
            self.brush_tip_combo,
            self.brush_pattern_combo,
            self.custom_brush_tip_path_edit,
            self.load_custom_brush_tip_button,
            self.clear_custom_brush_tip_button,
            self.symmetry_mode_combo,
            self.brush_size_slider,
            self.size_step_mode_combo,
            self.hardness_slider,
            self.roundness_slider,
            self.angle_slider,
            self.smoothing_slider,
            self.opacity_slider,
            self.flow_slider,
            self.spacing_slider,
            self.fill_tolerance_slider,
            self.fill_contiguous_checkbox,
            self.paint_blend_mode_combo,
            self.strength_slider,
            self.smudge_strength_slider,
            self.dodge_burn_mode_combo,
            self.dodge_burn_exposure_slider,
            self.patch_blend_slider,
            self.gradient_type_combo,
            self.sharpen_mode_combo,
            self.soften_mode_combo,
            self.sample_visible_layers_checkbox,
            self.clone_aligned_checkbox,
            self.clear_clone_source_button,
            self.lasso_snap_checkbox,
            self.lasso_snap_radius_slider,
            self.lasso_snap_sensitivity_slider,
            self.recolor_mode_combo,
            self.recolor_source_edit,
            self.recolor_source_pick_button,
            self.recolor_source_sample_button,
            self.recolor_target_edit,
            self.recolor_target_pick_button,
            self.recolor_target_sample_button,
            self.recolor_tolerance_slider,
            self.recolor_strength_slider,
            self.recolor_preserve_luma_checkbox,
            self.apply_recolor_button,
            self.selection_mode_combo,
            self.selection_invert_checkbox,
            self.selection_feather_slider,
            self.selection_refine_spin,
            self.selection_quick_mask_checkbox,
            self.selection_copy_layer_button,
            self.selection_select_all_button,
            self.selection_clear_button,
            self.selection_grow_button,
            self.selection_shrink_button,
            self.channel_red_checkbox,
            self.channel_green_checkbox,
            self.channel_blue_checkbox,
            self.channel_alpha_checkbox,
            self.channel_all_button,
            self.channel_rgb_button,
            self.channel_alpha_only_button,
            self.channel_extract_combo,
            self.channel_extract_button,
            self.channel_pack_combo,
            self.channel_pack_button,
            self.channel_selection_combo,
            self.channel_selection_from_button,
            self.channel_selection_to_combo,
            self.channel_selection_to_button,
            self.channel_copy_combo,
            self.channel_copy_button,
            self.channel_paste_combo,
            self.channel_paste_button,
            self.channel_swap_a_combo,
            self.channel_swap_b_combo,
            self.channel_swap_button,
        ):
            widget.setEnabled(has_doc and not busy)
        self._schedule_left_sidebar_scroll_restore(*sidebar_scroll)
        self.clear_clone_source_button.setEnabled(
            has_doc and not busy and self.current_tool_settings.clone_source_point is not None
        )
        self._apply_empty_state_layout(has_doc)
        self.channels_section.setVisible(has_doc)
        self.metadata_section.setVisible(has_doc)
        self.navigator_section.setVisible(has_doc)
        self.image_section.setVisible(has_doc)
        self.transform_float_layer_button.setEnabled(has_doc and not busy and self.document is not None and bool(self.document.active_layer_id))
        self.transform_section.setVisible(has_doc)
        self.adjustments_section.setVisible(has_doc)
        self.adjustment_add_combo.setEnabled(has_doc and not busy)
        self.adjustment_add_button.setEnabled(has_doc and not busy)
        self.adjustment_mode_label.setVisible(has_doc)
        has_adjustment_item = self.adjustments_list.currentItem() is not None
        self.adjustment_duplicate_button.setEnabled(has_doc and not busy and has_adjustment_item)
        self.adjustment_remove_button.setEnabled(has_doc and not busy and has_adjustment_item)
        self.adjustment_reset_button.setEnabled(has_doc and not busy and has_adjustment_item)
        self.adjustment_up_button.setEnabled(has_doc and not busy and has_adjustment_item and self.adjustments_list.currentRow() > 0)
        self.adjustment_down_button.setEnabled(
            has_doc
            and not busy
            and has_adjustment_item
            and 0 <= self.adjustments_list.currentRow() < (self.adjustments_list.count() - 1)
        )
        self.adjustment_solo_button.setEnabled(has_doc and not busy and has_adjustment_item)
        self.adjustment_use_active_mask_button.setEnabled(
            has_doc and not busy and has_adjustment_item and bool(self._current_layer_id())
        )
        selected_adjustment = self._selected_adjustment()
        self.adjustment_clear_mask_button.setEnabled(
            has_doc and not busy and selected_adjustment is not None and bool(selected_adjustment.mask_layer_id)
        )
        self.adjustments_list.setEnabled(has_doc and not busy)
        self.atlas_section.setVisible(has_doc)
        self.atlas_padding_spin.setEnabled(has_doc and not busy)
        self.atlas_trim_checkbox.setEnabled(has_doc and not busy)
        self.atlas_skip_empty_checkbox.setEnabled(has_doc and not busy)
        self.atlas_export_selection_button.setEnabled(has_doc and not busy and self._current_selection_bounds() is not None)
        self.atlas_export_grid_button.setEnabled(has_doc and not busy)
        self.history_list.setEnabled(has_doc and not busy)
        self._update_history_action_state()
        self._refresh_tool_visibility()

    def _handle_layer_selection_changed(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        self.document = dataclasses.replace(self.document, active_layer_id=layer_id)
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        self.layer_name_edit.setText(layer.name)
        self.layer_visible_checkbox.blockSignals(True)
        self.layer_visible_checkbox.setChecked(layer.visible)
        self.layer_visible_checkbox.blockSignals(False)
        self.layer_locked_checkbox.blockSignals(True)
        self.layer_locked_checkbox.setChecked(layer.locked)
        self.layer_locked_checkbox.blockSignals(False)
        self.layer_alpha_locked_checkbox.blockSignals(True)
        self.layer_alpha_locked_checkbox.setChecked(layer.alpha_locked)
        self.layer_alpha_locked_checkbox.blockSignals(False)
        self.layer_mask_enabled_checkbox.blockSignals(True)
        self.layer_mask_enabled_checkbox.setChecked(bool(layer.mask_layer_id) and layer.mask_enabled)
        self.layer_mask_enabled_checkbox.blockSignals(False)
        self.layer_edit_mask_checkbox.blockSignals(True)
        self.layer_edit_mask_checkbox.setChecked(bool(self._editing_mask_target and layer.mask_layer_id))
        self.layer_edit_mask_checkbox.blockSignals(False)
        self.layer_blend_mode_combo.blockSignals(True)
        blend_index = self.layer_blend_mode_combo.findData(layer.blend_mode)
        self.layer_blend_mode_combo.setCurrentIndex(max(0, blend_index))
        self.layer_blend_mode_combo.blockSignals(False)
        self.layer_opacity_slider.blockSignals(True)
        self.layer_opacity_slider.setValue(layer.opacity)
        self.layer_opacity_slider.blockSignals(False)
        has_mask = bool(layer.mask_layer_id and layer.mask_layer_id in self.layer_pixels)
        self.layer_mask_enabled_checkbox.setEnabled(has_mask)
        self.layer_edit_mask_checkbox.setEnabled(has_mask)
        self.layer_invert_mask_button.setEnabled(has_mask)
        self.layer_delete_mask_button.setEnabled(has_mask)

    def _handle_layers_reordered_by_drag(self, *_args) -> None:
        if self.document is None or self._refreshing_layers_list or self._busy():
            return
        display_ids: List[str] = []
        for row in range(self.layers_list.count()):
            item = self.layers_list.item(row)
            if item is None:
                continue
            value = item.data(Qt.UserRole)
            if value:
                display_ids.append(str(value))
        if len(display_ids) != len(self.document.layers):
            return
        desired_document_order = tuple(reversed(display_ids))
        current_document_order = tuple(layer.layer_id for layer in self.document.layers)
        if desired_document_order == current_document_order:
            return
        layers_by_id = {layer.layer_id: layer for layer in self.document.layers}
        if set(desired_document_order) != set(layers_by_id.keys()):
            return
        before_document = dataclasses.replace(self.document)
        updated_layers = tuple(
            dataclasses.replace(
                layers_by_id[layer_id],
                revision=int(layers_by_id[layer_id].revision) + 1,
            )
            for layer_id in desired_document_order
        )
        self.document = dataclasses.replace(
            self.document,
            layers=updated_layers,
            composite_revision=int(self.document.composite_revision) + 1,
        )
        self._invalidate_composite_cache()
        self._record_history_change(
            "Reorder Layer",
            before_document=before_document,
            before_layer_pixels={},
            kind="layer_reorder",
            tracked_layer_ids=[],
        )
        self._refresh_ui()
        self._set_status("Reordered layers.", False)

    def rename_selected_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        new_name = self.layer_name_edit.text().strip() or "Layer"
        if new_name == layer.name:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels: Dict[str, np.ndarray] = {}
        self.document = update_texture_editor_layer(self.document, layer_id, name=new_name)
        self._layer_property_dirty = False
        self._record_history_change(
            "Rename Layer",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_update",
            tracked_layer_ids=[],
        )
        self._refresh_layers()

    def preview_selected_layer_properties(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        new_visible = self.layer_visible_checkbox.isChecked()
        new_opacity = self.layer_opacity_slider.value()
        new_blend_mode = str(self.layer_blend_mode_combo.currentData() or "normal")
        changed = (
            (layer.visible != new_visible)
            or (layer.opacity != new_opacity)
            or (layer.blend_mode != new_blend_mode)
        )
        if not changed:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels: Dict[str, np.ndarray] = {}
        self.document = update_texture_editor_layer(
            self.document,
            layer_id,
            visible=new_visible,
            opacity=new_opacity,
            blend_mode=new_blend_mode,
        )
        self._layer_property_dirty = True
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._pending_layer_property_before_document = before_document
        self._pending_layer_property_before_pixels = before_layer_pixels
        if layer.visible != new_visible or layer.blend_mode != new_blend_mode:
            self._refresh_editor_views(canvas=True, layers=True, status=True, tool_visibility=False)
            return
        self._refresh_editor_views(canvas=True, status=True, tool_visibility=False)

    def commit_selected_layer_opacity(self) -> None:
        if not self._layer_property_dirty:
            return
        self._layer_property_dirty = False
        before_document = getattr(self, "_pending_layer_property_before_document", dataclasses.replace(self.document))
        before_layer_pixels = getattr(self, "_pending_layer_property_before_pixels", {})
        self._record_history_change(
            "Change Layer Opacity",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_update",
            tracked_layer_ids=[],
        )
        self._pending_layer_property_before_document = None
        self._pending_layer_property_before_pixels = {}
        self._refresh_editor_views(canvas=True, history=True, status=True, tool_visibility=False)

    def toggle_selected_layer_visibility(self) -> None:
        if self.document is None:
            return
        self.preview_selected_layer_properties()
        if not self._layer_property_dirty:
            return
        self._layer_property_dirty = False
        before_document = getattr(self, "_pending_layer_property_before_document", dataclasses.replace(self.document))
        before_layer_pixels = getattr(self, "_pending_layer_property_before_pixels", {})
        self._record_history_change(
            "Toggle Layer Visibility",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_update",
            tracked_layer_ids=[],
        )
        self._pending_layer_property_before_document = None
        self._pending_layer_property_before_pixels = {}
        self._refresh_editor_views(canvas=True, layers=True, history=True, status=True, tool_visibility=False)

    def commit_selected_layer_flags(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if layer is None:
            return
        new_locked = self.layer_locked_checkbox.isChecked()
        new_alpha_locked = self.layer_alpha_locked_checkbox.isChecked()
        if layer.locked == new_locked and layer.alpha_locked == new_alpha_locked:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_layer(
            self.document,
            layer_id,
            locked=new_locked,
            alpha_locked=new_alpha_locked,
        )
        self._record_history_change(
            "Layer Lock State",
            before_document=before_document,
            before_layer_pixels={},
            kind="layer_update",
            tracked_layer_ids=[],
        )
        self._refresh_editor_views(canvas=True, layers=True, history=True, status=True, tool_visibility=False)

    def add_layer(self) -> None:
        if self.document is None:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels, new_id = add_texture_editor_layer(self.document, self.layer_pixels)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Add Layer",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_add",
            force_checkpoint=True,
        )
        self._refresh_ui()
        for row in range(self.layers_list.count()):
            item = self.layers_list.item(row)
            if item.data(Qt.UserRole) == new_id:
                self.layers_list.setCurrentItem(item)
                break

    def duplicate_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels, new_id = duplicate_texture_editor_layer(self.document, self.layer_pixels, layer_id)
        if new_id is None:
            return
        self._invalidate_composite_cache()
        self._record_history_change(
            "Duplicate Layer",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_duplicate",
            force_checkpoint=True,
        )
        self._refresh_ui()

    def remove_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels = remove_texture_editor_layer(self.document, self.layer_pixels, layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Remove Layer",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_remove",
            force_checkpoint=True,
        )
        self._refresh_ui()

    def merge_layer_down(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels = merge_texture_editor_layer_down(self.document, self.layer_pixels, layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Merge Layer Down",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="layer_merge",
            force_checkpoint=True,
        )
        self._refresh_ui()

    def reorder_layer(self, direction: int) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id()
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        self.document = reorder_texture_editor_layer(self.document, layer_id, direction=direction)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Reorder Layer",
            before_document=before_document,
            before_layer_pixels={},
            kind="layer_reorder",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def clear_selection(self) -> None:
        if self.document is None:
            return
        before_document = dataclasses.replace(self.document)
        self.document = clear_texture_editor_selection(self.document)
        self._record_history_change(
            "Clear Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def select_all_image(self) -> None:
        if self.document is None:
            return
        before_document = dataclasses.replace(self.document)
        self.document = select_all_texture_editor(self.document)
        self._record_history_change(
            "Select All",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def _refresh_selection_button_labels(self) -> None:
        amount = max(1, int(self.selection_refine_spin.value()))
        self.selection_grow_button.setText(f"Grow +{amount}")
        self.selection_shrink_button.setText(f"Shrink -{amount}")

    def _refresh_channel_controls(self) -> None:
        values = (True, True, True, True)
        busy = self._busy()
        if self.document is not None:
            values = (
                bool(self.document.edit_red_channel),
                bool(self.document.edit_green_channel),
                bool(self.document.edit_blue_channel),
                bool(self.document.edit_alpha_channel),
            )
        for checkbox, value in (
            (self.channel_red_checkbox, values[0]),
            (self.channel_green_checkbox, values[1]),
            (self.channel_blue_checkbox, values[2]),
            (self.channel_alpha_checkbox, values[3]),
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(value)
            checkbox.blockSignals(False)
        has_doc = self.document is not None
        has_layer = bool(has_doc and (self._current_layer_id() or self.document.active_layer_id))
        self.channel_extract_button.setEnabled(has_layer and not busy)
        self.channel_extract_combo.setEnabled(has_layer and not busy)
        self.channel_pack_button.setEnabled(has_layer and not busy)
        self.channel_pack_combo.setEnabled(has_layer and not busy)
        has_selection = bool(has_doc and self.document.selection.mode != "none")
        self.channel_selection_combo.setEnabled(has_layer and not busy)
        self.channel_selection_from_button.setEnabled(has_layer and not busy)
        self.channel_selection_to_combo.setEnabled(has_layer and not busy and has_selection)
        self.channel_selection_to_button.setEnabled(has_layer and not busy and has_selection)
        self.channel_copy_combo.setEnabled(has_layer and not busy)
        self.channel_copy_button.setEnabled(has_layer and not busy)
        self.channel_paste_combo.setEnabled(has_layer and not busy and self.channel_clipboard is not None)
        self.channel_paste_button.setEnabled(has_layer and not busy and self.channel_clipboard is not None)
        self.channel_swap_a_combo.setEnabled(has_layer and not busy)
        self.channel_swap_b_combo.setEnabled(has_layer and not busy)
        self.channel_swap_button.setEnabled(has_layer and not busy)

    def adjust_selection_size(self, delta: int) -> None:
        if self.document is None or self.document.selection.mode == "none":
            return
        before_document = dataclasses.replace(self.document)
        if delta > 0:
            self.document = grow_texture_editor_selection(self.document, delta)
            label = "Grow Selection"
        else:
            self.document = shrink_texture_editor_selection(self.document, abs(delta))
            label = "Shrink Selection"
        self._record_history_change(
            label,
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def toggle_quick_mask(self, checked: bool) -> None:
        if self.document is None:
            self.selection_quick_mask_checkbox.blockSignals(True)
            self.selection_quick_mask_checkbox.setChecked(False)
            self.selection_quick_mask_checkbox.blockSignals(False)
            return
        before_document = dataclasses.replace(self.document)
        self.document = dataclasses.replace(self.document, quick_mask_enabled=bool(checked))
        self._record_history_change(
            "Toggle Quick Mask",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def toggle_quick_mask_shortcut(self) -> None:
        if self.document is None:
            return
        self.selection_quick_mask_checkbox.setChecked(not self.selection_quick_mask_checkbox.isChecked())

    def preview_selection_settings(self) -> None:
        if self.document is None:
            return
        self.document = update_texture_editor_selection_settings(
            self.document,
            feather_radius=self.selection_feather_slider.value(),
        )
        self._refresh_canvas()

    def commit_selection_settings(self) -> None:
        if self.document is None:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_selection_settings(
            self.document,
            feather_radius=self.selection_feather_slider.value(),
        )
        self._record_history_change(
            "Selection Feather",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def toggle_selection_invert(self, checked: bool) -> None:
        if self.document is None:
            return
        before_document = dataclasses.replace(self.document)
        self.document = update_texture_editor_selection_settings(self.document, inverted=checked)
        self._record_history_change(
            "Invert Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()

    def apply_selection_to_selected_layer_mask(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        self.document, self.layer_pixels, mask_id = apply_texture_editor_selection_to_layer_mask(
            self.document,
            self.layer_pixels,
            layer_id,
        )
        if not mask_id:
            self._set_status("Create a selection first, then use Selection To Mask.", True)
            return
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache()
        self._record_history_change(
            "Selection To Mask",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="mask_update",
            tracked_layer_ids=[layer_id, mask_id],
            force_checkpoint=True,
        )
        self._editing_mask_target = True
        self._refresh_ui()
        self._set_status("Converted the current selection into the active layer mask.", False)

    def load_selected_layer_mask_as_selection(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id:
            return
        before_document = dataclasses.replace(self.document)
        updated_document = load_texture_editor_layer_mask_as_selection(
            self.document,
            self.layer_pixels,
            layer_id,
            combine_mode=self.current_tool_settings.selection_combine_mode,
        )
        if updated_document is self.document:
            self._set_status("The active layer does not have a mask to load as a selection.", True)
            return
        self.document = updated_document
        self._record_history_change(
            "Mask To Selection",
            before_document=before_document,
            before_layer_pixels={},
            kind="selection_update",
            tracked_layer_ids=[],
        )
        self._refresh_ui()
        self._set_status("Loaded the active layer mask into the current selection.", False)

    def _current_selection_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        if self.document is None:
            return None
        if self.document.selection.mode == "none":
            return None
        mask = build_texture_editor_selection_mask(self.document.width, self.document.height, self.document.selection)
        if mask is None:
            return None
        ys, xs = np.where(mask > 0)
        if xs.size == 0 or ys.size == 0:
            return None
        x0 = int(xs.min())
        y0 = int(ys.min())
        x1 = int(xs.max()) + 1
        y1 = int(ys.max()) + 1
        return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def export_selection_region(self) -> None:
        if self.document is None:
            return
        bounds = self._current_selection_bounds()
        if bounds is None:
            self._set_status("Create a selection first, then use Export Selection Region.", True)
            return
        default_name = f"{self.document.title}_selection.png"
        output_path_text, _selected = QFileDialog.getSaveFileName(
            self,
            "Export selection region",
            str((Path(self._last_save_dir) / default_name).resolve()),
            "PNG files (*.png)",
        )
        if not output_path_text:
            return
        output_path = Path(output_path_text).expanduser().resolve()
        self._last_save_dir = str(output_path.parent)
        try:
            export_texture_editor_region_png(
                self.document,
                self.layer_pixels,
                output_path,
                bounds,
                padding=int(self.atlas_padding_spin.value()),
                trim_transparent=bool(self.atlas_trim_checkbox.isChecked()),
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, f"Could not export the selected region.\n\n{exc}")
            self._set_status("Selection region export failed.", True)
            return
        self._set_status(f"Exported selection region to {output_path.name}.", False)

    def export_grid_slices(self) -> None:
        if self.document is None:
            return
        output_dir_text = QFileDialog.getExistingDirectory(
            self,
            "Export grid slices",
            self._last_save_dir,
        )
        if not output_dir_text:
            return
        output_dir = Path(output_dir_text).expanduser().resolve()
        self._last_save_dir = str(output_dir)
        document = dataclasses.replace(self.document)
        layer_pixels = {key: value.copy() for key, value in self.layer_pixels.items()}
        cell_size = int(self.grid_size_spin.value())
        padding = int(self.atlas_padding_spin.value())
        trim_transparent = bool(self.atlas_trim_checkbox.isChecked())
        skip_empty = bool(self.atlas_skip_empty_checkbox.isChecked())

        def _task() -> object:
            return export_texture_editor_grid_slices(
                document,
                layer_pixels,
                output_dir,
                cell_width=cell_size,
                cell_height=cell_size,
                padding=padding,
                trim_transparent=trim_transparent,
                skip_empty=skip_empty,
            )

        def _on_success(result: object) -> None:
            exported = result if isinstance(result, list) else []
            count = len(exported)
            self._set_status(f"Exported {count} grid slice(s) to {output_dir.name}.", False)

        self._run_async_task(
            label="Exporting atlas grid slices...",
            task=_task,
            on_success=_on_success,
        )

    def _simplify_lasso_points(self, points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(points) < 3:
            return [(float(x), float(y)) for x, y in points]
        contour = np.array(points, dtype=np.float32).reshape((-1, 1, 2))
        epsilon = 0.35
        simplified = cv2.approxPolyDP(contour, epsilon, closed=False)
        output = [(float(point[0][0]), float(point[0][1])) for point in simplified]
        if len(output) < 3:
            return [(float(x), float(y)) for x, y in points]
        return output

    def _handle_canvas_selection(self, payload: object) -> None:
        if self.document is None or not isinstance(payload, dict):
            return
        before_document = dataclasses.replace(self.document)
        history_label = ""
        if payload.get("mode") == "rect":
            rect = payload.get("rect")
            if isinstance(rect, tuple) and len(rect) == 4:
                self.document = apply_texture_editor_rect_selection(
                    self.document,
                    rect,
                    combine_mode=self.current_tool_settings.selection_combine_mode,
                )
                history_label = "Rect Selection"
        elif payload.get("mode") == "lasso":
            points = payload.get("points")
            if isinstance(points, list) and len(points) >= 3:
                prepared_points = self._simplify_lasso_points(points)
                if self.current_tool_settings.lasso_snap_to_edges:
                    flattened = self._current_composite_rgba()
                    prepared_points = snap_lasso_points_to_edges(
                        flattened if flattened is not None else flatten_texture_editor_layers(self.document, self.layer_pixels),
                        prepared_points,
                        search_radius=self.current_tool_settings.lasso_snap_radius,
                        edge_sensitivity=self.current_tool_settings.lasso_edge_sensitivity,
                    )
                self.document = apply_texture_editor_lasso_selection(
                    self.document,
                    prepared_points,
                    combine_mode=self.current_tool_settings.selection_combine_mode,
                )
                history_label = "Lasso Selection"
        if history_label:
            self._record_history_change(
                history_label,
                before_document=before_document,
                before_layer_pixels={},
                kind="selection_update",
                tracked_layer_ids=[],
            )
        self._refresh_ui()

    def _handle_clone_source_picked(self, point: object) -> None:
        if not isinstance(point, tuple) or len(point) != 2:
            return
        self.current_tool_settings = dataclasses.replace(
            self.current_tool_settings,
            clone_source_point=(int(point[0]), int(point[1])),
        )
        self.canvas.set_clone_source_point(self.current_tool_settings.clone_source_point)
        self._refresh_ui()
        self._set_status(f"Clone source set to {self.current_tool_settings.clone_source_point}.", False)

    def clear_clone_source_point(self) -> None:
        self.current_tool_settings = dataclasses.replace(self.current_tool_settings, clone_source_point=None)
        self.canvas.set_clone_source_point(None)
        self._refresh_ui()
        self._set_status("Clone/heal source cleared.", False)

    def request_browse_archive(self) -> None:
        archive_path = ""
        if self.document is not None:
            binding = self.document.source_binding
            archive_path = (binding.archive_relative_path or "").strip()
            if not archive_path and binding.relative_path and binding.package_root:
                relative_parts = [part for part in PurePosixPath(binding.relative_path).parts if part]
                if len(relative_parts) > 1 and relative_parts[0] == binding.package_root:
                    archive_path = PurePosixPath(*relative_parts[1:]).as_posix()
        self.browse_archive_requested.emit(archive_path)

    def request_open_compare(self) -> None:
        if self.document is None:
            self._set_status("Open a texture first, then use Open In Compare.", True)
            return
        binding = dataclasses.replace(self.document.source_binding)
        relative_path = (binding.relative_path or binding.archive_relative_path).strip()
        if not relative_path:
            self._set_status(
                "The current document does not have a relative game path, so Compare cannot focus it automatically.",
                True,
            )
            return
        self.open_in_compare_requested.emit(relative_path, binding)

    def copy_active_layer(self) -> None:
        if self.document is None:
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id:
            return
        pixels = self.layer_pixels.get(layer_id)
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if pixels is None or layer is None:
            return
        self.layer_clipboard = (
            pixels.copy(),
            layer.name,
            int(layer.offset_x),
            int(layer.offset_y),
            str(layer.blend_mode or "normal"),
        )
        self.selection_clipboard = None
        self._set_status(f"Copied layer '{layer.name}'.", False)

    def _selection_pixels_from_active_layer(self) -> Optional[Tuple[np.ndarray, str, Tuple[int, int, int, int]]]:
        if self.document is None or self.document.selection.mode == "none":
            return None
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id:
            return None
        selection_payload = extract_texture_editor_selection(self.document, self.layer_pixels, layer_id)
        if selection_payload is None:
            return None
        extracted, bounds = selection_payload
        layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        label = layer.name if layer is not None else "Selection"
        return extracted, label, bounds

    def copy_selection_to_clipboard(self) -> bool:
        selection_payload = self._selection_pixels_from_active_layer()
        if selection_payload is None:
            return False
        extracted, label, bounds = selection_payload
        self.selection_clipboard = (extracted.copy(), label, int(bounds[0]), int(bounds[1]))
        self.layer_clipboard = None
        self._set_status(f"Copied the current selection from '{label}'.", False)
        return True

    def _clear_document_selection_only(self) -> None:
        if self.document is None:
            return
        feather = max(0, int(self.document.selection.feather_radius))
        self.document = dataclasses.replace(
            self.document,
            selection=TextureEditorSelection(
                inverted=False,
                feather_radius=feather,
            ),
        )

    def _snapshot_floating_pixels(self) -> Optional[np.ndarray]:
        return None if self._floating_pixels is None else self._floating_pixels.copy()

    def _set_floating_selection(
        self,
        pixels: np.ndarray,
        *,
        label: str,
        bounds: Tuple[int, int, int, int],
        source_layer_id: str = "",
        paste_mode: str = "in_place",
    ) -> None:
        if self.document is None:
            return
        self._floating_transform_before_document = None
        self._floating_transform_before_floating_pixels = None
        self._floating_transform_label = ""
        self._floating_pixels = np.asarray(pixels, dtype=np.uint8).copy()
        self._floating_mask = self._floating_pixels[..., 3].copy()
        self.document = dataclasses.replace(
            self.document,
            floating_selection=TextureEditorFloatingSelection(
                source_layer_id=source_layer_id,
                label=label,
                bounds=(int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3])),
                offset_x=0,
                offset_y=0,
                committed=False,
                paste_mode=paste_mode,
            ),
        )
        self._clear_document_selection_only()
        self._invalidate_composite_cache(bounds)

    def _clear_floating_selection(self) -> None:
        if self.document is None:
            return
        self._floating_transform_before_document = None
        self._floating_transform_before_floating_pixels = None
        self._floating_transform_label = ""
        self._floating_pixels = None
        self._floating_mask = None
        if self.document.floating_selection is not None:
            self.document = dataclasses.replace(self.document, floating_selection=None)
        self._invalidate_composite_cache()

    def commit_floating_selection(self) -> None:
        if self.document is None or self.document.floating_selection is None or self._floating_pixels is None:
            return
        floating = self.document.floating_selection
        transformed = self._transformed_floating_pixels()
        if transformed is None:
            self._clear_floating_selection()
            self._refresh_ui()
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        before_floating_pixels = self._snapshot_floating_pixels()
        target_x = int(floating.bounds[0] + floating.offset_x)
        target_y = int(floating.bounds[1] + floating.offset_y)
        self.document, self.layer_pixels, new_id = add_texture_editor_layer(
            self.document,
            self.layer_pixels,
            name=f"{floating.label or 'Floating'} Layer",
            initial_pixels=transformed,
            offset_x=target_x,
            offset_y=target_y,
        )
        self._clear_floating_selection()
        self._record_history_change(
            "Commit Floating Selection",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_commit",
            dirty_bounds=(target_x, target_y, transformed.shape[1], transformed.shape[0]),
            before_floating_pixels=before_floating_pixels,
        )
        self._refresh_ui()
        for row in range(self.layers_list.count()):
            item = self.layers_list.item(row)
            if item is not None and item.data(Qt.UserRole) == new_id:
                self.layers_list.setCurrentItem(item)
                break
        self._set_status("Committed floating selection to a new layer.", False)

    def cancel_floating_selection(self) -> None:
        if self.document is None or self.document.floating_selection is None:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        before_floating_pixels = self._snapshot_floating_pixels()
        self._clear_floating_selection()
        self._record_history_change(
            "Cancel Floating Selection",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_cancel",
            dirty_bounds=None,
            before_floating_pixels=before_floating_pixels,
        )
        self._refresh_ui()
        self._set_status("Canceled floating selection.", False)

    def copy_content(self) -> None:
        if self.document is not None and self.document.selection.mode != "none":
            if self.copy_selection_to_clipboard():
                return
        self.copy_active_layer()

    def cut_selection_to_floating(self) -> None:
        if self.document is None:
            return
        selection_payload = self._selection_pixels_from_active_layer()
        if selection_payload is None:
            self._set_status("Create a selection first, then use Cut.", True)
            return
        layer_id = self._current_layer_id() or self.document.active_layer_id
        if not layer_id or layer_id not in self.layer_pixels:
            return
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        before_floating_pixels = self._snapshot_floating_pixels()
        extracted, label, bounds = selection_payload
        selection_mask = build_texture_editor_selection_mask(self.document.width, self.document.height, self.document.selection)
        if selection_mask is not None:
            layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
            if layer is not None:
                lx0 = int(bounds[0] - layer.offset_x)
                ly0 = int(bounds[1] - layer.offset_y)
                lx1 = lx0 + int(bounds[2])
                ly1 = ly0 + int(bounds[3])
                target_pixels = self.layer_pixels[layer_id].copy()
                if 0 <= lx0 < lx1 <= target_pixels.shape[1] and 0 <= ly0 < ly1 <= target_pixels.shape[0]:
                    local_mask = selection_mask[int(bounds[1]):int(bounds[1] + bounds[3]), int(bounds[0]):int(bounds[0] + bounds[2])]
                    if local_mask.shape[:2] == (ly1 - ly0, lx1 - lx0):
                        alpha = np.clip(local_mask.astype(np.float32) / 255.0, 0.0, 1.0)[..., None]
                        cleared_region = target_pixels[ly0:ly1, lx0:lx1].astype(np.float32)
                        cleared_region *= (1.0 - alpha)
                        target_pixels[ly0:ly1, lx0:lx1] = np.clip(np.round(cleared_region), 0.0, 255.0).astype(np.uint8)
                    else:
                        target_pixels[ly0:ly1, lx0:lx1] = 0
                    self.layer_pixels[layer_id] = target_pixels
        self.selection_clipboard = (extracted.copy(), label, int(bounds[0]), int(bounds[1]))
        self._set_floating_selection(
            extracted,
            label=f"{label} Selection",
            bounds=(int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3])),
            source_layer_id=layer_id,
            paste_mode="in_place",
        )
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        self._invalidate_composite_cache(bounds)
        self._record_history_change(
            "Cut Selection To Floating",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_cut",
            tracked_layer_ids=[layer_id],
            dirty_bounds=(int(bounds[0]), int(bounds[1]), int(bounds[2]), int(bounds[3])),
            before_floating_pixels=before_floating_pixels,
        )
        self._set_active_tool("move")
        self._refresh_ui()
        self._set_status("Cut selection into floating content.", False)

    def paste_layer(self) -> None:
        if self.document is None or self.layer_clipboard is None:
            return
        pixels, layer_name, offset_x, offset_y, _blend_mode = self.layer_clipboard
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        before_floating_pixels = self._snapshot_floating_pixels()
        self._set_floating_selection(
            pixels,
            label=f"{layer_name} Copy",
            bounds=(offset_x, offset_y, pixels.shape[1], pixels.shape[0]),
            paste_mode="in_place",
        )
        self._record_history_change(
            "Paste Layer Floating",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_create",
            dirty_bounds=(offset_x, offset_y, pixels.shape[1], pixels.shape[0]),
            before_floating_pixels=before_floating_pixels,
        )
        self._set_active_tool("move")
        self._refresh_ui()
        self._set_status(f"Pasted layer '{layer_name} Copy' as floating content.", False)

    def paste_selection_as_layer(self) -> None:
        if self.document is None or self.selection_clipboard is None:
            return
        pixels, layer_name, offset_x, offset_y = self.selection_clipboard
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        before_floating_pixels = self._snapshot_floating_pixels()
        self._set_floating_selection(
            pixels,
            label=f"{layer_name} Selection",
            bounds=(offset_x, offset_y, pixels.shape[1], pixels.shape[0]),
            paste_mode="in_place",
        )
        self._record_history_change(
            "Paste Selection Floating",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_create",
            dirty_bounds=(offset_x, offset_y, pixels.shape[1], pixels.shape[0]),
            before_floating_pixels=before_floating_pixels,
        )
        self._set_active_tool("move")
        self._refresh_ui()
        self._set_status(f"Pasted selection as floating content from '{layer_name}'.", False)

    def paste_content(self) -> None:
        if self.document is not None and self.selection_clipboard is not None:
            self.paste_selection_as_layer()
            return
        self.paste_layer()

    def paste_content_centered(self) -> None:
        if self.document is None:
            return
        if self.selection_clipboard is not None:
            pixels, layer_name, _offset_x, _offset_y = self.selection_clipboard
            target_x = max(0, (self.document.width - pixels.shape[1]) // 2)
            target_y = max(0, (self.document.height - pixels.shape[0]) // 2)
            before_document = dataclasses.replace(self.document)
            before_layer_pixels = dict(self.layer_pixels)
            before_floating_pixels = self._snapshot_floating_pixels()
            self._set_floating_selection(
                pixels,
                label=f"{layer_name} Selection",
                bounds=(target_x, target_y, pixels.shape[1], pixels.shape[0]),
                paste_mode="centered",
            )
            self._record_history_change(
                "Paste Centered Floating",
                before_document=before_document,
                before_layer_pixels=before_layer_pixels,
                kind="floating_create",
                dirty_bounds=(target_x, target_y, pixels.shape[1], pixels.shape[0]),
                before_floating_pixels=before_floating_pixels,
            )
            self._set_active_tool("move")
            self._refresh_ui()
            self._set_status(f"Pasted selection as a centered layer from '{layer_name}'.", False)
            return
        if self.layer_clipboard is None:
            return
        pixels, layer_name, _offset_x, _offset_y, blend_mode = self.layer_clipboard
        target_x = max(0, (self.document.width - pixels.shape[1]) // 2)
        target_y = max(0, (self.document.height - pixels.shape[0]) // 2)
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = dict(self.layer_pixels)
        before_floating_pixels = self._snapshot_floating_pixels()
        self._set_floating_selection(
            pixels,
            label=f"{layer_name} Copy",
            bounds=(target_x, target_y, pixels.shape[1], pixels.shape[0]),
            paste_mode="centered",
            source_layer_id="",
        )
        self._record_history_change(
            "Paste Centered Floating",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="floating_create",
            dirty_bounds=(target_x, target_y, pixels.shape[1], pixels.shape[0]),
            before_floating_pixels=before_floating_pixels,
        )
        self._set_active_tool("move")
        self._refresh_ui()
        self._set_status(f"Pasted layer '{layer_name} Copy' centered.", False)

    def copy_selection_to_new_layer(self) -> None:
        if self.document is None:
            return
        selection_payload = self._selection_pixels_from_active_layer()
        if selection_payload is None:
            self._set_status("Create a selection first, then use Copy To New Layer.", True)
            return
        extracted, label, bounds = selection_payload
        self.document, self.layer_pixels, new_id = add_texture_editor_layer(
            self.document,
            self.layer_pixels,
            name=f"{label} Selection",
            initial_pixels=extracted,
            offset_x=int(bounds[0]),
            offset_y=int(bounds[1]),
        )
        self.document = clear_texture_editor_selection(self.document)
        self._push_history("Copy Selection To Layer")
        self._refresh_ui()
        for row in range(self.layers_list.count()):
            item = self.layers_list.item(row)
            if item.data(Qt.UserRole) == new_id:
                self.layers_list.setCurrentItem(item)
                break
        self._set_active_tool("move")
        self.selection_clipboard = (extracted.copy(), label, int(bounds[0]), int(bounds[1]))
        self._set_status("Copied selection to a new layer. Selection cleared so Move repositions the whole copied piece.", False)

    def _handle_canvas_stroke(self, payload: object) -> None:
        if self.document is None or not isinstance(payload, dict):
            return
        points = payload.get("points")
        if not isinstance(points, list) or not points:
            return
        tool = str(payload.get("tool", self.current_tool_settings.tool))
        if tool == "recolor":
            return
        before_document = dataclasses.replace(self.document)
        if tool == "move":
            start_x, start_y = points[0]
            end_x, end_y = points[-1]
            dx = int(end_x - start_x)
            dy = int(end_y - start_y)
            if dx == 0 and dy == 0:
                return
            if self.document.floating_selection is not None and self._floating_pixels is not None:
                before_floating_pixels = self._snapshot_floating_pixels()
                floating = self.document.floating_selection
                self.document = dataclasses.replace(
                    self.document,
                    floating_selection=dataclasses.replace(
                        floating,
                        offset_x=int(floating.offset_x + dx),
                        offset_y=int(floating.offset_y + dy),
                        committed=False,
                    ),
                )
                dirty_bounds = (
                    int(floating.bounds[0] + min(0, floating.offset_x + dx)),
                    int(floating.bounds[1] + min(0, floating.offset_y + dy)),
                    max(1, int(floating.bounds[2] + abs(dx))),
                    max(1, int(floating.bounds[3] + abs(dy))),
                )
                self._invalidate_composite_cache(dirty_bounds)
                self._record_history_change(
                    "Move Floating Selection",
                    before_document=before_document,
                    before_layer_pixels={},
                    kind="floating_transform",
                    tracked_layer_ids=[],
                    dirty_bounds=dirty_bounds,
                    before_floating_pixels=before_floating_pixels,
                )
            else:
                if not self.document.active_layer_id or self.document.active_layer_id not in self.layer_pixels:
                    return
                self.document = move_texture_editor_layer(
                    self.document,
                    self.document.active_layer_id,
                    dx=dx,
                    dy=dy,
                )
                self._invalidate_layer_thumbnail(self.document.active_layer_id)
                self._invalidate_composite_cache()
                self._record_history_change(
                    "Move Layer",
                    before_document=before_document,
                    before_layer_pixels={},
                    kind="layer_transform",
                    tracked_layer_ids=[],
                )
            self._refresh_editor_views(
                canvas=True,
                layers=self.document.floating_selection is None,
                history=True,
                transform=self.document.floating_selection is not None,
                status=True,
                tool_visibility=False,
            )
            return
        tool_settings = dataclasses.replace(self.current_tool_settings, tool=tool)
        if self.document.quick_mask_enabled:
            if tool not in {"paint", "erase", "fill"}:
                self._set_status("Quick Mask editing currently supports Paint, Erase, and Fill.", True)
                return
            before_document = dataclasses.replace(self.document)
            if tool == "fill":
                self.document = apply_texture_editor_selection_fill(
                    self.document,
                    tool_settings,
                    tuple(int(value) for value in points[-1]),
                )
            else:
                self.document = apply_texture_editor_selection_stroke(
                    self.document,
                    tool_settings,
                    points,
                )
            self._invalidate_composite_cache()
            self._record_history_change(
                f"Quick Mask {tool.title()}",
                before_document=before_document,
                before_layer_pixels={},
                kind="selection_update",
                tracked_layer_ids=[],
            )
            self._refresh_editor_views(
                canvas=True,
                history=True,
                selection=True,
                status=True,
                tool_visibility=False,
            )
            return
        source_snapshot = None
        if tool in {"clone", "heal"}:
            if tool_settings.clone_source_point is None:
                self._set_status("Set a clone/heal source point first with Ctrl+right-click.", True)
                return
        active_layer = self.layer_pixels.get(self.document.active_layer_id or "")
        if (
            tool in {"sharpen", "soften"}
            and active_layer is not None
            and not tool_settings.sample_visible_layers
            and not np.any(active_layer[..., 3] > 0)
        ):
            self._set_status("The active layer is empty. Duplicate a layer first, or enable 'Sample visible layers'.", True)
            return
        if tool in {"clone", "heal", "sharpen", "soften", "smudge", "patch"} and tool_settings.sample_visible_layers:
            source_snapshot = flatten_texture_editor_layers(self.document, self.layer_pixels)
        elif tool in {"clone", "heal", "smudge", "patch"} and active_layer is not None:
            source_snapshot = active_layer.copy()
        layer_id = self._current_edit_target_layer_id()
        if not layer_id or layer_id not in self.layer_pixels:
            return
        working_document = self.document if layer_id == self.document.active_layer_id else dataclasses.replace(self.document, active_layer_id=layer_id)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        dirty_bounds: Optional[Tuple[int, int, int, int]] = None
        if tool == "fill":
            self.layer_pixels = apply_texture_editor_fill(
                working_document,
                self.layer_pixels,
                tool_settings,
                tuple(int(value) for value in points[-1]),
                source_snapshot=source_snapshot,
            )
            dirty_bounds = self._current_selection_bounds() or self._layer_canvas_bounds(layer_id)
        elif tool == "gradient":
            self.layer_pixels = apply_texture_editor_gradient(
                working_document,
                self.layer_pixels,
                tool_settings,
                tuple(int(value) for value in points[0]),
                tuple(int(value) for value in points[-1]),
            )
            dirty_bounds = self._current_selection_bounds() or self._layer_canvas_bounds(layer_id)
        elif tool == "patch":
            if self.document.selection.mode == "none":
                self._set_status("Create a selection first, then drag with Patch to choose the repair source.", True)
                return
            start_x, start_y = points[0]
            end_x, end_y = points[-1]
            self.layer_pixels = apply_texture_editor_patch(
                working_document,
                self.layer_pixels,
                tool_settings,
                delta_x=int(end_x - start_x),
                delta_y=int(end_y - start_y),
                source_snapshot=source_snapshot,
            )
            dirty_bounds = self._current_selection_bounds() or self._estimated_brush_dirty_bounds(points)
        else:
            self.layer_pixels = apply_texture_editor_stroke(
                working_document,
                self.layer_pixels,
                tool_settings,
                points,
                source_snapshot=source_snapshot,
            )
            dirty_bounds = self._estimated_brush_dirty_bounds(points)
        active_layer_id = self.document.active_layer_id
        self.document = bump_texture_editor_layer_revision(self.document, active_layer_id if self._editing_mask_target and active_layer_id else layer_id)
        if active_layer_id:
            self._invalidate_layer_thumbnail(active_layer_id)
        self._invalidate_composite_cache(dirty_bounds)
        self._record_history_change(
            f"{tool.replace('_', ' ').title()}{' Mask' if self._editing_mask_target else ''}",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind=f"{tool}_stroke",
            tracked_layer_ids=[layer_id],
            dirty_bounds=dirty_bounds,
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            status=True,
            tool_visibility=False,
        )

    def apply_recolor_to_active_layer(self) -> None:
        if self.document is None:
            return
        if self.current_tool_settings.tool != "recolor":
            self._set_active_tool("recolor")
        layer_id = self.document.active_layer_id
        edit_target_id = self._current_edit_target_layer_id() or layer_id
        if not edit_target_id or edit_target_id not in self.layer_pixels:
            return
        if self._editing_mask_target:
            layer_id = edit_target_id
        else:
            layer_id = edit_target_id
        selection_mask = None
        if self.document is not None:
            from crimson_forge_toolkit.core.texture_editor import build_texture_editor_selection_mask

            selection_mask = build_texture_editor_selection_mask(self.document.width, self.document.height, self.document.selection)
        before_document = dataclasses.replace(self.document)
        before_layer_pixels = {layer_id: self.layer_pixels[layer_id].copy()}
        recolored = apply_texture_editor_recolor(
            self.layer_pixels[layer_id],
            self.current_tool_settings,
            selection_mask=selection_mask,
        )
        before_pixels = before_layer_pixels[layer_id]
        if not self.document.edit_red_channel:
            recolored[..., 0] = before_pixels[..., 0]
        if not self.document.edit_green_channel:
            recolored[..., 1] = before_pixels[..., 1]
        if not self.document.edit_blue_channel:
            recolored[..., 2] = before_pixels[..., 2]
        if not self.document.edit_alpha_channel:
            recolored[..., 3] = before_pixels[..., 3]
        active_layer = next((candidate for candidate in self.document.layers if candidate.layer_id == layer_id), None)
        if active_layer is not None and active_layer.alpha_locked:
            recolored[..., 3] = before_pixels[..., 3]
        self.layer_pixels[layer_id] = recolored
        self.document = bump_texture_editor_layer_revision(self.document, layer_id)
        self._invalidate_layer_thumbnail(layer_id)
        dirty_bounds = self._current_selection_bounds() or self._layer_canvas_bounds(layer_id)
        self._invalidate_composite_cache(dirty_bounds)
        self._record_history_change(
            "Recolor Layer",
            before_document=before_document,
            before_layer_pixels=before_layer_pixels,
            kind="recolor_stroke",
            tracked_layer_ids=[layer_id],
            dirty_bounds=dirty_bounds,
        )
        self._refresh_editor_views(
            canvas=True,
            history=True,
            status=True,
            tool_visibility=False,
        )

    def open_file_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open image or DDS for Texture Editor",
            self._last_open_dir,
            "Supported files (*.png *.dds *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
        )
        if not file_path:
            return
        self._last_open_dir = str(Path(file_path).expanduser().resolve().parent)
        self.open_source_path(Path(file_path), binding=TextureEditorSourceBinding(launch_origin="file"))

    def open_project_dialog(self) -> None:
        project_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Texture Editor project",
            self._last_open_dir,
            "Texture Editor projects (*.ctfedit.json);;JSON files (*.json);;All files (*.*)",
        )
        if not project_path:
            return
        self._last_open_dir = str(Path(project_path).expanduser().resolve().parent)
        self.load_project(Path(project_path))

    def open_source_path(self, source_path: Path, *, binding: Optional[TextureEditorSourceBinding] = None) -> None:
        resolved_source = source_path.expanduser().resolve()
        try:
            texture_binding = self._build_binding_for_source(
                resolved_source,
                launch_origin=binding.launch_origin if binding is not None else "file",
                binding=binding,
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        for index, session in enumerate(self._sessions):
            document = session.document
            if document is not None and document.source_binding.source_path:
                try:
                    if Path(document.source_binding.source_path).expanduser().resolve() == resolved_source:
                        session.document = dataclasses.replace(
                            document,
                            source_binding=texture_binding,
                            technical_warning=texture_binding.technical_warning,
                        )
                        self._load_session_index(index)
                        self._refresh_metadata()
                        self._refresh_canvas_status_strip()
                        self._set_status(f"{resolved_source.name} is already open in Texture Editor.", False)
                        return
                except Exception:
                    continue
        texconv_text = str(self.get_texconv_path()).strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None

        def _task() -> object:
            document, layer_pixels, _normalized_png = create_texture_editor_document_from_source(
                resolved_source,
                texconv_path=texconv_path,
                workspace_root=self.workspace_root,
                binding=texture_binding,
            )
            return (document, layer_pixels)

        def _handle_open(result: object) -> None:
            document, layer_pixels = result  # type: ignore[misc]
            self._create_session(document, layer_pixels, label=document.title)
            self._push_history("Open Document")
            self._refresh_ui()
            self._set_status(f"Opened {resolved_source.name} in Texture Editor.", False)

        self._run_async_task(label=f"Opening {resolved_source.name} in Texture Editor...", task=_task, on_success=_handle_open)

    def load_project(self, project_path: Path) -> None:
        resolved_project = project_path.expanduser().resolve()
        for index, session in enumerate(self._sessions):
            document = session.document
            if document is not None and document.project_path is not None and document.project_path == resolved_project:
                self._load_session_index(index)
                self._set_status(f"Project {resolved_project.name} is already open.", False)
                return

        def _task() -> object:
            return load_texture_editor_project(resolved_project)

        def _handle_open_project(result: object) -> None:
            document, layer_pixels, floating_pixels = result  # type: ignore[misc]
            self._create_session(document, layer_pixels, label=document.title)
            self._floating_pixels = None if floating_pixels is None else floating_pixels.copy()
            self._floating_mask = None if self._floating_pixels is None else self._floating_pixels[..., 3].copy()
            self._push_history("Open Project")
            self._refresh_ui()
            self._set_status(f"Opened project {resolved_project.name}.", False)

        self._run_async_task(label=f"Opening project {resolved_project.name}...", task=_task, on_success=_handle_open_project)

    def save_project_dialog(self) -> None:
        if self.document is None:
            return
        initial = self.document.project_path or (Path(self._last_save_dir) / f"{self.document.title}.ctfedit.json")
        project_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Texture Editor project",
            str(initial),
            "Texture Editor projects (*.ctfedit.json)",
        )
        if not project_path:
            return
        self._last_save_dir = str(Path(project_path).expanduser().resolve().parent)
        document = dataclasses.replace(self.document)
        layer_pixels = {key: value.copy() for key, value in self.layer_pixels.items()}
        floating_pixels = self._snapshot_floating_pixels()

        def _task() -> object:
            return save_texture_editor_project(
                document,
                layer_pixels,
                Path(project_path),
                floating_pixels=floating_pixels,
            )

        def _handle_save(result: object) -> None:
            self.document = result  # type: ignore[assignment]
            if 0 <= self._active_session_index < len(self._sessions):
                self._sessions[self._active_session_index].label = self.document.title
                self._sync_document_tab_label(self._active_session_index)
            self._set_status(f"Saved project to {project_path}.", False)
            self._refresh_ui()

        self._run_async_task(label=f"Saving project {Path(project_path).name}...", task=_task, on_success=_handle_save)

    def _export_workspace_png_sync(self, suffix: str) -> Path:
        if self.document is None:
            raise ValueError("No document open.")
        exports_root = (self.document.workspace_root or self.workspace_root) / "exports"
        exports_root.mkdir(parents=True, exist_ok=True)
        output_path = exports_root / f"{self._export_workspace_png_stem(suffix)}.png"
        export_texture_editor_flattened_png(self.document, self.layer_pixels, output_path)
        return output_path

    def _export_workspace_png_stem(self, suffix: str) -> str:
        if self.document is None:
            return f"texture_editor_{suffix}"
        if suffix == "replace_assistant":
            binding = self.document.source_binding
            for candidate in (
                binding.archive_relative_path,
                binding.relative_path,
                binding.source_path,
                self.document.title,
            ):
                candidate_text = str(candidate or "").strip()
                if not candidate_text:
                    continue
                stem = Path(PurePosixPath(candidate_text)).stem.strip()
                if stem:
                    return stem
        return f"{self.document.title}_{suffix}"

    def _export_workspace_png(self, suffix: str, *, on_ready: Optional[Callable[[Path], None]] = None) -> None:
        if self.document is None:
            return
        document = dataclasses.replace(self.document)
        layer_pixels = {key: value.copy() for key, value in self.layer_pixels.items()}

        def _task() -> object:
            exports_root = (document.workspace_root or self.workspace_root) / "exports"
            exports_root.mkdir(parents=True, exist_ok=True)
            output_path = exports_root / f"{self._export_workspace_png_stem(suffix)}.png"
            export_texture_editor_flattened_png(document, layer_pixels, output_path)
            return output_path

        def _handle_export(result: object) -> None:
            output_path = Path(str(result))
            if self.document is not None:
                self.document = dataclasses.replace(self.document, last_flattened_png_path=str(output_path))
                self._refresh_metadata()
            if on_ready is not None:
                on_ready(output_path)

        self._run_async_task(label=f"Exporting {suffix.replace('_', ' ')} PNG...", task=_task, on_success=_handle_export)

    def _update_last_flattened_output(self, output_path: Path) -> None:
        if self.document is not None:
            self.document = dataclasses.replace(self.document, last_flattened_png_path=str(output_path))
            self._refresh_metadata()
        return output_path

    def save_flattened_png_dialog(self) -> None:
        if self.document is None:
            return
        initial = Path(self._last_save_dir) / f"{self.document.title}.png"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save flattened PNG",
            str(initial),
            "PNG files (*.png)",
        )
        if not file_path:
            return
        self._last_save_dir = str(Path(file_path).expanduser().resolve().parent)
        document = dataclasses.replace(self.document)
        layer_pixels = {key: value.copy() for key, value in self.layer_pixels.items()}

        def _task() -> object:
            return export_texture_editor_flattened_png(document, layer_pixels, Path(file_path))

        def _handle_save_png(result: object) -> None:
            output_path = Path(str(result))
            self._update_last_flattened_output(output_path)
            self._set_status(f"Saved flattened PNG to {output_path}.", False)
            self._refresh_ui()

        self._run_async_task(label=f"Saving flattened PNG to {Path(file_path).name}...", task=_task, on_success=_handle_save_png)

    def send_to_replace_assistant(self) -> None:
        if self.document is None:
            return
        source_binding = dataclasses.replace(self.document.source_binding)

        def _handle_ready(output_path: Path) -> None:
            self.send_to_replace_assistant_requested.emit(str(output_path), source_binding)
            self._set_status(f"Sent flattened PNG to Replace Assistant: {output_path.name}", False)
            self._refresh_ui()

        self._export_workspace_png("replace_assistant", on_ready=_handle_ready)

    def send_to_texture_workflow(self) -> None:
        if self.document is None:
            return
        source_binding = dataclasses.replace(self.document.source_binding)

        def _handle_ready(output_path: Path) -> None:
            QTimer.singleShot(
                0,
                lambda path_text=str(output_path), binding=source_binding: self.send_to_texture_workflow_requested.emit(
                    path_text,
                    binding,
                ),
            )
            self._set_status(f"Preparing Texture Workflow handoff: {output_path.name}", False)
            self._refresh_ui()

        self._export_workspace_png("texture_workflow", on_ready=_handle_ready)
