from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import threading
from html import escape
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cdmw.core.archive import build_archive_preview_result, build_archive_tree_index
from cdmw.core.classification_registry import (
    remove_registered_texture_classifications,
    set_registered_texture_classifications,
    texture_classification_registry_path,
)
from cdmw.core.research import (
    MaterialTextureReferenceRow,
    MipAnalysisRow,
    NormalValidationRow,
    ResearchNote,
    SidecarDiscoveryRow,
    TextureBudgetClassSummary,
    TextureBudgetGroupSummary,
    TextureBudgetProfileSummary,
    TextureBudgetRow,
    TextureClassificationRow,
    TextureSetGroup,
    TextureUsageHeatRow,
    UnknownResolverGroup,
    UnknownResolverMember,
    analyze_mip_behavior,
    build_processing_plan_lookup,
    build_archive_research_snapshot,
    build_texture_budget_analysis,
    build_mip_analysis_family_members_by_path,
    build_ui_constraint_reference_rows,
    build_unknown_resolver_detail,
    build_texture_usage_heatmap,
    build_mip_analysis_detail,
    build_normal_validation_detail,
    bundle_texture_sets,
    classify_texture_entries,
    default_unknown_resolver_label_choice,
    delete_research_note,
    discover_archive_sidecars,
    export_texture_analysis_report,
    load_research_notes,
    resolve_material_texture_references,
    summarize_ui_reference_constraints,
    save_research_notes,
    unknown_resolver_choice_for,
    unknown_resolver_choice_label,
    unknown_resolver_label_choices,
    upsert_research_note,
    validate_normal_maps,
)
from cdmw.core.pipeline import describe_processing_path_kind
from cdmw.models import AppConfig, ArchiveEntry, ArchivePreviewResult
from cdmw.ui.widgets import (
    EmptyStateTreeWidget,
    FlatSectionPanel,
    PreviewLabel,
    PreviewScrollArea,
    clamp_splitter_sizes,
    build_responsive_splitter_sizes,
    responsive_sidebar_bounds,
)


def _wrapped_help_tooltip(text: str, *, width: int = 380) -> str:
    tooltip_html = escape(str(text)).replace("\n", "<br>")
    return f"<qt><div style='width: {width}px; white-space: normal;'>{tooltip_html}</div></qt>"


def _make_help_button(text: str) -> QToolButton:
    button = QToolButton()
    button.setText("?")
    button.setToolTip(_wrapped_help_tooltip(text))
    button.setCursor(Qt.WhatsThisCursor)
    button.setAutoRaise(True)
    button.setFixedSize(22, 22)
    return button


def _add_help_row(layout: QVBoxLayout, text: str) -> None:
    container = QWidget()
    container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    container.setFixedHeight(24)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addStretch(1)
    row.addWidget(_make_help_button(text), alignment=Qt.AlignRight | Qt.AlignTop)
    layout.addWidget(container)


def _add_titled_help_header(layout: QVBoxLayout, title: str, text: str) -> None:
    container = QWidget()
    container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    container.setFixedHeight(24)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    title_label = QLabel(title)
    title_label.setObjectName("FlatSectionTitle")
    row.addWidget(title_label, alignment=Qt.AlignLeft | Qt.AlignVCenter)
    row.addStretch(1)
    row.addWidget(_make_help_button(text), alignment=Qt.AlignRight | Qt.AlignVCenter)
    layout.addWidget(container)


def _set_help_button_text(button: QToolButton, text: str) -> None:
    button.setToolTip(_wrapped_help_tooltip(text))


def _add_flat_section_help(panel: FlatSectionPanel, text: str) -> None:
    header_layout = panel.header_widget.layout()
    if header_layout is None:
        return
    header_layout.setSpacing(6)
    header_layout.insertWidget(max(0, header_layout.count() - 1), _make_help_button(text), alignment=Qt.AlignTop)


def _shutdown_thread(thread: Optional[QThread], *, grace_ms: int = 2000, force_ms: int = 2000) -> None:
    if thread is None:
        return
    thread.quit()
    if thread.wait(grace_ms):
        return
    thread.wait(force_ms)


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
        sidecar_source_entries: Sequence[object],
        original_root: Optional[Path],
        output_root: Optional[Path],
        texconv_path: Optional[Path],
        app_config: Optional[AppConfig] = None,
        archive_snapshot_payload: Optional[Dict[str, object]] = None,
        ui_constraint_related_paths: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.archive_entries = archive_entries
        self.filtered_archive_entries = filtered_archive_entries
        self.sidecar_source_entries = sidecar_source_entries
        self.original_root = original_root
        self.output_root = output_root
        self.texconv_path = texconv_path
        self.app_config = app_config
        self.archive_snapshot_payload = dict(archive_snapshot_payload or {})
        self.ui_constraint_related_paths = [str(path) for path in ui_constraint_related_paths if isinstance(path, str)]
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            working_entries = self.filtered_archive_entries or self.archive_entries
            payload: Dict[str, object] = {}
            progress_total = 100

            def emit_snapshot_progress(current: int, total: int, detail: str) -> None:
                if total <= 0:
                    self.progress_changed.emit(0, progress_total, detail)
                    return
                mapped = int((min(max(current, 0), total) / total) * 65)
                self.progress_changed.emit(mapped, progress_total, detail)

            self.progress_changed.emit(0, progress_total, "Building archive research snapshot...")
            if self.archive_snapshot_payload:
                payload.update(self.archive_snapshot_payload)
            else:
                payload.update(
                    build_archive_research_snapshot(
                        working_entries,
                        sidecar_source_entries=self.sidecar_source_entries,
                        stop_event=self.stop_event,
                        on_progress=emit_snapshot_progress,
                    )
                )

            if self.stop_event.is_set():
                raise RuntimeError("Research refresh cancelled.")
            self.progress_changed.emit(66, progress_total, "Comparing original vs rebuilt mip behavior...")
            mip_rows: List[MipAnalysisRow] = []
            processing_plan_lookup: Dict[str, object] = {}
            if self.app_config is not None and self.original_root is not None and self.original_root.exists():
                try:
                    processing_plan_lookup = build_processing_plan_lookup(
                        self.app_config,
                        original_root_override=self.original_root,
                        stop_event=self.stop_event,
                    )
                except Exception:
                    processing_plan_lookup = {}
            if self.original_root is not None and self.output_root is not None:
                if self.original_root.exists() and self.output_root.exists():
                    mip_family_members_by_path = build_mip_analysis_family_members_by_path(
                        self.original_root,
                        self.output_root,
                        stop_event=self.stop_event,
                    )
                    mip_rows = analyze_mip_behavior(
                        self.original_root,
                        self.output_root,
                        texconv_path=self.texconv_path,
                        processing_plan_lookup=processing_plan_lookup,
                        stop_event=self.stop_event,
                        family_members_by_path=mip_family_members_by_path,
                    )
                    payload["mip_detail_family_members_by_path"] = mip_family_members_by_path
            payload["mip_rows"] = mip_rows

            if self.stop_event.is_set():
                raise RuntimeError("Research refresh cancelled.")
            self.progress_changed.emit(82, progress_total, "Validating normal maps...")
            normal_rows: List[NormalValidationRow] = []
            if self.original_root is not None and self.original_root.exists():
                normal_rows.extend(
                    validate_normal_maps(
                        self.original_root,
                        root_label="Original DDS root",
                        texconv_path=self.texconv_path,
                        processing_plan_lookup=processing_plan_lookup,
                        stop_event=self.stop_event,
                    )
                )
            if self.output_root is not None and self.output_root.exists() and self.output_root != self.original_root:
                normal_rows.extend(
                    validate_normal_maps(
                        self.output_root,
                        root_label="Output root",
                        texconv_path=self.texconv_path,
                        processing_plan_lookup=processing_plan_lookup,
                        stop_event=self.stop_event,
                    )
                )
            normal_rows.sort(key=lambda row: (-row.issue_count, row.path))
            payload["normal_rows"] = normal_rows[:1500]

            if self.stop_event.is_set():
                raise RuntimeError("Research refresh cancelled.")
            self.progress_changed.emit(92, progress_total, "Building budget and residency risk analysis...")
            budget_payload: Dict[str, object] = {
                "budget_rows": [],
                "budget_class_rows": [],
                "budget_group_rows": [],
                "budget_profile": None,
            }
            if self.original_root is not None and self.output_root is not None:
                if self.original_root.exists() and self.output_root.exists():
                    budget_payload = build_texture_budget_analysis(
                        self.original_root,
                        self.output_root,
                        processing_plan_lookup=processing_plan_lookup,
                        ui_constraint_related_paths=self.ui_constraint_related_paths,
                        stop_event=self.stop_event,
                    )
            payload.update(budget_payload)

            self.progress_changed.emit(progress_total, progress_total, "Research refresh complete.")
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


