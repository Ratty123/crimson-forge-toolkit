from __future__ import annotations

import hashlib
import threading
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from crimson_texture_forge.core.archive import build_archive_tree_index
from crimson_texture_forge.core.research import (
    MaterialTextureReferenceRow,
    MipAnalysisRow,
    NormalValidationRow,
    ResearchNote,
    SidecarDiscoveryRow,
    TextureClassificationRow,
    TextureSetGroup,
    TextureUsageHeatRow,
    analyze_mip_behavior,
    build_archive_research_snapshot,
    build_texture_usage_heatmap,
    build_mip_analysis_detail,
    build_normal_validation_detail,
    bundle_texture_sets,
    classify_texture_entries,
    delete_research_note,
    discover_archive_sidecars,
    export_texture_analysis_report,
    load_research_notes,
    resolve_material_texture_references,
    save_research_notes,
    upsert_research_note,
    validate_normal_maps,
)
from crimson_texture_forge.models import ArchiveEntry


class ResearchRefreshWorker(QObject):
    progress_changed = Signal(int, int, str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        archive_entries: Sequence[object],
        filtered_archive_entries: Sequence[object],
        original_root: Optional[Path],
        output_root: Optional[Path],
        texconv_path: Optional[Path],
        archive_snapshot_payload: Optional[Dict[str, object]] = None,
    ) -> None:
        super().__init__()
        self.archive_entries = list(archive_entries)
        self.filtered_archive_entries = list(filtered_archive_entries)
        self.original_root = original_root
        self.output_root = output_root
        self.texconv_path = texconv_path
        self.archive_snapshot_payload = dict(archive_snapshot_payload or {})
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            working_entries = self.filtered_archive_entries or self.archive_entries
            payload: Dict[str, object] = {}
            steps = 3

            self.progress_changed.emit(0, steps, "Building archive research snapshot...")
            if self.archive_snapshot_payload:
                payload.update(self.archive_snapshot_payload)
            else:
                payload.update(build_archive_research_snapshot(working_entries))

            if self.stop_event.is_set():
                raise RuntimeError("Research refresh cancelled.")
            self.progress_changed.emit(1, steps, "Comparing original vs rebuilt mip behavior...")
            mip_rows: List[MipAnalysisRow] = []
            if self.original_root is not None and self.output_root is not None:
                if self.original_root.exists() and self.output_root.exists():
                    mip_rows = analyze_mip_behavior(
                        self.original_root,
                        self.output_root,
                        texconv_path=self.texconv_path,
                        stop_event=self.stop_event,
                    )
            payload["mip_rows"] = mip_rows

            if self.stop_event.is_set():
                raise RuntimeError("Research refresh cancelled.")
            self.progress_changed.emit(2, steps, "Validating normal maps...")
            normal_rows: List[NormalValidationRow] = []
            if self.original_root is not None and self.original_root.exists():
                normal_rows.extend(
                    validate_normal_maps(
                        self.original_root,
                        texconv_path=self.texconv_path,
                        stop_event=self.stop_event,
                    )
                )
            if self.output_root is not None and self.output_root.exists() and self.output_root != self.original_root:
                normal_rows.extend(
                    validate_normal_maps(
                        self.output_root,
                        texconv_path=self.texconv_path,
                        stop_event=self.stop_event,
                    )
                )
            normal_rows.sort(key=lambda row: (-row.issue_count, row.path))
            payload["normal_rows"] = normal_rows[:1500]

            self.progress_changed.emit(steps, steps, "Research refresh complete.")
            self.completed.emit(payload)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ReferenceResolveWorker(QObject):
    progress_changed = Signal(int, int, str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        archive_entries: Sequence[object],
        target_path: str,
    ) -> None:
        super().__init__()
        self.archive_entries = list(archive_entries)
        self.target_path = target_path
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            rows, stats = resolve_material_texture_references(
                self.archive_entries,
                self.target_path,
                on_progress=self.progress_changed.emit,
                stop_event=self.stop_event,
            )
            if self.stop_event.is_set():
                raise RuntimeError("Reference resolve cancelled.")
            self.progress_changed.emit(1, 1, "Discovering archive sidecars...")
            sidecar_rows = discover_archive_sidecars(
                self.archive_entries,
                self.target_path,
                stop_event=self.stop_event,
            )
            extract_paths = {self.target_path.strip().replace("\\", "/").strip("/")}
            for row in rows:
                if row.source_path:
                    extract_paths.add(row.source_path)
                if row.related_path:
                    extract_paths.add(row.related_path)
            for row in sidecar_rows:
                if row.related_path:
                    extract_paths.add(row.related_path)
            self.completed.emit(
                {
                    "target_path": self.target_path,
                    "reference_rows": rows,
                    "reference_stats": stats,
                    "sidecar_rows": sidecar_rows,
                    "extract_paths": sorted(path for path in extract_paths if path),
                }
            )
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ResearchTab(QWidget):
    status_message_requested = Signal(str, bool)
    extract_related_set_requested = Signal(object, str)
    focus_archive_browser_requested = Signal()

    def __init__(
        self,
        *,
        settings,
        base_dir: Path,
        get_archive_entries: Callable[[], Sequence[object]],
        get_filtered_archive_entries: Callable[[], Sequence[object]],
        get_original_root: Callable[[], str],
        get_output_root: Callable[[], str],
        get_texconv_path: Callable[[], str],
        get_current_archive_path: Callable[[], str],
        get_current_text_search_path: Callable[[], str],
        get_current_compare_path: Callable[[], str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.base_dir = base_dir
        self.get_archive_entries = get_archive_entries
        self.get_filtered_archive_entries = get_filtered_archive_entries
        self.get_original_root = get_original_root
        self.get_output_root = get_output_root
        self.get_texconv_path = get_texconv_path
        self.get_current_archive_path = get_current_archive_path
        self.get_current_text_search_path = get_current_text_search_path
        self.get_current_compare_path = get_current_compare_path
        self.notes_path = self.base_dir / "research_notes.json"
        self.notes: Dict[str, ResearchNote] = load_research_notes(self.notes_path)
        self.refresh_thread: Optional[QThread] = None
        self.refresh_worker: Optional[ResearchRefreshWorker] = None
        self.resolve_thread: Optional[QThread] = None
        self.resolve_worker: Optional[ReferenceResolveWorker] = None
        self.research_payload: Dict[str, object] = {}
        self.reference_payload: Dict[str, object] = {}
        self.pending_mip_focus_relative_path = ""
        self.archive_snapshot_cache: Dict[str, Dict[str, object]] = {}
        self.pending_archive_snapshot_cache_key = ""
        self.archive_picker_entries: List[ArchiveEntry] = []
        self.archive_picker_entry_index_by_path: Dict[str, int] = {}
        self.archive_picker_child_folders: Dict[tuple[str, ...], List[tuple[str, tuple[str, ...]]]] = {}
        self.archive_picker_direct_files: Dict[tuple[str, ...], List[int]] = {}
        self.archive_picker_folder_entry_indexes: Dict[tuple[str, ...], List[int]] = {}
        self.archive_picker_items_by_folder_key: Dict[tuple[str, ...], QTreeWidgetItem] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.refresh_button = QPushButton("Refresh Research")
        self.refresh_status_label = QLabel("Ready. Use the current archive scan and compare roots.")
        self.refresh_status_label.setWordWrap(True)
        self.refresh_status_label.setObjectName("HintLabel")
        self.refresh_progress = QProgressBar()
        self.refresh_progress.setRange(0, 1)
        self.refresh_progress.setValue(0)
        self.refresh_progress.setFormat("Idle")
        top_row.addWidget(self.refresh_button)
        top_row.addWidget(self.refresh_status_label, stretch=1)
        root_layout.addLayout(top_row)
        root_layout.addWidget(self.refresh_progress)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.main_splitter, stretch=1)

        self.tab_widget = QTabWidget()
        self.main_splitter.addWidget(self.tab_widget)

        self.archive_tab = self._build_archive_tab()
        self.texture_tab = self._build_texture_tab()
        self.notes_tab = self._build_notes_tab()
        self.tab_widget.addTab(self.archive_tab, "Archive Insights")
        self.tab_widget.addTab(self.texture_tab, "Texture Analysis")
        self.tab_widget.addTab(self.notes_tab, "Notes")
        self.right_panel_stack = QStackedWidget()
        self.archive_picker_group = self._build_archive_picker_group()
        self.analysis_detail_group = self._build_analysis_detail_group()
        self.right_panel_stack.addWidget(self.archive_picker_group)
        self.right_panel_stack.addWidget(self.analysis_detail_group)
        self.main_splitter.addWidget(self.right_panel_stack)
        self.main_splitter.setSizes([1180, 620])

        self.refresh_button.clicked.connect(self.refresh_research)
        self.reference_use_archive_button.clicked.connect(
            self.use_selected_archive_picker_for_reference
        )
        self.reference_resolve_button.clicked.connect(self.resolve_references)
        self.reference_extract_button.clicked.connect(self.extract_resolved_related_set)
        self.reference_tree.currentItemChanged.connect(self._handle_reference_selection_changed)
        self.sidecar_tree.currentItemChanged.connect(self._handle_sidecar_selection_changed)
        self.texture_group_extract_button.clicked.connect(self.extract_selected_group)
        self.texture_group_tree.currentItemChanged.connect(self._handle_texture_group_selection_changed)
        self.export_report_csv_button.clicked.connect(lambda: self._export_analysis_report(".csv"))
        self.export_report_json_button.clicked.connect(lambda: self._export_analysis_report(".json"))
        self.tab_widget.currentChanged.connect(self._handle_research_subtab_changed)
        self.notes_use_archive_button.clicked.connect(
            self.use_selected_archive_picker_for_note
        )
        self.notes_use_search_button.clicked.connect(
            lambda: self._populate_note_target("text_search", self.get_current_text_search_path())
        )
        self.notes_use_compare_button.clicked.connect(
            lambda: self._populate_note_target("compare", self.get_current_compare_path())
        )
        self.notes_save_button.clicked.connect(self._save_note)
        self.notes_delete_button.clicked.connect(self._delete_note)
        self.notes_tree.currentItemChanged.connect(self._load_selected_note)
        self._populate_notes_tree()
        self.refresh_archive_picker()
        self._handle_research_subtab_changed(self.tab_widget.currentIndex())

    def set_theme(self, _theme_key: str) -> None:
        return

    def shutdown(self) -> None:
        if self.refresh_worker is not None:
            self.refresh_worker.stop()
        if self.resolve_worker is not None:
            self.resolve_worker.stop()
        for thread in (self.refresh_thread, self.resolve_thread):
            if thread is not None:
                thread.quit()
                thread.wait(3000)

    def refresh_archive_picker(self) -> None:
        entries = list(self.get_filtered_archive_entries()) or list(self.get_archive_entries())
        self.archive_picker_entries = [entry for entry in entries if isinstance(entry, ArchiveEntry)]
        self.archive_picker_entry_index_by_path = {
            self._normalize_archive_path(entry.path).casefold(): index
            for index, entry in enumerate(self.archive_picker_entries)
        }
        self._rebuild_archive_picker_index()
        self.archive_picker_tree.blockSignals(True)
        self.archive_picker_tree.clear()
        self.archive_picker_items_by_folder_key = {}
        for _leaf, child_key in self.archive_picker_child_folders.get((), []):
            self._create_archive_picker_folder_item(self.archive_picker_tree, child_key)
        for entry_index in self.archive_picker_direct_files.get((), []):
            self._create_archive_picker_file_item(self.archive_picker_tree, entry_index)
        self.archive_picker_tree.blockSignals(False)
        if self.archive_picker_tree.topLevelItemCount() > 0:
            first = self.archive_picker_tree.topLevelItem(0)
            if first is not None:
                self.archive_picker_tree.setCurrentItem(first)
        self.archive_picker_status_label.setText(
            f"{len(self.archive_picker_entries):,} archive file(s) available from the current Archive Browser view."
            if self.archive_picker_entries
            else "No archive files are available yet. Scan archives or broaden the current Archive Browser filter."
        )

    def _rebuild_archive_picker_index(self) -> None:
        (
            self.archive_picker_child_folders,
            self.archive_picker_direct_files,
            self.archive_picker_folder_entry_indexes,
        ) = build_archive_tree_index(self.archive_picker_entries)
        self.archive_picker_items_by_folder_key = {}

    def _archive_picker_item_kind(self, item: Optional[QTreeWidgetItem]) -> str:
        if item is None:
            return ""
        raw = item.data(0, Qt.UserRole)
        return raw if isinstance(raw, str) else ""

    def _archive_picker_item_value(self, item: Optional[QTreeWidgetItem]) -> object:
        if item is None:
            return None
        return item.data(0, Qt.UserRole + 1)

    def _archive_picker_folder_key(self, item: Optional[QTreeWidgetItem]) -> tuple[str, ...]:
        raw = self._archive_picker_item_value(item)
        return raw if isinstance(raw, tuple) else ()

    def _create_archive_picker_folder_item(
        self,
        parent: QTreeWidget | QTreeWidgetItem,
        folder_key: tuple[str, ...],
    ) -> QTreeWidgetItem:
        leaf = folder_key[-1] if folder_key else "/"
        item = QTreeWidgetItem([leaf, "Folder", ""])
        item.setData(0, Qt.UserRole, "folder")
        item.setData(0, Qt.UserRole + 1, folder_key)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        if self.archive_picker_child_folders.get(folder_key) or self.archive_picker_direct_files.get(folder_key):
            item.addChild(QTreeWidgetItem(["Loading...", "", ""]))
        parent.addTopLevelItem(item) if isinstance(parent, QTreeWidget) else parent.addChild(item)
        self.archive_picker_items_by_folder_key[folder_key] = item
        return item

    def _create_archive_picker_file_item(
        self,
        parent: QTreeWidget | QTreeWidgetItem,
        entry_index: int,
    ) -> Optional[QTreeWidgetItem]:
        if not (0 <= entry_index < len(self.archive_picker_entries)):
            return None
        entry = self.archive_picker_entries[entry_index]
        item = QTreeWidgetItem([PurePosixPath(entry.path.replace("\\", "/")).name, entry.extension or "file", entry.package_label])
        item.setData(0, Qt.UserRole, "file")
        item.setData(0, Qt.UserRole + 1, entry_index)
        item.setToolTip(0, entry.path)
        item.setToolTip(2, entry.package_label)
        parent.addTopLevelItem(item) if isinstance(parent, QTreeWidget) else parent.addChild(item)
        return item

    def _ensure_archive_picker_folder_item_populated(self, item: Optional[QTreeWidgetItem]) -> None:
        if item is None or self._archive_picker_item_kind(item) != "folder":
            return
        if item.childCount() == 1 and item.child(0).text(0) == "Loading...":
            item.takeChildren()
        elif item.childCount() > 0:
            return
        folder_key = self._archive_picker_folder_key(item)
        for _leaf, child_key in self.archive_picker_child_folders.get(folder_key, []):
            self._create_archive_picker_folder_item(item, child_key)
        for entry_index in self.archive_picker_direct_files.get(folder_key, []):
            self._create_archive_picker_file_item(item, entry_index)

    def _handle_archive_picker_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._ensure_archive_picker_folder_item_populated(item)

    @staticmethod
    def _normalize_archive_path(path_value: str) -> str:
        return path_value.strip().replace("\\", "/").strip("/")

    def _ensure_archive_picker_folder_path(
        self,
        folder_parts: tuple[str, ...],
    ) -> Optional[QTreeWidgetItem]:
        if not folder_parts:
            return None
        current_folder_key: tuple[str, ...] = ()
        current_item: Optional[QTreeWidgetItem] = None
        for part in folder_parts:
            current_folder_key = (*current_folder_key, part)
            folder_item = self.archive_picker_items_by_folder_key.get(current_folder_key)
            if folder_item is None:
                parent_item = self.archive_picker_items_by_folder_key.get(current_folder_key[:-1])
                if parent_item is not None:
                    self._ensure_archive_picker_folder_item_populated(parent_item)
                folder_item = self.archive_picker_items_by_folder_key.get(current_folder_key)
            if folder_item is None:
                return None
            self._ensure_archive_picker_folder_item_populated(folder_item)
            folder_item.setExpanded(True)
            current_item = folder_item
        return current_item

    def _focus_archive_picker_path(self, path_value: str) -> bool:
        normalized = self._normalize_archive_path(path_value)
        if not normalized:
            return False
        entry_index = self.archive_picker_entry_index_by_path.get(normalized.casefold())
        if entry_index is None:
            self.archive_picker_status_label.setText(
                f"Reference points to {normalized}, but that file is not visible in the current Archive Files list."
            )
            return False

        folder_parts = tuple(part for part in PurePosixPath(normalized).parts[:-1] if part)
        container: QTreeWidget | QTreeWidgetItem
        if folder_parts:
            folder_item = self._ensure_archive_picker_folder_path(folder_parts)
            if folder_item is None:
                return False
            container = folder_item
        else:
            container = self.archive_picker_tree
        file_item = self._find_archive_picker_file_item(container, entry_index)
        if file_item is None:
            return False
        self.right_panel_stack.setCurrentWidget(self.archive_picker_group)
        self.archive_picker_tree.setCurrentItem(file_item)
        self.archive_picker_tree.scrollToItem(file_item, QAbstractItemView.PositionAtCenter)
        return True

    def _find_archive_picker_file_item(
        self,
        container: QTreeWidget | QTreeWidgetItem,
        entry_index: int,
    ) -> Optional[QTreeWidgetItem]:
        child_count = container.topLevelItemCount() if isinstance(container, QTreeWidget) else container.childCount()
        for child_index in range(child_count):
            child = container.topLevelItem(child_index) if isinstance(container, QTreeWidget) else container.child(child_index)
            if child is None:
                continue
            if self._archive_picker_item_kind(child) == "file" and self._archive_picker_item_value(child) == entry_index:
                return child
        return None

    def _current_archive_picker_entry(self) -> Optional[ArchiveEntry]:
        item = self.archive_picker_tree.currentItem()
        if item is None or self._archive_picker_item_kind(item) != "file":
            return None
        value = self._archive_picker_item_value(item)
        if not isinstance(value, int) or not (0 <= value < len(self.archive_picker_entries)):
            return None
        return self.archive_picker_entries[value]

    def _handle_archive_picker_current_item_change(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        entry = self._current_archive_picker_entry() if current is not None else None
        if entry is not None:
            self.archive_picker_status_label.setText(f"Selected: {entry.path} ({entry.package_label})")
            return
        if current is not None and self._archive_picker_item_kind(current) == "folder":
            folder_key = self._archive_picker_folder_key(current)
            folder_text = "/".join(folder_key) if folder_key else "/"
            count = len(self.archive_picker_folder_entry_indexes.get(folder_key, []))
            self.archive_picker_status_label.setText(f"Folder: {folder_text} ({count:,} file(s))")

    def use_selected_archive_picker_for_reference(self) -> None:
        entry = self._current_archive_picker_entry()
        if entry is None:
            self.status_message_requested.emit("Select a file in Research -> Archive Files first.", True)
            return
        self._populate_reference_target(entry.path)

    def use_selected_archive_picker_for_note(self) -> None:
        entry = self._current_archive_picker_entry()
        if entry is None:
            self.status_message_requested.emit("Select a file in Research -> Archive Files first.", True)
            return
        self._populate_note_target("archive", entry.path)

    def _build_archive_snapshot_cache_key(self, entries: Sequence[ArchiveEntry]) -> str:
        digest = hashlib.sha256()
        for entry in entries:
            digest.update(self._normalize_archive_path(entry.path).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(str(entry.package_label).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(str(entry.extension).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\n")
        return f"{len(entries)}:{digest.hexdigest()}"

    def _build_archive_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        sub_tabs = QTabWidget()
        layout.addWidget(sub_tabs, stretch=1)

        groups_tab = QWidget()
        groups_layout = QVBoxLayout(groups_tab)
        groups_layout.setContentsMargins(0, 0, 0, 0)
        groups_layout.setSpacing(10)

        group_actions = QVBoxLayout()
        group_actions.setSpacing(6)
        group_buttons_row = QHBoxLayout()
        group_buttons_row.setSpacing(8)
        self.texture_group_extract_button = QPushButton("Extract Selected Set")
        self.texture_group_status_label = QLabel(
            "Select a grouped texture set to extract its related files and sidecars."
        )
        self.texture_group_status_label.setWordWrap(True)
        self.texture_group_status_label.setObjectName("HintLabel")
        group_buttons_row.addWidget(self.texture_group_extract_button)
        group_buttons_row.addStretch(1)
        group_actions.addLayout(group_buttons_row)
        group_actions.addWidget(self.texture_group_status_label)
        groups_layout.addLayout(group_actions)

        groups_splitter = QSplitter(Qt.Horizontal)
        groups_splitter.setChildrenCollapsible(False)
        groups_layout.addWidget(groups_splitter, stretch=1)

        group_group = QGroupBox("Texture Set Grouper")
        group_layout = QVBoxLayout(group_group)
        group_layout.setContentsMargins(10, 12, 10, 10)
        group_layout.setSpacing(8)
        group_hint = QLabel(
            "Bundles related texture members and sidecars such as base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML, and material files."
        )
        group_hint.setWordWrap(True)
        group_hint.setObjectName("HintLabel")
        group_layout.addWidget(group_hint)
        self.texture_group_tree = QTreeWidget()
        self.texture_group_tree.setAlternatingRowColors(True)
        self.texture_group_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.texture_group_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.texture_group_tree.setHeaderLabels(["Group", "Members", "Kinds", "Packages"])
        self.texture_group_tree.header().resizeSection(0, 280)
        group_layout.addWidget(self.texture_group_tree)
        groups_splitter.addWidget(group_group)

        classifier_group = QGroupBox("Texture-Type Classifier")
        classifier_layout = QVBoxLayout(classifier_group)
        classifier_layout.setContentsMargins(10, 12, 10, 10)
        classifier_layout.setSpacing(8)
        classifier_hint = QLabel(
            "Classifies archive textures as color, normal, mask, roughness, emissive, UI, impostor, or unknown using naming and path heuristics."
        )
        classifier_hint.setWordWrap(True)
        classifier_hint.setObjectName("HintLabel")
        classifier_layout.addWidget(classifier_hint)
        self.classifier_tree = QTreeWidget()
        self.classifier_tree.setRootIsDecorated(False)
        self.classifier_tree.setAlternatingRowColors(True)
        self.classifier_tree.setUniformRowHeights(True)
        self.classifier_tree.setHeaderLabels(["File", "Type", "Confidence", "Package", "Reason"])
        self.classifier_tree.header().resizeSection(0, 340)
        self.classifier_tree.header().resizeSection(1, 120)
        self.classifier_tree.header().resizeSection(2, 90)
        self.classifier_tree.header().resizeSection(3, 120)
        classifier_layout.addWidget(self.classifier_tree)
        groups_splitter.addWidget(classifier_group)
        groups_splitter.setSizes([620, 760])
        sub_tabs.addTab(groups_tab, "Groups")

        reference_tab = QWidget()
        reference_layout = QVBoxLayout(reference_tab)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        reference_layout.setSpacing(10)

        reference_controls = QGroupBox("Material-To-Texture Reference Resolver")
        controls_layout = QVBoxLayout(reference_controls)
        controls_layout.setContentsMargins(10, 12, 10, 10)
        controls_layout.setSpacing(8)
        controls_hint = QLabel(
            "Resolve material/shader/XML references for a selected texture, or inspect outbound texture references from a selected material sidecar."
        )
        controls_hint.setWordWrap(True)
        controls_hint.setObjectName("HintLabel")
        controls_layout.addWidget(controls_hint)
        target_row = QVBoxLayout()
        target_row.setSpacing(6)
        target_input_row = QHBoxLayout()
        target_input_row.setSpacing(8)
        target_actions_row = QHBoxLayout()
        target_actions_row.setSpacing(8)
        self.reference_target_edit = QLineEdit()
        self.reference_target_edit.setPlaceholderText(
            "Archive path to resolve, e.g. object/texture/example_diffuse.dds"
        )
        self.reference_use_archive_button = QPushButton("Use Selected File")
        self.reference_resolve_button = QPushButton("Resolve")
        self.reference_extract_button = QPushButton("Extract Related Set")
        target_input_row.addWidget(self.reference_target_edit, stretch=1)
        target_actions_row.addWidget(self.reference_use_archive_button)
        target_actions_row.addWidget(self.reference_resolve_button)
        target_actions_row.addWidget(self.reference_extract_button)
        target_actions_row.addStretch(1)
        target_row.addLayout(target_input_row)
        target_row.addLayout(target_actions_row)
        controls_layout.addLayout(target_row)
        self.reference_status_label = QLabel("Select an archive file or enter a path to resolve relationships.")
        self.reference_status_label.setWordWrap(True)
        self.reference_status_label.setObjectName("HintLabel")
        controls_layout.addWidget(self.reference_status_label)
        self.reference_progress = QProgressBar()
        self.reference_progress.setRange(0, 1)
        self.reference_progress.setValue(0)
        self.reference_progress.setFormat("Idle")
        controls_layout.addWidget(self.reference_progress)
        reference_layout.addWidget(reference_controls)

        reference_splitter = QSplitter(Qt.Horizontal)
        reference_splitter.setChildrenCollapsible(False)
        reference_layout.addWidget(reference_splitter, stretch=1)

        reference_group = QGroupBox("Reference Results")
        reference_group_layout = QVBoxLayout(reference_group)
        reference_group_layout.setContentsMargins(10, 12, 10, 10)
        reference_group_layout.setSpacing(8)
        self.reference_tree = QTreeWidget()
        self.reference_tree.setRootIsDecorated(False)
        self.reference_tree.setAlternatingRowColors(True)
        self.reference_tree.setUniformRowHeights(True)
        self.reference_tree.setHeaderLabels(["Source", "Related", "Relation", "Matches", "Package"])
        self.reference_tree.header().resizeSection(0, 300)
        self.reference_tree.header().resizeSection(1, 260)
        self.reference_tree.header().resizeSection(2, 180)
        self.reference_tree.header().resizeSection(3, 80)
        reference_group_layout.addWidget(self.reference_tree)
        reference_splitter.addWidget(reference_group)

        sidecar_group = QGroupBox("Archive-Side Sidecar Discovery")
        sidecar_layout = QVBoxLayout(sidecar_group)
        sidecar_layout.setContentsMargins(10, 12, 10, 10)
        sidecar_layout.setSpacing(8)
        self.sidecar_tree = QTreeWidget()
        self.sidecar_tree.setRootIsDecorated(False)
        self.sidecar_tree.setAlternatingRowColors(True)
        self.sidecar_tree.setUniformRowHeights(True)
        self.sidecar_tree.setHeaderLabels(["Related File", "Relation", "Confidence", "Package", "Reason"])
        self.sidecar_tree.header().resizeSection(0, 320)
        self.sidecar_tree.header().resizeSection(1, 160)
        self.sidecar_tree.header().resizeSection(2, 90)
        self.sidecar_tree.header().resizeSection(3, 120)
        sidecar_layout.addWidget(self.sidecar_tree)
        reference_splitter.addWidget(sidecar_group)
        reference_splitter.setSizes([780, 760])
        sub_tabs.addTab(reference_tab, "References")

        heatmap_tab = QWidget()
        heatmap_layout = QVBoxLayout(heatmap_tab)
        heatmap_layout.setContentsMargins(0, 0, 0, 0)
        heatmap_layout.setSpacing(10)
        heatmap_group = QGroupBox("Texture Usage Heatmap")
        heatmap_group_layout = QVBoxLayout(heatmap_group)
        heatmap_group_layout.setContentsMargins(10, 12, 10, 10)
        heatmap_group_layout.setSpacing(8)
        self.heatmap_tree = QTreeWidget()
        self.heatmap_tree.setAlternatingRowColors(True)
        self.heatmap_tree.setHeaderLabels(
            ["Label", "Heat", "Textures", "Sets", "Normals", "UI", "Sidecars", "Impostors"]
        )
        self.heatmap_tree.header().resizeSection(0, 360)
        heatmap_group_layout.addWidget(self.heatmap_tree)
        heatmap_layout.addWidget(heatmap_group, stretch=1)
        sub_tabs.addTab(heatmap_tab, "Heatmap")

        return tab

    def _build_archive_picker_group(self) -> QGroupBox:
        group = QGroupBox("Archive Files")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)

        picker_hint = QLabel(
            "Uses the current Archive Browser scan/filter state so you can pick files for Research without leaving this tab."
        )
        picker_hint.setWordWrap(True)
        picker_hint.setObjectName("HintLabel")
        layout.addWidget(picker_hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.archive_picker_refresh_button = QPushButton("Refresh List")
        self.archive_picker_use_reference_button = QPushButton("Use In References")
        self.archive_picker_use_note_button = QPushButton("Use In Notes")
        actions.addWidget(self.archive_picker_refresh_button)
        actions.addWidget(self.archive_picker_use_reference_button)
        actions.addWidget(self.archive_picker_use_note_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.archive_picker_status_label = QLabel("Load or filter archives first to browse related files here.")
        self.archive_picker_status_label.setWordWrap(True)
        self.archive_picker_status_label.setObjectName("HintLabel")
        layout.addWidget(self.archive_picker_status_label)

        self.archive_picker_tree = QTreeWidget()
        self.archive_picker_tree.setHeaderLabels(["Name", "Type", "Package"])
        self.archive_picker_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.archive_picker_tree.setSelectionBehavior(QTreeWidget.SelectRows)
        self.archive_picker_tree.setAlternatingRowColors(False)
        self.archive_picker_tree.setRootIsDecorated(True)
        self.archive_picker_tree.setUniformRowHeights(True)
        self.archive_picker_tree.header().setStretchLastSection(False)
        self.archive_picker_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.archive_picker_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.archive_picker_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.archive_picker_tree, stretch=1)

        self.archive_picker_refresh_button.clicked.connect(self.refresh_archive_picker)
        self.archive_picker_use_reference_button.clicked.connect(self.use_selected_archive_picker_for_reference)
        self.archive_picker_use_note_button.clicked.connect(self.use_selected_archive_picker_for_note)
        self.archive_picker_tree.currentItemChanged.connect(self._handle_archive_picker_current_item_change)
        self.archive_picker_tree.itemExpanded.connect(self._handle_archive_picker_item_expanded)
        self.archive_picker_tree.itemDoubleClicked.connect(
            lambda item, _column: self.use_selected_archive_picker_for_reference()
            if self._archive_picker_item_kind(item) == "file"
            else None
        )
        return group

    def _build_texture_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        export_row = QVBoxLayout()
        export_row.setSpacing(6)
        export_buttons_row = QHBoxLayout()
        export_buttons_row.setSpacing(8)
        self.export_report_csv_button = QPushButton("Export Report CSV")
        self.export_report_json_button = QPushButton("Export Report JSON")
        self.analysis_status_label = QLabel(
            "Texture Analysis uses your current Original DDS root and Output root. Refresh Research after changing either folder."
        )
        self.analysis_status_label.setWordWrap(True)
        self.analysis_status_label.setObjectName("HintLabel")
        export_buttons_row.addWidget(self.export_report_csv_button)
        export_buttons_row.addWidget(self.export_report_json_button)
        export_buttons_row.addStretch(1)
        export_row.addLayout(export_buttons_row)
        export_row.addWidget(self.analysis_status_label)
        layout.addLayout(export_row)

        self.analysis_context_label = QLabel(
            "Mip Analysis compares matching DDS files found in both Original DDS root and Output root, including header validity, file-size drift, color-space changes, preview-based alpha and brightness checks when texconv is available, and texture-specific warnings for normals, packed masks, and grayscale technical maps. "
            "Bulk Normal Validator scans normal-like DDS files from whichever of those roots currently exist."
        )
        self.analysis_context_label.setWordWrap(True)
        self.analysis_context_label.setObjectName("HintLabel")
        layout.addWidget(self.analysis_context_label)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        mip_group = QGroupBox("Mip Analysis")
        mip_layout = QVBoxLayout(mip_group)
        mip_layout.setContentsMargins(10, 12, 10, 10)
        mip_layout.setSpacing(8)
        mip_hint = QLabel(
            "Compares matching DDS files in Original DDS root and Output root. Results appear only when both roots exist and contain the same relative file path."
        )
        mip_hint.setWordWrap(True)
        mip_hint.setObjectName("HintLabel")
        mip_layout.addWidget(mip_hint)
        self.mip_tree = QTreeWidget()
        self.mip_tree.setRootIsDecorated(False)
        self.mip_tree.setAlternatingRowColors(True)
        self.mip_tree.setHeaderLabels(["Path", "Original", "Rebuilt", "Mips", "Warnings"])
        self.mip_tree.header().resizeSection(0, 320)
        mip_layout.addWidget(self.mip_tree)
        splitter.addWidget(mip_group)

        normal_group = QGroupBox("Bulk Normal Validator")
        normal_layout = QVBoxLayout(normal_group)
        normal_layout.setContentsMargins(10, 12, 10, 10)
        normal_layout.setSpacing(8)
        normal_hint = QLabel(
            "Scans normal-like DDS files from the current Original DDS root and Output root independently. "
            "This can show results even if no rebuilt outputs exist yet."
        )
        normal_hint.setWordWrap(True)
        normal_hint.setObjectName("HintLabel")
        normal_layout.addWidget(normal_hint)
        self.normal_tree = QTreeWidget()
        self.normal_tree.setRootIsDecorated(False)
        self.normal_tree.setAlternatingRowColors(True)
        self.normal_tree.setHeaderLabels(["Path", "Root", "Format", "Size", "Issues"])
        self.normal_tree.header().resizeSection(0, 340)
        normal_layout.addWidget(self.normal_tree)
        splitter.addWidget(normal_group)

        splitter.setSizes([700, 700])
        self.mip_tree.currentItemChanged.connect(self._handle_mip_selection_changed)
        self.normal_tree.currentItemChanged.connect(self._handle_normal_selection_changed)
        return tab

    def _build_analysis_detail_group(self) -> QGroupBox:
        detail_group = QGroupBox("Selected Result Details")
        detail_layout = QVBoxLayout(detail_group)
        detail_layout.setContentsMargins(10, 12, 10, 10)
        detail_layout.setSpacing(8)
        self.analysis_detail_label = QLabel(
            "Select a row in Mip Analysis or Bulk Normal Validator to see where the result came from and what it means."
        )
        self.analysis_detail_label.setWordWrap(True)
        self.analysis_detail_label.setObjectName("HintLabel")
        detail_layout.addWidget(self.analysis_detail_label)
        self.analysis_detail_edit = QPlainTextEdit()
        self.analysis_detail_edit.setReadOnly(True)
        self.analysis_detail_edit.setPlaceholderText(
            "Detailed analysis context and warnings will appear here."
        )
        detail_layout.addWidget(self.analysis_detail_edit, stretch=1)
        return detail_group

    def _build_notes_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        notes_group = QGroupBox("Tagging And Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_layout.setContentsMargins(10, 12, 10, 10)
        notes_layout.setSpacing(8)
        notes_hint = QLabel(
            "Annotate archive files, text-search results, or compare targets while you research. Notes are stored locally beside the EXE."
        )
        notes_hint.setWordWrap(True)
        notes_hint.setObjectName("HintLabel")
        notes_layout.addWidget(notes_hint)
        use_row = QHBoxLayout()
        self.notes_use_archive_button = QPushButton("Use Selected File")
        self.notes_use_search_button = QPushButton("Use Selected Search Result")
        self.notes_use_compare_button = QPushButton("Use Selected Compare File")
        use_row.addWidget(self.notes_use_archive_button)
        use_row.addWidget(self.notes_use_search_button)
        use_row.addWidget(self.notes_use_compare_button)
        notes_layout.addLayout(use_row)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.notes_target_edit = QLineEdit()
        self.notes_source_label = QLabel("manual")
        self.notes_tags_edit = QLineEdit()
        self.notes_tags_edit.setPlaceholderText("comma,separated,tags")
        form.addRow("Target", self.notes_target_edit)
        form.addRow("Source", self.notes_source_label)
        form.addRow("Tags", self.notes_tags_edit)
        notes_layout.addLayout(form)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "Add freeform notes, discoveries, unresolved questions, or file relationships here."
        )
        notes_layout.addWidget(self.notes_edit, stretch=1)
        buttons = QHBoxLayout()
        self.notes_save_button = QPushButton("Save Note")
        self.notes_delete_button = QPushButton("Delete Note")
        buttons.addWidget(self.notes_save_button)
        buttons.addWidget(self.notes_delete_button)
        buttons.addStretch(1)
        notes_layout.addLayout(buttons)
        splitter.addWidget(notes_group)

        list_group = QGroupBox("Saved Notes")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(10, 12, 10, 10)
        list_layout.setSpacing(8)
        self.notes_tree = QTreeWidget()
        self.notes_tree.setRootIsDecorated(False)
        self.notes_tree.setAlternatingRowColors(True)
        self.notes_tree.setHeaderLabels(["Target", "Tags", "Updated", "Source"])
        self.notes_tree.header().resizeSection(0, 360)
        self.notes_tree.header().resizeSection(1, 200)
        self.notes_tree.header().resizeSection(2, 180)
        list_layout.addWidget(self.notes_tree)
        splitter.addWidget(list_group)
        splitter.setSizes([720, 680])
        return tab

    def refresh_research(self) -> None:
        if self.refresh_thread is not None:
            return
        self.refresh_archive_picker()
        archive_entries = list(self.get_archive_entries())
        filtered_entries = list(self.get_filtered_archive_entries())
        working_entries = [entry for entry in (filtered_entries or archive_entries) if isinstance(entry, ArchiveEntry)]
        original_root = Path(self.get_original_root()).expanduser() if self.get_original_root().strip() else None
        output_root = Path(self.get_output_root()).expanduser() if self.get_output_root().strip() else None
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        archive_snapshot_cache_key = self._build_archive_snapshot_cache_key(working_entries)
        cached_archive_snapshot = self.archive_snapshot_cache.get(archive_snapshot_cache_key)

        worker = ResearchRefreshWorker(
            archive_entries=archive_entries,
            filtered_archive_entries=filtered_entries,
            original_root=original_root,
            output_root=output_root,
            texconv_path=texconv_path,
            archive_snapshot_payload=cached_archive_snapshot,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._handle_refresh_progress)
        worker.completed.connect(self._handle_refresh_complete)
        worker.error.connect(self._handle_refresh_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_refresh_refs)
        self.refresh_worker = worker
        self.refresh_thread = thread
        self.pending_archive_snapshot_cache_key = archive_snapshot_cache_key
        self.refresh_button.setEnabled(False)
        self.refresh_progress.setRange(0, 0)
        self.refresh_progress.setFormat("Working...")
        if cached_archive_snapshot:
            self.refresh_status_label.setText("Preparing research snapshot with cached archive insights...")
            self.status_message_requested.emit("Refreshing research snapshot with cached archive insights...", False)
        else:
            self.refresh_status_label.setText("Preparing research snapshot...")
            self.status_message_requested.emit("Refreshing research snapshot...", False)
        thread.start()

    def focus_texture_analysis_for_compare_path(
        self,
        relative_path: str,
        *,
        refresh_snapshot: bool = True,
    ) -> None:
        normalized_path = self._normalize_relative_path(relative_path)
        if not normalized_path:
            self.status_message_requested.emit("Select a DDS file in Compare first.", True)
            return
        self.pending_mip_focus_relative_path = normalized_path
        self.tab_widget.setCurrentWidget(self.texture_tab)
        self.right_panel_stack.setCurrentWidget(self.analysis_detail_group)
        if refresh_snapshot:
            if self.refresh_thread is None:
                self.refresh_research()
            else:
                self.refresh_status_label.setText(
                    f"Research refresh already running. Will focus mip analysis for {normalized_path} when ready."
                )
                self.status_message_requested.emit(
                    f"Research refresh already running. Will focus mip analysis for {normalized_path} when ready.",
                    False,
                )
            return
        self._focus_pending_mip_row()

    def resolve_references(self) -> None:
        if self.resolve_thread is not None:
            return
        target_path = self.reference_target_edit.text().strip()
        if not target_path:
            self.status_message_requested.emit("Select or enter an archive path first.", True)
            return
        worker = ReferenceResolveWorker(
            archive_entries=list(self.get_archive_entries()),
            target_path=target_path,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._handle_reference_progress)
        worker.completed.connect(self._handle_reference_complete)
        worker.error.connect(self._handle_reference_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_reference_refs)
        self.resolve_worker = worker
        self.resolve_thread = thread
        self.reference_resolve_button.setEnabled(False)
        self.reference_progress.setRange(0, 0)
        self.reference_progress.setFormat("Working...")
        self.reference_status_label.setText(f"Resolving archive relationships for {target_path}")
        self.status_message_requested.emit(f"Resolving archive relationships for {target_path}...", False)
        thread.start()

    def _handle_refresh_progress(self, current: int, total: int, detail: str) -> None:
        self.refresh_status_label.setText(detail)
        if total > 0:
            self.refresh_progress.setRange(0, total)
            self.refresh_progress.setValue(min(max(current, 0), total))
            self.refresh_progress.setFormat(f"{min(max(current, 0), total)} / {total}")
        else:
            self.refresh_progress.setRange(0, 0)
            self.refresh_progress.setFormat("Working...")
        self.status_message_requested.emit(detail, False)

    def _handle_refresh_complete(self, payload: object) -> None:
        self.research_payload = payload if isinstance(payload, dict) else {}
        if self.pending_archive_snapshot_cache_key and self.research_payload:
            self.archive_snapshot_cache[self.pending_archive_snapshot_cache_key] = {
                "classification_rows": self.research_payload.get("classification_rows", []),
                "texture_groups": self.research_payload.get("texture_groups", []),
                "heatmap_rows": self.research_payload.get("heatmap_rows", []),
            }
        self._populate_texture_groups(self.research_payload.get("texture_groups", []))
        self._populate_classifications(self.research_payload.get("classification_rows", []))
        self._populate_heatmap_rows(self.research_payload.get("heatmap_rows", []))
        self._populate_mip_rows(self.research_payload.get("mip_rows", []))
        self._populate_normal_rows(self.research_payload.get("normal_rows", []))
        self._refresh_texture_analysis_summary()
        self.refresh_status_label.setText("Research snapshot ready.")
        self.refresh_progress.setRange(0, 1)
        self.refresh_progress.setValue(1)
        self.refresh_progress.setFormat("Ready")
        self.status_message_requested.emit("Research snapshot ready.", False)
        self._focus_pending_mip_row()

    def _handle_refresh_error(self, message: str) -> None:
        self.pending_mip_focus_relative_path = ""
        self.refresh_status_label.setText(message)
        self.refresh_progress.setRange(0, 1)
        self.refresh_progress.setValue(0)
        self.refresh_progress.setFormat("Error")
        self.status_message_requested.emit(message, True)

    def _cleanup_refresh_refs(self) -> None:
        self.refresh_worker = None
        self.refresh_thread = None
        self.pending_archive_snapshot_cache_key = ""
        self.refresh_button.setEnabled(True)

    def _handle_reference_progress(self, current: int, total: int, detail: str) -> None:
        self.reference_status_label.setText(detail)
        if total > 0:
            safe_current = min(max(current, 0), total)
            self.reference_progress.setRange(0, total)
            self.reference_progress.setValue(safe_current)
            self.reference_progress.setFormat(f"{safe_current} / {total}")
        else:
            self.reference_progress.setRange(0, 0)
            self.reference_progress.setFormat("Working...")
        self.status_message_requested.emit(detail, False)

    def _handle_reference_complete(self, payload: object) -> None:
        self.reference_payload = payload if isinstance(payload, dict) else {}
        self._populate_reference_rows(self.reference_payload.get("reference_rows", []))
        self._populate_sidecar_rows(self.reference_payload.get("sidecar_rows", []))
        stats = self.reference_payload.get("reference_stats", {})
        if isinstance(stats, dict):
            mode = str(stats.get("mode", ""))
            searched_count = int(stats.get("searched_count", 0))
            unreadable_count = int(stats.get("unreadable_count", 0))
        else:
            mode = ""
            searched_count = 0
            unreadable_count = 0
        sidecar_count = len(self.reference_payload.get("sidecar_rows", [])) if isinstance(
            self.reference_payload.get("sidecar_rows", []), list
        ) else 0
        reference_count = len(self.reference_payload.get("reference_rows", [])) if isinstance(
            self.reference_payload.get("reference_rows", []), list
        ) else 0
        mode_label = "material -> textures" if mode == "outbound" else "textures <- materials"
        self.reference_status_label.setText(
            f"Resolved {reference_count:,} reference row(s), {sidecar_count:,} sidecar candidate(s), "
            f"searched {searched_count:,} text file(s), skipped {unreadable_count:,}. Mode: {mode_label}."
        )
        self.reference_progress.setRange(0, 1)
        self.reference_progress.setValue(1)
        self.reference_progress.setFormat("Ready")
        self.status_message_requested.emit("Reference resolver ready.", False)

    def _handle_reference_error(self, message: str) -> None:
        self.reference_status_label.setText(message)
        self.reference_progress.setRange(0, 1)
        self.reference_progress.setValue(0)
        self.reference_progress.setFormat("Error")
        self.status_message_requested.emit(message, True)

    def _cleanup_reference_refs(self) -> None:
        self.resolve_worker = None
        self.resolve_thread = None
        self.reference_resolve_button.setEnabled(True)

    def _populate_texture_groups(self, groups: object) -> None:
        self.texture_group_tree.clear()
        first_group_item: Optional[QTreeWidgetItem] = None
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, TextureSetGroup):
                continue
            parent = QTreeWidgetItem(
                [
                    group.display_name,
                    f"{group.member_count:,}",
                    ", ".join(group.member_kinds),
                    ", ".join(group.package_labels[:3]) + ("..." if len(group.package_labels) > 3 else ""),
                ]
            )
            parent.setData(0, Qt.UserRole, group.group_key)
            parent.setToolTip(0, group.group_key)
            for member in group.members[:40]:
                child = QTreeWidgetItem([PurePosixPath(member.path).name, "1", member.member_kind, member.package_label])
                child.setData(0, Qt.UserRole, group.group_key)
                child.setToolTip(0, member.path)
                parent.addChild(child)
            self.texture_group_tree.addTopLevelItem(parent)
            if first_group_item is None:
                first_group_item = parent
        if first_group_item is not None:
            self.texture_group_tree.setCurrentItem(first_group_item)
            self.texture_group_status_label.setText(
                f"Selected group: {first_group_item.text(0)}. Click 'Extract Selected Set' to extract its related files and sidecars."
            )
            self.texture_group_extract_button.setEnabled(True)
        else:
            self.texture_group_status_label.setText("No grouped texture sets are available in the current Research snapshot.")
            self.texture_group_extract_button.setEnabled(False)

    def _resolve_group_item(self, item: Optional[QTreeWidgetItem]) -> Optional[QTreeWidgetItem]:
        current = item
        while current is not None:
            group_key = current.data(0, Qt.UserRole)
            if isinstance(group_key, str) and group_key.strip():
                return current
            current = current.parent()
        return None

    def _selected_texture_group(self) -> Optional[TextureSetGroup]:
        candidate_items = list(self.texture_group_tree.selectedItems())
        current = self.texture_group_tree.currentItem()
        if current is not None and current not in candidate_items:
            candidate_items.insert(0, current)
        group_item = None
        for item in candidate_items:
            group_item = self._resolve_group_item(item)
            if group_item is not None:
                break
        if group_item is None:
            return None
        group_key = group_item.data(0, Qt.UserRole)
        if not isinstance(group_key, str) or not group_key.strip():
            return None
        groups = self.research_payload.get("texture_groups", [])
        if not isinstance(groups, list):
            return None
        for group in groups:
            if isinstance(group, TextureSetGroup) and group.group_key == group_key:
                return group
        return None

    def _handle_texture_group_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        group = self._selected_texture_group()
        if group is None:
            if current is None:
                self.texture_group_status_label.setText(
                    "Select a grouped texture set to extract its related files and sidecars."
                )
            else:
                self.texture_group_status_label.setText(
                    "Select a grouped texture set on the left, then click 'Extract Selected Set'."
                )
            self.texture_group_extract_button.setEnabled(False)
            return
        self.texture_group_status_label.setText(
            f"Selected group: {group.display_name} ({group.member_count:,} member(s), {len(group.package_labels):,} package(s))."
        )
        self.texture_group_extract_button.setEnabled(True)

    def _populate_classifications(self, rows: object) -> None:
        self.classifier_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, TextureClassificationRow):
                continue
            item = QTreeWidgetItem(
                [PurePosixPath(row.path).name, row.texture_type, f"{row.confidence}%", row.package_label, row.reason]
            )
            item.setToolTip(0, row.path)
            self.classifier_tree.addTopLevelItem(item)

    def _populate_heatmap_rows(self, rows: object) -> None:
        self.heatmap_tree.clear()
        grouped: Dict[str, QTreeWidgetItem] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, TextureUsageHeatRow):
                continue
            parent = grouped.get(row.scope)
            if parent is None:
                parent = QTreeWidgetItem([row.scope, "", "", "", "", "", "", ""])
                parent.setFirstColumnSpanned(True)
                parent.setExpanded(True)
                grouped[row.scope] = parent
                self.heatmap_tree.addTopLevelItem(parent)
            item = QTreeWidgetItem(
                [
                    row.label,
                    f"{row.heat_score:,}",
                    f"{row.texture_count:,}",
                    f"{row.set_count:,}",
                    f"{row.normal_count:,}",
                    f"{row.ui_count:,}",
                    f"{row.material_count:,}",
                    f"{row.impostor_count:,}",
                ]
            )
            if row.sample_paths:
                item.setToolTip(0, "\n".join(row.sample_paths))
            parent.addChild(item)

    def _populate_mip_rows(self, rows: object) -> None:
        self.mip_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, MipAnalysisRow):
                continue
            item = QTreeWidgetItem(
                [
                    row.relative_path,
                    f"{row.original_size} | {row.original_format}",
                    f"{row.rebuilt_size} | {row.rebuilt_format}",
                    f"{row.original_mips} -> {row.rebuilt_mips}",
                    "; ".join(row.warnings[:2]) if row.warnings else "No warning",
                ]
            )
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(4, "\n".join(row.warnings))
            self.mip_tree.addTopLevelItem(item)
        if self.mip_tree.topLevelItemCount() > 0:
            first = self.mip_tree.topLevelItem(0)
            if first is not None:
                self.mip_tree.setCurrentItem(first)

    def _populate_normal_rows(self, rows: object) -> None:
        self.normal_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, NormalValidationRow):
                continue
            item = QTreeWidgetItem([row.path, row.root_label, row.texconv_format, row.size_text, "; ".join(row.issues[:2])])
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(4, "\n".join(row.issues))
            self.normal_tree.addTopLevelItem(item)
        if self.normal_tree.topLevelItemCount() > 0 and self.mip_tree.topLevelItemCount() == 0:
            first = self.normal_tree.topLevelItem(0)
            if first is not None:
                self.normal_tree.setCurrentItem(first)
        if self.normal_tree.topLevelItemCount() == 0 and self.mip_tree.topLevelItemCount() == 0:
            self.analysis_detail_label.setText(
                "Select a row in Mip Analysis or Bulk Normal Validator to see where the result came from and what it means."
            )
            self.analysis_detail_edit.clear()

    @staticmethod
    def _normalize_relative_path(relative_path: str) -> str:
        text = str(relative_path).strip().replace("\\", "/")
        if not text:
            return ""
        return PurePosixPath(text).as_posix()

    def _focus_pending_mip_row(self) -> bool:
        target_path = self._normalize_relative_path(self.pending_mip_focus_relative_path)
        if not target_path:
            return False
        target_key = target_path.casefold()
        self.tab_widget.setCurrentWidget(self.texture_tab)
        self.right_panel_stack.setCurrentWidget(self.analysis_detail_group)
        for row_index in range(self.mip_tree.topLevelItemCount()):
            item = self.mip_tree.topLevelItem(row_index)
            if item is None:
                continue
            row = item.data(0, Qt.UserRole)
            if not isinstance(row, MipAnalysisRow):
                continue
            row_key = self._normalize_relative_path(row.relative_path).casefold()
            if row_key != target_key:
                continue
            self.pending_mip_focus_relative_path = ""
            self.mip_tree.setCurrentItem(item)
            self.mip_tree.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self._show_mip_row_details(row)
            self.analysis_status_label.setText(f"Showing Mip Analysis details for {target_path}.")
            self.status_message_requested.emit(f"Showing Mip Analysis details for {target_path}.", False)
            return True
        self.pending_mip_focus_relative_path = ""
        self.analysis_status_label.setText(
            f"No Mip Analysis row was found for {target_path}. Refresh Research again after verifying both DDS roots."
        )
        self.analysis_detail_label.setText("Mip Analysis details")
        self.analysis_detail_edit.setPlainText(
            "No matching Mip Analysis row was found for the selected Compare file in the current Research snapshot.\n\n"
            f"Relative path: {target_path}\n\n"
            "This usually means the same DDS path was not found in both Original DDS root and Output root, or the "
            "current roots changed before Research was refreshed."
        )
        self.status_message_requested.emit(
            f"No Mip Analysis row was found for {target_path}. Check the current DDS roots and refresh Research again.",
            True,
        )
        return False

    def _refresh_texture_analysis_summary(self) -> None:
        original_root_text = self.get_original_root().strip()
        output_root_text = self.get_output_root().strip()
        original_root = Path(original_root_text).expanduser() if original_root_text else None
        output_root = Path(output_root_text).expanduser() if output_root_text else None
        original_exists = original_root is not None and original_root.exists()
        output_exists = output_root is not None and output_root.exists()
        mip_rows = self.research_payload.get("mip_rows", [])
        normal_rows = self.research_payload.get("normal_rows", [])
        mip_count = len(mip_rows) if isinstance(mip_rows, list) else 0
        normal_count = len(normal_rows) if isinstance(normal_rows, list) else 0
        normal_roots: Dict[str, int] = {}
        if isinstance(normal_rows, list):
            for row in normal_rows:
                if isinstance(row, NormalValidationRow):
                    normal_roots[row.root_label] = normal_roots.get(row.root_label, 0) + 1
        normal_root_summary = ", ".join(
            f"{label}: {count:,}" for label, count in sorted(normal_roots.items())
        ) if normal_roots else "none"
        self.analysis_context_label.setText(
            "Texture Analysis context:\n"
            f"- Original DDS root: {original_root if original_root_text else '(not set)'}"
            + (" (available)" if original_exists else " (missing or not set)")
            + "\n"
            f"- Output root: {output_root if output_root_text else '(not set)'}"
            + (" (available)" if output_exists else " (missing or not set)")
            + "\n"
            f"- Mip Analysis rows: {mip_count:,} matching DDS file pair(s). Requires the same relative DDS path to exist in both roots. Uses texconv previews when available for alpha, brightness, range, and channel-drift checks.\n"
            f"- Bulk Normal Validator rows: {normal_count:,} normal-like DDS file(s). Current roots represented: {normal_root_summary}."
        )

    def _handle_research_subtab_changed(self, index: int) -> None:
        widget = self.tab_widget.widget(index)
        if widget is self.texture_tab:
            self.right_panel_stack.setCurrentWidget(self.analysis_detail_group)
        else:
            self.right_panel_stack.setCurrentWidget(self.archive_picker_group)

    def _handle_mip_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, MipAnalysisRow):
            return
        self._show_mip_row_details(row)

    def _handle_normal_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, NormalValidationRow):
            return
        self._show_normal_row_details(row)

    def _show_mip_row_details(self, row: MipAnalysisRow) -> None:
        original_root_text = self.get_original_root().strip()
        output_root_text = self.get_output_root().strip()
        self.analysis_detail_label.setText("Mip Analysis details")
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        detail_text = build_mip_analysis_detail(
            Path(original_root_text).expanduser() if original_root_text else Path("."),
            Path(output_root_text).expanduser() if output_root_text else Path("."),
            row,
            texconv_path=texconv_path,
        )
        self.analysis_detail_edit.setPlainText(detail_text)

    def _show_normal_row_details(self, row: NormalValidationRow) -> None:
        original_root_text = self.get_original_root().strip()
        output_root_text = self.get_output_root().strip()
        source_root = ""
        if original_root_text and row.root_label == (Path(original_root_text).name or original_root_text):
            source_root = str(Path(original_root_text).expanduser())
        elif output_root_text and row.root_label == (Path(output_root_text).name or output_root_text):
            source_root = str(Path(output_root_text).expanduser())
        self.analysis_detail_label.setText("Bulk Normal Validator details")
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        root_path = Path(source_root).expanduser() if source_root else Path(original_root_text).expanduser() if original_root_text else Path(".")
        detail_text = build_normal_validation_detail(root_path, row, texconv_path=texconv_path)
        self.analysis_detail_edit.setPlainText(detail_text)

    def _populate_reference_rows(self, rows: object) -> None:
        self.reference_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, MaterialTextureReferenceRow):
                continue
            item = QTreeWidgetItem(
                [
                    row.source_path,
                    row.related_path,
                    row.relation_kind,
                    f"{row.match_count:,}",
                    row.source_package_label or row.related_package_label,
                ]
            )
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, row.snippet)
            item.setToolTip(1, row.related_package_label)
            self.reference_tree.addTopLevelItem(item)
        if self.reference_tree.topLevelItemCount() > 0:
            first = self.reference_tree.topLevelItem(0)
            if first is not None:
                self.reference_tree.setCurrentItem(first)

    def _populate_sidecar_rows(self, rows: object) -> None:
        self.sidecar_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, SidecarDiscoveryRow):
                continue
            item = QTreeWidgetItem(
                [
                    row.related_path,
                    row.relation_kind,
                    f"{row.confidence}%",
                    row.package_label,
                    row.reason,
                ]
            )
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, row.related_path)
            self.sidecar_tree.addTopLevelItem(item)
        if self.sidecar_tree.topLevelItemCount() > 0:
            first = self.sidecar_tree.topLevelItem(0)
            if first is not None:
                self.sidecar_tree.setCurrentItem(first)

    def _handle_reference_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, MaterialTextureReferenceRow):
            return
        if self._focus_archive_picker_path(row.related_path):
            return
        self._focus_archive_picker_path(row.source_path)

    def _handle_sidecar_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, SidecarDiscoveryRow):
            return
        self._focus_archive_picker_path(row.related_path)

    def _populate_reference_target(self, target_path: str) -> None:
        normalized = target_path.strip().replace("\\", "/")
        if not normalized:
            self.focus_archive_browser_requested.emit()
            self.status_message_requested.emit(
                "No archive file is currently selected. Use the Archive Files panel in Research, or go to Archive Browser and select a file first.",
                True,
            )
            return
        self.reference_target_edit.setText(normalized)
        self.status_message_requested.emit(f"Loaded resolver target: {normalized}", False)

    def _selected_group_paths(self) -> List[str]:
        group = self._selected_texture_group()
        if group is None:
            return []
        return [member.path for member in group.members]

    def extract_selected_group(self) -> None:
        groups = self.research_payload.get("texture_groups", [])
        if not isinstance(groups, list) or not groups:
            self.status_message_requested.emit(
                "No grouped texture sets are available yet. Click 'Refresh Research' first.",
                True,
            )
            return
        paths = self._selected_group_paths()
        if not paths:
            self.status_message_requested.emit(
                "Select a grouped texture set first. If the list is stale or empty, click 'Refresh Research'.",
                True,
            )
            return
        self.extract_related_set_requested.emit(paths, "Extracting related texture set...")

    def extract_resolved_related_set(self) -> None:
        extract_paths = self.reference_payload.get("extract_paths", [])
        if not isinstance(extract_paths, list) or not extract_paths:
            self.status_message_requested.emit("Resolve a reference target first.", True)
            return
        self.extract_related_set_requested.emit(extract_paths, "Extracting resolved related set...")

    def _export_analysis_report(self, default_suffix: str) -> None:
        mip_rows = self.research_payload.get("mip_rows", [])
        normal_rows = self.research_payload.get("normal_rows", [])
        if not isinstance(mip_rows, list) or not isinstance(normal_rows, list):
            self.status_message_requested.emit("Refresh research first to build an analysis report.", True)
            return
        default_name = f"texture_analysis_report{default_suffix}"
        selected_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Texture Analysis Report",
            str(self.base_dir / default_name),
            "JSON report (*.json);;CSV report (*.csv)",
        )
        if not selected_path:
            return
        report_path = Path(selected_path)
        if report_path.suffix.lower() not in {".csv", ".json"}:
            report_path = report_path.with_suffix(default_suffix)
        try:
            final_path = export_texture_analysis_report(report_path, mip_rows, normal_rows)
            self.analysis_status_label.setText(f"Exported analysis report to {final_path}")
            self.status_message_requested.emit(f"Exported analysis report to {final_path}", False)
        except Exception as exc:
            self.analysis_status_label.setText(str(exc))
            self.status_message_requested.emit(str(exc), True)

    def _populate_note_target(self, source_kind: str, target_key: str) -> None:
        normalized_key = target_key.strip().replace("\\", "/")
        if not normalized_key:
            self.status_message_requested.emit("No current selection is available for notes.", True)
            return
        self.notes_target_edit.setText(normalized_key)
        self.notes_source_label.setText(source_kind)
        existing = self.notes.get(normalized_key)
        if existing is not None:
            self.notes_tags_edit.setText(", ".join(existing.tags))
            self.notes_edit.setPlainText(existing.note)
        self.status_message_requested.emit(f"Loaded note target: {normalized_key}", False)

    def _populate_notes_tree(self) -> None:
        self.notes_tree.clear()
        for key, note in sorted(self.notes.items(), key=lambda item: item[0].lower()):
            item = QTreeWidgetItem([key, ", ".join(note.tags), note.updated_at, note.source_kind])
            item.setData(0, Qt.UserRole, key)
            item.setToolTip(0, key)
            item.setToolTip(1, ", ".join(note.tags))
            self.notes_tree.addTopLevelItem(item)

    def _save_note(self) -> None:
        try:
            upsert_research_note(
                self.notes,
                target_key=self.notes_target_edit.text(),
                source_kind=self.notes_source_label.text(),
                tags_text=self.notes_tags_edit.text(),
                note_text=self.notes_edit.toPlainText(),
            )
            save_research_notes(self.notes_path, self.notes)
            self._populate_notes_tree()
            self.status_message_requested.emit("Saved research note.", False)
        except Exception as exc:
            self.status_message_requested.emit(str(exc), True)

    def _delete_note(self) -> None:
        delete_research_note(self.notes, self.notes_target_edit.text())
        save_research_notes(self.notes_path, self.notes)
        self._populate_notes_tree()
        self.notes_tags_edit.clear()
        self.notes_edit.clear()
        self.status_message_requested.emit("Deleted research note.", False)

    def _load_selected_note(self, current: Optional[QTreeWidgetItem], _previous: Optional[QTreeWidgetItem]) -> None:
        if current is None:
            return
        key = current.data(0, Qt.UserRole)
        if not isinstance(key, str):
            return
        note = self.notes.get(key)
        if note is None:
            return
        self.notes_target_edit.setText(note.target_key)
        self.notes_source_label.setText(note.source_kind)
        self.notes_tags_edit.setText(", ".join(note.tags))
        self.notes_edit.setPlainText(note.note)
