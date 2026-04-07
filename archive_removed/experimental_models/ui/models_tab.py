from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from crimson_texture_forge.constants import ARCHIVE_MODEL_EXTENSIONS
from crimson_texture_forge.models import ArchiveEntry
from crimson_texture_forge.core.archive import (
    build_archive_entry_detail_text,
    ensure_archive_preview_source,
    extract_archive_entries,
    format_byte_size,
)
from crimson_texture_forge.core.model_runtime import (
    build_wireframe_preview,
    export_entry_for_blender,
    load_runtime_meshes_for_entry,
)


SKELETON_EXTENSIONS = {".hkx", ".skel", ".skeleton"}
BLENDER_READY_EXTENSIONS = {".obj", ".fbx", ".dae", ".gltf", ".glb"}
MODEL_SIDECAR_EXTENSIONS = (
    set(ARCHIVE_MODEL_EXTENSIONS)
    | SKELETON_EXTENSIONS
    | {".mtl", ".material", ".json", ".bin", ".png", ".jpg", ".jpeg", ".tga", ".dds"}
)
MODEL_TREE_BATCH_SIZE = 1000
MODEL_SKELETON_COUNT_LIMIT = 2000


def is_model_like_entry(entry: ArchiveEntry) -> bool:
    return entry.extension in ARCHIVE_MODEL_EXTENSIONS or entry.extension in SKELETON_EXTENSIONS


def is_blender_ready_entry(entry: ArchiveEntry) -> bool:
    return entry.extension in BLENDER_READY_EXTENSIONS


def has_likely_skeleton_role(entry: ArchiveEntry) -> bool:
    stem = Path(entry.path).stem.lower()
    return entry.extension in SKELETON_EXTENSIONS or any(token in stem for token in ("skeleton", "skel", "rig", "armature"))


def normalize_model_stem(stem: str) -> str:
    return re.sub(r"(?:[_\-.](lod\d+|rig|skin|skeleton|skel|armature))+$", "", stem.lower()).strip()


def summarize_gltf_text(content: str) -> str:
    try:
        payload = json.loads(content)
    except Exception as exc:
        return f"glTF summary unavailable: {exc}"

    meshes = len(payload.get("meshes", []) or [])
    nodes = len(payload.get("nodes", []) or [])
    skins = len(payload.get("skins", []) or [])
    animations = len(payload.get("animations", []) or [])
    materials = len(payload.get("materials", []) or [])
    images = len(payload.get("images", []) or [])
    return (
        f"glTF summary: {meshes:,} meshes, {nodes:,} nodes, {skins:,} skins, "
        f"{animations:,} animations, {materials:,} materials, {images:,} images."
    )


def summarize_dae_text(content: str) -> str:
    geometry_count = len(re.findall(r"<geometry\b", content, re.IGNORECASE))
    controller_count = len(re.findall(r"<controller\b", content, re.IGNORECASE))
    animation_count = len(re.findall(r"<animation\b", content, re.IGNORECASE))
    image_count = len(re.findall(r"<image\b", content, re.IGNORECASE))
    return (
        f"DAE summary: {geometry_count:,} geometries, {controller_count:,} controllers, "
        f"{animation_count:,} animations, {image_count:,} images."
    )


def parse_obj_geometry(
    source_path: Path,
    *,
    max_vertices: int = 120_000,
    max_faces: int = 120_000,
) -> Tuple[List[Tuple[float, float, float]], List[Tuple[int, ...]], bool]:
    vertices: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []
    truncated = False

    with source_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("v "):
                if len(vertices) >= max_vertices:
                    truncated = True
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
            elif line.startswith("f "):
                if len(faces) >= max_faces:
                    truncated = True
                    continue
                indices: List[int] = []
                for token in line.split()[1:]:
                    head = token.split("/")[0]
                    if not head:
                        continue
                    try:
                        raw_index = int(head)
                    except ValueError:
                        continue
                    if raw_index < 0:
                        raw_index = len(vertices) + raw_index + 1
                    if raw_index <= 0:
                        continue
                    indices.append(raw_index - 1)
                if len(indices) >= 2:
                    faces.append(tuple(indices))

    return vertices, faces, truncated


def build_obj_edges(
    faces: Sequence[Tuple[int, ...]],
    *,
    max_edges: int = 180_000,
) -> Tuple[List[Tuple[int, int]], bool]:
    edges: List[Tuple[int, int]] = []
    seen: set[Tuple[int, int]] = set()
    truncated = False
    for face in faces:
        for index in range(len(face)):
            a = face[index]
            b = face[(index + 1) % len(face)]
            if a == b:
                continue
            edge = (a, b) if a < b else (b, a)
            if edge in seen:
                continue
            seen.add(edge)
            edges.append(edge)
            if len(edges) >= max_edges:
                truncated = True
                return edges, truncated
    return edges, truncated


def summarize_obj_text(content: str) -> str:
    vertices = 0
    texcoords = 0
    normals = 0
    faces = 0
    for raw_line in content.splitlines():
        line = raw_line.lstrip()
        if line.startswith("v "):
            vertices += 1
        elif line.startswith("vt "):
            texcoords += 1
        elif line.startswith("vn "):
            normals += 1
        elif line.startswith("f "):
            faces += 1
    return f"OBJ summary: {vertices:,} vertices, {texcoords:,} UVs, {normals:,} normals, {faces:,} faces."


def collect_folder_entry_map(entries: Sequence[ArchiveEntry], folder_path: PurePosixPath) -> Dict[str, ArchiveEntry]:
    result: Dict[str, ArchiveEntry] = {}
    for entry in entries:
        entry_path = PurePosixPath(entry.path.replace("\\", "/"))
        if entry_path.parent != folder_path:
            continue
        result[entry_path.name.lower()] = entry
    return result


def read_sidecar_text(entry: ArchiveEntry) -> Optional[str]:
    try:
        source_path, _note = ensure_archive_preview_source(entry)
        return source_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def collect_referenced_sidecars(
    entry: ArchiveEntry,
    source_path: Path,
    folder_entries: Dict[str, ArchiveEntry],
) -> List[ArchiveEntry]:
    referenced: List[ArchiveEntry] = []
    seen: set[str] = set()

    def add_by_name(filename: str) -> None:
        candidate = folder_entries.get(filename.lower())
        if candidate is None:
            return
        key = candidate.path.lower()
        if key in seen:
            return
        seen.add(key)
        referenced.append(candidate)

    extension = entry.extension.lower()
    if extension == ".obj":
        text = source_path.read_text(encoding="utf-8", errors="replace")
        mtl_names = re.findall(r"^\s*mtllib\s+(.+?)\s*$", text, re.MULTILINE)
        for raw_name in mtl_names:
            add_by_name(PurePosixPath(raw_name.strip()).name)
        for mtl_entry in list(referenced):
            if mtl_entry.extension.lower() != ".mtl":
                continue
            mtl_text = read_sidecar_text(mtl_entry)
            if not mtl_text:
                continue
            for match in re.findall(r"^\s*map_\w+\s+(.+?)\s*$", mtl_text, re.MULTILINE):
                add_by_name(PurePosixPath(match.strip()).name)
    elif extension == ".gltf":
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            payload = {}
        for buffer_ref in payload.get("buffers", []) or []:
            uri = buffer_ref.get("uri")
            if isinstance(uri, str) and uri and not uri.startswith("data:"):
                add_by_name(PurePosixPath(uri).name)
        for image_ref in payload.get("images", []) or []:
            uri = image_ref.get("uri")
            if isinstance(uri, str) and uri and not uri.startswith("data:"):
                add_by_name(PurePosixPath(uri).name)
    elif extension == ".dae":
        text = source_path.read_text(encoding="utf-8", errors="replace")
        for raw_name in re.findall(r"<init_from>(.*?)</init_from>", text, re.IGNORECASE):
            add_by_name(PurePosixPath(raw_name.strip()).name)

    return referenced