class UIConstraintRefreshWorker(QObject):
    progress_changed = Signal(int, int, str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        archive_entries: Sequence[object],
    ) -> None:
        super().__init__()
        self.archive_entries = archive_entries
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            archive_entries = [entry for entry in self.archive_entries if isinstance(entry, ArchiveEntry)]
            rows = build_ui_constraint_reference_rows(
                archive_entries,
                stop_event=self.stop_event,
                on_progress=self.progress_changed.emit,
            )
            if self.stop_event.is_set():
                raise RuntimeError("UI rect scan cancelled.")
            self.progress_changed.emit(1, 1, "UI rect scan complete.")
            self.completed.emit(rows)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class UnknownResolverPreviewWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(
        self,
        request_id: int,
        texconv_path: Optional[Path],
        entry: Optional[ArchiveEntry],
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.texconv_path = texconv_path
        self.entry = entry
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            payload = build_archive_preview_result(
                self.texconv_path,
                self.entry,
                [],
                stop_event=self.stop_event,
            )
            if self.stop_event.is_set():
                return
            payload = self._attach_loaded_images(payload)
            if not self.stop_event.is_set():
                self.completed.emit(self.request_id, payload)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()

    def _attach_loaded_images(self, result: ArchivePreviewResult) -> ArchivePreviewResult:
        preview_image = self._load_image(result.preview_image_path)
        if preview_image is None:
            return result
        return dataclasses.replace(result, preview_image=preview_image)

    def _load_image(self, image_path: str) -> object:
        if self.stop_event.is_set() or not image_path:
            return None
        reader = QImageReader(image_path)
        image = reader.read()
        if self.stop_event.is_set() or image.isNull():
            return None
        return image


class ResearchTab(QWidget):
    status_message_requested = Signal(str, bool)
    extract_related_set_requested = Signal(object, str)
    focus_archive_browser_requested = Signal()
    review_reference_in_text_search_requested = Signal(str, str)
    REFRESH_POPULATION_BATCH_SIZE = 80
    REFRESH_GROUP_BATCH_SIZE = 20
    UNKNOWN_GROUP_BATCH_SIZE = 50
    POPULATION_TIMER_INTERVAL_MS = 1

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
        get_app_config: Callable[[], AppConfig],
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
        self.get_app_config = get_app_config
        self.get_current_archive_path = get_current_archive_path
        self.get_current_text_search_path = get_current_text_search_path
        self.get_current_compare_path = get_current_compare_path
        self.notes_path = self.base_dir / "research_notes.json"
        self.notes: Dict[str, ResearchNote] = load_research_notes(self.notes_path)
        self.refresh_thread: Optional[QThread] = None
        self.refresh_worker: Optional[ResearchRefreshWorker] = None
        self.ui_constraint_thread: Optional[QThread] = None
        self.ui_constraint_worker: Optional[UIConstraintRefreshWorker] = None
        self.resolve_thread: Optional[QThread] = None
        self.resolve_worker: Optional[ReferenceResolveWorker] = None
        self.unknown_preview_thread: Optional[QThread] = None
        self.unknown_preview_worker: Optional[UnknownResolverPreviewWorker] = None
        self.unknown_preview_request_id = 0
        self.pending_unknown_preview_request: Optional[tuple[int, Optional[ArchiveEntry]]] = None
        self.unknown_preview_fit_to_view = True
        self.unknown_preview_zoom_factor = 1.0
        self.archive_picker_preview_thread: Optional[QThread] = None
        self.archive_picker_preview_worker: Optional[UnknownResolverPreviewWorker] = None
        self.archive_picker_preview_request_id = 0
        self.pending_archive_picker_preview_request: Optional[tuple[int, Optional[ArchiveEntry]]] = None
        self.archive_picker_preview_fit_to_view = True
        self.archive_picker_preview_zoom_factor = 1.0
        self.research_payload: Dict[str, object] = {}
        self.reference_payload: Dict[str, object] = {}
        self.pending_mip_focus_relative_path = ""
        self.archive_snapshot_cache: Dict[str, Dict[str, object]] = {}
        self.pending_archive_snapshot_cache_key = ""
        self.archive_picker_entries: List[ArchiveEntry] = []
        self.archive_picker_entry_index_by_path: Dict[str, int] = {}
        self.archive_picker_entry_by_path: Dict[str, ArchiveEntry] = {}
        self.archive_picker_child_folders: Dict[tuple[str, ...], List[tuple[str, tuple[str, ...]]]] = {}
        self.archive_picker_direct_files: Dict[tuple[str, ...], List[int]] = {}
        self.archive_picker_folder_entry_indexes: Dict[tuple[str, ...], List[int]] = {}
        self.archive_picker_folder_preview_stats: Dict[tuple[str, ...], tuple[int, int, int]] = {}
        self.archive_picker_items_by_folder_key: Dict[tuple[str, ...], QTreeWidgetItem] = {}
        self.archive_picker_refresh_pending = False
        self.defer_archive_picker_refresh = True
        self.classification_registry_path = texture_classification_registry_path()
        self.pending_classification_review_focus_keys: set[str] = set()
        self._classification_review_focus_uses_full_archive = False
        self._archive_snapshot_key_cache: Dict[tuple[int, int, str, str], str] = {}
        self._ui_constraint_scan_archive_key = ""
        self._pending_ui_constraint_archive_key = ""
        self._pending_refresh_full_archive_key = ""
        self._populating_unknown_resolver_controls = False
        self._refresh_population_timer = QTimer(self)
        self._refresh_population_timer.setSingleShot(True)
        self._refresh_population_timer.setInterval(self.POPULATION_TIMER_INTERVAL_MS)
        self._refresh_population_timer.timeout.connect(self._flush_refresh_population_batch)
        self._refresh_population_phases: List[Dict[str, object]] = []
        self._refresh_population_phase_index = 0
        self._refresh_population_total = 0
        self._refresh_population_processed = 0
        self._unknown_population_timer = QTimer(self)
        self._unknown_population_timer.setSingleShot(True)
        self._unknown_population_timer.setInterval(self.POPULATION_TIMER_INTERVAL_MS)
        self._unknown_population_timer.timeout.connect(self._flush_unknown_group_population_batch)
        self._pending_unknown_source_groups: List[UnknownResolverGroup] = []
        self._pending_unknown_groups: List[UnknownResolverGroup] = []
        self._pending_unknown_previous_group_key = ""
        self._pending_unknown_showing_classified = False
        self._pending_unknown_population_total = 0
        self._pending_unknown_scanned_total = 0
        self._pending_research_view_entry_count = 0
        self._pending_research_full_archive_entry_count = 0
        self._pending_research_uses_full_archive_view = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.refresh_button = QPushButton("Refresh Research")
        self.refresh_status_label = QLabel(
            "Ready. Research lists follow the current Archive Browser view, while DDS semantics can still use loaded .pac.xml / .pami sidecars when available."
        )
        self.refresh_status_label.setWordWrap(True)
        self.refresh_status_label.setObjectName("HintLabel")
        self.refresh_progress = QProgressBar()
        self.refresh_progress.setRange(0, 1)
        self.refresh_progress.setValue(0)
        self.refresh_progress.setFormat("Idle")
        self.refresh_progress.setMaximumWidth(180)
        self.refresh_progress.setMaximumHeight(18)
        top_row.addWidget(self.refresh_button)
        top_row.addWidget(self.refresh_status_label, stretch=1)
        top_row.addWidget(self.refresh_progress)
        root_layout.addLayout(top_row)

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
        details_min, _details_pref, details_max = responsive_sidebar_bounds(self, role="wide")
        self.tab_widget.setMinimumWidth(560)
        self.right_panel_stack.setMinimumWidth(details_min)
        self.right_panel_stack.setMaximumWidth(details_max)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setSizes(build_responsive_splitter_sizes(1800, [68, 32], [560, details_min]))

        self.refresh_button.clicked.connect(self.refresh_research)
        self.ui_constraint_refresh_button.clicked.connect(self.refresh_ui_constraints)
        self.reference_use_archive_button.clicked.connect(
            self.use_selected_archive_picker_for_reference
        )
        self.reference_resolve_button.clicked.connect(self.resolve_references)
        self.reference_extract_button.clicked.connect(self.extract_resolved_related_set)
        self.reference_review_text_button.clicked.connect(self.review_selected_reference_in_text_search)
        self.reference_tree.currentItemChanged.connect(self._handle_reference_selection_changed)
        self.ui_constraint_tree.currentItemChanged.connect(self._handle_reference_selection_changed)
        self.sidecar_tree.currentItemChanged.connect(self._handle_sidecar_selection_changed)
        self.texture_group_extract_button.clicked.connect(self.extract_selected_group)
        self.texture_group_tree.currentItemChanged.connect(self._handle_texture_group_selection_changed)
        self.unknown_group_tree.currentItemChanged.connect(self._handle_unknown_group_selection_changed)
        self.unknown_group_tree.itemSelectionChanged.connect(self._handle_unknown_group_item_selection_changed)
        self.unknown_member_tree.currentItemChanged.connect(self._handle_unknown_member_selection_changed)
        self.unknown_show_classified_checkbox.toggled.connect(self._handle_unknown_show_classified_toggled)
        self.unknown_name_filter_edit.textChanged.connect(self._handle_unknown_name_filter_changed)
        self.unknown_package_filter_edit.textChanged.connect(self._handle_unknown_package_filter_changed)
        self.unknown_select_all_button.clicked.connect(self._select_all_unknown_groups)
        self.unknown_clear_family_selection_button.clicked.connect(self._clear_unknown_group_selection)
        self.unknown_preview_button.clicked.connect(self._preview_selected_unknown_member)
        self.unknown_accept_current_role_button.clicked.connect(self._accept_unknown_current_role)
        self.unknown_apply_current_file_button.clicked.connect(self._apply_unknown_current_file_label)
        self.unknown_apply_selected_button.clicked.connect(self._apply_unknown_selected_file_label)
        self.unknown_apply_group_button.clicked.connect(self._apply_unknown_group_label)
        self.unknown_clear_current_file_button.clicked.connect(self._clear_unknown_current_file_label)
        self.unknown_clear_selected_button.clicked.connect(self._clear_unknown_selected_file_label)
        self.unknown_clear_group_button.clicked.connect(self._clear_unknown_group_label)
        self.unknown_preview_zoom_fit_button.clicked.connect(self._set_unknown_preview_fit_mode)
        self.unknown_preview_zoom_100_button.clicked.connect(lambda: self._set_unknown_preview_zoom_factor(1.0))
        self.unknown_preview_zoom_out_button.clicked.connect(lambda: self._adjust_unknown_preview_zoom(-1))
        self.unknown_preview_zoom_in_button.clicked.connect(lambda: self._adjust_unknown_preview_zoom(1))
        self.export_report_csv_button.clicked.connect(lambda: self._export_analysis_report(".csv"))
        self.export_report_json_button.clicked.connect(lambda: self._export_analysis_report(".json"))
        self.tab_widget.currentChanged.connect(self._handle_research_subtab_changed)
        self.archive_insights_tabs.currentChanged.connect(self._handle_archive_insights_subtab_changed)
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
        self._handle_research_subtab_changed(self.tab_widget.currentIndex())
        self._clear_unknown_preview("Select an unknown DDS file to preview it here.")
        self._clear_archive_picker_preview("Select a file in Archive Files to preview it here.")
        self.archive_picker_refresh_pending = True
        self.defer_archive_picker_refresh = False
        QTimer.singleShot(0, self._apply_responsive_splitter_defaults)

    def set_theme(self, _theme_key: str) -> None:
        return

    def shutdown(self) -> None:
        self._refresh_population_timer.stop()
        self._unknown_population_timer.stop()
        if self.refresh_worker is not None:
            self.refresh_worker.stop()
        if self.ui_constraint_worker is not None:
            self.ui_constraint_worker.stop()
        if self.resolve_worker is not None:
            self.resolve_worker.stop()
        if self.unknown_preview_worker is not None:
            self.unknown_preview_worker.stop()
        if self.archive_picker_preview_worker is not None:
            self.archive_picker_preview_worker.stop()
        for thread in (
            self.refresh_thread,
            self.ui_constraint_thread,
            self.resolve_thread,
            self.unknown_preview_thread,
            self.archive_picker_preview_thread,
        ):
            _shutdown_thread(thread)

    def refresh_archive_picker(self) -> None:
        entries = list(self.get_filtered_archive_entries()) or list(self.get_archive_entries())
        self.archive_picker_entries = [entry for entry in entries if isinstance(entry, ArchiveEntry)]
        self.archive_picker_entry_index_by_path = {
            self._normalize_archive_path(entry.path).casefold(): index
            for index, entry in enumerate(self.archive_picker_entries)
        }
        self.archive_picker_entry_by_path = {
            self._normalize_archive_path(entry.path): entry for entry in self.archive_picker_entries
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
        self.archive_picker_refresh_pending = False

    def mark_archive_picker_dirty(self) -> None:
        self.archive_picker_refresh_pending = True

    def refresh_archive_picker_if_pending(self) -> None:
        if self.archive_picker_refresh_pending:
            self.refresh_archive_picker()

    def _rebuild_archive_picker_index(self) -> None:
        (
            self.archive_picker_child_folders,
            self.archive_picker_direct_files,
            self.archive_picker_folder_entry_indexes,
            self.archive_picker_folder_preview_stats,
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
        self._ensure_archive_picker_ready()
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
            self._render_archive_picker_preview_for_entry(entry)
            return
        if current is not None and self._archive_picker_item_kind(current) == "folder":
            folder_key = self._archive_picker_folder_key(current)
            folder_text = "/".join(folder_key) if folder_key else "/"
            count = len(self.archive_picker_folder_entry_indexes.get(folder_key, []))
            self.archive_picker_status_label.setText(f"Folder: {folder_text} ({count:,} file(s))")
            self._show_archive_picker_folder_preview(folder_text, count)
            return
        self._clear_archive_picker_preview("Select a file in Archive Files to preview it here.")

    def use_selected_archive_picker_for_reference(self) -> None:
        self._ensure_archive_picker_ready()
        entry = self._current_archive_picker_entry()
        if entry is None:
            self.status_message_requested.emit("Select a file in Research -> Archive Files first.", True)
            return
        self._populate_reference_target(entry.path)

    def use_selected_archive_picker_for_note(self) -> None:
        self._ensure_archive_picker_ready()
        entry = self._current_archive_picker_entry()
        if entry is None:
            self.status_message_requested.emit("Select a file in Research -> Archive Files first.", True)
            return
        self._populate_note_target("archive", entry.path)

    def _set_archive_picker_preview_image_controls_enabled(self, enabled: bool) -> None:
        self.archive_picker_preview_zoom_out_button.setEnabled(enabled)
        self.archive_picker_preview_zoom_fit_button.setEnabled(enabled)
        self.archive_picker_preview_zoom_100_button.setEnabled(enabled)
        self.archive_picker_preview_zoom_in_button.setEnabled(enabled)
        if not enabled:
            self.archive_picker_preview_zoom_value.setText("-")
        else:
            self._update_archive_picker_preview_zoom_label()

    def _update_archive_picker_preview_zoom_label(self) -> None:
        if self.archive_picker_preview_fit_to_view:
            self.archive_picker_preview_zoom_value.setText("Fit")
        else:
            self.archive_picker_preview_zoom_value.setText(
                f"{int(round(self.archive_picker_preview_zoom_factor * 100))}%"
            )

    def _apply_archive_picker_preview_zoom(self) -> None:
        self.archive_picker_preview_label.set_fit_to_view(self.archive_picker_preview_fit_to_view)
        self.archive_picker_preview_label.set_zoom_factor(self.archive_picker_preview_zoom_factor)
        self._update_archive_picker_preview_zoom_label()

    def _set_archive_picker_preview_fit_mode(self) -> None:
        self.archive_picker_preview_fit_to_view = True
        self._apply_archive_picker_preview_zoom()

    def _set_archive_picker_preview_zoom_factor(self, zoom_factor: float) -> None:
        self.archive_picker_preview_fit_to_view = False
        self.archive_picker_preview_zoom_factor = min(max(zoom_factor, 0.1), 16.0)
        self._apply_archive_picker_preview_zoom()

    def _adjust_archive_picker_preview_zoom(self, step: int) -> None:
        current_zoom = (
            self.archive_picker_preview_label.current_display_scale()
            if self.archive_picker_preview_fit_to_view
            else self.archive_picker_preview_zoom_factor
        )
        zoom_steps = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]
        closest_index = min(range(len(zoom_steps)), key=lambda idx: abs(zoom_steps[idx] - current_zoom))
        next_index = min(max(closest_index + step, 0), len(zoom_steps) - 1)
        self._set_archive_picker_preview_zoom_factor(zoom_steps[next_index])

    def _clear_archive_picker_preview(self, message: str) -> None:
        self.archive_picker_preview_request_id += 1
        self.pending_archive_picker_preview_request = None
        self.archive_picker_preview_title_label.setText("Select an archive file")
        self.archive_picker_preview_meta_label.setText(message)
        self.archive_picker_preview_warning_label.clear()
        self.archive_picker_preview_warning_label.setVisible(False)
        self.archive_picker_preview_info_edit.setPlainText(message)
        self.archive_picker_preview_text_edit.clear()
        self.archive_picker_preview_details_edit.clear()
        self.archive_picker_preview_label.clear_preview(message)
        self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_info_edit)
        self.archive_picker_preview_tabs.setCurrentIndex(0)
        self._set_archive_picker_preview_image_controls_enabled(False)

    def _show_archive_picker_folder_preview(self, folder_text: str, count: int) -> None:
        message = f"Folder: {folder_text}\nFiles: {count:,}"
        self.archive_picker_preview_title_label.setText(folder_text or "/")
        self.archive_picker_preview_meta_label.setText(f"Folder | {count:,} file(s)")
        self.archive_picker_preview_warning_label.clear()
        self.archive_picker_preview_warning_label.setVisible(False)
        self.archive_picker_preview_info_edit.setPlainText(message)
        self.archive_picker_preview_details_edit.setPlainText(message)
        self.archive_picker_preview_label.clear_preview("Select a file to preview it here.")
        self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_info_edit)
        self.archive_picker_preview_tabs.setCurrentIndex(0)
        self._set_archive_picker_preview_image_controls_enabled(False)

    def _render_archive_picker_preview_for_entry(self, entry: Optional[ArchiveEntry]) -> None:
        request_id = self.archive_picker_preview_request_id + 1
        self.archive_picker_preview_request_id = request_id
        if entry is None:
            self.pending_archive_picker_preview_request = None
            self._clear_archive_picker_preview("Select a file in Archive Files to preview it here.")
            return

        self.archive_picker_preview_title_label.setText(entry.basename)
        self.archive_picker_preview_meta_label.setText("Loading preview...")
        self.archive_picker_preview_warning_label.clear()
        self.archive_picker_preview_warning_label.setVisible(False)
        self.archive_picker_preview_info_edit.setPlainText("Preparing preview...")
        self.archive_picker_preview_details_edit.setPlainText("Preparing preview...")
        self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_info_edit)
        self.pending_archive_picker_preview_request = None

        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        if self.archive_picker_preview_thread is not None:
            self.pending_archive_picker_preview_request = (request_id, entry)
            if self.archive_picker_preview_worker is not None:
                self.archive_picker_preview_worker.stop()
            return
        self._start_archive_picker_preview_worker(request_id, texconv_path, entry)

    def _start_archive_picker_preview_worker(
        self,
        request_id: int,
        texconv_path: Optional[Path],
        entry: Optional[ArchiveEntry],
    ) -> None:
        worker = UnknownResolverPreviewWorker(request_id, texconv_path, entry)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_archive_picker_preview_ready)
        worker.error.connect(self._handle_archive_picker_preview_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_archive_picker_preview_refs)
        self.archive_picker_preview_worker = worker
        self.archive_picker_preview_thread = thread
        thread.start()

    def _handle_archive_picker_preview_ready(self, request_id: int, payload: object) -> None:
        if request_id != self.archive_picker_preview_request_id:
            return
        if isinstance(payload, ArchivePreviewResult):
            self._apply_archive_picker_preview_result(payload)

    def _handle_archive_picker_preview_error(self, request_id: int, message: str) -> None:
        if request_id != self.archive_picker_preview_request_id:
            return
        self._clear_archive_picker_preview(f"Preview failed: {message}")

    def _cleanup_archive_picker_preview_refs(self) -> None:
        self.archive_picker_preview_worker = None
        self.archive_picker_preview_thread = None
        if self.pending_archive_picker_preview_request is None:
            return
        request_id, entry = self.pending_archive_picker_preview_request
        self.pending_archive_picker_preview_request = None
        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        self.archive_picker_preview_request_id = request_id
        self._start_archive_picker_preview_worker(request_id, texconv_path, entry)

    def _apply_archive_picker_preview_result(self, result: ArchivePreviewResult) -> None:
        title = result.title or "Selected Preview"
        metadata_summary = result.metadata_summary or "Preview ready."
        detail_text = result.detail_text or metadata_summary
        self.archive_picker_preview_title_label.setText(title)
        self.archive_picker_preview_meta_label.setText(metadata_summary)
        self.archive_picker_preview_warning_label.setText(result.warning_text)
        self.archive_picker_preview_warning_label.setVisible(bool(result.warning_text))
        self.archive_picker_preview_info_edit.setPlainText(detail_text)
        self.archive_picker_preview_details_edit.setPlainText(detail_text)
        if result.preferred_view == "image" and (result.preview_image is not None or result.preview_image_path):
            if result.preview_image is not None:
                self.archive_picker_preview_label.set_preview_image(result.preview_image, title or "Preview image")
            else:
                self.archive_picker_preview_label.set_preview_image_path(
                    result.preview_image_path, title or "Preview image"
                )
            self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_scroll)
            self.archive_picker_preview_tabs.setCurrentIndex(0)
            self._set_archive_picker_preview_image_controls_enabled(True)
            self._apply_archive_picker_preview_zoom()
            return
        if result.preferred_view == "text" and result.preview_text:
            self.archive_picker_preview_text_edit.setPlainText(result.preview_text)
            self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_text_edit)
            self.archive_picker_preview_tabs.setCurrentIndex(0)
            self.archive_picker_preview_label.clear_preview("No image preview available.")
            self._set_archive_picker_preview_image_controls_enabled(False)
            return
        self.archive_picker_preview_label.clear_preview("No image preview available.")
        self.archive_picker_preview_stack.setCurrentWidget(self.archive_picker_preview_info_edit)
        self.archive_picker_preview_tabs.setCurrentIndex(0)
        self._set_archive_picker_preview_image_controls_enabled(False)

    def _build_archive_snapshot_cache_key(self, entries: Sequence[ArchiveEntry]) -> str:
        if not entries:
            return "0:empty"
        first_path = self._normalize_archive_path(entries[0].path)
        last_path = self._normalize_archive_path(entries[-1].path)
        cache_token = (id(entries), len(entries), first_path, last_path)
        cached_key = self._archive_snapshot_key_cache.get(cache_token)
        if cached_key:
            return cached_key
        digest = hashlib.sha256()
        for entry in entries:
            digest.update(self._normalize_archive_path(entry.path).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(str(entry.package_label).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(str(entry.extension).casefold().encode("utf-8", errors="replace"))
            digest.update(b"\n")
        cache_key = f"{len(entries)}:{digest.hexdigest()}"
        if len(self._archive_snapshot_key_cache) > 16:
            self._archive_snapshot_key_cache.clear()
        self._archive_snapshot_key_cache[cache_token] = cache_key
        return cache_key

    def _current_ui_constraint_related_paths(self) -> List[str]:
        rows = self.research_payload.get("ui_constraint_rows", []) if isinstance(self.research_payload, dict) else []
        return [
            row.related_path
            for row in rows
            if isinstance(row, MaterialTextureReferenceRow) and str(row.related_path or "").strip()
        ]

    def _build_archive_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        sub_tabs = QTabWidget()
        self.archive_insights_tabs = sub_tabs
        sub_tabs.setCornerWidget(
            _make_help_button(
                "Research rows in this tab are built from the current Archive Browser view/filter. When available, DDS classification can still consult loaded archive sidecars such as .pac.xml and .pami so color/albedo and other roles do not depend only on filename guessing."
            ),
            Qt.TopRightCorner,
        )
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
        self.texture_group_status_label = QLabel("No grouped texture set selected.")
        self.texture_group_status_label.setWordWrap(True)
        self.texture_group_status_label.setObjectName("HintLabel")
        group_buttons_row.addWidget(self.texture_group_extract_button)
        group_buttons_row.addWidget(
            _make_help_button("Select a grouped texture set to extract its related files and sidecars.")
        )
        group_buttons_row.addStretch(1)
        group_actions.addLayout(group_buttons_row)
        group_actions.addWidget(self.texture_group_status_label)
        groups_layout.addLayout(group_actions)

        self.groups_splitter = QSplitter(Qt.Horizontal)
        groups_splitter = self.groups_splitter
        groups_splitter.setChildrenCollapsible(False)
        groups_layout.addWidget(groups_splitter, stretch=1)

        group_group = QGroupBox("")
        group_layout = QVBoxLayout(group_group)
        group_layout.setContentsMargins(10, 8, 10, 10)
        group_layout.setSpacing(6)
        _add_titled_help_header(
            group_layout,
            "Texture Set Grouper",
            "Bundles related texture members and sidecars such as base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML, and material files."
        )
        self.texture_group_tree = EmptyStateTreeWidget(
            "Refresh research",
            "Texture sets will appear here after the archive view has been analyzed.",
        )
        self.texture_group_tree.setAlternatingRowColors(True)
        self.texture_group_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.texture_group_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.texture_group_tree.setHeaderLabels(["Group", "Members", "Kinds", "Packages"])
        self.texture_group_tree.header().resizeSection(0, 280)
        group_layout.addWidget(self.texture_group_tree, stretch=1)
        groups_splitter.addWidget(group_group)

        classifier_group = QGroupBox("")
        classifier_layout = QVBoxLayout(classifier_group)
        classifier_layout.setContentsMargins(10, 8, 10, 10)
        classifier_layout.setSpacing(6)
        _add_titled_help_header(
            classifier_layout,
            "Texture-Type Classifier",
            "Classifies archive textures as color, normal, mask, roughness, emissive, UI, impostor, or unknown using naming/path heuristics plus exact sidecar bindings from files such as .pac.xml and .pami when available."
        )
        self.classifier_tree = EmptyStateTreeWidget(
            "No classifications yet",
            "Run Refresh Research to classify visible DDS files and review confidence by package.",
        )
        self.classifier_tree.setRootIsDecorated(False)
        self.classifier_tree.setAlternatingRowColors(True)
        self.classifier_tree.setUniformRowHeights(True)
        self.classifier_tree.setHeaderLabels(["File", "Type", "Confidence", "Package", "Reason"])
        self.classifier_tree.header().resizeSection(0, 340)
        self.classifier_tree.header().resizeSection(1, 120)
        self.classifier_tree.header().resizeSection(2, 90)
        self.classifier_tree.header().resizeSection(3, 120)
        classifier_layout.addWidget(self.classifier_tree, stretch=1)
        groups_splitter.addWidget(classifier_group)
        groups_splitter.setSizes(build_responsive_splitter_sizes(1380, [44, 56], [520, 360]))
        sub_tabs.addTab(groups_tab, "Groups")

        unknown_tab = QWidget()
        unknown_layout = QVBoxLayout(unknown_tab)
        unknown_layout.setContentsMargins(0, 0, 0, 0)
        unknown_layout.setSpacing(6)

        _add_help_row(
            unknown_layout,
            "Review DDS files here, preview them directly, and approve a label once so the app remembers it for future scans and policy planning. The list follows the current Research snapshot from Archive Browser, while inferred roles may still use loaded sidecar bindings."
        )

        self.unknown_resolver_status_label = QLabel(
            "Refresh Research to build the current classification review list."
        )
        self.unknown_resolver_status_label.setWordWrap(True)
        self.unknown_resolver_status_label.setObjectName("HintLabel")
        unknown_layout.addWidget(self.unknown_resolver_status_label)

        unknown_filter_row = QHBoxLayout()
        unknown_filter_row.setSpacing(8)
        self.unknown_show_classified_checkbox = QCheckBox("Also show already classified DDS families")
        self.unknown_show_classified_checkbox.setToolTip(
            "Include already classified texture families too, so you can override them manually if you want."
        )
        self.unknown_name_filter_edit = QLineEdit()
        self.unknown_name_filter_edit.setPlaceholderText("Name filter, supports * and ?")
        self.unknown_package_filter_edit = QLineEdit()
        self.unknown_package_filter_edit.setPlaceholderText("Package filter, for example 0000 or 0015*")
        self.unknown_select_all_button = QPushButton("Select All Shown")
        self.unknown_clear_family_selection_button = QPushButton("Clear Selection")
        unknown_filter_row.addWidget(self.unknown_show_classified_checkbox)
        unknown_filter_row.addWidget(QLabel("Name"))
        unknown_filter_row.addWidget(self.unknown_name_filter_edit, stretch=1)
        unknown_filter_row.addWidget(QLabel("Package"))
        unknown_filter_row.addWidget(self.unknown_package_filter_edit)
        unknown_filter_row.addWidget(self.unknown_select_all_button)
        unknown_filter_row.addWidget(self.unknown_clear_family_selection_button)
        unknown_layout.addLayout(unknown_filter_row)

        self.unknown_splitter = QSplitter(Qt.Horizontal)
        unknown_splitter = self.unknown_splitter
        unknown_splitter.setChildrenCollapsible(False)
        unknown_layout.addWidget(unknown_splitter, stretch=1)

        unknown_left_panel = QWidget()
        unknown_left_layout = QVBoxLayout(unknown_left_panel)
        unknown_left_layout.setContentsMargins(0, 0, 0, 0)
        unknown_left_layout.setSpacing(8)

        self.unknown_group_tree = EmptyStateTreeWidget(
            "No unknown groups",
            "Unclassified texture families will appear here when the current archive view contains unresolved DDS files.",
        )
        self.unknown_group_tree.setRootIsDecorated(False)
        self.unknown_group_tree.setAlternatingRowColors(True)
        self.unknown_group_tree.setUniformRowHeights(True)
        self.unknown_group_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.unknown_group_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.unknown_group_tree.setHeaderLabels(["Name", "Classification", "Local Approval", "Package"])
        self.unknown_group_tree.header().setStretchLastSection(False)
        self.unknown_group_tree.header().resizeSection(0, 340)
        self.unknown_group_tree.header().resizeSection(1, 220)
        self.unknown_group_tree.header().resizeSection(2, 110)
        self.unknown_group_tree.header().resizeSection(3, 120)
        unknown_left_layout.addWidget(self.unknown_group_tree, stretch=1)

        unknown_actions_widget = QWidget()
        unknown_actions_layout = QVBoxLayout(unknown_actions_widget)
        unknown_actions_layout.setContentsMargins(0, 0, 0, 0)
        unknown_actions_layout.setSpacing(8)

        approval_row = QHBoxLayout()
        approval_row.setSpacing(8)
        self.unknown_label_combo = QComboBox()
        for choice_key, texture_type, semantic_subtype in unknown_resolver_label_choices():
            self.unknown_label_combo.addItem(
                unknown_resolver_choice_label(choice_key),
                (choice_key, texture_type, semantic_subtype),
            )
        self.unknown_preview_button = QPushButton("Preview Current")
        self.unknown_apply_selected_button = QPushButton("Apply To Current Family")
        self.unknown_apply_group_button = QPushButton("Apply To Selected Families")
        self.unknown_clear_selected_button = QPushButton("Clear Current Family")
        self.unknown_clear_group_button = QPushButton("Clear Selected Families")
        approval_row.addWidget(QLabel("Label"))
        approval_row.addWidget(self.unknown_label_combo, stretch=1)
        approval_row.addWidget(self.unknown_preview_button)
        unknown_actions_layout.addLayout(approval_row)

        self.unknown_accept_current_role_button = QPushButton("Save Current Role Locally")
        self.unknown_apply_current_file_button = QPushButton("Apply To Current File")
        self.unknown_clear_current_file_button = QPushButton("Clear Current File")
        self.unknown_apply_selected_button.setText("Apply To Unknown Files In Current Family")
        self.unknown_apply_group_button.setText("Apply To Unknown Files In Selected Families")
        self.unknown_clear_selected_button.setText("Clear Current Family")
        self.unknown_clear_group_button.setText("Clear Selected Families")

        file_actions_row = QHBoxLayout()
        file_actions_row.setSpacing(8)
        file_actions_row.addWidget(self.unknown_accept_current_role_button)
        file_actions_row.addWidget(self.unknown_apply_current_file_button)
        file_actions_row.addWidget(self.unknown_clear_current_file_button)
        file_actions_row.addStretch(1)
        unknown_actions_layout.addLayout(file_actions_row)

        current_family_actions_row = QHBoxLayout()
        current_family_actions_row.setSpacing(8)
        current_family_actions_row.addWidget(self.unknown_apply_selected_button)
        current_family_actions_row.addWidget(self.unknown_clear_selected_button)
        current_family_actions_row.addStretch(1)
        unknown_actions_layout.addLayout(current_family_actions_row)

        selected_family_actions_row = QHBoxLayout()
        selected_family_actions_row.setSpacing(8)
        selected_family_actions_row.addWidget(self.unknown_apply_group_button)
        selected_family_actions_row.addWidget(self.unknown_clear_group_button)
        selected_family_actions_row.addStretch(1)
        unknown_actions_layout.addLayout(selected_family_actions_row)
        unknown_left_layout.addWidget(unknown_actions_widget)

        unknown_members_group = QGroupBox("Family Members")
        self.unknown_members_group = unknown_members_group
        unknown_members_layout = QVBoxLayout(unknown_members_group)
        unknown_members_layout.setContentsMargins(10, 12, 10, 10)
        unknown_members_layout.setSpacing(8)
        self.unknown_members_hint_label = QLabel(
            "Shown only when the selected family has multiple texture files."
        )
        self.unknown_members_hint_label.setWordWrap(True)
        self.unknown_members_hint_label.setObjectName("HintLabel")
        unknown_members_layout.addWidget(self.unknown_members_hint_label)
        self.unknown_member_tree = EmptyStateTreeWidget(
            "Select a family",
            "Files in the selected unknown group will appear here.",
        )
        self.unknown_member_tree.setRootIsDecorated(False)
        self.unknown_member_tree.setAlternatingRowColors(True)
        self.unknown_member_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.unknown_member_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.unknown_member_tree.setHeaderLabels(["File", "Current", "Local", "Role", "Package", "Reason"])
        self.unknown_member_tree.header().resizeSection(0, 320)
        self.unknown_member_tree.header().resizeSection(1, 90)
        self.unknown_member_tree.header().resizeSection(2, 130)
        self.unknown_member_tree.header().resizeSection(3, 90)
        self.unknown_member_tree.header().resizeSection(4, 120)
        unknown_members_layout.addWidget(self.unknown_member_tree, stretch=1)
        unknown_left_layout.addWidget(unknown_members_group)
        unknown_splitter.addWidget(unknown_left_panel)

        unknown_preview_group = QGroupBox("Selected Preview")
        unknown_preview_layout = QVBoxLayout(unknown_preview_group)
        unknown_preview_layout.setContentsMargins(10, 12, 10, 10)
        unknown_preview_layout.setSpacing(8)
        unknown_preview_title_row = QHBoxLayout()
        unknown_preview_title_row.setSpacing(8)
        self.unknown_preview_title_label = QLabel("Select a review item")
        self.unknown_preview_title_label.setWordWrap(True)
        self.unknown_preview_zoom_out_button = QPushButton("-")
        self.unknown_preview_zoom_out_button.setToolTip("Zoom out.")
        self.unknown_preview_zoom_fit_button = QPushButton("Fit")
        self.unknown_preview_zoom_fit_button.setToolTip("Fit the preview to the available space.")
        self.unknown_preview_zoom_100_button = QPushButton("100%")
        self.unknown_preview_zoom_100_button.setToolTip("Show the preview at 100% zoom.")
        self.unknown_preview_zoom_in_button = QPushButton("+")
        self.unknown_preview_zoom_in_button.setToolTip("Zoom in.")
        self.unknown_preview_zoom_value = QLabel("-")
        self.unknown_preview_zoom_value.setObjectName("HintLabel")
        unknown_preview_title_row.addWidget(self.unknown_preview_title_label, stretch=1)
        unknown_preview_title_row.addWidget(self.unknown_preview_zoom_out_button)
        unknown_preview_title_row.addWidget(self.unknown_preview_zoom_fit_button)
        unknown_preview_title_row.addWidget(self.unknown_preview_zoom_100_button)
        unknown_preview_title_row.addWidget(self.unknown_preview_zoom_in_button)
        unknown_preview_title_row.addWidget(self.unknown_preview_zoom_value)
        unknown_preview_layout.addLayout(unknown_preview_title_row)

        self.unknown_preview_meta_label = QLabel("Select a DDS file to preview it here.")
        self.unknown_preview_meta_label.setWordWrap(True)
        self.unknown_preview_meta_label.setObjectName("HintLabel")
        unknown_preview_layout.addWidget(self.unknown_preview_meta_label)
        self.unknown_preview_warning_label = QLabel("")
        self.unknown_preview_warning_label.setWordWrap(True)
        self.unknown_preview_warning_label.setObjectName("WarningText")
        self.unknown_preview_warning_label.setVisible(False)
        unknown_preview_layout.addWidget(self.unknown_preview_warning_label)

        self.unknown_preview_stack = QStackedWidget()
        self.unknown_preview_label = PreviewLabel("Select a DDS file to preview it here.")
        self.unknown_preview_scroll = PreviewScrollArea()
        self.unknown_preview_scroll.setWidgetResizable(False)
        self.unknown_preview_scroll.setAlignment(Qt.AlignCenter)
        self.unknown_preview_scroll.setWidget(self.unknown_preview_label)
        self.unknown_preview_label.attach_scroll_area(self.unknown_preview_scroll)
        self.unknown_preview_label.set_wheel_zoom_handler(self._adjust_unknown_preview_zoom)
        self.unknown_preview_info_edit = QPlainTextEdit()
        self.unknown_preview_info_edit.setReadOnly(True)
        self.unknown_preview_info_edit.setPlaceholderText("Select a DDS file to preview it here.")
        self.unknown_preview_stack.addWidget(self.unknown_preview_scroll)
        self.unknown_preview_stack.addWidget(self.unknown_preview_info_edit)
        unknown_preview_layout.addWidget(self.unknown_preview_stack, stretch=1)
        unknown_preview_group.setMinimumWidth(400)
        unknown_splitter.addWidget(unknown_preview_group)

        unknown_details_group = QGroupBox("Details")
        unknown_details_layout = QVBoxLayout(unknown_details_group)
        unknown_details_layout.setContentsMargins(10, 12, 10, 10)
        unknown_details_layout.setSpacing(8)
        self.unknown_detail_edit = QPlainTextEdit()
        self.unknown_detail_edit.setReadOnly(True)
        self.unknown_detail_edit.setPlaceholderText(
            "Select a DDS review item to inspect suggestions, sidecars, DDS facts, and approval guidance."
        )
        unknown_details_layout.addWidget(self.unknown_detail_edit, stretch=1)
        unknown_details_group.setMinimumWidth(300)
        unknown_splitter.addWidget(unknown_details_group)
        unknown_splitter.setSizes(build_responsive_splitter_sizes(2160, [28, 47, 25], [360, 400, 300]))
        self.classification_review_tab = unknown_tab
        sub_tabs.addTab(unknown_tab, "Classification Review")

        reference_tab = QWidget()
        reference_layout = QVBoxLayout(reference_tab)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        reference_layout.setSpacing(10)

        reference_controls = QGroupBox("")
        controls_layout = QVBoxLayout(reference_controls)
        controls_layout.setContentsMargins(10, 8, 10, 10)
        controls_layout.setSpacing(6)
        _add_titled_help_header(
            controls_layout,
            "Material-To-Texture Reference Resolver",
            "Resolve material/shader/XML references for a selected texture, or inspect outbound texture references from a selected material sidecar."
        )
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
        self.reference_review_text_button = QPushButton("Review In Text Search")
        self.reference_review_text_button.setEnabled(False)
        target_input_row.addWidget(self.reference_target_edit, stretch=1)
        target_actions_row.addWidget(self.reference_use_archive_button)
        target_actions_row.addWidget(self.reference_resolve_button)
        target_actions_row.addWidget(self.reference_extract_button)
        target_actions_row.addWidget(self.reference_review_text_button)
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

        self.reference_splitter = QSplitter(Qt.Horizontal)
        reference_splitter = self.reference_splitter
        reference_splitter.setChildrenCollapsible(False)
        reference_layout.addWidget(reference_splitter, stretch=1)

        reference_group = QGroupBox("Reference Results")
        reference_group_layout = QVBoxLayout(reference_group)
        reference_group_layout.setContentsMargins(10, 12, 10, 10)
        reference_group_layout.setSpacing(8)
        self.reference_tree = EmptyStateTreeWidget(
            "No material references",
            "Resolve references to list sidecar, skeleton, animation, and texture relationships.",
        )
        self.reference_tree.setRootIsDecorated(False)
        self.reference_tree.setAlternatingRowColors(True)
        self.reference_tree.setUniformRowHeights(True)
        self.reference_tree.setHeaderLabels(["Source", "Related", "GetRect", "Constraint", "Matches", "Package"])
        self.reference_tree.header().resizeSection(0, 300)
        self.reference_tree.header().resizeSection(1, 260)
        self.reference_tree.header().resizeSection(2, 110)
        self.reference_tree.header().resizeSection(3, 220)
        self.reference_tree.header().resizeSection(4, 80)
        reference_group_layout.addWidget(self.reference_tree)
        reference_splitter.addWidget(reference_group)

        sidecar_group = QGroupBox("Archive-Side Sidecar Discovery")
        sidecar_layout = QVBoxLayout(sidecar_group)
        sidecar_layout.setContentsMargins(10, 12, 10, 10)
        sidecar_layout.setSpacing(8)
        self.sidecar_tree = EmptyStateTreeWidget(
            "No sidecars discovered",
            "Sidecar discovery results will appear here after analysis.",
        )
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
        reference_splitter.setSizes(build_responsive_splitter_sizes(1540, [52, 48], [520, 360]))
        sub_tabs.addTab(reference_tab, "References")

        ui_constraints_tab = QWidget()
        ui_constraints_layout = QVBoxLayout(ui_constraints_tab)
        ui_constraints_layout.setContentsMargins(0, 0, 0, 0)
        ui_constraints_layout.setSpacing(10)
        ui_constraints_group = QGroupBox("")
        ui_constraints_group_layout = QVBoxLayout(ui_constraints_group)
        ui_constraints_group_layout.setContentsMargins(10, 8, 10, 10)
        ui_constraints_group_layout.setSpacing(6)
        _add_titled_help_header(
            ui_constraints_group_layout,
            "UI Rect References",
            "Shows textures that are explicitly referenced by archive UI/XML text with a GetRect-style size box. "
            "This is informational evidence only: it warns that DDS-only upscaling may not change the rendered size if the UI still uses the same rect."
        )
        ui_constraints_actions = QHBoxLayout()
        ui_constraints_actions.setSpacing(8)
        self.ui_constraint_refresh_button = QPushButton("Scan UI Rect References")
        self.ui_constraint_status_label = QLabel(
            "Not scanned for the current archive set yet. Run this when you specifically want UI/XML rect evidence."
        )
        self.ui_constraint_status_label.setWordWrap(True)
        self.ui_constraint_status_label.setObjectName("HintLabel")
        self.ui_constraint_progress = QProgressBar()
        self.ui_constraint_progress.setRange(0, 1)
        self.ui_constraint_progress.setValue(0)
        self.ui_constraint_progress.setFormat("Idle")
        self.ui_constraint_progress.setMaximumWidth(180)
        self.ui_constraint_progress.setMaximumHeight(18)
        ui_constraints_actions.addWidget(self.ui_constraint_refresh_button)
        ui_constraints_actions.addWidget(self.ui_constraint_status_label, stretch=1)
        ui_constraints_actions.addWidget(self.ui_constraint_progress)
        ui_constraints_group_layout.addLayout(ui_constraints_actions)
        self.ui_constraint_tree = EmptyStateTreeWidget(
            "No UI constraints",
            "Refresh UI constraints to identify referenced files that must preserve dimensions or format.",
        )
        self.ui_constraint_tree.setRootIsDecorated(False)
        self.ui_constraint_tree.setAlternatingRowColors(True)
        self.ui_constraint_tree.setUniformRowHeights(True)
        self.ui_constraint_tree.setHeaderLabels(
            ["Texture", "Source XML", "DDS Size", "GetRect", "Constraint", "Package"]
        )
        self.ui_constraint_tree.header().resizeSection(0, 320)
        self.ui_constraint_tree.header().resizeSection(1, 280)
        self.ui_constraint_tree.header().resizeSection(2, 90)
        self.ui_constraint_tree.header().resizeSection(3, 90)
        self.ui_constraint_tree.header().resizeSection(4, 220)
        ui_constraints_group_layout.addWidget(self.ui_constraint_tree)
        ui_constraints_layout.addWidget(ui_constraints_group, stretch=1)
        sub_tabs.addTab(ui_constraints_tab, "UI Constraints")

        heatmap_tab = QWidget()
        heatmap_layout = QVBoxLayout(heatmap_tab)
        heatmap_layout.setContentsMargins(0, 0, 0, 0)
        heatmap_layout.setSpacing(10)
        heatmap_group = QGroupBox("Texture Usage Heatmap")
        heatmap_group_layout = QVBoxLayout(heatmap_group)
        heatmap_group_layout.setContentsMargins(10, 12, 10, 10)
        heatmap_group_layout.setSpacing(8)
        self.heatmap_tree = EmptyStateTreeWidget(
            "No usage heatmap",
            "Texture usage hotspots will appear here after research refresh.",
        )
        self.heatmap_tree.setAlternatingRowColors(True)
        self.heatmap_tree.setHeaderLabels(
            ["Label", "Heat", "Textures", "Sets", "Normals", "UI", "Sidecars", "Impostors"]
        )
        self.heatmap_tree.header().resizeSection(0, 360)
        heatmap_group_layout.addWidget(self.heatmap_tree)
        heatmap_layout.addWidget(heatmap_group, stretch=1)
        sub_tabs.addTab(heatmap_tab, "Heatmap")

        return tab

    def _build_archive_picker_group(self) -> QWidget:
        group = FlatSectionPanel("Archive Files")
        _add_flat_section_help(
            group,
            "Uses the current Archive Browser scan/filter state so you can pick files for Research without leaving this tab. DDS semantics can still use loaded archive sidecars when they are available.",
        )
        layout = group.body_layout
        layout.setSpacing(8)

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

        self.archive_picker_splitter = QSplitter(Qt.Horizontal)
        self.archive_picker_splitter.setChildrenCollapsible(False)
        self.archive_picker_splitter.setHandleWidth(8)

        tree_container = QWidget()
        tree_layout = QVBoxLayout(tree_container)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)
        self.archive_picker_tree = EmptyStateTreeWidget(
            "No archive files",
            "Scan archives or broaden the Archive Browser filter to populate this picker.",
        )
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
        tree_layout.addWidget(self.archive_picker_tree)
        self.archive_picker_splitter.addWidget(tree_container)

        preview_group = FlatSectionPanel("Selected Preview")
        preview_layout = preview_group.body_layout
        preview_layout.setSpacing(8)

        preview_title_row = QHBoxLayout()
        preview_title_row.setSpacing(8)
        self.archive_picker_preview_title_label = QLabel("Select an archive file")
        self.archive_picker_preview_title_label.setWordWrap(True)
        self.archive_picker_preview_zoom_out_button = QPushButton("-")
        self.archive_picker_preview_zoom_fit_button = QPushButton("Fit")
        self.archive_picker_preview_zoom_100_button = QPushButton("100%")
        self.archive_picker_preview_zoom_in_button = QPushButton("+")
        self.archive_picker_preview_zoom_value = QLabel("Fit")
        self.archive_picker_preview_zoom_value.setObjectName("HintLabel")
        preview_title_row.addWidget(self.archive_picker_preview_title_label, stretch=1)
        preview_title_row.addWidget(self.archive_picker_preview_zoom_out_button)
        preview_title_row.addWidget(self.archive_picker_preview_zoom_fit_button)
        preview_title_row.addWidget(self.archive_picker_preview_zoom_100_button)
        preview_title_row.addWidget(self.archive_picker_preview_zoom_in_button)
        preview_title_row.addWidget(self.archive_picker_preview_zoom_value)
        preview_layout.addLayout(preview_title_row)

        self.archive_picker_preview_meta_label = QLabel("Select a file in Archive Files to preview it here.")
        self.archive_picker_preview_meta_label.setWordWrap(True)
        self.archive_picker_preview_meta_label.setObjectName("HintLabel")
        preview_layout.addWidget(self.archive_picker_preview_meta_label)

        self.archive_picker_preview_warning_label = QLabel("")
        self.archive_picker_preview_warning_label.setWordWrap(True)
        self.archive_picker_preview_warning_label.setObjectName("WarningText")
        self.archive_picker_preview_warning_label.setVisible(False)
        preview_layout.addWidget(self.archive_picker_preview_warning_label)

        self.archive_picker_preview_stack = QStackedWidget()
        self.archive_picker_preview_label = PreviewLabel("Select a file to preview it here.")
        self.archive_picker_preview_scroll = PreviewScrollArea()
        self.archive_picker_preview_scroll.setWidgetResizable(False)
        self.archive_picker_preview_scroll.setAlignment(Qt.AlignCenter)
        self.archive_picker_preview_scroll.setWidget(self.archive_picker_preview_label)
        self.archive_picker_preview_label.attach_scroll_area(self.archive_picker_preview_scroll)
        self.archive_picker_preview_label.set_wheel_zoom_handler(self._adjust_archive_picker_preview_zoom)
        self.archive_picker_preview_text_edit = QPlainTextEdit()
        self.archive_picker_preview_text_edit.setReadOnly(True)
        self.archive_picker_preview_info_edit = QPlainTextEdit()
        self.archive_picker_preview_info_edit.setReadOnly(True)
        self.archive_picker_preview_stack.addWidget(self.archive_picker_preview_scroll)
        self.archive_picker_preview_stack.addWidget(self.archive_picker_preview_text_edit)
        self.archive_picker_preview_stack.addWidget(self.archive_picker_preview_info_edit)

        self.archive_picker_preview_details_edit = QPlainTextEdit()
        self.archive_picker_preview_details_edit.setReadOnly(True)
        self.archive_picker_preview_tabs = QTabWidget()
        preview_tab = QWidget()
        preview_tab_layout = QVBoxLayout(preview_tab)
        preview_tab_layout.setContentsMargins(0, 0, 0, 0)
        preview_tab_layout.setSpacing(0)
        preview_tab_layout.addWidget(self.archive_picker_preview_stack)
        details_tab = QWidget()
        details_tab_layout = QVBoxLayout(details_tab)
        details_tab_layout.setContentsMargins(0, 0, 0, 0)
        details_tab_layout.setSpacing(0)
        details_tab_layout.addWidget(self.archive_picker_preview_details_edit)
        self.archive_picker_preview_tabs.addTab(preview_tab, "Preview")
        self.archive_picker_preview_tabs.addTab(details_tab, "Details")
        preview_layout.addWidget(self.archive_picker_preview_tabs, stretch=1)
        self.archive_picker_splitter.addWidget(preview_group)
        self.archive_picker_splitter.setSizes(build_responsive_splitter_sizes(1200, [55, 45], [360, 300]))
        layout.addWidget(self.archive_picker_splitter, stretch=1)

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
        self.archive_picker_preview_zoom_fit_button.clicked.connect(self._set_archive_picker_preview_fit_mode)
        self.archive_picker_preview_zoom_100_button.clicked.connect(lambda: self._set_archive_picker_preview_zoom_factor(1.0))
        self.archive_picker_preview_zoom_out_button.clicked.connect(lambda: self._adjust_archive_picker_preview_zoom(-1))
        self.archive_picker_preview_zoom_in_button.clicked.connect(lambda: self._adjust_archive_picker_preview_zoom(1))
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
        self.analysis_status_label = QLabel("Ready.")
        self.analysis_status_label.setWordWrap(True)
        self.analysis_status_label.setObjectName("HintLabel")
        analysis_context_text = (
            "Texture Analysis uses your current Original DDS root and Output root. Refresh Research after changing either folder.\n\n"
            "Mip Analysis compares matching DDS files found in both Original DDS root and Output root, including header validity, file-size drift, color-space changes, preview-based alpha and brightness checks when texconv is available, and texture-specific warnings for normals, packed masks, and grayscale technical maps. "
            "Bulk Normal Validator scans normal-like DDS files from whichever of those roots currently exist. "
            "Budget Analysis adds exact mod-vs-vanilla growth metrics plus clearly labeled heuristic risk summaries."
        )
        self.analysis_context_help_button = _make_help_button(analysis_context_text)
        export_buttons_row.addWidget(self.export_report_csv_button)
        export_buttons_row.addWidget(self.export_report_json_button)
        export_buttons_row.addWidget(self.analysis_status_label)
        export_buttons_row.addWidget(self.analysis_context_help_button)
        export_buttons_row.addStretch(1)
        export_row.addLayout(export_buttons_row)
        layout.addLayout(export_row)

        self.analysis_context_label = QLabel(analysis_context_text)
        self.analysis_context_label.setWordWrap(True)
        self.analysis_context_label.setObjectName("HintLabel")
        self.analysis_context_label.setVisible(False)

        self.analysis_splitter = QSplitter(Qt.Horizontal)
        splitter = self.analysis_splitter
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        mip_group = QGroupBox("")
        mip_layout = QVBoxLayout(mip_group)
        mip_layout.setContentsMargins(10, 8, 10, 10)
        mip_layout.setSpacing(6)
        _add_titled_help_header(
            mip_layout,
            "Mip Analysis",
            "Compares matching DDS files in Original DDS root and Output root. Results appear only when both roots exist and contain the same relative file path."
        )
        self.mip_tree = EmptyStateTreeWidget(
            "No mip analysis",
            "Refresh research with original and output roots available to compare mip behavior.",
        )
        self.mip_tree.setRootIsDecorated(False)
        self.mip_tree.setAlternatingRowColors(True)
        self.mip_tree.setHeaderLabels(["Path", "Original", "Rebuilt", "Mips", "Warnings"])
        self.mip_tree.header().resizeSection(0, 320)
        mip_layout.addWidget(self.mip_tree, stretch=1)
        splitter.addWidget(mip_group)

        normal_group = QGroupBox("")
        normal_layout = QVBoxLayout(normal_group)
        normal_layout.setContentsMargins(10, 8, 10, 10)
        normal_layout.setSpacing(6)
        _add_titled_help_header(
            normal_layout,
            "Bulk Normal Validator",
            "Scans normal-like DDS files from the current Original DDS root and Output root independently. "
            "This can show results even if no rebuilt outputs exist yet."
        )
        self.normal_tree = EmptyStateTreeWidget(
            "No normal-map validation",
            "Normal validation results will appear here after research refresh.",
        )
        self.normal_tree.setRootIsDecorated(False)
        self.normal_tree.setAlternatingRowColors(True)
        self.normal_tree.setHeaderLabels(["Path", "Root", "Format", "Size", "Issues"])
        self.normal_tree.header().resizeSection(0, 340)
        normal_layout.addWidget(self.normal_tree, stretch=1)
        splitter.addWidget(normal_group)

        budget_group = QGroupBox("")
        budget_layout = QVBoxLayout(budget_group)
        budget_layout.setContentsMargins(10, 8, 10, 10)
        budget_layout.setSpacing(6)
        _add_titled_help_header(
            budget_layout,
            "Budget Analysis",
            "Exact budget rows compare matching DDS files in Original DDS root and Output root. "
            "Class, terrain-group, and profile sections are heuristic summaries and are labeled as such."
        )
        self.budget_tabs = QTabWidget()
        self.budget_file_tree = EmptyStateTreeWidget(
            "No texture budget data",
            "Per-file budget rows will appear here after visible textures are analyzed.",
        )
        self.budget_file_tree.setRootIsDecorated(False)
        self.budget_file_tree.setAlternatingRowColors(True)
        self.budget_file_tree.setUniformRowHeights(True)
        self.budget_file_tree.setHeaderLabels(["Path", "Delta", "Ratio", "Size", "Type", "Risk"])
        self.budget_file_tree.header().resizeSection(0, 340)
        self.budget_tabs.addTab(self.budget_file_tree, "Files")
        self.budget_class_tree = EmptyStateTreeWidget("No class summary", "Class-level budget totals will appear here.")
        self.budget_class_tree.setRootIsDecorated(False)
        self.budget_class_tree.setAlternatingRowColors(True)
        self.budget_class_tree.setUniformRowHeights(True)
        self.budget_class_tree.setHeaderLabels(["Texture Type", "Affected", "Byte Delta", "Avg Risk", "Band"])
        self.budget_tabs.addTab(self.budget_class_tree, "Class Risk")
        self.budget_group_tree = EmptyStateTreeWidget("No group summary", "Grouped texture budget totals will appear here.")
        self.budget_group_tree.setRootIsDecorated(False)
        self.budget_group_tree.setAlternatingRowColors(True)
        self.budget_group_tree.setUniformRowHeights(True)
        self.budget_group_tree.setHeaderLabels(["Group", "Textures", "Byte Delta", "Avg Ratio", "Risk", "Band"])
        self.budget_group_tree.header().resizeSection(0, 300)
        self.budget_tabs.addTab(self.budget_group_tree, "Terrain-Like Groups")
        self.budget_profile_tree = EmptyStateTreeWidget("No profile summary", "Profile budget totals will appear here.")
        self.budget_profile_tree.setRootIsDecorated(False)
        self.budget_profile_tree.setAlternatingRowColors(True)
        self.budget_profile_tree.setUniformRowHeights(True)
        self.budget_profile_tree.setHeaderLabels(["Profile", "Total Delta", "Total Ratio", "Changed", "Upscaled"])
        self.budget_tabs.addTab(self.budget_profile_tree, "Profile")
        budget_layout.addWidget(self.budget_tabs, stretch=1)
        splitter.addWidget(budget_group)

        splitter.setSizes(build_responsive_splitter_sizes(1740, [32, 32, 36], [320, 320, 360]))
        self.mip_tree.currentItemChanged.connect(self._handle_mip_selection_changed)
        self.normal_tree.currentItemChanged.connect(self._handle_normal_selection_changed)
        self.budget_file_tree.currentItemChanged.connect(self._handle_budget_selection_changed)
        self.budget_class_tree.currentItemChanged.connect(self._handle_budget_selection_changed)
        self.budget_group_tree.currentItemChanged.connect(self._handle_budget_selection_changed)
        self.budget_profile_tree.currentItemChanged.connect(self._handle_budget_selection_changed)
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

        self.notes_splitter = QSplitter(Qt.Horizontal)
        splitter = self.notes_splitter
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, stretch=1)

        notes_group = QGroupBox("")
        notes_layout = QVBoxLayout(notes_group)
        notes_layout.setContentsMargins(10, 8, 10, 10)
        notes_layout.setSpacing(6)
        _add_titled_help_header(
            notes_layout,
            "Tagging And Notes",
            "Annotate archive files, text-search results, or compare targets while you research. Notes are stored locally beside the EXE."
        )
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
        self.notes_tree = EmptyStateTreeWidget(
            "No notes saved",
            "Saved research notes for archive, search, and compare targets will appear here.",
        )
        self.notes_tree.setRootIsDecorated(False)
        self.notes_tree.setAlternatingRowColors(True)
        self.notes_tree.setHeaderLabels(["Target", "Tags", "Updated", "Source"])
        self.notes_tree.header().resizeSection(0, 360)
        self.notes_tree.header().resizeSection(1, 200)
        self.notes_tree.header().resizeSection(2, 180)
        list_layout.addWidget(self.notes_tree)
        splitter.addWidget(list_group)
        splitter.setSizes(build_responsive_splitter_sizes(1400, [52, 48], [360, 360]))
        return tab

    def apply_responsive_splitter_sizes(self, total_width: Optional[int] = None) -> None:
        details_min, _details_pref, details_max = responsive_sidebar_bounds(self, role="wide")
        if hasattr(self, "right_panel_stack"):
            self.right_panel_stack.setMinimumWidth(details_min)
            self.right_panel_stack.setMaximumWidth(details_max)
        total_width = total_width or max(self.width() - 32, sum([560, details_min]))
        self.main_splitter.setSizes(build_responsive_splitter_sizes(total_width, [68, 32], [560, details_min]))
        if hasattr(self, "groups_splitter"):
            self.groups_splitter.setSizes(
                build_responsive_splitter_sizes(max(total_width - 80, 880), [44, 56], [520, 360])
            )
        if hasattr(self, "unknown_splitter"):
            self.unknown_splitter.setSizes(
                build_responsive_splitter_sizes(max(total_width + 200, 1060), [28, 47, 25], [360, 400, 300])
            )
        if hasattr(self, "reference_splitter"):
            self.reference_splitter.setSizes(
                build_responsive_splitter_sizes(max(total_width - 40, 880), [52, 48], [520, 360])
            )
        if hasattr(self, "analysis_splitter"):
            self.analysis_splitter.setSizes(
                build_responsive_splitter_sizes(max(total_width + 120, 1000), [32, 32, 36], [320, 320, 360])
            )
        if hasattr(self, "notes_splitter"):
            self.notes_splitter.setSizes(
                build_responsive_splitter_sizes(max(total_width - 80, 720), [52, 48], [360, 360])
            )

    def _auto_fit_tree_columns(
        self,
        tree: QTreeWidget,
        *,
        stretch_column: int,
        min_widths: Dict[int, int],
    ) -> None:
        header = tree.header()
        if header is None or tree.columnCount() <= 0:
            return
        viewport_width = max(tree.viewport().width(), tree.width() - 24, 0)
        if viewport_width <= 0:
            return
        tree.setUpdatesEnabled(False)
        try:
            fixed_width = 0
            for column in range(tree.columnCount()):
                if column == stretch_column:
                    continue
                width = max(min_widths.get(column, 72), header.sectionSize(column))
                header.resizeSection(column, width)
                fixed_width += width
            header.resizeSection(
                stretch_column,
                max(min_widths.get(stretch_column, 220), viewport_width - fixed_width - 12),
            )
        finally:
            tree.setUpdatesEnabled(True)

    def auto_fit_columns(self) -> None:
        self._auto_fit_tree_columns(self.archive_picker_tree, stretch_column=0, min_widths={0: 260, 1: 90, 2: 110})
        self._auto_fit_tree_columns(self.texture_group_tree, stretch_column=0, min_widths={0: 280, 1: 86, 2: 150, 3: 120})
        self._auto_fit_tree_columns(self.classifier_tree, stretch_column=0, min_widths={0: 280, 1: 110, 2: 90, 3: 120, 4: 220})
        self._auto_fit_tree_columns(self.unknown_group_tree, stretch_column=0, min_widths={0: 280, 1: 180, 2: 120, 3: 120})
        self._auto_fit_tree_columns(self.unknown_member_tree, stretch_column=0, min_widths={0: 260, 1: 90, 2: 110, 3: 90, 4: 110, 5: 220})
        self._auto_fit_tree_columns(self.reference_tree, stretch_column=0, min_widths={0: 260, 1: 220, 2: 90, 3: 180, 4: 80, 5: 110})
        self._auto_fit_tree_columns(self.sidecar_tree, stretch_column=0, min_widths={0: 280, 1: 140, 2: 90, 3: 110, 4: 180})
        self._auto_fit_tree_columns(self.ui_constraint_tree, stretch_column=0, min_widths={0: 260, 1: 220, 2: 90, 3: 90, 4: 180})
        self._auto_fit_tree_columns(self.heatmap_tree, stretch_column=0, min_widths={0: 300, 1: 120, 2: 110, 3: 110, 4: 110, 5: 110})
        self._auto_fit_tree_columns(self.mip_tree, stretch_column=0, min_widths={0: 280, 1: 120, 2: 120, 3: 80, 4: 200})
        self._auto_fit_tree_columns(self.normal_tree, stretch_column=0, min_widths={0: 280, 1: 120, 2: 110, 3: 90, 4: 220})
        self._auto_fit_tree_columns(self.budget_file_tree, stretch_column=0, min_widths={0: 280, 1: 100, 2: 80, 3: 120, 4: 100, 5: 80})
        self._auto_fit_tree_columns(self.budget_class_tree, stretch_column=0, min_widths={0: 180, 1: 90, 2: 110, 3: 90, 4: 100})
        self._auto_fit_tree_columns(self.budget_group_tree, stretch_column=0, min_widths={0: 240, 1: 90, 2: 110, 3: 90, 4: 80, 5: 100})
        self._auto_fit_tree_columns(self.budget_profile_tree, stretch_column=0, min_widths={0: 180, 1: 110, 2: 90, 3: 90, 4: 90})
        self._auto_fit_tree_columns(self.notes_tree, stretch_column=0, min_widths={0: 280, 1: 160, 2: 160, 3: 120})

    def _apply_responsive_splitter_defaults(self) -> None:
        self.apply_responsive_splitter_sizes()

    def set_main_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        details_min, _details_pref, details_max = responsive_sidebar_bounds(self, role="wide")
        if hasattr(self, "right_panel_stack"):
            self.right_panel_stack.setMaximumWidth(details_max)
        available_width = total_width or max(self.width() - 32, sum([560, details_min]))
        self.main_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [560, details_min], fallback_weights=[68, 32])
        )

    def set_groups_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        available_width = max((total_width or max(self.width() - 32, sum([720, 360]))) - 80, 880)
        self.groups_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [520, 360], fallback_weights=[44, 56])
        )

    def set_unknown_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        available_width = max((total_width or max(self.width() - 32, sum([720, 360]))) + 200, 1060)
        self.unknown_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [360, 400, 300], fallback_weights=[28, 47, 25])
        )

    def set_reference_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        available_width = max((total_width or max(self.width() - 32, sum([720, 360]))) - 40, 880)
        self.reference_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [520, 360], fallback_weights=[52, 48])
        )

    def set_analysis_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        available_width = max((total_width or max(self.width() - 32, sum([720, 360]))) + 120, 1000)
        self.analysis_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [320, 320, 360], fallback_weights=[32, 32, 36])
        )

    def set_notes_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        available_width = max((total_width or max(self.width() - 32, sum([720, 360]))) - 80, 720)
        self.notes_splitter.setSizes(
            clamp_splitter_sizes(available_width, sizes, [360, 360], fallback_weights=[52, 48])
        )

    def main_splitter_sizes(self) -> List[int]:
        return self.main_splitter.sizes()

    def groups_splitter_sizes(self) -> List[int]:
        return self.groups_splitter.sizes()

    def unknown_splitter_sizes(self) -> List[int]:
        return self.unknown_splitter.sizes()

    def reference_splitter_sizes(self) -> List[int]:
        return self.reference_splitter.sizes()

    def analysis_splitter_sizes(self) -> List[int]:
        return self.analysis_splitter.sizes()

    def notes_splitter_sizes(self) -> List[int]:
        return self.notes_splitter.sizes()

    def refresh_research(self) -> None:
        if self.refresh_thread is not None:
            return
        self.mark_archive_picker_dirty()
        archive_entries = self.get_archive_entries()
        filtered_entries = self.get_filtered_archive_entries()
        use_full_archive_for_focus = (
            self._classification_review_focus_uses_full_archive
            and bool(self.pending_classification_review_focus_keys)
        )
        source_entries = archive_entries if use_full_archive_for_focus else (filtered_entries or archive_entries)
        working_entries = source_entries
        full_archive_key = self._build_archive_snapshot_cache_key(archive_entries)
        original_root = Path(self.get_original_root()).expanduser() if self.get_original_root().strip() else None
        output_root = Path(self.get_output_root()).expanduser() if self.get_output_root().strip() else None
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        working_archive_key = self._build_archive_snapshot_cache_key(working_entries)
        archive_snapshot_cache_key = f"{working_archive_key}|sidecars:{full_archive_key}"
        cached_archive_snapshot = self.archive_snapshot_cache.get(archive_snapshot_cache_key)
        ui_constraint_related_paths = ()
        if self._ui_constraint_scan_archive_key and self._ui_constraint_scan_archive_key == full_archive_key:
            ui_constraint_related_paths = tuple(self._current_ui_constraint_related_paths())

        worker = ResearchRefreshWorker(
            archive_entries=archive_entries,
            filtered_archive_entries=filtered_entries,
            sidecar_source_entries=archive_entries,
            original_root=original_root,
            output_root=output_root,
            texconv_path=texconv_path,
            app_config=self.get_app_config(),
            archive_snapshot_payload=cached_archive_snapshot,
            ui_constraint_related_paths=ui_constraint_related_paths,
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
        self._pending_refresh_full_archive_key = full_archive_key
        self._pending_research_view_entry_count = len(working_entries)
        self._pending_research_full_archive_entry_count = len(archive_entries)
        self._pending_research_uses_full_archive_view = use_full_archive_for_focus
        self.refresh_button.setEnabled(False)
        self.refresh_progress.setRange(0, 0)
        self.refresh_progress.setFormat("Working...")
        if cached_archive_snapshot:
            self.refresh_status_label.setText(
                (
                    f"Preparing research snapshot from the full loaded archive ({len(archive_entries):,} file(s)) with cached archive insights..."
                    if use_full_archive_for_focus
                    else f"Preparing research snapshot from the current Archive Browser view ({len(working_entries):,} file(s)) with cached archive insights..."
                )
            )
            self.status_message_requested.emit("Refreshing research snapshot with cached archive insights...", False)
        else:
            self.refresh_status_label.setText(
                (
                    f"Preparing research snapshot from the full loaded archive ({len(archive_entries):,} file(s))..."
                    if use_full_archive_for_focus
                    else f"Preparing research snapshot from the current Archive Browser view ({len(working_entries):,} file(s))..."
                )
            )
            self.status_message_requested.emit("Refreshing research snapshot...", False)
        thread.start()

    def refresh_ui_constraints(self) -> None:
        if self.ui_constraint_thread is not None:
            return
        archive_entries = self.get_archive_entries()
        archive_key = self._build_archive_snapshot_cache_key(archive_entries)
        worker = UIConstraintRefreshWorker(archive_entries=archive_entries)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress_changed.connect(self._handle_ui_constraint_progress)
        worker.completed.connect(self._handle_ui_constraint_complete)
        worker.error.connect(self._handle_ui_constraint_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_ui_constraint_refs)
        self.ui_constraint_worker = worker
        self.ui_constraint_thread = thread
        self._pending_ui_constraint_archive_key = archive_key
        self.ui_constraint_refresh_button.setEnabled(False)
        self.ui_constraint_progress.setRange(0, 0)
        self.ui_constraint_progress.setFormat("Working...")
        self.ui_constraint_status_label.setText("Preparing UI/XML rect scan across archive text references...")
        self.status_message_requested.emit("Scanning archive UI/XML references for explicit GetRect evidence...", False)
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

    def focus_references_for_path(self, target_path: str, auto_resolve: bool = True) -> None:
        normalized_path = self._normalize_relative_path(target_path)
        if not normalized_path:
            self.status_message_requested.emit("Select a DDS archive path first.", True)
            return
        self.tab_widget.setCurrentWidget(self.archive_tab)
        if hasattr(self, "archive_insights_tabs"):
            self.archive_insights_tabs.setCurrentIndex(2)
        self._populate_reference_target(normalized_path)
        if auto_resolve:
            if self.resolve_thread is None:
                self.resolve_references()
            else:
                self.reference_status_label.setText(
                    f"Reference resolve already running. Will use {normalized_path} next."
                )

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
        previous_ui_rows = self.research_payload.get("ui_constraint_rows", []) if isinstance(self.research_payload, dict) else []
        preserve_ui_rows = (
            self._ui_constraint_scan_archive_key
            and self._pending_refresh_full_archive_key
            and self._ui_constraint_scan_archive_key == self._pending_refresh_full_archive_key
            and isinstance(previous_ui_rows, list)
        )
        self.research_payload = payload if isinstance(payload, dict) else {}
        if preserve_ui_rows:
            self.research_payload["ui_constraint_rows"] = previous_ui_rows
            self.ui_constraint_status_label.setText("Using the latest UI rect scan for the current archive set.")
            self.ui_constraint_progress.setRange(0, 1)
            self.ui_constraint_progress.setValue(1)
            self.ui_constraint_progress.setFormat("Ready")
        else:
            self.research_payload["ui_constraint_rows"] = []
            if self._ui_constraint_scan_archive_key != self._pending_refresh_full_archive_key:
                self._ui_constraint_scan_archive_key = ""
            self.ui_constraint_status_label.setText(
                "Not scanned for the current archive set yet. Run 'Scan UI Rect References' when you need UI/XML rect evidence."
            )
            self.ui_constraint_progress.setRange(0, 1)
            self.ui_constraint_progress.setValue(0)
            self.ui_constraint_progress.setFormat("Idle")
        if self.pending_archive_snapshot_cache_key and self.research_payload:
            self.archive_snapshot_cache[self.pending_archive_snapshot_cache_key] = {
                "classification_rows": self.research_payload.get("classification_rows", []),
                "texture_groups": self.research_payload.get("texture_groups", []),
                "heatmap_rows": self.research_payload.get("heatmap_rows", []),
                "unknown_resolver_groups": self.research_payload.get("unknown_resolver_groups", []),
                "classification_review_groups": self.research_payload.get("classification_review_groups", []),
            }
        self._begin_refresh_population()

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
        self._pending_refresh_full_archive_key = ""
        self.refresh_button.setEnabled(True)

    def _handle_ui_constraint_progress(self, current: int, total: int, detail: str) -> None:
        self.ui_constraint_status_label.setText(detail)
        if total > 0:
            safe_current = min(max(current, 0), total)
            self.ui_constraint_progress.setRange(0, total)
            self.ui_constraint_progress.setValue(safe_current)
            self.ui_constraint_progress.setFormat(f"{safe_current} / {total}")
        else:
            self.ui_constraint_progress.setRange(0, 0)
            self.ui_constraint_progress.setFormat("Working...")
        self.status_message_requested.emit(detail, False)

    def _handle_ui_constraint_complete(self, rows: object) -> None:
        ui_rows = [row for row in rows if isinstance(row, MaterialTextureReferenceRow)] if isinstance(rows, list) else []
        self.research_payload["ui_constraint_rows"] = ui_rows
        self._ui_constraint_scan_archive_key = self._pending_ui_constraint_archive_key
        self._populate_ui_constraint_rows(ui_rows)
        self.ui_constraint_status_label.setText(
            f"UI rect scan complete. Found {len(ui_rows):,} explicit UI/XML rect reference row(s)."
        )
        self.ui_constraint_progress.setRange(0, 1)
        self.ui_constraint_progress.setValue(1)
        self.ui_constraint_progress.setFormat("Ready")
        self._refresh_texture_analysis_summary()
        self.status_message_requested.emit(
            f"UI rect scan complete. Found {len(ui_rows):,} explicit UI/XML rect reference row(s).",
            False,
        )

    def _handle_ui_constraint_error(self, message: str) -> None:
        self.ui_constraint_status_label.setText(message)
        self.ui_constraint_progress.setRange(0, 1)
        self.ui_constraint_progress.setValue(0)
        self.ui_constraint_progress.setFormat("Error")
        self.status_message_requested.emit(message, True)

    def _cleanup_ui_constraint_refs(self) -> None:
        self.ui_constraint_worker = None
        self.ui_constraint_thread = None
        self._pending_ui_constraint_archive_key = ""
        self.ui_constraint_refresh_button.setEnabled(True)

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

    def _stop_refresh_population(self) -> None:
        self._refresh_population_timer.stop()
        self._refresh_population_phases = []
        self._refresh_population_phase_index = 0
        self._refresh_population_total = 0
        self._refresh_population_processed = 0

    def _build_texture_group_item(self, group: TextureSetGroup) -> QTreeWidgetItem:
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
        return parent

    def _build_classification_item(self, row: TextureClassificationRow) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [PurePosixPath(row.path).name, row.texture_type, f"{row.confidence}%", row.package_label, row.reason]
        )
        item.setToolTip(0, row.path)
        return item

    def _build_heatmap_scope_item(self, scope_rows: tuple[str, List[TextureUsageHeatRow]]) -> QTreeWidgetItem:
        scope, rows = scope_rows
        parent = QTreeWidgetItem([scope, "", "", "", "", "", "", ""])
        parent.setFirstColumnSpanned(True)
        parent.setExpanded(True)
        for row in rows:
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
        return parent

    def _build_mip_item(self, row: MipAnalysisRow) -> QTreeWidgetItem:
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
        tooltip_lines = [*row.warnings]
        if row.planner_profile or row.planner_path_kind:
            tooltip_lines.extend(
                [
                    "",
                    f"Planner profile: {row.planner_profile or 'unavailable'}",
                    f"Planner path: {row.planner_path_kind or 'unavailable'}",
                    f"Planner path detail: {describe_processing_path_kind(row.planner_path_kind) if row.planner_path_kind else 'unavailable'}",
                    f"Planner backend mode: {row.planner_backend_mode or 'unavailable'}",
                    f"Planner alpha policy: {row.planner_alpha_policy or 'unavailable'}",
                ]
            )
            if row.planner_preserve_reason:
                tooltip_lines.append(f"Planner preserve reason: {row.planner_preserve_reason}")
        item.setToolTip(4, "\n".join(line for line in tooltip_lines if line))
        return item

    def _build_normal_item(self, row: NormalValidationRow) -> QTreeWidgetItem:
        item = QTreeWidgetItem([row.path, row.root_label, row.texconv_format, row.size_text, "; ".join(row.issues[:2])])
        item.setData(0, Qt.UserRole, row)
        tooltip_lines = [*row.issues]
        if row.planner_profile or row.planner_path_kind:
            tooltip_lines.extend(
                [
                    "",
                    f"Planner profile: {row.planner_profile or 'unavailable'}",
                    f"Planner path: {row.planner_path_kind or 'unavailable'}",
                    f"Planner path detail: {describe_processing_path_kind(row.planner_path_kind) if row.planner_path_kind else 'unavailable'}",
                    f"Planner backend mode: {row.planner_backend_mode or 'unavailable'}",
                    f"Planner alpha policy: {row.planner_alpha_policy or 'unavailable'}",
                ]
            )
            if row.planner_preserve_reason:
                tooltip_lines.append(f"Planner preserve reason: {row.planner_preserve_reason}")
        item.setToolTip(4, "\n".join(line for line in tooltip_lines if line))
        return item

    def _build_ui_constraint_item(self, row: MaterialTextureReferenceRow) -> QTreeWidgetItem:
        dds_size = f"{row.texture_width}x{row.texture_height}" if row.texture_width > 0 and row.texture_height > 0 else "-"
        item = QTreeWidgetItem(
            [
                row.related_path,
                row.source_path,
                dds_size,
                row.get_rect_raw or "-",
                row.constraint_kind or "No explicit UI rect found",
                row.related_package_label or row.source_package_label,
            ]
        )
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(0, row.related_path)
        item.setToolTip(1, row.source_path)
        item.setToolTip(2, row.warning_text or dds_size)
        item.setToolTip(3, row.warning_text or row.constraint_kind)
        return item

    def _build_budget_file_item(self, row: TextureBudgetRow) -> QTreeWidgetItem:
        size_text = f"{row.original_width}x{row.original_height} -> {row.rebuilt_width}x{row.rebuilt_height}"
        item = QTreeWidgetItem(
            [
                row.relative_path,
                f"{row.byte_delta:+,}",
                f"{row.byte_ratio:.2f}x",
                size_text,
                row.texture_type,
                f"{row.risk_score} ({row.risk_band})",
            ]
        )
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(0, row.relative_path)
        tooltip_lines = [
            f"Original bytes: {row.original_bytes:,}",
            f"Rebuilt bytes: {row.rebuilt_bytes:,}",
            f"Original format: {row.original_format}",
            f"Rebuilt format: {row.rebuilt_format}",
            f"Original mips: {row.original_mips}",
            f"Rebuilt mips: {row.rebuilt_mips}",
            "",
            *row.risk_signals,
        ]
        item.setToolTip(5, "\n".join(line for line in tooltip_lines if line))
        return item

    def _build_budget_class_item(self, row: TextureBudgetClassSummary) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                row.texture_type,
                f"{row.affected_count:,}",
                f"{row.total_byte_delta:+,}",
                f"{row.average_risk:.1f}",
                row.risk_band,
            ]
        )
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(0, "\n".join(row.sample_paths))
        return item

    def _build_budget_group_item(self, row: TextureBudgetGroupSummary) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                row.group_key,
                f"{row.texture_count:,}",
                f"{row.total_byte_delta:+,}",
                f"{row.average_byte_ratio:.2f}x",
                str(row.risk_score),
                row.risk_band,
            ]
        )
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(0, "\n".join(row.signals))
        return item

    def _build_budget_profile_item(self, row: TextureBudgetProfileSummary) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                row.profile_label,
                f"{row.total_byte_delta:+,}",
                f"{row.total_byte_ratio:.2f}x",
                f"{row.changed_texture_count:,}",
                f"{row.upscaled_texture_count:,}",
            ]
        )
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(0, "\n".join(row.reasons))
        return item

    def _begin_refresh_population(self) -> None:
        self._stop_refresh_population()
        self._refresh_unknown_resolver_view()
        texture_groups = [group for group in self.research_payload.get("texture_groups", []) if isinstance(group, TextureSetGroup)]
        classification_rows = [row for row in self.research_payload.get("classification_rows", []) if isinstance(row, TextureClassificationRow)]
        grouped_heatmap: Dict[str, List[TextureUsageHeatRow]] = {}
        heatmap_groups: List[tuple[str, List[TextureUsageHeatRow]]] = []
        for row in self.research_payload.get("heatmap_rows", []) if isinstance(self.research_payload.get("heatmap_rows", []), list) else []:
            if not isinstance(row, TextureUsageHeatRow):
                continue
            if row.scope not in grouped_heatmap:
                grouped_heatmap[row.scope] = []
                heatmap_groups.append((row.scope, grouped_heatmap[row.scope]))
            grouped_heatmap[row.scope].append(row)
        mip_rows = [row for row in self.research_payload.get("mip_rows", []) if isinstance(row, MipAnalysisRow)]
        normal_rows = [row for row in self.research_payload.get("normal_rows", []) if isinstance(row, NormalValidationRow)]
        ui_constraint_rows = [
            row for row in self.research_payload.get("ui_constraint_rows", []) if isinstance(row, MaterialTextureReferenceRow)
        ]
        budget_rows = [row for row in self.research_payload.get("budget_rows", []) if isinstance(row, TextureBudgetRow)]
        budget_class_rows = [
            row for row in self.research_payload.get("budget_class_rows", []) if isinstance(row, TextureBudgetClassSummary)
        ]
        budget_group_rows = [
            row for row in self.research_payload.get("budget_group_rows", []) if isinstance(row, TextureBudgetGroupSummary)
        ]
        budget_profile = self.research_payload.get("budget_profile")

        self.texture_group_tree.clear()
        self.classifier_tree.clear()
        self.heatmap_tree.clear()
        self.mip_tree.clear()
        self.normal_tree.clear()
        self.ui_constraint_tree.clear()
        self.budget_file_tree.clear()
        self.budget_class_tree.clear()
        self.budget_group_tree.clear()
        self.budget_profile_tree.clear()

        self._refresh_population_phases = [
            {
                "name": "texture groups",
                "items": texture_groups,
                "cursor": 0,
                "tree": self.texture_group_tree,
                "build": self._build_texture_group_item,
                "finalize": self._finalize_texture_group_population,
                "batch_size": self.REFRESH_GROUP_BATCH_SIZE,
            },
            {
                "name": "classifications",
                "items": classification_rows,
                "cursor": 0,
                "tree": self.classifier_tree,
                "build": self._build_classification_item,
                "finalize": self._finalize_classification_population,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "usage heatmap",
                "items": heatmap_groups,
                "cursor": 0,
                "tree": self.heatmap_tree,
                "build": self._build_heatmap_scope_item,
                "finalize": self._finalize_heatmap_population,
                "batch_size": self.REFRESH_GROUP_BATCH_SIZE,
            },
            {
                "name": "mip analysis",
                "items": mip_rows,
                "cursor": 0,
                "tree": self.mip_tree,
                "build": self._build_mip_item,
                "finalize": self._finalize_mip_population,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "normal validation",
                "items": normal_rows,
                "cursor": 0,
                "tree": self.normal_tree,
                "build": self._build_normal_item,
                "finalize": self._finalize_normal_population,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "ui constraints",
                "items": ui_constraint_rows,
                "cursor": 0,
                "tree": self.ui_constraint_tree,
                "build": self._build_ui_constraint_item,
                "finalize": self._finalize_ui_constraint_population,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "budget files",
                "items": budget_rows,
                "cursor": 0,
                "tree": self.budget_file_tree,
                "build": self._build_budget_file_item,
                "finalize": self._finalize_budget_population,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "budget classes",
                "items": budget_class_rows,
                "cursor": 0,
                "tree": self.budget_class_tree,
                "build": self._build_budget_class_item,
                "finalize": None,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "budget groups",
                "items": budget_group_rows,
                "cursor": 0,
                "tree": self.budget_group_tree,
                "build": self._build_budget_group_item,
                "finalize": None,
                "batch_size": self.REFRESH_POPULATION_BATCH_SIZE,
            },
            {
                "name": "budget profile",
                "items": [budget_profile] if isinstance(budget_profile, TextureBudgetProfileSummary) else [],
                "cursor": 0,
                "tree": self.budget_profile_tree,
                "build": self._build_budget_profile_item,
                "finalize": None,
                "batch_size": 1,
            },
        ]
        self._refresh_population_total = sum(len(phase["items"]) for phase in self._refresh_population_phases)
        self._refresh_population_processed = 0
        if self._refresh_population_total <= 0:
            self._finish_refresh_population()
            return
        self.refresh_status_label.setText(
            (
                f"Populating research snapshot for the full loaded archive... 0 / {self._refresh_population_total:,}"
                if self._pending_research_uses_full_archive_view
                else f"Populating research snapshot for the current Archive Browser view... 0 / {self._refresh_population_total:,}"
            )
        )
        self.refresh_progress.setRange(0, self._refresh_population_total)
        self.refresh_progress.setValue(0)
        self.refresh_progress.setFormat(f"0 / {self._refresh_population_total}")
        self._refresh_population_timer.start()

    def _flush_refresh_population_batch(self) -> None:
        while self._refresh_population_phase_index < len(self._refresh_population_phases):
            phase = self._refresh_population_phases[self._refresh_population_phase_index]
            items = phase["items"]
            cursor = int(phase.get("cursor", 0))
            if cursor >= len(items):
                finalize = phase.get("finalize")
                if callable(finalize):
                    finalize()
                self._refresh_population_phase_index += 1
                continue
            batch_size = max(1, int(phase.get("batch_size", self.REFRESH_POPULATION_BATCH_SIZE)))
            end = min(cursor + batch_size, len(items))
            build = phase.get("build")
            tree = phase.get("tree")
            if not callable(build) or not isinstance(tree, QTreeWidget):
                self._refresh_population_phase_index += 1
                continue
            built = [build(item) for item in items[cursor:end]]
            tree.setUpdatesEnabled(False)
            tree.addTopLevelItems(built)
            tree.setUpdatesEnabled(True)
            phase["cursor"] = end
            self._refresh_population_processed += end - cursor
            self.refresh_status_label.setText(
                f"Populating {phase.get('name', 'research')}... {self._refresh_population_processed:,} / {self._refresh_population_total:,}"
            )
            self.refresh_progress.setRange(0, self._refresh_population_total)
            self.refresh_progress.setValue(self._refresh_population_processed)
            self.refresh_progress.setFormat(f"{self._refresh_population_processed} / {self._refresh_population_total}")
            if end < len(items):
                self._refresh_population_timer.start()
                return
        self._finish_refresh_population()

    def _finalize_texture_group_population(self) -> None:
        first_group_item = self.texture_group_tree.topLevelItem(0)
        if first_group_item is not None:
            self.texture_group_tree.setCurrentItem(first_group_item)
            self.texture_group_status_label.setText(
                f"Selected group: {first_group_item.text(0)}. Click 'Extract Selected Set' to extract its related files and sidecars."
            )
            self.texture_group_extract_button.setEnabled(True)
        else:
            self.texture_group_status_label.setText("No grouped texture sets are available in the current Research snapshot.")
            self.texture_group_extract_button.setEnabled(False)

    def _finalize_classification_population(self) -> None:
        return

    def _finalize_heatmap_population(self) -> None:
        return

    def _finalize_mip_population(self) -> None:
        if self.mip_tree.topLevelItemCount() > 0:
            first = self.mip_tree.topLevelItem(0)
            if first is not None:
                self.mip_tree.setCurrentItem(first)

    def _finalize_normal_population(self) -> None:
        if self.normal_tree.topLevelItemCount() > 0 and self.mip_tree.topLevelItemCount() == 0:
            first = self.normal_tree.topLevelItem(0)
            if first is not None:
                self.normal_tree.setCurrentItem(first)
        if self.normal_tree.topLevelItemCount() == 0 and self.mip_tree.topLevelItemCount() == 0:
            self.analysis_detail_label.setText(
                "Select a row in Texture Analysis to see where the result came from and what it means."
            )
            self.analysis_detail_edit.clear()

    def _finalize_ui_constraint_population(self) -> None:
        if self.ui_constraint_tree.topLevelItemCount() > 0:
            first = self.ui_constraint_tree.topLevelItem(0)
            if first is not None:
                self.ui_constraint_tree.setCurrentItem(first)

    def _finalize_budget_population(self) -> None:
        if self.budget_file_tree.topLevelItemCount() > 0:
            first = self.budget_file_tree.topLevelItem(0)
            if first is not None:
                self.budget_file_tree.setCurrentItem(first)

    def _finish_refresh_population(self) -> None:
        self._stop_refresh_population()
        self._refresh_texture_analysis_summary()
        self.refresh_status_label.setText(
            (
                f"Research snapshot ready for the full loaded archive ({self._pending_research_full_archive_entry_count:,} file(s))."
                if self._pending_research_uses_full_archive_view
                else f"Research snapshot ready for the current Archive Browser view ({self._pending_research_view_entry_count:,} file(s))."
            )
        )
        self.refresh_progress.setRange(0, 1)
        self.refresh_progress.setValue(1)
        self.refresh_progress.setFormat("Ready")
        self.status_message_requested.emit(self.refresh_status_label.text(), False)
        self._focus_pending_mip_row()

    def _populate_texture_groups(self, groups: object) -> None:
        self.texture_group_tree.setUpdatesEnabled(False)
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
        self.texture_group_tree.setUpdatesEnabled(True)

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
        self.classifier_tree.setUpdatesEnabled(False)
        self.classifier_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, TextureClassificationRow):
                continue
            item = QTreeWidgetItem(
                [PurePosixPath(row.path).name, row.texture_type, f"{row.confidence}%", row.package_label, row.reason]
            )
            item.setToolTip(0, row.path)
            self.classifier_tree.addTopLevelItem(item)
        self.classifier_tree.setUpdatesEnabled(True)

    def _current_unknown_resolver_groups(self) -> object:
        if self.unknown_show_classified_checkbox.isChecked():
            return self.research_payload.get("classification_review_groups", [])
        return self.research_payload.get("unknown_resolver_groups", [])

    @staticmethod
    def _normalize_classification_review_focus_key(path_value: str) -> str:
        return str(path_value or "").strip().replace("\\", "/").strip("/").casefold()

    @classmethod
    def _classification_review_focus_candidates(cls, path_value: str) -> set[str]:
        normalized = str(path_value or "").strip().replace("\\", "/").strip("/")
        if not normalized:
            return set()
        candidates = {cls._normalize_classification_review_focus_key(normalized)}
        parts = normalized.split("/")
        if len(parts) > 1 and len(parts[0]) == 4 and parts[0].isdigit():
            stripped = "/".join(parts[1:]).strip("/")
            if stripped:
                candidates.add(cls._normalize_classification_review_focus_key(stripped))
        return candidates

    def _clear_pending_classification_review_focus(self) -> None:
        self.pending_classification_review_focus_keys.clear()
        self._classification_review_focus_uses_full_archive = False

    def focus_classification_review_for_paths(
        self,
        paths: Sequence[str],
        *,
        include_classified: bool = False,
        refresh_if_needed: bool = False,
    ) -> None:
        focus_keys: set[str] = set()
        for path_value in paths:
            focus_keys.update(self._classification_review_focus_candidates(path_value))
        self.pending_classification_review_focus_keys = focus_keys
        self._classification_review_focus_uses_full_archive = bool(focus_keys)
        self.unknown_name_filter_edit.blockSignals(True)
        self.unknown_package_filter_edit.blockSignals(True)
        try:
            self.unknown_name_filter_edit.clear()
            self.unknown_package_filter_edit.clear()
        finally:
            self.unknown_name_filter_edit.blockSignals(False)
            self.unknown_package_filter_edit.blockSignals(False)
        self.unknown_show_classified_checkbox.setChecked(include_classified)
        if focus_keys:
            self.unknown_resolver_status_label.setText(
                "Workflow needs a saved local approval for the selected DDS file(s). 'Current' can be inferred from archive context; 'Local' is the explicit saved approval Texture Workflow requires."
            )
        self.tab_widget.setCurrentWidget(self.archive_tab)
        self.archive_insights_tabs.setCurrentWidget(self.classification_review_tab)
        if refresh_if_needed or not self.research_payload:
            self.refresh_research()
        else:
            self._refresh_unknown_resolver_view()

    @staticmethod
    def _wildcard_filter_matches(value: str, pattern_text: str) -> bool:
        normalized_value = value.casefold()
        normalized_pattern = pattern_text.strip().casefold()
        if not normalized_pattern:
            return True
        if "*" not in normalized_pattern and "?" not in normalized_pattern:
            normalized_pattern = f"*{normalized_pattern}*"
        return fnmatch.fnmatchcase(normalized_value, normalized_pattern)

    def _unknown_group_display_name(self, group: UnknownResolverGroup) -> str:
        member = self._primary_unknown_member(group)
        if member is None:
            return group.display_name
        basename = PurePosixPath(member.path).name
        extra_members = max(group.total_members - 1, 0)
        return f"{basename} (+{extra_members})" if extra_members > 0 else basename

    def _unknown_group_classification_text(self, group: UnknownResolverGroup) -> str:
        if group.unknown_count > 0:
            return group.suggestion_label or "Unknown"
        return ", ".join(group.known_kinds) if group.known_kinds else "Classified"

    def _unknown_group_package_text(self, group: UnknownResolverGroup) -> str:
        return ", ".join(group.package_labels[:2]) + ("..." if len(group.package_labels) > 2 else "")

    @staticmethod
    def _unknown_member_local_text(member: UnknownResolverMember) -> str:
        local_texture_type = str(member.local_texture_type or "").strip().lower()
        local_semantic_subtype = str(member.local_semantic_subtype or "").strip().lower()
        if not local_texture_type:
            return "No"
        return (
            f"Yes: {local_texture_type}/{local_semantic_subtype}"
            if local_semantic_subtype
            else f"Yes: {local_texture_type}"
        )

    def _unknown_group_matches_filters(self, group: UnknownResolverGroup) -> bool:
        if self.pending_classification_review_focus_keys:
            if not any(
                self._classification_review_focus_candidates(member.path) & self.pending_classification_review_focus_keys
                for member in group.members
                if member.extension == ".dds"
            ):
                return False
        name_filter = self.unknown_name_filter_edit.text().strip()
        package_filter = self.unknown_package_filter_edit.text().strip()
        if name_filter:
            name_candidates = [group.display_name, group.group_key, self._unknown_group_display_name(group)]
            primary_member = self._primary_unknown_member(group)
            if primary_member is not None:
                name_candidates.append(primary_member.path)
            if not any(self._wildcard_filter_matches(candidate, name_filter) for candidate in name_candidates if candidate):
                return False
        if package_filter:
            if not any(self._wildcard_filter_matches(package_label, package_filter) for package_label in group.package_labels):
                return False
        return True

    def _handle_unknown_show_classified_toggled(self, _checked: bool) -> None:
        self.unknown_resolver_status_label.setText("Refreshing classification review for the current Research snapshot...")
        QTimer.singleShot(0, self._refresh_unknown_resolver_view)

    def _handle_unknown_name_filter_changed(self, _text: str) -> None:
        self._clear_pending_classification_review_focus()
        self._refresh_unknown_resolver_view()

    def _handle_unknown_package_filter_changed(self, _text: str) -> None:
        self._clear_pending_classification_review_focus()
        self._refresh_unknown_resolver_view()

    def _refresh_unknown_resolver_view(self) -> None:
        self._populate_unknown_resolver(self._current_unknown_resolver_groups())

    def _populate_unknown_resolver(self, groups: object) -> None:
        previous_group = self._current_unknown_group()
        previous_group_key = previous_group.group_key if previous_group is not None else ""
        self._unknown_population_timer.stop()
        self.unknown_group_tree.blockSignals(True)
        self.unknown_group_tree.setUpdatesEnabled(False)
        try:
            self.unknown_group_tree.clear()
        finally:
            self.unknown_group_tree.setUpdatesEnabled(True)
            self.unknown_group_tree.blockSignals(False)
        self._pending_unknown_source_groups = [
            group
            for group in groups
            if isinstance(group, UnknownResolverGroup)
        ]
        self._pending_unknown_groups = []
        self._pending_unknown_previous_group_key = previous_group_key
        self._pending_unknown_showing_classified = self.unknown_show_classified_checkbox.isChecked()
        self._pending_unknown_population_total = 0
        self._pending_unknown_scanned_total = len(self._pending_unknown_source_groups)
        if not self._pending_unknown_source_groups:
            self._finalize_unknown_group_population()
            return
        self.unknown_resolver_status_label.setText(
            f"Filtering classification review... 0 / {self._pending_unknown_scanned_total:,} scanned | 0 matched"
        )
        self._update_unknown_resolver_controls()
        self._unknown_population_timer.start()

    def _build_unknown_group_item(self, group: UnknownResolverGroup) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            [
                self._unknown_group_display_name(group),
                self._unknown_group_classification_text(group),
                group.local_approval_state,
                self._unknown_group_package_text(group),
            ]
        )
        item.setData(0, Qt.UserRole, group)
        item.setToolTip(0, group.group_key)
        item.setToolTip(1, self._unknown_group_classification_text(group))
        item.setToolTip(2, group.local_approval_state)
        item.setToolTip(3, ", ".join(group.package_labels))
        return item

    def _flush_unknown_group_population_batch(self) -> None:
        if self._pending_unknown_source_groups:
            batch = self._pending_unknown_source_groups[: self.UNKNOWN_GROUP_BATCH_SIZE]
            del self._pending_unknown_source_groups[: self.UNKNOWN_GROUP_BATCH_SIZE]
            matched_groups = [group for group in batch if self._unknown_group_matches_filters(group)]
            self._pending_unknown_groups.extend(matched_groups)
            items = [self._build_unknown_group_item(group) for group in matched_groups]
            if items:
                self.unknown_group_tree.setUpdatesEnabled(False)
                self.unknown_group_tree.addTopLevelItems(items)
                self.unknown_group_tree.setUpdatesEnabled(True)
            self._pending_unknown_population_total += len(matched_groups)
            scanned = self._pending_unknown_scanned_total - len(self._pending_unknown_source_groups)
            self.unknown_resolver_status_label.setText(
                f"Filtering classification review... {scanned:,} / {self._pending_unknown_scanned_total:,} scanned | {self._pending_unknown_population_total:,} matched"
            )
            self._unknown_population_timer.start()
            return
        self._finalize_unknown_group_population()

    def _finalize_unknown_group_population(self) -> None:
        if self.unknown_group_tree.topLevelItemCount() <= 0:
            self.unknown_member_tree.clear()
            self.unknown_members_group.setVisible(False)
            self.unknown_detail_edit.clear()
            self._clear_unknown_preview("No matching DDS preview is available for the current review filter.")
            self.unknown_resolver_status_label.setText(
                (
                    "No review items are available in the current Research snapshot."
                    if self._pending_unknown_showing_classified
                    else "No unresolved review items match the current filters."
                )
                if not self.pending_classification_review_focus_keys
                else "No current-run unclassified DDS files matched the current Research snapshot. Scan archives or broaden the current Archive Browser view if needed."
            )
            self._update_unknown_resolver_controls()
            return
        selected_item: Optional[QTreeWidgetItem] = None
        if self._pending_unknown_previous_group_key:
            for index in range(self.unknown_group_tree.topLevelItemCount()):
                item = self.unknown_group_tree.topLevelItem(index)
                value = item.data(0, Qt.UserRole)
                if isinstance(value, UnknownResolverGroup) and value.group_key == self._pending_unknown_previous_group_key:
                    selected_item = item
                    break
        first_item = self.unknown_group_tree.topLevelItem(0)
        if first_item is not None:
            self.unknown_group_tree.setCurrentItem(selected_item or first_item)
            registry_text = str(self.classification_registry_path) if self.classification_registry_path is not None else "local registry"
            self.unknown_resolver_status_label.setText(
                (
                    f"{self._pending_unknown_population_total:,} review item(s) are available. Approved labels are stored in {registry_text}."
                    if self._pending_unknown_showing_classified
                    else f"{self._pending_unknown_population_total:,} unresolved item(s) need review. Approved labels are stored in {registry_text}."
                )
            )
            if self.pending_classification_review_focus_keys:
                self.unknown_resolver_status_label.setText(
                    self.unknown_resolver_status_label.text()
                    + " Showing the current run's targeted DDS files. 'Current' can be inferred from Research; 'Local' is what Texture Workflow requires."
                )
        self._update_unknown_resolver_controls()

    def _current_unknown_group(self) -> Optional[UnknownResolverGroup]:
        item = self.unknown_group_tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.UserRole)
        return value if isinstance(value, UnknownResolverGroup) else None

    def _selected_unknown_groups(self) -> List[UnknownResolverGroup]:
        groups: List[UnknownResolverGroup] = []
        seen_keys: set[str] = set()
        for item in self.unknown_group_tree.selectedItems():
            value = item.data(0, Qt.UserRole)
            if not isinstance(value, UnknownResolverGroup):
                continue
            if value.group_key in seen_keys:
                continue
            seen_keys.add(value.group_key)
            groups.append(value)
        return groups

    def _primary_unknown_member(self, group: Optional[UnknownResolverGroup]) -> Optional[UnknownResolverMember]:
        if group is None:
            return None
        for member in group.members:
            if member.is_unknown and member.extension == ".dds":
                return member
        for member in group.members:
            if member.extension == ".dds":
                return member
        return group.members[0] if group.members else None

    def _current_unknown_member(self) -> Optional[UnknownResolverMember]:
        item = self.unknown_member_tree.currentItem()
        if item is not None:
            value = item.data(0, Qt.UserRole)
            if isinstance(value, UnknownResolverMember):
                return value
        return self._primary_unknown_member(self._current_unknown_group())

    def _update_unknown_member_group_visibility(self, group: Optional[UnknownResolverGroup]) -> None:
        self.unknown_members_group.setVisible(bool(group is not None and group.total_members > 1))

    def _unknown_group_target_paths(
        self,
        groups: Sequence[UnknownResolverGroup],
        *,
        unknown_only: bool,
    ) -> List[str]:
        target_paths: List[str] = []
        seen_paths: set[str] = set()
        for group in groups:
            for member in group.members:
                if member.extension != ".dds":
                    continue
                if unknown_only and not member.is_unknown:
                    continue
                if member.path in seen_paths:
                    continue
                seen_paths.add(member.path)
                target_paths.append(member.path)
        return target_paths

    def _current_unknown_classifiable_member(self) -> Optional[UnknownResolverMember]:
        member = self._current_unknown_member()
        if member is None or member.extension != ".dds":
            return None
        return member

    def _handle_unknown_group_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        self._ensure_archive_picker_ready()
        group = current.data(0, Qt.UserRole) if current is not None else None
        if not isinstance(group, UnknownResolverGroup):
            self.unknown_member_tree.clear()
            self._update_unknown_member_group_visibility(None)
            self.unknown_detail_edit.clear()
            self._clear_unknown_preview("Select a DDS review item to preview it here.")
            self._update_unknown_resolver_controls()
            return
        self._populating_unknown_resolver_controls = True
        try:
            self.unknown_member_tree.blockSignals(True)
            self.unknown_member_tree.clear()
            self._update_unknown_member_group_visibility(group)
            focused_member_item: Optional[QTreeWidgetItem] = None
            focused_member: Optional[UnknownResolverMember] = None
            for member in group.members:
                item = QTreeWidgetItem(
                    [
                        PurePosixPath(member.path).name,
                        member.current_kind,
                        self._unknown_member_local_text(member),
                        member.role_hint or "-",
                        member.package_label,
                        member.reason,
                    ]
                )
                item.setData(0, Qt.UserRole, member)
                item.setToolTip(0, member.path)
                self.unknown_member_tree.addTopLevelItem(item)
                if (
                    focused_member_item is None
                    and self.pending_classification_review_focus_keys
                    and self._classification_review_focus_candidates(member.path) & self.pending_classification_review_focus_keys
                ):
                    focused_member_item = item
                    focused_member = member
            suggested_choice = self._preferred_unknown_choice_for_member(
                focused_member or self._primary_unknown_member(group),
                group,
            )
            self._select_unknown_label_choice(suggested_choice)
        finally:
            self.unknown_member_tree.blockSignals(False)
            self._populating_unknown_resolver_controls = False
        if self.unknown_member_tree.topLevelItemCount() > 0:
            first_member = focused_member_item or self.unknown_member_tree.topLevelItem(0)
            if first_member is not None:
                self.unknown_member_tree.setCurrentItem(first_member)
        else:
            self.unknown_detail_edit.setPlainText("No reviewable members found in this unknown family.")
        self._update_unknown_resolver_controls()

    def _handle_unknown_group_item_selection_changed(self) -> None:
        self._update_unknown_resolver_controls()

    def _handle_unknown_member_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        self._ensure_archive_picker_ready()
        member = current.data(0, Qt.UserRole) if current is not None else None
        group = self._current_unknown_group()
        if not isinstance(member, UnknownResolverMember) or group is None:
            self.unknown_detail_edit.clear()
            self._clear_unknown_preview("Select a DDS review item to preview it here.")
            self._update_unknown_resolver_controls()
            return
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        detail_text = build_unknown_resolver_detail(
            group,
            member.path,
            entries_by_path=self.archive_picker_entry_by_path,
            texconv_path=texconv_path,
        )
        self.unknown_detail_edit.setPlainText(detail_text)
        self._select_unknown_label_choice(self._preferred_unknown_choice_for_member(member, group))
        self._render_unknown_preview_for_member(member)
        self._focus_archive_picker_path(member.path)
        self._update_unknown_resolver_controls()

    def _preview_selected_unknown_member(self) -> None:
        member = self._current_unknown_member()
        if member is None:
            return
        self._render_unknown_preview_for_member(member)
        self._focus_archive_picker_path(member.path)

    def _select_all_unknown_groups(self) -> None:
        if self.unknown_group_tree.topLevelItemCount() <= 0:
            return
        current = self.unknown_group_tree.currentItem()
        if current is None:
            current = self.unknown_group_tree.topLevelItem(0)
        self.unknown_group_tree.blockSignals(True)
        try:
            self.unknown_group_tree.selectAll()
            if current is not None:
                self.unknown_group_tree.setCurrentItem(current)
        finally:
            self.unknown_group_tree.blockSignals(False)
        if current is not None:
            self._handle_unknown_group_selection_changed(current, None)
        self._update_unknown_resolver_controls()

    def _clear_unknown_group_selection(self) -> None:
        self.unknown_group_tree.blockSignals(True)
        try:
            self.unknown_group_tree.clearSelection()
        finally:
            self.unknown_group_tree.blockSignals(False)
        self._update_unknown_resolver_controls()

    def _selected_unknown_label(self) -> tuple[str, str, str]:
        raw = self.unknown_label_combo.currentData()
        if isinstance(raw, tuple) and len(raw) == 3:
            return (str(raw[0]), str(raw[1]), str(raw[2]))
        return ("color_albedo", "color", "albedo")

    @staticmethod
    def _semantic_subtype_for_current_member(member: UnknownResolverMember) -> str:
        texture_type = str(member.current_kind or "").strip().lower()
        path_lower = member.path.lower()
        if texture_type == "mask":
            if any(token in path_lower for token in ("specular", "_spec", "_sp")):
                return "specular"
            if any(token in path_lower for token in ("opacity", "alpha", "_mask")):
                return "opacity_mask"
            return "mask"
        if texture_type == "color":
            return "albedo"
        if texture_type == "ui":
            return "ui"
        if texture_type == "emissive":
            return "emissive"
        if texture_type == "normal":
            return "normal"
        if texture_type == "roughness":
            return "roughness"
        if texture_type == "height":
            return "displacement"
        if texture_type == "vector":
            return "vector"
        return texture_type or "unknown"

    def _preferred_unknown_choice_for_member(
        self,
        member: Optional[UnknownResolverMember],
        group: Optional[UnknownResolverGroup],
    ) -> str:
        if member is not None:
            texture_type = str(member.current_kind or "").strip().lower()
            if texture_type and texture_type not in {"unknown", "sidecar"}:
                semantic_subtype = self._semantic_subtype_for_current_member(member)
                return unknown_resolver_choice_for(texture_type, semantic_subtype)
        if group is not None and group.suggestions:
            return group.suggestions[0].choice_key
        return default_unknown_resolver_label_choice()

    def _select_unknown_label_choice(self, choice_key: str) -> None:
        combo_index = self.unknown_label_combo.findData(next(
            (
                data
                for data in [
                    self.unknown_label_combo.itemData(index)
                    for index in range(self.unknown_label_combo.count())
                ]
                if isinstance(data, tuple) and data and data[0] == choice_key
            ),
            None,
        ))
        if combo_index >= 0:
            self.unknown_label_combo.setCurrentIndex(combo_index)

    def _accept_unknown_current_role(self) -> None:
        member = self._current_unknown_classifiable_member()
        if member is None:
            self.status_message_requested.emit("Select a DDS file in Family Members first.", True)
            return
        texture_type = str(member.current_kind or "").strip().lower()
        if not texture_type or texture_type in {"unknown", "sidecar"}:
            self.status_message_requested.emit(
                "The selected DDS does not currently have a concrete role to accept yet.",
                True,
            )
            return
        semantic_subtype = self._semantic_subtype_for_current_member(member)
        updated = set_registered_texture_classifications(
            [member.path],
            texture_type,
            semantic_subtype,
            source="unknown_resolver",
            note=f"Accepted current Research role for file {member.path}",
        )
        if updated:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Saved current role locally as {texture_type}/{semantic_subtype} for the selected DDS file. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                [member.path],
                include_classified=True,
                refresh_if_needed=True,
            )

    def _apply_unknown_current_file_label(self) -> None:
        member = self._current_unknown_classifiable_member()
        if member is None:
            self.status_message_requested.emit("Select a DDS file in Family Members first.", True)
            return
        _choice_key, texture_type, semantic_subtype = self._selected_unknown_label()
        updated = set_registered_texture_classifications(
            [member.path],
            texture_type,
            semantic_subtype,
            source="unknown_resolver",
            note=f"Approved from Research -> Classification Review for file {member.path}",
        )
        if updated:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Saved classification {texture_type}/{semantic_subtype} for the current DDS file. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                [member.path],
                include_classified=self.unknown_show_classified_checkbox.isChecked(),
                refresh_if_needed=True,
            )

    def _apply_unknown_selected_file_label(self) -> None:
        group = self._current_unknown_group()
        if group is None:
            self.status_message_requested.emit("Select a texture family first.", True)
            return
        target_paths = self._unknown_group_target_paths([group], unknown_only=True)
        if not target_paths:
            self.status_message_requested.emit("No unknown DDS files remain in the current family.", True)
            return
        _choice_key, texture_type, semantic_subtype = self._selected_unknown_label()
        updated = set_registered_texture_classifications(
            target_paths,
            texture_type,
            semantic_subtype,
            source="unknown_resolver",
            note=f"Approved from Research -> Classification Review for family {group.group_key}",
        )
        if updated:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Saved classification {texture_type}/{semantic_subtype} for {updated} file(s) in the current family. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                self._unknown_group_target_paths([group], unknown_only=False),
                include_classified=self.unknown_show_classified_checkbox.isChecked(),
                refresh_if_needed=True,
            )

    def _apply_unknown_group_label(self) -> None:
        groups = self._selected_unknown_groups()
        if not groups:
            self.status_message_requested.emit("Select one or more texture families first.", True)
            return
        target_paths = self._unknown_group_target_paths(groups, unknown_only=True)
        if not target_paths:
            self.status_message_requested.emit("No unknown DDS files remain in the selected families.", True)
            return
        _choice_key, texture_type, semantic_subtype = self._selected_unknown_label()
        updated = set_registered_texture_classifications(
            target_paths,
            texture_type,
            semantic_subtype,
            source="unknown_resolver",
            note="Approved from Research -> Classification Review for selected families",
        )
        if updated:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Saved classification {texture_type}/{semantic_subtype} for {updated} file(s) across {len(groups)} selected family/families. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                self._unknown_group_target_paths(groups, unknown_only=False),
                include_classified=self.unknown_show_classified_checkbox.isChecked(),
                refresh_if_needed=True,
            )

    def _clear_unknown_current_file_label(self) -> None:
        member = self._current_unknown_classifiable_member()
        if member is None:
            self.status_message_requested.emit("Select a DDS file in Family Members first.", True)
            return
        removed = remove_registered_texture_classifications([member.path])
        if removed:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                "Removed the saved classification override from the current DDS file. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                [member.path],
                include_classified=True,
                refresh_if_needed=True,
            )

    def _clear_unknown_selected_file_label(self) -> None:
        group = self._current_unknown_group()
        if group is None:
            self.status_message_requested.emit("Select a texture family first.", True)
            return
        removed = remove_registered_texture_classifications(
            self._unknown_group_target_paths([group], unknown_only=False)
        )
        if removed:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Removed {removed} saved classification override(s) from the current family. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                self._unknown_group_target_paths([group], unknown_only=False),
                include_classified=True,
                refresh_if_needed=True,
            )

    def _clear_unknown_group_label(self) -> None:
        groups = self._selected_unknown_groups()
        if not groups:
            self.status_message_requested.emit("Select one or more texture families first.", True)
            return
        target_paths = self._unknown_group_target_paths(groups, unknown_only=False)
        removed = remove_registered_texture_classifications(target_paths)
        if removed:
            self.archive_snapshot_cache.clear()
            self.status_message_requested.emit(
                f"Removed {removed} saved classification override(s) across {len(groups)} selected family/families. Refreshing Research...",
                False,
            )
            self.focus_classification_review_for_paths(
                self._unknown_group_target_paths(groups, unknown_only=False),
                include_classified=True,
                refresh_if_needed=True,
            )

    def _update_unknown_resolver_controls(self) -> None:
        has_group = self._current_unknown_group() is not None
        has_selected_groups = bool(self._selected_unknown_groups())
        current_member = self._current_unknown_classifiable_member()
        has_member = current_member is not None
        has_accept_current_role = bool(
            current_member is not None
            and str(current_member.current_kind or "").strip().lower() not in {"", "unknown", "sidecar"}
        )
        self.unknown_label_combo.setEnabled(has_group)
        self.unknown_preview_button.setEnabled(has_member)
        self.unknown_accept_current_role_button.setEnabled(has_accept_current_role)
        self.unknown_apply_current_file_button.setEnabled(has_member)
        self.unknown_apply_selected_button.setEnabled(has_group)
        self.unknown_apply_group_button.setEnabled(has_selected_groups)
        self.unknown_clear_current_file_button.setEnabled(has_member)
        self.unknown_clear_selected_button.setEnabled(has_group)
        self.unknown_clear_group_button.setEnabled(has_selected_groups)
        has_rows = self.unknown_group_tree.topLevelItemCount() > 0
        self.unknown_select_all_button.setEnabled(has_rows)
        self.unknown_clear_family_selection_button.setEnabled(has_rows and has_selected_groups)

    def _set_unknown_preview_image_controls_enabled(self, enabled: bool) -> None:
        self.unknown_preview_zoom_out_button.setEnabled(enabled)
        self.unknown_preview_zoom_fit_button.setEnabled(enabled)
        self.unknown_preview_zoom_100_button.setEnabled(enabled)
        self.unknown_preview_zoom_in_button.setEnabled(enabled)
        if not enabled:
            self.unknown_preview_zoom_value.setText("-")
        else:
            self._update_unknown_preview_zoom_label()

    def _update_unknown_preview_zoom_label(self) -> None:
        if self.unknown_preview_fit_to_view:
            self.unknown_preview_zoom_value.setText("Fit")
        else:
            self.unknown_preview_zoom_value.setText(f"{int(round(self.unknown_preview_zoom_factor * 100))}%")

    def _apply_unknown_preview_zoom(self) -> None:
        self.unknown_preview_label.set_fit_to_view(self.unknown_preview_fit_to_view)
        self.unknown_preview_label.set_zoom_factor(self.unknown_preview_zoom_factor)
        self._update_unknown_preview_zoom_label()

    def _set_unknown_preview_fit_mode(self) -> None:
        self.unknown_preview_fit_to_view = True
        self._apply_unknown_preview_zoom()

    def _set_unknown_preview_zoom_factor(self, zoom_factor: float) -> None:
        self.unknown_preview_fit_to_view = False
        self.unknown_preview_zoom_factor = min(max(zoom_factor, 0.1), 16.0)
        self._apply_unknown_preview_zoom()

    def _adjust_unknown_preview_zoom(self, step: int) -> None:
        current_zoom = (
            self.unknown_preview_label.current_display_scale()
            if self.unknown_preview_fit_to_view
            else self.unknown_preview_zoom_factor
        )
        zoom_steps = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]
        closest_index = min(range(len(zoom_steps)), key=lambda idx: abs(zoom_steps[idx] - current_zoom))
        next_index = min(max(closest_index + step, 0), len(zoom_steps) - 1)
        self._set_unknown_preview_zoom_factor(zoom_steps[next_index])

    def _clear_unknown_preview(self, message: str) -> None:
        self.unknown_preview_title_label.setText("Select an unknown family member")
        self.unknown_preview_meta_label.setText(message)
        self.unknown_preview_warning_label.clear()
        self.unknown_preview_warning_label.setVisible(False)
        self.unknown_preview_info_edit.setPlainText(message)
        self.unknown_preview_label.clear_preview(message)
        self.unknown_preview_stack.setCurrentWidget(self.unknown_preview_info_edit)
        self._set_unknown_preview_image_controls_enabled(False)

    def _render_unknown_preview_for_member(self, member: Optional[UnknownResolverMember]) -> None:
        self._ensure_archive_picker_ready()
        entry = (
            self.archive_picker_entry_by_path.get(self._normalize_archive_path(member.path))
            if member is not None
            else None
        )
        request_id = self.unknown_preview_request_id + 1
        self.unknown_preview_request_id = request_id
        if entry is None:
            self.pending_unknown_preview_request = None
            self._clear_unknown_preview("No archive preview is available for the selected item in the current archive view.")
            return

        self.unknown_preview_title_label.setText(entry.basename)
        self.unknown_preview_meta_label.setText("Loading preview...")
        self.unknown_preview_warning_label.setVisible(False)
        self.unknown_preview_warning_label.clear()
        self.unknown_preview_info_edit.setPlainText("Preparing preview...")
        self.unknown_preview_stack.setCurrentWidget(self.unknown_preview_info_edit)
        self.pending_unknown_preview_request = None

        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        if self.unknown_preview_thread is not None:
            self.pending_unknown_preview_request = (request_id, entry)
            if self.unknown_preview_worker is not None:
                self.unknown_preview_worker.stop()
            return
        self._start_unknown_preview_worker(request_id, texconv_path, entry)

    def _start_unknown_preview_worker(
        self,
        request_id: int,
        texconv_path: Optional[Path],
        entry: Optional[ArchiveEntry],
    ) -> None:
        worker = UnknownResolverPreviewWorker(request_id, texconv_path, entry)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_unknown_preview_ready)
        worker.error.connect(self._handle_unknown_preview_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_unknown_preview_refs)
        self.unknown_preview_worker = worker
        self.unknown_preview_thread = thread
        thread.start()

    def _handle_unknown_preview_ready(self, request_id: int, payload: object) -> None:
        if request_id != self.unknown_preview_request_id:
            return
        if isinstance(payload, ArchivePreviewResult):
            self._apply_unknown_preview_result(payload)

    def _handle_unknown_preview_error(self, request_id: int, message: str) -> None:
        if request_id != self.unknown_preview_request_id:
            return
        self._clear_unknown_preview(f"Preview failed: {message}")

    def _cleanup_unknown_preview_refs(self) -> None:
        self.unknown_preview_worker = None
        self.unknown_preview_thread = None
        if self.pending_unknown_preview_request is None:
            return
        request_id, entry = self.pending_unknown_preview_request
        self.pending_unknown_preview_request = None
        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        self.unknown_preview_request_id = request_id
        self._start_unknown_preview_worker(request_id, texconv_path, entry)

    def _apply_unknown_preview_result(self, result: ArchivePreviewResult) -> None:
        title = result.title or "Selected Preview"
        metadata_summary = result.metadata_summary or "Preview ready."
        detail_text = result.detail_text or metadata_summary
        self.unknown_preview_title_label.setText(title)
        self.unknown_preview_meta_label.setText(metadata_summary)
        self.unknown_preview_warning_label.setText(result.warning_text)
        self.unknown_preview_warning_label.setVisible(bool(result.warning_text))
        self.unknown_preview_info_edit.setPlainText(detail_text)
        if result.preferred_view == "image" and (result.preview_image is not None or result.preview_image_path):
            if result.preview_image is not None:
                self.unknown_preview_label.set_preview_image(result.preview_image, title or "Preview image")
            else:
                self.unknown_preview_label.set_preview_image_path(result.preview_image_path, title or "Preview image")
            self.unknown_preview_stack.setCurrentWidget(self.unknown_preview_scroll)
            self._set_unknown_preview_image_controls_enabled(True)
            self._apply_unknown_preview_zoom()
            return
        if result.preferred_view == "text" and result.preview_text:
            self.unknown_preview_info_edit.setPlainText(result.preview_text)
        self.unknown_preview_label.clear_preview("No image preview available.")
        self.unknown_preview_stack.setCurrentWidget(self.unknown_preview_info_edit)
        self._set_unknown_preview_image_controls_enabled(False)

    def _populate_heatmap_rows(self, rows: object) -> None:
        self.heatmap_tree.setUpdatesEnabled(False)
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
        self.heatmap_tree.setUpdatesEnabled(True)

    def _populate_mip_rows(self, rows: object) -> None:
        self.mip_tree.setUpdatesEnabled(False)
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
            tooltip_lines = [*row.warnings]
            if row.planner_profile or row.planner_path_kind:
                tooltip_lines.extend(
                    [
                        "",
                        f"Planner profile: {row.planner_profile or 'unavailable'}",
                        f"Planner path: {row.planner_path_kind or 'unavailable'}",
                        f"Planner path detail: {describe_processing_path_kind(row.planner_path_kind) if row.planner_path_kind else 'unavailable'}",
                        f"Planner backend mode: {row.planner_backend_mode or 'unavailable'}",
                        f"Planner alpha policy: {row.planner_alpha_policy or 'unavailable'}",
                    ]
                )
                if row.planner_preserve_reason:
                    tooltip_lines.append(f"Planner preserve reason: {row.planner_preserve_reason}")
            item.setToolTip(4, "\n".join(line for line in tooltip_lines if line))
            self.mip_tree.addTopLevelItem(item)
        if self.mip_tree.topLevelItemCount() > 0:
            first = self.mip_tree.topLevelItem(0)
            if first is not None:
                self.mip_tree.setCurrentItem(first)
        self.mip_tree.setUpdatesEnabled(True)

    def _populate_normal_rows(self, rows: object) -> None:
        self.normal_tree.setUpdatesEnabled(False)
        self.normal_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, NormalValidationRow):
                continue
            item = QTreeWidgetItem([row.path, row.root_label, row.texconv_format, row.size_text, "; ".join(row.issues[:2])])
            item.setData(0, Qt.UserRole, row)
            tooltip_lines = [*row.issues]
            if row.planner_profile or row.planner_path_kind:
                tooltip_lines.extend(
                    [
                        "",
                        f"Planner profile: {row.planner_profile or 'unavailable'}",
                        f"Planner path: {row.planner_path_kind or 'unavailable'}",
                        f"Planner path detail: {describe_processing_path_kind(row.planner_path_kind) if row.planner_path_kind else 'unavailable'}",
                        f"Planner backend mode: {row.planner_backend_mode or 'unavailable'}",
                        f"Planner alpha policy: {row.planner_alpha_policy or 'unavailable'}",
                    ]
                )
                if row.planner_preserve_reason:
                    tooltip_lines.append(f"Planner preserve reason: {row.planner_preserve_reason}")
            item.setToolTip(4, "\n".join(line for line in tooltip_lines if line))
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
        self.normal_tree.setUpdatesEnabled(True)

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
        budget_rows = self.research_payload.get("budget_rows", [])
        budget_profile = self.research_payload.get("budget_profile")
        mip_count = len(mip_rows) if isinstance(mip_rows, list) else 0
        normal_count = len(normal_rows) if isinstance(normal_rows, list) else 0
        budget_count = len(budget_rows) if isinstance(budget_rows, list) else 0
        planner_path_counts: Dict[str, int] = {}
        planner_profile_counts: Dict[str, int] = {}
        if isinstance(mip_rows, list):
            for row in mip_rows:
                if isinstance(row, MipAnalysisRow):
                    if row.planner_path_kind:
                        planner_path_counts[row.planner_path_kind] = planner_path_counts.get(row.planner_path_kind, 0) + 1
                    if row.planner_profile:
                        planner_profile_counts[row.planner_profile] = planner_profile_counts.get(row.planner_profile, 0) + 1
        normal_roots: Dict[str, int] = {}
        if isinstance(normal_rows, list):
            for row in normal_rows:
                if isinstance(row, NormalValidationRow):
                    normal_roots[row.root_label] = normal_roots.get(row.root_label, 0) + 1
        normal_root_summary = ", ".join(
            f"{label}: {count:,}" for label, count in sorted(normal_roots.items())
        ) if normal_roots else "none"
        planner_path_summary = ", ".join(
            f"{label}: {count:,}" for label, count in sorted(planner_path_counts.items())
        ) if planner_path_counts else "unavailable"
        planner_profile_summary = ", ".join(
            f"{label}: {count:,}" for label, count in sorted(planner_profile_counts.items())
        ) if planner_profile_counts else "unavailable"
        analysis_context = (
            "Texture Analysis context:\n"
            f"- Original DDS root: {original_root if original_root_text else '(not set)'}"
            + (" (available)" if original_exists else " (missing or not set)")
            + "\n"
            f"- Output root: {output_root if output_root_text else '(not set)'}"
            + (" (available)" if output_exists else " (missing or not set)")
            + "\n"
            f"- Mip Analysis rows: {mip_count:,} matching DDS file pair(s). Requires the same relative DDS path to exist in both roots. Uses texconv previews when available for alpha, brightness, range, and channel-drift checks.\n"
            f"- Planner path summary: {planner_path_summary}.\n"
            f"- Planner profile summary: {planner_profile_summary}.\n"
            f"- Bulk Normal Validator rows: {normal_count:,} normal-like DDS file(s). Current roots represented: {normal_root_summary}.\n"
            f"- Budget rows: {budget_count:,} matching DDS pair(s)."
            + (
                f" Current heuristic budget profile: {budget_profile.profile_label}."
                if isinstance(budget_profile, TextureBudgetProfileSummary)
                else ""
            )
        )
        self.analysis_context_label.setText(analysis_context)
        if hasattr(self, "analysis_context_help_button"):
            _set_help_button_text(self.analysis_context_help_button, analysis_context)

    def _handle_research_subtab_changed(self, index: int) -> None:
        del index
        self._update_research_side_panel()

    def _handle_archive_insights_subtab_changed(self, _index: int) -> None:
        self._update_research_side_panel()

    def _update_research_side_panel(self) -> None:
        widget = self.tab_widget.currentWidget()
        if widget is self.texture_tab:
            self.right_panel_stack.setVisible(True)
            self.right_panel_stack.setCurrentWidget(self.analysis_detail_group)
            return
        current_archive_tab = self.archive_insights_tabs.currentWidget()
        if current_archive_tab is getattr(self, "classification_review_tab", None):
            self.right_panel_stack.setVisible(False)
            return
        if not self.defer_archive_picker_refresh:
            self.refresh_archive_picker_if_pending()
        self.right_panel_stack.setVisible(True)
        self.right_panel_stack.setCurrentWidget(self.archive_picker_group)

    def _ensure_archive_picker_ready(self) -> None:
        if self.defer_archive_picker_refresh:
            return
        self.refresh_archive_picker_if_pending()

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
            family_members_by_path=self.research_payload.get("mip_detail_family_members_by_path")
            if isinstance(self.research_payload.get("mip_detail_family_members_by_path"), dict)
            else None,
        )
        self.analysis_detail_edit.setPlainText(detail_text)

    def _show_normal_row_details(self, row: NormalValidationRow) -> None:
        self.analysis_detail_label.setText("Bulk Normal Validator details")
        texconv_path = Path(self.get_texconv_path()).expanduser() if self.get_texconv_path().strip() else None
        root_path = Path(row.root_path).expanduser() if row.root_path else Path(".")
        detail_text = build_normal_validation_detail(root_path, row, texconv_path=texconv_path)
        self.analysis_detail_edit.setPlainText(detail_text)

    def _show_budget_details(self, row: object) -> None:
        if isinstance(row, TextureBudgetRow):
            self.analysis_detail_label.setText("Budget file details")
            detail_lines = [
                f"Path: {row.relative_path}",
                f"Group key: {row.group_key}",
                f"System area: {row.system_area}",
                f"Folder bucket: {row.folder_bucket}",
                f"Texture type: {row.texture_type}",
                f"Planner profile: {row.planner_profile or 'unavailable'}",
                f"Planner path kind: {row.planner_path_kind or 'unavailable'}",
                f"Planner alpha policy: {row.planner_alpha_policy or 'unavailable'}",
                f"Original bytes: {row.original_bytes:,}",
                f"Rebuilt bytes: {row.rebuilt_bytes:,}",
                f"Byte delta: {row.byte_delta:+,}",
                f"Byte ratio: {row.byte_ratio:.2f}x",
                f"Original size: {row.original_width}x{row.original_height}",
                f"Rebuilt size: {row.rebuilt_width}x{row.rebuilt_height}",
                f"Pixel ratio: {row.pixel_ratio:.2f}x",
                f"Mips: {row.original_mips} -> {row.rebuilt_mips} (delta {row.mip_delta:+d})",
                f"Format: {row.original_format} -> {row.rebuilt_format}",
                f"UI rect evidence: {row.ui_constraint_summary or 'none'}",
                f"Risk: {row.risk_score} ({row.risk_band})",
                "",
                "Signals:",
                *[f"- {signal}" for signal in row.risk_signals],
            ]
            self.analysis_detail_edit.setPlainText("\n".join(detail_lines))
            return
        if isinstance(row, TextureBudgetClassSummary):
            self.analysis_detail_label.setText("Budget class summary")
            self.analysis_detail_edit.setPlainText(
                "\n".join(
                    [
                        f"Texture type: {row.texture_type}",
                        f"Affected textures: {row.affected_count:,}",
                        f"Total byte delta: {row.total_byte_delta:+,}",
                        f"Average risk: {row.average_risk:.1f} ({row.risk_band})",
                        "",
                        "Sample paths:",
                        *[f"- {path}" for path in row.sample_paths],
                    ]
                )
            )
            return
        if isinstance(row, TextureBudgetGroupSummary):
            self.analysis_detail_label.setText("Terrain-like group summary")
            self.analysis_detail_edit.setPlainText(
                "\n".join(
                    [
                        f"Group key: {row.group_key}",
                        f"System area: {row.system_area}",
                        f"Textures: {row.texture_count:,}",
                        f"Original bytes: {row.total_original_bytes:,}",
                        f"Rebuilt bytes: {row.total_rebuilt_bytes:,}",
                        f"Byte delta: {row.total_byte_delta:+,}",
                        f"Average ratio: {row.average_byte_ratio:.2f}x",
                        f"Max ratio: {row.max_byte_ratio:.2f}x",
                        f"Average dimensions: {row.average_width:.1f} x {row.average_height:.1f}",
                        f"2048+ members: {row.large_2048_count}",
                        f"4096+ members: {row.large_4096_count}",
                        f"Risk: {row.risk_score} ({row.risk_band})",
                        "",
                        "Signals:",
                        *[f"- {signal}" for signal in row.signals],
                    ]
                )
            )
            return
        if isinstance(row, TextureBudgetProfileSummary):
            self.analysis_detail_label.setText("Budget profile summary")
            self.analysis_detail_edit.setPlainText(
                "\n".join(
                    [
                        f"Profile: {row.profile_label}",
                        f"Original bytes: {row.total_original_bytes:,}",
                        f"Rebuilt bytes: {row.total_rebuilt_bytes:,}",
                        f"Byte delta: {row.total_byte_delta:+,}",
                        f"Total ratio: {row.total_byte_ratio:.2f}x",
                        f"Changed textures: {row.changed_texture_count:,}",
                        f"Upscaled textures: {row.upscaled_texture_count:,}",
                        f"High-risk fraction: {row.high_risk_texture_fraction * 100.0:.1f}%",
                        f"Highest terrain-like group risk: {row.highest_group_risk}",
                        "",
                        "Reasons:",
                        *[f"- {reason}" for reason in row.reasons],
                    ]
                )
            )
            return

    def _populate_reference_rows(self, rows: object) -> None:
        self.reference_tree.clear()
        self.reference_review_text_button.setEnabled(False)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, MaterialTextureReferenceRow):
                continue
            item = QTreeWidgetItem(
                [
                    row.source_path,
                    row.related_path,
                    row.get_rect_raw or "-",
                    row.constraint_kind or row.relation_kind,
                    f"{row.match_count:,}",
                    row.source_package_label or row.related_package_label,
                ]
            )
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, row.snippet)
            item.setToolTip(1, row.related_package_label)
            item.setToolTip(2, row.get_rect_raw or "")
            item.setToolTip(3, row.warning_text or row.constraint_kind or row.relation_kind)
            self.reference_tree.addTopLevelItem(item)
        if self.reference_tree.topLevelItemCount() > 0:
            first = self.reference_tree.topLevelItem(0)
            if first is not None:
                self.reference_tree.setCurrentItem(first)

    def _populate_ui_constraint_rows(self, rows: object) -> None:
        self.ui_constraint_tree.clear()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, MaterialTextureReferenceRow):
                continue
            dds_size = f"{row.texture_width}x{row.texture_height}" if row.texture_width > 0 and row.texture_height > 0 else "-"
            item = QTreeWidgetItem(
                [
                    row.related_path,
                    row.source_path,
                    dds_size,
                    row.get_rect_raw or "-",
                    row.constraint_kind or "Explicit UI rect found",
                    row.related_package_label or row.source_package_label,
                ]
            )
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, row.warning_text or row.related_path)
            item.setToolTip(1, row.source_path)
            item.setToolTip(4, row.warning_text or row.constraint_kind)
            self.ui_constraint_tree.addTopLevelItem(item)

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
        self.reference_review_text_button.setEnabled(False)
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, MaterialTextureReferenceRow):
            return
        self.reference_review_text_button.setEnabled(bool(row.source_path and row.related_path))
        if self._focus_archive_picker_path(row.related_path):
            return
        self._focus_archive_picker_path(row.source_path)

    def _handle_budget_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            return
        row = current.data(0, Qt.UserRole)
        self._show_budget_details(row)

    def review_selected_reference_in_text_search(self) -> None:
        item = self.reference_tree.currentItem()
        if item is None:
            self.status_message_requested.emit("Select a reference result first.", True)
            return
        row = item.data(0, Qt.UserRole)
        if not isinstance(row, MaterialTextureReferenceRow):
            self.status_message_requested.emit("Select a reference result first.", True)
            return
        source_path = row.source_path.strip().replace("\\", "/")
        highlight_query = PurePosixPath(row.related_path.strip().replace("\\", "/")).name
        if not source_path or not highlight_query:
            self.status_message_requested.emit(
                "The selected reference row does not include enough information for Text Search review.",
                True,
            )
            return
        self.review_reference_in_text_search_requested.emit(source_path, highlight_query)

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
        budget_rows = self.research_payload.get("budget_rows", [])
        budget_class_rows = self.research_payload.get("budget_class_rows", [])
        budget_group_rows = self.research_payload.get("budget_group_rows", [])
        budget_profile = self.research_payload.get("budget_profile")
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
            final_path = export_texture_analysis_report(
                report_path,
                mip_rows,
                normal_rows,
                budget_rows=budget_rows if isinstance(budget_rows, list) else (),
                budget_class_rows=budget_class_rows if isinstance(budget_class_rows, list) else (),
                budget_group_rows=budget_group_rows if isinstance(budget_group_rows, list) else (),
                budget_profile=budget_profile if isinstance(budget_profile, TextureBudgetProfileSummary) else None,
            )
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
