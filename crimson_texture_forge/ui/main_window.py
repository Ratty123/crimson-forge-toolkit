from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from crimson_texture_forge.constants import *
from crimson_texture_forge.models import *
from crimson_texture_forge.core.archive import *
from crimson_texture_forge.core.chainner import *
from crimson_texture_forge.core.pipeline import *


def run_gui() -> int:
    try:
        from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, QObject, Signal, Slot
        from PySide6.QtGui import (
            QActionGroup,
            QDesktopServices,
            QFont,
            QIcon,
            QPixmap,
        )
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QComboBox,
            QFileDialog,
            QFrame,
            QGridLayout,
            QGroupBox,
            QHeaderView,
            QHBoxLayout,
            QInputDialog,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QSplitter,
            QStackedWidget,
            QSpinBox,
            QTabWidget,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError:
        print("PySide6 is required to run the GUI. Install it with: pip install PySide6", file=sys.stderr)
        return 1

    from crimson_texture_forge.ui.themes import UI_THEME_SCHEMES, build_app_palette, build_app_stylesheet
    from crimson_texture_forge.ui.widgets import (
        AboutDialog,
        CollapsibleSection,
        LogHighlighter,
        PreviewLabel,
        PreviewScrollArea,
        QuickStartDialog,
    )
    from crimson_texture_forge.ui.text_search_tab import TextSearchTab

    def resolve_settings_file_path() -> Path:
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parents[2]
        return base_dir / f"{APP_NAME}.cfg"

    settings_file_path = resolve_settings_file_path()

    def create_settings() -> QSettings:
        settings_file_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_settings_path = settings_file_path.with_name("DDSRebuildApp.cfg")
        if not settings_file_path.exists() and legacy_settings_path.exists():
            try:
                shutil.copy2(legacy_settings_path, settings_file_path)
            except OSError:
                pass
        settings = QSettings(str(settings_file_path), QSettings.Format.IniFormat)
        settings.setFallbacksEnabled(False)
        return settings

    def resolve_app_icon_path() -> Optional[Path]:
        search_roots: List[Path] = []
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                search_roots.append(Path(str(meipass)))
            search_roots.append(Path(sys.executable).resolve().parent)
        else:
            search_roots.append(Path(__file__).resolve().parents[2])

        relative_candidates = (
            Path("assets") / "crimson_texture_forge.ico",
            Path("assets") / "crimson_texture_forge.png",
            Path("crimson_texture_forge.ico"),
            Path("crimson_texture_forge.png"),
        )
        for root in search_roots:
            for relative_path in relative_candidates:
                candidate = root / relative_path
                if candidate.exists():
                    return candidate
        return None

    def apply_windows_app_user_model_id() -> None:
        if os.name != "nt":
            return
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ORGANIZATION}.{APP_NAME}")
        except Exception:
            pass

    def apply_app_theme(app: QApplication, theme_key: str) -> str:
        resolved_theme = theme_key if theme_key in UI_THEME_SCHEMES else DEFAULT_UI_THEME
        app.setPalette(build_app_palette(resolved_theme))
        app.setStyleSheet(build_app_stylesheet(resolved_theme))
        return resolved_theme

    class ScanWorker(QObject):
        log_message = Signal(str)
        result_ready = Signal(int)
        error = Signal(str)
        finished = Signal()

        def __init__(self, config: AppConfig):
            super().__init__()
            self.config = config

        @Slot()
        def run(self) -> None:
            try:
                self.log_message.emit("Scanning DDS files...")
                result = scan_dds_files(self.config)
                self.result_ready.emit(result.total_files)
                self.log_message.emit(f"Scan complete. Found {result.total_files} DDS files.")
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class ArchiveScanWorker(QObject):
        log_message = Signal(str)
        progress_changed = Signal(int, int, str)
        completed = Signal(object)
        error = Signal(str)
        finished = Signal()

        def __init__(
            self,
            package_root: Path,
            cache_root: Path,
            *,
            force_refresh: bool = False,
            filter_text: str = "",
            extension_filter: str = "*",
            package_filter_text: str = "",
            structure_filter: str = "",
            role_filter: str = "all",
            min_size_kb: int = 0,
            previewable_only: bool = False,
        ):
            super().__init__()
            self.package_root = package_root
            self.cache_root = cache_root
            self.force_refresh = force_refresh
            self.filter_text = filter_text
            self.extension_filter = extension_filter
            self.package_filter_text = package_filter_text
            self.structure_filter = structure_filter
            self.role_filter = role_filter
            self.min_size_kb = min_size_kb
            self.previewable_only = previewable_only

        @Slot()
        def run(self) -> None:
            try:
                if self.force_refresh:
                    self.log_message.emit(f"Refreshing archive packages under {self.package_root}")
                else:
                    self.log_message.emit(f"Loading archive packages under {self.package_root}")
                entries, source, cache_path = scan_archive_entries_cached(
                    self.package_root,
                    self.cache_root,
                    force_refresh=self.force_refresh,
                    on_log=self.log_message.emit,
                    on_progress=self.progress_changed.emit,
                )
                self.log_message.emit("Preparing archive browser state from loaded entries...")
                browser_state = prepare_archive_browser_state(
                    entries,
                    filter_text=self.filter_text,
                    extension_filter=self.extension_filter,
                    package_filter_text=self.package_filter_text,
                    structure_filter=self.structure_filter,
                    role_filter=self.role_filter,
                    min_size_kb=self.min_size_kb,
                    previewable_only=self.previewable_only,
                    build_structure_children=True,
                    on_progress=self.progress_changed.emit,
                )
                self.completed.emit(
                    {
                        "entries": entries,
                        "source": source,
                        "cache_path": str(cache_path) if cache_path is not None else "",
                        "browser_state": browser_state,
                    }
                )
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class BuildWorker(QObject):
        log_message = Signal(str)
        phase_changed = Signal(str, str, bool)
        phase_progress_changed = Signal(int, int, str)
        total_found = Signal(int)
        current_file = Signal(str)
        progress = Signal(int, int, int, int, int)
        completed = Signal(object)
        cancelled = Signal(str)
        error = Signal(str)
        finished = Signal()

        def __init__(self, config: AppConfig):
            super().__init__()
            self.config = config
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()

        @Slot()
        def run(self) -> None:
            try:
                summary = rebuild_dds_files(
                    self.config,
                    on_log=self.log_message.emit,
                    on_total=self.total_found.emit,
                    on_current_file=self.current_file.emit,
                    on_progress=self.progress.emit,
                    on_phase=self.phase_changed.emit,
                    on_phase_progress=self.phase_progress_changed.emit,
                    stop_event=self.stop_event,
                )
                if summary.cancelled:
                    self.cancelled.emit("Processing stopped by user.")
                else:
                    self.completed.emit(summary)
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class DdsToPngWorker(QObject):
        log_message = Signal(str)
        phase_changed = Signal(str, str, bool)
        phase_progress_changed = Signal(int, int, str)
        total_found = Signal(int)
        current_file = Signal(str)
        progress = Signal(int, int, int, int, int)
        completed = Signal(object)
        cancelled = Signal(str)
        error = Signal(str)
        finished = Signal()

        def __init__(self, config: AppConfig):
            super().__init__()
            self.config = config
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()

        @Slot()
        def run(self) -> None:
            try:
                summary = convert_dds_to_pngs(
                    self.config,
                    on_log=self.log_message.emit,
                    on_total=self.total_found.emit,
                    on_current_file=self.current_file.emit,
                    on_progress=self.progress.emit,
                    on_phase=self.phase_changed.emit,
                    on_phase_progress=self.phase_progress_changed.emit,
                    stop_event=self.stop_event,
                )
                if summary.cancelled:
                    self.cancelled.emit("DDS to PNG conversion stopped by user.")
                else:
                    self.completed.emit(summary)
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class UtilityWorker(QObject):
        log_message = Signal(str)
        completed = Signal(object)
        error = Signal(str)
        finished = Signal()

        def __init__(self, task: Callable[[Callable[[str], None]], object]):
            super().__init__()
            self.task = task

        @Slot()
        def run(self) -> None:
            try:
                result = self.task(self.log_message.emit)
                self.completed.emit(result)
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class ComparePreviewWorker(QObject):
        completed = Signal(int, object)
        error = Signal(int, str)
        finished = Signal()

        def __init__(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            original_path: Optional[Path],
            output_path: Optional[Path],
        ):
            super().__init__()
            self.request_id = request_id
            self.texconv_path = texconv_path
            self.original_path = original_path
            self.output_path = output_path

        @Slot()
        def run(self) -> None:
            try:
                payload = {
                    "original": build_compare_preview_pane_result(
                        self.texconv_path,
                        self.original_path,
                        "Original DDS not found.",
                    ),
                    "output": build_compare_preview_pane_result(
                        self.texconv_path,
                        self.output_path,
                        "Output DDS not found.",
                    ),
                }
                self.completed.emit(self.request_id, payload)
            except Exception as exc:
                self.error.emit(self.request_id, str(exc))
            finally:
                self.finished.emit()

    class ArchivePreviewWorker(QObject):
        completed = Signal(int, object)
        error = Signal(int, str)
        finished = Signal()

        def __init__(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            entry: Optional[ArchiveEntry],
            loose_search_roots: Sequence[Path],
        ):
            super().__init__()
            self.request_id = request_id
            self.texconv_path = texconv_path
            self.entry = entry
            self.loose_search_roots = list(loose_search_roots)

        @Slot()
        def run(self) -> None:
            try:
                payload = build_archive_preview_result(
                    self.texconv_path,
                    self.entry,
                    self.loose_search_roots,
                )
                self.completed.emit(self.request_id, payload)
            except Exception as exc:
                self.error.emit(self.request_id, str(exc))
            finally:
                self.finished.emit()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle(APP_TITLE)
            self.settings = create_settings()
            self.settings_file_path = settings_file_path
            self.archive_cache_root = self.settings_file_path.parent / ARCHIVE_SCAN_CACHE_DIRNAME
            self._settings_ready = False
            self.current_theme_key = str(self.settings.value("appearance/theme", DEFAULT_UI_THEME))
            self.show_quick_start_on_launch = not self.settings.contains("ui/quick_start_shown")
            self.resize(1360, 840)
            self.setMinimumSize(1180, 720)
            self.worker_thread: Optional[QThread] = None
            self.scan_worker: Optional[ScanWorker] = None
            self.archive_scan_worker: Optional[ArchiveScanWorker] = None
            self.build_worker: Optional[BuildWorker] = None
            self.dds_to_png_worker: Optional[DdsToPngWorker] = None
            self.utility_worker: Optional[UtilityWorker] = None
            self._utility_completion_handler: Optional[Callable[[object], None]] = None
            self.compare_relative_paths: List[Path] = []
            self.compare_preview_thread: Optional[QThread] = None
            self.compare_preview_worker: Optional[ComparePreviewWorker] = None
            self.compare_preview_request_id = 0
            self.pending_compare_preview_request: Optional[Tuple[int, Path]] = None
            self.compare_syncing_scrollbars = False
            self.archive_preview_thread: Optional[QThread] = None
            self.archive_preview_worker: Optional[ArchivePreviewWorker] = None
            self.archive_preview_request_id = 0
            self.pending_archive_preview_request: Optional[Tuple[int, Optional[ArchiveEntry]]] = None
            self.current_archive_preview_result: Optional[ArchivePreviewResult] = None
            self.archive_preview_showing_loose = False
            self.archive_entries: List[ArchiveEntry] = []
            self.archive_filtered_entries: List[ArchiveEntry] = []
            self.archive_filtered_dds_count = 0
            self.archive_filters_dirty = False
            self.archive_tree_child_folders: Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]] = {}
            self.archive_tree_direct_files: Dict[Tuple[str, ...], List[int]] = {}
            self.archive_tree_folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = {}
            self.archive_tree_items_by_folder_key: Dict[Tuple[str, ...], QTreeWidgetItem] = {}
            self.archive_structure_filter_pending_value = ""
            self.archive_structure_filter_children: Dict[str, List[Tuple[str, int]]] = {}
            self.archive_structure_filter_combos: List[QComboBox] = []
            self.rebuilding_archive_structure_filters = False
            self.original_compare_zoom_factor = 1.0
            self.original_compare_fit_to_view = True
            self.output_compare_zoom_factor = 1.0
            self.output_compare_fit_to_view = True
            self.archive_preview_zoom_factor = 1.0
            self.archive_preview_fit_to_view = True

            icon_path = resolve_app_icon_path()
            if icon_path is not None:
                self.setWindowIcon(QIcon(str(icon_path)))

            menu_bar = self.menuBar()
            self.profile_menu = menu_bar.addMenu("Profile")
            self.export_profile_action = self.profile_menu.addAction("Export Profile...")
            self.import_profile_action = self.profile_menu.addAction("Import Profile...")
            self.tools_menu = menu_bar.addMenu("Tools")
            self.validate_chainner_menu_action = self.tools_menu.addAction("Validate chaiNNer Chain")
            self.export_diagnostics_action = self.tools_menu.addAction("Export Diagnostics...")
            self.help_menu = menu_bar.addMenu("Help")
            self.quick_start_menu_action = self.help_menu.addAction("Quick Start")
            self.about_menu_action = self.help_menu.addAction("About")
            self.theme_menu = menu_bar.addMenu("Theme")
            self.theme_action_group = QActionGroup(self)
            self.theme_action_group.setExclusive(True)
            self.theme_actions_by_key: Dict[str, object] = {}
            for theme_key, theme in UI_THEME_SCHEMES.items():
                action = self.theme_menu.addAction(theme["label"])
                action.setCheckable(True)
                action.setData(theme_key)
                action.triggered.connect(lambda _checked=False, key=theme_key: self._handle_theme_changed(key))
                self.theme_action_group.addAction(action)
                self.theme_actions_by_key[theme_key] = action

            central = QWidget()
            central.setObjectName("AppRoot")
            root_layout = QVBoxLayout(central)
            root_layout.setContentsMargins(12, 0, 12, 12)
            root_layout.setSpacing(8)

            self.main_tabs = QTabWidget()
            root_layout.addWidget(self.main_tabs, stretch=1)

            self.workflow_tab = QWidget()
            workflow_layout = QVBoxLayout(self.workflow_tab)
            workflow_layout.setContentsMargins(0, 0, 0, 0)
            workflow_layout.setSpacing(10)
            self.main_tabs.addTab(self.workflow_tab, "Workflow")

            self.workflow_splitter = QSplitter(Qt.Horizontal)
            self.workflow_splitter.setChildrenCollapsible(False)
            workflow_layout.addWidget(self.workflow_splitter, stretch=1)

            self.left_panel = QWidget()
            self.left_panel.setMinimumWidth(420)
            left_layout = QVBoxLayout(self.left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(10)

            self.left_scroll_area = QScrollArea()
            self.left_scroll_area.setWidgetResizable(True)
            self.left_scroll_area.setFrameShape(QFrame.NoFrame)
            self.left_scroll_area.setMinimumWidth(420)
            self.left_scroll_area.setWidget(self.left_panel)

            self.right_panel = QWidget()
            self.right_panel.setMinimumWidth(420)
            right_layout = QVBoxLayout(self.right_panel)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(10)

            self.workflow_splitter.addWidget(self.left_scroll_area)
            self.workflow_splitter.addWidget(self.right_panel)
            self.workflow_splitter.setStretchFactor(0, 1)
            self.workflow_splitter.setStretchFactor(1, 2)
            self.workflow_splitter.setSizes([540, 760])

            self.paths_section = CollapsibleSection("Paths", expanded=False)
            paths_group = QWidget()
            paths_layout = QGridLayout(paths_group)
            paths_layout.setContentsMargins(0, 0, 0, 0)
            paths_layout.setHorizontalSpacing(10)
            paths_layout.setVerticalSpacing(10)
            paths_layout.setColumnMinimumWidth(0, 136)
            paths_layout.setColumnStretch(1, 1)

            self.original_dds_edit = QLineEdit()
            self.png_root_edit = QLineEdit()
            self.dds_staging_root_edit = QLineEdit()
            self.output_root_edit = QLineEdit()
            self.texconv_path_edit = QLineEdit()

            self._add_path_row(paths_layout, 0, "Original DDS root", self.original_dds_edit, self._browse_original_dds_root)
            self._add_path_row(paths_layout, 1, "PNG root", self.png_root_edit, self._browse_png_root)
            self.dds_staging_browse_button = self._add_path_row(
                paths_layout,
                2,
                "Staging PNG root",
                self.dds_staging_root_edit,
                self._browse_dds_staging_root,
            )
            self._add_path_row(paths_layout, 3, "Output root", self.output_root_edit, self._browse_output_root)
            self._add_path_row(paths_layout, 4, "texconv.exe path", self.texconv_path_edit, self._browse_texconv_path)

            self.paths_section.body_layout.addWidget(paths_group)

            self.setup_section = CollapsibleSection("Setup", expanded=False)
            setup_group = QWidget()
            setup_layout = QVBoxLayout(setup_group)
            setup_layout.setContentsMargins(0, 0, 0, 0)
            setup_layout.setSpacing(8)

            setup_buttons_row_1 = QHBoxLayout()
            setup_buttons_row_1.setSpacing(8)
            self.init_workspace_button = QPushButton("Init Workspace")
            self.create_folders_button = QPushButton("Create Folders")
            setup_buttons_row_1.addWidget(self.init_workspace_button)
            setup_buttons_row_1.addWidget(self.create_folders_button)
            setup_layout.addLayout(setup_buttons_row_1)

            setup_buttons_row_2 = QHBoxLayout()
            setup_buttons_row_2.setSpacing(8)
            self.download_chainner_button = QPushButton("Download chaiNNer")
            self.download_texconv_button = QPushButton("Download texconv")
            setup_buttons_row_2.addWidget(self.download_chainner_button)
            setup_buttons_row_2.addWidget(self.download_texconv_button)
            setup_layout.addLayout(setup_buttons_row_2)

            self.setup_section.body_layout.addWidget(setup_group)
            left_layout.addWidget(self.setup_section)
            left_layout.addWidget(self.paths_section)

            self.settings_section = CollapsibleSection("Settings", expanded=False)
            settings_group = QWidget()
            settings_layout = QVBoxLayout(settings_group)
            settings_layout.setContentsMargins(0, 0, 0, 0)
            settings_layout.setSpacing(8)

            self.dry_run_checkbox = QCheckBox("Dry run")
            self.enable_incremental_resume_checkbox = QCheckBox("Enable incremental resume")
            self.csv_log_enabled_checkbox = QCheckBox("Write CSV log")
            self.unique_basename_checkbox = QCheckBox("Allow unique basename fallback")
            self.overwrite_existing_checkbox = QCheckBox("Overwrite existing DDS")

            settings_layout.addWidget(self.dry_run_checkbox)
            settings_layout.addWidget(self.enable_incremental_resume_checkbox)
            settings_layout.addWidget(self.csv_log_enabled_checkbox)

            csv_path_row = QHBoxLayout()
            csv_path_row.setSpacing(8)
            self.csv_log_path_edit = QLineEdit()
            self.csv_log_browse_button = QPushButton("Browse")
            self.csv_log_browse_button.clicked.connect(self._browse_csv_log_path)
            csv_path_row.addWidget(self.csv_log_path_edit, stretch=1)
            csv_path_row.addWidget(self.csv_log_browse_button)
            settings_layout.addLayout(csv_path_row)

            settings_layout.addWidget(self.unique_basename_checkbox)
            settings_layout.addWidget(self.overwrite_existing_checkbox)

            self.settings_section.body_layout.addWidget(settings_group)
            left_layout.addWidget(self.settings_section)

            self.dds_output_section = CollapsibleSection("DDS Output", expanded=False)
            dds_output_group = QWidget()
            dds_output_layout = QGridLayout(dds_output_group)
            dds_output_layout.setContentsMargins(0, 0, 0, 0)
            dds_output_layout.setHorizontalSpacing(10)
            dds_output_layout.setVerticalSpacing(8)
            dds_output_layout.setColumnMinimumWidth(0, 132)
            dds_output_layout.setColumnStretch(1, 1)

            self.enable_dds_staging_checkbox = QCheckBox("Convert DDS to PNG before processing")
            dds_output_mode_hint = QLabel(
                "Uses texconv to create PNG files first. If chaiNNer is disabled, Start will stop after PNG conversion."
            )
            dds_output_mode_hint.setObjectName("HintLabel")
            dds_output_mode_hint.setWordWrap(True)

            self.dds_format_mode_combo = QComboBox()
            self._add_combo_choice(self.dds_format_mode_combo, "Match original DDS format", DDS_FORMAT_MODE_MATCH_ORIGINAL)
            self._add_combo_choice(self.dds_format_mode_combo, "Custom format", DDS_FORMAT_MODE_CUSTOM)

            self.dds_custom_format_label = QLabel("Custom format")
            self.dds_custom_format_combo = QComboBox()
            for format_name in SUPPORTED_TEXCONV_FORMAT_CHOICES:
                self._add_combo_choice(self.dds_custom_format_combo, format_name, format_name)

            self.dds_size_mode_combo = QComboBox()
            self._add_combo_choice(self.dds_size_mode_combo, "Use PNG size (upscaled)", DDS_SIZE_MODE_PNG)
            self._add_combo_choice(self.dds_size_mode_combo, "Use original DDS size", DDS_SIZE_MODE_ORIGINAL)
            self._add_combo_choice(self.dds_size_mode_combo, "Custom size", DDS_SIZE_MODE_CUSTOM)

            self.dds_custom_size_label = QLabel("Custom size")
            self.dds_custom_width_spin = QSpinBox()
            self.dds_custom_width_spin.setRange(1, 32768)
            self.dds_custom_width_spin.setSingleStep(64)
            self.dds_custom_height_spin = QSpinBox()
            self.dds_custom_height_spin.setRange(1, 32768)
            self.dds_custom_height_spin.setSingleStep(64)

            self.dds_mip_mode_combo = QComboBox()
            self._add_combo_choice(self.dds_mip_mode_combo, "Match original DDS mip count", DDS_MIP_MODE_MATCH_ORIGINAL)
            self._add_combo_choice(self.dds_mip_mode_combo, "Full mip chain for output size", DDS_MIP_MODE_FULL_CHAIN)
            self._add_combo_choice(self.dds_mip_mode_combo, "Single mip only", DDS_MIP_MODE_SINGLE)
            self._add_combo_choice(self.dds_mip_mode_combo, "Custom mip count", DDS_MIP_MODE_CUSTOM)

            self.dds_custom_mip_label = QLabel("Custom mip count")
            self.dds_custom_mip_spin = QSpinBox()
            self.dds_custom_mip_spin.setRange(1, 16)

            dds_output_hint = QLabel(
                "Default behavior keeps the original DDS format and mip policy, while size follows the PNG unless you change it."
            )
            dds_output_hint.setObjectName("HintLabel")
            dds_output_hint.setWordWrap(True)

            self.dds_custom_size_widget = QWidget()
            custom_size_row = QHBoxLayout(self.dds_custom_size_widget)
            custom_size_row.setContentsMargins(0, 0, 0, 0)
            custom_size_row.setSpacing(8)
            custom_size_row.addWidget(self.dds_custom_width_spin)
            custom_size_row.addWidget(QLabel("x"))
            custom_size_row.addWidget(self.dds_custom_height_spin)
            custom_size_row.addStretch(1)

            dds_output_layout.addWidget(self.enable_dds_staging_checkbox, 0, 0, 1, 3)
            dds_output_layout.addWidget(dds_output_mode_hint, 1, 0, 1, 3)
            dds_output_layout.addWidget(QLabel("Format"), 2, 0)
            dds_output_layout.addWidget(self.dds_format_mode_combo, 2, 1)
            dds_output_layout.addWidget(self.dds_custom_format_label, 3, 0)
            dds_output_layout.addWidget(self.dds_custom_format_combo, 3, 1)
            dds_output_layout.addWidget(QLabel("Size"), 4, 0)
            dds_output_layout.addWidget(self.dds_size_mode_combo, 4, 1)
            dds_output_layout.addWidget(self.dds_custom_size_label, 5, 0)
            dds_output_layout.addWidget(self.dds_custom_size_widget, 5, 1, 1, 2)
            dds_output_layout.addWidget(QLabel("Mipmaps"), 6, 0)
            dds_output_layout.addWidget(self.dds_mip_mode_combo, 6, 1)
            dds_output_layout.addWidget(self.dds_custom_mip_label, 7, 0)
            dds_output_layout.addWidget(self.dds_custom_mip_spin, 7, 1)
            dds_output_layout.addWidget(dds_output_hint, 8, 0, 1, 3)

            self.dds_output_section.body_layout.addWidget(dds_output_group)
            left_layout.addWidget(self.dds_output_section)

            self.filters_section = CollapsibleSection("Filters & Rules", expanded=False)
            filters_group = QWidget()
            filters_layout = QVBoxLayout(filters_group)
            filters_layout.setContentsMargins(0, 0, 0, 0)
            filters_layout.setSpacing(8)
            filters_label = QLabel("Folder / file filter")
            filters_hint = QLabel("Optional glob patterns, one per line or separated by semicolons.")
            filters_hint.setObjectName("HintLabel")
            filters_hint.setWordWrap(True)
            self.filters_edit = QPlainTextEdit()
            self.filters_edit.setPlaceholderText("examples:\ncharacters/*\nui/**/*.dds")
            self.filters_edit.setMinimumHeight(80)
            self.filters_edit.setMaximumHeight(96)
            self.filters_edit.document().setMaximumBlockCount(200)

            texture_rules_label = QLabel("Texture rules")
            texture_rules_hint = QLabel(
                "Optional per-pattern overrides. Hover for the rule syntax."
            )
            texture_rules_hint.setObjectName("HintLabel")
            texture_rules_hint.setWordWrap(True)
            texture_rules_hint.setToolTip(
                "One rule per line: pattern ; action=skip/process ; format=BC7_UNORM ; "
                "size=original/png/2048x2048 ; mips=match_original/full/single/1"
            )
            self.texture_rules_edit = QPlainTextEdit()
            self.texture_rules_edit.setPlaceholderText(
                "# examples\n*_n.dds; format=BC5_UNORM; size=original; mips=match_original\nui/**/*.dds; mips=single\n*_mask.dds; action=skip"
            )
            self.texture_rules_edit.setMinimumHeight(90)
            self.texture_rules_edit.setMaximumHeight(120)
            self.texture_rules_edit.document().setMaximumBlockCount(300)

            filters_layout.addWidget(filters_label)
            filters_layout.addWidget(filters_hint)
            filters_layout.addWidget(self.filters_edit)
            filters_layout.addWidget(texture_rules_label)
            filters_layout.addWidget(texture_rules_hint)
            filters_layout.addWidget(self.texture_rules_edit)
            self.filters_section.body_layout.addWidget(filters_group)
            left_layout.addWidget(self.filters_section)

            self.chainner_section = CollapsibleSection("chaiNNer", expanded=False)
            chainner_group = QWidget()
            chainner_layout = QVBoxLayout(chainner_group)
            chainner_layout.setContentsMargins(0, 0, 0, 0)
            chainner_layout.setSpacing(8)

            self.enable_chainner_checkbox = QCheckBox("Run chaiNNer before DDS rebuild")
            chainner_layout.addWidget(self.enable_chainner_checkbox)

            chainner_paths_layout = QGridLayout()
            chainner_paths_layout.setHorizontalSpacing(10)
            chainner_paths_layout.setVerticalSpacing(10)
            chainner_paths_layout.setColumnMinimumWidth(0, 136)
            chainner_paths_layout.setColumnStretch(1, 1)
            self.chainner_exe_path_edit = QLineEdit()
            self.chainner_chain_path_edit = QLineEdit()
            self.chainner_exe_browse_button = self._add_path_row(
                chainner_paths_layout,
                0,
                "chaiNNer exe path",
                self.chainner_exe_path_edit,
                self._browse_chainner_exe_path,
            )
            self.chainner_chain_browse_button = self._add_path_row(
                chainner_paths_layout,
                1,
                ".chn file path",
                self.chainner_chain_path_edit,
                self._browse_chainner_chain_path,
            )
            chainner_layout.addLayout(chainner_paths_layout)

            chainner_actions = QHBoxLayout()
            chainner_actions.setSpacing(8)
            self.validate_chainner_button = QPushButton("Validate Chain")
            chainner_actions.addStretch(1)
            chainner_actions.addWidget(self.validate_chainner_button)
            chainner_layout.addLayout(chainner_actions)

            chainner_detected_paths_label = QLabel("Chain inspection")
            chainner_detected_paths_label.setObjectName("HintLabel")
            self.chainner_chain_info_view = QPlainTextEdit()
            self.chainner_chain_info_view.setReadOnly(True)
            self.chainner_chain_info_view.setMinimumHeight(128)
            self.chainner_chain_info_view.setMaximumHeight(190)
            self.chainner_chain_info_view.document().setMaximumBlockCount(120)
            self.chainner_chain_info_view.setPlainText(
                "Select a .chn file to inspect and validate its Load Images, Save Images, model paths, and upscale nodes."
            )
            chainner_layout.addWidget(chainner_detected_paths_label)
            chainner_layout.addWidget(self.chainner_chain_info_view)

            chainner_hint = QLabel(
                "Optional override JSON. Supports app path tokens."
            )
            chainner_hint.setObjectName("HintLabel")
            chainner_hint.setWordWrap(True)
            chainner_hint.setToolTip(
                "Paste either the full chaiNNer override object or just the inputs object. "
                "Supported path tokens: ${original_dds_root}, ${staging_png_root}, ${png_root}, ${output_root}, ${texconv_path}."
            )
            self.chainner_override_edit = QPlainTextEdit()
            self.chainner_override_edit.setPlaceholderText(
                '{\n  "inputs": {\n    "your_override_id": "${png_root}"\n  }\n}'
            )
            self.chainner_override_edit.setMinimumHeight(116)
            self.chainner_override_edit.setMaximumHeight(120)
            self.chainner_override_edit.document().setMaximumBlockCount(300)
            chainner_layout.addWidget(chainner_hint)
            chainner_layout.addWidget(self.chainner_override_edit)

            self.chainner_section.body_layout.addWidget(chainner_group)
            left_layout.addWidget(self.chainner_section)
            left_layout.addStretch(1)

            progress_group = QGroupBox("Progress")
            progress_layout = QGridLayout(progress_group)
            progress_layout.setHorizontalSpacing(12)
            progress_layout.setVerticalSpacing(8)
            progress_layout.setColumnMinimumWidth(0, 150)
            progress_layout.setColumnStretch(1, 1)

            self.phase_value = QLabel("Idle")
            self.phase_progress_value = QLabel("Waiting")
            self.total_files_value = QLabel("0")
            self.current_file_value = QLabel("Idle")
            self.current_file_value.setWordWrap(True)
            self.converted_value = QLabel("0")
            self.skipped_value = QLabel("0")
            self.failed_value = QLabel("0")
            self.error_message_value = QLabel("Ready.")
            self.error_message_value.setObjectName("StatusLabel")
            self.error_message_value.setWordWrap(True)

            progress_layout.addWidget(QLabel("Phase"), 0, 0)
            progress_layout.addWidget(self.phase_value, 0, 1)
            progress_layout.addWidget(QLabel("Phase progress"), 1, 0)
            progress_layout.addWidget(self.phase_progress_value, 1, 1)
            progress_layout.addWidget(QLabel("Total files found"), 2, 0)
            progress_layout.addWidget(self.total_files_value, 2, 1)
            progress_layout.addWidget(QLabel("Current file"), 3, 0)
            progress_layout.addWidget(self.current_file_value, 3, 1)
            progress_layout.addWidget(QLabel("Converted / planned"), 4, 0)
            progress_layout.addWidget(self.converted_value, 4, 1)
            progress_layout.addWidget(QLabel("Skipped"), 5, 0)
            progress_layout.addWidget(self.skipped_value, 5, 1)
            progress_layout.addWidget(QLabel("Failed"), 6, 0)
            progress_layout.addWidget(self.failed_value, 6, 1)
            progress_layout.addWidget(QLabel("Status"), 7, 0, alignment=Qt.AlignTop)
            progress_layout.addWidget(self.error_message_value, 7, 1)

            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setFormat("%v / %m")
            progress_layout.addWidget(self.progress_bar, 8, 0, 1, 2)

            right_layout.addWidget(progress_group)

            self.content_tabs = QTabWidget()

            log_tab = QWidget()
            log_tab_layout = QVBoxLayout(log_tab)
            log_tab_layout.setContentsMargins(0, 8, 0, 0)
            log_actions = QHBoxLayout()
            log_actions.setSpacing(8)
            self.clear_log_button = QPushButton("Clear Log")
            log_actions.addStretch(1)
            log_actions.addWidget(self.clear_log_button)
            log_tab_layout.addLayout(log_actions)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.document().setMaximumBlockCount(5000)
            self.log_highlighter = LogHighlighter(self.log_view.document(), self.current_theme_key)
            log_tab_layout.addWidget(self.log_view)
            self.content_tabs.addTab(log_tab, "Live Log")

            compare_tab = QWidget()
            compare_tab_layout = QVBoxLayout(compare_tab)
            compare_tab_layout.setContentsMargins(0, 8, 0, 0)
            compare_tab_layout.setSpacing(10)

            compare_actions = QHBoxLayout()
            compare_actions.setSpacing(8)
            compare_help = QLabel("Select one DDS to compare the original input with the rebuilt output.")
            compare_help.setObjectName("HintLabel")
            compare_help.setWordWrap(True)
            self.compare_previous_button = QPushButton("Previous")
            self.compare_next_button = QPushButton("Next")
            self.compare_sync_pan_checkbox = QCheckBox("Sync Pan")
            self.compare_sync_pan_checkbox.setChecked(True)
            self.refresh_compare_button = QPushButton("Refresh Compare")
            compare_actions.addWidget(compare_help, stretch=1)
            compare_actions.addWidget(self.compare_previous_button)
            compare_actions.addWidget(self.compare_next_button)
            compare_actions.addWidget(self.compare_sync_pan_checkbox)
            compare_actions.addWidget(self.refresh_compare_button)
            compare_tab_layout.addLayout(compare_actions)

            self.compare_splitter = QSplitter(Qt.Horizontal)
            self.compare_splitter.setChildrenCollapsible(False)

            self.compare_list = QListWidget()
            self.compare_list.setSelectionMode(QAbstractItemView.SingleSelection)
            self.compare_list.setMinimumWidth(280)
            self.compare_splitter.addWidget(self.compare_list)

            preview_container = QWidget()
            preview_layout = QHBoxLayout(preview_container)
            preview_layout.setContentsMargins(0, 0, 0, 0)
            preview_layout.setSpacing(10)

            original_preview_column = QVBoxLayout()
            original_preview_header = QHBoxLayout()
            original_preview_header.setSpacing(6)
            original_preview_title = QLabel("Original DDS")
            self.original_compare_zoom_out_button = QPushButton("Zoom -")
            self.original_compare_zoom_fit_button = QPushButton("Fit")
            self.original_compare_zoom_100_button = QPushButton("100%")
            self.original_compare_zoom_in_button = QPushButton("Zoom +")
            self.original_compare_zoom_value = QLabel("Fit")
            self.original_compare_zoom_value.setObjectName("HintLabel")
            original_preview_header.addWidget(original_preview_title)
            original_preview_header.addStretch(1)
            original_preview_header.addWidget(self.original_compare_zoom_out_button)
            original_preview_header.addWidget(self.original_compare_zoom_fit_button)
            original_preview_header.addWidget(self.original_compare_zoom_100_button)
            original_preview_header.addWidget(self.original_compare_zoom_in_button)
            original_preview_header.addWidget(self.original_compare_zoom_value)
            self.original_preview_meta_label = QLabel("")
            self.original_preview_meta_label.setObjectName("HintLabel")
            self.original_preview_meta_label.setWordWrap(True)
            self.original_preview_label = PreviewLabel("Select a DDS file to preview.")
            self.original_preview_scroll = PreviewScrollArea()
            self.original_preview_scroll.setWidgetResizable(False)
            self.original_preview_scroll.setAlignment(Qt.AlignCenter)
            self.original_preview_scroll.setWidget(self.original_preview_label)
            self.original_preview_label.attach_scroll_area(self.original_preview_scroll)
            original_preview_column.addLayout(original_preview_header)
            original_preview_column.addWidget(self.original_preview_meta_label)
            original_preview_column.addWidget(self.original_preview_scroll, stretch=1)

            output_preview_column = QVBoxLayout()
            output_preview_header = QHBoxLayout()
            output_preview_header.setSpacing(6)
            output_preview_title = QLabel("Output DDS")
            self.output_compare_zoom_out_button = QPushButton("Zoom -")
            self.output_compare_zoom_fit_button = QPushButton("Fit")
            self.output_compare_zoom_100_button = QPushButton("100%")
            self.output_compare_zoom_in_button = QPushButton("Zoom +")
            self.output_compare_zoom_value = QLabel("Fit")
            self.output_compare_zoom_value.setObjectName("HintLabel")
            output_preview_header.addWidget(output_preview_title)
            output_preview_header.addStretch(1)
            output_preview_header.addWidget(self.output_compare_zoom_out_button)
            output_preview_header.addWidget(self.output_compare_zoom_fit_button)
            output_preview_header.addWidget(self.output_compare_zoom_100_button)
            output_preview_header.addWidget(self.output_compare_zoom_in_button)
            output_preview_header.addWidget(self.output_compare_zoom_value)
            self.output_preview_meta_label = QLabel("")
            self.output_preview_meta_label.setObjectName("HintLabel")
            self.output_preview_meta_label.setWordWrap(True)
            self.output_preview_label = PreviewLabel("Select a DDS file to preview.")
            self.output_preview_scroll = PreviewScrollArea()
            self.output_preview_scroll.setWidgetResizable(False)
            self.output_preview_scroll.setAlignment(Qt.AlignCenter)
            self.output_preview_scroll.setWidget(self.output_preview_label)
            self.output_preview_label.attach_scroll_area(self.output_preview_scroll)
            output_preview_column.addLayout(output_preview_header)
            output_preview_column.addWidget(self.output_preview_meta_label)
            output_preview_column.addWidget(self.output_preview_scroll, stretch=1)

            preview_layout.addLayout(original_preview_column, stretch=1)
            preview_layout.addLayout(output_preview_column, stretch=1)
            self.compare_splitter.addWidget(preview_container)
            self.compare_splitter.setStretchFactor(0, 1)
            self.compare_splitter.setStretchFactor(1, 3)

            compare_tab_layout.addWidget(self.compare_splitter, stretch=1)
            self.content_tabs.addTab(compare_tab, "Compare")

            right_layout.addWidget(self.content_tabs, stretch=1)

            button_row = QHBoxLayout()
            button_row.setSpacing(8)
            self.scan_button = QPushButton("Scan")
            self.start_button = QPushButton("Start")
            self.stop_button = QPushButton("Stop")
            self.open_output_button = QPushButton("Open Output")
            self.stop_button.setEnabled(False)
            button_row.addWidget(self.scan_button)
            button_row.addWidget(self.start_button)
            button_row.addWidget(self.stop_button)
            button_row.addStretch(1)
            button_row.addWidget(self.open_output_button)
            workflow_layout.addLayout(button_row)

            self.archive_browser_tab = QWidget()
            archive_tab_layout = QVBoxLayout(self.archive_browser_tab)
            archive_tab_layout.setContentsMargins(0, 0, 0, 0)
            archive_tab_layout.setSpacing(10)

            self.archive_splitter = QSplitter(Qt.Horizontal)
            self.archive_splitter.setChildrenCollapsible(False)
            archive_tab_layout.addWidget(self.archive_splitter, stretch=1)

            archive_controls_group = QGroupBox("Archive Controls")
            archive_controls_group.setMinimumWidth(400)
            archive_controls_group.setMaximumWidth(520)
            archive_controls_layout = QVBoxLayout(archive_controls_group)
            archive_controls_layout.setContentsMargins(12, 16, 12, 12)
            archive_controls_layout.setSpacing(8)

            archive_hint = QLabel(
                "Read-only package browser for scan, filter, preview, and extraction."
            )
            archive_hint.setObjectName("HintLabel")
            archive_hint.setWordWrap(True)
            archive_controls_layout.addWidget(archive_hint)

            archive_paths_layout = QVBoxLayout()
            archive_paths_layout.setSpacing(8)
            self.archive_package_root_edit = QLineEdit()
            self.archive_extract_root_edit = QLineEdit()
            package_root_label = QLabel("Package root")
            package_root_row = QHBoxLayout()
            package_root_row.setSpacing(8)
            self.archive_package_root_browse_button = QPushButton("Browse")
            self.archive_package_root_browse_button.setMinimumWidth(80)
            self.archive_package_root_browse_button.clicked.connect(self._browse_archive_package_root)
            self.archive_package_root_detect_button = QPushButton("Auto-detect")
            self.archive_package_root_detect_button.setMinimumWidth(96)
            package_root_row.addWidget(self.archive_package_root_edit, stretch=1)
            package_root_row.addWidget(self.archive_package_root_browse_button)
            package_root_row.addWidget(self.archive_package_root_detect_button)
            archive_paths_layout.addWidget(package_root_label)
            archive_paths_layout.addLayout(package_root_row)

            extract_root_label = QLabel("Extract root")
            extract_root_row = QHBoxLayout()
            extract_root_row.setSpacing(8)
            self.archive_extract_root_browse_button = QPushButton("Browse")
            self.archive_extract_root_browse_button.setMinimumWidth(80)
            self.archive_extract_root_browse_button.clicked.connect(self._browse_archive_extract_root)
            extract_root_row.addWidget(self.archive_extract_root_edit, stretch=1)
            extract_root_row.addWidget(self.archive_extract_root_browse_button)
            archive_paths_layout.addWidget(extract_root_label)
            archive_paths_layout.addLayout(extract_root_row)
            archive_controls_layout.addLayout(archive_paths_layout)

            archive_primary_filter_row = QHBoxLayout()
            archive_primary_filter_row.setSpacing(8)
            self.archive_scan_button = QPushButton("Scan")
            self.archive_refresh_scan_button = QPushButton("Refresh")
            self.archive_refresh_scan_button.setToolTip("Ignore the archive cache and rebuild it from the .pamt files.")
            self.archive_filter_edit = QLineEdit()
            self.archive_filter_edit.setPlaceholderText("Path filter or glob, e.g. */texture/* or *_n.dds")
            self.archive_extension_filter_combo = QComboBox()
            self._add_combo_choice(self.archive_extension_filter_combo, "DDS only", ".dds")
            self._add_combo_choice(self.archive_extension_filter_combo, "All files", "*")
            self._add_combo_choice(self.archive_extension_filter_combo, "PNG only", ".png")
            self._add_combo_choice(self.archive_extension_filter_combo, "PAT only", ".pat")
            self._add_combo_choice(self.archive_extension_filter_combo, "PATX only", ".patx")
            archive_primary_filter_row.addWidget(self.archive_scan_button)
            archive_primary_filter_row.addWidget(self.archive_refresh_scan_button)
            archive_primary_filter_row.addWidget(self.archive_filter_edit, stretch=1)
            archive_primary_filter_row.addWidget(self.archive_extension_filter_combo)
            archive_controls_layout.addLayout(archive_primary_filter_row)

            archive_package_filter_row = QHBoxLayout()
            archive_package_filter_row.setSpacing(8)
            self.archive_package_filter_edit = QLineEdit()
            self.archive_package_filter_edit.setPlaceholderText("Package filter, e.g. 0000/0.pamt or 0012")
            self.archive_package_filter_edit.setMinimumWidth(220)
            self.archive_role_filter_combo = QComboBox()
            self._add_combo_choice(self.archive_role_filter_combo, "All roles", "all")
            self._add_combo_choice(self.archive_role_filter_combo, "Textures", "texture")
            self._add_combo_choice(self.archive_role_filter_combo, "Normal maps", "normal")
            self._add_combo_choice(self.archive_role_filter_combo, "Material / mask", "material")
            self._add_combo_choice(self.archive_role_filter_combo, "Impostor", "impostor")
            self._add_combo_choice(self.archive_role_filter_combo, "UI", "ui")
            self._add_combo_choice(self.archive_role_filter_combo, "Text", "text")
            self.archive_role_filter_combo.setMinimumWidth(132)
            self.archive_min_size_spin = QSpinBox()
            self.archive_min_size_spin.setRange(0, 1024 * 1024)
            self.archive_min_size_spin.setPrefix("Min ")
            self.archive_min_size_spin.setSuffix(" KB")
            self.archive_min_size_spin.setSingleStep(64)
            self.archive_min_size_spin.setMinimumWidth(116)
            self.archive_previewable_only_checkbox = QCheckBox("Previewable")
            self.archive_filter_apply_button = QPushButton("Apply")
            self.archive_filter_clear_button = QPushButton("Clear")
            self.archive_role_filter_combo.setToolTip("Filter by likely asset role.")
            self.archive_min_size_spin.setToolTip("Hide very small files below this original size.")
            self.archive_package_filter_edit.setToolTip("Limit results to matching package names or pamt paths.")
            self.archive_previewable_only_checkbox.setToolTip("Show only files the built-in preview can handle.")
            archive_package_filter_label = QLabel("Package")
            archive_package_filter_label.setObjectName("HintLabel")
            archive_package_filter_row.addWidget(archive_package_filter_label)
            archive_package_filter_row.addWidget(self.archive_package_filter_edit, stretch=1)
            archive_controls_layout.addLayout(archive_package_filter_row)

            archive_structure_filter_row = QHBoxLayout()
            archive_structure_filter_row.setSpacing(8)
            archive_structure_filter_label = QLabel("Folders")
            archive_structure_filter_label.setObjectName("HintLabel")
            self.archive_structure_filter_widget = QWidget()
            self.archive_structure_filter_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.archive_structure_filter_widget.setToolTip("Filter by discovered package and folder structures from the last scan.")
            self.archive_structure_filter_layout = QHBoxLayout(self.archive_structure_filter_widget)
            self.archive_structure_filter_layout.setContentsMargins(0, 0, 0, 0)
            self.archive_structure_filter_layout.setSpacing(8)
            archive_structure_filter_row.addWidget(archive_structure_filter_label)
            archive_structure_filter_row.addWidget(self.archive_structure_filter_widget, stretch=1)
            archive_controls_layout.addLayout(archive_structure_filter_row)

            archive_secondary_filter_row = QHBoxLayout()
            archive_secondary_filter_row.setSpacing(8)
            archive_secondary_filter_row.addWidget(self.archive_role_filter_combo)
            archive_secondary_filter_row.addWidget(self.archive_min_size_spin)
            archive_secondary_filter_row.addWidget(self.archive_previewable_only_checkbox)
            archive_controls_layout.addLayout(archive_secondary_filter_row)

            archive_secondary_actions_row = QHBoxLayout()
            archive_secondary_actions_row.setSpacing(8)
            archive_secondary_actions_row.addStretch(1)
            archive_secondary_actions_row.addWidget(self.archive_filter_apply_button)
            archive_secondary_actions_row.addWidget(self.archive_filter_clear_button)
            archive_controls_layout.addLayout(archive_secondary_actions_row)

            self.archive_package_filter_hint_label = QLabel(
                "Scan uses a saved archive cache when valid. Refresh ignores the cache and rebuilds it from the .pamt files."
            )
            self.archive_package_filter_hint_label.setObjectName("HintLabel")
            self.archive_package_filter_hint_label.setWordWrap(True)
            archive_controls_layout.addWidget(self.archive_package_filter_hint_label)

            archive_actions_row = QHBoxLayout()
            archive_actions_row.setSpacing(8)
            self.archive_extract_selected_button = QPushButton("Extract Selected")
            self.archive_extract_filtered_button = QPushButton("Extract Filtered")
            self.archive_extract_to_workflow_button = QPushButton("DDS To Workflow")
            archive_actions_row.addWidget(self.archive_extract_selected_button)
            archive_actions_row.addWidget(self.archive_extract_filtered_button)
            archive_actions_row.addWidget(self.archive_extract_to_workflow_button)
            archive_controls_layout.addLayout(archive_actions_row)

            self.archive_stats_label = QLabel("No archives scanned.")
            self.archive_stats_label.setObjectName("HintLabel")
            self.archive_stats_label.setWordWrap(True)
            archive_controls_layout.addWidget(self.archive_stats_label)

            self.archive_scan_progress_label = QLabel("Ready to scan archive indexes.")
            self.archive_scan_progress_label.setObjectName("HintLabel")
            self.archive_scan_progress_label.setWordWrap(True)
            archive_controls_layout.addWidget(self.archive_scan_progress_label)

            self.archive_scan_progress_bar = QProgressBar()
            self.archive_scan_progress_bar.setRange(0, 1)
            self.archive_scan_progress_bar.setValue(0)
            self.archive_scan_progress_bar.setTextVisible(True)
            self.archive_scan_progress_bar.setFormat("%v / %m")
            archive_controls_layout.addWidget(self.archive_scan_progress_bar)

            archive_log_actions = QHBoxLayout()
            archive_log_actions.setSpacing(8)
            archive_log_label = QLabel("Archive Scan Log")
            archive_log_label.setObjectName("HintLabel")
            self.clear_archive_log_button = QPushButton("Clear")
            self.clear_archive_log_button.setMinimumWidth(72)
            archive_log_actions.addWidget(archive_log_label)
            archive_log_actions.addStretch(1)
            archive_log_actions.addWidget(self.clear_archive_log_button)
            archive_controls_layout.addLayout(archive_log_actions)

            self.archive_log_view = QPlainTextEdit()
            self.archive_log_view.setReadOnly(True)
            self.archive_log_view.setMinimumHeight(110)
            self.archive_log_view.setMaximumHeight(160)
            self.archive_log_view.document().setMaximumBlockCount(2000)
            self.archive_log_highlighter = LogHighlighter(self.archive_log_view.document(), self.current_theme_key)
            archive_controls_layout.addWidget(self.archive_log_view)

            self.archive_controls_scroll = QScrollArea()
            self.archive_controls_scroll.setWidgetResizable(True)
            self.archive_controls_scroll.setFrameShape(QFrame.NoFrame)
            self.archive_controls_scroll.setMinimumWidth(400)
            self.archive_controls_scroll.setMaximumWidth(540)
            archive_controls_wrapper = QWidget()
            archive_controls_wrapper_layout = QVBoxLayout(archive_controls_wrapper)
            archive_controls_wrapper_layout.setContentsMargins(0, 0, 0, 0)
            archive_controls_wrapper_layout.setSpacing(0)
            archive_controls_wrapper_layout.addWidget(archive_controls_group)
            archive_controls_wrapper_layout.addStretch(1)
            self.archive_controls_scroll.setWidget(archive_controls_wrapper)
            self.archive_splitter.addWidget(self.archive_controls_scroll)

            archive_files_group = QGroupBox("Archive Files")
            archive_files_group.setMinimumWidth(320)
            archive_files_layout = QVBoxLayout(archive_files_group)
            archive_files_layout.setContentsMargins(10, 12, 10, 10)
            archive_files_layout.setSpacing(0)

            self.archive_tree = QTreeWidget()
            self.archive_tree.setHeaderLabels(["Name", "Type", "Size", "Stored", "Comp", "Package"])
            self.archive_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.archive_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.archive_tree.setAlternatingRowColors(False)
            self.archive_tree.setRootIsDecorated(True)
            self.archive_tree.setUniformRowHeights(True)
            self.archive_tree.header().setStretchLastSection(False)
            self.archive_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.archive_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.archive_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.archive_tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            self.archive_tree.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
            self.archive_tree.header().setSectionResizeMode(5, QHeaderView.ResizeToContents)
            archive_files_layout.addWidget(self.archive_tree)
            self.archive_splitter.addWidget(archive_files_group)

            archive_preview_group = QGroupBox("Archive Preview")
            archive_preview_group.setMinimumWidth(360)
            archive_preview_container_layout = QVBoxLayout(archive_preview_group)
            archive_preview_container_layout.setContentsMargins(10, 12, 10, 10)
            archive_preview_container_layout.setSpacing(10)

            archive_preview_header = QHBoxLayout()
            archive_preview_header.setSpacing(8)
            self.archive_preview_title_label = QLabel("Select an archive file")
            self.archive_preview_title_label.setWordWrap(True)
            self.archive_preview_warning_badge = QLabel("")
            self.archive_preview_warning_badge.setObjectName("WarningBadge")
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_loose_toggle_button = QPushButton("Show Loose File")
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_zoom_out_button = QPushButton("Zoom -")
            self.archive_preview_zoom_fit_button = QPushButton("Fit")
            self.archive_preview_zoom_100_button = QPushButton("100%")
            self.archive_preview_zoom_in_button = QPushButton("Zoom +")
            self.archive_preview_zoom_value = QLabel("Fit")
            self.archive_preview_zoom_value.setObjectName("HintLabel")
            archive_preview_header.addWidget(self.archive_preview_title_label, stretch=1)
            archive_preview_header.addWidget(self.archive_preview_warning_badge)
            archive_preview_header.addWidget(self.archive_preview_loose_toggle_button)
            archive_preview_header.addWidget(self.archive_preview_zoom_out_button)
            archive_preview_header.addWidget(self.archive_preview_zoom_fit_button)
            archive_preview_header.addWidget(self.archive_preview_zoom_100_button)
            archive_preview_header.addWidget(self.archive_preview_zoom_in_button)
            archive_preview_header.addWidget(self.archive_preview_zoom_value)
            archive_preview_container_layout.addLayout(archive_preview_header)

            self.archive_preview_meta_label = QLabel("Select an archive file to preview it here.")
            self.archive_preview_meta_label.setObjectName("HintLabel")
            self.archive_preview_meta_label.setWordWrap(True)
            archive_preview_container_layout.addWidget(self.archive_preview_meta_label)
            self.archive_preview_warning_label = QLabel("")
            self.archive_preview_warning_label.setObjectName("WarningText")
            self.archive_preview_warning_label.setWordWrap(True)
            self.archive_preview_warning_label.setVisible(False)
            archive_preview_container_layout.addWidget(self.archive_preview_warning_label)

            self.archive_preview_stack = QStackedWidget()
            self.archive_preview_label = PreviewLabel("Select an archive file to preview it here.")
            self.archive_preview_scroll = PreviewScrollArea()
            self.archive_preview_scroll.setWidgetResizable(False)
            self.archive_preview_scroll.setAlignment(Qt.AlignCenter)
            self.archive_preview_scroll.setWidget(self.archive_preview_label)
            self.archive_preview_label.attach_scroll_area(self.archive_preview_scroll)
            self.archive_preview_text_edit = QPlainTextEdit()
            self.archive_preview_text_edit.setReadOnly(True)
            self.archive_preview_text_edit.document().setMaximumBlockCount(5000)
            self.archive_preview_info_edit = QPlainTextEdit()
            self.archive_preview_info_edit.setReadOnly(True)
            self.archive_preview_info_edit.document().setMaximumBlockCount(2000)
            self.archive_preview_stack.addWidget(self.archive_preview_scroll)
            self.archive_preview_stack.addWidget(self.archive_preview_text_edit)
            self.archive_preview_stack.addWidget(self.archive_preview_info_edit)
            self.archive_preview_details_edit = QPlainTextEdit()
            self.archive_preview_details_edit.setReadOnly(True)
            self.archive_preview_details_edit.document().setMaximumBlockCount(2000)
            self.archive_preview_tabs = QTabWidget()
            archive_preview_tab = QWidget()
            archive_preview_tab_layout = QVBoxLayout(archive_preview_tab)
            archive_preview_tab_layout.setContentsMargins(0, 0, 0, 0)
            archive_preview_tab_layout.setSpacing(0)
            archive_preview_tab_layout.addWidget(self.archive_preview_stack)
            archive_details_tab = QWidget()
            archive_details_tab_layout = QVBoxLayout(archive_details_tab)
            archive_details_tab_layout.setContentsMargins(0, 0, 0, 0)
            archive_details_tab_layout.setSpacing(0)
            archive_details_tab_layout.addWidget(self.archive_preview_details_edit)
            self.archive_preview_tabs.addTab(archive_preview_tab, "Preview")
            self.archive_preview_tabs.addTab(archive_details_tab, "Details")
            archive_preview_container_layout.addWidget(self.archive_preview_tabs, stretch=1)
            self.archive_splitter.addWidget(archive_preview_group)
            self.archive_splitter.setStretchFactor(0, 1)
            self.archive_splitter.setStretchFactor(1, 2)
            self.archive_splitter.setStretchFactor(2, 3)
            self.archive_splitter.setSizes([420, 420, 760])
            self.main_tabs.addTab(self.archive_browser_tab, "Archive Browser")

            self.text_search_tab = TextSearchTab(
                settings=self.settings,
                base_dir=self.settings_file_path.parent,
                theme_key=self.current_theme_key,
            )
            self.text_search_tab.status_message_requested.connect(
                lambda message, is_error: self.set_status_message(message, error=is_error)
            )
            self.main_tabs.addTab(self.text_search_tab, "Text Search")

            self.setCentralWidget(central)

            self.export_profile_action.triggered.connect(self.export_profile)
            self.import_profile_action.triggered.connect(self.import_profile)
            self.validate_chainner_menu_action.triggered.connect(self.validate_chainner_chain)
            self.export_diagnostics_action.triggered.connect(self.export_diagnostic_bundle)
            self.quick_start_menu_action.triggered.connect(self.show_quick_start_dialog)
            self.about_menu_action.triggered.connect(self.show_about_dialog)
            self.scan_button.clicked.connect(self.start_scan)
            self.start_button.clicked.connect(self.start_build)
            self.stop_button.clicked.connect(self.stop_build)
            self.open_output_button.clicked.connect(self.open_output_folder)
            self.init_workspace_button.clicked.connect(self.initialize_workspace)
            self.create_folders_button.clicked.connect(self.create_missing_folders)
            self.download_chainner_button.clicked.connect(self.download_chainner)
            self.download_texconv_button.clicked.connect(self.download_texconv)
            self.validate_chainner_button.clicked.connect(self.validate_chainner_chain)
            self.clear_log_button.clicked.connect(self.clear_live_log)
            self.clear_archive_log_button.clicked.connect(self.clear_archive_scan_log)
            self.refresh_compare_button.clicked.connect(self.refresh_compare_list)
            self.archive_package_root_detect_button.clicked.connect(self.autodetect_archive_package_root)
            self.archive_scan_button.clicked.connect(self.scan_archives)
            self.archive_refresh_scan_button.clicked.connect(lambda: self.scan_archives(force_refresh=True))
            self.archive_extract_selected_button.clicked.connect(self.extract_selected_archive_entries)
            self.archive_extract_filtered_button.clicked.connect(self.extract_filtered_archive_entries)
            self.archive_extract_to_workflow_button.clicked.connect(self.extract_filtered_archive_dds_to_workflow)
            self.archive_filter_apply_button.clicked.connect(self._apply_archive_filter)
            self.archive_filter_clear_button.clicked.connect(self._clear_archive_filters)
            self.archive_filter_edit.returnPressed.connect(self._apply_archive_filter)
            self.archive_package_filter_edit.returnPressed.connect(self._apply_archive_filter)
            self.archive_filter_edit.textChanged.connect(self._save_settings)
            self.archive_filter_edit.textChanged.connect(self._mark_archive_filters_dirty)
            self.archive_package_filter_edit.textChanged.connect(self._save_settings)
            self.archive_package_filter_edit.textChanged.connect(self._mark_archive_filters_dirty)
            self.archive_extension_filter_combo.currentIndexChanged.connect(self._save_settings)
            self.archive_extension_filter_combo.currentIndexChanged.connect(self._mark_archive_filters_dirty)
            self.archive_role_filter_combo.currentIndexChanged.connect(self._save_settings)
            self.archive_role_filter_combo.currentIndexChanged.connect(self._mark_archive_filters_dirty)
            self.archive_min_size_spin.valueChanged.connect(self._save_settings)
            self.archive_min_size_spin.valueChanged.connect(self._mark_archive_filters_dirty)
            self.archive_previewable_only_checkbox.toggled.connect(self._save_settings)
            self.archive_previewable_only_checkbox.toggled.connect(self._mark_archive_filters_dirty)
            self.archive_tree.currentItemChanged.connect(self._handle_archive_current_item_change)
            self.archive_tree.itemSelectionChanged.connect(self._update_archive_selection_state)
            self.archive_tree.itemExpanded.connect(self._handle_archive_item_expanded)
            self.archive_preview_zoom_fit_button.clicked.connect(self._set_archive_preview_fit_mode)
            self.archive_preview_zoom_100_button.clicked.connect(lambda: self._set_archive_preview_zoom_factor(1.0))
            self.archive_preview_zoom_out_button.clicked.connect(lambda: self._adjust_archive_preview_zoom(-1))
            self.archive_preview_zoom_in_button.clicked.connect(lambda: self._adjust_archive_preview_zoom(1))
            self.archive_preview_loose_toggle_button.clicked.connect(self._toggle_archive_loose_preview)
            self.compare_previous_button.clicked.connect(lambda: self._select_compare_offset(-1))
            self.compare_next_button.clicked.connect(lambda: self._select_compare_offset(1))
            self.compare_sync_pan_checkbox.toggled.connect(self._sync_compare_scroll_positions)
            self.original_compare_zoom_fit_button.clicked.connect(lambda: self._set_compare_fit_mode("original"))
            self.original_compare_zoom_100_button.clicked.connect(lambda: self._set_compare_zoom_factor("original", 1.0))
            self.original_compare_zoom_out_button.clicked.connect(lambda: self._adjust_compare_zoom("original", -1))
            self.original_compare_zoom_in_button.clicked.connect(lambda: self._adjust_compare_zoom("original", 1))
            self.output_compare_zoom_fit_button.clicked.connect(lambda: self._set_compare_fit_mode("output"))
            self.output_compare_zoom_100_button.clicked.connect(lambda: self._set_compare_zoom_factor("output", 1.0))
            self.output_compare_zoom_out_button.clicked.connect(lambda: self._adjust_compare_zoom("output", -1))
            self.output_compare_zoom_in_button.clicked.connect(lambda: self._adjust_compare_zoom("output", 1))
            self.compare_list.currentItemChanged.connect(self._handle_compare_selection_change)
            self.original_preview_scroll.horizontalScrollBar().valueChanged.connect(
                lambda value: self._sync_compare_scrollbar(
                    self.original_preview_scroll.horizontalScrollBar(),
                    self.output_preview_scroll.horizontalScrollBar(),
                    value,
                )
            )
            self.original_preview_scroll.verticalScrollBar().valueChanged.connect(
                lambda value: self._sync_compare_scrollbar(
                    self.original_preview_scroll.verticalScrollBar(),
                    self.output_preview_scroll.verticalScrollBar(),
                    value,
                )
            )
            self.output_preview_scroll.horizontalScrollBar().valueChanged.connect(
                lambda value: self._sync_compare_scrollbar(
                    self.output_preview_scroll.horizontalScrollBar(),
                    self.original_preview_scroll.horizontalScrollBar(),
                    value,
                )
            )
            self.output_preview_scroll.verticalScrollBar().valueChanged.connect(
                lambda value: self._sync_compare_scrollbar(
                    self.output_preview_scroll.verticalScrollBar(),
                    self.original_preview_scroll.verticalScrollBar(),
                    value,
                )
            )

            self._connect_auto_save()
            self._load_settings()
            self._rebuild_archive_structure_filter_controls()
            self._refresh_chainner_chain_info()
            self._apply_csv_log_enabled_state()
            self._apply_chainner_enabled_state()
            self._apply_dds_staging_enabled_state()
            self._apply_dds_output_state()
            self._apply_compare_zoom("original")
            self._apply_compare_zoom("output")
            self._clear_archive_preview("Select an archive file to preview it here.")
            self.archive_filters_dirty = False
            self._update_archive_filter_button_state()
            self._update_archive_selection_state()
            self._update_compare_navigation_state()
            self.refresh_compare_list()
            self._settings_ready = True
            self._save_settings()

            geometry = self.settings.value("window/geometry")
            if geometry:
                self.restoreGeometry(geometry)
            QTimer.singleShot(0, self._apply_responsive_window_defaults)
            QTimer.singleShot(120, self._show_first_run_guide_if_needed)

        def focus_quick_start_sections(self, *, include_chainner: bool) -> None:
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.setup_section.set_expanded(True)
            self.paths_section.set_expanded(True)
            self.settings_section.set_expanded(False)
            self.dds_output_section.set_expanded(False)
            self.filters_section.set_expanded(False)
            self.chainner_section.set_expanded(include_chainner)

        def show_quick_start_dialog(self) -> None:
            dialog = QuickStartDialog(self)
            dialog.exec()

        def _build_about_html(self) -> str:
            readme_path = Path(__file__).resolve().parents[2] / "README.md"
            notices_path = Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md"
            license_path = Path(__file__).resolve().parents[2] / "LICENSE"
            return f"""
            <h3>{APP_TITLE}</h3>
            <p>A Windows desktop tool for Crimson Desert texture workflows: read-only archive browsing, DDS-to-PNG conversion, optional external upscaling through <b>chaiNNer</b>, DDS rebuild with <b>texconv</b>, and side-by-side compare review.</p>
            <h3>What It Covers</h3>
            <ul>
              <li>Read-only <code>.pamt/.paz</code> archive browsing and selective extraction</li>
              <li>Loose DDS workflow scanning, DDS-to-PNG conversion, rebuild, and compare</li>
              <li>Optional <b>chaiNNer</b> stage before DDS rebuild</li>
              <li>Local config and archive cache stored beside the EXE</li>
            </ul>
            <h3>Important chaiNNer Notes</h3>
            <ul>
              <li><b>chaiNNer</b> is optional and external to this app.</li>
              <li>You must install and maintain <b>chaiNNer</b> separately.</li>
              <li>You must install the backends/packages your chain needs inside <b>chaiNNer</b>, such as <b>PyTorch</b>, <b>NCNN</b>, or <b>ONNX Runtime</b>.</li>
              <li>You must provide and test your own <code>.chn</code> chain.</li>
              <li>If you enable DDS-to-PNG conversion before processing, your chain must read PNG input from the correct folder.</li>
            </ul>
            <h3>Dependencies</h3>
            <ul>
              <li><a href=\"https://doc.qt.io/qtforpython-6/\">PySide6 / Qt for Python</a></li>
              <li><a href=\"https://pyinstaller.org/\">PyInstaller</a></li>
              <li><a href=\"https://github.com/python-lz4/python-lz4\">python-lz4</a></li>
              <li><a href=\"https://github.com/microsoft/DirectXTex\">DirectXTex / texconv</a></li>
              <li><a href=\"https://chainner.app/download/\">chaiNNer</a></li>
            </ul>
            <h3>Credits and References</h3>
            <ul>
              <li><a href=\"https://github.com/lazorr410/crimson-desert-unpacker\">lazorr410/crimson-desert-unpacker</a> for archive format reference.</li>
              <li><a href=\"https://github.com/microsoft/DirectXTex/releases\">Microsoft DirectXTex releases</a> for texconv.</li>
              <li><a href=\"https://chainner.app/\">chaiNNer</a> for the optional upscaling stage.</li>
            </ul>
            <h3>Project Files</h3>
            <p>License: <b>{license_path}</b></p>
            <p>Third-party notices: <b>{notices_path}</b></p>
            <p>Config file: <b>{self.settings_file_path}</b></p>
            <p>Archive cache: <b>{self.archive_cache_root}</b></p>
            <p>README: <b>{readme_path}</b></p>
            <h3>Known Limitations</h3>
            <ul>
              <li>Archive previews are best-effort for unusual or game-specific DDS cases.</li>
              <li><b>chaiNNer</b> remains an external dependency and chain behavior is only as reliable as the chain you provide.</li>
              <li>Large initial archive scans and cache rebuilds can still take time because Crimson Desert package indexes are large.</li>
            </ul>
            <h3>Notes</h3>
            <p>The archive browser is read-only. It extracts to loose files only and never writes back to <code>.pamt</code> or <code>.paz</code>.</p>
            <p>For the full setup flow, usage notes, credits, and troubleshooting guidance, open the Quick Start guide or the README.</p>
            """

        def show_about_dialog(self) -> None:
            dialog = AboutDialog(self, title=f"About {APP_TITLE}", html=self._build_about_html())
            dialog.exec()

        def _collect_profile_payload(self) -> Dict[str, object]:
            return {
                "app": APP_TITLE,
                "profile_format": 1,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "theme": self.current_theme_key,
                "config": dict(vars(self.collect_config())),
            }

        def _resolve_chainner_analysis(self) -> Tuple[Optional[ChainnerChainAnalysis], str]:
            chain_path_text = self.chainner_chain_path_edit.text().strip()
            if not chain_path_text:
                return None, "Select a .chn file to inspect and validate it."

            try:
                chain_path = Path(chain_path_text).expanduser().resolve()
            except OSError as exc:
                return None, f"Could not resolve chain path: {exc}"

            if not chain_path.exists() or not chain_path.is_file():
                return None, f"Chain file not found: {chain_path}"

            original_root_text = self.original_dds_edit.text().strip()
            png_root_text = self.png_root_edit.text().strip()
            original_root = Path(original_root_text).expanduser().resolve() if original_root_text else None
            png_root = Path(png_root_text).expanduser().resolve() if png_root_text else None

            analysis = analyze_chainner_chain_paths(
                chain_path,
                original_dds_root=original_root,
                png_root=png_root,
                chainner_override_json=self.chainner_override_edit.toPlainText(),
            )
            text = format_chainner_analysis(analysis)

            notes: List[str] = []
            if self.chainner_override_edit.toPlainText().strip():
                notes.append(
                    "Override JSON is configured. Runtime overrides may replace some hardcoded chain paths shown above."
                )
            if original_root is None or png_root is None:
                notes.append(
                    "Path-mismatch validation is limited until both Original DDS root and PNG root are configured."
                )
            if notes:
                text += "\n\nNotes:\n" + "\n".join(f"- {note}" for note in notes)

            return analysis, text

        def _apply_profile_config(self, config: AppConfig, *, theme_key: Optional[str] = None) -> None:
            previous_ready = self._settings_ready
            self._settings_ready = False
            try:
                self.original_dds_edit.setText(config.original_dds_root)
                self.png_root_edit.setText(config.png_root)
                self.dds_staging_root_edit.setText(config.dds_staging_root)
                self.output_root_edit.setText(config.output_root)
                self.texconv_path_edit.setText(config.texconv_path)
                self._set_combo_by_value(self.dds_format_mode_combo, config.dds_format_mode)
                self._set_combo_by_value(self.dds_custom_format_combo, config.dds_custom_format)
                self._set_combo_by_value(self.dds_size_mode_combo, config.dds_size_mode)
                self.dds_custom_width_spin.setValue(int(config.dds_custom_width))
                self.dds_custom_height_spin.setValue(int(config.dds_custom_height))
                self._set_combo_by_value(self.dds_mip_mode_combo, config.dds_mip_mode)
                self.dds_custom_mip_spin.setValue(int(config.dds_custom_mip_count))
                self.enable_dds_staging_checkbox.setChecked(bool(config.enable_dds_staging))
                self.enable_incremental_resume_checkbox.setChecked(bool(config.enable_incremental_resume))
                self.texture_rules_edit.setPlainText(config.texture_rules_text)
                self.dry_run_checkbox.setChecked(bool(config.dry_run))
                self.csv_log_enabled_checkbox.setChecked(bool(config.csv_log_enabled))
                self.csv_log_path_edit.setText(config.csv_log_path)
                self.unique_basename_checkbox.setChecked(bool(config.allow_unique_basename_fallback))
                self.overwrite_existing_checkbox.setChecked(bool(config.overwrite_existing_dds))
                self.filters_edit.setPlainText(config.include_filters)
                self.enable_chainner_checkbox.setChecked(bool(config.enable_chainner))
                self.chainner_exe_path_edit.setText(config.chainner_exe_path)
                self.chainner_chain_path_edit.setText(config.chainner_chain_path)
                self.chainner_override_edit.setPlainText(config.chainner_override_json)
                self.archive_package_root_edit.setText(config.archive_package_root)
                self.archive_extract_root_edit.setText(config.archive_extract_root)
                self.archive_filter_edit.setText(config.archive_filter_text)
                self._set_combo_by_value(self.archive_extension_filter_combo, config.archive_extension_filter)
                self.archive_package_filter_edit.setText(config.archive_package_filter_text)
                self.archive_structure_filter_pending_value = config.archive_structure_filter
                self._set_combo_by_value(self.archive_role_filter_combo, config.archive_role_filter)
                self.archive_min_size_spin.setValue(int(config.archive_min_size_kb))
                self.archive_previewable_only_checkbox.setChecked(bool(config.archive_previewable_only))
            finally:
                self._settings_ready = previous_ready

            self._apply_csv_log_enabled_state()
            self._apply_chainner_enabled_state()
            self._apply_dds_staging_enabled_state()
            self._apply_dds_output_state()
            self._refresh_chainner_chain_info()
            if theme_key and theme_key in UI_THEME_SCHEMES:
                self._handle_theme_changed(theme_key)
            self._save_settings()

        def export_profile(self) -> None:
            try:
                default_name = self.settings_file_path.parent / "crimson_texture_forge_profile.ctfprofile.json"
                selected, _ = QFileDialog.getSaveFileName(
                    self,
                    "Export Profile",
                    str(default_name),
                    "Crimson Texture Forge profile (*.ctfprofile.json);;JSON files (*.json);;All files (*.*)",
                )
                if not selected:
                    return

                target = Path(selected).expanduser()
                if not target.suffix:
                    target = target.with_suffix(".ctfprofile.json")

                payload = self._collect_profile_payload()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                self.set_status_message(f"Profile exported to {target}")
                self.append_log(f"Profile exported: {target}")
            except Exception as exc:
                self.set_status_message(str(exc), error=True)
                self.append_log(f"ERROR: {exc}")

        def import_profile(self) -> None:
            try:
                selected, _ = QFileDialog.getOpenFileName(
                    self,
                    "Import Profile",
                    str(self.settings_file_path.parent),
                    "Crimson Texture Forge profile (*.ctfprofile.json *.json);;All files (*.*)",
                )
                if not selected:
                    return

                answer = QMessageBox.question(
                    self,
                    "Import Profile",
                    "Importing a profile will replace the current paths and workflow settings. Continue?",
                )
                if answer != QMessageBox.Yes:
                    return

                source = Path(selected).expanduser()
                payload = json.loads(source.read_text(encoding="utf-8"))
                raw_config = payload.get("config", payload) if isinstance(payload, dict) else payload
                if not isinstance(raw_config, dict):
                    raise ValueError("Profile file is invalid. Expected a JSON object.")

                defaults = default_config()
                config_values = dict(vars(defaults))
                for key in list(config_values):
                    if key in raw_config:
                        config_values[key] = raw_config[key]

                imported_config = AppConfig(**config_values)
                theme_key = payload.get("theme") if isinstance(payload, dict) else None
                theme_text = str(theme_key) if isinstance(theme_key, str) else None
                self._apply_profile_config(imported_config, theme_key=theme_text)
                self.set_status_message(f"Profile imported from {source}")
                self.append_log(f"Profile imported: {source}")
            except Exception as exc:
                self.set_status_message(str(exc), error=True)
                self.append_log(f"ERROR: {exc}")

        def export_diagnostic_bundle(self) -> None:
            try:
                default_name = self.settings_file_path.parent / "crimson_texture_forge_diagnostics.zip"
                selected, _ = QFileDialog.getSaveFileName(
                    self,
                    "Export Diagnostic Bundle",
                    str(default_name),
                    "ZIP archive (*.zip);;All files (*.*)",
                )
                if not selected:
                    return

                target = Path(selected).expanduser()
                if not target.suffix:
                    target = target.with_suffix(".zip")

                analysis, analysis_text = self._resolve_chainner_analysis()
                cache_files: List[Dict[str, object]] = []
                if self.archive_cache_root.exists():
                    for cache_file in sorted(self.archive_cache_root.glob("*")):
                        if not cache_file.is_file():
                            continue
                        try:
                            stat = cache_file.stat()
                        except OSError:
                            continue
                        cache_files.append(
                            {
                                "name": cache_file.name,
                                "size_bytes": stat.st_size,
                                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                            }
                        )

                diagnostics = {
                    "app": APP_TITLE,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "platform": platform.platform(),
                    "python_version": sys.version,
                    "executable": sys.executable,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "theme": self.current_theme_key,
                    "settings_file": str(self.settings_file_path),
                    "archive_cache_root": str(self.archive_cache_root),
                    "archive_cache_files": cache_files,
                    "profile": self._collect_profile_payload(),
                    "chainner_warning_count": len(analysis.warnings) if analysis is not None else None,
                }

                readme_path = Path(__file__).resolve().parents[2] / "README.md"
                notices_path = Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md"
                license_path = Path(__file__).resolve().parents[2] / "LICENSE"

                target.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("diagnostics.json", json.dumps(diagnostics, indent=2))
                    archive.writestr("chainner_analysis.txt", analysis_text)
                    archive.writestr("live_log.txt", self.log_view.toPlainText())
                    archive.writestr("archive_scan_log.txt", self.archive_log_view.toPlainText())
                    if self.settings_file_path.exists():
                        archive.writestr(
                            self.settings_file_path.name,
                            self.settings_file_path.read_text(encoding="utf-8"),
                        )
                    if readme_path.exists():
                        archive.writestr(readme_path.name, readme_path.read_text(encoding="utf-8"))
                    if notices_path.exists():
                        archive.writestr(notices_path.name, notices_path.read_text(encoding="utf-8"))
                    if license_path.exists():
                        archive.writestr(license_path.name, license_path.read_text(encoding="utf-8"))
                    for archive_name, archive_text in self.text_search_tab.diagnostic_entries().items():
                        archive.writestr(archive_name, archive_text)

                self.set_status_message(f"Diagnostic bundle exported to {target}")
                self.append_log(f"Diagnostic bundle exported: {target}")
            except Exception as exc:
                self.set_status_message(str(exc), error=True)
                self.append_log(f"ERROR: {exc}")

        def validate_chainner_chain(self) -> None:
            analysis, text = self._resolve_chainner_analysis()
            self.chainner_chain_info_view.setPlainText(text)
            if analysis is None:
                self.set_status_message(text, error=True)
                return
            if analysis.warnings:
                self.set_status_message(
                    f"chaiNNer chain validation found {len(analysis.warnings)} issue(s).",
                    error=True,
                )
                self.append_log(f"chaiNNer validation warnings: {len(analysis.warnings)} issue(s) found.")
                for warning in analysis.warnings:
                    self.append_log(f"chaiNNer validation: {warning}")
            else:
                self.set_status_message("chaiNNer chain validation passed.")
                self.append_log("chaiNNer validation: no obvious issues detected.")

        def _show_first_run_guide_if_needed(self) -> None:
            if not self.show_quick_start_on_launch:
                return
            self.show_quick_start_on_launch = False
            self.settings.setValue("ui/quick_start_shown", True)
            self.settings.sync()
            self.focus_quick_start_sections(include_chainner=False)
            self.show_quick_start_dialog()

        def _add_path_row(
            self,
            layout: QGridLayout,
            row: int,
            label_text: str,
            line_edit: QLineEdit,
            browse_handler: Callable[[], None],
        ) -> QPushButton:
            label = QLabel(label_text)
            label.setMinimumWidth(124)
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            browse_button = QPushButton("Browse")
            browse_button.setMinimumWidth(88)
            browse_button.clicked.connect(browse_handler)
            layout.addWidget(label, row, 0)
            layout.addWidget(line_edit, row, 1)
            layout.addWidget(browse_button, row, 2)
            return browse_button

        def _set_theme_menu_selection(self, theme_key: str) -> None:
            resolved = theme_key if theme_key in UI_THEME_SCHEMES else DEFAULT_UI_THEME
            for action_theme_key, action in self.theme_actions_by_key.items():
                action.blockSignals(True)
                action.setChecked(action_theme_key == resolved)
                action.blockSignals(False)

        def _handle_theme_changed(self, theme_key: Optional[str] = None) -> None:
            resolved_theme_key = theme_key if theme_key in UI_THEME_SCHEMES else self.current_theme_key
            app = QApplication.instance()
            if app is None:
                return
            self.current_theme_key = apply_app_theme(app, resolved_theme_key)
            self._set_theme_menu_selection(self.current_theme_key)
            self.log_highlighter.set_theme(self.current_theme_key)
            self.archive_log_highlighter.set_theme(self.current_theme_key)
            self.text_search_tab.set_theme(self.current_theme_key)
            self._save_settings()

        def _apply_responsive_window_defaults(self) -> None:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            available = screen.availableGeometry()
            if self.width() > available.width() - 24 or self.height() > available.height() - 24:
                self.resize(
                    max(self.minimumWidth(), min(int(available.width() * 0.94), available.width() - 24)),
                    max(self.minimumHeight(), min(int(available.height() * 0.92), available.height() - 24)),
                )
            frame = self.frameGeometry()
            x = frame.x()
            y = frame.y()
            max_x = max(available.left(), available.right() - frame.width() + 1)
            max_y = max(available.top(), available.bottom() - frame.height() + 1)
            self.move(
                min(max(x, available.left()), max_x),
                min(max(y, available.top()), max_y),
            )
            total_width = max(self.width() - 64, self.minimumWidth())
            self.workflow_splitter.setSizes(
                [
                    max(390, int(total_width * 0.35)),
                    max(380, int(total_width * 0.65)),
                ]
            )
            self.compare_splitter.setSizes(
                [
                    max(260, int(total_width * 0.26)),
                    max(480, int(total_width * 0.74)),
                ]
            )
            self.archive_splitter.setSizes(
                [
                    max(400, int(total_width * 0.24)),
                    max(320, int(total_width * 0.28)),
                    max(360, int(total_width * 0.48)),
                ]
            )

        def _add_combo_choice(self, combo: QComboBox, label: str, value: str) -> None:
            combo.addItem(label, value)

        def _combo_value(self, combo: QComboBox) -> str:
            data = combo.currentData()
            return str(data) if data is not None else combo.currentText().strip()

        def _set_combo_by_value(self, combo: QComboBox, value: str) -> None:
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)

        def _connect_auto_save(self) -> None:
            line_edits = [
                self.original_dds_edit,
                self.png_root_edit,
                self.dds_staging_root_edit,
                self.output_root_edit,
                self.texconv_path_edit,
                self.csv_log_path_edit,
                self.chainner_exe_path_edit,
                self.chainner_chain_path_edit,
                self.archive_package_root_edit,
                self.archive_extract_root_edit,
            ]
            for line_edit in line_edits:
                line_edit.textChanged.connect(self._save_settings)

            checkboxes = [
                self.dry_run_checkbox,
                self.enable_dds_staging_checkbox,
                self.enable_incremental_resume_checkbox,
                self.csv_log_enabled_checkbox,
                self.unique_basename_checkbox,
                self.overwrite_existing_checkbox,
                self.enable_chainner_checkbox,
            ]
            for checkbox in checkboxes:
                checkbox.toggled.connect(self._save_settings)

            combos = [
                self.dds_format_mode_combo,
                self.dds_custom_format_combo,
                self.dds_size_mode_combo,
                self.dds_mip_mode_combo,
            ]
            for combo in combos:
                combo.currentIndexChanged.connect(self._save_settings)

            spins = [
                self.dds_custom_width_spin,
                self.dds_custom_height_spin,
                self.dds_custom_mip_spin,
            ]
            for spin in spins:
                spin.valueChanged.connect(self._save_settings)

            self.csv_log_enabled_checkbox.toggled.connect(self._apply_csv_log_enabled_state)
            self.enable_chainner_checkbox.toggled.connect(self._apply_chainner_enabled_state)
            self.enable_dds_staging_checkbox.toggled.connect(self._apply_dds_staging_enabled_state)
            self.dds_format_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.dds_size_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.dds_mip_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.compare_sync_pan_checkbox.toggled.connect(self._save_settings)
            self.main_tabs.currentChanged.connect(self._handle_main_tab_changed)
            self.setup_section.toggled.connect(self._save_settings)
            self.paths_section.toggled.connect(self._save_settings)
            self.settings_section.toggled.connect(self._save_settings)
            self.dds_output_section.toggled.connect(self._save_settings)
            self.filters_section.toggled.connect(self._save_settings)
            self.chainner_section.toggled.connect(self._save_settings)
            self.filters_edit.textChanged.connect(self._save_settings)
            self.texture_rules_edit.textChanged.connect(self._save_settings)
            self.chainner_override_edit.textChanged.connect(self._save_settings)
            self.chainner_chain_path_edit.textChanged.connect(self._refresh_chainner_chain_info)
            self.chainner_override_edit.textChanged.connect(self._refresh_chainner_chain_info)

        def _handle_main_tab_changed(self, index: int) -> None:
            self._save_settings()

        def _save_settings(self) -> None:
            if not self._settings_ready:
                return
            self.settings.setValue("appearance/theme", self.current_theme_key)
            self.settings.setValue("paths/original_dds_root", self.original_dds_edit.text())
            self.settings.setValue("paths/png_root", self.png_root_edit.text())
            self.settings.setValue("paths/dds_staging_root", self.dds_staging_root_edit.text())
            self.settings.setValue("paths/output_root", self.output_root_edit.text())
            self.settings.setValue("paths/texconv_path", self.texconv_path_edit.text())
            self.settings.setValue("archive/package_root", self.archive_package_root_edit.text())
            self.settings.setValue("archive/extract_root", self.archive_extract_root_edit.text())
            self.settings.setValue("archive/filter_text", self.archive_filter_edit.text())
            self.settings.setValue("archive/extension_filter", self._combo_value(self.archive_extension_filter_combo))
            self.settings.setValue("archive/package_filter_text", self.archive_package_filter_edit.text())
            self.settings.setValue("archive/structure_filter", self._current_archive_structure_filter_value())
            self.settings.setValue("archive/role_filter", self._combo_value(self.archive_role_filter_combo))
            self.settings.setValue("archive/min_size_kb", self.archive_min_size_spin.value())
            self.settings.setValue("archive/previewable_only", self.archive_previewable_only_checkbox.isChecked())
            self.settings.setValue("dds_output/format_mode", self._combo_value(self.dds_format_mode_combo))
            self.settings.setValue("dds_output/custom_format", self._combo_value(self.dds_custom_format_combo))
            self.settings.setValue("dds_output/size_mode", self._combo_value(self.dds_size_mode_combo))
            self.settings.setValue("dds_output/custom_width", self.dds_custom_width_spin.value())
            self.settings.setValue("dds_output/custom_height", self.dds_custom_height_spin.value())
            self.settings.setValue("dds_output/mip_mode", self._combo_value(self.dds_mip_mode_combo))
            self.settings.setValue("dds_output/custom_mip_count", self.dds_custom_mip_spin.value())
            self.settings.setValue("settings/dry_run", self.dry_run_checkbox.isChecked())
            self.settings.setValue("settings/enable_dds_staging", self.enable_dds_staging_checkbox.isChecked())
            self.settings.setValue("settings/enable_incremental_resume", self.enable_incremental_resume_checkbox.isChecked())
            self.settings.setValue("settings/csv_log_enabled", self.csv_log_enabled_checkbox.isChecked())
            self.settings.setValue("settings/csv_log_path", self.csv_log_path_edit.text())
            self.settings.setValue(
                "settings/allow_unique_basename_fallback",
                self.unique_basename_checkbox.isChecked(),
            )
            self.settings.setValue(
                "settings/overwrite_existing_dds",
                self.overwrite_existing_checkbox.isChecked(),
            )
            self.settings.setValue("settings/include_filters", self.filters_edit.toPlainText())
            self.settings.setValue("settings/texture_rules_text", self.texture_rules_edit.toPlainText())
            self.settings.setValue("chainner/enabled", self.enable_chainner_checkbox.isChecked())
            self.settings.setValue("chainner/exe_path", self.chainner_exe_path_edit.text())
            self.settings.setValue("chainner/chain_path", self.chainner_chain_path_edit.text())
            self.settings.setValue("chainner/override_json", self.chainner_override_edit.toPlainText())
            self.settings.setValue("ui/main_tab_index", self.main_tabs.currentIndex())
            self.settings.setValue("ui/compare_sync_pan", self.compare_sync_pan_checkbox.isChecked())
            self.settings.setValue("sections/setup_expanded", self.setup_section.toggle_button.isChecked())
            self.settings.setValue("sections/paths_expanded", self.paths_section.toggle_button.isChecked())
            self.settings.setValue("sections/settings_expanded", self.settings_section.toggle_button.isChecked())
            self.settings.setValue("sections/dds_output_expanded", self.dds_output_section.toggle_button.isChecked())
            self.settings.setValue("sections/filters_expanded", self.filters_section.toggle_button.isChecked())
            self.settings.setValue("sections/chainner_expanded", self.chainner_section.toggle_button.isChecked())
            self.settings.sync()

        def _load_settings(self) -> None:
            defaults = default_config()
            self.current_theme_key = str(self.settings.value("appearance/theme", self.current_theme_key or DEFAULT_UI_THEME))
            if self.current_theme_key not in UI_THEME_SCHEMES:
                self.current_theme_key = DEFAULT_UI_THEME
            self._set_theme_menu_selection(self.current_theme_key)
            self.original_dds_edit.setText(
                self.settings.value("paths/original_dds_root", defaults.original_dds_root)
            )
            self.png_root_edit.setText(self.settings.value("paths/png_root", defaults.png_root))
            self.dds_staging_root_edit.setText(self.settings.value("paths/dds_staging_root", defaults.dds_staging_root))
            self.output_root_edit.setText(self.settings.value("paths/output_root", defaults.output_root))
            self.texconv_path_edit.setText(self.settings.value("paths/texconv_path", defaults.texconv_path))
            self.archive_package_root_edit.setText(self.settings.value("archive/package_root", defaults.archive_package_root))
            self.archive_extract_root_edit.setText(self.settings.value("archive/extract_root", defaults.archive_extract_root))
            self.archive_filter_edit.setText(self.settings.value("archive/filter_text", defaults.archive_filter_text))
            self._set_combo_by_value(
                self.archive_extension_filter_combo,
                str(self.settings.value("archive/extension_filter", defaults.archive_extension_filter)),
            )
            self.archive_package_filter_edit.setText(
                self.settings.value("archive/package_filter_text", defaults.archive_package_filter_text)
            )
            self.archive_structure_filter_pending_value = str(
                self.settings.value("archive/structure_filter", defaults.archive_structure_filter)
            )
            self._set_combo_by_value(
                self.archive_role_filter_combo,
                str(self.settings.value("archive/role_filter", defaults.archive_role_filter)),
            )
            self.archive_min_size_spin.setValue(
                int(self.settings.value("archive/min_size_kb", defaults.archive_min_size_kb))
            )
            self.archive_previewable_only_checkbox.setChecked(
                self._read_bool("archive/previewable_only", defaults.archive_previewable_only)
            )
            size_mode_value = self.settings.value("dds_output/size_mode")
            if size_mode_value is None:
                old_keep_original_size = self._read_bool("settings/keep_original_size", False)
                size_mode_value = DDS_SIZE_MODE_ORIGINAL if old_keep_original_size else defaults.dds_size_mode
            self._set_combo_by_value(
                self.dds_format_mode_combo,
                str(self.settings.value("dds_output/format_mode", defaults.dds_format_mode)),
            )
            self._set_combo_by_value(
                self.dds_custom_format_combo,
                str(self.settings.value("dds_output/custom_format", defaults.dds_custom_format)),
            )
            self._set_combo_by_value(self.dds_size_mode_combo, str(size_mode_value))
            self.dds_custom_width_spin.setValue(
                int(self.settings.value("dds_output/custom_width", defaults.dds_custom_width))
            )
            self.dds_custom_height_spin.setValue(
                int(self.settings.value("dds_output/custom_height", defaults.dds_custom_height))
            )
            self._set_combo_by_value(
                self.dds_mip_mode_combo,
                str(self.settings.value("dds_output/mip_mode", defaults.dds_mip_mode)),
            )
            self.dds_custom_mip_spin.setValue(
                int(self.settings.value("dds_output/custom_mip_count", defaults.dds_custom_mip_count))
            )
            self.dry_run_checkbox.setChecked(self._read_bool("settings/dry_run", defaults.dry_run))
            self.enable_dds_staging_checkbox.setChecked(
                self._read_bool("settings/enable_dds_staging", defaults.enable_dds_staging)
            )
            self.enable_incremental_resume_checkbox.setChecked(
                self._read_bool("settings/enable_incremental_resume", defaults.enable_incremental_resume)
            )
            self.csv_log_enabled_checkbox.setChecked(
                self._read_bool("settings/csv_log_enabled", defaults.csv_log_enabled)
            )
            self.csv_log_path_edit.setText(
                self.settings.value("settings/csv_log_path", defaults.csv_log_path)
            )
            self.unique_basename_checkbox.setChecked(
                self._read_bool(
                    "settings/allow_unique_basename_fallback",
                    defaults.allow_unique_basename_fallback,
                )
            )
            self.overwrite_existing_checkbox.setChecked(
                self._read_bool("settings/overwrite_existing_dds", defaults.overwrite_existing_dds)
            )
            self.filters_edit.setPlainText(
                self.settings.value("settings/include_filters", defaults.include_filters)
            )
            self.texture_rules_edit.setPlainText(
                self.settings.value("settings/texture_rules_text", defaults.texture_rules_text)
            )
            self.enable_chainner_checkbox.setChecked(
                self._read_bool("chainner/enabled", defaults.enable_chainner)
            )
            self.chainner_exe_path_edit.setText(
                self.settings.value("chainner/exe_path", defaults.chainner_exe_path)
            )
            self.chainner_chain_path_edit.setText(
                self.settings.value("chainner/chain_path", defaults.chainner_chain_path)
            )
            self.chainner_override_edit.setPlainText(
                self.settings.value("chainner/override_json", defaults.chainner_override_json)
            )
            saved_main_tab = int(self.settings.value("ui/main_tab_index", 0))
            self.main_tabs.setCurrentIndex(max(0, min(saved_main_tab, self.main_tabs.count() - 1)))
            self.compare_sync_pan_checkbox.setChecked(self._read_bool("ui/compare_sync_pan", False))
            self.setup_section.set_expanded(self._read_bool("sections/setup_expanded", False))
            self.paths_section.set_expanded(self._read_bool("sections/paths_expanded", False))
            self.settings_section.set_expanded(self._read_bool("sections/settings_expanded", False))
            self.dds_output_section.set_expanded(self._read_bool("sections/dds_output_expanded", False))
            self.filters_section.set_expanded(self._read_bool("sections/filters_expanded", False))
            self.chainner_section.set_expanded(self._read_bool("sections/chainner_expanded", False))

        def _read_bool(self, key: str, default: bool) -> bool:
            value = self.settings.value(key, default)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def _apply_csv_log_enabled_state(self) -> None:
            enabled = self.csv_log_enabled_checkbox.isChecked()
            self.csv_log_path_edit.setEnabled(enabled)
            self.csv_log_browse_button.setEnabled(enabled)
            if enabled and not self.csv_log_path_edit.text().strip():
                self.csv_log_path_edit.setText(default_config().csv_log_path)

        def _apply_chainner_enabled_state(self) -> None:
            enabled = self.enable_chainner_checkbox.isChecked()
            self.chainner_exe_path_edit.setEnabled(enabled)
            self.chainner_chain_path_edit.setEnabled(enabled)
            self.chainner_override_edit.setEnabled(enabled)
            self.chainner_exe_browse_button.setEnabled(enabled)
            self.chainner_chain_browse_button.setEnabled(enabled)
            self.validate_chainner_button.setEnabled(enabled)

        def _refresh_chainner_chain_info(self) -> None:
            _analysis, text = self._resolve_chainner_analysis()
            self.chainner_chain_info_view.setPlainText(text)

        def _apply_dds_staging_enabled_state(self) -> None:
            enabled = self.enable_dds_staging_checkbox.isChecked()
            self.dds_staging_root_edit.setEnabled(enabled)
            self.dds_staging_browse_button.setEnabled(enabled)

        def _apply_dds_output_state(self) -> None:
            format_is_custom = self._combo_value(self.dds_format_mode_combo) == DDS_FORMAT_MODE_CUSTOM
            size_is_custom = self._combo_value(self.dds_size_mode_combo) == DDS_SIZE_MODE_CUSTOM
            mip_is_custom = self._combo_value(self.dds_mip_mode_combo) == DDS_MIP_MODE_CUSTOM
            self.dds_custom_format_label.setVisible(format_is_custom)
            self.dds_custom_format_combo.setVisible(format_is_custom)
            self.dds_custom_size_label.setVisible(size_is_custom)
            self.dds_custom_size_widget.setVisible(size_is_custom)
            self.dds_custom_mip_label.setVisible(mip_is_custom)
            self.dds_custom_mip_spin.setVisible(mip_is_custom)

        def _browse_directory(self, line_edit: QLineEdit, title: str) -> None:
            start_dir = self._pick_existing_directory(line_edit.text())
            selected = QFileDialog.getExistingDirectory(self, title, start_dir)
            if selected:
                line_edit.setText(selected)

        def _browse_file(self, line_edit: QLineEdit, title: str, file_filter: str, save_mode: bool = False) -> None:
            start_path = line_edit.text().strip() or str(Path.cwd())
            if save_mode:
                selected, _ = QFileDialog.getSaveFileName(self, title, start_path, file_filter)
            else:
                selected, _ = QFileDialog.getOpenFileName(self, title, start_path, file_filter)
            if selected:
                line_edit.setText(selected)

        def _pick_existing_directory(self, current_text: str) -> str:
            raw = current_text.strip()
            if not raw:
                return str(Path.cwd())
            path = Path(raw).expanduser()
            if path.is_file():
                return str(path.parent)
            if path.exists():
                return str(path)
            if path.parent.exists():
                return str(path.parent)
            return str(Path.cwd())

        def _browse_original_dds_root(self) -> None:
            self._browse_directory(self.original_dds_edit, "Select Original DDS Root")

        def _browse_png_root(self) -> None:
            self._browse_directory(self.png_root_edit, "Select PNG Root")

        def _browse_dds_staging_root(self) -> None:
            self._browse_directory(self.dds_staging_root_edit, "Select DDS Staging PNG Root")

        def _browse_output_root(self) -> None:
            self._browse_directory(self.output_root_edit, "Select Output Root")

        def _browse_texconv_path(self) -> None:
            self._browse_file(self.texconv_path_edit, "Select texconv.exe", "Executable (*.exe);;All files (*.*)")

        def _browse_csv_log_path(self) -> None:
            self._browse_file(
                self.csv_log_path_edit,
                "Select CSV Log Path",
                "CSV files (*.csv);;All files (*.*)",
                save_mode=True,
            )

        def _browse_chainner_exe_path(self) -> None:
            self._browse_file(
                self.chainner_exe_path_edit,
                "Select chaiNNer executable",
                "Executable (*.exe);;All files (*.*)",
            )

        def _browse_chainner_chain_path(self) -> None:
            self._browse_file(
                self.chainner_chain_path_edit,
                "Select chaiNNer chain",
                "chaiNNer chain (*.chn);;All files (*.*)",
            )

        def _browse_archive_package_root(self) -> None:
            self._browse_directory(self.archive_package_root_edit, "Select Archive Package Root")

        def _browse_archive_extract_root(self) -> None:
            self._browse_directory(self.archive_extract_root_edit, "Select Archive Extract Root")

        def autodetect_archive_package_root(self) -> None:
            if self._background_task_active():
                return

            def task(on_log: Callable[[str], None]) -> List[str]:
                on_log("Auto-detecting Crimson Desert archive package roots from known install locations...")
                roots = autodetect_archive_package_roots(on_log=on_log)
                return [str(path) for path in roots]

            def on_complete(result: object) -> None:
                candidates = [str(item) for item in result] if isinstance(result, list) else []
                if not candidates:
                    self.set_status_message(
                        "No valid Crimson Desert archive package root was auto-detected. Use Browse to set it manually.",
                        error=True,
                    )
                    return

                selected_path = candidates[0]
                if len(candidates) > 1:
                    selected_path, accepted = QInputDialog.getItem(
                        self,
                        "Select Package Root",
                        "Multiple Crimson Desert package roots were found. Choose one:",
                        candidates,
                        0,
                        False,
                    )
                    if not accepted or not selected_path:
                        self.set_status_message("Archive package root auto-detect cancelled.")
                        return

                self.archive_package_root_edit.setText(selected_path)
                self.main_tabs.setCurrentWidget(self.archive_browser_tab)
                self.set_status_message(f"Auto-detected archive package root: {selected_path}")
                self.append_log(f"Using detected archive package root: {selected_path}")

            self._run_utility_task(
                status_message="Auto-detecting archive package root...",
                task=task,
                on_complete=on_complete,
            )

        def _suggest_workspace_base_dir(self) -> str:
            common = common_workspace_root_from_config(self.collect_config())
            if common is not None:
                return str(common)
            return str(Path.cwd())

        def _run_utility_task(
            self,
            *,
            status_message: str,
            task: Callable[[Callable[[str], None]], object],
            on_complete: Optional[Callable[[object], None]] = None,
        ) -> None:
            if self._background_task_active():
                return

            self.set_status_message(status_message)
            self.append_log(status_message)

            worker = UtilityWorker(task)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.completed.connect(self._handle_utility_completed)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.utility_worker = worker
            self.worker_thread = thread
            self._utility_completion_handler = on_complete
            self.set_busy(True, build_mode=False)
            thread.start()

        def _directory_has_contents(self, path: Path) -> bool:
            try:
                if not path.exists() or not path.is_dir():
                    return False
                next(path.iterdir())
                return True
            except StopIteration:
                return False
            except OSError:
                return False

        def _prompt_clear_directory_before_start(self, label: str, path: Path) -> Optional[bool]:
            if not self._directory_has_contents(path):
                return False

            box = QMessageBox(self)
            box.setWindowTitle(f"{label} Not Empty")
            box.setIcon(QMessageBox.Warning)
            box.setText(f"{label} already contains files or folders.")
            box.setInformativeText(
                f"{path}\n\n"
                "Clear it before starting?\n"
                "Choose Keep Existing to leave the current contents in place, or Cancel to stop."
            )
            clear_button = box.addButton("Clear Folder", QMessageBox.DestructiveRole)
            keep_button = box.addButton("Keep Existing", QMessageBox.AcceptRole)
            cancel_button = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(keep_button)
            box.exec()

            clicked = box.clickedButton()
            if clicked == cancel_button:
                return None
            return clicked == clear_button

        def _prepare_workflow_output_roots_for_start(
            self,
            config: AppConfig,
            *,
            include_output_root: bool,
        ) -> bool:
            if config.dry_run:
                return True

            targets: List[Tuple[str, Path]] = []
            if config.enable_chainner or config.enable_dds_staging:
                png_root_text = config.png_root.strip()
                if png_root_text:
                    targets.append(("PNG root", Path(png_root_text).expanduser()))
            if include_output_root:
                output_root_text = config.output_root.strip()
                if output_root_text:
                    targets.append(("Output root", Path(output_root_text).expanduser()))

            seen_paths: set[str] = set()
            unique_targets: List[Tuple[str, Path]] = []
            for label, path in targets:
                try:
                    normalized_key = str(path.resolve())
                except OSError:
                    normalized_key = str(path)
                if normalized_key in seen_paths:
                    continue
                seen_paths.add(normalized_key)
                unique_targets.append((label, path))

            for label, path in unique_targets:
                decision = self._prompt_clear_directory_before_start(label, path)
                if decision is None:
                    self.set_status_message("Start cancelled.")
                    self.append_log(f"Start cancelled while reviewing {label.lower()} contents.")
                    return False
                if not decision:
                    self.append_log(f"Keeping existing contents in {label}: {path}")
                    continue
                path.mkdir(parents=True, exist_ok=True)
                clear_directory_contents(path)
                self.append_log(f"Cleared {label} before start: {path}")

            return True

        def initialize_workspace(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select Workspace Folder",
                self._suggest_workspace_base_dir(),
            )
            if not selected:
                return

            base_dir = Path(selected)

            def task(on_log: Callable[[str], None]) -> Dict[str, str]:
                on_log(f"Creating workspace structure under {base_dir}")
                paths = create_workspace_structure(base_dir)
                return {key: str(value) for key, value in paths.items()}

            def on_complete(result: object) -> None:
                if not isinstance(result, dict):
                    return
                self.original_dds_edit.setText(str(result["original_dds_root"]))
                self.png_root_edit.setText(str(result["png_root"]))
                if not self.dds_staging_root_edit.text().strip():
                    self.dds_staging_root_edit.setText(str(result["dds_staging_root"]))
                self.output_root_edit.setText(str(result["output_root"]))
                if not self.archive_extract_root_edit.text().strip():
                    self.archive_extract_root_edit.setText(str(result["archive_extract_root"]))
                if not self.texconv_path_edit.text().strip():
                    self.texconv_path_edit.setText(str(result["texconv_path"]))
                if not self.csv_log_path_edit.text().strip():
                    self.csv_log_path_edit.setText(str(result["csv_log_path"]))
                if not self.chainner_exe_path_edit.text().strip():
                    self.chainner_exe_path_edit.setText(str(result["chainner_exe_path"]))
                self.set_status_message(f"Workspace initialized at {base_dir}")
                self.append_log("Workspace initialization complete.")

            self._run_utility_task(
                status_message="Initializing workspace...",
                task=task,
                on_complete=on_complete,
            )

        def create_missing_folders(self) -> None:
            config = self.collect_config()

            def task(on_log: Callable[[str], None]) -> List[str]:
                created = create_missing_directories_for_config(config)
                if created:
                    for path in created:
                        on_log(f"Created folder: {path}")
                else:
                    on_log("No folders needed to be created.")
                return [str(path) for path in created]

            def on_complete(result: object) -> None:
                created = result if isinstance(result, list) else []
                if created:
                    self.set_status_message(f"Created {len(created)} folder(s).")
                else:
                    self.set_status_message("All requested folders already existed.")

            self._run_utility_task(
                status_message="Creating missing folders...",
                task=task,
                on_complete=on_complete,
            )

        def download_chainner(self) -> None:
            default_dir = self.chainner_exe_path_edit.text().strip()
            start_dir = self._pick_existing_directory(default_dir) if default_dir else self._suggest_workspace_base_dir()
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select chaiNNer Download Folder",
                start_dir,
            )
            if not selected:
                return

            install_dir = Path(selected)

            def task(on_log: Callable[[str], None]) -> str:
                on_log("Resolving the latest Windows portable chaiNNer package from the official download page...")
                exe_path = download_chainner_portable(install_dir, on_log=on_log)
                return str(exe_path)

            def on_complete(result: object) -> None:
                if isinstance(result, str):
                    self.chainner_exe_path_edit.setText(result)
                    self.set_status_message(f"chaiNNer downloaded to {result}")
                    self.append_log(f"chaiNNer executable ready: {result}")

            self._run_utility_task(
                status_message="Downloading chaiNNer...",
                task=task,
                on_complete=on_complete,
            )

        def download_texconv(self) -> None:
            current_text = self.texconv_path_edit.text().strip()
            if current_text:
                start_path = current_text
            else:
                suggested = suggested_workspace_paths(Path(self._suggest_workspace_base_dir()))
                start_path = str(suggested["texconv_path"])

            selected, _ = QFileDialog.getSaveFileName(
                self,
                "Save texconv.exe As",
                start_path,
                "Executable (*.exe);;All files (*.*)",
            )
            if not selected:
                return

            destination = Path(selected)

            def task(on_log: Callable[[str], None]) -> str:
                on_log("Resolving the latest official texconv.exe release asset...")
                texconv_path = download_texconv_executable(destination, on_log=on_log)
                return str(texconv_path)

            def on_complete(result: object) -> None:
                if isinstance(result, str):
                    self.texconv_path_edit.setText(result)
                    self.set_status_message(f"texconv.exe downloaded to {result}")
                    self.append_log(f"texconv.exe ready: {result}")

            self._run_utility_task(
                status_message="Downloading texconv.exe...",
                task=task,
                on_complete=on_complete,
            )

        def _suggest_archive_extract_root(self) -> Path:
            text = self.archive_extract_root_edit.text().strip()
            if text:
                return Path(text).expanduser()
            common = common_workspace_root_from_config(self.collect_config())
            if common is not None:
                return suggested_workspace_paths(common).get("archive_extract_root", common / "archive_extract")
            return Path.cwd() / "archive_extract"

        def scan_archives(self, force_refresh: bool = False) -> None:
            if self._background_task_active():
                return
            package_root_text = self.archive_package_root_edit.text().strip()
            if not package_root_text:
                self.set_status_message("Set an archive package root first.", error=True)
                return

            package_root = Path(package_root_text).expanduser()
            self.main_tabs.setCurrentWidget(self.archive_browser_tab)
            self.archive_scan_progress_label.setText("Preparing archive refresh..." if force_refresh else "Preparing archive scan / cache load...")
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Working...")
            self.set_status_message("Refreshing archives..." if force_refresh else "Loading archives...")
            self.append_log("Refreshing archives..." if force_refresh else "Loading archives...")
            self.clear_archive_scan_log()
            self.append_archive_log(
                "Starting archive refresh." if force_refresh else "Starting archive scan (cache-aware)."
            )

            worker = ArchiveScanWorker(
                package_root,
                self.archive_cache_root,
                force_refresh=force_refresh,
                filter_text=self.archive_filter_edit.text().strip(),
                extension_filter=self._combo_value(self.archive_extension_filter_combo),
                package_filter_text=self.archive_package_filter_edit.text().strip(),
                structure_filter=self._current_archive_structure_filter_value(),
                role_filter=self._combo_value(self.archive_role_filter_combo),
                min_size_kb=self.archive_min_size_spin.value(),
                previewable_only=self.archive_previewable_only_checkbox.isChecked(),
            )
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.log_message.connect(self.append_archive_log)
            worker.progress_changed.connect(self._handle_archive_scan_progress)
            worker.completed.connect(self._handle_archive_scan_complete)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.archive_scan_worker = worker
            self.worker_thread = thread
            self.set_busy(True, build_mode=False)
            thread.start()

        def _handle_archive_scan_progress(self, current: int, total: int, detail: str) -> None:
            self.archive_scan_progress_label.setText(detail)
            if total > 0:
                completed_value = min(max(current, 0), total)
                display_value = min(completed_value + 1, total) if detail.startswith("Parsing ") else completed_value
                self.archive_scan_progress_bar.setRange(0, total)
                self.archive_scan_progress_bar.setValue(completed_value)
                self.archive_scan_progress_bar.setFormat(f"{display_value} / {total}")
            else:
                self.archive_scan_progress_bar.setRange(0, 0)
                self.archive_scan_progress_bar.setFormat("Working...")
            self.set_status_message(detail)

        def _handle_archive_scan_complete(self, result: object) -> None:
            payload = result if isinstance(result, dict) else {}
            self.archive_entries = payload.get("entries", []) if isinstance(payload.get("entries"), list) else []
            self.text_search_tab.set_archive_entries(self.archive_entries, self.archive_package_root_edit.text().strip())
            browser_state = payload.get("browser_state") if isinstance(payload.get("browser_state"), dict) else {}
            self.archive_structure_filter_children = (
                browser_state.get("structure_children", {})
                if isinstance(browser_state.get("structure_children"), dict)
                else {}
            )
            self.archive_filtered_entries = (
                browser_state.get("filtered_entries", [])
                if isinstance(browser_state.get("filtered_entries"), list)
                else []
            )
            self.archive_tree_child_folders = (
                browser_state.get("tree_child_folders", {})
                if isinstance(browser_state.get("tree_child_folders"), dict)
                else {}
            )
            self.archive_tree_direct_files = (
                browser_state.get("tree_direct_files", {})
                if isinstance(browser_state.get("tree_direct_files"), dict)
                else {}
            )
            self.archive_tree_folder_entry_indexes = (
                browser_state.get("tree_folder_entry_indexes", {})
                if isinstance(browser_state.get("tree_folder_entry_indexes"), dict)
                else {}
            )
            self.archive_tree_items_by_folder_key = {}
            self.archive_filtered_dds_count = int(browser_state.get("dds_count", 0))
            self.archive_filters_dirty = False
            self._update_archive_filter_button_state()
            source = str(payload.get("source", "scan"))
            cache_path_text = str(payload.get("cache_path", "")).strip()
            self.archive_scan_progress_label.setText("Rendering archive browser view...")
            self.main_tabs.setCurrentWidget(self.archive_browser_tab)
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Rendering...")
            self.set_status_message("Rendering archive browser view...")
            self.append_archive_log("Rendering archive browser view...")
            QTimer.singleShot(
                0,
                lambda source=source, cache_path_text=cache_path_text: self._finalize_archive_scan_complete(
                    source,
                    cache_path_text,
                ),
            )

        def _finalize_archive_scan_complete(self, source: str, cache_path_text: str) -> None:
            self._rebuild_archive_structure_filter_controls()
            self._populate_archive_tree(rebuild_index=False)
            completion_text = (
                f"Loaded {len(self.archive_entries):,} archive entries from cache."
                if source == "cache"
                else f"Archive scan complete. Found {len(self.archive_entries):,} entries."
            )
            self.archive_scan_progress_label.setText(completion_text)
            self.archive_scan_progress_bar.setRange(0, 1)
            self.archive_scan_progress_bar.setValue(1)
            self.archive_scan_progress_bar.setFormat("Ready")
            self.set_status_message(completion_text)
            self.append_archive_log(completion_text)
            if cache_path_text and source == "scan":
                self.append_archive_log(f"Archive cache ready: {cache_path_text}")

        def _mark_archive_filters_dirty(self) -> None:
            self.archive_filters_dirty = True
            self._update_archive_filter_button_state()

        def _clear_archive_structure_filter_widgets(self) -> None:
            while self.archive_structure_filter_layout.count():
                item = self.archive_structure_filter_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        def _current_archive_structure_filter_value(self) -> str:
            if not self.archive_structure_filter_combos:
                return normalize_archive_structure_filter_value(self.archive_structure_filter_pending_value)
            selected_value = ""
            for combo in self.archive_structure_filter_combos:
                value = normalize_archive_structure_filter_value(self._combo_value(combo))
                if not value or value == selected_value:
                    break
                selected_value = value
            return selected_value

        def _set_archive_structure_filter_enabled(self, enabled: bool) -> None:
            for combo in self.archive_structure_filter_combos:
                combo.setEnabled(enabled)

        def _format_archive_structure_combo_label(self, value: str, count: int) -> str:
            leaf = value.rsplit("/", 1)[-1]
            return f"{leaf}/ ({count:,})"

        def _rebuild_archive_structure_filter_controls(
            self,
            selected_value: Optional[str] = None,
            *,
            rebuild_children: bool = False,
        ) -> None:
            preferred_value = normalize_archive_structure_filter_value(
                selected_value
                if selected_value is not None
                else (self._current_archive_structure_filter_value() or self.archive_structure_filter_pending_value)
            )
            if rebuild_children or (not self.archive_structure_filter_children and self.archive_entries):
                self.archive_structure_filter_children = build_archive_structure_children_map(self.archive_entries)
            self.rebuilding_archive_structure_filters = True
            self._clear_archive_structure_filter_widgets()
            self.archive_structure_filter_combos = []

            if not self.archive_structure_filter_children:
                empty_label = QLabel("Scan archives to load folder filters.")
                empty_label.setObjectName("HintLabel")
                self.archive_structure_filter_layout.addWidget(empty_label)
                self.archive_structure_filter_layout.addStretch(1)
                self.archive_structure_filter_pending_value = preferred_value
                self.rebuilding_archive_structure_filters = False
                return

            segments = preferred_value.split("/") if preferred_value else []
            parent = ""
            level = 0
            while True:
                child_options = self.archive_structure_filter_children.get(parent, [])
                if not child_options:
                    break

                combo = QComboBox()
                combo.setMaxVisibleItems(30)
                combo.setMinimumWidth(170)
                if parent == "":
                    self._add_combo_choice(combo, "All packages", "")
                else:
                    self._add_combo_choice(combo, f"All in {parent.rsplit('/', 1)[-1]}/", parent)
                for child_value, count in child_options:
                    self._add_combo_choice(combo, self._format_archive_structure_combo_label(child_value, count), child_value)

                selected_child_value = ""
                if len(segments) > level:
                    candidate = "/".join(segments[: level + 1])
                    if combo.findData(candidate) >= 0:
                        selected_child_value = candidate
                self._set_combo_by_value(combo, selected_child_value if selected_child_value else (parent if parent else ""))
                combo.currentIndexChanged.connect(
                    lambda _index, level=level: self._handle_archive_structure_combo_changed(level)
                )
                combo.setEnabled(self.worker_thread is None)
                self.archive_structure_filter_layout.addWidget(combo)
                self.archive_structure_filter_combos.append(combo)

                if not selected_child_value:
                    break
                parent = selected_child_value
                level += 1

            self.archive_structure_filter_layout.addStretch(1)
            self.archive_structure_filter_pending_value = self._current_archive_structure_filter_value() or preferred_value
            self.rebuilding_archive_structure_filters = False

        def _handle_archive_structure_combo_changed(self, _level: int) -> None:
            if self.rebuilding_archive_structure_filters:
                return
            self.archive_structure_filter_pending_value = self._current_archive_structure_filter_value()
            self._rebuild_archive_structure_filter_controls(self.archive_structure_filter_pending_value)
            self._save_settings()
            self._mark_archive_filters_dirty()

        def _update_archive_filter_button_state(self) -> None:
            button_label = "Apply Filters*" if self.archive_filters_dirty else "Apply Filters"
            self.archive_filter_apply_button.setText(button_label)
            can_apply = self.worker_thread is None and self.archive_filters_dirty
            self.archive_filter_apply_button.setEnabled(can_apply)
            self.archive_filter_clear_button.setEnabled(self.worker_thread is None)

        def _clear_archive_filters(self) -> None:
            self.archive_filter_edit.clear()
            self._set_combo_by_value(self.archive_extension_filter_combo, ARCHIVE_EXTENSION_FILTER)
            self.archive_package_filter_edit.clear()
            self.archive_structure_filter_pending_value = ARCHIVE_STRUCTURE_FILTER
            self._rebuild_archive_structure_filter_controls(ARCHIVE_STRUCTURE_FILTER)
            self._set_combo_by_value(self.archive_role_filter_combo, ARCHIVE_ROLE_FILTER)
            self.archive_min_size_spin.setValue(ARCHIVE_MIN_SIZE_KB)
            self.archive_previewable_only_checkbox.setChecked(ARCHIVE_PREVIEWABLE_ONLY)
            self._save_settings()
            self._apply_archive_filter()

        def _apply_archive_filter(self) -> None:
            current_entry = self._current_archive_entry()
            current_entry_path = current_entry.path if current_entry is not None else ""
            filter_text = self.archive_filter_edit.text().strip()
            extension_filter = self._combo_value(self.archive_extension_filter_combo)
            package_filter_text = self.archive_package_filter_edit.text().strip()
            structure_filter = self._current_archive_structure_filter_value()
            self.archive_structure_filter_pending_value = structure_filter
            role_filter = self._combo_value(self.archive_role_filter_combo)
            min_size_kb = self.archive_min_size_spin.value()
            previewable_only = self.archive_previewable_only_checkbox.isChecked()
            self.archive_filtered_entries = filter_archive_entries(
                self.archive_entries,
                filter_text=filter_text,
                extension_filter=extension_filter,
                package_filter_text=package_filter_text,
                structure_filter=structure_filter,
                role_filter=role_filter,
                min_size_kb=min_size_kb,
                previewable_only=previewable_only,
            )
            self.archive_filtered_dds_count = count_archive_entries_with_extension(
                self.archive_filtered_entries,
                ".dds",
            )
            self.archive_filters_dirty = False
            self._update_archive_filter_button_state()
            self._populate_archive_tree(current_entry_path)

        def _archive_tree_item_kind(self, item: Optional[QTreeWidgetItem]) -> str:
            if item is None:
                return ""
            raw = item.data(0, Qt.UserRole)
            return raw if isinstance(raw, str) else ""

        def _archive_tree_item_value(self, item: Optional[QTreeWidgetItem]) -> object:
            if item is None:
                return None
            return item.data(0, Qt.UserRole + 1)

        def _archive_tree_folder_key(self, item: Optional[QTreeWidgetItem]) -> Tuple[str, ...]:
            raw = self._archive_tree_item_value(item)
            return raw if isinstance(raw, tuple) else ()

        def _rebuild_archive_tree_index(self) -> None:
            (
                self.archive_tree_child_folders,
                self.archive_tree_direct_files,
                self.archive_tree_folder_entry_indexes,
            ) = build_archive_tree_index(self.archive_filtered_entries)
            self.archive_tree_items_by_folder_key = {}

        def _create_archive_folder_item(
            self,
            parent: QTreeWidget | QTreeWidgetItem,
            folder_key: Tuple[str, ...],
        ) -> QTreeWidgetItem:
            item = QTreeWidgetItem(parent)
            item.setText(0, folder_key[-1] if folder_key else "(root)")
            item.setText(1, "Folder")
            item.setData(0, Qt.UserRole, "folder")
            item.setData(0, Qt.UserRole + 1, folder_key)
            item.setData(0, Qt.UserRole + 2, False)
            item.setToolTip(0, "/".join(folder_key))
            if self.archive_tree_child_folders.get(folder_key) or self.archive_tree_direct_files.get(folder_key):
                QTreeWidgetItem(item, [""])
            self.archive_tree_items_by_folder_key[folder_key] = item
            return item

        def _create_archive_file_item(
            self,
            parent: QTreeWidget | QTreeWidgetItem,
            entry_index: int,
        ) -> QTreeWidgetItem:
            entry = self.archive_filtered_entries[entry_index]
            normalized_parts = tuple(part for part in PurePosixPath(entry.path.replace("\\", "/")).parts if part)
            item = QTreeWidgetItem(parent)
            item.setText(0, normalized_parts[-1] if normalized_parts else entry.basename)
            item.setText(1, entry.extension or "-")
            item.setText(2, format_byte_size(entry.orig_size))
            item.setText(3, format_byte_size(entry.comp_size))
            item.setText(4, entry.compression_label)
            item.setText(5, entry.package_label)
            item.setData(0, Qt.UserRole, "file")
            item.setData(0, Qt.UserRole + 1, entry_index)
            item.setToolTip(0, entry.path)
            return item

        def _ensure_archive_folder_item_populated(self, item: Optional[QTreeWidgetItem]) -> None:
            if item is None or self._archive_tree_item_kind(item) != "folder":
                return
            if bool(item.data(0, Qt.UserRole + 2)):
                return

            folder_key = self._archive_tree_folder_key(item)
            item.takeChildren()
            for _leaf, child_key in self.archive_tree_child_folders.get(folder_key, []):
                self._create_archive_folder_item(item, child_key)
            for entry_index in self.archive_tree_direct_files.get(folder_key, []):
                self._create_archive_file_item(item, entry_index)
            item.setData(0, Qt.UserRole + 2, True)

        def _handle_archive_item_expanded(self, item: QTreeWidgetItem) -> None:
            self._ensure_archive_folder_item_populated(item)

        def _find_archive_file_item(
            self,
            parent: QTreeWidget | QTreeWidgetItem,
            entry_index: int,
        ) -> Optional[QTreeWidgetItem]:
            child_count = parent.topLevelItemCount() if isinstance(parent, QTreeWidget) else parent.childCount()
            for child_index in range(child_count):
                child = parent.topLevelItem(child_index) if isinstance(parent, QTreeWidget) else parent.child(child_index)
                if (
                    child is not None
                    and self._archive_tree_item_kind(child) == "file"
                    and child.data(0, Qt.UserRole + 1) == entry_index
                ):
                    return child
            return None

        def _select_archive_tree_entry(self, entry_index: int) -> Optional[QTreeWidgetItem]:
            if not (0 <= entry_index < len(self.archive_filtered_entries)):
                return None

            entry = self.archive_filtered_entries[entry_index]
            parts = tuple(part for part in PurePosixPath(entry.path.replace("\\", "/")).parts if part)
            folder_key = parts[:-1]
            parent_item: Optional[QTreeWidgetItem] = None
            current_parent_key: Tuple[str, ...] = ()

            for depth in range(len(folder_key)):
                current_folder_key = folder_key[: depth + 1]
                if parent_item is None:
                    folder_item = self.archive_tree_items_by_folder_key.get(current_folder_key)
                else:
                    self._ensure_archive_folder_item_populated(parent_item)
                    folder_item = self.archive_tree_items_by_folder_key.get(current_folder_key)
                if folder_item is None:
                    return None
                folder_item.setExpanded(True)
                self._ensure_archive_folder_item_populated(folder_item)
                parent_item = folder_item
                current_parent_key = current_folder_key

            container: QTreeWidget | QTreeWidgetItem = parent_item if parent_item is not None else self.archive_tree
            target_item = self._find_archive_file_item(container, entry_index)
            if target_item is None and parent_item is None and current_parent_key == ():
                for top_level_index in self.archive_tree_direct_files.get((), []):
                    if top_level_index == entry_index:
                        target_item = self._find_archive_file_item(self.archive_tree, entry_index)
                        break
            return target_item

        def _populate_archive_tree(self, preferred_path: str = "", *, rebuild_index: bool = True) -> None:
            if rebuild_index:
                self._rebuild_archive_tree_index()
            self.archive_tree.blockSignals(True)
            self.archive_tree.clear()
            self.archive_tree_items_by_folder_key = {}
            for _leaf, child_key in self.archive_tree_child_folders.get((), []):
                self._create_archive_folder_item(self.archive_tree, child_key)
            for entry_index in self.archive_tree_direct_files.get((), []):
                self._create_archive_file_item(self.archive_tree, entry_index)
            self.archive_tree.blockSignals(False)

            total_entries = len(self.archive_entries)
            filtered_entries = len(self.archive_filtered_entries)
            self.archive_stats_label.setText(
                f"{filtered_entries:,} shown / {total_entries:,} total entries. DDS in current view: {self.archive_filtered_dds_count:,}."
            )

            preferred_index = next(
                (index for index, entry in enumerate(self.archive_filtered_entries) if preferred_path and entry.path == preferred_path),
                -1,
            )
            target_item = self._select_archive_tree_entry(preferred_index) if preferred_index >= 0 else None
            if target_item is None and self.archive_tree.topLevelItemCount() > 0:
                target_item = self.archive_tree.topLevelItem(0)
            if target_item is not None:
                self.archive_tree.setCurrentItem(target_item)
                target_item.setSelected(True)
            else:
                self._clear_archive_preview("No archive entries match the current filter.")
            self._update_archive_selection_state()

        def _collect_archive_entries_from_item(
            self,
            item: Optional[QTreeWidgetItem],
            collected_indexes: set[int],
        ) -> None:
            if item is None:
                return
            kind = self._archive_tree_item_kind(item)
            value = self._archive_tree_item_value(item)
            if kind == "file" and isinstance(value, int) and 0 <= value < len(self.archive_filtered_entries):
                collected_indexes.add(value)
                return
            if kind == "folder":
                folder_key = value if isinstance(value, tuple) else ()
                collected_indexes.update(self.archive_tree_folder_entry_indexes.get(folder_key, []))

        def _selected_archive_entries(self) -> List[ArchiveEntry]:
            collected_indexes: set[int] = set()
            for item in self.archive_tree.selectedItems():
                self._collect_archive_entries_from_item(item, collected_indexes)
            return [self.archive_filtered_entries[index] for index in sorted(collected_indexes)]

        def _current_archive_entry(self) -> Optional[ArchiveEntry]:
            item = self.archive_tree.currentItem()
            if item is None:
                return None
            kind = self._archive_tree_item_kind(item)
            value = self._archive_tree_item_value(item)
            if kind == "file" and isinstance(value, int) and 0 <= value < len(self.archive_filtered_entries):
                return self.archive_filtered_entries[value]
            return None

        def _clear_archive_preview(self, message: str) -> None:
            self.archive_preview_request_id += 1
            self.pending_archive_preview_request = None
            self.current_archive_preview_result = None
            self.archive_preview_showing_loose = False
            self.archive_preview_title_label.setText("Select an archive file")
            self.archive_preview_meta_label.setText(message)
            self.archive_preview_warning_badge.clear()
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_warning_label.clear()
            self.archive_preview_warning_label.setVisible(False)
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_loose_toggle_button.setEnabled(False)
            self.archive_preview_label.clear_preview(message)
            self.archive_preview_text_edit.clear()
            self.archive_preview_info_edit.setPlainText(message)
            self.archive_preview_details_edit.clear()
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self._set_archive_preview_image_controls_enabled(False)

        def _show_archive_folder_preview(self, item: Optional[QTreeWidgetItem]) -> None:
            self.archive_preview_request_id += 1
            self.pending_archive_preview_request = None
            collected_indexes: set[int] = set()
            self._collect_archive_entries_from_item(item, collected_indexes)
            entries = [self.archive_filtered_entries[index] for index in sorted(collected_indexes)]
            folder_path = item.toolTip(0) if item is not None else ""
            total_original = sum(entry.orig_size for entry in entries)
            total_stored = sum(entry.comp_size for entry in entries)
            preview_text = "\n".join(
                [
                    f"Folder: {folder_path or '(root)'}",
                    f"Entries: {len(entries):,}",
                    f"Total original size: {format_byte_size(total_original)}",
                    f"Total stored size: {format_byte_size(total_stored)}",
                    "",
                    "Select a file to preview its contents.",
                ]
            )
            self.archive_preview_title_label.setText(item.text(0) if item is not None else "Select an archive file")
            self.archive_preview_meta_label.setText(f"Folder | {len(entries):,} entries")
            self.archive_preview_warning_badge.clear()
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_warning_label.clear()
            self.archive_preview_warning_label.setVisible(False)
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_loose_toggle_button.setEnabled(False)
            self.archive_preview_info_edit.setPlainText(preview_text)
            self.archive_preview_details_edit.setPlainText(preview_text)
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self.archive_preview_label.clear_preview("Select a file to preview it here.")
            self._set_archive_preview_image_controls_enabled(False)

        def _handle_archive_current_item_change(
            self,
            current: Optional[QTreeWidgetItem],
            previous: Optional[QTreeWidgetItem],
        ) -> None:
            del previous
            if current is None:
                self._clear_archive_preview("Select an archive file to preview it here.")
                return
            if self._archive_tree_item_kind(current) == "folder":
                self._ensure_archive_folder_item_populated(current)
                self._show_archive_folder_preview(current)
            else:
                entry = self._current_archive_entry()
                if entry is not None:
                    self._render_archive_preview(entry)
                else:
                    self._show_archive_folder_preview(current)
            self._update_archive_selection_state()

        def _render_archive_preview(self, entry: Optional[ArchiveEntry]) -> None:
            texconv_text = self.texconv_path_edit.text().strip()
            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            loose_search_roots = self._collect_archive_preview_loose_roots()
            request_id = self.archive_preview_request_id + 1
            self.archive_preview_request_id = request_id
            self.archive_preview_title_label.setText(entry.basename if entry is not None else "Select an archive file")
            self.archive_preview_meta_label.setText("Loading preview...")
            self.archive_preview_warning_badge.clear()
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_warning_label.clear()
            self.archive_preview_warning_label.setVisible(False)
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_loose_toggle_button.setEnabled(False)
            self.archive_preview_details_edit.setPlainText("Preparing archive preview...")
            self.archive_preview_info_edit.setPlainText("Preparing archive preview...")

            if self.archive_preview_thread is not None:
                self.pending_archive_preview_request = (request_id, entry)
                return

            self._start_archive_preview_worker(request_id, texconv_path, entry, loose_search_roots)

        def _start_archive_preview_worker(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            entry: Optional[ArchiveEntry],
            loose_search_roots: Sequence[Path],
        ) -> None:
            worker = ArchivePreviewWorker(request_id, texconv_path, entry, loose_search_roots)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.completed.connect(self._handle_archive_preview_ready)
            worker.error.connect(self._handle_archive_preview_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_archive_preview_refs)

            self.archive_preview_worker = worker
            self.archive_preview_thread = thread
            thread.start()

        def _handle_archive_preview_ready(self, request_id: int, payload: object) -> None:
            if request_id != self.archive_preview_request_id:
                return
            if isinstance(payload, ArchivePreviewResult):
                self._apply_archive_preview_result(payload)

        def _handle_archive_preview_error(self, request_id: int, message: str) -> None:
            if request_id != self.archive_preview_request_id:
                return
            self._clear_archive_preview(f"Preview failed: {message}")

        def _collect_archive_preview_loose_roots(self) -> List[Path]:
            roots: List[Path] = []
            seen: set[str] = set()
            for raw in (
                self.original_dds_edit.text().strip(),
                self.archive_extract_root_edit.text().strip(),
                self.output_root_edit.text().strip(),
            ):
                if not raw:
                    continue
                try:
                    path = Path(raw).expanduser().resolve()
                except OSError:
                    continue
                lowered = str(path).lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                roots.append(path)
            return roots

        def _update_archive_preview_warning_controls(
            self,
            *,
            badge_text: str,
            warning_text: str,
            can_toggle_loose: bool,
        ) -> None:
            self.archive_preview_warning_badge.setText(badge_text)
            self.archive_preview_warning_badge.setVisible(bool(badge_text))
            self.archive_preview_warning_label.setText(warning_text)
            self.archive_preview_warning_label.setVisible(bool(warning_text))
            self.archive_preview_loose_toggle_button.setVisible(can_toggle_loose)
            self.archive_preview_loose_toggle_button.setEnabled(can_toggle_loose)
            if can_toggle_loose:
                self.archive_preview_loose_toggle_button.setText(
                    "Show Archive Preview" if self.archive_preview_showing_loose else "Show Loose File"
                )

        def _show_archive_preview_result(self, result: ArchivePreviewResult, *, use_loose: bool) -> None:
            self.archive_preview_showing_loose = use_loose and bool(result.loose_file_path)
            if self.archive_preview_showing_loose:
                title = result.loose_preview_title or result.title or "Archive Preview"
                metadata_summary = result.loose_preview_metadata_summary or result.metadata_summary or "Preview ready."
                detail_text = result.loose_preview_detail_text or result.detail_text or metadata_summary
                warning_badge = "Loose File Preview"
                warning_text = (
                    f"Using external loose-file preview from {result.loose_file_path}."
                    if result.loose_file_path
                    else ""
                )
                preview_image_path = result.loose_preview_image_path
                preferred_view = "image" if preview_image_path else "info"
            else:
                title = result.title or "Archive Preview"
                metadata_summary = result.metadata_summary or "Preview ready."
                detail_text = result.detail_text or metadata_summary
                warning_badge = result.warning_badge
                warning_text = result.warning_text
                preview_image_path = result.preview_image_path
                preferred_view = result.preferred_view

            self.archive_preview_title_label.setText(title)
            self.archive_preview_meta_label.setText(metadata_summary)
            self.archive_preview_details_edit.setPlainText(detail_text)
            self._update_archive_preview_warning_controls(
                badge_text=warning_badge,
                warning_text=warning_text,
                can_toggle_loose=bool(result.loose_file_path),
            )

            if preferred_view == "image" and preview_image_path:
                pixmap = QPixmap(preview_image_path)
                if not pixmap.isNull():
                    self.archive_preview_label.set_preview_pixmap(pixmap, title or "Preview image")
                    self.archive_preview_stack.setCurrentWidget(self.archive_preview_scroll)
                    self.archive_preview_tabs.setCurrentIndex(0)
                    self._set_archive_preview_image_controls_enabled(True)
                    self._apply_archive_preview_zoom()
                    return

            if preferred_view == "text":
                self.archive_preview_text_edit.setPlainText(result.preview_text or "No text preview available.")
                self.archive_preview_stack.setCurrentWidget(self.archive_preview_text_edit)
                self.archive_preview_tabs.setCurrentIndex(0)
                self.archive_preview_label.clear_preview("No image preview available.")
                self._set_archive_preview_image_controls_enabled(False)
                return

            self.archive_preview_info_edit.setPlainText(detail_text or metadata_summary or "No preview available.")
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self.archive_preview_label.clear_preview("No image preview available.")
            self._set_archive_preview_image_controls_enabled(False)

        def _toggle_archive_loose_preview(self) -> None:
            if self.current_archive_preview_result is None or not self.current_archive_preview_result.loose_file_path:
                return
            self._show_archive_preview_result(
                self.current_archive_preview_result,
                use_loose=not self.archive_preview_showing_loose,
            )

        def _apply_archive_preview_result(self, result: ArchivePreviewResult) -> None:
            self.current_archive_preview_result = result
            self.archive_preview_showing_loose = False
            self._show_archive_preview_result(result, use_loose=False)

        def _cleanup_archive_preview_refs(self) -> None:
            self.archive_preview_thread = None
            self.archive_preview_worker = None
            if self.pending_archive_preview_request is None:
                return
            request_id, entry = self.pending_archive_preview_request
            self.pending_archive_preview_request = None
            texconv_text = self.texconv_path_edit.text().strip()
            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            self._start_archive_preview_worker(
                request_id,
                texconv_path,
                entry,
                self._collect_archive_preview_loose_roots(),
            )

        def _set_archive_preview_image_controls_enabled(self, enabled: bool) -> None:
            self.archive_preview_zoom_out_button.setEnabled(enabled)
            self.archive_preview_zoom_fit_button.setEnabled(enabled)
            self.archive_preview_zoom_100_button.setEnabled(enabled)
            self.archive_preview_zoom_in_button.setEnabled(enabled)
            if not enabled:
                self.archive_preview_zoom_value.setText("-")
            else:
                self._update_archive_preview_zoom_label()

        def _update_archive_selection_state(self) -> None:
            selected_count = len(self._selected_archive_entries())
            has_filtered_entries = bool(self.archive_filtered_entries)
            has_filtered_dds = any(entry.extension == ".dds" for entry in self.archive_filtered_entries)
            self.archive_extract_selected_button.setEnabled(self.worker_thread is None and selected_count > 0)
            self.archive_extract_filtered_button.setEnabled(self.worker_thread is None and has_filtered_entries)
            self.archive_extract_to_workflow_button.setEnabled(self.worker_thread is None and has_filtered_dds)
            if not self.archive_entries:
                self.archive_stats_label.setText("No archives scanned.")
                return
            self.archive_stats_label.setText(
                f"{len(self.archive_filtered_entries):,} shown / {len(self.archive_entries):,} total entries. "
                f"DDS in current view: {sum(1 for entry in self.archive_filtered_entries if entry.extension == '.dds'):,}. "
                f"Selected files: {selected_count:,}."
            )

        def _update_archive_preview_zoom_label(self) -> None:
            if self.archive_preview_fit_to_view:
                self.archive_preview_zoom_value.setText("Fit")
            else:
                self.archive_preview_zoom_value.setText(f"{int(round(self.archive_preview_zoom_factor * 100))}%")

        def _apply_archive_preview_zoom(self) -> None:
            self.archive_preview_label.set_fit_to_view(self.archive_preview_fit_to_view)
            self.archive_preview_label.set_zoom_factor(self.archive_preview_zoom_factor)
            self._update_archive_preview_zoom_label()

        def _set_archive_preview_fit_mode(self) -> None:
            self.archive_preview_fit_to_view = True
            self._apply_archive_preview_zoom()

        def _set_archive_preview_zoom_factor(self, zoom_factor: float) -> None:
            self.archive_preview_fit_to_view = False
            self.archive_preview_zoom_factor = min(max(zoom_factor, 0.1), 16.0)
            self._apply_archive_preview_zoom()

        def _adjust_archive_preview_zoom(self, step: int) -> None:
            current_zoom = 1.0 if self.archive_preview_fit_to_view else self.archive_preview_zoom_factor
            zoom_steps = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]
            closest_index = min(range(len(zoom_steps)), key=lambda idx: abs(zoom_steps[idx] - current_zoom))
            next_index = min(max(closest_index + step, 0), len(zoom_steps) - 1)
            self._set_archive_preview_zoom_factor(zoom_steps[next_index])

        def _prompt_archive_extract_options(
            self,
            entries: Sequence[ArchiveEntry],
            output_root: Path,
        ) -> Optional[Tuple[bool, str]]:
            clear_root = False
            collision_mode = "overwrite"

            if output_root.exists() and directory_has_contents(output_root):
                clear_box = QMessageBox(self)
                clear_box.setWindowTitle("Target Folder Already Contains Files")
                clear_box.setIcon(QMessageBox.Question)
                clear_box.setText("The selected extraction target already contains files or folders.")
                clear_box.setInformativeText(
                    f"{output_root}\n\nChoose whether to clear it first or keep the existing files."
                )
                clear_button = clear_box.addButton("Clear Root", QMessageBox.AcceptRole)
                keep_button = clear_box.addButton("Keep Existing", QMessageBox.ActionRole)
                cancel_button = clear_box.addButton(QMessageBox.Cancel)
                clear_box.setDefaultButton(keep_button)
                clear_box.exec()
                clicked = clear_box.clickedButton()
                if clicked == cancel_button:
                    return None
                if clicked == clear_button:
                    clear_root = True
                    collision_mode = "overwrite"
                else:
                    collisions = count_existing_archive_targets(entries, output_root)
                    if collisions > 0:
                        collision_box = QMessageBox(self)
                        collision_box.setWindowTitle("Existing Files Found")
                        collision_box.setIcon(QMessageBox.Question)
                        collision_box.setText(f"{collisions:,} extracted path(s) already exist in the target.")
                        collision_box.setInformativeText(
                            "Choose whether to overwrite existing files or rename the newly extracted copies."
                        )
                        overwrite_button = collision_box.addButton("Overwrite Existing", QMessageBox.AcceptRole)
                        rename_button = collision_box.addButton("Rename New Files", QMessageBox.ActionRole)
                        collision_cancel_button = collision_box.addButton(QMessageBox.Cancel)
                        collision_box.setDefaultButton(overwrite_button)
                        collision_box.exec()
                        clicked_collision = collision_box.clickedButton()
                        if clicked_collision == collision_cancel_button:
                            return None
                        if clicked_collision == rename_button:
                            collision_mode = "rename"
                        else:
                            collision_mode = "overwrite"

            return clear_root, collision_mode

        def _prompt_archive_extract_target(
            self,
            entries: Sequence[ArchiveEntry],
            archive_extract_root: Path,
            *,
            prefer_original_dds_root: bool = False,
        ) -> Optional[Tuple[Path, bool]]:
            if not entries or any(entry.extension != ".dds" for entry in entries):
                return archive_extract_root, True

            original_root_text = self.original_dds_edit.text().strip()
            if not original_root_text:
                return archive_extract_root, True

            try:
                original_dds_root = Path(original_root_text).expanduser().resolve()
            except OSError:
                return archive_extract_root, True

            if original_dds_root == archive_extract_root:
                return archive_extract_root, True

            target_box = QMessageBox(self)
            target_box.setWindowTitle("DDS Extraction Target")
            target_box.setIcon(QMessageBox.Question)
            target_box.setText("Choose where to extract these DDS files.")
            target_box.setInformativeText(
                "Archive extract root:\n"
                f"{archive_extract_root}\n\n"
                "Original DDS root:\n"
                f"{original_dds_root}\n\n"
                "Use Original DDS root if you want the extracted DDS files to feed the workflow directly."
            )
            extract_root_button = target_box.addButton("Use Extract Root", QMessageBox.AcceptRole)
            original_root_button = target_box.addButton("Use Original DDS Root", QMessageBox.ActionRole)
            cancel_button = target_box.addButton(QMessageBox.Cancel)
            target_box.setDefaultButton(original_root_button if prefer_original_dds_root else extract_root_button)
            target_box.exec()

            clicked = target_box.clickedButton()
            if clicked == cancel_button:
                return None
            if clicked == original_root_button:
                return original_dds_root, False
            return archive_extract_root, True

        def _run_archive_extract(
            self,
            entries: Sequence[ArchiveEntry],
            *,
            set_original_dds_root: bool = False,
            allow_original_dds_root: bool = False,
            description: str,
        ) -> None:
            if not entries:
                self.set_status_message("No archive entries selected for extraction.", error=True)
                return

            output_root = self._suggest_archive_extract_root().resolve()
            update_archive_extract_root = True
            if allow_original_dds_root:
                target_result = self._prompt_archive_extract_target(
                    entries,
                    output_root,
                    prefer_original_dds_root=set_original_dds_root,
                )
                if target_result is None:
                    self.set_status_message("Archive extraction cancelled.")
                    return
                output_root, update_archive_extract_root = target_result
            extract_options = self._prompt_archive_extract_options(entries, output_root)
            if extract_options is None:
                self.set_status_message("Archive extraction cancelled.")
                return
            clear_root, collision_mode = extract_options

            def task(on_log: Callable[[str], None]) -> Dict[str, object]:
                if clear_root:
                    output_root.mkdir(parents=True, exist_ok=True)
                    on_log(f"Clearing extract root contents under {output_root}")
                    clear_directory_contents(output_root)
                on_log(f"Extracting {len(entries):,} archive entries to {output_root}")
                stats = extract_archive_entries(entries, output_root, collision_mode=collision_mode, on_log=on_log)
                return {
                    "output_root": str(output_root),
                    "stats": stats,
                    "collision_mode": collision_mode,
                    "cleared": clear_root,
                }

            def on_complete(result: object) -> None:
                if not isinstance(result, dict):
                    return
                output_root_value = str(result.get("output_root", output_root))
                stats = result.get("stats", {})
                if isinstance(stats, dict):
                    extracted = int(stats.get("extracted", 0))
                    failed = int(stats.get("failed", 0))
                    decompressed = int(stats.get("decompressed", 0))
                    renamed = int(stats.get("renamed", 0))
                else:
                    extracted = failed = decompressed = renamed = 0
                if update_archive_extract_root:
                    self.archive_extract_root_edit.setText(output_root_value)
                if set_original_dds_root:
                    self.original_dds_edit.setText(output_root_value)
                    self.main_tabs.setCurrentWidget(self.workflow_tab)
                    self.set_status_message(
                        f"Extracted {extracted} archive DDS file(s) to {output_root_value} and set Original DDS root."
                    )
                else:
                    self.set_status_message(f"Extracted {extracted} archive file(s) to {output_root_value}.")
                self.append_log(
                    f"Archive extraction summary: extracted={extracted}, decompressed={decompressed}, renamed={renamed}, failed={failed}."
                )

            self._run_utility_task(
                status_message=description,
                task=task,
                on_complete=on_complete,
            )

        def extract_selected_archive_entries(self) -> None:
            self._run_archive_extract(
                self._selected_archive_entries(),
                allow_original_dds_root=True,
                description="Extracting selected archive entries...",
            )

        def extract_filtered_archive_entries(self) -> None:
            self._run_archive_extract(
                self.archive_filtered_entries,
                allow_original_dds_root=True,
                description="Extracting filtered archive entries...",
            )

        def extract_filtered_archive_dds_to_workflow(self) -> None:
            dds_entries = [entry for entry in self.archive_filtered_entries if entry.extension == ".dds"]
            self._run_archive_extract(
                dds_entries,
                set_original_dds_root=True,
                allow_original_dds_root=True,
                description="Extracting filtered DDS archive entries to workflow root...",
            )

        def collect_config(self) -> AppConfig:
            return AppConfig(
                original_dds_root=self.original_dds_edit.text().strip(),
                png_root=self.png_root_edit.text().strip(),
                dds_staging_root=self.dds_staging_root_edit.text().strip(),
                output_root=self.output_root_edit.text().strip(),
                texconv_path=self.texconv_path_edit.text().strip(),
                dds_format_mode=self._combo_value(self.dds_format_mode_combo),
                dds_custom_format=self._combo_value(self.dds_custom_format_combo),
                dds_size_mode=self._combo_value(self.dds_size_mode_combo),
                dds_custom_width=self.dds_custom_width_spin.value(),
                dds_custom_height=self.dds_custom_height_spin.value(),
                dds_mip_mode=self._combo_value(self.dds_mip_mode_combo),
                dds_custom_mip_count=self.dds_custom_mip_spin.value(),
                enable_dds_staging=self.enable_dds_staging_checkbox.isChecked(),
                enable_incremental_resume=self.enable_incremental_resume_checkbox.isChecked(),
                texture_rules_text=self.texture_rules_edit.toPlainText(),
                dry_run=self.dry_run_checkbox.isChecked(),
                csv_log_enabled=self.csv_log_enabled_checkbox.isChecked(),
                csv_log_path=self.csv_log_path_edit.text().strip(),
                allow_unique_basename_fallback=self.unique_basename_checkbox.isChecked(),
                overwrite_existing_dds=self.overwrite_existing_checkbox.isChecked(),
                include_filters=self.filters_edit.toPlainText(),
                enable_chainner=self.enable_chainner_checkbox.isChecked(),
                chainner_exe_path=self.chainner_exe_path_edit.text().strip(),
                chainner_chain_path=self.chainner_chain_path_edit.text().strip(),
                chainner_override_json=self.chainner_override_edit.toPlainText(),
                archive_package_root=self.archive_package_root_edit.text().strip(),
                archive_extract_root=self.archive_extract_root_edit.text().strip(),
                archive_filter_text=self.archive_filter_edit.text().strip(),
                archive_extension_filter=self._combo_value(self.archive_extension_filter_combo),
                archive_package_filter_text=self.archive_package_filter_edit.text().strip(),
                archive_structure_filter=self._current_archive_structure_filter_value(),
                archive_role_filter=self._combo_value(self.archive_role_filter_combo),
                archive_min_size_kb=self.archive_min_size_spin.value(),
                archive_previewable_only=self.archive_previewable_only_checkbox.isChecked(),
            )

        def clear_live_log(self) -> None:
            self.log_view.clear()
            self.set_status_message("Live log cleared.")

        def clear_archive_scan_log(self) -> None:
            self.archive_log_view.clear()
            self.set_status_message("Archive scan log cleared.")

        def _background_task_active(self) -> bool:
            if self.worker_thread is not None:
                return True
            if self.text_search_tab.is_busy():
                self.set_status_message("Text Search is still running. Stop it first before starting another task.", error=True)
                return True
            return False

        def append_log(self, message: str) -> None:
            timestamp = time.strftime("%H:%M:%S")
            self.log_view.appendPlainText(f"[{timestamp}] {message}")
            scrollbar = self.log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        def append_archive_log(self, message: str) -> None:
            timestamp = time.strftime("%H:%M:%S")
            self.archive_log_view.appendPlainText(f"[{timestamp}] {message}")
            scrollbar = self.archive_log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        def set_status_message(self, message: str, *, error: bool = False) -> None:
            self.error_message_value.setText(message)
            self.error_message_value.setProperty("error", error)
            self.error_message_value.style().unpolish(self.error_message_value)
            self.error_message_value.style().polish(self.error_message_value)

        def set_busy(self, busy: bool, build_mode: bool = False) -> None:
            self.export_profile_action.setEnabled(not busy)
            self.import_profile_action.setEnabled(not busy)
            self.validate_chainner_menu_action.setEnabled(not busy)
            self.export_diagnostics_action.setEnabled(not busy)
            self.quick_start_menu_action.setEnabled(not busy)
            self.about_menu_action.setEnabled(not busy)
            self.left_panel.setEnabled(not busy)
            self.scan_button.setEnabled(not busy)
            self.start_button.setEnabled(not busy)
            self.stop_button.setEnabled(busy and build_mode)
            self.refresh_compare_button.setEnabled(not busy)
            self.compare_list.setEnabled(not busy)
            self.compare_previous_button.setEnabled(not busy and self.compare_list.currentRow() > 0)
            self.compare_next_button.setEnabled(
                not busy and 0 <= self.compare_list.currentRow() < self.compare_list.count() - 1
            )
            self.compare_sync_pan_checkbox.setEnabled(not busy)
            self.archive_package_root_edit.setEnabled(not busy)
            self.archive_extract_root_edit.setEnabled(not busy)
            self.archive_package_root_browse_button.setEnabled(not busy)
            self.archive_package_root_detect_button.setEnabled(not busy)
            self.archive_extract_root_browse_button.setEnabled(not busy)
            self.archive_scan_button.setEnabled(not busy)
            self.archive_refresh_scan_button.setEnabled(not busy)
            self.archive_filter_edit.setEnabled(not busy)
            self.archive_extension_filter_combo.setEnabled(not busy)
            self.archive_package_filter_edit.setEnabled(not busy)
            self._set_archive_structure_filter_enabled(not busy)
            self.archive_role_filter_combo.setEnabled(not busy)
            self.archive_min_size_spin.setEnabled(not busy)
            self.archive_previewable_only_checkbox.setEnabled(not busy)
            self.archive_extract_selected_button.setEnabled(not busy and len(self._selected_archive_entries()) > 0)
            self.archive_extract_filtered_button.setEnabled(not busy and bool(self.archive_filtered_entries))
            self.archive_extract_to_workflow_button.setEnabled(
                not busy and any(entry.extension == ".dds" for entry in self.archive_filtered_entries)
            )
            self.archive_tree.setEnabled(not busy)
            self.archive_preview_text_edit.setEnabled(not busy)
            self.archive_preview_info_edit.setEnabled(not busy)
            self.text_search_tab.set_external_busy(busy)
            self.archive_preview_loose_toggle_button.setEnabled(
                not busy and self.archive_preview_loose_toggle_button.isVisible()
            )
            image_preview_enabled = (
                not busy and self.archive_preview_stack.currentWidget() is self.archive_preview_scroll
            )
            self.archive_preview_zoom_out_button.setEnabled(image_preview_enabled)
            self.archive_preview_zoom_fit_button.setEnabled(image_preview_enabled)
            self.archive_preview_zoom_100_button.setEnabled(image_preview_enabled)
            self.archive_preview_zoom_in_button.setEnabled(image_preview_enabled)
            self._update_archive_filter_button_state()

        def reset_progress(self, total: int = 0) -> None:
            self.phase_value.setText("Idle")
            self.phase_progress_value.setText("Waiting")
            self.total_files_value.setText(str(total))
            self.current_file_value.setText("Idle")
            self.converted_value.setText("0")
            self.skipped_value.setText("0")
            self.failed_value.setText("0")
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("%v / %m")

        def start_scan(self) -> None:
            if self._background_task_active():
                return

            self.set_status_message("Scanning DDS files...")
            self.append_log("Starting scan.")
            self.reset_progress()
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(0)

            worker = ScanWorker(self.collect_config())
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.result_ready.connect(self._handle_scan_result)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.scan_worker = worker
            self.worker_thread = thread
            self.set_busy(True, build_mode=False)
            thread.start()

        def start_dds_to_png(self) -> None:
            if self._background_task_active():
                return

            config = self.collect_config()
            if not self._prepare_workflow_output_roots_for_start(config, include_output_root=False):
                return
            self.set_status_message("Preparing DDS to PNG conversion...")
            self.append_log("Starting DDS -> PNG conversion.")
            if not config.enable_chainner:
                self.append_log(
                    "Warning: DDS-to-PNG conversion is enabled while chaiNNer is disabled, so Start will convert DDS files to PNG and stop."
                )
            self.reset_progress()
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(0)

            worker = DdsToPngWorker(config)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.phase_changed.connect(self._handle_phase_changed)
            worker.phase_progress_changed.connect(self._handle_phase_progress_changed)
            worker.total_found.connect(self._handle_total_found)
            worker.current_file.connect(self._handle_current_file)
            worker.progress.connect(self._handle_progress)
            worker.completed.connect(self._handle_dds_to_png_complete)
            worker.cancelled.connect(self._handle_build_cancelled)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.dds_to_png_worker = worker
            self.worker_thread = thread
            self.set_busy(True, build_mode=True)
            thread.start()

        def start_build(self) -> None:
            if self._background_task_active():
                return

            config = self.collect_config()
            if config.enable_dds_staging and not config.enable_chainner:
                self.start_dds_to_png()
                return
            if not self._prepare_workflow_output_roots_for_start(config, include_output_root=True):
                return

            self.set_status_message("Preparing build...")
            self.append_log("Starting build.")
            self.reset_progress()
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(0)

            worker = BuildWorker(config)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.phase_changed.connect(self._handle_phase_changed)
            worker.phase_progress_changed.connect(self._handle_phase_progress_changed)
            worker.total_found.connect(self._handle_total_found)
            worker.current_file.connect(self._handle_current_file)
            worker.progress.connect(self._handle_progress)
            worker.completed.connect(self._handle_build_complete)
            worker.cancelled.connect(self._handle_build_cancelled)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.build_worker = worker
            self.worker_thread = thread
            self.set_busy(True, build_mode=True)
            thread.start()

        def stop_build(self) -> None:
            active_worker = self.build_worker or self.dds_to_png_worker
            if active_worker is None:
                return
            active_worker.stop()
            self.set_status_message("Stop requested. Waiting for the current task to exit cleanly...")
            self.append_log("Stop requested by user.")
            self.stop_button.setEnabled(False)

        def open_output_folder(self) -> None:
            raw = self.output_root_edit.text().strip()
            if not raw:
                self.set_status_message("Output root is empty.", error=True)
                return

            path = Path(raw).expanduser()
            if not path.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    self.set_status_message(f"Could not create output root: {exc}", error=True)
                    return

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

        def _handle_scan_result(self, total: int) -> None:
            self.total_files_value.setText(str(total))
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(0)
            self.current_file_value.setText("Ready to start")
            self.set_status_message(f"Scan complete. Found {total} DDS files.")

        def _handle_total_found(self, total: int) -> None:
            self.total_files_value.setText(str(total))
            self._set_phase_progress(0, total, "0 / {total} DDS files".format(total=total), "DDS files")
            self.set_status_message(f"Found {total} DDS files. Processing...")

        def _handle_phase_changed(self, phase_name: str, detail: str, indeterminate: bool) -> None:
            self.phase_value.setText(phase_name)
            if indeterminate:
                self.phase_progress_value.setText("Working...")
                self.progress_bar.setRange(0, 0)
                self.progress_bar.setFormat("Working...")
            else:
                total = max(int(self.total_files_value.text() or "0"), 1)
                self.progress_bar.setRange(0, total)
                self.progress_bar.setFormat("%v / %m")
            self.set_status_message(detail)

        def _handle_phase_progress_changed(self, current: int, total: int, detail: str) -> None:
            units = "Items"
            lowered = detail.lower()
            if "node" in lowered:
                units = "Nodes"
            elif "png" in lowered:
                units = "PNG outputs"
            elif "dds" in lowered:
                units = "DDS files"
            self._set_phase_progress(current, total, detail, units)

        def _handle_current_file(self, current_file: str) -> None:
            self.current_file_value.setText(current_file)

        def _handle_progress(self, processed: int, total: int, converted: int, skipped: int, failed: int) -> None:
            self._set_phase_progress(processed, total, f"{processed} / {total} DDS files", "DDS files")
            self.converted_value.setText(str(converted))
            self.skipped_value.setText(str(skipped))
            self.failed_value.setText(str(failed))

        def _set_phase_progress(self, current: int, total: int, detail: str, units: str) -> None:
            self.phase_progress_value.setText(detail)
            if total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(min(max(current, 0), total))
                self.progress_bar.setFormat(f"{units}: %v / %m")
            else:
                self.progress_bar.setRange(0, 0)
                self.progress_bar.setFormat(detail or "Working...")

        def _handle_build_complete(self, summary: RunSummary) -> None:
            self._handle_progress(
                summary.converted + summary.skipped + summary.failed,
                summary.total_files,
                summary.converted,
                summary.skipped,
                summary.failed,
            )
            self.current_file_value.setText("Completed")
            if summary.failed:
                self.set_status_message(
                    f"Build completed with {summary.failed} failed file(s). Review the log for details.",
                    error=True,
                )
            else:
                self.set_status_message("Build completed successfully.")
            self.append_log(
                f"Finished. Converted/planned={summary.converted}, skipped={summary.skipped}, failed={summary.failed}."
            )
            if summary.log_csv_path:
                self.append_log(f"CSV log saved to {summary.log_csv_path}")
            self.refresh_compare_list(select_current=True)
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(1)

        def _handle_dds_to_png_complete(self, summary: RunSummary) -> None:
            self._handle_progress(
                summary.converted + summary.skipped + summary.failed,
                summary.total_files,
                summary.converted,
                summary.skipped,
                summary.failed,
            )
            self.current_file_value.setText("Completed")
            if summary.failed:
                self.set_status_message(
                    f"DDS to PNG conversion completed with {summary.failed} failed file(s). Review the log for details.",
                    error=True,
                )
            else:
                self.set_status_message("DDS to PNG conversion completed successfully.")
            self.append_log(
                f"Finished DDS -> PNG. Converted/planned={summary.converted}, skipped={summary.skipped}, failed={summary.failed}."
            )
            if summary.log_csv_path:
                self.append_log(f"CSV log saved to {summary.log_csv_path}")
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(0)

        def _handle_build_cancelled(self, message: str) -> None:
            self.set_status_message(message, error=True)
            self.current_file_value.setText("Stopped")
            self.append_log(message)

        def _handle_utility_completed(self, result: object) -> None:
            if self._utility_completion_handler is not None:
                self._utility_completion_handler(result)

        def _handle_worker_error(self, message: str) -> None:
            self.set_status_message(message, error=True)
            self.append_log(f"ERROR: {message}")
            if self.archive_scan_worker is not None:
                self.append_archive_log(f"ERROR: {message}")
                self.archive_scan_progress_label.setText(f"Archive scan failed: {message}")
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(0)
                self.archive_scan_progress_bar.setFormat("%v / %m")

        def _get_compare_zoom_state(self, side: str) -> Tuple[PreviewLabel, bool, float, QLabel]:
            if side == "original":
                return (
                    self.original_preview_label,
                    self.original_compare_fit_to_view,
                    self.original_compare_zoom_factor,
                    self.original_compare_zoom_value,
                )
            if side == "output":
                return (
                    self.output_preview_label,
                    self.output_compare_fit_to_view,
                    self.output_compare_zoom_factor,
                    self.output_compare_zoom_value,
                )
            raise ValueError(f"Unknown compare side: {side}")

        def _update_compare_zoom_label(self, side: str) -> None:
            _label, fit_to_view, zoom_factor, value_label = self._get_compare_zoom_state(side)
            if fit_to_view:
                value_label.setText("Fit")
            else:
                value_label.setText(f"{int(round(zoom_factor * 100))}%")

        def _apply_compare_zoom(self, side: str) -> None:
            preview_label, fit_to_view, zoom_factor, _value_label = self._get_compare_zoom_state(side)
            preview_label.set_fit_to_view(fit_to_view)
            preview_label.set_zoom_factor(zoom_factor)
            self._update_compare_zoom_label(side)

        def _set_compare_fit_mode(self, side: str) -> None:
            if side == "original":
                self.original_compare_fit_to_view = True
            else:
                self.output_compare_fit_to_view = True
            self._apply_compare_zoom(side)

        def _set_compare_zoom_factor(self, side: str, zoom_factor: float) -> None:
            bounded_zoom = max(0.25, min(8.0, zoom_factor))
            if side == "original":
                self.original_compare_fit_to_view = False
                self.original_compare_zoom_factor = bounded_zoom
            else:
                self.output_compare_fit_to_view = False
                self.output_compare_zoom_factor = bounded_zoom
            self._apply_compare_zoom(side)

        def _adjust_compare_zoom(self, side: str, step: int) -> None:
            _preview_label, fit_to_view, zoom_factor, _value_label = self._get_compare_zoom_state(side)
            current = zoom_factor if not fit_to_view else 1.0
            if step > 0:
                new_zoom = current * 1.25
            else:
                new_zoom = current / 1.25
            self._set_compare_zoom_factor(side, new_zoom)

        def _select_compare_offset(self, offset: int) -> None:
            count = self.compare_list.count()
            if count == 0:
                return
            current_row = self.compare_list.currentRow()
            if current_row < 0:
                current_row = 0
            next_row = max(0, min(count - 1, current_row + offset))
            self.compare_list.setCurrentRow(next_row)

        def _update_compare_navigation_state(self) -> None:
            count = self.compare_list.count()
            current_row = self.compare_list.currentRow()
            self.compare_previous_button.setEnabled(count > 0 and current_row > 0)
            self.compare_next_button.setEnabled(count > 0 and 0 <= current_row < count - 1)

        def _sync_compare_scrollbar(self, source_bar, target_bar, value: int) -> None:
            del source_bar
            if not self.compare_sync_pan_checkbox.isChecked() or self.compare_syncing_scrollbars:
                return
            self.compare_syncing_scrollbars = True
            try:
                target_bar.setValue(value)
            finally:
                self.compare_syncing_scrollbars = False

        def _sync_compare_scroll_positions(self) -> None:
            if not self.compare_sync_pan_checkbox.isChecked():
                return
            self._sync_compare_scrollbar(
                self.original_preview_scroll.horizontalScrollBar(),
                self.output_preview_scroll.horizontalScrollBar(),
                self.original_preview_scroll.horizontalScrollBar().value(),
            )
            self._sync_compare_scrollbar(
                self.original_preview_scroll.verticalScrollBar(),
                self.output_preview_scroll.verticalScrollBar(),
                self.original_preview_scroll.verticalScrollBar().value(),
            )

        def refresh_compare_list(self, select_current: bool = False) -> None:
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()
            selected_text = None
            if select_current and self.compare_list.currentItem() is not None:
                selected_text = self.compare_list.currentItem().data(Qt.UserRole)
            self.compare_list.clear()
            self.compare_relative_paths = []

            if not original_root_text and not output_root_text:
                self.compare_preview_request_id += 1
                self.original_preview_meta_label.setText("")
                self.output_preview_meta_label.setText("")
                self.original_preview_label.clear_preview("Set the original and output folders to enable compare mode.")
                self.output_preview_label.clear_preview("Set the original and output folders to enable compare mode.")
                self._update_compare_navigation_state()
                return

            original_root = Path(original_root_text).expanduser()
            output_root = Path(output_root_text).expanduser()
            self.compare_relative_paths = collect_compare_relative_paths(original_root, output_root)

            for relative_path in self.compare_relative_paths:
                item = QListWidgetItem(relative_path.as_posix())
                item.setData(Qt.UserRole, str(relative_path))
                self.compare_list.addItem(item)

            if not self.compare_relative_paths:
                self.compare_preview_request_id += 1
                self.original_preview_meta_label.setText("")
                self.output_preview_meta_label.setText("")
                self.original_preview_label.clear_preview("No DDS files found to compare.")
                self.output_preview_label.clear_preview("No DDS files found to compare.")
                self._update_compare_navigation_state()
                return

            if selected_text is not None:
                for row in range(self.compare_list.count()):
                    item = self.compare_list.item(row)
                    if item.data(Qt.UserRole) == selected_text:
                        self.compare_list.setCurrentItem(item)
                        self._update_compare_navigation_state()
                        return

            self.compare_list.setCurrentRow(0)
            self._update_compare_navigation_state()

        def _handle_compare_selection_change(
            self,
            current: Optional[QListWidgetItem],
            previous: Optional[QListWidgetItem],
        ) -> None:
            del previous
            self._update_compare_navigation_state()
            if current is None:
                self.compare_preview_request_id += 1
                self.original_preview_meta_label.setText("")
                self.output_preview_meta_label.setText("")
                self.original_preview_label.clear_preview("Select a DDS file to preview.")
                self.output_preview_label.clear_preview("Select a DDS file to preview.")
                return

            relative_path = Path(current.data(Qt.UserRole))
            self._render_compare_preview(relative_path)

        def _render_compare_preview(self, relative_path: Path) -> None:
            texconv_text = self.texconv_path_edit.text().strip()
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()

            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            original_path = Path(original_root_text).expanduser() / relative_path if original_root_text else None
            output_path = Path(output_root_text).expanduser() / relative_path if output_root_text else None
            request_id = self.compare_preview_request_id + 1
            self.compare_preview_request_id = request_id

            self.original_preview_meta_label.setText("")
            self.output_preview_meta_label.setText("")
            self.original_preview_label.clear_preview("Loading preview...")
            self.output_preview_label.clear_preview("Loading preview...")

            if self.compare_preview_thread is not None:
                self.pending_compare_preview_request = (request_id, relative_path)
                return

            self._start_compare_preview_worker(request_id, texconv_path, original_path, output_path)

        def _start_compare_preview_worker(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            original_path: Optional[Path],
            output_path: Optional[Path],
        ) -> None:
            worker = ComparePreviewWorker(request_id, texconv_path, original_path, output_path)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.completed.connect(self._handle_compare_preview_ready)
            worker.error.connect(self._handle_compare_preview_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_compare_preview_refs)

            self.compare_preview_worker = worker
            self.compare_preview_thread = thread
            thread.start()

        def _handle_compare_preview_ready(self, request_id: int, payload: object) -> None:
            if request_id != self.compare_preview_request_id:
                return
            if not isinstance(payload, dict):
                return

            original_result = payload.get("original")
            output_result = payload.get("output")
            if isinstance(original_result, ComparePreviewPaneResult):
                self._apply_compare_preview_result(
                    self.original_preview_label,
                    self.original_preview_meta_label,
                    original_result,
                )
            if isinstance(output_result, ComparePreviewPaneResult):
                self._apply_compare_preview_result(
                    self.output_preview_label,
                    self.output_preview_meta_label,
                    output_result,
                )

        def _handle_compare_preview_error(self, request_id: int, message: str) -> None:
            if request_id != self.compare_preview_request_id:
                return
            self.original_preview_meta_label.setText("")
            self.output_preview_meta_label.setText("")
            self.original_preview_label.clear_preview(message)
            self.output_preview_label.clear_preview(message)

        def _apply_compare_preview_result(
            self,
            label: PreviewLabel,
            meta_label: QLabel,
            result: ComparePreviewPaneResult,
        ) -> None:
            if result.status != "ok":
                meta_label.setText("")
                label.clear_preview(result.message)
                return

            pixmap = QPixmap(result.preview_png_path)
            if pixmap.isNull():
                meta_label.setText("")
                label.clear_preview("Qt could not load the generated PNG preview.")
                return
            meta_label.setText(result.metadata_summary)
            label.set_preview_pixmap(pixmap, result.title)

        def _cleanup_compare_preview_refs(self) -> None:
            self.compare_preview_thread = None
            self.compare_preview_worker = None
            if self.pending_compare_preview_request is None:
                return

            request_id, relative_path = self.pending_compare_preview_request
            self.pending_compare_preview_request = None
            texconv_text = self.texconv_path_edit.text().strip()
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()
            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            original_path = Path(original_root_text).expanduser() / relative_path if original_root_text else None
            output_path = Path(output_root_text).expanduser() / relative_path if output_root_text else None
            self._start_compare_preview_worker(request_id, texconv_path, original_path, output_path)

        def _cleanup_worker_refs(self) -> None:
            self.worker_thread = None
            self.scan_worker = None
            self.archive_scan_worker = None
            self.build_worker = None
            self.dds_to_png_worker = None
            self.utility_worker = None
            self._utility_completion_handler = None
            self.set_busy(False, build_mode=False)

        def closeEvent(self, event) -> None:  # type: ignore[override]
            self.settings.setValue("window/geometry", self.saveGeometry())
            self._save_settings()
            if self.build_worker is not None:
                self.build_worker.stop()
                if self.worker_thread is not None:
                    self.worker_thread.quit()
                    self.worker_thread.wait(3000)
            if self.dds_to_png_worker is not None:
                self.dds_to_png_worker.stop()
                if self.worker_thread is not None:
                    self.worker_thread.quit()
                    self.worker_thread.wait(3000)
            if self.compare_preview_thread is not None:
                self.compare_preview_thread.quit()
                self.compare_preview_thread.wait(3000)
            if self.archive_preview_thread is not None:
                self.archive_preview_thread.quit()
                self.archive_preview_thread.wait(3000)
            self.text_search_tab.shutdown()
            super().closeEvent(event)

    apply_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setOrganizationName(APP_ORGANIZATION)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    icon_path = resolve_app_icon_path()
    if icon_path is not None:
        app_icon = QIcon(str(icon_path))
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)
    startup_settings = create_settings()
    startup_theme = str(startup_settings.value("appearance/theme", DEFAULT_UI_THEME))
    apply_app_theme(app, startup_theme)

    log_font = QFont("Consolas")
    if not log_font.exactMatch():
        log_font = QFont("Courier New")

    window = MainWindow()
    if not app.windowIcon().isNull():
        window.setWindowIcon(app.windowIcon())
    window.log_view.setFont(log_font)
    window.archive_log_view.setFont(log_font)
    window.text_search_tab.log_view.setFont(log_font)
    window.show()
    return app.exec()

__all__ = ["run_gui"]