def build_model_bundle_entries(entry: ArchiveEntry, all_entries: Sequence[ArchiveEntry]) -> List[ArchiveEntry]:
    selected_path = PurePosixPath(entry.path.replace("\\", "/"))
    folder_path = selected_path.parent
    selected_stem = Path(selected_path.name).stem
    normalized_stem = normalize_model_stem(selected_stem)
    folder_entries = collect_folder_entry_map(all_entries, folder_path)
    bundle: Dict[str, ArchiveEntry] = {entry.path.lower(): entry}

    try:
        source_path, _note = ensure_archive_preview_source(entry)
    except Exception:
        source_path = None

    if source_path is not None:
        for referenced_entry in collect_referenced_sidecars(entry, source_path, folder_entries):
            bundle[referenced_entry.path.lower()] = referenced_entry

    for candidate in all_entries:
        candidate_path = PurePosixPath(candidate.path.replace("\\", "/"))
        if candidate_path.parent != folder_path:
            continue
        if candidate.extension.lower() not in MODEL_SIDECAR_EXTENSIONS:
            continue
        candidate_stem = Path(candidate_path.name).stem
        normalized_candidate = normalize_model_stem(candidate_stem)
        if normalized_candidate == normalized_stem:
            bundle[candidate.path.lower()] = candidate
            continue
        if has_likely_skeleton_role(candidate) and (
            normalized_candidate.startswith(normalized_stem) or normalized_stem.startswith(normalized_candidate)
        ):
            bundle[candidate.path.lower()] = candidate

    selected_key = entry.path.lower()
    return sorted(
        bundle.values(),
        key=lambda item: (0 if item.path.lower() == selected_key else 1, item.path.lower()),
    )


def prepare_model_entry_state(entries: Sequence[ArchiveEntry]) -> Dict[str, object]:
    model_entries = sorted(
        [entry for entry in entries if is_model_like_entry(entry)],
        key=lambda item: item.path.lower(),
    )
    model_entries_by_path = {entry.path.lower(): entry for entry in model_entries}
    folder_model_skeleton_entries: Dict[str, List[ArchiveEntry]] = {}
    for entry in model_entries:
        folder_key = PurePosixPath(entry.path.replace("\\", "/")).parent.as_posix().lower()
        folder_model_skeleton_entries.setdefault(folder_key, []).append(entry)
    return {
        "model_entries": model_entries,
        "model_entries_by_path": model_entries_by_path,
        "folder_model_skeleton_entries": folder_model_skeleton_entries,
    }


class ModelPreparationWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(self, request_id: int, entries: Sequence[ArchiveEntry]) -> None:
        super().__init__()
        self.request_id = request_id
        self.entries = list(entries)

    @Slot()
    def run(self) -> None:
        try:
            payload = prepare_model_entry_state(self.entries)
            self.completed.emit(self.request_id, payload)
        except Exception as exc:
            self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class ModelExportWorker(QObject):
    log_message = Signal(str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        entries: Sequence[ArchiveEntry],
        output_root: Path,
        *,
        description: str,
    ) -> None:
        super().__init__()
        self.entries = list(entries)
        self.output_root = output_root
        self.description = description

    @Slot()
    def run(self) -> None:
        try:
            self.log_message.emit(self.description)
            result = extract_archive_entries(
                self.entries,
                self.output_root,
                collision_mode="overwrite",
                on_log=self.log_message.emit,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class BlenderExportWorker(QObject):
    log_message = Signal(str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        entry: ArchiveEntry,
        all_entries: Sequence[ArchiveEntry],
        output_root: Path,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.all_entries = list(all_entries)
        self.output_root = output_root

    @Slot()
    def run(self) -> None:
        try:
            self.log_message.emit(f"Exporting Blender-ready model: {self.entry.path}")
            result = export_entry_for_blender(
                self.entry,
                self.all_entries,
                self.output_root,
                on_log=self.log_message.emit,
            )
            self.completed.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ModelPreviewWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(self, request_id: int, entry: ArchiveEntry) -> None:
        super().__init__()
        self.request_id = request_id
        self.entry = entry

    @Slot()
    def run(self) -> None:
        try:
            payload: Dict[str, object] = {"entry_path": self.entry.path, "extension": self.entry.extension.lower()}
            extension = self.entry.extension.lower()
            if extension == ".obj":
                source_path, note = ensure_archive_preview_source(self.entry)
                text = source_path.read_text(encoding="utf-8", errors="replace")
                summary = summarize_obj_text(text)
                vertices, faces, geometry_truncated = parse_obj_geometry(source_path)
                edges, edge_truncated = build_obj_edges(faces, max_edges=60_000)
                payload.update(
                    {
                        "mode": "wireframe",
                        "vertices": vertices,
                        "edges": edges,
                        "summary": summary,
                        "note": note,
                        "geometry_truncated": geometry_truncated,
                        "edge_truncated": edge_truncated,
                    }
                )
            elif extension in {".pac", ".pam"}:
                meshes = load_runtime_meshes_for_entry(self.entry)
                vertices, edges, stats = build_wireframe_preview(meshes, max_preview_triangles=14_000)
                payload.update(
                    {
                        "mode": "runtime_wireframe",
                        "vertices": vertices,
                        "edges": edges,
                        "stats": stats,
                    }
                )
            else:
                payload["mode"] = "noop"
            self.completed.emit(self.request_id, payload)
        except Exception as exc:
            self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class ObjWireframeViewer(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(420, 320)
        self._message = "Select a model to preview."
        self._vertices: List[Tuple[float, float, float]] = []
        self._edges: List[Tuple[int, int]] = []
        self._stats = ""
        self._center = (0.0, 0.0, 0.0)
        self._radius = 1.0
        self._rotation_x = -0.45
        self._rotation_y = 0.75
        self._zoom = 1.0
        self._last_mouse_pos = None
        self.setMouseTracking(True)

    def clear_scene(self, message: str) -> None:
        self._message = message
        self._vertices = []
        self._edges = []
        self._stats = ""
        self._center = (0.0, 0.0, 0.0)
        self._radius = 1.0
        self._zoom = 1.0
        self.update()

    def load_obj_geometry(
        self,
        vertices: Sequence[Tuple[float, float, float]],
        edges: Sequence[Tuple[int, int]],
        *,
        stats: str,
    ) -> None:
        self._vertices = list(vertices)
        self._edges = list(edges)
        self._stats = stats
        self._message = ""
        if self._vertices:
            xs = [vertex[0] for vertex in self._vertices]
            ys = [vertex[1] for vertex in self._vertices]
            zs = [vertex[2] for vertex in self._vertices]
            self._center = (
                (min(xs) + max(xs)) / 2.0,
                (min(ys) + max(ys)) / 2.0,
                (min(zs) + max(zs)) / 2.0,
            )
            max_radius = 0.0
            for x, y, z in self._vertices:
                dx = x - self._center[0]
                dy = y - self._center[1]
                dz = z - self._center[2]
                max_radius = max(max_radius, math.sqrt(dx * dx + dy * dy + dz * dz))
            self._radius = max(max_radius, 1.0)
        else:
            self._center = (0.0, 0.0, 0.0)
            self._radius = 1.0
        self._rotation_x = -0.45
        self._rotation_y = 0.75
        self._zoom = 1.0
        self.update()

    def reset_view(self) -> None:
        self._rotation_x = -0.45
        self._rotation_y = 0.75
        self._zoom = 1.0
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._last_mouse_pos = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._last_mouse_pos is not None and (event.buttons() & Qt.LeftButton):
            current = event.position().toPoint()
            delta = current - self._last_mouse_pos
            self._last_mouse_pos = current
            self._rotation_y += delta.x() * 0.01
            self._rotation_x += delta.y() * 0.01
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._last_mouse_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        if delta:
            self._zoom = min(max(self._zoom * (1.12 if delta > 0 else 1 / 1.12), 0.2), 8.0)
            self.update()
            event.accept()
            return
        super().wheelEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())

        if not self._vertices or not self._edges:
            painter.setPen(self.palette().text().color())
            painter.drawText(self.rect().adjusted(24, 24, -24, -24), Qt.AlignCenter | Qt.TextWordWrap, self._message)
            return

        width = max(1, self.width())
        height = max(1, self.height())
        cx = width / 2.0
        cy = height / 2.0
        scale = (min(width, height) * 0.42 / max(self._radius, 1e-6)) * self._zoom

        cos_y = math.cos(self._rotation_y)
        sin_y = math.sin(self._rotation_y)
        cos_x = math.cos(self._rotation_x)
        sin_x = math.sin(self._rotation_x)

        projected: List[Tuple[float, float, float]] = []
        for x, y, z in self._vertices:
            x -= self._center[0]
            y -= self._center[1]
            z -= self._center[2]
            ry_x = x * cos_y + z * sin_y
            ry_z = -x * sin_y + z * cos_y
            rx_y = y * cos_x - ry_z * sin_x
            rx_z = y * sin_x + ry_z * cos_x
            distance = 3.4
            perspective = distance / max(0.1, distance + rx_z)
            projected.append((cx + ry_x * scale * perspective, cy - rx_y * scale * perspective, rx_z))

        pen = QPen(self.palette().highlight().color(), 1.0)
        painter.setPen(pen)
        for a, b in self._edges:
            if not (0 <= a < len(projected) and 0 <= b < len(projected)):
                continue
            ax, ay, _az = projected[a]
            bx, by, _bz = projected[b]
            painter.drawLine(int(ax), int(ay), int(bx), int(by))

        if self._stats:
            overlay_rect = self.rect().adjusted(10, 10, -10, -10)
            painter.setPen(QColor(self.palette().text().color()))
            painter.drawText(overlay_rect, Qt.AlignLeft | Qt.AlignTop, self._stats)


class ModelBrowserTab(QWidget):
    def __init__(
        self,
        *,
        settings,
        append_log: Callable[[str], None],
        set_status_message: Callable[..., None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.append_log = append_log
        self.set_status_message = set_status_message
        self.archive_entries: List[ArchiveEntry] = []
        self.model_entries: List[ArchiveEntry] = []
        self.filtered_entries: List[ArchiveEntry] = []
        self.model_entries_by_path: Dict[str, ArchiveEntry] = {}
        self.folder_model_skeleton_entries: Dict[str, List[ArchiveEntry]] = {}
        self.bundle_cache: Dict[str, List[ArchiveEntry]] = {}
        self.skeleton_presence_cache: Dict[str, bool] = {}
        self.prepare_thread: Optional[QThread] = None
        self.prepare_worker: Optional[ModelPreparationWorker] = None
        self.export_thread: Optional[QThread] = None
        self.export_worker: Optional[QObject] = None
        self.preview_thread: Optional[QThread] = None
        self.preview_worker: Optional[ModelPreviewWorker] = None
        self.preview_request_id = 0
        self.pending_preview_entry: Optional[ArchiveEntry] = None
        self.external_busy = False
        self._settings_ready = False
        self._entries_dirty = False
        self._load_started = False
        self._prepare_request_id = 0
        self._apply_filters_after_prepare = False
        self._populating_tree = False
        self._populate_generation = 0
        self._populate_index = 0
        self._populate_selected_path = ""
        self._populate_selected_item: Optional[QTreeWidgetItem] = None
        self._populate_first_item: Optional[QTreeWidgetItem] = None
        self._stats_summary_text = "No archive scan available yet."
        self._visible_row_limit = 0
        self._populate_timer = QTimer(self)
        self._populate_timer.setSingleShot(True)
        self._populate_timer.timeout.connect(self._populate_tree_batch)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(10)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.splitter, stretch=1)

        controls_group = QGroupBox("Model Tools")
        controls_group.setMinimumWidth(380)
        controls_group.setMaximumWidth(470)
        controls_layout = QVBoxLayout(controls_group)
        controls_layout.setContentsMargins(12, 16, 12, 12)
        controls_layout.setSpacing(8)

        hint = QLabel(
            "Experimental model browser. It reuses the current archive scan, gives safe in-app previews for OBJ files, "
            "detects likely rig sidecars, and exports model bundles for Blender or external tools."
        )
        hint.setWordWrap(True)
        hint.setObjectName("HintLabel")
        controls_layout.addWidget(hint)

        export_root_label = QLabel("Export root")
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        self.export_root_edit = QLineEdit()
        self.export_root_browse_button = QPushButton("Browse")
        self.export_root_open_button = QPushButton("Open")
        export_row.addWidget(self.export_root_edit, stretch=1)
        export_row.addWidget(self.export_root_browse_button)
        export_row.addWidget(self.export_root_open_button)
        controls_layout.addWidget(export_root_label)
        controls_layout.addLayout(export_row)

        filter_grid = QGridLayout()
        filter_grid.setHorizontalSpacing(8)
        filter_grid.setVerticalSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Search model path, name, or folder")
        self.package_filter_edit = QLineEdit()
        self.package_filter_edit.setPlaceholderText("Package filter, e.g. 0000/0.pamt")
        self.format_combo = QComboBox()
        self.format_combo.addItem("All model files", "all")
        self.format_combo.addItem("Blender-ready only", "blender")
        self.format_combo.addItem("Proprietary/native only", "native")
        self.format_combo.addItem("OBJ only", ".obj")
        self.format_combo.addItem("glTF/glb only", "gltf")
        self.format_combo.addItem("DAE/FBX only", "exchange")
        self.format_combo.addItem("Skeleton files only", "skeleton")
        self.with_skeleton_only_checkbox = QCheckBox("Has related skeleton")
        self.bundle_related_checkbox = QCheckBox("Bundle related sidecars")
        self.bundle_related_checkbox.setChecked(True)
        self.apply_filter_button = QPushButton("Apply")
        self.clear_filter_button = QPushButton("Clear")

        filter_grid.addWidget(QLabel("Search"), 0, 0)
        filter_grid.addWidget(self.filter_edit, 0, 1, 1, 3)
        filter_grid.addWidget(QLabel("Package"), 1, 0)
        filter_grid.addWidget(self.package_filter_edit, 1, 1, 1, 3)
        filter_grid.addWidget(QLabel("Format"), 2, 0)
        filter_grid.addWidget(self.format_combo, 2, 1)
        filter_grid.addWidget(self.with_skeleton_only_checkbox, 2, 2)
        filter_grid.addWidget(self.bundle_related_checkbox, 2, 3)
        controls_layout.addLayout(filter_grid)

        filter_actions_row = QHBoxLayout()
        filter_actions_row.setSpacing(8)
        filter_actions_row.addStretch(1)
        filter_actions_row.addWidget(self.apply_filter_button)
        filter_actions_row.addWidget(self.clear_filter_button)
        controls_layout.addLayout(filter_actions_row)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.export_selected_button = QPushButton("Export Selected Raw")
        self.export_bundle_button = QPushButton("Export Selected Bundle")
        self.export_blender_button = QPushButton("Export For Blender")
        actions_row.addWidget(self.export_selected_button)
        actions_row.addWidget(self.export_bundle_button)
        actions_row.addWidget(self.export_blender_button)
        controls_layout.addLayout(actions_row)

        self.stats_label = QLabel("No archive scan available yet.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setObjectName("HintLabel")
        controls_layout.addWidget(self.stats_label)

        model_log_label_row = QHBoxLayout()
        model_log_label_row.setSpacing(8)
        model_log_label = QLabel("Model Export Log")
        model_log_label.setObjectName("HintLabel")
        self.clear_log_button = QPushButton("Clear")
        model_log_label_row.addWidget(model_log_label)
        model_log_label_row.addStretch(1)
        model_log_label_row.addWidget(self.clear_log_button)
        controls_layout.addLayout(model_log_label_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        self.log_view.document().setMaximumBlockCount(1500)
        controls_layout.addWidget(self.log_view, stretch=1)
        self.splitter.addWidget(controls_group)

        files_group = QGroupBox("Model Files")
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(10, 12, 10, 10)
        self.model_tree = QTreeWidget()
        self.model_tree.setAlternatingRowColors(True)
        self.model_tree.setRootIsDecorated(False)
        self.model_tree.setUniformRowHeights(True)
        self.model_tree.setHeaderLabels(["Name", "Type", "Size", "Package", "Blender", "Skeleton"])
        header = self.model_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        files_layout.addWidget(self.model_tree)
        self.splitter.addWidget(files_group)

        preview_group = QGroupBox("Model Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(10, 12, 10, 10)
        preview_layout.setSpacing(8)

        preview_header = QHBoxLayout()
        preview_header.setSpacing(8)
        self.selected_summary_label = QLabel("Select a model-like archive file to inspect it.")
        self.selected_summary_label.setWordWrap(True)
        self.reset_view_button = QPushButton("Reset View")
        preview_header.addWidget(self.selected_summary_label, stretch=1)
        preview_header.addWidget(self.reset_view_button)
        preview_layout.addLayout(preview_header)

        self.preview_tabs = QTabWidget()
        viewer_tab = QWidget()
        viewer_tab_layout = QVBoxLayout(viewer_tab)
        viewer_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.obj_viewer = ObjWireframeViewer()
        viewer_tab_layout.addWidget(self.obj_viewer, stretch=1)
        viewer_hint = QLabel(
            "OBJ files render here as a simple wireframe. Other formats are summarized and exported, "
            "but not rendered in-app yet."
        )
        viewer_hint.setWordWrap(True)
        viewer_hint.setObjectName("HintLabel")
        viewer_tab_layout.addWidget(viewer_hint)
        self.preview_tabs.addTab(viewer_tab, "Viewer")

        details_tab = QWidget()
        details_layout = QVBoxLayout(details_tab)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        details_layout.addWidget(self.details_view)
        self.preview_tabs.addTab(details_tab, "Details")

        related_tab = QWidget()
        related_layout = QVBoxLayout(related_tab)
        related_layout.setContentsMargins(0, 0, 0, 0)
        self.related_tree = QTreeWidget()
        self.related_tree.setRootIsDecorated(False)
        self.related_tree.setHeaderLabels(["Related file", "Type", "Reason"])
        self.related_tree.header().setStretchLastSection(True)
        related_layout.addWidget(self.related_tree)
        self.preview_tabs.addTab(related_tab, "Related")

        notes_tab = QWidget()
        notes_layout = QVBoxLayout(notes_tab)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setPlainText(
            "Experimental model support\n\n"
            "- OBJ files get an in-app wireframe viewer.\n"
            "- glTF/DAE/FBX/GLB are summarized and exported, but not rendered in-app yet.\n"
            "- Proprietary formats such as .mesh/.model/.pat/.patx are exported raw.\n"
            "- Armature/skeleton support here means detection and bundle export of likely rig sidecar files, "
            "not full rig reconstruction for every proprietary Crimson Desert format."
        )
        notes_layout.addWidget(self.notes_view)
        self.preview_tabs.addTab(notes_tab, "Notes")

        preview_layout.addWidget(self.preview_tabs, stretch=1)
        self.splitter.addWidget(preview_group)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setStretchFactor(2, 2)
        self.splitter.setSizes([410, 580, 760])

        self.export_root_browse_button.clicked.connect(self._browse_export_root)
        self.export_root_open_button.clicked.connect(self._open_export_root)
        self.apply_filter_button.clicked.connect(self.apply_filters)
        self.clear_filter_button.clicked.connect(self.clear_filters)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.model_tree.itemSelectionChanged.connect(self._handle_selection_change)
        self.related_tree.itemDoubleClicked.connect(self._handle_related_item_activated)
        self.export_selected_button.clicked.connect(self.export_selected_raw)
        self.export_bundle_button.clicked.connect(self.export_selected_bundle)
        self.export_blender_button.clicked.connect(self.export_selected_blender)
        self.reset_view_button.clicked.connect(self.obj_viewer.reset_view)
        self.filter_edit.returnPressed.connect(self.apply_filters)
        self.package_filter_edit.returnPressed.connect(self.apply_filters)

        self.export_root_edit.textChanged.connect(self._save_settings)
        self.filter_edit.textChanged.connect(self._save_settings)
        self.package_filter_edit.textChanged.connect(self._save_settings)
        self.format_combo.currentIndexChanged.connect(self._save_settings)
        self.with_skeleton_only_checkbox.stateChanged.connect(self._save_settings)
        self.bundle_related_checkbox.stateChanged.connect(self._save_settings)
        self.preview_tabs.currentChanged.connect(self._save_settings)

        self._load_settings()
        self._settings_ready = True
        self._apply_enabled_state()

    def _save_settings(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("models/export_root", self.export_root_edit.text())
        self.settings.setValue("models/filter_text", self.filter_edit.text())
        self.settings.setValue("models/package_filter_text", self.package_filter_edit.text())
        self.settings.setValue("models/format_filter", self._combo_value(self.format_combo))
        self.settings.setValue("models/with_skeleton_only", self.with_skeleton_only_checkbox.isChecked())
        self.settings.setValue("models/bundle_related", self.bundle_related_checkbox.isChecked())
        self.settings.setValue("models/preview_tab_index", self.preview_tabs.currentIndex())
        self.settings.sync()

    def _load_settings(self) -> None:
        export_root_default = str(self.settings.value("archive/extract_root", ""))
        self.export_root_edit.setText(str(self.settings.value("models/export_root", export_root_default)))
        self.filter_edit.setText(str(self.settings.value("models/filter_text", "")))
        self.package_filter_edit.setText(str(self.settings.value("models/package_filter_text", "")))
        self._set_combo_by_value(self.format_combo, str(self.settings.value("models/format_filter", "all")))
        self.with_skeleton_only_checkbox.setChecked(self._read_bool("models/with_skeleton_only", False))
        self.bundle_related_checkbox.setChecked(self._read_bool("models/bundle_related", True))
        preview_tab = int(self.settings.value("models/preview_tab_index", 0))
        self.preview_tabs.setCurrentIndex(max(0, min(preview_tab, self.preview_tabs.count() - 1)))

    def _read_bool(self, key: str, default: bool) -> bool:
        value = self.settings.value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _set_combo_by_value(self, combo: QComboBox, value: str) -> None:
        for index in range(combo.count()):
            if str(combo.itemData(index)) == value:
                combo.setCurrentIndex(index)
                return

    def _combo_value(self, combo: QComboBox) -> str:
        data = combo.currentData()
        return "" if data is None else str(data)

    def _append_model_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.append_log(f"[Models] {message}")

    def _apply_enabled_state(self) -> None:
        worker_busy = (
            self.prepare_thread is not None
            or self.export_thread is not None
            or self.preview_thread is not None
            or self._populating_tree
        )
        enabled = not self.external_busy and not worker_busy
        self.export_root_edit.setEnabled(enabled)
        self.export_root_browse_button.setEnabled(enabled)
        self.export_root_open_button.setEnabled(True)
        self.filter_edit.setEnabled(enabled)
        self.package_filter_edit.setEnabled(enabled)
        self.format_combo.setEnabled(enabled)
        self.with_skeleton_only_checkbox.setEnabled(enabled)
        self.bundle_related_checkbox.setEnabled(enabled)
        self.apply_filter_button.setEnabled(enabled)
        self.clear_filter_button.setEnabled(enabled)
        self.model_tree.setEnabled(enabled)
        self.related_tree.setEnabled(enabled)
        selected_entry = self._selected_entry()
        selected_exists = selected_entry is not None
        self.export_selected_button.setEnabled(enabled and selected_exists)
        self.export_bundle_button.setEnabled(enabled and selected_exists)
        self.export_blender_button.setEnabled(
            enabled and selected_entry is not None and selected_entry.extension.lower() in {".pac", ".pam"}
        )
        self.reset_view_button.setEnabled(True)

    def set_external_busy(self, busy: bool) -> None:
        self.external_busy = busy
        self._apply_enabled_state()

    def shutdown(self) -> None:
        self._populate_timer.stop()
        if self.prepare_thread is not None:
            self.prepare_thread.quit()
            self.prepare_thread.wait(3000)
        if self.export_thread is not None:
            self.export_thread.quit()
            self.export_thread.wait(3000)
        if self.preview_thread is not None:
            self.preview_thread.quit()
            self.preview_thread.wait(3000)

    def set_archive_entries(self, entries: Sequence[ArchiveEntry]) -> None:
        self._populate_timer.stop()
        self._populating_tree = False
        self._prepare_request_id += 1
        self.archive_entries = entries if isinstance(entries, list) else list(entries)
        self._entries_dirty = True
        self._load_started = False
        self._apply_filters_after_prepare = False
        self.model_entries = []
        self.filtered_entries = []
        self.model_entries_by_path = {}
        self.folder_model_skeleton_entries = {}
        self.bundle_cache.clear()
        self.skeleton_presence_cache.clear()
        self.model_tree.clear()
        self.related_tree.clear()
        self.obj_viewer.clear_scene("Open the Models tab to prepare model entries.")
        self.details_view.setPlainText("Open the Models tab to prepare model entries from the current archive scan.")
        self._stats_summary_text = (
            f"Archive scan contains {len(self.archive_entries):,} entries. Model preparation is deferred until you open this tab."
        )
        self.stats_label.setText(self._stats_summary_text)
        self._apply_enabled_state()

    def activate(self) -> None:
        if self._entries_dirty:
            self._prepare_model_entries()

    def _prepare_model_entries(self) -> None:
        if self._load_started or self.prepare_thread is not None:
            return
        self._load_started = True
        request_id = self._prepare_request_id
        self._stats_summary_text = "Preparing model entries from the archive scan..."
        self.stats_label.setText(self._stats_summary_text)
        self._append_model_log("Preparing model entries from the current archive scan...")
        worker = ModelPreparationWorker(request_id, self.archive_entries)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_prepare_ready)
        worker.error.connect(self._handle_prepare_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_prepare_refs)
        self.prepare_worker = worker
        self.prepare_thread = thread
        self._apply_enabled_state()
        thread.start()

    def _handle_prepare_ready(self, request_id: int, payload: object) -> None:
        if request_id != self._prepare_request_id or not isinstance(payload, dict):
            return
        self.model_entries = payload.get("model_entries", []) if isinstance(payload.get("model_entries"), list) else []
        self.model_entries_by_path = (
            payload.get("model_entries_by_path", {})
            if isinstance(payload.get("model_entries_by_path"), dict)
            else {}
        )
        self.folder_model_skeleton_entries = (
            payload.get("folder_model_skeleton_entries", {})
            if isinstance(payload.get("folder_model_skeleton_entries"), dict)
            else {}
        )
        self.bundle_cache.clear()
        self.skeleton_presence_cache.clear()
        self._entries_dirty = False
        self._append_model_log(f"Prepared {len(self.model_entries):,} model-like archive entries.")
        self._apply_filters_after_prepare = True

    def _handle_prepare_error(self, request_id: int, message: str) -> None:
        if request_id != self._prepare_request_id:
            return
        self._entries_dirty = True
        self._load_started = False
        self._apply_filters_after_prepare = False
        self._stats_summary_text = f"Model preparation failed: {message}"
        self.stats_label.setText(self._stats_summary_text)
        self._append_model_log(f"ERROR: {message}")
        self.set_status_message(f"Model preparation failed: {message}", error=True)

    def _cleanup_prepare_refs(self) -> None:
        self.prepare_thread = None
        self.prepare_worker = None
        if self._apply_filters_after_prepare:
            self._apply_filters_after_prepare = False
            self.apply_filters()
            return
        self._apply_enabled_state()

    def clear_filters(self) -> None:
        self.filter_edit.clear()
        self.package_filter_edit.clear()
        self._set_combo_by_value(self.format_combo, "all")
        self.with_skeleton_only_checkbox.setChecked(False)
        self.apply_filters()

    def _matches_format_filter(self, entry: ArchiveEntry, format_value: str) -> bool:
        extension = entry.extension.lower()
        if format_value == "all":
            return True
        if format_value == "blender":
            return is_blender_ready_entry(entry) or extension in {".pac", ".pam"}
        if format_value == "native":
            return not (is_blender_ready_entry(entry) or extension in {".pac", ".pam"}) and not has_likely_skeleton_role(entry)
        if format_value == ".obj":
            return extension == ".obj"
        if format_value == "gltf":
            return extension in {".gltf", ".glb"}
        if format_value == "exchange":
            return extension in {".dae", ".fbx"}
        if format_value == "skeleton":
            return has_likely_skeleton_role(entry)
        return True

    def _has_related_skeleton(self, entry: ArchiveEntry) -> bool:
        cache_key = entry.path.lower()
        if cache_key in self.skeleton_presence_cache:
            return self.skeleton_presence_cache[cache_key]
        if has_likely_skeleton_role(entry):
            self.skeleton_presence_cache[cache_key] = True
            return True
        folder_key = PurePosixPath(entry.path.replace("\\", "/")).parent.as_posix().lower()
        siblings = self.folder_model_skeleton_entries.get(folder_key, [])
        normalized = normalize_model_stem(Path(entry.path).stem)
        for candidate in siblings:
            if candidate.path.lower() == cache_key:
                continue
            if not has_likely_skeleton_role(candidate):
                continue
            candidate_stem = normalize_model_stem(Path(candidate.path).stem)
            if candidate_stem == normalized or candidate_stem.startswith(normalized) or normalized.startswith(candidate_stem):
                self.skeleton_presence_cache[cache_key] = True
                return True
        self.skeleton_presence_cache[cache_key] = False
        return False

    def apply_filters(self) -> None:
        if self._entries_dirty:
            self.activate()
            return
        if self.prepare_thread is not None:
            self._stats_summary_text = "Preparing model entries from the archive scan..."
            self.stats_label.setText(self._stats_summary_text)
            return

        search_text = self.filter_edit.text().strip().lower()
        package_text = self.package_filter_edit.text().strip().lower()
        format_value = self._combo_value(self.format_combo)
        require_skeleton = self.with_skeleton_only_checkbox.isChecked()

        filtered: List[ArchiveEntry] = []
        for entry in self.model_entries:
            entry_path_lower = entry.path.lower()
            if search_text and search_text not in entry_path_lower:
                continue
            if package_text and package_text not in entry.package_label.lower() and package_text not in str(entry.pamt_path).lower():
                continue
            if not self._matches_format_filter(entry, format_value):
                continue
            if require_skeleton and not self._has_related_skeleton(entry):
                continue
            filtered.append(entry)

        self.filtered_entries = filtered
        self._populate_tree()
        blender_ready_count = sum(1 for entry in filtered if is_blender_ready_entry(entry) or entry.extension.lower() in {".pac", ".pam"})
        if require_skeleton:
            skeleton_text = f"Likely skeleton matches: {len(filtered):,}."
        elif len(filtered) <= MODEL_SKELETON_COUNT_LIMIT:
            skeleton_count = sum(1 for entry in filtered if self._has_related_skeleton(entry))
            skeleton_text = f"With likely skeleton sidecars: {skeleton_count:,}."
        else:
            skeleton_text = "Skeleton sidecar counts are deferred for large result sets."
        self._stats_summary_text = (
            f"{len(filtered):,} shown / {len(self.model_entries):,} model-like archive entries. "
            f"Blender-ready: {blender_ready_count:,}. {skeleton_text}"
        )
        self.stats_label.setText(self._stats_summary_text)
        self._save_settings()
        self._apply_enabled_state()

    def _populate_tree(self) -> None:
        self._populate_generation += 1
        self._populate_timer.stop()
        selected_path = None
        current_item = self.model_tree.currentItem()
        if current_item is not None:
            selected_path = str(current_item.data(0, Qt.UserRole) or "")
        self._populate_index = 0
        self._populate_selected_path = (selected_path or "").lower()
        self._populate_selected_item = None
        self._populate_first_item = None
        self._visible_row_limit = len(self.filtered_entries)
        self._populating_tree = True
        self.model_tree.clear()
        if not self.filtered_entries:
            self._populating_tree = False
            self.stats_label.setText(self._stats_summary_text)
            self._clear_preview("Scan archives and select a model-like file to inspect it.")
            self._apply_enabled_state()
            return
        self.stats_label.setText(
            f"{self._stats_summary_text}\nRendering model list... 0 / {len(self.filtered_entries):,}."
        )
        self._apply_enabled_state()
        self._populate_timer.start(0)

    def _populate_tree_batch(self) -> None:
        generation = self._populate_generation
        total = len(self.filtered_entries)
        end_index = min(self._populate_index + MODEL_TREE_BATCH_SIZE, total)
        require_skeleton = self.with_skeleton_only_checkbox.isChecked()

        for entry in self.filtered_entries[self._populate_index : end_index]:
            path_key = entry.path.lower()
            skeleton_text = ""
            if require_skeleton or has_likely_skeleton_role(entry):
                skeleton_text = "Yes"
            elif self.skeleton_presence_cache.get(path_key):
                skeleton_text = "Yes"
            item = QTreeWidgetItem(
                [
                    entry.path,
                    entry.extension or "(none)",
                    format_byte_size(entry.orig_size),
                    entry.package_label,
                    "Yes" if is_blender_ready_entry(entry) or entry.extension.lower() in {".pac", ".pam"} else "Raw",
                    skeleton_text,
                ]
            )
            item.setData(0, Qt.UserRole, entry.path)
            self.model_tree.addTopLevelItem(item)
            if self._populate_first_item is None:
                self._populate_first_item = item
            if self._populate_selected_path and path_key == self._populate_selected_path:
                self._populate_selected_item = item

        self._populate_index = end_index
        self.stats_label.setText(
            f"{self._stats_summary_text}\nRendering model list... {self._populate_index:,} / {total:,}."
        )

        if self._populate_index < total and generation == self._populate_generation:
            self._populate_timer.start(0)
            return

        self._populating_tree = False
        self.stats_label.setText(self._stats_summary_text)
        target_item = self._populate_selected_item or self._populate_first_item
        if target_item is not None:
            self.model_tree.setCurrentItem(target_item)
        else:
            self._clear_preview("Scan archives and select a model-like file to inspect it.")
        self._apply_enabled_state()

    def _selected_entry(self) -> Optional[ArchiveEntry]:
        item = self.model_tree.currentItem()
        if item is None:
            return None
        path = str(item.data(0, Qt.UserRole) or "").lower()
        return self.model_entries_by_path.get(path)

    def _bundle_entries_for(self, entry: ArchiveEntry) -> List[ArchiveEntry]:
        cache_key = entry.path.lower()
        cached = self.bundle_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        bundle = build_model_bundle_entries(entry, self.archive_entries)
        self.bundle_cache[cache_key] = list(bundle)
        return bundle

    def _build_related_reason(self, selected_entry: ArchiveEntry, related_entry: ArchiveEntry) -> str:
        if related_entry.path.lower() == selected_entry.path.lower():
            return "Selected model"
        if has_likely_skeleton_role(related_entry):
            return "Likely skeleton / armature sidecar"
        if related_entry.extension.lower() in {".mtl", ".material", ".json", ".bin"}:
            return "Referenced material / buffer sidecar"
        if related_entry.extension.lower() in {".png", ".jpg", ".jpeg", ".tga", ".dds"}:
            return "Likely texture sidecar"
        return "Same-folder related asset"

    def _populate_related_tree(self, entry: ArchiveEntry, bundle_entries: Sequence[ArchiveEntry]) -> None:
        self.related_tree.clear()
        for related_entry in bundle_entries:
            item = QTreeWidgetItem(
                [
                    related_entry.path,
                    related_entry.extension or "(none)",
                    self._build_related_reason(entry, related_entry),
                ]
            )
            item.setData(0, Qt.UserRole, related_entry.path)
            self.related_tree.addTopLevelItem(item)

    def _clear_preview(self, message: str) -> None:
        self.selected_summary_label.setText(message)
        self.obj_viewer.clear_scene(message)
        self.details_view.setPlainText(message)
        self.related_tree.clear()

    def _handle_selection_change(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._clear_preview("Select a model-like archive file to inspect it.")
            self._apply_enabled_state()
            return
        self._show_preview_for_entry(entry)
        self._apply_enabled_state()

    def _handle_related_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        related_path = str(item.data(0, Qt.UserRole) or "").lower()
        target_entry = self.model_entries_by_path.get(related_path)
        if target_entry is None:
            return
        for row in range(self.model_tree.topLevelItemCount()):
            candidate = self.model_tree.topLevelItem(row)
            if str(candidate.data(0, Qt.UserRole) or "").lower() == related_path:
                self.model_tree.setCurrentItem(candidate)
                return

    def _build_detail_text(self, entry: ArchiveEntry, extra_lines: Sequence[str]) -> str:
        combined = [
            f"Blender-ready: {'Yes' if is_blender_ready_entry(entry) or entry.extension.lower() in {'.pac', '.pam'} else 'No'}",
            f"Likely skeleton sidecar present: {'Yes' if self._has_related_skeleton(entry) else 'No'}",
        ]
        combined.extend(line for line in extra_lines if line)
        return build_archive_entry_detail_text(entry, "\n".join(combined))

    def _show_preview_for_entry(self, entry: ArchiveEntry) -> None:
        bundle_entries = self._bundle_entries_for(entry)
        self._populate_related_tree(entry, bundle_entries)
        self.selected_summary_label.setText(
            f"{entry.path} | {format_byte_size(entry.orig_size)} | {entry.package_label}"
        )
        if entry.extension.lower() in {".obj", ".pac", ".pam"}:
            self.preview_request_id += 1
            self.obj_viewer.clear_scene("Loading model preview...")
            self.details_view.setPlainText(self._build_detail_text(entry, ["Building preview geometry..."]))
            if self.preview_thread is not None:
                self.pending_preview_entry = entry
                return
            self._start_preview_worker(self.preview_request_id, entry)
            return
        self._render_preview(entry)

    def _render_preview(self, entry: ArchiveEntry) -> None:
        bundle_entries = self._bundle_entries_for(entry)
        self._populate_related_tree(entry, bundle_entries)
        self.selected_summary_label.setText(
            f"{entry.path} | {format_byte_size(entry.orig_size)} | {entry.package_label}"
        )

        detail_lines: List[str] = []
        preview_message = ""

        try:
            source_path, note = ensure_archive_preview_source(entry)
        except Exception as exc:
            note = ""
            source_path = None
            preview_message = f"Preview source unavailable: {exc}"

        if note:
            detail_lines.append(note)

        extension = entry.extension.lower()

        if source_path is None:
            self.obj_viewer.clear_scene(preview_message or "Preview source unavailable.")
            self.details_view.setPlainText(self._build_detail_text(entry, [preview_message]))
            self.preview_tabs.setCurrentIndex(1)
            return

        if extension == ".gltf":
            try:
                gltf_text = source_path.read_text(encoding="utf-8", errors="replace")
                detail_lines.append(summarize_gltf_text(gltf_text))
            except Exception as exc:
                detail_lines.append(f"glTF summary unavailable: {exc}")
            self.obj_viewer.clear_scene(
                "glTF is export-ready, but this first version only provides summary/export support, not live 3D rendering."
            )
            self.preview_tabs.setCurrentIndex(1)
        elif extension == ".dae":
            try:
                dae_text = source_path.read_text(encoding="utf-8", errors="replace")
                detail_lines.append(summarize_dae_text(dae_text))
            except Exception as exc:
                detail_lines.append(f"DAE summary unavailable: {exc}")
            self.obj_viewer.clear_scene(
                "DAE is export-ready, but this first version only provides summary/export support, not live 3D rendering."
            )
            self.preview_tabs.setCurrentIndex(1)
        elif extension in {".fbx", ".glb"}:
            self.obj_viewer.clear_scene(
                f"{entry.extension} is likely Blender-ready after export, but this first version does not render it in-app yet."
            )
            detail_lines.append("This format is export-focused in the current build. Use Export Selected Raw or Export Selected Bundle.")
            self.preview_tabs.setCurrentIndex(1)
        elif has_likely_skeleton_role(entry):
            self.obj_viewer.clear_scene(
                "Likely skeleton/armature sidecar detected. The app can export it alongside related meshes, but does not reconstruct or preview rigs in-app yet."
            )
            detail_lines.append(
                "Skeleton support is currently export-oriented. Use Export Selected Bundle to keep likely rig sidecars together."
            )
            self.preview_tabs.setCurrentIndex(1)
        else:
            self.obj_viewer.clear_scene(
                f"{entry.extension} appears to be a proprietary/native model format. Export it for external reverse-engineering or conversion tools."
            )
            detail_lines.append(
                "This format is treated as raw/native. The app can export it, but does not convert or render it in-app."
            )
            self.preview_tabs.setCurrentIndex(1)

        self.details_view.setPlainText(self._build_detail_text(entry, detail_lines))

    def _start_preview_worker(self, request_id: int, entry: ArchiveEntry) -> None:
        worker = ModelPreviewWorker(request_id, entry)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_preview_ready)
        worker.error.connect(self._handle_preview_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_preview_refs)
        self.preview_worker = worker
        self.preview_thread = thread
        self._apply_enabled_state()
        thread.start()

    def _handle_preview_ready(self, request_id: int, payload: object) -> None:
        if request_id != self.preview_request_id or not isinstance(payload, dict):
            return
        entry = self._selected_entry()
        if entry is None:
            return

        mode = str(payload.get("mode", ""))
        detail_lines: List[str] = []
        note = str(payload.get("note", "")).strip()
        if note:
            detail_lines.append(note)

        if mode == "wireframe":
            vertices = payload.get("vertices") or []
            edges = payload.get("edges") or []
            summary = str(payload.get("summary", "OBJ preview"))
            truncation_notes: List[str] = []
            if payload.get("geometry_truncated"):
                truncation_notes.append("vertex/face limit reached")
            if payload.get("edge_truncated"):
                truncation_notes.append("edge limit reached")
            stats = summary
            if truncation_notes:
                stats = f"{stats}\nPreview simplified: {', '.join(truncation_notes)}."
            if vertices and edges:
                self.obj_viewer.load_obj_geometry(vertices, edges, stats=stats)
                detail_lines.append(summary)
                self.preview_tabs.setCurrentIndex(0)
            else:
                self.obj_viewer.clear_scene("OBJ file loaded, but no previewable geometry was found.")
                detail_lines.append(summary)
                detail_lines.append("OBJ geometry could not be rendered in the wireframe preview.")
                self.preview_tabs.setCurrentIndex(1)
        elif mode == "runtime_wireframe":
            vertices = payload.get("vertices") or []
            edges = payload.get("edges") or []
            stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
            meshes = int(stats.get("meshes", 0))
            total_vertices = int(stats.get("vertices", 0))
            total_triangles = int(stats.get("triangles", 0))
            sampled_triangles = int(stats.get("sampled_triangles", 0))
            summary = (
                f"Runtime model preview: {meshes:,} meshes, {total_vertices:,} vertices, "
                f"{total_triangles:,} triangles."
            )
            if sampled_triangles and sampled_triangles < total_triangles:
                summary += f"\nPreview simplified to {sampled_triangles:,} sampled triangles for responsiveness."
            if vertices and edges:
                self.obj_viewer.load_obj_geometry(vertices, edges, stats=summary)
                detail_lines.append(summary)
                detail_lines.append("Preview generated from reconstructed PAC/PAM geometry.")
                self.preview_tabs.setCurrentIndex(0)
            else:
                self.obj_viewer.clear_scene("The model parsed successfully, but no previewable geometry was produced.")
                detail_lines.append(summary)
                detail_lines.append("No previewable geometry was produced.")
                self.preview_tabs.setCurrentIndex(1)

        self.details_view.setPlainText(self._build_detail_text(entry, detail_lines))

    def _handle_preview_error(self, request_id: int, message: str) -> None:
        if request_id != self.preview_request_id:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        self.obj_viewer.clear_scene(message)
        self.details_view.setPlainText(self._build_detail_text(entry, [message]))
        self.preview_tabs.setCurrentIndex(1)

    def _cleanup_preview_refs(self) -> None:
        self.preview_thread = None
        self.preview_worker = None
        self._apply_enabled_state()
        if self.pending_preview_entry is not None:
            next_entry = self.pending_preview_entry
            self.pending_preview_entry = None
            self.preview_request_id += 1
            self._start_preview_worker(self.preview_request_id, next_entry)

    def _browse_export_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select model export root", self.export_root_edit.text().strip())
        if directory:
            self.export_root_edit.setText(directory)
            self._save_settings()

    def _open_export_root(self) -> None:
        path_text = self.export_root_edit.text().strip()
        if not path_text:
            self.set_status_message("Set a model export root first.", error=True)
            return
        export_root = Path(path_text).expanduser()
        export_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(export_root)))

    def _resolve_export_root(self) -> Optional[Path]:
        path_text = self.export_root_edit.text().strip()
        if not path_text:
            directory = QFileDialog.getExistingDirectory(self, "Select model export root")
            if not directory:
                return None
            self.export_root_edit.setText(directory)
            path_text = directory
        export_root = Path(path_text).expanduser()
        export_root.mkdir(parents=True, exist_ok=True)
        self._save_settings()
        return export_root

    def _start_export(self, entries: Sequence[ArchiveEntry], description: str) -> None:
        if not entries:
            self.set_status_message("No model files selected for export.", error=True)
            return
        if self.export_thread is not None:
            self.set_status_message("A model export is already running.", error=True)
            return
        output_root = self._resolve_export_root()
        if output_root is None:
            return

        worker = ModelExportWorker(entries, output_root, description=description)
        self._launch_export_worker(worker)

    def _start_blender_export(self, entry: ArchiveEntry) -> None:
        if self.export_thread is not None:
            self.set_status_message("A model export is already running.", error=True)
            return
        output_root = self._resolve_export_root()
        if output_root is None:
            return
        worker = BlenderExportWorker(entry, self.archive_entries, output_root)
        self._launch_export_worker(worker)

    def _launch_export_worker(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_message.connect(self._append_model_log)
        worker.completed.connect(self._handle_export_complete)
        worker.error.connect(self._handle_export_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_export_refs)

        self.export_worker = worker
        self.export_thread = thread
        self._apply_enabled_state()
        thread.start()

    def export_selected_raw(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.set_status_message("Select a model-like archive file to export.", error=True)
            return
        self._start_export([entry], f"Exporting raw model file: {entry.path}")

    def export_selected_bundle(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.set_status_message("Select a model-like archive file to export.", error=True)
            return
        bundle = self._bundle_entries_for(entry) if self.bundle_related_checkbox.isChecked() else [entry]
        self._start_export(bundle, f"Exporting model bundle: {entry.path} ({len(bundle)} file(s))")

    def export_selected_blender(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.set_status_message("Select a model-like archive file to export.", error=True)
            return
        if entry.extension.lower() not in {".pac", ".pam"}:
            self.set_status_message(
                "Blender export is currently available for PAC and PAM files. Use Raw or Bundle export for other formats.",
                error=True,
            )
            return
        self._start_blender_export(entry)

    def _handle_export_complete(self, result: object) -> None:
        payload = result if isinstance(result, dict) else {}
        if "obj_path" in payload:
            summary = (
                f"Blender export complete. Meshes={int(payload.get('mesh_count', 0)):,}, "
                f"vertices={int(payload.get('vertex_count', 0)):,}, triangles={int(payload.get('triangle_count', 0)):,}, "
                f"textures={int(payload.get('texture_count', 0)):,}."
            )
            output_dir = str(payload.get("output_dir", "")).strip()
            if output_dir:
                summary = f"{summary} Output: {output_dir}"
        else:
            extracted = int(payload.get("extracted", 0))
            failed = int(payload.get("failed", 0))
            decompressed = int(payload.get("decompressed", 0))
            renamed = int(payload.get("renamed", 0))
            summary = (
                f"Model export complete. Extracted {extracted:,} file(s), "
                f"decompressed {decompressed:,}, renamed {renamed:,}, failed {failed:,}."
            )
        self._append_model_log(summary)
        self.set_status_message(summary, error=bool(payload.get("failed", 0)))

    def _handle_export_error(self, message: str) -> None:
        self._append_model_log(f"ERROR: {message}")
        self.set_status_message(message, error=True)
        QMessageBox.warning(self, "Model export failed", message)

    def _cleanup_export_refs(self) -> None:
        self.export_thread = None
        self.export_worker = None
        self._apply_enabled_state()
