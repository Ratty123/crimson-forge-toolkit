from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import zipfile
from collections import Counter, OrderedDict
from html import escape
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from crimson_forge_toolkit.constants import *
from crimson_forge_toolkit.models import *
from crimson_forge_toolkit.core.archive import *
from crimson_forge_toolkit.core.classification_registry import (
    configure_texture_classification_registry,
    get_registered_texture_classification,
)
from crimson_forge_toolkit.core.archive_modding import (
    ARCHIVE_AUDIO_EXPORT_EXTENSIONS,
    ARCHIVE_AUDIO_PATCH_EXTENSIONS,
    ARCHIVE_MESH_EXTENSIONS,
    ARCHIVE_PATCH_BACKUP_ROOT,
    ArchiveLooseExportResult,
    ArchivePatchRequest,
    ArchivePatchResult,
    MeshExportResult,
    MeshImportPreviewResult,
    build_archive_audio_patch_payload,
    build_archive_texture_payload_from_dds,
    build_archive_texture_payload_from_png,
    build_mesh_import_preview,
    export_archive_payloads_to_mod_ready_loose,
    export_archive_audio_as_wav,
    export_archive_mesh,
    get_archive_texture_patch_blocker,
    list_archive_patch_backups,
    patch_archive_entries,
    restore_archive_patch_backup,
)
from crimson_forge_toolkit.core.model_export import export_model_preview_to_obj
from crimson_forge_toolkit.core.chainner import *
from crimson_forge_toolkit.core.pipeline import *
from crimson_forge_toolkit.core.realesrgan_ncnn import discover_realesrgan_ncnn_models, resolve_ncnn_model_dir
from crimson_forge_toolkit.core.ncnn_model_catalog import (
    NCNN_CATALOG_SOURCE_LINKS,
    NCNN_MODEL_CATALOG,
    get_ncnn_catalog_entry,
)
from crimson_forge_toolkit.core.upscale_profiles import get_texture_preset_definition


def run_gui() -> int:
    try:
        from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, QObject, Signal, Slot
        from PySide6.QtGui import (
            QDesktopServices,
            QFont,
            QIcon,
            QImageReader,
            QPixmap,
        )
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
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
            QMenu,
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

    from crimson_forge_toolkit.ui.themes import UI_THEME_SCHEMES, build_app_palette, build_app_stylesheet
    from crimson_forge_toolkit.ui.widgets import (
        AboutDialog,
        CodePreviewEditor,
        FlatSectionPanel,
        build_responsive_splitter_sizes,
        clamp_splitter_sizes,
        CollapsibleSection,
        ensure_app_wheel_guard,
        LogHighlighter,
        MediaPreviewWidget,
        ModelPreviewWidget,
        PreviewLabel,
        PreviewScrollArea,
        QuickStartDialog,
    )
    from crimson_forge_toolkit.ui.research_tab import ResearchTab
    from crimson_forge_toolkit.ui.settings_tab import SettingsTab
    from crimson_forge_toolkit.ui.policy_preview_dialog import TexturePolicyPreviewDialog
    from crimson_forge_toolkit.ui.safe_upscale_wizard import SafeUpscaleWizard
    from crimson_forge_toolkit.ui.replace_assistant_tab import ReplaceAssistantTab
    from crimson_forge_toolkit.ui.text_search_tab import TextSearchTab
    from crimson_forge_toolkit.ui.texture_editor_tab import TextureEditorTab

    def resolve_settings_file_path() -> Path:
        if getattr(sys, "frozen", False):
            base_dir = Path(sys.executable).resolve().parent
        else:
            base_dir = Path(__file__).resolve().parents[2]
        return base_dir / f"{APP_NAME}.cfg"

    settings_file_path = resolve_settings_file_path()
    crash_reports_dir = settings_file_path.parent / "crash_reports"
    _default_sys_excepthook = sys.excepthook
    _default_threading_excepthook = getattr(threading, "excepthook", None)
    _default_unraisablehook = getattr(sys, "unraisablehook", None)
    _active_main_window: Optional["MainWindow"] = None
    _capture_crash_details_enabled = False

    def _set_crash_capture_enabled(enabled: bool) -> None:
        nonlocal _capture_crash_details_enabled
        _capture_crash_details_enabled = bool(enabled)

    def _collect_crash_context() -> Dict[str, object]:
        window = _active_main_window
        context: Dict[str, object] = {}
        if window is None:
            return context
        try:
            current_tab_index = window.main_tabs.currentIndex()
            if current_tab_index >= 0:
                context["current_tab"] = window.main_tabs.tabText(current_tab_index)
        except Exception:
            pass
        try:
            entry = window._current_archive_entry()
            if entry is not None:
                context["selected_archive_path"] = entry.path
                context["selected_archive_package"] = str(entry.pamt_path)
        except Exception:
            pass
        try:
            context["texconv_path"] = window.texconv_path_edit.text().strip()
        except Exception:
            pass
        try:
            context["archive_package_root"] = window.archive_package_root_edit.text().strip()
        except Exception:
            pass
        try:
            log_lines = window.log_view.toPlainText().splitlines()
            context["recent_log_tail"] = log_lines[-80:]
        except Exception:
            pass
        return context

    def _write_crash_report(
        kind: str,
        title: str,
        body: str,
        *,
        context: Optional[Dict[str, object]] = None,
    ) -> None:
        if not _capture_crash_details_enabled:
            return
        try:
            crash_reports_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            report_path = crash_reports_dir / f"{kind}_{timestamp}.log"
            lines = [
                f"{APP_TITLE} crash/details report",
                f"Kind: {kind}",
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Version: {APP_VERSION}",
                f"Python: {sys.version}",
                f"Platform: {platform.platform()}",
                "",
                title.strip(),
                "",
                body.rstrip(),
            ]
            report_context = context if context is not None else _collect_crash_context()
            if report_context:
                lines.extend(["", "Context:", json.dumps(report_context, indent=2, ensure_ascii=False)])
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _handle_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
        formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        _write_crash_report("unhandled_exception", "Unhandled exception", formatted)
        _default_sys_excepthook(exc_type, exc_value, exc_traceback)

    def _handle_thread_exception(args) -> None:
        formatted = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        thread_name = getattr(getattr(args, "thread", None), "name", "unknown thread")
        _write_crash_report("thread_exception", f"Unhandled thread exception in {thread_name}", formatted)
        if _default_threading_excepthook is not None:
            _default_threading_excepthook(args)

    def _handle_unraisable_exception(args) -> None:
        formatted = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _write_crash_report(
            "unraisable_exception",
            f"Unraisable exception from {getattr(args, 'object', None)!r}",
            formatted,
        )
        if _default_unraisablehook is not None:
            _default_unraisablehook(args)

    sys.excepthook = _handle_uncaught_exception
    if _default_threading_excepthook is not None:
        threading.excepthook = _handle_thread_exception
    if _default_unraisablehook is not None:
        sys.unraisablehook = _handle_unraisable_exception

    def create_settings() -> QSettings:
        settings_file_path.parent.mkdir(parents=True, exist_ok=True)
        configure_texture_classification_registry(
            settings_file_path.parent / "texture_classification_registry.json"
        )
        legacy_settings_candidates = [settings_file_path.with_name(f"{name}.cfg") for name in LEGACY_APP_NAMES]
        if not settings_file_path.exists():
            for legacy_settings_path in legacy_settings_candidates:
                if not legacy_settings_path.exists():
                    continue
                try:
                    shutil.copy2(legacy_settings_path, settings_file_path)
                    break
                except OSError:
                    continue
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
            Path("assets") / "crimson_forge_toolkit.ico",
            Path("assets") / "crimson_forge_toolkit.png",
            Path("crimson_forge_toolkit.ico"),
            Path("crimson_forge_toolkit.png"),
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

    def _read_int_setting(settings: QSettings, key: str, default: int) -> int:
        try:
            return int(settings.value(key, default))
        except (TypeError, ValueError):
            return int(default)

    def _read_bool_setting(settings: QSettings, key: str, default: bool) -> bool:
        value = settings.value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def apply_app_theme(app: QApplication, settings: QSettings, theme_key: str) -> str:
        resolved_theme = theme_key if theme_key in UI_THEME_SCHEMES else DEFAULT_UI_THEME
        ui_font_family = str(settings.value("appearance/ui_font_family", DEFAULT_UI_FONT_FAMILY) or DEFAULT_UI_FONT_FAMILY)
        base_font_size = max(
            UI_FONT_SIZE_MIN,
            min(UI_FONT_SIZE_MAX, _read_int_setting(settings, "appearance/ui_font_size", DEFAULT_UI_FONT_SIZE)),
        )
        data_font_size = max(
            UI_FONT_SIZE_MIN,
            min(UI_FONT_SIZE_MAX, _read_int_setting(settings, "appearance/data_font_size", DEFAULT_UI_DATA_FONT_SIZE)),
        )
        density_key = str(settings.value("appearance/ui_density", DEFAULT_UI_DENSITY) or DEFAULT_UI_DENSITY)
        app_font = QFont(app.font())
        app_font.setFamily(ui_font_family)
        app_font.setPointSize(base_font_size)
        app.setFont(app_font)
        app.setPalette(build_app_palette(resolved_theme))
        app.setStyleSheet(
            build_app_stylesheet(
                resolved_theme,
                base_font_size=base_font_size,
                data_font_size=data_font_size,
                density_key=density_key,
            )
        )
        return resolved_theme

    def build_monospace_font(settings: QSettings) -> QFont:
        point_size = _read_int_setting(settings, "appearance/log_font_size", DEFAULT_UI_LOG_FONT_SIZE)
        selected_family = str(
            settings.value("appearance/log_font_family", DEFAULT_UI_LOG_FONT_FAMILY) or DEFAULT_UI_LOG_FONT_FAMILY
        )
        bold_enabled = _read_bool_setting(settings, "appearance/log_font_bold", DEFAULT_UI_LOG_FONT_BOLD)
        fallback_order = [selected_family] + [family for family in LOG_FONT_FAMILY_OPTIONS if family != selected_family]
        font = QFont(fallback_order[0])
        for family in fallback_order:
            candidate = QFont(family)
            if candidate.exactMatch():
                font = candidate
                break
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(point_size)
        font.setBold(bold_enabled)
        return font

    def apply_window_data_fonts(window: "MainWindow") -> None:
        log_font = build_monospace_font(window.settings)
        window.log_view.setFont(log_font)
        window.log_view.document().setDefaultFont(log_font)
        window.archive_log_view.setFont(log_font)
        window.archive_log_view.document().setDefaultFont(log_font)
        window.archive_preview_text_edit.apply_font_preferences(log_font, preserve_size=True)
        window.text_search_tab.log_view.setFont(log_font)
        window.text_search_tab.log_view.document().setDefaultFont(log_font)
        window.text_search_tab.preview_text_edit.apply_font_preferences(log_font, preserve_size=True)
        window.replace_assistant_tab.log_view.setFont(log_font)
        window.replace_assistant_tab.log_view.document().setDefaultFont(log_font)
        window.replace_assistant_tab.preview_details_edit.setFont(log_font)
        window.replace_assistant_tab.preview_details_edit.document().setDefaultFont(log_font)
        bold_enabled = _read_bool_setting(window.settings, "appearance/log_font_bold", DEFAULT_UI_LOG_FONT_BOLD)
        window.log_highlighter.set_bold_enabled(bold_enabled)
        window.archive_log_highlighter.set_bold_enabled(bold_enabled)
        window.text_search_tab.log_highlighter.set_bold_enabled(bold_enabled)

    class ScanWorker(QObject):
        log_message = Signal(str)
        result_ready = Signal(int)
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
                self.log_message.emit("Scanning DDS files...")
                result = scan_dds_files(self.config, stop_event=self.stop_event)
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
            build_tree_index: bool = True,
            filter_text: str = "",
            exclude_filter_text: str = "",
            extension_filter: str = "*",
            package_filter_text: str = "",
            structure_filter: str = "",
            role_filter: str = "all",
            exclude_common_technical_suffixes: bool = False,
            min_size_kb: int = 0,
            previewable_only: bool = False,
        ):
            super().__init__()
            self.package_root = package_root
            self.cache_root = cache_root
            self.force_refresh = force_refresh
            self.build_tree_index = build_tree_index
            self.filter_text = filter_text
            self.exclude_filter_text = exclude_filter_text
            self.extension_filter = extension_filter
            self.package_filter_text = package_filter_text
            self.structure_filter = structure_filter
            self.role_filter = role_filter
            self.exclude_common_technical_suffixes = exclude_common_technical_suffixes
            self.min_size_kb = min_size_kb
            self.previewable_only = previewable_only
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()

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
                    stop_event=self.stop_event,
                )
                self.log_message.emit("Preparing archive browser state from loaded entries...")
                browser_state = prepare_archive_browser_state(
                    entries,
                    filter_text=self.filter_text,
                    exclude_filter_text=self.exclude_filter_text,
                    extension_filter=self.extension_filter,
                    package_filter_text=self.package_filter_text,
                    structure_filter=self.structure_filter,
                    role_filter=self.role_filter,
                    exclude_common_technical_suffixes=self.exclude_common_technical_suffixes,
                    min_size_kb=self.min_size_kb,
                    previewable_only=self.previewable_only,
                    build_structure_children=True,
                    build_tree_index=self.build_tree_index,
                    on_progress=self.progress_changed.emit,
                    stop_event=self.stop_event,
                )
                path_index = build_archive_entry_path_index(entries)
                basename_index = build_archive_entry_basename_index(entries)
                extension_index = build_archive_entry_extension_index(entries)
                self.completed.emit(
                    {
                        "entries": entries,
                        "source": source,
                        "cache_path": str(cache_path) if cache_path is not None else "",
                        "browser_state": browser_state,
                        "path_index": path_index,
                        "basename_index": basename_index,
                        "extension_index": extension_index,
                    }
                )
            except Exception as exc:
                self.error.emit(str(exc))
            finally:
                self.finished.emit()

    class ArchiveFilterWorker(QObject):
        progress_changed = Signal(int, int, str)
        completed = Signal(object)
        error = Signal(str)
        finished = Signal()

        def __init__(
            self,
            entries: Sequence[ArchiveEntry],
            *,
            entries_by_extension: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
            request_signature: Tuple[object, ...] = (),
            preferred_path: str = "",
            build_tree_index: bool = True,
            filter_text: str = "",
            exclude_filter_text: str = "",
            extension_filter: str = "*",
            package_filter_text: str = "",
            structure_filter: str = "",
            role_filter: str = "all",
            exclude_common_technical_suffixes: bool = False,
            min_size_kb: int = 0,
            previewable_only: bool = False,
        ):
            super().__init__()
            # Keep references instead of copying 1M+ archive entries on the UI thread
            # before the worker even starts running.
            self.entries = entries
            self.entries_by_extension = entries_by_extension or {}
            self.request_signature = tuple(request_signature or ())
            self.preferred_path = preferred_path
            self.build_tree_index = build_tree_index
            self.filter_text = filter_text
            self.exclude_filter_text = exclude_filter_text
            self.extension_filter = extension_filter
            self.package_filter_text = package_filter_text
            self.structure_filter = structure_filter
            self.role_filter = role_filter
            self.exclude_common_technical_suffixes = exclude_common_technical_suffixes
            self.min_size_kb = min_size_kb
            self.previewable_only = previewable_only
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()

        @Slot()
        def run(self) -> None:
            try:
                source_entries = self.entries
                normalized_extension = normalize_archive_extension_filter(self.extension_filter)
                if normalized_extension and normalized_extension not in {"*", "all", ".*"} and self.entries_by_extension:
                    source_entries = self.entries_by_extension.get(normalized_extension, [])
                browser_state = prepare_archive_browser_state(
                    source_entries,
                    filter_text=self.filter_text,
                    exclude_filter_text=self.exclude_filter_text,
                    extension_filter=self.extension_filter,
                    package_filter_text=self.package_filter_text,
                    structure_filter=self.structure_filter,
                    role_filter=self.role_filter,
                    exclude_common_technical_suffixes=self.exclude_common_technical_suffixes,
                    min_size_kb=self.min_size_kb,
                    previewable_only=self.previewable_only,
                    build_structure_children=False,
                    build_tree_index=self.build_tree_index,
                    on_progress=self.progress_changed.emit,
                    stop_event=self.stop_event,
                )
                self.completed.emit(
                    {
                        "browser_state": browser_state,
                        "request_signature": self.request_signature,
                        "preferred_path": self.preferred_path,
                    }
                )
            except RunCancelled:
                pass
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
            original_planner_summary: str = "",
            output_planner_summary: str = "",
        ):
            super().__init__()
            self.request_id = request_id
            self.texconv_path = texconv_path
            self.original_path = original_path
            self.output_path = output_path
            self.original_planner_summary = original_planner_summary
            self.output_planner_summary = output_planner_summary
            self.stop_event = threading.Event()

        def stop(self) -> None:
            self.stop_event.set()

        @Slot()
        def run(self) -> None:
            try:
                if self.stop_event.is_set():
                    return
                payload = {
                    "original": build_compare_preview_pane_result(
                        self.texconv_path,
                        self.original_path,
                        "Original DDS not found.",
                        self.original_planner_summary,
                        stop_event=self.stop_event,
                    ),
                    "output": build_compare_preview_pane_result(
                        self.texconv_path,
                        self.output_path,
                        "Output DDS not found.",
                        self.output_planner_summary,
                        stop_event=self.stop_event,
                    ),
                }
                if not self.stop_event.is_set():
                    self.completed.emit(self.request_id, payload)
            except Exception as exc:
                if not self.stop_event.is_set():
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
            companion_entry: Optional[ArchiveEntry],
            texture_entries_by_normalized_path: Dict[str, List[ArchiveEntry]],
            texture_entries_by_basename: Dict[str, List[ArchiveEntry]],
            loose_search_roots: Sequence[Path],
            include_loose_preview_assets: bool = False,
        ):
            super().__init__()
            self.request_id = request_id
            self.texconv_path = texconv_path
            self.entry = entry
            self.companion_entry = companion_entry
            self.texture_entries_by_normalized_path = texture_entries_by_normalized_path
            self.texture_entries_by_basename = texture_entries_by_basename
            self.loose_search_roots = list(loose_search_roots)
            self.include_loose_preview_assets = include_loose_preview_assets
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
                    self.loose_search_roots,
                    companion_entry=self.companion_entry,
                    texture_entries_by_normalized_path=self.texture_entries_by_normalized_path,
                    texture_entries_by_basename=self.texture_entries_by_basename,
                    include_loose_preview_assets=self.include_loose_preview_assets,
                    stop_event=self.stop_event,
                )
                if self.stop_event.is_set():
                    return
                payload = self._attach_loaded_images(payload)
                if self.stop_event.is_set():
                    return
                if not self.stop_event.is_set():
                    self.completed.emit(self.request_id, payload)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.error.emit(self.request_id, str(exc))
            finally:
                self.finished.emit()

        def _attach_loaded_images(self, result: ArchivePreviewResult) -> ArchivePreviewResult:
            preview_image = self._load_image(result.preview_image_path)
            loose_preview_image = self._load_image(result.loose_preview_image_path)
            preview_model = result.preview_model
            meshes = getattr(preview_model, "meshes", None)
            if meshes:
                for mesh in meshes:
                    preview_texture_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
                    if preview_texture_path and getattr(mesh, "preview_texture_image", None) is None:
                        texture_image = self._load_image(preview_texture_path)
                        if texture_image is not None:
                            mesh.preview_texture_image = texture_image
            if preview_image is None and loose_preview_image is None:
                return result
            return dataclasses.replace(
                result,
                preview_image=preview_image,
                loose_preview_image=loose_preview_image,
            )

        def _load_image(self, image_path: str) -> object:
            if self.stop_event.is_set() or not image_path:
                return None
            reader = QImageReader(image_path)
            image = reader.read()
            if self.stop_event.is_set() or image.isNull():
                return None
            return image

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            nonlocal _active_main_window
            _active_main_window = self
            self.setWindowTitle(APP_TITLE)
            self.settings = create_settings()
            _set_crash_capture_enabled(self._preference_bool("capture_crash_details", False))
            self.settings_file_path = settings_file_path
            self.archive_cache_root = self.settings_file_path.parent / ARCHIVE_SCAN_CACHE_DIRNAME
            self._settings_ready = False
            self.current_theme_key = str(self.settings.value("appearance/theme", DEFAULT_UI_THEME))
            self.show_quick_start_on_launch = not self.settings.contains("ui/quick_start_shown")
            self.resize(1360, 840)
            self.setMinimumSize(1120, 720)
            self.worker_thread: Optional[QThread] = None
            self.scan_worker: Optional[ScanWorker] = None
            self.archive_scan_worker: Optional[ArchiveScanWorker] = None
            self.archive_filter_worker: Optional[ArchiveFilterWorker] = None
            self.build_worker: Optional[BuildWorker] = None
            self.dds_to_png_worker: Optional[DdsToPngWorker] = None
            self.utility_worker: Optional[UtilityWorker] = None
            self._utility_completion_handler: Optional[Callable[[object], None]] = None
            self._utility_updates_archive_progress = False
            self.compare_relative_paths: List[Path] = []
            self.compare_preview_thread: Optional[QThread] = None
            self.compare_preview_worker: Optional[ComparePreviewWorker] = None
            self.compare_preview_request_id = 0
            self.pending_compare_preview_request: Optional[Tuple[int, Path]] = None
            self.pending_compare_preview_selection: Optional[Path] = None
            self._pending_texture_editor_workflow_export: Optional[Dict[str, str]] = None
            self._pending_archive_workflow_extract: Optional[Dict[str, object]] = None
            self._shutting_down = False
            self._settings_save_timer = QTimer(self)
            self._settings_save_timer.setSingleShot(True)
            self._settings_save_timer.setInterval(250)
            self._settings_save_timer.timeout.connect(self._save_settings)
            self._column_autofit_timer = QTimer(self)
            self._column_autofit_timer.setSingleShot(True)
            self._column_autofit_timer.setInterval(90)
            self._column_autofit_timer.timeout.connect(self._apply_column_autofit)
            self._chainner_analysis_timer = QTimer(self)
            self._chainner_analysis_timer.setSingleShot(True)
            self._chainner_analysis_timer.setInterval(250)
            self._chainner_analysis_timer.timeout.connect(self._refresh_chainner_chain_info)
            self._compare_preview_timer = QTimer(self)
            self._compare_preview_timer.setSingleShot(True)
            self._compare_preview_timer.setInterval(90)
            self.compare_syncing_scrollbars = False
            self.workflow_right_splitter_normal_sizes: Optional[List[int]] = None
            self.archive_preview_thread: Optional[QThread] = None
            self.archive_preview_worker: Optional[ArchivePreviewWorker] = None
            self.archive_preview_request_id = 0
            self.pending_archive_preview_request: Optional[Tuple[int, Optional[ArchiveEntry], bool]] = None
            self.scheduled_archive_preview_request: Optional[Tuple[int, Optional[ArchiveEntry], bool]] = None
            self.current_archive_preview_result: Optional[ArchivePreviewResult] = None
            self.current_archive_model_texture_references: List[ArchiveModelTextureReference] = []
            self.archive_preview_cache: OrderedDict[str, ArchivePreviewResult] = OrderedDict()
            self.archive_preview_cache_keys: Dict[int, str] = {}
            self.archive_preview_cache_limit = 24
            self.archive_preview_requested_loose = False
            self.archive_preview_showing_loose = False
            self.archive_entries: List[ArchiveEntry] = []
            self.archive_entries_by_normalized_path: Dict[str, List[ArchiveEntry]] = {}
            self.archive_entries_by_basename: Dict[str, List[ArchiveEntry]] = {}
            self.archive_entries_by_extension: Dict[str, List[ArchiveEntry]] = {}
            self.archive_scan_finalize_pending = False
            self.archive_filtered_entries: List[ArchiveEntry] = []
            self.archive_filtered_dds_count = 0
            self.archive_filters_dirty = False
            self.archive_filter_apply_pending = False
            self.archive_filter_requested_signature: Tuple[object, ...] = ()
            self.archive_browser_refresh_pending = False
            self._activate_archive_browser_on_scan_complete = True
            self.archive_tree_child_folders: Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]] = {}
            self.archive_tree_direct_files: Dict[Tuple[str, ...], List[int]] = {}
            self.archive_tree_folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = {}
            self.archive_tree_items_by_folder_key: Dict[Tuple[str, ...], QTreeWidgetItem] = {}
            self.archive_tree_index_ready = False
            self.archive_tree_population_active = False
            self.archive_tree_population_next_index = 0
            self.archive_tree_population_preferred_path = ""
            self.archive_tree_population_target_item: Optional[QTreeWidgetItem] = None
            self.archive_tree_population_default_batch_size = 500
            self.archive_tree_population_batch_size = self.archive_tree_population_default_batch_size
            self.archive_tree_population_continue_batch_size = 300
            self.archive_tree_population_time_budget_ms = 12.0
            self.archive_tree_population_last_progress_update = 0.0
            self._archive_tree_population_on_complete: Optional[Callable[[], None]] = None
            self.archive_preview_loading_started_at = 0.0
            self.archive_preview_loading_entry_name = ""
            self.archive_preview_loading_loose = False
            self.archive_structure_filter_pending_value = ""
            self.archive_structure_filter_children: Dict[str, List[Tuple[str, int]]] = {}
            self.archive_structure_filter_combos: List[QComboBox] = []
            self.rebuilding_archive_structure_filters = False
            self.original_compare_zoom_factor = 1.0
            self.original_compare_fit_to_view = True
            self.output_compare_zoom_factor = 1.0
            self.output_compare_fit_to_view = True
            self.compare_preview_fit_scale = 1.25
            self.archive_preview_zoom_factor = 1.0
            self.archive_preview_fit_to_view = True
            self.archive_preview_debounce_timer = QTimer(self)
            self.archive_preview_debounce_timer.setSingleShot(True)
            self.archive_preview_debounce_timer.setInterval(90)
            self.archive_preview_debounce_timer.timeout.connect(self._flush_scheduled_archive_preview_request)
            self.archive_preview_loading_timer = QTimer(self)
            self.archive_preview_loading_timer.setInterval(250)
            self.archive_preview_loading_timer.timeout.connect(self._update_archive_preview_loading_indicator)
            self.archive_selection_state_timer = QTimer(self)
            self.archive_selection_state_timer.setSingleShot(True)
            self.archive_selection_state_timer.setInterval(30)
            self.archive_selection_state_timer.timeout.connect(self._update_archive_selection_state)
            self.archive_tree_population_timer = QTimer(self)
            self.archive_tree_population_timer.setSingleShot(True)
            self.archive_tree_population_timer.setInterval(0)
            self.archive_tree_population_timer.timeout.connect(self._continue_archive_tree_population)
            self._last_build_unknown_review_result: Optional[Dict[str, object]] = None

            icon_path = resolve_app_icon_path()
            if icon_path is not None:
                self.setWindowIcon(QIcon(str(icon_path)))

            menu_bar = self.menuBar()
            self.profile_menu = menu_bar.addMenu("Profile")
            self.export_profile_action = self.profile_menu.addAction("Export Profile...")
            self.import_profile_action = self.profile_menu.addAction("Import Profile...")
            self.quick_start_menu_action = menu_bar.addAction("Quick Start")
            self.documentation_menu = menu_bar.addMenu("Documentation")
            self.open_documentation_action = self.documentation_menu.addAction("Open Documentation")
            self.documentation_menu.addSeparator()
            self.export_diagnostics_action = self.documentation_menu.addAction("Export Diagnostics...")

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
            self.main_tabs.addTab(self.workflow_tab, "Texture Workflow")

            self.workflow_splitter = QSplitter(Qt.Horizontal)
            self.workflow_splitter.setChildrenCollapsible(False)
            workflow_layout.addWidget(self.workflow_splitter, stretch=1)

            self.left_panel = QWidget()
            self.left_panel.setMinimumWidth(320)
            left_layout = QVBoxLayout(self.left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(10)

            self.left_scroll_area = QScrollArea()
            self.left_scroll_area.setWidgetResizable(True)
            self.left_scroll_area.setFrameShape(QFrame.NoFrame)
            self.left_scroll_area.setMinimumWidth(320)
            self.left_scroll_area.setWidget(self.left_panel)

            self.right_panel = QWidget()
            self.right_panel.setMinimumWidth(320)
            right_layout = QVBoxLayout(self.right_panel)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(10)
            self.workflow_right_splitter = QSplitter(Qt.Vertical)
            self.workflow_right_splitter.setChildrenCollapsible(False)

            self.workflow_splitter.addWidget(self.left_scroll_area)
            self.workflow_splitter.addWidget(self.right_panel)
            self.workflow_splitter.setStretchFactor(0, 1)
            self.workflow_splitter.setStretchFactor(1, 2)
            self.workflow_splitter.setSizes([420, 760])

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
            self.texture_editor_png_root_edit = QLineEdit()
            self.dds_staging_root_edit = QLineEdit()
            self.output_root_edit = QLineEdit()
            self.texconv_path_edit = QLineEdit()

            self._add_path_row(paths_layout, 0, "Original DDS root", self.original_dds_edit, self._browse_original_dds_root)
            self._add_path_row(paths_layout, 1, "PNG root", self.png_root_edit, self._browse_png_root)
            self._add_path_row(
                paths_layout,
                2,
                "Texture Editor PNG root",
                self.texture_editor_png_root_edit,
                self._browse_texture_editor_png_root,
            )
            self.dds_staging_browse_button = self._add_path_row(
                paths_layout,
                3,
                "Staging PNG root",
                self.dds_staging_root_edit,
                self._browse_dds_staging_root,
            )
            self._add_path_row(paths_layout, 4, "Output root", self.output_root_edit, self._browse_output_root)
            self._add_path_row(paths_layout, 5, "texconv.exe path", self.texconv_path_edit, self._browse_texconv_path)

            self.paths_section.body_layout.addWidget(paths_group)

            self.setup_section = CollapsibleSection("Setup", expanded=False)
            setup_group = QWidget()
            setup_layout = QVBoxLayout(setup_group)
            setup_layout.setContentsMargins(0, 0, 0, 0)
            setup_layout.setSpacing(8)

            setup_buttons_row_1 = QGridLayout()
            setup_buttons_row_1.setHorizontalSpacing(8)
            setup_buttons_row_1.setVerticalSpacing(8)
            self.init_workspace_button = QPushButton("Init Workspace")
            self.create_folders_button = QPushButton("Create Folders")
            self.open_texture_editor_button = QPushButton("Open File In Texture Editor")
            setup_buttons_row_1.addWidget(self.init_workspace_button, 0, 0)
            setup_buttons_row_1.addWidget(self.create_folders_button, 0, 1)
            setup_buttons_row_1.addWidget(self.open_texture_editor_button, 1, 0, 1, 2)
            setup_layout.addLayout(setup_buttons_row_1)

            setup_buttons_row_2 = QGridLayout()
            setup_buttons_row_2.setHorizontalSpacing(8)
            setup_buttons_row_2.setVerticalSpacing(8)
            self.download_chainner_button = QPushButton("Open chaiNNer Download Page")
            self.download_chainner_button.setToolTip("Open the official chaiNNer download page in your default browser.")
            self.download_texconv_button = QPushButton("Open texconv Download Page")
            self.download_texconv_button.setToolTip("Open the official DirectXTex releases page in your default browser.")
            setup_buttons_row_2.addWidget(self.download_chainner_button, 0, 0, 1, 2)
            setup_buttons_row_2.addWidget(self.download_texconv_button, 1, 0, 1, 2)
            setup_layout.addLayout(setup_buttons_row_2)

            setup_buttons_row_3 = QHBoxLayout()
            setup_buttons_row_3.setSpacing(8)
            self.download_ncnn_button = QPushButton("Open Real-ESRGAN NCNN Download Page")
            self.download_ncnn_button.setToolTip("Open the official Real-ESRGAN NCNN releases page in your default browser.")
            setup_buttons_row_3.addWidget(self.download_ncnn_button)
            setup_layout.addLayout(setup_buttons_row_3)

            setup_buttons_row_4 = QHBoxLayout()
            setup_buttons_row_4.setSpacing(8)
            self.import_ncnn_models_button = QPushButton("Import NCNN Models")
            self.import_ncnn_models_button.setToolTip(
                "Import NCNN models from a folder, zip, or files that contain matching .param + .bin pairs."
            )
            setup_buttons_row_4.addWidget(self.import_ncnn_models_button)
            setup_layout.addLayout(setup_buttons_row_4)
            for button in (
                self.init_workspace_button,
                self.create_folders_button,
                self.open_texture_editor_button,
                self.download_chainner_button,
                self.download_texconv_button,
                self.download_ncnn_button,
                self.import_ncnn_models_button,
            ):
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            setup_hint = QLabel(
                "Direct backends can be prepared here. The setup buttons open official external download or install pages "
                "in your browser instead of downloading files inside the app. NCNN models can still be imported "
                "from files you already downloaded locally."
            )
            setup_hint.setObjectName("HintLabel")
            setup_hint.setWordWrap(True)
            setup_layout.addWidget(setup_hint)

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

            self.enable_dds_staging_checkbox = QCheckBox("Create source PNGs from DDS before processing")
            self.enable_dds_staging_checkbox.setToolTip(
                "When enabled, the app first converts loose DDS files into PNGs. "
                "If an upscaling backend is active, those source PNGs are written to the staging folder first."
            )
            self.dds_output_mode_hint = QLabel(
                "Uses texconv to create source PNG files first. If no upscaling backend is selected, Start stops after PNG conversion."
            )
            self.dds_output_mode_hint.setObjectName("HintLabel")
            self.dds_output_mode_hint.setWordWrap(True)
            self.dds_output_flow_hint = QLabel()
            self.dds_output_flow_hint.setObjectName("HintLabel")
            self.dds_output_flow_hint.setWordWrap(True)

            self.dds_format_mode_combo = QComboBox()
            self._add_combo_choice(self.dds_format_mode_combo, "Match original DDS format", DDS_FORMAT_MODE_MATCH_ORIGINAL)
            self._add_combo_choice(self.dds_format_mode_combo, "Custom format", DDS_FORMAT_MODE_CUSTOM)

            self.dds_custom_format_label = QLabel("Custom format")
            self.dds_custom_format_combo = QComboBox()
            for format_name in SUPPORTED_TEXCONV_FORMAT_CHOICES:
                self._add_combo_choice(self.dds_custom_format_combo, format_name, format_name)

            self.dds_size_mode_combo = QComboBox()
            self._add_combo_choice(self.dds_size_mode_combo, "Use final PNG size for rebuilt DDS", DDS_SIZE_MODE_PNG)
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

            self.dds_output_size_hint = QLabel()
            self.dds_output_size_hint.setObjectName("HintLabel")
            self.dds_output_size_hint.setWordWrap(True)

            self.dds_custom_size_widget = QWidget()
            custom_size_row = QHBoxLayout(self.dds_custom_size_widget)
            custom_size_row.setContentsMargins(0, 0, 0, 0)
            custom_size_row.setSpacing(8)
            custom_size_row.addWidget(self.dds_custom_width_spin)
            custom_size_row.addWidget(QLabel("x"))
            custom_size_row.addWidget(self.dds_custom_height_spin)
            custom_size_row.addStretch(1)

            dds_output_layout.addWidget(self.enable_dds_staging_checkbox, 0, 0, 1, 3)
            dds_output_layout.addWidget(self.dds_output_mode_hint, 1, 0, 1, 3)
            dds_output_layout.addWidget(self.dds_output_flow_hint, 2, 0, 1, 3)
            dds_output_layout.addWidget(QLabel("Format"), 3, 0)
            dds_output_layout.addWidget(self.dds_format_mode_combo, 3, 1)
            dds_output_layout.addWidget(self.dds_custom_format_label, 4, 0)
            dds_output_layout.addWidget(self.dds_custom_format_combo, 4, 1)
            dds_output_layout.addWidget(QLabel("Size"), 5, 0)
            dds_output_layout.addWidget(self.dds_size_mode_combo, 5, 1)
            dds_output_layout.addWidget(self.dds_custom_size_label, 6, 0)
            dds_output_layout.addWidget(self.dds_custom_size_widget, 6, 1, 1, 2)
            dds_output_layout.addWidget(QLabel("Mipmaps"), 7, 0)
            dds_output_layout.addWidget(self.dds_mip_mode_combo, 7, 1)
            dds_output_layout.addWidget(self.dds_custom_mip_label, 8, 0)
            dds_output_layout.addWidget(self.dds_custom_mip_spin, 8, 1)
            dds_output_layout.addWidget(self.dds_output_size_hint, 9, 0, 1, 3)

            self.dds_output_section.body_layout.addWidget(dds_output_group)
            left_layout.addWidget(self.dds_output_section)

            self.filters_section = CollapsibleSection("Workflow Profiles, Rules & Matches", expanded=False)
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

            texture_rules_label = QLabel("Per-file workflow matching")
            texture_rules_hint = QLabel(
                "Build reusable per-file workflow profiles, assign them with ordered rules, and inspect the live matched DDS set."
            )
            texture_rules_hint.setObjectName("HintLabel")
            texture_rules_hint.setWordWrap(True)
            texture_rules_hint.setToolTip(
                "Rules are evaluated top-to-bottom with last match wins. "
                "Use profiles for DDS output and direct NCNN overrides, and use rules to assign profiles by glob or exact relative path."
            )

            self.texture_rules_legacy_text = ""
            self.workflow_profiles_state: List[TextureWorkflowProfile] = []
            self.texture_rules_state: List[TextureRule] = []
            self.workflow_matched_processing_plan: List[TextureProcessingPlan] = []
            self._workflow_editor_syncing = False
            self._workflow_match_refresh_timer = QTimer(self)
            self._workflow_match_refresh_timer.setSingleShot(True)
            self._workflow_match_refresh_timer.setInterval(300)
            self._workflow_match_refresh_timer.timeout.connect(self._refresh_workflow_matched_files_view)

            profiles_group = QGroupBox("Workflow Profiles")
            profiles_layout = QVBoxLayout(profiles_group)
            profiles_layout.setContentsMargins(10, 10, 10, 10)
            profiles_layout.setSpacing(8)
            profiles_button_row = QHBoxLayout()
            profiles_button_row.setSpacing(6)
            self.workflow_profile_add_button = QPushButton("Add")
            self.workflow_profile_duplicate_button = QPushButton("Duplicate")
            self.workflow_profile_delete_button = QPushButton("Delete")
            profiles_button_row.addWidget(self.workflow_profile_add_button)
            profiles_button_row.addWidget(self.workflow_profile_duplicate_button)
            profiles_button_row.addWidget(self.workflow_profile_delete_button)
            profiles_button_row.addStretch(1)
            profiles_layout.addLayout(profiles_button_row)

            self.workflow_profiles_tree = QTreeWidget()
            self.workflow_profiles_tree.setRootIsDecorated(False)
            self.workflow_profiles_tree.setAlternatingRowColors(True)
            self.workflow_profiles_tree.setSelectionMode(QAbstractItemView.SingleSelection)
            self.workflow_profiles_tree.setHeaderLabels(["Name", "Action", "DDS Output", "NCNN"])
            self.workflow_profiles_tree.header().setStretchLastSection(False)
            self.workflow_profiles_tree.header().resizeSection(0, 180)
            self.workflow_profiles_tree.header().resizeSection(1, 130)
            self.workflow_profiles_tree.header().resizeSection(2, 220)
            self.workflow_profiles_tree.header().resizeSection(3, 240)
            profiles_layout.addWidget(self.workflow_profiles_tree)

            profile_detail_group = QGroupBox("Selected Profile")
            profile_detail_layout = QGridLayout(profile_detail_group)
            profile_detail_layout.setHorizontalSpacing(10)
            profile_detail_layout.setVerticalSpacing(8)
            self.workflow_profile_name_edit = QLineEdit()
            self.workflow_profile_action_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_action_combo, "Inherit Planner", "")
            self._add_combo_choice(self.workflow_profile_action_combo, "Upscale Then Rebuild", "upscale_then_rebuild")
            self._add_combo_choice(self.workflow_profile_action_combo, "Rebuild From PNG", "rebuild_from_png")
            self._add_combo_choice(self.workflow_profile_action_combo, "Preserve Original", "preserve_original")
            self._add_combo_choice(self.workflow_profile_action_combo, "Skip", "skip")
            self.workflow_profile_format_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_format_combo, "Inherit Main DDS Output", "")
            self._add_combo_choice(self.workflow_profile_format_combo, "Match Original DDS", DDS_FORMAT_MODE_MATCH_ORIGINAL)
            for texconv_format in SUPPORTED_TEXCONV_FORMAT_CHOICES:
                self._add_combo_choice(self.workflow_profile_format_combo, texconv_format, texconv_format)
            self.workflow_profile_size_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_size_combo, "Inherit Main DDS Output", "")
            self._add_combo_choice(self.workflow_profile_size_combo, "Match PNG Size", DDS_SIZE_MODE_PNG)
            self._add_combo_choice(self.workflow_profile_size_combo, "Match Original DDS", DDS_SIZE_MODE_ORIGINAL)
            self._add_combo_choice(self.workflow_profile_size_combo, "Custom Size", "__custom__")
            self.workflow_profile_custom_width_spin = QSpinBox()
            self.workflow_profile_custom_width_spin.setRange(1, 65535)
            self.workflow_profile_custom_height_spin = QSpinBox()
            self.workflow_profile_custom_height_spin.setRange(1, 65535)
            self.workflow_profile_custom_size_widget = QWidget()
            workflow_profile_custom_size_row = QHBoxLayout(self.workflow_profile_custom_size_widget)
            workflow_profile_custom_size_row.setContentsMargins(0, 0, 0, 0)
            workflow_profile_custom_size_row.setSpacing(6)
            workflow_profile_custom_size_row.addWidget(self.workflow_profile_custom_width_spin)
            workflow_profile_custom_size_row.addWidget(QLabel("x"))
            workflow_profile_custom_size_row.addWidget(self.workflow_profile_custom_height_spin)
            workflow_profile_custom_size_row.addStretch(1)
            self.workflow_profile_mip_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_mip_combo, "Inherit Main DDS Output", "")
            self._add_combo_choice(self.workflow_profile_mip_combo, "Match Original DDS", DDS_MIP_MODE_MATCH_ORIGINAL)
            self._add_combo_choice(self.workflow_profile_mip_combo, "Full Chain", DDS_MIP_MODE_FULL_CHAIN)
            self._add_combo_choice(self.workflow_profile_mip_combo, "Single Mip", DDS_MIP_MODE_SINGLE)
            self._add_combo_choice(self.workflow_profile_mip_combo, "Custom Mip Count", "__custom__")
            self.workflow_profile_custom_mip_spin = QSpinBox()
            self.workflow_profile_custom_mip_spin.setRange(1, 32)
            self.workflow_profile_ncnn_model_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_ncnn_model_combo, "Inherit Direct NCNN Model", "")
            self.workflow_profile_ncnn_scale_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_ncnn_scale_combo, "Inherit Direct NCNN Scale", "")
            self._add_combo_choice(self.workflow_profile_ncnn_scale_combo, "2x", "2")
            self._add_combo_choice(self.workflow_profile_ncnn_scale_combo, "3x", "3")
            self._add_combo_choice(self.workflow_profile_ncnn_scale_combo, "4x", "4")
            self.workflow_profile_ncnn_tile_override_checkbox = QCheckBox("Override tile")
            self.workflow_profile_ncnn_tile_spin = QSpinBox()
            self.workflow_profile_ncnn_tile_spin.setRange(0, 65535)
            self.workflow_profile_ncnn_extra_args_edit = QLineEdit()
            self.workflow_profile_post_correction_combo = QComboBox()
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Inherit Direct NCNN Correction", "")
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Off", UPSCALE_POST_CORRECTION_NONE)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Match Mean Luma", UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Match Levels", UPSCALE_POST_CORRECTION_MATCH_LEVELS)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Match Histogram", UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Source Match Balanced", UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Source Match Extended", UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED)
            self._add_combo_choice(self.workflow_profile_post_correction_combo, "Source Match Experimental", UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL)
            profile_detail_layout.addWidget(QLabel("Name"), 0, 0)
            profile_detail_layout.addWidget(self.workflow_profile_name_edit, 0, 1)
            profile_detail_layout.addWidget(QLabel("Action"), 0, 2)
            profile_detail_layout.addWidget(self.workflow_profile_action_combo, 0, 3)
            profile_detail_layout.addWidget(QLabel("DDS Format"), 1, 0)
            profile_detail_layout.addWidget(self.workflow_profile_format_combo, 1, 1)
            profile_detail_layout.addWidget(QLabel("DDS Size"), 1, 2)
            profile_detail_layout.addWidget(self.workflow_profile_size_combo, 1, 3)
            profile_detail_layout.addWidget(QLabel("Custom Size"), 2, 0)
            profile_detail_layout.addWidget(self.workflow_profile_custom_size_widget, 2, 1)
            profile_detail_layout.addWidget(QLabel("Mipmaps"), 2, 2)
            profile_detail_layout.addWidget(self.workflow_profile_mip_combo, 2, 3)
            profile_detail_layout.addWidget(QLabel("Custom Mips"), 3, 0)
            profile_detail_layout.addWidget(self.workflow_profile_custom_mip_spin, 3, 1)
            profile_detail_layout.addWidget(QLabel("Direct NCNN Model"), 4, 0)
            profile_detail_layout.addWidget(self.workflow_profile_ncnn_model_combo, 4, 1)
            profile_detail_layout.addWidget(QLabel("Direct NCNN Scale"), 4, 2)
            profile_detail_layout.addWidget(self.workflow_profile_ncnn_scale_combo, 4, 3)
            profile_detail_layout.addWidget(self.workflow_profile_ncnn_tile_override_checkbox, 5, 0)
            profile_detail_layout.addWidget(self.workflow_profile_ncnn_tile_spin, 5, 1)
            profile_detail_layout.addWidget(QLabel("NCNN Extra Args"), 5, 2)
            profile_detail_layout.addWidget(self.workflow_profile_ncnn_extra_args_edit, 5, 3)
            profile_detail_layout.addWidget(QLabel("Post Correction"), 6, 0)
            profile_detail_layout.addWidget(self.workflow_profile_post_correction_combo, 6, 1)
            profiles_layout.addWidget(profile_detail_group)

            rules_group = QGroupBox("Ordered Rules")
            rules_layout = QVBoxLayout(rules_group)
            rules_layout.setContentsMargins(10, 10, 10, 10)
            rules_layout.setSpacing(8)
            rules_button_row = QHBoxLayout()
            rules_button_row.setSpacing(6)
            self.workflow_rule_add_button = QPushButton("Add")
            self.workflow_rule_duplicate_button = QPushButton("Duplicate")
            self.workflow_rule_delete_button = QPushButton("Delete")
            self.workflow_rule_move_up_button = QPushButton("Move Up")
            self.workflow_rule_move_down_button = QPushButton("Move Down")
            rules_button_row.addWidget(self.workflow_rule_add_button)
            rules_button_row.addWidget(self.workflow_rule_duplicate_button)
            rules_button_row.addWidget(self.workflow_rule_delete_button)
            rules_button_row.addWidget(self.workflow_rule_move_up_button)
            rules_button_row.addWidget(self.workflow_rule_move_down_button)
            rules_button_row.addStretch(1)
            rules_layout.addLayout(rules_button_row)

            self.workflow_rules_tree = QTreeWidget()
            self.workflow_rules_tree.setRootIsDecorated(False)
            self.workflow_rules_tree.setAlternatingRowColors(True)
            self.workflow_rules_tree.setSelectionMode(QAbstractItemView.SingleSelection)
            self.workflow_rules_tree.setHeaderLabels(
                ["On", "Match", "Pattern", "Workflow", "Semantic", "Planner", "Color", "Alpha", "Path"]
            )
            self.workflow_rules_tree.header().resizeSection(0, 50)
            self.workflow_rules_tree.header().resizeSection(1, 60)
            self.workflow_rules_tree.header().resizeSection(2, 210)
            self.workflow_rules_tree.header().resizeSection(3, 120)
            self.workflow_rules_tree.header().resizeSection(4, 140)
            self.workflow_rules_tree.header().resizeSection(5, 120)
            self.workflow_rules_tree.header().resizeSection(6, 90)
            self.workflow_rules_tree.header().resizeSection(7, 120)
            self.workflow_rules_tree.header().resizeSection(8, 150)
            rules_layout.addWidget(self.workflow_rules_tree)

            rule_detail_group = QGroupBox("Selected Rule")
            rule_detail_layout = QGridLayout(rule_detail_group)
            rule_detail_layout.setHorizontalSpacing(10)
            rule_detail_layout.setVerticalSpacing(8)
            self.workflow_rule_enabled_checkbox = QCheckBox("Enabled")
            self.workflow_rule_match_mode_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_match_mode_combo, "Glob", "glob")
            self._add_combo_choice(self.workflow_rule_match_mode_combo, "Exact Path", "exact")
            self.workflow_rule_pattern_edit = QLineEdit()
            self.workflow_rule_profile_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_profile_combo, "No Workflow Profile", "")
            self.workflow_rule_semantic_combo = QComboBox()
            self.workflow_rule_semantic_combo.setEditable(True)
            self.workflow_rule_semantic_combo.addItems(
                [
                    "",
                    "color:albedo",
                    "normal:normal",
                    "mask:packed_mask",
                    "mask:opacity_mask",
                    "height:height",
                    "roughness:roughness",
                    "ui:ui",
                    "emissive:emissive",
                ]
            )
            self.workflow_rule_planner_profile_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_planner_profile_combo, "Inherit Planner Profile", "")
            for planner_profile_key in get_texture_processing_profile_keys():
                self._add_combo_choice(self.workflow_rule_planner_profile_combo, planner_profile_key, planner_profile_key)
            self.workflow_rule_planner_profile_combo.setToolTip(
                "Optional processing-profile override for this rule. Use the Docs button for an explanation of each planner profile."
            )
            self.workflow_rule_colorspace_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_colorspace_combo, "Inherit Colorspace", "")
            self._add_combo_choice(self.workflow_rule_colorspace_combo, "sRGB", "srgb")
            self._add_combo_choice(self.workflow_rule_colorspace_combo, "Linear", "linear")
            self._add_combo_choice(self.workflow_rule_colorspace_combo, "Match Source", "match_source")
            self.workflow_rule_alpha_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_alpha_combo, "Inherit Alpha Policy", "")
            self._add_combo_choice(self.workflow_rule_alpha_combo, "None", "none")
            self._add_combo_choice(self.workflow_rule_alpha_combo, "Straight", "straight")
            self._add_combo_choice(self.workflow_rule_alpha_combo, "Cutout Coverage", "cutout_coverage")
            self._add_combo_choice(self.workflow_rule_alpha_combo, "Channel Data", "channel_data")
            self._add_combo_choice(self.workflow_rule_alpha_combo, "Premultiplied", "premultiplied")
            self.workflow_rule_intermediate_combo = QComboBox()
            self._add_combo_choice(self.workflow_rule_intermediate_combo, "Inherit Planner Path", "")
            self._add_combo_choice(self.workflow_rule_intermediate_combo, "Visible Color PNG", "visible_color_png_path")
            self._add_combo_choice(self.workflow_rule_intermediate_combo, "Technical Preserve", "technical_preserve_path")
            self._add_combo_choice(self.workflow_rule_intermediate_combo, "Technical High Precision", "technical_high_precision_path")
            self.workflow_rule_intermediate_combo.setToolTip(
                "Optional planner-path override for this rule. Use the Docs button for path meanings and backend notes."
            )
            planner_profile_label_widget = QWidget()
            planner_profile_label_layout = QHBoxLayout(planner_profile_label_widget)
            planner_profile_label_layout.setContentsMargins(0, 0, 0, 0)
            planner_profile_label_layout.setSpacing(6)
            planner_profile_label_layout.addWidget(QLabel("Planner Profile"))
            self.workflow_rule_planner_profile_help_button = QPushButton("Docs")
            self.workflow_rule_planner_profile_help_button.setMaximumWidth(56)
            self.workflow_rule_planner_profile_help_button.setToolTip(
                "Open the in-app documentation topic that explains planner profiles."
            )
            planner_profile_label_layout.addWidget(self.workflow_rule_planner_profile_help_button)
            planner_profile_label_layout.addStretch(1)
            planner_path_label_widget = QWidget()
            planner_path_label_layout = QHBoxLayout(planner_path_label_widget)
            planner_path_label_layout.setContentsMargins(0, 0, 0, 0)
            planner_path_label_layout.setSpacing(6)
            planner_path_label_layout.addWidget(QLabel("Planner Path"))
            self.workflow_rule_planner_path_help_button = QPushButton("Docs")
            self.workflow_rule_planner_path_help_button.setMaximumWidth(56)
            self.workflow_rule_planner_path_help_button.setToolTip(
                "Open the in-app documentation topic that explains planner paths."
            )
            planner_path_label_layout.addWidget(self.workflow_rule_planner_path_help_button)
            planner_path_label_layout.addStretch(1)
            rule_detail_layout.addWidget(self.workflow_rule_enabled_checkbox, 0, 0)
            rule_detail_layout.addWidget(QLabel("Match"), 0, 1)
            rule_detail_layout.addWidget(self.workflow_rule_match_mode_combo, 0, 2)
            rule_detail_layout.addWidget(QLabel("Pattern"), 1, 0)
            rule_detail_layout.addWidget(self.workflow_rule_pattern_edit, 1, 1, 1, 3)
            rule_detail_layout.addWidget(QLabel("Workflow Profile"), 2, 0)
            rule_detail_layout.addWidget(self.workflow_rule_profile_combo, 2, 1)
            rule_detail_layout.addWidget(QLabel("Semantic"), 2, 2)
            rule_detail_layout.addWidget(self.workflow_rule_semantic_combo, 2, 3)
            rule_detail_layout.addWidget(planner_profile_label_widget, 3, 0)
            rule_detail_layout.addWidget(self.workflow_rule_planner_profile_combo, 3, 1)
            rule_detail_layout.addWidget(QLabel("Colorspace"), 3, 2)
            rule_detail_layout.addWidget(self.workflow_rule_colorspace_combo, 3, 3)
            rule_detail_layout.addWidget(QLabel("Alpha Policy"), 4, 0)
            rule_detail_layout.addWidget(self.workflow_rule_alpha_combo, 4, 1)
            rule_detail_layout.addWidget(planner_path_label_widget, 4, 2)
            rule_detail_layout.addWidget(self.workflow_rule_intermediate_combo, 4, 3)
            self.workflow_rule_planner_profile_help_button.clicked.connect(
                lambda: self.show_about_dialog(topic_id="workflow_planner_profiles")
            )
            self.workflow_rule_planner_path_help_button.clicked.connect(
                lambda: self.show_about_dialog(topic_id="workflow_planner_paths")
            )
            rules_layout.addWidget(rule_detail_group)

            matched_group = QGroupBox("Matched Files")
            matched_layout = QVBoxLayout(matched_group)
            matched_layout.setContentsMargins(10, 10, 10, 10)
            matched_layout.setSpacing(8)
            matched_button_row = QHBoxLayout()
            matched_button_row.setSpacing(6)
            self.workflow_matched_refresh_button = QPushButton("Refresh")
            self.workflow_assign_profile_button = QPushButton("Assign Profile")
            matched_button_row.addWidget(self.workflow_matched_refresh_button)
            matched_button_row.addWidget(self.workflow_assign_profile_button)
            matched_button_row.addStretch(1)
            matched_layout.addLayout(matched_button_row)
            self.workflow_matched_summary_label = QLabel(
                "This table follows the current Original DDS root and workflow filter. Exact-path assignments append new last-match rules."
            )
            self.workflow_matched_summary_label.setObjectName("HintLabel")
            self.workflow_matched_summary_label.setWordWrap(True)
            matched_layout.addWidget(self.workflow_matched_summary_label)
            self.workflow_matched_files_tree = QTreeWidget()
            self.workflow_matched_files_tree.setRootIsDecorated(False)
            self.workflow_matched_files_tree.setAlternatingRowColors(True)
            self.workflow_matched_files_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.workflow_matched_files_tree.setHeaderLabels(
                ["Path", "Semantic", "Rule", "Workflow", "DDS Output", "NCNN", "Action"]
            )
            self.workflow_matched_files_tree.header().resizeSection(0, 300)
            self.workflow_matched_files_tree.header().resizeSection(1, 150)
            self.workflow_matched_files_tree.header().resizeSection(2, 200)
            self.workflow_matched_files_tree.header().resizeSection(3, 150)
            self.workflow_matched_files_tree.header().resizeSection(4, 200)
            self.workflow_matched_files_tree.header().resizeSection(5, 220)
            self.workflow_matched_files_tree.header().resizeSection(6, 150)
            matched_layout.addWidget(self.workflow_matched_files_tree)

            filters_layout.addWidget(filters_label)
            filters_layout.addWidget(filters_hint)
            filters_layout.addWidget(self.filters_edit)
            filters_layout.addWidget(texture_rules_label)
            filters_layout.addWidget(texture_rules_hint)
            filters_layout.addWidget(profiles_group)
            filters_layout.addWidget(rules_group)
            filters_layout.addWidget(matched_group)
            self.filters_section.body_layout.addWidget(filters_group)
            left_layout.addWidget(self.filters_section)

            self.chainner_section = CollapsibleSection("Upscaling", expanded=False)
            upscale_group = QWidget()
            upscale_layout = QVBoxLayout(upscale_group)
            upscale_layout.setContentsMargins(0, 0, 0, 0)
            upscale_layout.setSpacing(8)

            upscale_backend_grid = QGridLayout()
            upscale_backend_grid.setHorizontalSpacing(10)
            upscale_backend_grid.setVerticalSpacing(8)
            upscale_backend_grid.setColumnMinimumWidth(0, 136)
            upscale_backend_grid.setColumnStretch(1, 1)
            self.upscale_backend_combo = QComboBox()
            self._add_combo_choice(self.upscale_backend_combo, "Disabled", UPSCALE_BACKEND_NONE)
            self._add_combo_choice(self.upscale_backend_combo, "chaiNNer", UPSCALE_BACKEND_CHAINNER)
            self._add_combo_choice(self.upscale_backend_combo, "Real-ESRGAN NCNN", UPSCALE_BACKEND_REALESRGAN_NCNN)
            self.safe_upscale_wizard_button = QPushButton("Run Summary")
            self.safe_upscale_wizard_button.setToolTip(
                "Open a read-only summary of the current sources, backend, texture policy, and direct upscale settings before running."
            )
            upscale_backend_grid.addWidget(QLabel("Backend"), 0, 0)
            upscale_backend_grid.addWidget(self.upscale_backend_combo, 0, 1)
            upscale_backend_grid.addWidget(self.safe_upscale_wizard_button, 0, 2)
            upscale_layout.addLayout(upscale_backend_grid)

            upscale_hint = QLabel(
                "Choose one optional upscaling backend. Texture Policy below still applies before DDS rebuild, while scale/tile controls only appear for the direct NCNN backend."
            )
            upscale_hint.setObjectName("HintLabel")
            upscale_hint.setWordWrap(True)
            upscale_layout.addWidget(upscale_hint)

            self.upscale_backend_stack = QStackedWidget()
            self.upscale_backend_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            upscale_layout.addWidget(self.upscale_backend_stack)

            upscale_none_page = QWidget()
            upscale_none_page.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            upscale_none_layout = QVBoxLayout(upscale_none_page)
            upscale_none_layout.setContentsMargins(0, 0, 0, 0)
            upscale_none_layout.setSpacing(8)
            no_upscale_hint = QLabel(
                "Disabled: the app will rebuild DDS from the existing PNG root. If DDS-to-PNG conversion is enabled, Start stops after PNG creation."
            )
            no_upscale_hint.setObjectName("HintLabel")
            no_upscale_hint.setWordWrap(True)
            upscale_none_layout.addWidget(no_upscale_hint)
            self.upscale_backend_stack.addWidget(upscale_none_page)

            chainner_page = QWidget()
            chainner_page.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            chainner_layout = QVBoxLayout(chainner_page)
            chainner_layout.setContentsMargins(0, 0, 0, 0)
            chainner_layout.setSpacing(8)

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

            chainner_hint = QLabel("Optional override JSON. Supports app path tokens.")
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
            self.upscale_backend_stack.addWidget(chainner_page)

            ncnn_page = QWidget()
            ncnn_page.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            ncnn_layout = QVBoxLayout(ncnn_page)
            ncnn_layout.setContentsMargins(0, 0, 0, 0)
            ncnn_layout.setSpacing(8)

            ncnn_paths_layout = QGridLayout()
            ncnn_paths_layout.setHorizontalSpacing(10)
            ncnn_paths_layout.setVerticalSpacing(10)
            ncnn_paths_layout.setColumnMinimumWidth(0, 136)
            ncnn_paths_layout.setColumnStretch(1, 1)
            self.ncnn_exe_path_edit = QLineEdit()
            self.ncnn_model_dir_edit = QLineEdit()
            self.ncnn_exe_browse_button = self._add_path_row(
                ncnn_paths_layout,
                0,
                "NCNN exe path",
                self.ncnn_exe_path_edit,
                self._browse_ncnn_exe_path,
            )
            self.ncnn_model_dir_browse_button = self._add_path_row(
                ncnn_paths_layout,
                1,
                "Model folder",
                self.ncnn_model_dir_edit,
                self._browse_ncnn_model_dir,
            )
            ncnn_layout.addLayout(ncnn_paths_layout)

            ncnn_options_layout = QGridLayout()
            ncnn_options_layout.setHorizontalSpacing(10)
            ncnn_options_layout.setVerticalSpacing(8)
            ncnn_options_layout.setColumnMinimumWidth(0, 136)
            ncnn_options_layout.setColumnStretch(1, 1)

            self.ncnn_model_combo = QComboBox()
            self.ncnn_model_refresh_button = QPushButton("Refresh Models")
            self.ncnn_model_catalog_button = QPushButton("Catalog")
            self.ncnn_model_catalog_button.setToolTip(
                "Browse grouped NCNN model recommendations with short descriptions, source pages, and non-downloading model pages."
            )
            model_row = QHBoxLayout()
            model_row.setContentsMargins(0, 0, 0, 0)
            model_row.setSpacing(8)
            model_row.addWidget(self.ncnn_model_combo, stretch=1)
            model_row.addWidget(self.ncnn_model_refresh_button)
            model_row.addWidget(self.ncnn_model_catalog_button)

            ncnn_options_layout.addWidget(QLabel("Model"), 0, 0)
            ncnn_options_layout.addLayout(model_row, 0, 1)
            ncnn_layout.addLayout(ncnn_options_layout)

            self.upscale_backend_stack.addWidget(ncnn_page)

            self.ncnn_scale_spin = QSpinBox()
            self.ncnn_scale_spin.setRange(1, 8)
            self.ncnn_tile_size_spin = QSpinBox()
            self.ncnn_tile_size_spin.setRange(0, 32768)
            self.ncnn_tile_size_spin.setSingleStep(32)
            self.ncnn_extra_args_edit = QLineEdit()
            self.upscale_post_correction_combo = QComboBox()
            self._add_combo_choice(self.upscale_post_correction_combo, "Off", UPSCALE_POST_CORRECTION_NONE)
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Match Mean Luma",
                UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
            )
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Match Levels",
                UPSCALE_POST_CORRECTION_MATCH_LEVELS,
            )
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Match Histogram",
                UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
            )
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Source Match Balanced (recommended)",
                UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
            )
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Source Match Extended",
                UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
            )
            self._add_combo_choice(
                self.upscale_post_correction_combo,
                "Source Match Experimental",
                UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
            )
            self.upscale_texture_preset_combo = QComboBox()
            self._add_combo_choice(self.upscale_texture_preset_combo, "Balanced mixed textures (recommended)", UPSCALE_TEXTURE_PRESET_BALANCED)
            self._add_combo_choice(self.upscale_texture_preset_combo, "Color + UI only (safer)", UPSCALE_TEXTURE_PRESET_COLOR_UI)
            self._add_combo_choice(self.upscale_texture_preset_combo, "Color + UI + emissive", UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE)
            self._add_combo_choice(self.upscale_texture_preset_combo, "All textures (advanced)", UPSCALE_TEXTURE_PRESET_ALL)
            self.enable_automatic_texture_rules_checkbox = QCheckBox("Use automatic texture safety rules")
            self.enable_unsafe_technical_override_checkbox = QCheckBox(
                "Expert override: force technical maps through PNG/upscale path (unsafe)"
            )
            self.retry_smaller_tile_checkbox = QCheckBox("Retry with smaller tile on failure")
            self.enable_mod_ready_loose_export_checkbox = QCheckBox("Create ready mod package after rebuild")
            self.mod_ready_export_root_edit = QLineEdit()
            self.mod_ready_export_browse_button = QPushButton("Browse")
            self.mod_ready_create_no_encrypt_checkbox = QCheckBox("Create .no_encrypt file")
            self.mod_ready_create_no_encrypt_checkbox.setChecked(MOD_READY_CREATE_NO_ENCRYPT)
            self.mod_ready_package_title_edit = QLineEdit()
            self.mod_ready_package_version_edit = QLineEdit()
            self.mod_ready_package_author_edit = QLineEdit()
            self.mod_ready_package_description_edit = QLineEdit()
            self.mod_ready_package_nexus_url_edit = QLineEdit()
            self.mod_ready_package_title_edit.setPlaceholderText(MOD_READY_PACKAGE_TITLE)
            self.mod_ready_package_version_edit.setPlaceholderText(MOD_READY_PACKAGE_VERSION)
            self.mod_ready_package_nexus_url_edit.setPlaceholderText("https://www.nexusmods.com/...")
            self.ncnn_scale_spin.setToolTip(
                "Final PNG scale for direct backends. For predictable results, keep this close to the selected model's intended scale."
            )
            self.ncnn_tile_size_spin.setToolTip(
                "Tile size for direct backends. 0 means no manual tiling. Smaller values use less VRAM and can recover from failures, but run slower."
            )
            self.ncnn_extra_args_edit.setToolTip(
                "Optional extra command-line arguments appended to the Real-ESRGAN NCNN call. "
                "Example: -dn 0.2. Use only flags supported by the selected NCNN build/model."
            )
            self.ncnn_extra_args_edit.setPlaceholderText('Example: -dn 0.2')
            self.upscale_post_correction_combo.setToolTip(
                "Optional post-upscale correction applied after a direct backend writes the final PNG and before DDS rebuild. "
                "Source Match modes automatically decide per texture whether to apply visible RGB correction, scalar grayscale correction, limited RGB-only correction, or a full skip."
            )
            self.upscale_texture_preset_combo.setToolTip(
                "Controls which texture types are allowed into the PNG/upscale path and which ones are copied through unchanged."
            )
            self.enable_automatic_texture_rules_checkbox.setToolTip(
                "Applies safer DDS rebuild recommendations for format flags, alpha handling, and technical-map preservation. "
                "This is a safety/policy feature, not a brightness correction feature."
            )
            self.enable_unsafe_technical_override_checkbox.setToolTip(
                "Expert-only override. Forces technical textures such as normals, masks, roughness, height, and vectors onto the generic visible-color PNG/upscale path "
                "instead of preserving them. This can produce broken normals, bad masks, or incorrect shading."
            )

            self.texture_policy_group = QGroupBox("Texture Policy")
            policy_layout = QGridLayout(self.texture_policy_group)
            policy_layout.setHorizontalSpacing(10)
            policy_layout.setVerticalSpacing(8)
            policy_layout.setColumnMinimumWidth(0, 136)
            policy_layout.setColumnStretch(1, 1)

            policy_layout.addWidget(QLabel("Preset"), 0, 0)
            policy_layout.addWidget(self.upscale_texture_preset_combo, 0, 1)
            policy_layout.addWidget(self.enable_automatic_texture_rules_checkbox, 1, 0, 1, 2)
            policy_layout.addWidget(self.enable_unsafe_technical_override_checkbox, 2, 0, 1, 2)
            policy_layout.addWidget(self.enable_mod_ready_loose_export_checkbox, 3, 0, 1, 2)
            policy_layout.addWidget(QLabel("Mod package parent root"), 4, 0)
            loose_export_row = QHBoxLayout()
            loose_export_row.setContentsMargins(0, 0, 0, 0)
            loose_export_row.setSpacing(8)
            loose_export_row.addWidget(self.mod_ready_export_root_edit, stretch=1)
            loose_export_row.addWidget(self.mod_ready_export_browse_button)
            policy_layout.addLayout(loose_export_row, 4, 1)
            self.mod_ready_package_group = QGroupBox("Mod Package Metadata")
            mod_package_layout = QGridLayout(self.mod_ready_package_group)
            mod_package_layout.setHorizontalSpacing(10)
            mod_package_layout.setVerticalSpacing(8)
            mod_package_layout.setColumnMinimumWidth(0, 136)
            mod_package_layout.setColumnStretch(1, 1)
            mod_package_layout.addWidget(QLabel("Title"), 0, 0)
            mod_package_layout.addWidget(self.mod_ready_package_title_edit, 0, 1)
            mod_package_layout.addWidget(QLabel("Version"), 1, 0)
            mod_package_layout.addWidget(self.mod_ready_package_version_edit, 1, 1)
            mod_package_layout.addWidget(QLabel("Author"), 2, 0)
            mod_package_layout.addWidget(self.mod_ready_package_author_edit, 2, 1)
            mod_package_layout.addWidget(QLabel("Description"), 3, 0)
            mod_package_layout.addWidget(self.mod_ready_package_description_edit, 3, 1)
            mod_package_layout.addWidget(QLabel("Nexus URL"), 4, 0)
            mod_package_layout.addWidget(self.mod_ready_package_nexus_url_edit, 4, 1)
            mod_package_layout.addWidget(self.mod_ready_create_no_encrypt_checkbox, 5, 0, 1, 2)
            self.mod_ready_package_group.setVisible(False)
            policy_layout.addWidget(self.mod_ready_package_group, 5, 0, 1, 2)

            self.texture_policy_hint_label = QLabel()
            self.texture_policy_hint_label.setObjectName("HintLabel")
            self.texture_policy_hint_label.setWordWrap(True)
            policy_layout.addWidget(self.texture_policy_hint_label, 6, 0, 1, 2)
            upscale_layout.addWidget(self.texture_policy_group)

            self.direct_backend_controls_group = QGroupBox("Direct Upscale Controls (NCNN only)")
            direct_layout = QGridLayout(self.direct_backend_controls_group)
            direct_layout.setHorizontalSpacing(10)
            direct_layout.setVerticalSpacing(8)
            direct_layout.setColumnMinimumWidth(0, 136)
            direct_layout.setColumnStretch(1, 1)

            direct_layout.addWidget(QLabel("Scale"), 0, 0)
            direct_layout.addWidget(self.ncnn_scale_spin, 0, 1)
            direct_layout.addWidget(QLabel("Tile size"), 1, 0)
            direct_layout.addWidget(self.ncnn_tile_size_spin, 1, 1)
            direct_layout.addWidget(QLabel("NCNN extra args"), 2, 0)
            direct_layout.addWidget(self.ncnn_extra_args_edit, 2, 1)
            direct_layout.addWidget(QLabel("Post correction"), 3, 0)
            direct_layout.addWidget(self.upscale_post_correction_combo, 3, 1)
            direct_layout.addWidget(self.retry_smaller_tile_checkbox, 4, 0, 1, 2)

            self.direct_backend_hint_label = QLabel()
            self.direct_backend_hint_label.setObjectName("HintLabel")
            self.direct_backend_hint_label.setWordWrap(True)
            direct_layout.addWidget(self.direct_backend_hint_label, 5, 0, 1, 2)
            upscale_layout.addWidget(self.direct_backend_controls_group)

            self.safe_wizard_help_label = QLabel(
                "Start always uses the current settings shown in Texture Workflow. "
                "Run Summary is optional and shows the current sources, backend, and policy without duplicating those controls."
            )
            self.safe_wizard_help_label.setObjectName("HintLabel")
            self.safe_wizard_help_label.setWordWrap(True)
            self.safe_wizard_help_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            upscale_layout.addWidget(self.safe_wizard_help_label)

            self.chainner_section.body_layout.addWidget(upscale_group)
            left_layout.addWidget(self.chainner_section)
            left_layout.addStretch(1)

            self.progress_group = QGroupBox("Progress")
            progress_layout = QGridLayout(self.progress_group)
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
            self.progress_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            self.progress_group_min_height = max(170, self.progress_group.sizeHint().height())
            self.progress_group.setMinimumHeight(self.progress_group_min_height)
            self.workflow_right_splitter.addWidget(self.progress_group)

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

            self.compare_tab = QWidget()
            compare_tab_layout = QVBoxLayout(self.compare_tab)
            compare_tab_layout.setContentsMargins(4, 8, 4, 4)
            compare_tab_layout.setSpacing(6)

            compare_header = QHBoxLayout()
            compare_header.setSpacing(8)
            self.compare_previous_button = QPushButton("Previous")
            self.compare_next_button = QPushButton("Next")
            self.compare_sync_pan_checkbox = QCheckBox("Sync Pan")
            self.compare_sync_pan_checkbox.setChecked(True)
            compare_preview_size_label = QLabel("Preview size")
            self.compare_preview_size_combo = QComboBox()
            self._add_combo_choice(self.compare_preview_size_combo, "Fit", "fit:1.00")
            self._add_combo_choice(self.compare_preview_size_combo, "Fit 125%", "fit:1.25")
            self._add_combo_choice(self.compare_preview_size_combo, "Fit 150%", "fit:1.50")
            self._add_combo_choice(self.compare_preview_size_combo, "Fit 175%", "fit:1.75")
            self._add_combo_choice(self.compare_preview_size_combo, "Fit 200%", "fit:2.00")
            self.compare_preview_size_combo.setToolTip(
                "Apply the same preview size to both compare panes. "
                "Larger fit sizes keep the side-by-side view but let you pan if the image exceeds the viewport."
            )
            self.compare_mip_details_button = QPushButton("Mip Details")
            self.compare_mip_details_button.setToolTip(
                "Refresh Research, open Texture Analysis, and jump to the current compare file's mip details."
            )
            self.compare_open_in_editor_button = QPushButton("Open In Texture Editor")
            self.refresh_compare_button = QPushButton("Refresh")
            self.refresh_compare_button.setToolTip("Refresh the compare list and current previews.")
            compare_header.addWidget(compare_preview_size_label)
            compare_header.addWidget(self.compare_preview_size_combo)
            compare_header.addWidget(self.compare_mip_details_button)
            compare_header.addWidget(self.compare_open_in_editor_button)
            compare_header.addStretch(1)
            compare_header.addWidget(self.compare_previous_button)
            compare_header.addWidget(self.compare_next_button)
            compare_header.addWidget(self.compare_sync_pan_checkbox)
            compare_header.addWidget(self.refresh_compare_button)
            compare_tab_layout.addLayout(compare_header)

            self.compare_splitter = QSplitter(Qt.Horizontal)
            self.compare_splitter.setChildrenCollapsible(False)

            self.compare_list = QListWidget()
            self.compare_list.setSelectionMode(QAbstractItemView.SingleSelection)
            self.compare_list.setMinimumWidth(220)
            self.compare_splitter.addWidget(self.compare_list)

            preview_container = QWidget()
            preview_layout = QHBoxLayout(preview_container)
            preview_layout.setContentsMargins(0, 0, 0, 0)
            preview_layout.setSpacing(8)

            original_preview_column = QVBoxLayout()
            original_preview_column.setContentsMargins(6, 0, 3, 0)
            original_preview_column.setSpacing(4)
            original_preview_header_row = QHBoxLayout()
            original_preview_header_row.setSpacing(6)
            original_preview_title = QLabel("Original DDS")
            self.original_compare_zoom_out_button = QPushButton("-")
            self.original_compare_zoom_out_button.setToolTip("Zoom out.")
            self.original_compare_zoom_fit_button = QPushButton("Fit")
            self.original_compare_zoom_fit_button.setToolTip("Fit the preview to the available space.")
            self.original_compare_zoom_100_button = QPushButton("100%")
            self.original_compare_zoom_100_button.setToolTip("Show the preview at 100% zoom.")
            self.original_compare_zoom_in_button = QPushButton("+")
            self.original_compare_zoom_in_button.setToolTip("Zoom in.")
            self.original_compare_zoom_value = QLabel("Fit")
            self.original_compare_zoom_value.setObjectName("HintLabel")
            original_preview_header_row.addWidget(original_preview_title)
            original_preview_header_row.addStretch(1)
            original_preview_header_row.addWidget(self.original_compare_zoom_out_button)
            original_preview_header_row.addWidget(self.original_compare_zoom_fit_button)
            original_preview_header_row.addWidget(self.original_compare_zoom_100_button)
            original_preview_header_row.addWidget(self.original_compare_zoom_in_button)
            original_preview_header_row.addWidget(self.original_compare_zoom_value)
            self.original_preview_meta_label = QLabel("")
            self.original_preview_meta_label.setObjectName("HintLabel")
            self.original_preview_meta_label.setWordWrap(True)
            self.original_preview_label = PreviewLabel("Select a DDS file to preview.")
            self.original_preview_scroll = PreviewScrollArea()
            self.original_preview_scroll.setWidgetResizable(False)
            self.original_preview_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.original_preview_scroll.setWidget(self.original_preview_label)
            self.original_preview_label.attach_scroll_area(self.original_preview_scroll)
            self.original_preview_label.set_wheel_zoom_handler(
                lambda step: self._adjust_compare_zoom("original", step)
            )
            original_preview_column.addLayout(original_preview_header_row)
            original_preview_column.addWidget(self.original_preview_meta_label)
            original_preview_column.addWidget(self.original_preview_scroll, stretch=1)

            output_preview_column = QVBoxLayout()
            output_preview_column.setContentsMargins(3, 0, 6, 0)
            output_preview_column.setSpacing(4)
            output_preview_header_row = QHBoxLayout()
            output_preview_header_row.setSpacing(6)
            output_preview_title = QLabel("Output DDS")
            self.output_compare_zoom_out_button = QPushButton("-")
            self.output_compare_zoom_out_button.setToolTip("Zoom out.")
            self.output_compare_zoom_fit_button = QPushButton("Fit")
            self.output_compare_zoom_fit_button.setToolTip("Fit the preview to the available space.")
            self.output_compare_zoom_100_button = QPushButton("100%")
            self.output_compare_zoom_100_button.setToolTip("Show the preview at 100% zoom.")
            self.output_compare_zoom_in_button = QPushButton("+")
            self.output_compare_zoom_in_button.setToolTip("Zoom in.")
            self.output_compare_zoom_value = QLabel("Fit")
            self.output_compare_zoom_value.setObjectName("HintLabel")
            output_preview_header_row.addWidget(output_preview_title)
            output_preview_header_row.addStretch(1)
            output_preview_header_row.addWidget(self.output_compare_zoom_out_button)
            output_preview_header_row.addWidget(self.output_compare_zoom_fit_button)
            output_preview_header_row.addWidget(self.output_compare_zoom_100_button)
            output_preview_header_row.addWidget(self.output_compare_zoom_in_button)
            output_preview_header_row.addWidget(self.output_compare_zoom_value)
            self.output_preview_meta_label = QLabel("")
            self.output_preview_meta_label.setObjectName("HintLabel")
            self.output_preview_meta_label.setWordWrap(True)
            self.output_preview_label = PreviewLabel("Select a DDS file to preview.")
            self.output_preview_scroll = PreviewScrollArea()
            self.output_preview_scroll.setWidgetResizable(False)
            self.output_preview_scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.output_preview_scroll.setWidget(self.output_preview_label)
            self.output_preview_label.attach_scroll_area(self.output_preview_scroll)
            self.output_preview_label.set_wheel_zoom_handler(
                lambda step: self._adjust_compare_zoom("output", step)
            )
            output_preview_column.addLayout(output_preview_header_row)
            output_preview_column.addWidget(self.output_preview_meta_label)
            output_preview_column.addWidget(self.output_preview_scroll, stretch=1)

            preview_layout.addLayout(original_preview_column, stretch=1)
            preview_layout.addLayout(output_preview_column, stretch=1)
            self.compare_splitter.addWidget(preview_container)
            self.compare_splitter.setStretchFactor(0, 1)
            self.compare_splitter.setStretchFactor(1, 3)

            compare_tab_layout.addWidget(self.compare_splitter, stretch=1)
            self.content_tabs.addTab(self.compare_tab, "Compare")

            self.workflow_right_splitter.addWidget(self.content_tabs)
            self.workflow_right_splitter.setStretchFactor(0, 0)
            self.workflow_right_splitter.setStretchFactor(1, 1)
            right_layout.addWidget(self.workflow_right_splitter, stretch=1)

            button_row = QHBoxLayout()
            button_row.setSpacing(8)
            self.scan_button = QPushButton("Scan")
            self.preview_policy_button = QPushButton("Preview Policy")
            self.preview_policy_button.setToolTip(
                "Show the current per-texture processing plan before running Start."
            )
            self.clear_workflow_roots_button = QPushButton("Clear Workflow Roots...")
            self.start_button = QPushButton("Start")
            self.stop_button = QPushButton("Stop")
            self.open_output_button = QPushButton("Open Output")
            self.stop_button.setEnabled(False)
            button_row.addWidget(self.scan_button)
            button_row.addWidget(self.preview_policy_button)
            button_row.addWidget(self.clear_workflow_roots_button)
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

            archive_controls_group = FlatSectionPanel("Archive Controls")
            archive_controls_group.setMinimumWidth(320)
            archive_controls_group.setMaximumWidth(460)
            archive_controls_layout = archive_controls_group.body_layout
            archive_controls_layout.setContentsMargins(12, 12, 12, 12)
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
            self.archive_filter_edit.setPlaceholderText("Include path filter or glob, e.g. wood or */texture/*")
            self.archive_extension_filter_combo = QComboBox()
            self.archive_extension_filter_combo.setToolTip(
                "Filter by extension. The dropdown is rebuilt from the currently loaded archive index."
            )
            self._rebuild_archive_extension_filter_choices(ARCHIVE_EXTENSION_FILTER)
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
            self._add_combo_choice(self.archive_role_filter_combo, "Base / likely albedo images", "image")
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
            self.archive_tree_view_checkbox = QCheckBox("Tree View")
            self.archive_tree_view_checkbox.setChecked(True)
            self.archive_filter_apply_button = QPushButton("Apply")
            self.archive_filter_clear_button = QPushButton("Clear")
            self.archive_role_filter_combo.setToolTip("Filter by likely asset role. 'Base / likely albedo images' tries to keep base/color-style entries and hide common companion-map suffixes.")
            self.archive_min_size_spin.setToolTip("Hide very small files below this original size.")
            self.archive_package_filter_edit.setToolTip("Limit results to matching package names or pamt paths.")
            self.archive_previewable_only_checkbox.setToolTip("Show only files the built-in preview can handle.")
            self.archive_tree_view_checkbox.setToolTip("Show archive files inside folder nodes. Turn this off to list every file directly.")
            archive_package_filter_label = QLabel("Package")
            archive_package_filter_label.setObjectName("HintLabel")
            archive_package_filter_row.addWidget(archive_package_filter_label)
            archive_package_filter_row.addWidget(self.archive_package_filter_edit, stretch=1)
            archive_controls_layout.addLayout(archive_package_filter_row)

            archive_exclude_filter_row = QHBoxLayout()
            archive_exclude_filter_row.setSpacing(8)
            archive_exclude_filter_label = QLabel("Exclude")
            archive_exclude_filter_label.setObjectName("HintLabel")
            self.archive_exclude_filter_edit = QLineEdit()
            self.archive_exclude_filter_edit.setPlaceholderText("Exclude substrings or globs, e.g. *_n.dds; *_sp.dds; *_d.dds; *_dmap.dds")
            self.archive_exclude_filter_edit.setToolTip(
                "Exclude matching archive paths or basenames. Supports semicolon-separated substrings or glob patterns."
            )
            self.archive_exclude_common_technical_checkbox = QCheckBox("Hide common companion DDS suffixes")
            self.archive_exclude_common_technical_checkbox.setToolTip(
                "Also excludes common companion-map suffixes such as *_n.dds, *_wn.dds, *_sp.dds, *_m.dds, *_ma.dds, *_mg.dds, *_d.dds, *_dmap.dds, *_op.dds, *_pivotpos.dds, *_1bit.dds, *_mask_amg.dds, and similar patterns."
            )
            archive_exclude_filter_row.addWidget(archive_exclude_filter_label)
            archive_exclude_filter_row.addWidget(self.archive_exclude_filter_edit, stretch=1)
            archive_exclude_filter_row.addWidget(self.archive_exclude_common_technical_checkbox)
            archive_controls_layout.addLayout(archive_exclude_filter_row)

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
            archive_secondary_filter_row.addWidget(self.archive_tree_view_checkbox)
            archive_controls_layout.addLayout(archive_secondary_filter_row)

            archive_secondary_actions_row = QHBoxLayout()
            archive_secondary_actions_row.setSpacing(8)
            archive_secondary_actions_row.addStretch(1)
            archive_secondary_actions_row.addWidget(self.archive_filter_apply_button)
            archive_secondary_actions_row.addWidget(self.archive_filter_clear_button)
            archive_controls_layout.addLayout(archive_secondary_actions_row)

            self.archive_package_filter_hint_label = QLabel(
                "Scan uses a saved archive cache when valid. Refresh ignores the cache and rebuilds it from the .pamt files. "
                "Exclude accepts semicolon-separated substrings or globs, so you can search for broad names like 'wood' while hiding suffix variants."
            )
            self.archive_package_filter_hint_label.setObjectName("HintLabel")
            self.archive_package_filter_hint_label.setWordWrap(True)
            archive_controls_layout.addWidget(self.archive_package_filter_hint_label)

            archive_actions_row = QGridLayout()
            archive_actions_row.setHorizontalSpacing(8)
            archive_actions_row.setVerticalSpacing(8)
            self.archive_extract_selected_button = QPushButton("Extract Selected")
            self.archive_extract_filtered_button = QPushButton("Extract Filtered")
            self.archive_extract_to_workflow_button = QPushButton("DDS To Workflow")
            self.archive_open_in_editor_button = QPushButton("Open in Texture Editor")
            self.archive_resolve_in_research_button = QPushButton("Resolve In Research")
            self.archive_extract_to_workflow_button.setToolTip(
                "If one or more archive files/folders are selected, only selected DDS files are extracted to the workflow root. "
                "If nothing is selected, all DDS files from the current filtered view are used."
            )
            for button in (
                self.archive_extract_selected_button,
                self.archive_extract_filtered_button,
                self.archive_extract_to_workflow_button,
                self.archive_open_in_editor_button,
                self.archive_resolve_in_research_button,
            ):
                button.setMinimumHeight(28)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            archive_actions_row.addWidget(self.archive_extract_selected_button, 0, 0)
            archive_actions_row.addWidget(self.archive_extract_filtered_button, 0, 1)
            archive_actions_row.addWidget(self.archive_extract_to_workflow_button, 1, 0)
            archive_actions_row.addWidget(self.archive_open_in_editor_button, 1, 1)
            archive_actions_row.addWidget(self.archive_resolve_in_research_button, 2, 0, 1, 2)
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
            self.archive_controls_scroll.setMinimumWidth(320)
            self.archive_controls_scroll.setMaximumWidth(500)
            archive_controls_wrapper = QWidget()
            archive_controls_wrapper_layout = QVBoxLayout(archive_controls_wrapper)
            archive_controls_wrapper_layout.setContentsMargins(0, 0, 0, 0)
            archive_controls_wrapper_layout.setSpacing(0)
            archive_controls_wrapper_layout.addWidget(archive_controls_group)
            archive_controls_wrapper_layout.addStretch(1)
            self.archive_controls_scroll.setWidget(archive_controls_wrapper)
            self.archive_splitter.addWidget(self.archive_controls_scroll)

            archive_files_group = FlatSectionPanel("Archive Files")
            archive_files_group.setMinimumWidth(260)
            archive_files_layout = archive_files_group.body_layout
            archive_files_layout.setSpacing(0)

            self.archive_tree = QTreeWidget()
            self.archive_tree.setHeaderLabels(["Name", "Type", "Size", "Stored", "Comp", "Package"])
            self.archive_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.archive_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.archive_tree.setAlternatingRowColors(False)
            self.archive_tree.setRootIsDecorated(True)
            self.archive_tree.setUniformRowHeights(True)
            self.archive_tree.setContextMenuPolicy(Qt.CustomContextMenu)
            archive_header = self.archive_tree.header()
            archive_header.setStretchLastSection(False)
            archive_header.setSectionsMovable(False)
            for section in range(self.archive_tree.columnCount()):
                archive_header.setSectionResizeMode(section, QHeaderView.Interactive)
            archive_header.resizeSection(0, 480)
            archive_header.resizeSection(1, 78)
            archive_header.resizeSection(2, 88)
            archive_header.resizeSection(3, 88)
            archive_header.resizeSection(4, 72)
            archive_header.resizeSection(5, 130)
            archive_files_layout.addWidget(self.archive_tree)
            self.archive_splitter.addWidget(archive_files_group)

            archive_preview_group = FlatSectionPanel("Archive Preview")
            archive_preview_group.setMinimumWidth(300)
            archive_preview_container_layout = archive_preview_group.body_layout
            archive_preview_container_layout.setSpacing(10)

            archive_preview_header = QVBoxLayout()
            archive_preview_header.setSpacing(8)
            archive_preview_title_row = QHBoxLayout()
            archive_preview_title_row.setSpacing(8)
            self.archive_preview_title_label = QLabel("Select an archive file")
            self.archive_preview_title_label.setWordWrap(True)
            self.archive_preview_warning_badge = QLabel("")
            self.archive_preview_warning_badge.setObjectName("WarningBadge")
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_loose_toggle_button = QPushButton("Loose File")
            self.archive_preview_loose_toggle_button.setToolTip("Switch between the archive preview and the matching loose file preview.")
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_zoom_out_button = QPushButton("-")
            self.archive_preview_zoom_out_button.setToolTip("Zoom out.")
            self.archive_preview_zoom_fit_button = QPushButton("Fit")
            self.archive_preview_zoom_fit_button.setToolTip("Fit the preview to the available space.")
            self.archive_preview_zoom_100_button = QPushButton("100%")
            self.archive_preview_zoom_100_button.setToolTip("Show the preview at 100% zoom.")
            self.archive_preview_zoom_in_button = QPushButton("+")
            self.archive_preview_zoom_in_button.setToolTip("Zoom in.")
            self.archive_preview_zoom_value = QLabel("Fit")
            self.archive_preview_zoom_value.setObjectName("HintLabel")
            self.archive_model_texture_toggle = QCheckBox("Use Textures")
            self.archive_model_texture_toggle.setToolTip(
                "Shade supported .pam/.pamlod/.pac previews with resolved archive DDS textures when available."
            )
            self.archive_model_texture_toggle.setEnabled(False)
            self.archive_model_export_obj_button = QPushButton("Export OBJ...")
            self.archive_model_export_obj_button.setToolTip(
                "Export the selected archive mesh as Wavefront OBJ with MTL and resolved preview textures for Blender."
            )
            self.archive_model_export_obj_button.setEnabled(False)
            self.archive_model_export_fbx_button = QPushButton("Export FBX...")
            self.archive_model_export_fbx_button.setToolTip(
                "Export the selected archive mesh as FBX. PAC exports also try to attach the matching PAB skeleton."
            )
            self.archive_model_export_fbx_button.setEnabled(False)
            self.archive_model_import_preview_button = QPushButton("Import OBJ Preview...")
            self.archive_model_import_preview_button.setToolTip(
                "Rebuild the selected archive mesh from an OBJ and show the result in the preview without patching the game files."
            )
            self.archive_model_import_preview_button.setEnabled(False)
            self.archive_model_import_patch_button = QPushButton("Import OBJ...")
            self.archive_model_import_patch_button.setToolTip(
                "Rebuild the selected archive mesh from an OBJ, then choose whether to patch the game archives or write a mod-ready loose file."
            )
            self.archive_model_import_patch_button.setEnabled(False)
            self.archive_restore_patch_backup_button = QPushButton("Restore Backup...")
            self.archive_restore_patch_backup_button.setToolTip(
                "Restore a previously created archive patch backup."
            )
            archive_preview_title_row.addWidget(self.archive_preview_title_label, stretch=1)
            archive_preview_title_row.addWidget(self.archive_preview_warning_badge)
            archive_preview_controls_row = QHBoxLayout()
            archive_preview_controls_row.setSpacing(8)
            archive_preview_controls_row.addWidget(self.archive_preview_loose_toggle_button)
            archive_preview_controls_row.addWidget(self.archive_preview_zoom_out_button)
            archive_preview_controls_row.addWidget(self.archive_preview_zoom_fit_button)
            archive_preview_controls_row.addWidget(self.archive_preview_zoom_100_button)
            archive_preview_controls_row.addWidget(self.archive_preview_zoom_in_button)
            archive_preview_controls_row.addWidget(self.archive_preview_zoom_value)
            archive_preview_controls_row.addStretch(1)
            archive_preview_controls_row.addWidget(self.archive_model_texture_toggle)
            archive_model_actions_row = QGridLayout()
            archive_model_actions_row.setContentsMargins(0, 0, 0, 0)
            archive_model_actions_row.setHorizontalSpacing(8)
            archive_model_actions_row.setVerticalSpacing(8)
            archive_model_actions_row.addWidget(self.archive_model_export_obj_button, 0, 0)
            archive_model_actions_row.addWidget(self.archive_model_export_fbx_button, 0, 1)
            archive_model_actions_row.addWidget(self.archive_model_import_preview_button, 1, 0)
            archive_model_actions_row.addWidget(self.archive_model_import_patch_button, 1, 1)
            archive_model_actions_row.addWidget(self.archive_restore_patch_backup_button, 2, 0, 1, 2)
            archive_preview_header.addLayout(archive_preview_title_row)
            archive_preview_header.addLayout(archive_preview_controls_row)
            archive_preview_header.addLayout(archive_model_actions_row)
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
            self.archive_texture_refs_group = QGroupBox("Referenced Textures")
            self.archive_texture_refs_group.setVisible(False)
            self.archive_texture_refs_group.setMinimumWidth(280)
            self.archive_texture_refs_group.setMaximumWidth(420)
            archive_texture_refs_layout = QVBoxLayout(self.archive_texture_refs_group)
            archive_texture_refs_layout.setContentsMargins(10, 10, 10, 10)
            archive_texture_refs_layout.setSpacing(8)
            self.archive_texture_refs_tree = QTreeWidget()
            self.archive_texture_refs_tree.setColumnCount(6)
            self.archive_texture_refs_tree.setHeaderLabels(
                ["Reference", "Status", "Semantic", "Archive Path", "Package", "Uses"]
            )
            self.archive_texture_refs_tree.setRootIsDecorated(False)
            self.archive_texture_refs_tree.setAlternatingRowColors(True)
            self.archive_texture_refs_tree.setSelectionMode(QAbstractItemView.SingleSelection)
            self.archive_texture_refs_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.archive_texture_refs_tree.setUniformRowHeights(True)
            self.archive_texture_refs_tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
            self.archive_texture_refs_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            texture_refs_header = self.archive_texture_refs_tree.header()
            texture_refs_header.setStretchLastSection(False)
            texture_refs_header.setMinimumSectionSize(56)
            texture_refs_header.setSectionResizeMode(0, QHeaderView.Interactive)
            texture_refs_header.setSectionResizeMode(1, QHeaderView.Interactive)
            texture_refs_header.setSectionResizeMode(2, QHeaderView.Interactive)
            texture_refs_header.setSectionResizeMode(3, QHeaderView.Interactive)
            texture_refs_header.setSectionResizeMode(4, QHeaderView.Interactive)
            texture_refs_header.setSectionResizeMode(5, QHeaderView.Fixed)
            archive_texture_refs_layout.addWidget(self.archive_texture_refs_tree)
            archive_texture_actions_row = QHBoxLayout()
            archive_texture_actions_row.setSpacing(8)
            self.archive_texture_open_button = QPushButton("Open")
            self.archive_texture_export_button = QPushButton("Export DDS...")
            self.archive_texture_replace_dds_button = QPushButton("Replace DDS...")
            self.archive_texture_replace_png_button = QPushButton("Replace From PNG...")
            self.archive_texture_open_button.setEnabled(False)
            self.archive_texture_export_button.setEnabled(False)
            self.archive_texture_replace_dds_button.setEnabled(False)
            self.archive_texture_replace_png_button.setEnabled(False)
            archive_texture_actions_row.addWidget(self.archive_texture_open_button)
            archive_texture_actions_row.addWidget(self.archive_texture_export_button)
            archive_texture_actions_row.addWidget(self.archive_texture_replace_dds_button)
            archive_texture_actions_row.addWidget(self.archive_texture_replace_png_button)
            archive_texture_actions_row.addStretch(1)
            archive_texture_refs_layout.addLayout(archive_texture_actions_row)

            self.archive_preview_stack = QStackedWidget()
            self.archive_preview_label = PreviewLabel("Select an archive file to preview it here.")
            self.archive_preview_scroll = PreviewScrollArea()
            self.archive_preview_scroll.setWidgetResizable(False)
            self.archive_preview_scroll.setAlignment(Qt.AlignCenter)
            self.archive_preview_scroll.setWidget(self.archive_preview_label)
            self.archive_preview_label.attach_scroll_area(self.archive_preview_scroll)
            self.archive_preview_label.set_wheel_zoom_handler(self._adjust_archive_preview_zoom)
            self.archive_model_preview = ModelPreviewWidget(
                "Select an archive file to preview it here.",
                theme_key=self.current_theme_key,
            )
            self.archive_model_preview.view_state_changed.connect(self._handle_archive_model_view_state_changed)
            self.archive_media_preview = MediaPreviewWidget(
                "Select an archive file to preview it here.",
                theme_key=self.current_theme_key,
            )
            self.archive_preview_text_edit = CodePreviewEditor(theme_key=self.current_theme_key)
            self.archive_preview_text_edit.document().setMaximumBlockCount(5000)
            self.archive_preview_info_edit = QPlainTextEdit()
            self.archive_preview_info_edit.setReadOnly(True)
            self.archive_preview_info_edit.document().setMaximumBlockCount(2000)
            self.archive_preview_stack.addWidget(self.archive_preview_scroll)
            self.archive_preview_stack.addWidget(self.archive_model_preview)
            self.archive_preview_stack.addWidget(self.archive_media_preview)
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
            self.archive_preview_content_splitter = QSplitter(Qt.Horizontal)
            self.archive_preview_content_splitter.addWidget(self.archive_texture_refs_group)
            self.archive_preview_content_splitter.addWidget(self.archive_preview_tabs)
            self.archive_preview_content_splitter.setCollapsible(0, True)
            self.archive_preview_content_splitter.setCollapsible(1, False)
            self.archive_preview_content_splitter.setStretchFactor(0, 0)
            self.archive_preview_content_splitter.setStretchFactor(1, 1)
            self.archive_preview_content_splitter.setSizes([320, 960])
            archive_preview_container_layout.addWidget(self.archive_preview_content_splitter, stretch=1)
            self.archive_splitter.addWidget(archive_preview_group)
            self.archive_splitter.setStretchFactor(0, 1)
            self.archive_splitter.setStretchFactor(1, 2)
            self.archive_splitter.setStretchFactor(2, 3)
            self.archive_splitter.setSizes([360, 360, 760])
            self.main_tabs.addTab(self.archive_browser_tab, "Archive Browser")

            self.text_search_tab = TextSearchTab(
                settings=self.settings,
                base_dir=self.settings_file_path.parent,
                theme_key=self.current_theme_key,
            )
            self.text_search_tab.status_message_requested.connect(
                lambda message, is_error: self.set_status_message(message, error=is_error)
            )
            self.research_tab = ResearchTab(
                settings=self.settings,
                base_dir=self.settings_file_path.parent,
                get_archive_entries=lambda: self.archive_entries,
                get_filtered_archive_entries=lambda: self.archive_filtered_entries,
                get_original_root=lambda: self.original_dds_edit.text(),
                get_output_root=lambda: self.output_root_edit.text(),
                get_texconv_path=lambda: self.texconv_path_edit.text(),
                get_app_config=self.collect_config,
                get_current_archive_path=self.current_archive_path_for_research,
                get_current_text_search_path=self.text_search_tab.current_result_path,
                get_current_compare_path=self.current_compare_path_for_research,
            )
            self.research_tab.status_message_requested.connect(
                lambda message, is_error: self.set_status_message(message, error=is_error)
            )
            self.research_tab.focus_archive_browser_requested.connect(
                lambda: self.main_tabs.setCurrentWidget(self.archive_browser_tab)
            )
            self.research_tab.extract_related_set_requested.connect(self.extract_related_archive_set_from_paths)
            self.research_tab.review_reference_in_text_search_requested.connect(
                self._review_reference_in_text_search
            )
            self.main_tabs.addTab(self.research_tab, "Research")
            self.main_tabs.addTab(self.text_search_tab, "Text Search")
            self.settings_tab = SettingsTab(
                settings=self.settings,
                theme_key=self.current_theme_key,
            )
            self.settings_tab.theme_changed.connect(self._handle_theme_changed)
            self.settings_tab.crash_capture_changed.connect(_set_crash_capture_enabled)
            self.main_tabs.addTab(self.settings_tab, "Settings")
            self.replace_assistant_tab = ReplaceAssistantTab(
                settings=self.settings,
                base_dir=self.settings_file_path.parent,
                get_archive_entries=lambda: self.archive_entries,
                get_original_root=lambda: self.original_dds_edit.text(),
                get_texconv_path=lambda: self.texconv_path_edit.text(),
                get_current_config=self.collect_config,
            )
            self.replace_assistant_tab.status_message_requested.connect(
                lambda message, is_error: self.set_status_message(message, error=is_error)
            )
            self.replace_assistant_tab.open_in_texture_editor_requested.connect(self._open_source_in_texture_editor)
            self.main_tabs.insertTab(1, self.replace_assistant_tab, "Replace Assistant")
            self.texture_editor_tab = TextureEditorTab(
                settings=self.settings,
                base_dir=self.settings_file_path.parent,
                get_texconv_path=lambda: self.texconv_path_edit.text(),
                get_png_root=lambda: self.png_root_edit.text(),
                get_original_dds_root=lambda: self.original_dds_edit.text(),
                get_archive_entries=lambda: self.archive_entries,
                get_current_config=self.collect_config,
            )
            self.texture_editor_tab.sync_ui_font_from_application()
            self.texture_editor_tab.status_message_requested.connect(
                lambda message, is_error: self.set_status_message(message, error=is_error)
            )
            self.texture_editor_tab.browse_archive_requested.connect(self._show_archive_browser_from_texture_editor)
            self.texture_editor_tab.open_in_compare_requested.connect(self._show_compare_from_texture_editor)
            self.texture_editor_tab.send_to_replace_assistant_requested.connect(
                self._handle_texture_editor_send_to_replace_assistant
            )
            self.texture_editor_tab.send_to_texture_workflow_requested.connect(
                self._handle_texture_editor_send_to_texture_workflow
            )
            self.main_tabs.insertTab(2, self.texture_editor_tab, "Texture Editor")
            self.setCentralWidget(central)

            self.export_profile_action.triggered.connect(self.export_profile)
            self.import_profile_action.triggered.connect(self.import_profile)
            self.export_diagnostics_action.triggered.connect(self.export_diagnostic_bundle)
            self.quick_start_menu_action.triggered.connect(self.show_quick_start_dialog)
            self.open_documentation_action.triggered.connect(self.show_about_dialog)
            self.scan_button.clicked.connect(self.start_scan)
            self.preview_policy_button.clicked.connect(self.preview_texture_policy)
            self.clear_workflow_roots_button.clicked.connect(self.clear_workflow_roots)
            self.start_button.clicked.connect(self.start_build)
            self.stop_button.clicked.connect(self.stop_build)
            self.open_output_button.clicked.connect(self.open_output_folder)
            self.init_workspace_button.clicked.connect(self.initialize_workspace)
            self.create_folders_button.clicked.connect(self.create_missing_folders)
            self.open_texture_editor_button.clicked.connect(self._browse_texture_editor_source)
            self.download_chainner_button.clicked.connect(self.open_chainner_download_page)
            self.download_texconv_button.clicked.connect(self.open_texconv_download_page)
            self.download_ncnn_button.clicked.connect(self.open_realesrgan_ncnn_download_page)
            self.import_ncnn_models_button.clicked.connect(self.import_ncnn_models)
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
            self.archive_open_in_editor_button.clicked.connect(self._open_archive_current_in_texture_editor)
            self.archive_resolve_in_research_button.clicked.connect(self._resolve_archive_current_in_research)
            self.archive_filter_apply_button.clicked.connect(self._apply_archive_filter)
            self.archive_filter_clear_button.clicked.connect(self._clear_archive_filters)
            self.archive_filter_edit.returnPressed.connect(self._apply_archive_filter)
            self.archive_exclude_filter_edit.returnPressed.connect(self._apply_archive_filter)
            self.archive_package_filter_edit.returnPressed.connect(self._apply_archive_filter)
            self.archive_filter_edit.textChanged.connect(self.schedule_settings_save)
            self.archive_filter_edit.textChanged.connect(self._mark_archive_filters_dirty)
            self.archive_exclude_filter_edit.textChanged.connect(self.schedule_settings_save)
            self.archive_exclude_filter_edit.textChanged.connect(self._mark_archive_filters_dirty)
            self.archive_package_filter_edit.textChanged.connect(self.schedule_settings_save)
            self.archive_package_filter_edit.textChanged.connect(self._mark_archive_filters_dirty)
            self.archive_extension_filter_combo.currentIndexChanged.connect(self.schedule_settings_save)
            self.archive_extension_filter_combo.currentIndexChanged.connect(self._mark_archive_filters_dirty)
            self.archive_role_filter_combo.currentIndexChanged.connect(self.schedule_settings_save)
            self.archive_role_filter_combo.currentIndexChanged.connect(self._mark_archive_filters_dirty)
            self.archive_exclude_common_technical_checkbox.toggled.connect(self.schedule_settings_save)
            self.archive_exclude_common_technical_checkbox.toggled.connect(self._mark_archive_filters_dirty)
            self.archive_min_size_spin.valueChanged.connect(self.schedule_settings_save)
            self.archive_min_size_spin.valueChanged.connect(self._mark_archive_filters_dirty)
            self.archive_previewable_only_checkbox.toggled.connect(self.schedule_settings_save)
            self.archive_previewable_only_checkbox.toggled.connect(self._mark_archive_filters_dirty)
            self.archive_tree_view_checkbox.toggled.connect(self.schedule_settings_save)
            self.archive_tree_view_checkbox.toggled.connect(self._handle_archive_tree_view_toggled)
            self.archive_tree.currentItemChanged.connect(self._handle_archive_current_item_change)
            self.archive_tree.itemSelectionChanged.connect(self._schedule_archive_selection_state_update)
            self.archive_tree.itemExpanded.connect(self._handle_archive_item_expanded)
            self.archive_tree.customContextMenuRequested.connect(self._show_archive_tree_context_menu)
            self.archive_preview_zoom_fit_button.clicked.connect(self._set_archive_preview_fit_mode)
            self.archive_preview_zoom_100_button.clicked.connect(lambda: self._set_archive_preview_zoom_factor(1.0))
            self.archive_preview_zoom_out_button.clicked.connect(lambda: self._adjust_archive_preview_zoom(-1))
            self.archive_preview_zoom_in_button.clicked.connect(lambda: self._adjust_archive_preview_zoom(1))
            self.archive_model_texture_toggle.toggled.connect(self._handle_archive_model_texture_toggle)
            self.archive_model_texture_toggle.toggled.connect(self.schedule_settings_save)
            self.archive_model_export_obj_button.clicked.connect(self._export_current_archive_model)
            self.archive_model_export_fbx_button.clicked.connect(
                lambda: self._export_current_archive_mesh("fbx")
            )
            self.archive_model_import_preview_button.clicked.connect(self._preview_current_archive_mesh_import)
            self.archive_model_import_patch_button.clicked.connect(self._patch_current_archive_mesh_from_obj)
            self.archive_restore_patch_backup_button.clicked.connect(self._restore_archive_patch_backup_from_ui)
            self.archive_texture_refs_tree.itemSelectionChanged.connect(self._update_archive_texture_reference_action_controls)
            self.archive_texture_refs_tree.itemDoubleClicked.connect(
                lambda _item, _column: self._open_selected_archive_texture_reference()
            )
            self.archive_preview_content_splitter.splitterMoved.connect(self._layout_archive_texture_reference_columns)
            self.archive_texture_open_button.clicked.connect(self._open_selected_archive_texture_reference)
            self.archive_texture_export_button.clicked.connect(self._export_selected_archive_texture_reference)
            self.archive_texture_replace_dds_button.clicked.connect(self._replace_selected_archive_texture_reference_from_dds)
            self.archive_texture_replace_png_button.clicked.connect(self._replace_selected_archive_texture_reference_from_png)
            self.archive_preview_loose_toggle_button.clicked.connect(self._toggle_archive_loose_preview)
            self.compare_previous_button.clicked.connect(lambda: self._select_compare_offset(-1))
            self.compare_next_button.clicked.connect(lambda: self._select_compare_offset(1))
            self.compare_mip_details_button.clicked.connect(self._open_compare_in_texture_analysis)
            self.compare_open_in_editor_button.clicked.connect(self._open_compare_in_texture_editor)
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
            self._compare_preview_timer.timeout.connect(self._flush_pending_compare_preview_selection)
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
            self._apply_upscale_backend_state()
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
            self._schedule_workflow_match_refresh()
            self._save_settings()

            geometry = self.settings.value("window/geometry")
            if geometry:
                self.restoreGeometry(geometry)
            QTimer.singleShot(0, self._apply_responsive_window_defaults)
            QTimer.singleShot(140, self._schedule_column_autofit)
            QTimer.singleShot(120, self._show_first_run_guide_if_needed)
            QTimer.singleShot(260, self._maybe_autoload_archive_on_startup)

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

        def _build_about_intro_html(self) -> str:
            return f"""
            <p><b>{APP_TITLE} v{APP_VERSION}</b> is a Windows desktop tool for Crimson Desert archive browsing and preview, supported archive patching, DDS rebuild workflows, visible-texture editing, replacement packaging, research, and text search.</p>
            <p>Use the search box and topic list on the left, or jump straight to:
            <a href="topic:workflow_overview">Texture Workflow</a>,
            <a href="topic:workflow_profiles">Workflow Profiles</a>,
            <a href="topic:workflow_rules">Ordered Rules</a>,
            <a href="topic:workflow_planner_profiles">Planner Profiles</a>,
            <a href="topic:workflow_planner_paths">Planner Paths</a>,
            <a href="topic:archive_browser">Archive Browser</a>,
            <a href="topic:texture_editor">Texture Editor</a>,
            <a href="topic:replace_assistant">Replace Assistant</a>,
            <a href="topic:research">Research</a>,
            <a href="topic:text_search">Text Search</a>.
            </p>
            """

        def _build_about_sections(self) -> List[Dict[str, str]]:
            readme_path = Path(__file__).resolve().parents[2] / "README.md"
            notices_path = Path(__file__).resolve().parents[2] / "THIRD_PARTY_NOTICES.md"
            license_path = Path(__file__).resolve().parents[2] / "LICENSE"
            readme_text = escape(str(readme_path))
            notices_text = escape(str(notices_path))
            license_text = escape(str(license_path))
            settings_text = escape(str(self.settings_file_path))
            cache_text = escape(str(self.archive_cache_root))
            return [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "High-level tour of the app and its main surfaces.",
                    "keywords": "overview about features tabs archive workflow editor replace assistant research text search settings",
                    "html": """
                    <p>The app is split into major work areas rather than one single pipeline.</p>
                    <ul>
                      <li><b>Texture Workflow</b>: batch loose DDS processing, optional upscaling, DDS rebuild, compare, and mod-ready loose export.</li>
                      <li><b>Archive Browser</b>: archive scanning, filtering, preview, extraction, supported patch/loose-export workflows, and Research/Editor handoff.</li>
                      <li><b>Texture Editor</b>: layered visible-texture editing and direct workflow handoff.</li>
                      <li><b>Replace Assistant</b>: guided one-off replacement packaging for edited PNG/DDS files.</li>
                      <li><b>Research</b>: grouped texture families, unknown-resolution work, DDS analysis, references, reports, and notes.</li>
                      <li><b>Text Search</b>: text-like archive/loose-file search with preview and export.</li>
                      <li><b>Settings</b>: appearance, cache/startup behavior, and persistent local preferences.</li>
                    </ul>
                    <p>If you are new to the app, start with <a href="topic:workflow_overview">Texture Workflow</a> and then review <a href="topic:compare_review">Compare &amp; Review</a>.</p>
                    """,
                },
                {
                    "id": "workflow_overview",
                    "title": "Texture Workflow",
                    "summary": "Main batch-processing tab for loose DDS files.",
                    "keywords": "texture workflow batch dds png rebuild compare start scan preview policy run summary",
                    "html": """
                    <p><b>Texture Workflow</b> is the main batch-processing tab. It scans loose DDS files under <b>Original DDS root</b>, plans what to do per file, optionally creates/stages PNG intermediates, optionally upscales them, rebuilds DDS output, and lets you review the result in Compare.</p>
                    <h4>Typical run</h4>
                    <ol>
                      <li>Configure <b>Setup</b>, <b>Paths</b>, and <b>DDS Output</b>.</li>
                      <li>Review <a href="topic:workflow_profiles">Workflow Profiles</a>, <a href="topic:workflow_rules">Ordered Rules</a>, and <a href="topic:workflow_matched_files">Matched Files</a>.</li>
                      <li>Choose your backend in <a href="topic:upscaling_backends">Upscaling</a>.</li>
                      <li>Use <b>Preview Policy</b> to inspect the current per-file plan.</li>
                      <li>Run <b>Scan</b> and then <b>Start</b>.</li>
                      <li>Review output in <a href="topic:compare_review">Compare</a>.</li>
                    </ol>
                    <h4>Main sections inside Texture Workflow</h4>
                    <ul>
                      <li><b>Setup</b>: workspace initialization, external tools, help links, and import helpers.</li>
                      <li><b>Paths</b>: source, PNG, staging, output, and optional mod-export roots.</li>
                      <li><b>DDS Output</b>: global default output format/size/mip behavior.</li>
                      <li><b>Workflow Profiles, Rules &amp; Matches</b>: per-file planning surface.</li>
                      <li><b>Upscaling</b>: backend, texture preset, direct NCNN controls, and policy notes.</li>
                      <li><b>Progress / Live Log / Compare</b>: runtime feedback and review.</li>
                    </ul>
                    """,
                },
                {
                    "id": "workflow_profiles",
                    "title": "Workflow Profiles",
                    "summary": "Reusable named per-file override sets for DDS output and direct NCNN settings.",
                    "keywords": "workflow profile selected profile action dds format size mips ncnn scale tile post correction",
                    "html": """
                    <p><b>Workflow Profiles</b> are reusable named override sets. They are assigned by ordered rules and only override the fields you fill in. Blank fields inherit the current global workflow settings.</p>
                    <h4>Selected Profile fields</h4>
                    <ul>
                      <li><b>Name</b>: display name used in the rules table and matched-files table.</li>
                      <li><b>Action</b>: force one action mode for matching files. <code>Inherit Planner</code> lets the planner decide; <code>Upscale Then Rebuild</code>, <code>Rebuild From PNG</code>, <code>Preserve Original</code>, and <code>Skip</code> force that result.</li>
                      <li><b>DDS Format / DDS Size / Mipmaps</b>: optional DDS output overrides applied after the file is matched.</li>
                      <li><b>Direct NCNN Model / Scale / Tile / Extra Args / Post Correction</b>: optional per-file direct-NCNN overrides. These matter only when the selected backend is direct <b>Real-ESRGAN NCNN</b>.</li>
                    </ul>
                    <h4>Starter profiles</h4>
                    <ul>
                      <li><b>Starter Color / Albedo</b>: batch-upscale baseline for visible color-style textures.</li>
                      <li><b>Starter Normal</b>: preserve-first baseline for normal maps.</li>
                      <li><b>Starter Height / Displacement</b>: preserve-first baseline for scalar height/displacement maps.</li>
                      <li><b>Starter Specular</b>: preserve-first baseline for specular-like scalar masks.</li>
                    </ul>
                    <p>The starters are meant to be sane defaults, not universal “best” answers. If a file family really should be pushed through rebuild or direct NCNN, duplicate the starter and make a more aggressive variant.</p>
                    """,
                },
                {
                    "id": "workflow_rules",
                    "title": "Ordered Rules",
                    "summary": "Last-match-wins assignment table that maps files to workflow behavior.",
                    "keywords": "ordered rules selected rule match glob exact path workflow profile semantic planner profile colorspace alpha planner path last match wins",
                    "html": """
                    <p><b>Ordered Rules</b> are evaluated from top to bottom with <b>last match wins</b>. Exact-path rules and glob rules share one list.</p>
                    <h4>Selected Rule fields</h4>
                    <ul>
                      <li><b>Enabled</b>: disabled rules stay in the list but are ignored.</li>
                      <li><b>Match</b>: <code>Glob</code> matches basename or relative path patterns; <code>Exact Path</code> targets one exact relative path.</li>
                      <li><b>Pattern</b>: the glob or exact relative path to match.</li>
                      <li><b>Workflow Profile</b>: assigns one of the reusable workflow profiles above.</li>
                      <li><b>Semantic</b>: optional manual semantic override such as <code>normal:normal</code> or <code>height:displacement</code>.</li>
                      <li><b>Planner Profile</b>: optional processing-profile override that changes planner assumptions such as preferred compression, colorspace, and preserve behavior. See <a href="topic:workflow_planner_profiles">Planner Profiles</a>.</li>
                      <li><b>Colorspace</b>: optional rule-level colorspace override.</li>
                      <li><b>Alpha Policy</b>: optional rule-level alpha handling override.</li>
                      <li><b>Planner Path</b>: optional path override that tells the planner which intermediate route to prefer. See <a href="topic:workflow_planner_paths">Planner Paths</a>.</li>
                    </ul>
                    <h4>Authoring notes</h4>
                    <ul>
                      <li>Put broad family rules near the top and one-off exact-path fixes near the bottom.</li>
                      <li>Use the <a href="topic:workflow_matched_files">Matched Files</a> table when you want to turn selected rows into exact-path assignment rules quickly.</li>
                      <li>If you are unsure, inspect the result in <b>Preview Policy</b> before running <b>Start</b>.</li>
                    </ul>
                    """,
                },
                {
                    "id": "workflow_planner_profiles",
                    "title": "Planner Profiles",
                    "summary": "Meaning of planner-profile values under Selected Rule.",
                    "keywords": "planner profiles selected rule color_default normal_bc5 scalar_bc4 scalar_high_precision_bc4 packed mask premultiplied float vector",
                    "html": """
                    <p><b>Planner Profile</b> is a low-level processing profile used by the planner. It influences preferred texture format, colorspace, alpha policy, mip hint, allowed path kinds, and preserve-first behavior.</p>
                    <ul>
                      <li><code>color_default</code>: visible color textures. Prefers sRGB-aware color handling and color-style mip treatment.</li>
                      <li><code>color_cutout_alpha</code>: visible textures with cutout alpha where alpha coverage should be preserved more carefully.</li>
                      <li><code>ui_alpha</code>: UI-style visible textures with alpha.</li>
                      <li><code>normal_bc5</code>: normal maps. Linear, BC5-oriented, preserve-first.</li>
                      <li><code>scalar_bc4</code>: generic single-channel scalar/mask data. Linear, BC4-oriented, preserve-first.</li>
                      <li><code>scalar_high_precision_bc4</code>: eligible scalar technical maps that may use the high-precision technical path instead of the generic visible PNG path.</li>
                      <li><code>packed_mask_preserve_layout</code>: packed-channel masks and response maps. Preserve-first to avoid channel drift.</li>
                      <li><code>premultiplied_alpha_review_required</code>: premultiplied-alpha content that should be reviewed manually.</li>
                      <li><code>float_or_vector_preserve_only</code>: float, vector, or other precision-sensitive technical data that should stay preserve-only.</li>
                    </ul>
                    <p>In practice, use workflow profiles for the user-facing batch behavior and use planner profiles only when you specifically need to change the planner’s technical assumptions for a matched file or file family.</p>
                    """,
                },
                {
                    "id": "workflow_planner_paths",
                    "title": "Planner Paths",
                    "summary": "Meaning of planner-path values under Selected Rule.",
                    "keywords": "planner paths selected rule visible_color_png_path technical_preserve_path technical_high_precision_path",
                    "html": f"""
                    <p><b>Planner Path</b> chooses the intermediate route the planner should prefer for a matched file.</p>
                    <ul>
                      <li><code>visible_color_png_path</code>: {escape(describe_processing_path_kind("visible_color_png_path"))}</li>
                      <li><code>technical_preserve_path</code>: {escape(describe_processing_path_kind("technical_preserve_path"))}</li>
                      <li><code>technical_high_precision_path</code>: {escape(describe_processing_path_kind("technical_high_precision_path"))}</li>
                    </ul>
                    <h4>Backend behavior</h4>
                    <ul>
                      <li><b>Disabled backend</b>: visible-color path rebuilds from current PNG inputs. The high-precision path can rebuild from valid high-bit-depth PNG data if present.</li>
                      <li><b>Direct Real-ESRGAN NCNN</b>: visible-color path is supported. Technical high-precision path is not supported in the current tranche, so preserve behavior wins when that path is chosen.</li>
                      <li><b>chaiNNer</b>: only the visible-color path is trusted in the current tranche. Technical preserve/high-precision paths stay preserve-first.</li>
                    </ul>
                    <p>Use planner-path overrides carefully. Forcing technical textures onto <code>visible_color_png_path</code> is intentionally treated as a higher-risk choice.</p>
                    """,
                },
                {
                    "id": "workflow_matched_files",
                    "title": "Matched Files",
                    "summary": "Live current-match view for the workflow file set.",
                    "keywords": "matched files assign profile exact path rules action effective dds ncnn summary current filter",
                    "html": """
                    <p><b>Matched Files</b> is a live table for the current workflow match set: DDS files under <b>Original DDS root</b> that also match the current folder/file filter.</p>
                    <h4>Columns</h4>
                    <ul>
                      <li><b>Path</b>: relative path inside the current Original DDS root.</li>
                      <li><b>Semantic</b>: current inferred or overridden semantic type/subtype.</li>
                      <li><b>Rule</b>: the last matching ordered rule.</li>
                      <li><b>Workflow</b>: the assigned workflow profile, if any.</li>
                      <li><b>DDS Output</b>: effective DDS output override summary.</li>
                      <li><b>NCNN</b>: effective direct-NCNN summary for that file.</li>
                      <li><b>Action</b>: final planned action after planner, rule, profile, backend, and safety logic are combined.</li>
                    </ul>
                    <p><b>Assign Profile</b> creates new exact-path rules for the selected rows. Because ordered rules are last-match-wins, these one-off assignment rules are appended at the end of the current rule list.</p>
                    """,
                },
                {
                    "id": "dds_output",
                    "title": "DDS Output & Staging",
                    "summary": "Global DDS rebuild defaults used by the workflow.",
                    "keywords": "dds output format size mipmaps staging texconv png size match original custom",
                    "html": """
                    <p><b>DDS Output</b> defines the global rebuild defaults for files that do not receive a workflow-profile override.</p>
                    <ul>
                      <li><b>Format</b>: match the original DDS or force one texconv format.</li>
                      <li><b>Size</b>: rebuild to PNG size, original DDS size, or a custom size.</li>
                      <li><b>Mipmaps</b>: keep original count, generate a full chain, use a single mip, or force a custom count.</li>
                      <li><b>DDS staging</b>: when enabled with an upscaling backend, the app first converts matched DDS files to a staging PNG root before the backend runs.</li>
                    </ul>
                    <p>Workflow profiles can override these per file. That is why DDS Output should be thought of as the global default layer, not always the final result.</p>
                    """,
                },
                {
                    "id": "upscaling_backends",
                    "title": "Upscaling & Backends",
                    "summary": "How disabled mode, direct NCNN, and chaiNNer behave.",
                    "keywords": "upscaling backend ncnn chainner disabled scale tile retry post correction source match direct backend",
                    "html": """
                    <p>The app supports three high-level modes in <b>Upscaling</b>.</p>
                    <ul>
                      <li><b>Disabled</b>: no upscale backend. The workflow can still rebuild DDS from existing PNG inputs.</li>
                      <li><b>Real-ESRGAN NCNN</b>: direct in-app backend with global model, scale, tile, extra args, retry/fallback behavior, and optional post correction.</li>
                      <li><b>chaiNNer</b>: external chain-based backend. The chain remains the source of truth for what actually happens.</li>
                    </ul>
                    <h4>Important notes</h4>
                    <ul>
                      <li>Per-file direct-NCNN overrides only apply when the selected backend is direct <b>Real-ESRGAN NCNN</b>.</li>
                      <li>Automatic texture rules and planner behavior still matter even when an upscale backend is enabled.</li>
                      <li><b>Run Summary</b> is the read-only preflight view for current sources, backend, texture preset, and export behavior.</li>
                      <li><b>Preview Policy</b> is the per-file plan view for the current workflow match set.</li>
                    </ul>
                    """,
                },
                {
                    "id": "compare_review",
                    "title": "Compare & Review",
                    "summary": "Side-by-side original/output DDS review.",
                    "keywords": "compare review side by side sync pan preview size mip details open in texture editor",
                    "html": """
                    <p><b>Compare</b> is the final review surface for the current loose output set.</p>
                    <ul>
                      <li>Preview original DDS and output DDS side by side.</li>
                      <li>Change fit/zoom level, pan each side, or enable <b>Sync Pan</b>.</li>
                      <li>Open the current texture in <b>Texture Editor</b> or jump to mip details in Research.</li>
                      <li>Use this before large runs or before packaging output for a mod-ready folder.</li>
                    </ul>
                    """,
                },
                {
                    "id": "archive_browser",
                    "title": "Archive Browser",
                    "summary": "Archive scan, preview, extraction, and supported patch surface.",
                    "keywords": "archive browser pamt paz scan preview filter extract patch mod ready mesh audio video text dds workflow research texture editor",
                    "html": """
                    <p><b>Archive Browser</b> is the in-app inspection surface for Crimson Desert package data. It can browse archives in flat or tree view, preview many supported formats directly, extract files, and for supported workflows either patch the game archives or write mod-ready loose output with confirmation and backup support.</p>
                    <ul>
                      <li>Scan package roots and cache the discovered archive index locally.</li>
                      <li>Filter by path, package, folder, likely role, size, and previewability, then switch between flat and tree browsing as needed.</li>
                      <li>Preview supported DDS/images, text-like files, audio/video, and model assets such as <code>.pam</code>, <code>.pamlod</code>, and <code>.pac</code>.</li>
                      <li>Extract selected or filtered content to loose folders.</li>
                      <li>Inspect referenced model textures, export supported meshes as OBJ/FBX, import OBJ for rebuilt preview, and choose between archive patching or mod-ready loose export for supported mesh flows.</li>
                      <li>Replace supported archive DDS entries from DDS or PNG, patch supported audio entries, and restore backups created by supported patch operations.</li>
                      <li>Send DDS content into Texture Workflow, open supported images in Texture Editor, or resolve items in Research.</li>
                    </ul>
                    <p>Not every archive format is editable. Browsing and preview support is broader than patch support, so use the visible actions beside the preview to see what is currently available for the selected entry.</p>
                    """,
                },
                {
                    "id": "texture_editor",
                    "title": "Texture Editor",
                    "summary": "Layered editor for visible-texture work.",
                    "keywords": "texture editor layers masks selections brush clone heal smudge patch gradient dodge burn channels compare",
                    "html": """
                    <p><b>Texture Editor</b> is built for visible-color texture work rather than general-purpose technical-map authoring.</p>
                    <ul>
                      <li>Layered document with paint, erase, fill, gradient, clone, heal, smudge, patch, dodge/burn, sharpen, and soften tools.</li>
                      <li>Selections, floating paste/move workflow, masks, channel locks, and non-destructive adjustments.</li>
                      <li>RGBA/original/split preview modes and direct handoff to Compare, Replace Assistant, and Texture Workflow.</li>
                      <li>Warnings are shown for technical textures because the editor is not the safest place to rebuild those blindly.</li>
                    </ul>
                    """,
                },
                {
                    "id": "replace_assistant",
                    "title": "Replace Assistant",
                    "summary": "Guided one-off replacement flow for edited PNG/DDS files.",
                    "keywords": "replace assistant replace edited png dds original match mod ready loose export package",
                    "html": """
                    <p><b>Replace Assistant</b> is the best route when you already have an edited PNG or DDS and want to match it back to the correct original texture, rebuild it safely, and export a ready loose mod folder.</p>
                    <ul>
                      <li>Match edited assets to original DDS files or archive entries.</li>
                      <li>Apply correction and rebuild logic with current output settings.</li>
                      <li>Export a mod-ready loose folder structure for the matched results.</li>
                    </ul>
                    """,
                },
                {
                    "id": "research",
                    "title": "Research",
                    "summary": "Texture-family inspection, unknown-resolution, DDS QA, reports, and notes.",
                    "keywords": "research unknown resolver grouped families dds qa reports heatmaps notes references",
                    "html": """
                    <p><b>Research</b> is the analysis surface for grouped texture families and metadata-heavy review.</p>
                    <ul>
                      <li>Inspect grouped DDS families and their inferred semantic roles.</li>
                      <li>Use <b>Unknown Resolver</b> to review and assign uncertain classifications.</li>
                      <li>Open DDS analysis, reports, references, and local notes.</li>
                      <li>Review planner/path/profile summaries and family-level context before committing to risky workflow overrides.</li>
                    </ul>
                    """,
                },
                {
                    "id": "text_search",
                    "title": "Text Search",
                    "summary": "Search archive and loose text-like files with preview and export.",
                    "keywords": "text search xml json cfg lua regex export preview encrypted xml archive loose",
                    "html": """
                    <p><b>Text Search</b> is for archive or loose-file search across text-like assets such as <code>.xml</code>, <code>.json</code>, <code>.cfg</code>, <code>.lua</code>, and similar formats.</p>
                    <ul>
                      <li>Search with preview and syntax-colored match context.</li>
                      <li>Work against archive data or loose folders.</li>
                      <li>Export matched results while preserving folder structure.</li>
                    </ul>
                    """,
                },
                {
                    "id": "settings_files",
                    "title": "Settings, Files & Dependencies",
                    "summary": "Local config, cache, project files, and external dependencies.",
                    "keywords": "settings files config cache dependency texconv ncnn chainner license readme notices",
                    "html": f"""
                    <p>The app stores its local settings and archive cache beside the executable or local source checkout.</p>
                    <ul>
                      <li><b>Config file</b>: <code>{settings_text}</code></li>
                      <li><b>Archive cache</b>: <code>{cache_text}</code></li>
                      <li><b>README</b>: <code>{readme_text}</code></li>
                      <li><b>License</b>: <code>{license_text}</code></li>
                      <li><b>Third-party notices</b>: <code>{notices_text}</code></li>
                    </ul>
                    <h4>External requirements</h4>
                    <ul>
                      <li><b>texconv</b> is required for DDS preview, DDS-to-PNG conversion, compare preview, and DDS rebuild.</li>
                      <li><b>Real-ESRGAN NCNN</b> and <b>chaiNNer</b> are optional backends.</li>
                    </ul>
                    <h4>References</h4>
                    <ul>
                      <li><a href="https://github.com/microsoft/DirectXTex/releases">Microsoft DirectXTex releases</a></li>
                      <li><a href="https://chainner.app/download/">chaiNNer download page</a></li>
                      <li><a href="https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan">Real-ESRGAN NCNN Vulkan</a></li>
                      <li><a href="https://www.nexusmods.com/crimsondesert/mods/62">Crimson Desert Unpacker</a></li>
                      <li><a href="https://www.nexusmods.com/crimsondesert/mods/84">Crimson Browser &amp; Mod Manager</a></li>
                    </ul>
                    """,
                },
                {
                    "id": "troubleshooting",
                    "title": "Troubleshooting & Limits",
                    "summary": "Common failure cases and current limitations.",
                    "keywords": "troubleshooting limits texconv ncnn chainner png output brightness drift preview archive cache",
                    "html": """
                    <ul>
                      <li><b>Missing texconv</b>: DDS preview, DDS-to-PNG conversion, compare preview, and DDS rebuild all depend on it.</li>
                      <li><b>Missing NCNN models</b>: direct NCNN requires a valid executable and matching <code>.param</code> / <code>.bin</code> models.</li>
                      <li><b>No matching PNG outputs</b>: if the selected backend produces no usable PNG output, DDS rebuild has nothing to convert.</li>
                      <li><b>Wrong chaiNNer paths</b>: hardcoded chain paths can make the chain read from or write to the wrong directory.</li>
                      <li><b>Brightness or detail drift</b>: compare outputs carefully, test another model, or change direct-NCNN post correction.</li>
                      <li><b>Archive preview limits</b>: unusual DDS layouts and very large archive scans are still best-effort cases.</li>
                      <li><b>Technical textures</b>: preserve-first handling is still the safer default for normals, masks, packed channels, vectors, and other precision-sensitive maps.</li>
                    </ul>
                    """,
                },
            ]

        def show_about_dialog(self, _checked: bool = False, topic_id: str = "") -> None:
            dialog = AboutDialog(
                self,
                title=f"{APP_TITLE} Documentation",
                intro_html=self._build_about_intro_html(),
                sections=self._build_about_sections(),
                initial_section_id=topic_id or "overview",
            )
            dialog.exec()

        def _collect_profile_payload(self) -> Dict[str, object]:
            return {
                "app": APP_TITLE,
                "profile_format": 2,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "theme": self.current_theme_key,
                "config": dataclasses.asdict(self.collect_config()),
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
            staging_root_text = self.dds_staging_root_edit.text().strip()
            png_root_text = self.png_root_edit.text().strip()
            original_root = Path(original_root_text).expanduser().resolve() if original_root_text else None
            staging_root = Path(staging_root_text).expanduser().resolve() if staging_root_text else None
            png_root = Path(png_root_text).expanduser().resolve() if png_root_text else None

            analysis = analyze_chainner_chain_paths(
                chain_path,
                original_dds_root=original_root,
                staging_png_root=staging_root,
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
                    "Path-mismatch validation is limited until Original DDS root and PNG root are configured. DDS staging validation is also limited until DDS staging root is configured when staging is enabled."
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
                self.texture_editor_png_root_edit.setText(getattr(config, "texture_editor_png_root", ""))
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
                self.dry_run_checkbox.setChecked(bool(config.dry_run))
                self.csv_log_enabled_checkbox.setChecked(bool(config.csv_log_enabled))
                self.csv_log_path_edit.setText(config.csv_log_path)
                self.unique_basename_checkbox.setChecked(bool(config.allow_unique_basename_fallback))
                self.overwrite_existing_checkbox.setChecked(bool(config.overwrite_existing_dds))
                self.filters_edit.setPlainText(config.include_filters)
                self._set_combo_by_value(
                    self.upscale_backend_combo,
                    getattr(
                        config,
                        "upscale_backend",
                        UPSCALE_BACKEND_CHAINNER if config.enable_chainner else UPSCALE_BACKEND_NONE,
                    ),
                )
                self.chainner_exe_path_edit.setText(config.chainner_exe_path)
                self.chainner_chain_path_edit.setText(config.chainner_chain_path)
                self.chainner_override_edit.setPlainText(config.chainner_override_json)
                self.ncnn_exe_path_edit.setText(getattr(config, "ncnn_exe_path", ""))
                self.ncnn_model_dir_edit.setText(getattr(config, "ncnn_model_dir", ""))
                self.ncnn_extra_args_edit.setText(getattr(config, "ncnn_extra_args", ""))
                self.ncnn_scale_spin.setValue(int(getattr(config, "ncnn_scale", REALESRGAN_NCNN_SCALE)))
                self.ncnn_tile_size_spin.setValue(int(getattr(config, "ncnn_tile_size", REALESRGAN_NCNN_TILE_SIZE)))
                self._set_combo_by_value(
                    self.upscale_post_correction_combo,
                    getattr(config, "upscale_post_correction_mode", DEFAULT_UPSCALE_POST_CORRECTION),
                )
                self._set_combo_by_value(
                    self.upscale_texture_preset_combo,
                    getattr(config, "upscale_texture_preset", DEFAULT_UPSCALE_TEXTURE_PRESET),
                )
                self.enable_automatic_texture_rules_checkbox.setChecked(
                    bool(getattr(config, "enable_automatic_texture_rules", ENABLE_AUTOMATIC_TEXTURE_RULES))
                )
                self.enable_unsafe_technical_override_checkbox.setChecked(
                    bool(getattr(config, "enable_unsafe_technical_override", ENABLE_UNSAFE_TECHNICAL_OVERRIDE))
                )
                self.retry_smaller_tile_checkbox.setChecked(
                    bool(getattr(config, "retry_smaller_tile_on_failure", RETRY_SMALLER_TILE_ON_FAILURE))
                )
                self.enable_mod_ready_loose_export_checkbox.setChecked(
                    bool(getattr(config, "enable_mod_ready_loose_export", ENABLE_MOD_READY_LOOSE_EXPORT))
                )
                self.mod_ready_export_root_edit.setText(getattr(config, "mod_ready_export_root", ""))
                self.mod_ready_create_no_encrypt_checkbox.setChecked(
                    bool(getattr(config, "mod_ready_create_no_encrypt_file", MOD_READY_CREATE_NO_ENCRYPT))
                )
                self.mod_ready_package_title_edit.setText(getattr(config, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE))
                self.mod_ready_package_version_edit.setText(getattr(config, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION))
                self.mod_ready_package_author_edit.setText(getattr(config, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR))
                self.mod_ready_package_description_edit.setText(
                    getattr(config, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION)
                )
                self.mod_ready_package_nexus_url_edit.setText(
                    getattr(config, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL)
                )
                self._refresh_ncnn_model_picker(preferred_name=getattr(config, "ncnn_model_name", ""))
                self.archive_package_root_edit.setText(config.archive_package_root)
                self.archive_extract_root_edit.setText(config.archive_extract_root)
                self.archive_filter_edit.setText(config.archive_filter_text)
                self.archive_exclude_filter_edit.setText(getattr(config, "archive_exclude_filter_text", ""))
                self._rebuild_archive_extension_filter_choices(config.archive_extension_filter)
                self._set_combo_by_value(self.archive_extension_filter_combo, config.archive_extension_filter)
                self.archive_package_filter_edit.setText(config.archive_package_filter_text)
                self.archive_structure_filter_pending_value = config.archive_structure_filter
                self._set_combo_by_value(self.archive_role_filter_combo, config.archive_role_filter)
                self.archive_exclude_common_technical_checkbox.setChecked(
                    bool(getattr(config, "archive_exclude_common_technical_suffixes", ARCHIVE_EXCLUDE_COMMON_TECHNICAL_SUFFIXES))
                )
                self.archive_min_size_spin.setValue(int(config.archive_min_size_kb))
                self.archive_previewable_only_checkbox.setChecked(bool(config.archive_previewable_only))
                self._apply_workflow_state_from_config(config)
            finally:
                self._settings_ready = previous_ready

            self._apply_csv_log_enabled_state()
            self._apply_upscale_backend_state()
            self._apply_mod_ready_export_state()
            self._apply_dds_staging_enabled_state()
            self._apply_dds_output_state()
            self._refresh_chainner_chain_info()
            self._schedule_workflow_match_refresh()
            if theme_key and theme_key in UI_THEME_SCHEMES:
                self._handle_theme_changed(theme_key)
            self.flush_settings_save()

        def export_profile(self) -> None:
            try:
                default_name = self.settings_file_path.parent / "crimson_forge_toolkit_profile.ctfprofile.json"
                selected, _ = QFileDialog.getSaveFileName(
                    self,
                    "Export Profile",
                    str(default_name),
                    "Crimson Forge Toolkit profile (*.ctfprofile.json);;JSON files (*.json);;All files (*.*)",
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
                    "Crimson Forge Toolkit profile (*.ctfprofile.json *.json);;All files (*.*)",
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
                config_values = dataclasses.asdict(defaults)
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
                default_name = self.settings_file_path.parent / "crimson_forge_toolkit_diagnostics.zip"
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
                    if crash_reports_dir.exists():
                        latest_crash_report = max(
                            (path for path in crash_reports_dir.glob("*.log") if path.is_file()),
                            default=None,
                            key=lambda path: path.stat().st_mtime,
                        )
                        if latest_crash_report is not None:
                            archive.writestr(
                                f"crash_reports/{latest_crash_report.name}",
                                latest_crash_report.read_text(encoding="utf-8"),
                            )
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

        def _handle_theme_changed(self, theme_key: Optional[str] = None) -> None:
            resolved_theme_key = theme_key if theme_key in UI_THEME_SCHEMES else self.current_theme_key
            app = QApplication.instance()
            if app is None:
                return
            self.current_theme_key = apply_app_theme(app, self.settings, resolved_theme_key)
            apply_window_data_fonts(self)
            self.log_highlighter.set_theme(self.current_theme_key)
            self.archive_log_highlighter.set_theme(self.current_theme_key)
            self.archive_model_preview.set_theme(self.current_theme_key)
            self.archive_media_preview.set_theme(self.current_theme_key)
            self.archive_preview_text_edit.set_theme(self.current_theme_key)
            self.text_search_tab.set_theme(self.current_theme_key)
            self.research_tab.set_theme(self.current_theme_key)
            self.texture_editor_tab.sync_ui_font(app.font())
            self.settings_tab.sync_appearance_controls(self.current_theme_key)
            self._schedule_column_autofit()
            self.schedule_settings_save()

        def _archive_preview_text_language_extension(self, preview_text: str) -> str:
            entry = self._current_archive_entry()
            extension = entry.extension.lower() if entry is not None else ""
            stripped = preview_text.lstrip("\ufeff\r\n\t ")
            if extension == ".pami":
                return ".xml"
            if stripped.startswith(("<?xml", "<")):
                return ".xml"
            if stripped.startswith(("{", "[")):
                return ".json"

            non_empty_lines = [line.strip() for line in preview_text.splitlines() if line.strip()]
            sample_lines = non_empty_lines[:8]
            if any(line.startswith("[") and line.endswith("]") for line in sample_lines):
                return ".ini"
            if any("=" in line and not line.startswith("<") for line in sample_lines):
                return ".ini"
            if any(line.startswith("--") for line in sample_lines) or "function " in preview_text[:4096]:
                return ".lua"
            return extension

        def _preference_bool(self, key: str, default: bool) -> bool:
            return self._read_bool(f"preferences/{key}", default)

        def _load_saved_splitter_sizes(self, key: str) -> Optional[List[int]]:
            raw_value = self.settings.value(key)
            if raw_value in (None, ""):
                return None
            if isinstance(raw_value, str):
                parts = [part.strip() for part in raw_value.split(",") if part.strip()]
            elif isinstance(raw_value, (list, tuple)):
                parts = list(raw_value)
            else:
                return None
            sizes: List[int] = []
            for part in parts:
                try:
                    value = int(part)
                except (TypeError, ValueError):
                    return None
                if value <= 0:
                    return None
                sizes.append(value)
            return sizes or None

        def _apply_default_splitter_sizes(self, total_width: int) -> None:
            self.workflow_splitter.setSizes(
                build_responsive_splitter_sizes(total_width, [34, 66], [320, 420])
            )
            available_right_height = max(420, self.height() - 260)
            progress_min_height = getattr(self, "progress_group_min_height", 190)
            self.workflow_right_splitter.setSizes(
                build_responsive_splitter_sizes(
                    available_right_height,
                    [18, 82],
                    [progress_min_height, 320],
                )
            )
            self.compare_splitter.setSizes(
                build_responsive_splitter_sizes(total_width, [22, 78], [220, 520])
            )
            self.archive_splitter.setSizes(
                build_responsive_splitter_sizes(total_width, [23, 29, 48], [320, 260, 300])
            )
            self.replace_assistant_tab.apply_responsive_splitter_sizes(total_width)
            self.research_tab.apply_responsive_splitter_sizes(total_width)
            self.text_search_tab.apply_responsive_splitter_sizes(total_width)

        def _apply_saved_splitter_sizes_if_enabled(self, total_width: int) -> None:
            self._apply_default_splitter_sizes(total_width)
            if not self._preference_bool("remember_splitter_sizes", True):
                return

            for splitter, setting_key in (
                (self.workflow_splitter, "ui/workflow_splitter_sizes"),
                (self.workflow_right_splitter, "ui/workflow_right_splitter_sizes_v2"),
                (self.compare_splitter, "ui/compare_splitter_sizes_v2"),
                (self.archive_splitter, "ui/archive_splitter_sizes"),
            ):
                sizes = self._load_saved_splitter_sizes(setting_key)
                if sizes:
                    if splitter is self.workflow_right_splitter and len(sizes) >= 2:
                        available_right_height = max(420, self.height() - 260)
                        progress_min_height = getattr(self, "progress_group_min_height", 190)
                        sizes = clamp_splitter_sizes(
                            available_right_height,
                            sizes,
                            [progress_min_height, 320],
                            fallback_weights=[18, 82],
                        )
                    elif splitter is self.workflow_splitter:
                        sizes = clamp_splitter_sizes(total_width, sizes, [320, 420], fallback_weights=[34, 66])
                    elif splitter is self.compare_splitter:
                        sizes = clamp_splitter_sizes(total_width, sizes, [220, 520], fallback_weights=[22, 78])
                    elif splitter is self.archive_splitter:
                        sizes = clamp_splitter_sizes(
                            total_width,
                            sizes,
                            [320, 260, 300],
                            fallback_weights=[23, 29, 48],
                        )
                    splitter.setSizes(sizes)

            text_search_sizes = self._load_saved_splitter_sizes("ui/text_search_splitter_sizes")
            if text_search_sizes:
                self.text_search_tab.set_splitter_sizes(text_search_sizes)
            replace_assistant_sizes = self._load_saved_splitter_sizes("ui/replace_assistant_splitter_sizes")
            if replace_assistant_sizes:
                self.replace_assistant_tab.set_splitter_sizes(replace_assistant_sizes, total_width=total_width)
            research_main_sizes = self._load_saved_splitter_sizes("ui/research_main_splitter_sizes")
            if research_main_sizes:
                self.research_tab.set_main_splitter_sizes(research_main_sizes, total_width=total_width)
            research_groups_sizes = self._load_saved_splitter_sizes("ui/research_groups_splitter_sizes")
            if research_groups_sizes:
                self.research_tab.set_groups_splitter_sizes(research_groups_sizes, total_width=total_width)
            research_unknown_sizes = self._load_saved_splitter_sizes("ui/research_unknown_splitter_sizes")
            if research_unknown_sizes:
                self.research_tab.set_unknown_splitter_sizes(research_unknown_sizes, total_width=total_width)
            research_reference_sizes = self._load_saved_splitter_sizes("ui/research_reference_splitter_sizes")
            if research_reference_sizes:
                self.research_tab.set_reference_splitter_sizes(research_reference_sizes, total_width=total_width)
            research_analysis_sizes = self._load_saved_splitter_sizes("ui/research_analysis_splitter_sizes")
            if research_analysis_sizes:
                self.research_tab.set_analysis_splitter_sizes(research_analysis_sizes, total_width=total_width)
            research_notes_sizes = self._load_saved_splitter_sizes("ui/research_notes_splitter_sizes")
            if research_notes_sizes:
                self.research_tab.set_notes_splitter_sizes(research_notes_sizes, total_width=total_width)

        def _maybe_autoload_archive_on_startup(self) -> None:
            if self.show_quick_start_on_launch:
                return
            if not self._preference_bool("auto_load_archive_on_startup", False):
                return
            if self.worker_thread is not None or self.archive_entries:
                return

            package_root_text = self.archive_package_root_edit.text().strip()
            if not package_root_text:
                return
            package_root = Path(package_root_text).expanduser()
            if not package_root.exists():
                self.append_archive_log(f"Startup archive auto-load skipped: package root does not exist: {package_root}")
                return

            self.append_archive_log("Startup archive auto-load is enabled.")
            self.scan_archives(
                force_refresh=not self._preference_bool("prefer_archive_cache_on_startup", True),
                activate_archive_tab=False,
            )

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
            self._apply_saved_splitter_sizes_if_enabled(total_width)
            self._schedule_column_autofit()

        def _schedule_column_autofit(self) -> None:
            if self._shutting_down:
                return
            self._column_autofit_timer.start()

        def _fit_tree_columns(
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
                stretch_width = max(min_widths.get(stretch_column, 180), viewport_width - fixed_width - 12)
                header.resizeSection(stretch_column, stretch_width)
            finally:
                tree.setUpdatesEnabled(True)

        def _autofit_archive_tree_columns(self) -> None:
            header = self.archive_tree.header()
            if header is None:
                return
            font_metrics = header.fontMetrics()
            min_widths = {
                0: 280,
                1: max(82, font_metrics.horizontalAdvance("Folder") + 28),
                2: max(96, font_metrics.horizontalAdvance("9999.9 KB") + 28),
                3: max(96, font_metrics.horizontalAdvance("9999.9 KB") + 28),
                4: max(72, font_metrics.horizontalAdvance("Partial") + 28),
                5: max(132, font_metrics.horizontalAdvance("0009/20.pamt") + 28),
            }
            self._fit_tree_columns(self.archive_tree, stretch_column=0, min_widths=min_widths)

        def _apply_column_autofit(self) -> None:
            self._autofit_archive_tree_columns()
            self.replace_assistant_tab.auto_fit_columns()
            self.text_search_tab.auto_fit_columns()
            self.research_tab.auto_fit_columns()

        def _add_combo_choice(self, combo: QComboBox, label: str, value: str) -> None:
            combo.addItem(label, value)

        def _rebuild_archive_extension_filter_choices(self, selected_value: Optional[str] = None) -> None:
            selected_raw = (
                selected_value
                if selected_value is not None
                else (self._combo_value(self.archive_extension_filter_combo) or ARCHIVE_EXTENSION_FILTER)
            )
            preferred_value = normalize_archive_extension_filter(selected_raw)
            entries_by_extension = getattr(self, "archive_entries_by_extension", {})
            if isinstance(entries_by_extension, dict) and entries_by_extension:
                extension_counts = {
                    str(extension): len(items)
                    for extension, items in entries_by_extension.items()
                    if extension and isinstance(items, list)
                }
            else:
                entries = getattr(self, "archive_entries", [])
                extension_counts = Counter(
                    entry.extension for entry in entries if getattr(entry, "extension", "")
                )

            self.archive_extension_filter_combo.blockSignals(True)
            self.archive_extension_filter_combo.clear()
            self._add_combo_choice(self.archive_extension_filter_combo, "All files", "*")

            if extension_counts:
                for extension, count in sorted(extension_counts.items(), key=lambda item: (-item[1], item[0])):
                    self._add_combo_choice(
                        self.archive_extension_filter_combo,
                        f"{extension} ({count:,})",
                        extension,
                    )
            else:
                self._add_combo_choice(self.archive_extension_filter_combo, "DDS only", ".dds")

            if preferred_value and preferred_value not in {"*", "all", ".*"}:
                if self.archive_extension_filter_combo.findData(preferred_value) < 0:
                    self._add_combo_choice(
                        self.archive_extension_filter_combo,
                        f"{preferred_value} (saved)",
                        preferred_value,
                    )

            target_value = preferred_value or "*"
            if self.archive_extension_filter_combo.findData(target_value) < 0:
                target_value = "*"
            self._set_combo_by_value(self.archive_extension_filter_combo, target_value)
            self.archive_extension_filter_combo.blockSignals(False)

        def _combo_value(self, combo: QComboBox) -> str:
            if combo.isEditable():
                text = combo.currentText().strip()
                index = combo.currentIndex()
                item_text = combo.itemText(index).strip() if index >= 0 else ""
                if text and text != item_text:
                    return text
            data = combo.currentData()
            return str(data) if data is not None else combo.currentText().strip()

        def _set_combo_by_value(self, combo: QComboBox, value: str) -> None:
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            elif combo.isEditable():
                combo.setEditText(str(value or "").strip())

        def _next_workflow_profile_id(self) -> str:
            existing_ids = {profile.profile_id for profile in self.workflow_profiles_state}
            index = 1
            while True:
                candidate = f"profile_{index}"
                if candidate not in existing_ids:
                    return candidate
                index += 1

        def _selected_workflow_profile_index(self) -> int:
            item = self.workflow_profiles_tree.currentItem()
            if item is None:
                return -1
            try:
                return int(item.data(0, Qt.UserRole))
            except (TypeError, ValueError):
                return -1

        def _selected_workflow_rule_index(self) -> int:
            item = self.workflow_rules_tree.currentItem()
            if item is None:
                return -1
            try:
                return int(item.data(0, Qt.UserRole))
            except (TypeError, ValueError):
                return -1

        def _workflow_profile_by_id(self, profile_id: str) -> Optional[TextureWorkflowProfile]:
            target = str(profile_id or "").strip()
            for profile in self.workflow_profiles_state:
                if profile.profile_id == target:
                    return profile
            return None

        def _workflow_profile_label(self, profile_id: str) -> str:
            profile = self._workflow_profile_by_id(profile_id)
            return profile.label if profile is not None else ""

        def _workflow_profile_dds_summary(self, profile: TextureWorkflowProfile) -> str:
            parts: List[str] = []
            if profile.format_value:
                parts.append(f"fmt={profile.format_value}")
            if profile.size_value:
                parts.append(f"size={profile.size_value}")
            if profile.mip_value:
                parts.append(f"mips={profile.mip_value}")
            return ", ".join(parts) if parts else "Inherit"

        def _workflow_profile_ncnn_summary(self, profile: TextureWorkflowProfile) -> str:
            parts: List[str] = []
            if profile.ncnn_model_name:
                parts.append(profile.ncnn_model_name)
            if profile.ncnn_scale is not None:
                parts.append(f"{profile.ncnn_scale}x")
            if profile.ncnn_tile_size is not None:
                parts.append(f"tile {profile.ncnn_tile_size}")
            if profile.post_correction_mode:
                parts.append(profile.post_correction_mode)
            if profile.ncnn_extra_args:
                parts.append("extra args")
            return " | ".join(parts) if parts else "Inherit"

        def _workflow_rule_summary(self, rule: TextureRule) -> Tuple[str, str, str, str, str, str, str, str, str]:
            return (
                "Yes" if rule.enabled else "No",
                "Exact" if str(rule.match_mode or "glob").strip().lower() == "exact" else "Glob",
                rule.pattern,
                self._workflow_profile_label(rule.workflow_profile_id) or "(none)",
                rule.semantic_value or "",
                rule.profile_value or "",
                rule.colorspace_value or "",
                rule.alpha_policy_value or "",
                rule.intermediate_value or "",
            )

        def _refresh_workflow_profile_ncnn_model_combo(self) -> None:
            current_value = self._combo_value(self.workflow_profile_ncnn_model_combo)
            models = [self.ncnn_model_combo.itemData(index) for index in range(self.ncnn_model_combo.count())]
            self.workflow_profile_ncnn_model_combo.blockSignals(True)
            self.workflow_profile_ncnn_model_combo.clear()
            self._add_combo_choice(self.workflow_profile_ncnn_model_combo, "Inherit Direct NCNN Model", "")
            seen: set[str] = set()
            for model_name in models:
                normalized = str(model_name or "").strip()
                if not normalized or normalized in seen:
                    continue
                self._add_combo_choice(self.workflow_profile_ncnn_model_combo, normalized, normalized)
                seen.add(normalized)
            if current_value and current_value not in seen:
                self._add_combo_choice(
                    self.workflow_profile_ncnn_model_combo,
                    f"{current_value} (missing)",
                    current_value,
                )
            self._set_combo_by_value(self.workflow_profile_ncnn_model_combo, current_value)
            self.workflow_profile_ncnn_model_combo.blockSignals(False)

        def _refresh_workflow_rule_profile_combo(self, preferred_profile_id: str = "") -> None:
            current_value = preferred_profile_id or self._combo_value(self.workflow_rule_profile_combo)
            self.workflow_rule_profile_combo.blockSignals(True)
            self.workflow_rule_profile_combo.clear()
            self._add_combo_choice(self.workflow_rule_profile_combo, "No Workflow Profile", "")
            for profile in self.workflow_profiles_state:
                self._add_combo_choice(self.workflow_rule_profile_combo, profile.label, profile.profile_id)
            self._set_combo_by_value(self.workflow_rule_profile_combo, current_value)
            self.workflow_rule_profile_combo.blockSignals(False)

        def _set_workflow_profile_custom_controls_state(self, *_args) -> None:
            size_value = self._combo_value(self.workflow_profile_size_combo)
            size_custom = size_value == "__custom__"
            self.workflow_profile_custom_size_widget.setVisible(size_custom)
            self.workflow_profile_custom_width_spin.setEnabled(size_custom)
            self.workflow_profile_custom_height_spin.setEnabled(size_custom)
            mip_value = self._combo_value(self.workflow_profile_mip_combo)
            mip_custom = mip_value == "__custom__"
            self.workflow_profile_custom_mip_spin.setVisible(mip_custom)
            self.workflow_profile_custom_mip_spin.setEnabled(mip_custom)
            tile_enabled = self.workflow_profile_ncnn_tile_override_checkbox.isChecked()
            self.workflow_profile_ncnn_tile_spin.setEnabled(tile_enabled and self._current_upscale_backend() == UPSCALE_BACKEND_REALESRGAN_NCNN)

        def _sync_workflow_editor_state(self, *_args) -> None:
            profile_index = self._selected_workflow_profile_index()
            rule_index = self._selected_workflow_rule_index()
            has_profile = 0 <= profile_index < len(self.workflow_profiles_state)
            has_rule = 0 <= rule_index < len(self.texture_rules_state)
            self.workflow_profile_duplicate_button.setEnabled(has_profile)
            self.workflow_profile_delete_button.setEnabled(has_profile)
            self.workflow_rule_duplicate_button.setEnabled(has_rule)
            self.workflow_rule_delete_button.setEnabled(has_rule)
            self.workflow_rule_move_up_button.setEnabled(has_rule and rule_index > 0)
            self.workflow_rule_move_down_button.setEnabled(has_rule and rule_index < len(self.texture_rules_state) - 1)
            self.workflow_assign_profile_button.setEnabled(
                bool(self.workflow_profiles_state) and bool(self.workflow_matched_files_tree.selectedItems())
            )
            direct_backend_enabled = self._current_upscale_backend() == UPSCALE_BACKEND_REALESRGAN_NCNN
            for widget in (
                self.workflow_profile_ncnn_model_combo,
                self.workflow_profile_ncnn_scale_combo,
                self.workflow_profile_ncnn_tile_override_checkbox,
                self.workflow_profile_ncnn_extra_args_edit,
                self.workflow_profile_post_correction_combo,
            ):
                widget.setEnabled(has_profile and direct_backend_enabled)
            self.workflow_profile_ncnn_tile_spin.setEnabled(
                has_profile and direct_backend_enabled and self.workflow_profile_ncnn_tile_override_checkbox.isChecked()
            )
            self._set_workflow_profile_custom_controls_state()

        def _refresh_workflow_profiles_tree(self, *, select_profile_id: str = "") -> None:
            if not select_profile_id:
                current_index = self._selected_workflow_profile_index()
                if 0 <= current_index < len(self.workflow_profiles_state):
                    select_profile_id = self.workflow_profiles_state[current_index].profile_id
            self._workflow_editor_syncing = True
            try:
                self.workflow_profiles_tree.clear()
                selected_item = None
                for index, profile in enumerate(self.workflow_profiles_state):
                    item = QTreeWidgetItem(
                        [
                            profile.label,
                            profile.action_mode or "inherit",
                            self._workflow_profile_dds_summary(profile),
                            self._workflow_profile_ncnn_summary(profile),
                        ]
                    )
                    item.setData(0, Qt.UserRole, index)
                    item.setData(0, Qt.UserRole + 1, profile.profile_id)
                    self.workflow_profiles_tree.addTopLevelItem(item)
                    if profile.profile_id == select_profile_id:
                        selected_item = item
                if selected_item is None and self.workflow_profiles_tree.topLevelItemCount() > 0:
                    selected_item = self.workflow_profiles_tree.topLevelItem(0)
                if selected_item is not None:
                    self.workflow_profiles_tree.setCurrentItem(selected_item)
            finally:
                self._workflow_editor_syncing = False
            self._refresh_workflow_rule_profile_combo()
            self._update_workflow_profile_detail_widgets()
            self._refresh_workflow_rules_tree()

        def _refresh_workflow_rules_tree(self, *, select_index: Optional[int] = None) -> None:
            if select_index is None:
                current_index = self._selected_workflow_rule_index()
                select_index = current_index if current_index >= 0 else None
            self._workflow_editor_syncing = True
            try:
                self.workflow_rules_tree.clear()
                selected_item = None
                for index, rule in enumerate(self.texture_rules_state):
                    item = QTreeWidgetItem(list(self._workflow_rule_summary(rule)))
                    item.setData(0, Qt.UserRole, index)
                    self.workflow_rules_tree.addTopLevelItem(item)
                    if select_index is not None and index == select_index:
                        selected_item = item
                if selected_item is None and self.workflow_rules_tree.topLevelItemCount() > 0:
                    selected_item = self.workflow_rules_tree.topLevelItem(0)
                if selected_item is not None:
                    self.workflow_rules_tree.setCurrentItem(selected_item)
            finally:
                self._workflow_editor_syncing = False
            self._update_workflow_rule_detail_widgets()

        def _update_workflow_profile_detail_widgets(self, *_args) -> None:
            index = self._selected_workflow_profile_index()
            has_profile = 0 <= index < len(self.workflow_profiles_state)
            profile = self.workflow_profiles_state[index] if has_profile else None
            self._workflow_editor_syncing = True
            try:
                for widget in (
                    self.workflow_profile_name_edit,
                    self.workflow_profile_action_combo,
                    self.workflow_profile_format_combo,
                    self.workflow_profile_size_combo,
                    self.workflow_profile_mip_combo,
                    self.workflow_profile_ncnn_model_combo,
                    self.workflow_profile_ncnn_scale_combo,
                    self.workflow_profile_ncnn_tile_override_checkbox,
                    self.workflow_profile_ncnn_extra_args_edit,
                    self.workflow_profile_post_correction_combo,
                ):
                    widget.setEnabled(has_profile)
                self.workflow_profile_custom_width_spin.setEnabled(has_profile)
                self.workflow_profile_custom_height_spin.setEnabled(has_profile)
                self.workflow_profile_custom_mip_spin.setEnabled(has_profile)
                if profile is None:
                    self.workflow_profile_name_edit.clear()
                    self._set_combo_by_value(self.workflow_profile_action_combo, "")
                    self._set_combo_by_value(self.workflow_profile_format_combo, "")
                    self._set_combo_by_value(self.workflow_profile_size_combo, "")
                    self.workflow_profile_custom_width_spin.setValue(2048)
                    self.workflow_profile_custom_height_spin.setValue(2048)
                    self._set_combo_by_value(self.workflow_profile_mip_combo, "")
                    self.workflow_profile_custom_mip_spin.setValue(1)
                    self._refresh_workflow_profile_ncnn_model_combo()
                    self._set_combo_by_value(self.workflow_profile_ncnn_scale_combo, "")
                    self.workflow_profile_ncnn_tile_override_checkbox.setChecked(False)
                    self.workflow_profile_ncnn_tile_spin.setValue(max(0, self.ncnn_tile_size_spin.value()))
                    self.workflow_profile_ncnn_extra_args_edit.clear()
                    self._set_combo_by_value(self.workflow_profile_post_correction_combo, "")
                else:
                    self.workflow_profile_name_edit.setText(profile.label)
                    self._set_combo_by_value(self.workflow_profile_action_combo, profile.action_mode)
                    self._set_combo_by_value(self.workflow_profile_format_combo, profile.format_value or "")
                    size_value = profile.size_value or ""
                    if "x" in size_value:
                        self._set_combo_by_value(self.workflow_profile_size_combo, "__custom__")
                        width_text, height_text = size_value.split("x", 1)
                        self.workflow_profile_custom_width_spin.setValue(int(width_text))
                        self.workflow_profile_custom_height_spin.setValue(int(height_text))
                    else:
                        self._set_combo_by_value(self.workflow_profile_size_combo, size_value)
                    mip_value = profile.mip_value or ""
                    if mip_value.isdigit():
                        self._set_combo_by_value(self.workflow_profile_mip_combo, "__custom__")
                        self.workflow_profile_custom_mip_spin.setValue(int(mip_value))
                    else:
                        self._set_combo_by_value(self.workflow_profile_mip_combo, mip_value)
                    self._refresh_workflow_profile_ncnn_model_combo()
                    self._set_combo_by_value(self.workflow_profile_ncnn_model_combo, profile.ncnn_model_name)
                    self._set_combo_by_value(
                        self.workflow_profile_ncnn_scale_combo,
                        str(profile.ncnn_scale) if profile.ncnn_scale is not None else "",
                    )
                    self.workflow_profile_ncnn_tile_override_checkbox.setChecked(profile.ncnn_tile_size is not None)
                    self.workflow_profile_ncnn_tile_spin.setValue(
                        int(profile.ncnn_tile_size) if profile.ncnn_tile_size is not None else max(0, self.ncnn_tile_size_spin.value())
                    )
                    self.workflow_profile_ncnn_extra_args_edit.setText(profile.ncnn_extra_args)
                    self._set_combo_by_value(self.workflow_profile_post_correction_combo, profile.post_correction_mode or "")
            finally:
                self._workflow_editor_syncing = False
            self._sync_workflow_editor_state()

        def _update_workflow_rule_detail_widgets(self, *_args) -> None:
            index = self._selected_workflow_rule_index()
            has_rule = 0 <= index < len(self.texture_rules_state)
            rule = self.texture_rules_state[index] if has_rule else None
            self._workflow_editor_syncing = True
            try:
                for widget in (
                    self.workflow_rule_enabled_checkbox,
                    self.workflow_rule_match_mode_combo,
                    self.workflow_rule_pattern_edit,
                    self.workflow_rule_profile_combo,
                    self.workflow_rule_semantic_combo,
                    self.workflow_rule_planner_profile_combo,
                    self.workflow_rule_colorspace_combo,
                    self.workflow_rule_alpha_combo,
                    self.workflow_rule_intermediate_combo,
                ):
                    widget.setEnabled(has_rule)
                if rule is None:
                    self.workflow_rule_enabled_checkbox.setChecked(False)
                    self._set_combo_by_value(self.workflow_rule_match_mode_combo, "glob")
                    self.workflow_rule_pattern_edit.clear()
                    self._refresh_workflow_rule_profile_combo("")
                    self.workflow_rule_semantic_combo.setCurrentText("")
                    self._set_combo_by_value(self.workflow_rule_planner_profile_combo, "")
                    self._set_combo_by_value(self.workflow_rule_colorspace_combo, "")
                    self._set_combo_by_value(self.workflow_rule_alpha_combo, "")
                    self._set_combo_by_value(self.workflow_rule_intermediate_combo, "")
                else:
                    self.workflow_rule_enabled_checkbox.setChecked(rule.enabled)
                    self._set_combo_by_value(self.workflow_rule_match_mode_combo, rule.match_mode)
                    self.workflow_rule_pattern_edit.setText(rule.pattern)
                    self._refresh_workflow_rule_profile_combo(rule.workflow_profile_id)
                    self.workflow_rule_semantic_combo.setCurrentText(rule.semantic_value or "")
                    self._set_combo_by_value(self.workflow_rule_planner_profile_combo, rule.profile_value or "")
                    self._set_combo_by_value(self.workflow_rule_colorspace_combo, rule.colorspace_value or "")
                    self._set_combo_by_value(self.workflow_rule_alpha_combo, rule.alpha_policy_value or "")
                    self._set_combo_by_value(self.workflow_rule_intermediate_combo, rule.intermediate_value or "")
            finally:
                self._workflow_editor_syncing = False
            self._sync_workflow_editor_state()

        def _schedule_workflow_match_refresh(self, *_args) -> None:
            if not self._settings_ready or self._shutting_down or self._workflow_editor_syncing:
                return
            self._workflow_match_refresh_timer.start()

        def _refresh_workflow_matched_files_view(self, *_args) -> None:
            self.workflow_matched_files_tree.clear()
            self.workflow_matched_processing_plan = []
            try:
                config = self.collect_config()
                normalized = normalize_config_for_planning(config)
                dds_files = collect_dds_files(
                    normalized.original_dds_root,
                    normalized.include_filter_patterns,
                )
                if not dds_files:
                    self.workflow_matched_summary_label.setText(
                        "No DDS files matched the current Original DDS root and filter."
                    )
                    self._sync_workflow_editor_state()
                    return
                processing_plan = build_texture_processing_plan(normalized, dds_files)
                self.workflow_matched_processing_plan = list(processing_plan)
                self.workflow_matched_summary_label.setText(
                    f"{len(processing_plan):,} matched DDS file(s). Last matching rule wins. "
                    "Use Assign Profile to append exact-path rules for the selected rows."
                )
                for entry in processing_plan:
                    item = QTreeWidgetItem(
                        [
                            entry.relative_path.as_posix(),
                            f"{entry.decision.texture_type}/{entry.decision.semantic_subtype}",
                            summarize_texture_workflow_rule(entry.matched_rule),
                            entry.workflow_profile.label if entry.workflow_profile is not None else "(none)",
                            summarize_effective_dds_override(entry),
                            summarize_effective_ncnn_settings(normalized, entry),
                            f"{entry.action} | {entry.action_reason}",
                        ]
                    )
                    item.setData(0, Qt.UserRole, entry.relative_path.as_posix())
                    self.workflow_matched_files_tree.addTopLevelItem(item)
            except Exception as exc:
                self.workflow_matched_summary_label.setText(f"Matched files preview unavailable: {exc}")
            self._sync_workflow_editor_state()

        def _apply_selected_workflow_profile_edits(self, *_args) -> None:
            if self._workflow_editor_syncing:
                return
            index = self._selected_workflow_profile_index()
            if not (0 <= index < len(self.workflow_profiles_state)):
                return
            current = self.workflow_profiles_state[index]
            size_value = self._combo_value(self.workflow_profile_size_combo)
            if size_value == "__custom__":
                size_value = f"{self.workflow_profile_custom_width_spin.value()}x{self.workflow_profile_custom_height_spin.value()}"
            mip_value = self._combo_value(self.workflow_profile_mip_combo)
            if mip_value == "__custom__":
                mip_value = str(self.workflow_profile_custom_mip_spin.value())
            updated = TextureWorkflowProfile(
                profile_id=current.profile_id,
                label=self.workflow_profile_name_edit.text().strip() or current.label,
                action_mode=self._combo_value(self.workflow_profile_action_combo),
                format_value=self._combo_value(self.workflow_profile_format_combo) or None,
                size_value=size_value or None,
                mip_value=mip_value or None,
                ncnn_model_name=self._combo_value(self.workflow_profile_ncnn_model_combo),
                ncnn_scale=int(self._combo_value(self.workflow_profile_ncnn_scale_combo)) if self._combo_value(self.workflow_profile_ncnn_scale_combo) else None,
                ncnn_tile_size=self.workflow_profile_ncnn_tile_spin.value() if self.workflow_profile_ncnn_tile_override_checkbox.isChecked() else None,
                ncnn_extra_args=self.workflow_profile_ncnn_extra_args_edit.text().strip(),
                post_correction_mode=self._combo_value(self.workflow_profile_post_correction_combo),
            )
            self.workflow_profiles_state[index] = updated
            self._refresh_workflow_profiles_tree(select_profile_id=updated.profile_id)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _apply_selected_workflow_rule_edits(self, *_args) -> None:
            if self._workflow_editor_syncing:
                return
            index = self._selected_workflow_rule_index()
            if not (0 <= index < len(self.texture_rules_state)):
                return
            current = self.texture_rules_state[index]
            pattern_text = self.workflow_rule_pattern_edit.text().strip() or current.pattern
            updated = TextureRule(
                pattern=pattern_text,
                action=current.action,
                format_value=current.format_value,
                size_value=current.size_value,
                mip_value=current.mip_value,
                semantic_value=self.workflow_rule_semantic_combo.currentText().strip().lower() or None,
                profile_value=self._combo_value(self.workflow_rule_planner_profile_combo) or None,
                colorspace_value=self._combo_value(self.workflow_rule_colorspace_combo) or None,
                alpha_policy_value=self._combo_value(self.workflow_rule_alpha_combo) or None,
                intermediate_value=self._combo_value(self.workflow_rule_intermediate_combo) or None,
                enabled=self.workflow_rule_enabled_checkbox.isChecked(),
                match_mode=self._combo_value(self.workflow_rule_match_mode_combo) or "glob",
                workflow_profile_id=self._combo_value(self.workflow_rule_profile_combo),
                source_line=current.source_line or pattern_text,
            )
            self.texture_rules_state[index] = updated
            self._refresh_workflow_rules_tree(select_index=index)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _add_workflow_profile(self, *_args) -> None:
            new_profile = TextureWorkflowProfile(
                profile_id=self._next_workflow_profile_id(),
                label=f"Profile {len(self.workflow_profiles_state) + 1}",
            )
            self.workflow_profiles_state.append(new_profile)
            self._refresh_workflow_profiles_tree(select_profile_id=new_profile.profile_id)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _duplicate_workflow_profile(self, *_args) -> None:
            index = self._selected_workflow_profile_index()
            if not (0 <= index < len(self.workflow_profiles_state)):
                return
            current = self.workflow_profiles_state[index]
            duplicated = dataclasses.replace(
                current,
                profile_id=self._next_workflow_profile_id(),
                label=f"{current.label} Copy",
            )
            self.workflow_profiles_state.insert(index + 1, duplicated)
            self._refresh_workflow_profiles_tree(select_profile_id=duplicated.profile_id)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _delete_workflow_profile(self, *_args) -> None:
            index = self._selected_workflow_profile_index()
            if not (0 <= index < len(self.workflow_profiles_state)):
                return
            profile_id = self.workflow_profiles_state[index].profile_id
            del self.workflow_profiles_state[index]
            for rule_index, rule in enumerate(self.texture_rules_state):
                if rule.workflow_profile_id == profile_id:
                    self.texture_rules_state[rule_index] = dataclasses.replace(rule, workflow_profile_id="")
            self._refresh_workflow_profiles_tree()
            self._refresh_workflow_rules_tree()
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _add_workflow_rule(self, *_args) -> None:
            rule = TextureRule(pattern="*.dds", enabled=True, match_mode="glob")
            self.texture_rules_state.append(rule)
            self._refresh_workflow_rules_tree(select_index=len(self.texture_rules_state) - 1)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _duplicate_workflow_rule(self, *_args) -> None:
            index = self._selected_workflow_rule_index()
            if not (0 <= index < len(self.texture_rules_state)):
                return
            duplicated = dataclasses.replace(self.texture_rules_state[index])
            self.texture_rules_state.insert(index + 1, duplicated)
            self._refresh_workflow_rules_tree(select_index=index + 1)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _delete_workflow_rule(self, *_args) -> None:
            index = self._selected_workflow_rule_index()
            if not (0 <= index < len(self.texture_rules_state)):
                return
            del self.texture_rules_state[index]
            next_index = min(index, len(self.texture_rules_state) - 1)
            self._refresh_workflow_rules_tree(select_index=next_index if next_index >= 0 else None)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _move_workflow_rule(self, offset: int, *_args) -> None:
            index = self._selected_workflow_rule_index()
            target_index = index + offset
            if not (0 <= index < len(self.texture_rules_state)):
                return
            if not (0 <= target_index < len(self.texture_rules_state)):
                return
            self.texture_rules_state[index], self.texture_rules_state[target_index] = (
                self.texture_rules_state[target_index],
                self.texture_rules_state[index],
            )
            self._refresh_workflow_rules_tree(select_index=target_index)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _assign_profile_to_selected_workflow_matches(self, *_args) -> None:
            selected_items = self.workflow_matched_files_tree.selectedItems()
            if not selected_items or not self.workflow_profiles_state:
                return
            profile_labels = [profile.label for profile in self.workflow_profiles_state]
            selected_label, accepted = QInputDialog.getItem(
                self,
                "Assign Workflow Profile",
                "Choose a workflow profile for the selected files:",
                profile_labels,
                0,
                False,
            )
            if not accepted or not selected_label:
                return
            selected_profile = next((profile for profile in self.workflow_profiles_state if profile.label == selected_label), None)
            if selected_profile is None:
                return
            for item in selected_items:
                relative_path = str(item.data(0, Qt.UserRole) or "").strip()
                if not relative_path:
                    continue
                self.texture_rules_state.append(
                    TextureRule(
                        pattern=relative_path,
                        enabled=True,
                        match_mode="exact",
                        workflow_profile_id=selected_profile.profile_id,
                        source_line=relative_path,
                    )
                )
            self._refresh_workflow_rules_tree(select_index=len(self.texture_rules_state) - 1)
            self.schedule_settings_save()
            self._schedule_workflow_match_refresh()

        def _apply_workflow_state_from_config(self, config: AppConfig) -> None:
            legacy_text = str(getattr(config, "texture_rules_text", "") or "")
            workflow_profiles = list(coerce_texture_workflow_profiles(getattr(config, "workflow_profiles", ())))
            texture_rules = list(coerce_texture_workflow_rules(getattr(config, "texture_rules", ())))
            if not workflow_profiles and not texture_rules and legacy_text.strip():
                migrated_profiles, migrated_rules = migrate_legacy_texture_rules_to_structured(legacy_text)
                workflow_profiles = list(migrated_profiles)
                texture_rules = list(migrated_rules)
            elif should_seed_default_texture_workflow_state(workflow_profiles, texture_rules):
                workflow_profiles = list(build_default_texture_workflow_profiles())
                texture_rules = list(build_default_texture_workflow_rules())
            workflow_profiles_tuple, texture_rules_tuple = upgrade_default_texture_workflow_state(workflow_profiles, texture_rules)
            self.texture_rules_legacy_text = legacy_text
            self.workflow_profiles_state = list(workflow_profiles_tuple)
            self.texture_rules_state = list(texture_rules_tuple)
            self._refresh_workflow_profile_ncnn_model_combo()
            self._refresh_workflow_profiles_tree()
            self._refresh_workflow_rules_tree()
            self._schedule_workflow_match_refresh()

        def _connect_auto_save(self) -> None:
            line_edits = [
                self.original_dds_edit,
                self.png_root_edit,
                self.texture_editor_png_root_edit,
                self.dds_staging_root_edit,
                self.output_root_edit,
                self.texconv_path_edit,
                self.csv_log_path_edit,
                self.chainner_exe_path_edit,
                self.chainner_chain_path_edit,
                self.ncnn_exe_path_edit,
                self.ncnn_model_dir_edit,
                self.ncnn_extra_args_edit,
                self.mod_ready_export_root_edit,
                self.mod_ready_package_title_edit,
                self.mod_ready_package_version_edit,
                self.mod_ready_package_author_edit,
                self.mod_ready_package_description_edit,
                self.mod_ready_package_nexus_url_edit,
                self.archive_package_root_edit,
                self.archive_extract_root_edit,
            ]
            for line_edit in line_edits:
                line_edit.textChanged.connect(self.schedule_settings_save)

            checkboxes = [
                self.dry_run_checkbox,
                self.enable_dds_staging_checkbox,
                self.enable_incremental_resume_checkbox,
                self.csv_log_enabled_checkbox,
                self.unique_basename_checkbox,
                self.overwrite_existing_checkbox,
                self.enable_automatic_texture_rules_checkbox,
                self.enable_unsafe_technical_override_checkbox,
                self.retry_smaller_tile_checkbox,
                self.enable_mod_ready_loose_export_checkbox,
                self.mod_ready_create_no_encrypt_checkbox,
            ]
            for checkbox in checkboxes:
                checkbox.toggled.connect(self.schedule_settings_save)

            combos = [
                self.dds_format_mode_combo,
                self.dds_custom_format_combo,
                self.dds_size_mode_combo,
                self.dds_mip_mode_combo,
                self.upscale_backend_combo,
                self.ncnn_model_combo,
                self.upscale_post_correction_combo,
                self.upscale_texture_preset_combo,
                self.compare_preview_size_combo,
            ]
            for combo in combos:
                combo.currentIndexChanged.connect(self.schedule_settings_save)

            spins = [
                self.dds_custom_width_spin,
                self.dds_custom_height_spin,
                self.dds_custom_mip_spin,
                self.ncnn_scale_spin,
                self.ncnn_tile_size_spin,
            ]
            for spin in spins:
                spin.valueChanged.connect(self.schedule_settings_save)

            self.csv_log_enabled_checkbox.toggled.connect(self._apply_csv_log_enabled_state)
            self.upscale_backend_combo.currentIndexChanged.connect(self._apply_upscale_backend_state)
            self.enable_dds_staging_checkbox.toggled.connect(self._apply_dds_staging_enabled_state)
            self.png_root_edit.textChanged.connect(lambda *_args: self._apply_upscale_backend_state())
            self.dds_staging_root_edit.textChanged.connect(lambda *_args: self._apply_upscale_backend_state())
            self.output_root_edit.textChanged.connect(lambda *_args: self._apply_upscale_backend_state())
            self.dds_format_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.dds_size_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.dds_mip_mode_combo.currentIndexChanged.connect(self._apply_dds_output_state)
            self.upscale_texture_preset_combo.currentIndexChanged.connect(self._update_ncnn_preset_hint)
            self.enable_unsafe_technical_override_checkbox.toggled.connect(self._update_ncnn_preset_hint)
            self.safe_upscale_wizard_button.clicked.connect(self.open_run_summary)
            self.ncnn_model_refresh_button.clicked.connect(self._refresh_ncnn_model_picker)
            self.ncnn_model_catalog_button.clicked.connect(self.open_ncnn_model_catalog)
            self.ncnn_exe_path_edit.textChanged.connect(self._refresh_ncnn_model_picker)
            self.ncnn_model_dir_edit.textChanged.connect(self._refresh_ncnn_model_picker)
            self.mod_ready_export_browse_button.clicked.connect(self._browse_mod_ready_export_root)
            self.enable_mod_ready_loose_export_checkbox.toggled.connect(self._apply_mod_ready_export_state)
            self.compare_sync_pan_checkbox.toggled.connect(self.schedule_settings_save)
            self.compare_preview_size_combo.currentIndexChanged.connect(self._apply_compare_preview_size_mode)
            self.main_tabs.currentChanged.connect(self._handle_main_tab_changed)
            self.content_tabs.currentChanged.connect(self._handle_workflow_content_tab_changed)
            self.workflow_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.workflow_right_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.compare_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.archive_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.replace_assistant_tab.main_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.main_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.groups_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.unknown_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.reference_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.analysis_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.research_tab.notes_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.text_search_tab.main_splitter.splitterMoved.connect(lambda *_args: self.schedule_settings_save())
            self.setup_section.toggled.connect(self.schedule_settings_save)
            self.paths_section.toggled.connect(self.schedule_settings_save)
            self.settings_section.toggled.connect(self.schedule_settings_save)
            self.dds_output_section.toggled.connect(self.schedule_settings_save)
            self.filters_section.toggled.connect(self.schedule_settings_save)
            self.chainner_section.toggled.connect(self.schedule_settings_save)
            self.filters_edit.textChanged.connect(self.schedule_settings_save)
            self.chainner_override_edit.textChanged.connect(self.schedule_settings_save)
            self.chainner_chain_path_edit.textChanged.connect(self._schedule_chainner_chain_info_refresh)
            self.chainner_override_edit.textChanged.connect(self._schedule_chainner_chain_info_refresh)
            self.workflow_profiles_tree.currentItemChanged.connect(lambda *_args: self._update_workflow_profile_detail_widgets())
            self.workflow_rules_tree.currentItemChanged.connect(lambda *_args: self._update_workflow_rule_detail_widgets())
            self.workflow_matched_files_tree.itemSelectionChanged.connect(self._sync_workflow_editor_state)
            self.workflow_profile_add_button.clicked.connect(self._add_workflow_profile)
            self.workflow_profile_duplicate_button.clicked.connect(self._duplicate_workflow_profile)
            self.workflow_profile_delete_button.clicked.connect(self._delete_workflow_profile)
            self.workflow_rule_add_button.clicked.connect(self._add_workflow_rule)
            self.workflow_rule_duplicate_button.clicked.connect(self._duplicate_workflow_rule)
            self.workflow_rule_delete_button.clicked.connect(self._delete_workflow_rule)
            self.workflow_rule_move_up_button.clicked.connect(lambda: self._move_workflow_rule(-1))
            self.workflow_rule_move_down_button.clicked.connect(lambda: self._move_workflow_rule(1))
            self.workflow_matched_refresh_button.clicked.connect(self._refresh_workflow_matched_files_view)
            self.workflow_assign_profile_button.clicked.connect(self._assign_profile_to_selected_workflow_matches)
            self.workflow_profile_name_edit.editingFinished.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_action_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_format_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_size_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_size_combo.currentIndexChanged.connect(self._set_workflow_profile_custom_controls_state)
            self.workflow_profile_custom_width_spin.valueChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_custom_height_spin.valueChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_mip_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_mip_combo.currentIndexChanged.connect(self._set_workflow_profile_custom_controls_state)
            self.workflow_profile_custom_mip_spin.valueChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_ncnn_model_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_ncnn_scale_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_ncnn_tile_override_checkbox.toggled.connect(self._set_workflow_profile_custom_controls_state)
            self.workflow_profile_ncnn_tile_override_checkbox.toggled.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_ncnn_tile_spin.valueChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_ncnn_extra_args_edit.editingFinished.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_profile_post_correction_combo.currentIndexChanged.connect(self._apply_selected_workflow_profile_edits)
            self.workflow_rule_enabled_checkbox.toggled.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_match_mode_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_pattern_edit.editingFinished.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_profile_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_semantic_combo.currentTextChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_semantic_combo.lineEdit().editingFinished.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_planner_profile_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_colorspace_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_alpha_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            self.workflow_rule_intermediate_combo.currentIndexChanged.connect(self._apply_selected_workflow_rule_edits)
            for widget in (
                self.original_dds_edit,
                self.filters_edit,
                self.png_root_edit,
                self.texture_editor_png_root_edit,
                self.dds_staging_root_edit,
                self.output_root_edit,
                self.texconv_path_edit,
            ):
                widget.textChanged.connect(self._schedule_workflow_match_refresh)
            for checkbox in (
                self.enable_automatic_texture_rules_checkbox,
                self.enable_unsafe_technical_override_checkbox,
                self.enable_dds_staging_checkbox,
            ):
                checkbox.toggled.connect(self._schedule_workflow_match_refresh)
            for combo in (
                self.dds_format_mode_combo,
                self.dds_custom_format_combo,
                self.dds_size_mode_combo,
                self.dds_mip_mode_combo,
                self.upscale_backend_combo,
                self.ncnn_model_combo,
                self.upscale_post_correction_combo,
                self.upscale_texture_preset_combo,
            ):
                combo.currentIndexChanged.connect(self._schedule_workflow_match_refresh)
            for spin in (
                self.dds_custom_width_spin,
                self.dds_custom_height_spin,
                self.dds_custom_mip_spin,
                self.ncnn_scale_spin,
                self.ncnn_tile_size_spin,
            ):
                spin.valueChanged.connect(self._schedule_workflow_match_refresh)

        def _handle_main_tab_changed(self, index: int) -> None:
            if 0 <= index < self.main_tabs.count() and self.main_tabs.widget(index) is self.workflow_tab:
                self._apply_workflow_content_tab_layout()
            if 0 <= index < self.main_tabs.count() and self.main_tabs.widget(index) is self.archive_browser_tab:
                self._refresh_archive_browser_if_pending()
            if 0 <= index < self.main_tabs.count() and self.main_tabs.widget(index) is self.research_tab:
                self.research_tab.refresh_archive_picker_if_pending()
            self._save_settings()

        def _refresh_archive_browser_view(self) -> None:
            self._cancel_archive_tree_population()
            self._rebuild_archive_extension_filter_choices()
            self._rebuild_archive_structure_filter_controls()
            rebuild_tree_index = self._archive_tree_view_enabled() and not self.archive_tree_index_ready
            self._populate_archive_tree(rebuild_index=rebuild_tree_index)
            self.archive_browser_refresh_pending = False

        def _refresh_archive_browser_if_pending(self) -> None:
            if self.archive_browser_refresh_pending:
                self._refresh_archive_browser_view()

        def _refresh_or_defer_archive_browser_view(self, *, activate_tab: bool) -> None:
            if activate_tab:
                self.main_tabs.setCurrentWidget(self.archive_browser_tab)
            if self.main_tabs.currentWidget() is self.archive_browser_tab:
                self._refresh_archive_browser_view()
            else:
                self.archive_browser_refresh_pending = True

        def _refresh_or_defer_research_archive_picker(self) -> None:
            if self.main_tabs.currentWidget() is self.research_tab:
                self.research_tab.refresh_archive_picker()
            else:
                self.research_tab.mark_archive_picker_dirty()

        def _default_workflow_right_splitter_sizes(self) -> List[int]:
            available_right_height = max(420, self.height() - 260)
            progress_min_height = getattr(self, "progress_group_min_height", 190)
            progress_height = min(
                max(progress_min_height, int(available_right_height * 0.18)),
                max(progress_min_height, 210),
            )
            return [progress_height, max(320, available_right_height - progress_height)]

        def _apply_workflow_content_tab_layout(self, *_args) -> None:
            compare_active = (
                self.main_tabs.currentWidget() is self.workflow_tab
                and self.content_tabs.currentWidget() is self.compare_tab
            )
            if compare_active:
                current_sizes = self.workflow_right_splitter.sizes()
                if len(current_sizes) >= 2 and current_sizes[0] > 0:
                    self.workflow_right_splitter_normal_sizes = current_sizes
                self.progress_group.setVisible(False)
                self.workflow_right_splitter.setHandleWidth(0)
                self.workflow_right_splitter.setSizes([0, max(1, self.workflow_right_splitter.height())])
                return

            self.progress_group.setVisible(True)
            self.workflow_right_splitter.setHandleWidth(4)
            restore_sizes = self.workflow_right_splitter_normal_sizes or self._default_workflow_right_splitter_sizes()
            self.workflow_right_splitter.setSizes(restore_sizes)

        def _handle_workflow_content_tab_changed(self, index: int) -> None:
            del index
            self._apply_workflow_content_tab_layout()
            self._save_settings()

        def _save_settings(self) -> None:
            if not self._settings_ready:
                return
            self.settings.setValue("appearance/theme", self.current_theme_key)
            self.settings.setValue("paths/original_dds_root", self.original_dds_edit.text())
            self.settings.setValue("paths/png_root", self.png_root_edit.text())
            self.settings.setValue("paths/texture_editor_png_root", self.texture_editor_png_root_edit.text())
            self.settings.setValue("paths/dds_staging_root", self.dds_staging_root_edit.text())
            self.settings.setValue("paths/output_root", self.output_root_edit.text())
            self.settings.setValue("paths/texconv_path", self.texconv_path_edit.text())
            self.settings.setValue("archive/package_root", self.archive_package_root_edit.text())
            self.settings.setValue("archive/extract_root", self.archive_extract_root_edit.text())
            self.settings.setValue("archive/filter_text", self.archive_filter_edit.text())
            self.settings.setValue("archive/exclude_filter_text", self.archive_exclude_filter_edit.text())
            self.settings.setValue("archive/extension_filter", self._combo_value(self.archive_extension_filter_combo))
            self.settings.setValue("archive/package_filter_text", self.archive_package_filter_edit.text())
            self.settings.setValue("archive/structure_filter", self._current_archive_structure_filter_value())
            self.settings.setValue("archive/role_filter", self._combo_value(self.archive_role_filter_combo))
            self.settings.setValue(
                "archive/exclude_common_technical_suffixes",
                self.archive_exclude_common_technical_checkbox.isChecked(),
            )
            self.settings.setValue("archive/min_size_kb", self.archive_min_size_spin.value())
            self.settings.setValue("archive/previewable_only", self.archive_previewable_only_checkbox.isChecked())
            self.settings.setValue("archive/tree_view", self.archive_tree_view_checkbox.isChecked())
            self.settings.setValue("archive/model_use_textures", self.archive_model_texture_toggle.isChecked())
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
            self.settings.setValue("settings/texture_rules_text", self.texture_rules_legacy_text)
            self.settings.setValue(
                "settings/workflow_profiles_json",
                json.dumps([dataclasses.asdict(profile) for profile in self.workflow_profiles_state], indent=2),
            )
            self.settings.setValue(
                "settings/workflow_rules_json",
                json.dumps([dataclasses.asdict(rule) for rule in self.texture_rules_state], indent=2),
            )
            current_upscale_backend = self._current_upscale_backend()
            self.settings.setValue("upscale/backend", current_upscale_backend)
            self.settings.setValue("chainner/enabled", current_upscale_backend == UPSCALE_BACKEND_CHAINNER)
            self.settings.setValue("chainner/exe_path", self.chainner_exe_path_edit.text())
            self.settings.setValue("chainner/chain_path", self.chainner_chain_path_edit.text())
            self.settings.setValue("chainner/override_json", self.chainner_override_edit.toPlainText())
            self.settings.setValue("ncnn/exe_path", self.ncnn_exe_path_edit.text())
            self.settings.setValue("ncnn/model_dir", self.ncnn_model_dir_edit.text())
            self.settings.setValue("ncnn/model_name", self._combo_value(self.ncnn_model_combo))
            self.settings.setValue("ncnn/scale", self.ncnn_scale_spin.value())
            self.settings.setValue("ncnn/tile_size", self.ncnn_tile_size_spin.value())
            self.settings.setValue("ncnn/extra_args", self.ncnn_extra_args_edit.text())
            self.settings.setValue("upscale/post_correction_mode", self._combo_value(self.upscale_post_correction_combo))
            self.settings.setValue("ncnn/texture_preset", self._combo_value(self.upscale_texture_preset_combo))
            self.settings.setValue("upscale/automatic_texture_rules", self.enable_automatic_texture_rules_checkbox.isChecked())
            self.settings.setValue("upscale/unsafe_technical_override", self.enable_unsafe_technical_override_checkbox.isChecked())
            self.settings.setValue("upscale/retry_smaller_tile", self.retry_smaller_tile_checkbox.isChecked())
            self.settings.setValue("upscale/mod_ready_loose_export", self.enable_mod_ready_loose_export_checkbox.isChecked())
            self.settings.setValue("upscale/mod_ready_export_root", self.mod_ready_export_root_edit.text())
            self.settings.setValue("upscale/mod_ready_create_no_encrypt", self.mod_ready_create_no_encrypt_checkbox.isChecked())
            self.settings.setValue("upscale/mod_ready_package_title", self.mod_ready_package_title_edit.text())
            self.settings.setValue("upscale/mod_ready_package_version", self.mod_ready_package_version_edit.text())
            self.settings.setValue("upscale/mod_ready_package_author", self.mod_ready_package_author_edit.text())
            self.settings.setValue("upscale/mod_ready_package_description", self.mod_ready_package_description_edit.text())
            self.settings.setValue("upscale/mod_ready_package_nexus_url", self.mod_ready_package_nexus_url_edit.text())
            self.settings.setValue("ui/main_tab_index", self.main_tabs.currentIndex())
            self.settings.setValue("ui/compare_sync_pan", self.compare_sync_pan_checkbox.isChecked())
            self.settings.setValue("ui/compare_preview_size_mode", self._combo_value(self.compare_preview_size_combo))
            if self._preference_bool("remember_splitter_sizes", True):
                self.settings.setValue("ui/workflow_splitter_sizes", ",".join(str(value) for value in self.workflow_splitter.sizes()))
                workflow_right_sizes = (
                    self.workflow_right_splitter_normal_sizes
                    if self.progress_group.isHidden() and self.workflow_right_splitter_normal_sizes
                    else self.workflow_right_splitter.sizes()
                )
                self.settings.setValue(
                    "ui/workflow_right_splitter_sizes_v2",
                    ",".join(str(value) for value in workflow_right_sizes),
                )
                self.settings.setValue(
                    "ui/compare_splitter_sizes_v2",
                    ",".join(str(value) for value in self.compare_splitter.sizes()),
                )
                self.settings.setValue("ui/archive_splitter_sizes", ",".join(str(value) for value in self.archive_splitter.sizes()))
                self.settings.setValue("ui/text_search_splitter_sizes", ",".join(str(value) for value in self.text_search_tab.splitter_sizes()))
                self.settings.setValue(
                    "ui/replace_assistant_splitter_sizes",
                    ",".join(str(value) for value in self.replace_assistant_tab.splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_main_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.main_splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_groups_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.groups_splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_unknown_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.unknown_splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_reference_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.reference_splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_analysis_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.analysis_splitter_sizes()),
                )
                self.settings.setValue(
                    "ui/research_notes_splitter_sizes",
                    ",".join(str(value) for value in self.research_tab.notes_splitter_sizes()),
                )
            self.settings.setValue("sections/setup_expanded", self.setup_section.toggle_button.isChecked())
            self.settings.setValue("sections/paths_expanded", self.paths_section.toggle_button.isChecked())
            self.settings.setValue("sections/settings_expanded", self.settings_section.toggle_button.isChecked())
            self.settings.setValue("sections/dds_output_expanded", self.dds_output_section.toggle_button.isChecked())
            self.settings.setValue("sections/filters_expanded", self.filters_section.toggle_button.isChecked())
            self.settings.setValue("sections/chainner_expanded", self.chainner_section.toggle_button.isChecked())
            self.settings.sync()

        def schedule_settings_save(self, *_args) -> None:
            if not self._settings_ready or self._shutting_down:
                return
            self._settings_save_timer.start()

        def flush_settings_save(self) -> None:
            if self._settings_save_timer.isActive():
                self._settings_save_timer.stop()
            self._save_settings()

        def _load_settings(self) -> None:
            defaults = default_config()
            self.current_theme_key = str(self.settings.value("appearance/theme", self.current_theme_key or DEFAULT_UI_THEME))
            if self.current_theme_key not in UI_THEME_SCHEMES:
                self.current_theme_key = DEFAULT_UI_THEME
            self.original_dds_edit.setText(
                self.settings.value("paths/original_dds_root", defaults.original_dds_root)
            )
            self.png_root_edit.setText(self.settings.value("paths/png_root", defaults.png_root))
            self.texture_editor_png_root_edit.setText(
                self.settings.value("paths/texture_editor_png_root", getattr(defaults, "texture_editor_png_root", ""))
            )
            self.dds_staging_root_edit.setText(self.settings.value("paths/dds_staging_root", defaults.dds_staging_root))
            self.output_root_edit.setText(self.settings.value("paths/output_root", defaults.output_root))
            self.texconv_path_edit.setText(self.settings.value("paths/texconv_path", defaults.texconv_path))
            self.archive_package_root_edit.setText(self.settings.value("archive/package_root", defaults.archive_package_root))
            self.archive_extract_root_edit.setText(self.settings.value("archive/extract_root", defaults.archive_extract_root))
            self.archive_filter_edit.setText(self.settings.value("archive/filter_text", defaults.archive_filter_text))
            self.archive_exclude_filter_edit.setText(
                self.settings.value("archive/exclude_filter_text", defaults.archive_exclude_filter_text)
            )
            self._rebuild_archive_extension_filter_choices(
                str(self.settings.value("archive/extension_filter", defaults.archive_extension_filter))
            )
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
            self.archive_exclude_common_technical_checkbox.setChecked(
                str(
                    self.settings.value(
                        "archive/exclude_common_technical_suffixes",
                        defaults.archive_exclude_common_technical_suffixes,
                    )
                ).lower()
                in {"1", "true", "yes"}
            )
            self.archive_min_size_spin.setValue(
                int(self.settings.value("archive/min_size_kb", defaults.archive_min_size_kb))
            )
            self.archive_previewable_only_checkbox.setChecked(
                self._read_bool("archive/previewable_only", defaults.archive_previewable_only)
            )
            self.archive_tree_view_checkbox.setChecked(self._read_bool("archive/tree_view", True))
            self.archive_model_texture_toggle.setChecked(self._read_bool("archive/model_use_textures", False))
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
            legacy_texture_rules_text = str(
                self.settings.value("settings/texture_rules_text", getattr(defaults, "texture_rules_text", ""))
                or ""
            )
            workflow_profiles_json = str(self.settings.value("settings/workflow_profiles_json", "") or "")
            workflow_rules_json = str(self.settings.value("settings/workflow_rules_json", "") or "")
            loaded_workflow_profiles: Sequence[object] = ()
            loaded_workflow_rules: Sequence[object] = ()
            if workflow_profiles_json.strip():
                try:
                    parsed_profiles = json.loads(workflow_profiles_json)
                    if isinstance(parsed_profiles, list):
                        loaded_workflow_profiles = parsed_profiles
                except Exception:
                    loaded_workflow_profiles = ()
            if workflow_rules_json.strip():
                try:
                    parsed_rules = json.loads(workflow_rules_json)
                    if isinstance(parsed_rules, list):
                        loaded_workflow_rules = parsed_rules
                except Exception:
                    loaded_workflow_rules = ()
            self.texture_rules_legacy_text = legacy_texture_rules_text
            self.workflow_profiles_state = list(coerce_texture_workflow_profiles(loaded_workflow_profiles))
            self.texture_rules_state = list(coerce_texture_workflow_rules(loaded_workflow_rules))
            if not self.workflow_profiles_state and not self.texture_rules_state and legacy_texture_rules_text.strip():
                migrated_profiles, migrated_rules = migrate_legacy_texture_rules_to_structured(legacy_texture_rules_text)
                self.workflow_profiles_state = list(migrated_profiles)
                self.texture_rules_state = list(migrated_rules)
            elif should_seed_default_texture_workflow_state(self.workflow_profiles_state, self.texture_rules_state):
                self.workflow_profiles_state = list(build_default_texture_workflow_profiles())
                self.texture_rules_state = list(build_default_texture_workflow_rules())
            upgraded_profiles, upgraded_rules = upgrade_default_texture_workflow_state(
                self.workflow_profiles_state,
                self.texture_rules_state,
            )
            self.workflow_profiles_state = list(upgraded_profiles)
            self.texture_rules_state = list(upgraded_rules)
            saved_backend = str(self.settings.value("upscale/backend", "") or "").strip()
            if saved_backend not in {
                UPSCALE_BACKEND_NONE,
                UPSCALE_BACKEND_CHAINNER,
                UPSCALE_BACKEND_REALESRGAN_NCNN,
            }:
                saved_backend = UPSCALE_BACKEND_CHAINNER if self._read_bool("chainner/enabled", defaults.enable_chainner) else DEFAULT_UPSCALE_BACKEND
            self._set_combo_by_value(self.upscale_backend_combo, saved_backend)
            self.chainner_exe_path_edit.setText(
                self.settings.value("chainner/exe_path", defaults.chainner_exe_path)
            )
            self.chainner_chain_path_edit.setText(
                self.settings.value("chainner/chain_path", defaults.chainner_chain_path)
            )
            self.chainner_override_edit.setPlainText(
                self.settings.value("chainner/override_json", defaults.chainner_override_json)
            )
            self.ncnn_exe_path_edit.setText(
                self.settings.value("ncnn/exe_path", getattr(defaults, "ncnn_exe_path", REALESRGAN_NCNN_EXE_PATH))
            )
            self.ncnn_model_dir_edit.setText(
                self.settings.value("ncnn/model_dir", getattr(defaults, "ncnn_model_dir", REALESRGAN_NCNN_MODEL_DIR))
            )
            self.ncnn_extra_args_edit.setText(
                str(self.settings.value("ncnn/extra_args", getattr(defaults, "ncnn_extra_args", REALESRGAN_NCNN_EXTRA_ARGS)))
            )
            self.ncnn_scale_spin.setValue(
                int(self.settings.value("ncnn/scale", getattr(defaults, "ncnn_scale", REALESRGAN_NCNN_SCALE)))
            )
            self.ncnn_tile_size_spin.setValue(
                int(self.settings.value("ncnn/tile_size", getattr(defaults, "ncnn_tile_size", REALESRGAN_NCNN_TILE_SIZE)))
            )
            self._set_combo_by_value(
                self.upscale_post_correction_combo,
                str(
                    self.settings.value(
                        "upscale/post_correction_mode",
                        getattr(defaults, "upscale_post_correction_mode", DEFAULT_UPSCALE_POST_CORRECTION),
                    )
                ),
            )
            self._set_combo_by_value(
                self.upscale_texture_preset_combo,
                str(
                    self.settings.value(
                        "ncnn/texture_preset",
                        getattr(defaults, "upscale_texture_preset", DEFAULT_UPSCALE_TEXTURE_PRESET),
                    )
                ),
            )
            self._refresh_ncnn_model_picker(
                preferred_name=str(
                    self.settings.value(
                        "ncnn/model_name",
                        getattr(defaults, "ncnn_model_name", REALESRGAN_NCNN_MODEL_NAME),
                    )
                )
            )
            self.enable_automatic_texture_rules_checkbox.setChecked(
                self._read_bool(
                    "upscale/automatic_texture_rules",
                    getattr(defaults, "enable_automatic_texture_rules", ENABLE_AUTOMATIC_TEXTURE_RULES),
                )
            )
            self.enable_unsafe_technical_override_checkbox.setChecked(
                self._read_bool(
                    "upscale/unsafe_technical_override",
                    getattr(defaults, "enable_unsafe_technical_override", ENABLE_UNSAFE_TECHNICAL_OVERRIDE),
                )
            )
            self.retry_smaller_tile_checkbox.setChecked(
                self._read_bool(
                    "upscale/retry_smaller_tile",
                    getattr(defaults, "retry_smaller_tile_on_failure", RETRY_SMALLER_TILE_ON_FAILURE),
                )
            )
            self.enable_mod_ready_loose_export_checkbox.setChecked(
                self._read_bool(
                    "upscale/mod_ready_loose_export",
                    getattr(defaults, "enable_mod_ready_loose_export", ENABLE_MOD_READY_LOOSE_EXPORT),
                )
            )
            self.mod_ready_export_root_edit.setText(
                self.settings.value(
                    "upscale/mod_ready_export_root",
                    getattr(defaults, "mod_ready_export_root", MOD_READY_EXPORT_ROOT),
                )
            )
            self.mod_ready_create_no_encrypt_checkbox.setChecked(
                self._read_bool(
                    "upscale/mod_ready_create_no_encrypt",
                    getattr(defaults, "mod_ready_create_no_encrypt_file", MOD_READY_CREATE_NO_ENCRYPT),
                )
            )
            self.mod_ready_package_title_edit.setText(
                str(
                    self.settings.value(
                        "upscale/mod_ready_package_title",
                        getattr(defaults, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE),
                    )
                )
            )
            self.mod_ready_package_version_edit.setText(
                str(
                    self.settings.value(
                        "upscale/mod_ready_package_version",
                        getattr(defaults, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION),
                    )
                )
            )
            self.mod_ready_package_author_edit.setText(
                str(
                    self.settings.value(
                        "upscale/mod_ready_package_author",
                        getattr(defaults, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR),
                    )
                )
            )
            self.mod_ready_package_description_edit.setText(
                str(
                    self.settings.value(
                        "upscale/mod_ready_package_description",
                        getattr(defaults, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION),
                    )
                )
            )
            self.mod_ready_package_nexus_url_edit.setText(
                str(
                    self.settings.value(
                        "upscale/mod_ready_package_nexus_url",
                        getattr(defaults, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL),
                    )
                )
            )
            if self._preference_bool("restore_last_active_tab", True):
                saved_main_tab = int(self.settings.value("ui/main_tab_index", 0))
            else:
                saved_main_tab = 0
            self.main_tabs.setCurrentIndex(max(0, min(saved_main_tab, self.main_tabs.count() - 1)))
            self.compare_sync_pan_checkbox.setChecked(self._read_bool("ui/compare_sync_pan", False))
            self._set_combo_by_value(
                self.compare_preview_size_combo,
                str(self.settings.value("ui/compare_preview_size_mode", "fit:1.25")),
            )
            self.setup_section.set_expanded(self._read_bool("sections/setup_expanded", False))
            self.paths_section.set_expanded(self._read_bool("sections/paths_expanded", False))
            self.settings_section.set_expanded(self._read_bool("sections/settings_expanded", False))
            self.dds_output_section.set_expanded(self._read_bool("sections/dds_output_expanded", False))
            self.filters_section.set_expanded(self._read_bool("sections/filters_expanded", False))
            self.chainner_section.set_expanded(self._read_bool("sections/chainner_expanded", False))
            self._apply_mod_ready_export_state()
            self._refresh_workflow_profile_ncnn_model_combo()
            self._refresh_workflow_profiles_tree()
            self._refresh_workflow_rules_tree()
            self._schedule_workflow_match_refresh()

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

        def _current_upscale_backend(self) -> str:
            return self._combo_value(self.upscale_backend_combo)

        def _sync_upscale_backend_stack_height(self) -> None:
            current_page = self.upscale_backend_stack.currentWidget()
            if current_page is None:
                self.upscale_backend_stack.setMinimumHeight(0)
                self.upscale_backend_stack.setMaximumHeight(16777215)
                return
            target_height = max(0, current_page.sizeHint().height())
            self.upscale_backend_stack.setMinimumHeight(target_height)
            self.upscale_backend_stack.setMaximumHeight(target_height)

        def _apply_upscale_backend_state(self) -> None:
            backend = self._current_upscale_backend()
            if backend == UPSCALE_BACKEND_CHAINNER:
                self.upscale_backend_stack.setCurrentIndex(1)
            elif backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
                self.upscale_backend_stack.setCurrentIndex(2)
            else:
                self.upscale_backend_stack.setCurrentIndex(0)

            chainner_enabled = backend == UPSCALE_BACKEND_CHAINNER
            self.chainner_exe_path_edit.setEnabled(chainner_enabled)
            self.chainner_chain_path_edit.setEnabled(chainner_enabled)
            self.chainner_override_edit.setEnabled(chainner_enabled)
            self.chainner_exe_browse_button.setEnabled(chainner_enabled)
            self.chainner_chain_browse_button.setEnabled(chainner_enabled)
            self.validate_chainner_button.setEnabled(chainner_enabled)

            ncnn_enabled = backend == UPSCALE_BACKEND_REALESRGAN_NCNN
            self.ncnn_exe_path_edit.setEnabled(ncnn_enabled)
            self.ncnn_model_dir_edit.setEnabled(ncnn_enabled)
            self.ncnn_exe_browse_button.setEnabled(ncnn_enabled)
            self.ncnn_model_dir_browse_button.setEnabled(ncnn_enabled)
            self.ncnn_model_combo.setEnabled(ncnn_enabled and self.ncnn_model_combo.count() > 0 and bool(self._combo_value(self.ncnn_model_combo)))
            self.ncnn_model_refresh_button.setEnabled(ncnn_enabled)
            self.ncnn_extra_args_edit.setEnabled(ncnn_enabled)
            direct_backend_enabled = backend == UPSCALE_BACKEND_REALESRGAN_NCNN
            self.texture_policy_group.setVisible(True)
            self.direct_backend_controls_group.setVisible(direct_backend_enabled)
            self.ncnn_scale_spin.setEnabled(direct_backend_enabled)
            self.ncnn_tile_size_spin.setEnabled(direct_backend_enabled)
            self.upscale_post_correction_combo.setEnabled(direct_backend_enabled)
            self.upscale_texture_preset_combo.setEnabled(True)
            self.enable_automatic_texture_rules_checkbox.setEnabled(True)
            self.retry_smaller_tile_checkbox.setEnabled(direct_backend_enabled)
            self.enable_mod_ready_loose_export_checkbox.setEnabled(True)
            self.mod_ready_export_root_edit.setEnabled(self.enable_mod_ready_loose_export_checkbox.isChecked())
            self.mod_ready_export_browse_button.setEnabled(self.enable_mod_ready_loose_export_checkbox.isChecked())
            self.mod_ready_package_group.setVisible(self.enable_mod_ready_loose_export_checkbox.isChecked())
            self._refresh_workflow_profile_ncnn_model_combo()
            self._sync_workflow_editor_state()
            self._update_ncnn_preset_hint()
            self._refresh_dds_output_hints()
            self._sync_upscale_backend_stack_height()

        def _refresh_chainner_chain_info(self) -> None:
            if self._shutting_down:
                return
            _analysis, text = self._resolve_chainner_analysis()
            self.chainner_chain_info_view.setPlainText(text)

        def _schedule_chainner_chain_info_refresh(self, *_args) -> None:
            if self._shutting_down or not self._settings_ready:
                return
            self._chainner_analysis_timer.start()

        def _apply_dds_staging_enabled_state(self) -> None:
            enabled = self.enable_dds_staging_checkbox.isChecked()
            self.dds_staging_root_edit.setEnabled(enabled)
            self.dds_staging_browse_button.setEnabled(enabled)
            self._apply_upscale_backend_state()

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
            self._refresh_dds_output_hints()

        def _refresh_dds_output_hints(self) -> None:
            backend = self._current_upscale_backend()
            staging_enabled = self.enable_dds_staging_checkbox.isChecked()
            staging_root_text = self.dds_staging_root_edit.text().strip() or "(staging PNG root)"
            png_root_text = self.png_root_edit.text().strip() or "(PNG root)"
            output_root_text = self.output_root_edit.text().strip() or "(output root)"

            if staging_enabled:
                if backend == UPSCALE_BACKEND_CHAINNER:
                    self.dds_output_mode_hint.setText(
                        "DDS files are converted to source PNGs first. PNG-input chaiNNer chains should read the staging PNG root. DDS-direct chains can ignore the staged PNGs if the chain already reads DDS."
                    )
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"Source PNG folder: {staging_root_text}",
                                f"Final PNG folder after chaiNNer: {png_root_text}",
                                f"Rebuilt DDS folder: {output_root_text}",
                            ]
                        )
                    )
                elif backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
                    self.dds_output_mode_hint.setText(
                        "DDS files are converted to source PNGs first. Real-ESRGAN NCNN reads the staged PNGs and writes the final upscaled PNGs into PNG root."
                    )
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"Source PNG folder: {staging_root_text}",
                                f"Final upscaled PNG folder: {png_root_text}",
                                f"Rebuilt DDS folder: {output_root_text}",
                            ]
                        )
                    )
                else:
                    self.dds_output_mode_hint.setText(
                        "DDS files are converted to PNG first. With no backend selected, Start stops after PNG conversion and does not rebuild DDS."
                    )
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"Converted PNG folder: {png_root_text}",
                                "No DDS rebuild happens in this mode.",
                            ]
                        )
                    )
            else:
                if backend == UPSCALE_BACKEND_CHAINNER:
                    self.dds_output_mode_hint.setText(
                        "chaiNNer is enabled without DDS staging. PNG-input chains must read from the existing PNG root or another path defined by the chain. DDS-direct chains can still read DDS directly if the chain supports it."
                    )
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"PNG input folder for PNG-input chains: {png_root_text}",
                                f"Final PNG folder after chaiNNer: {png_root_text}",
                                f"Rebuilt DDS folder: {output_root_text}",
                            ]
                        )
                    )
                elif backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
                    self.dds_output_mode_hint.setText(
                        "Real-ESRGAN NCNN is enabled without DDS staging, so it upscales the existing PNG root before DDS rebuild."
                    )
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"Source and final PNG folder: {png_root_text}",
                                f"Rebuilt DDS folder: {output_root_text}",
                            ]
                        )
                    )
                else:
                    self.dds_output_mode_hint.setText("DDS rebuild uses the existing PNG root directly.")
                    self.dds_output_flow_hint.setText(
                        "\n".join(
                            [
                                f"Existing PNG folder: {png_root_text}",
                                f"Rebuilt DDS folder: {output_root_text}",
                            ]
                        )
                    )

            size_mode = self._combo_value(self.dds_size_mode_combo)
            if size_mode == DDS_SIZE_MODE_PNG:
                self.dds_output_size_hint.setText(
                    "Size mode: the rebuilt DDS uses the final PNG dimensions from PNG root. This changes DDS size only. It does not decide where PNG files are written."
                )
            elif size_mode == DDS_SIZE_MODE_ORIGINAL:
                self.dds_output_size_hint.setText(
                    "Size mode: the rebuilt DDS keeps the original DDS width and height, even if the PNG files in PNG root are larger or smaller."
                )
            else:
                self.dds_output_size_hint.setText(
                    "Size mode: the rebuilt DDS uses the custom width and height below. This does not change where PNG files are written."
                )

        def _update_ncnn_preset_hint(self) -> None:
            preset_definition = get_texture_preset_definition(self._combo_value(self.upscale_texture_preset_combo))
            upscale_list = ", ".join(preset_definition.upscale_types)
            copy_list = ", ".join(preset_definition.copy_types) if preset_definition.copy_types else "nothing"
            policy_lines = [
                preset_definition.description,
                f"Upscaled: {upscale_list}.",
                f"Copied unchanged: {copy_list}.",
                "This policy applies before DDS rebuild for every backend. Files kept out of the PNG path are copied through as original DDS when the current rules say they are safer untouched.",
                "Automatic rules still control final color space, compression, alpha-aware hints, and technical-map preservation after that policy is applied.",
            ]
            if self.enable_unsafe_technical_override_checkbox.isChecked():
                policy_lines.append(
                    "Expert override is enabled: technical textures can be forced through the generic visible-color PNG/upscale path even when the planner would normally preserve them."
                )
            if preset_definition.warning:
                policy_lines.append(preset_definition.warning)
            self.texture_policy_hint_label.setText(" ".join(policy_lines))

            backend = self._current_upscale_backend()
            if backend == UPSCALE_BACKEND_CHAINNER:
                direct_text = (
                    "chaiNNer uses its own chain settings for the actual upscale step. "
                    "The Texture Policy above still decides which files are allowed into the PNG/upscale path and which ones stay original."
                )
            elif backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
                direct_text = (
                    "These controls only affect the direct Real-ESRGAN NCNN PNG upscale pass. "
                    "Scale should stay close to the selected model's intended native scale, smaller tile sizes trade speed for lower VRAM use, "
                    "and post correction can automatically decide per texture how aggressively to pull safe outputs back toward the source before DDS rebuild."
                )
            else:
                direct_text = (
                    "Direct upscale controls are only used when Real-ESRGAN NCNN is selected. "
                    "With no backend selected, the Texture Policy still affects how existing PNG or preserve-original paths are handled."
                )
            self.direct_backend_hint_label.setText(direct_text)

        def open_run_summary(self) -> None:
            dialog = SafeUpscaleWizard(theme_key=self.current_theme_key, parent=self)
            config = self.collect_config()
            dialog.populate_from_config(
                {
                    "upscale_backend": config.upscale_backend,
                    "preset": config.upscale_texture_preset,
                    "scale": config.ncnn_scale,
                    "tile_size": config.ncnn_tile_size,
                    "ncnn_extra_args": config.ncnn_extra_args,
                    "post_correction_mode": config.upscale_post_correction_mode,
                    "use_automatic_rules": config.enable_automatic_texture_rules,
                    "unsafe_technical_override": config.enable_unsafe_technical_override,
                    "retry_smaller_tile": config.retry_smaller_tile_on_failure,
                    "loose_export": config.enable_mod_ready_loose_export,
                    "source_root": config.archive_package_root or config.original_dds_root,
                    "archive_root": config.archive_package_root,
                    "original_dds_root": config.original_dds_root,
                    "png_root": config.png_root,
                    "output_root": config.output_root,
                    "staging_png_root": config.dds_staging_root,
                    "notes": "This dialog is read-only. Model paths and all editable backend or texture-policy controls remain in the main Texture Workflow panel.",
                }
            )
            dialog.exec()

        def _open_external_urls(self, urls: Sequence[str], *, label: str) -> None:
            unique_urls: List[str] = []
            seen: set[str] = set()
            for raw_url in urls:
                url = str(raw_url or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                unique_urls.append(url)

            if not unique_urls:
                self.set_status_message(f"No external URL is available for {label}.", error=True)
                return

            opened = 0
            for url in unique_urls:
                if QDesktopServices.openUrl(QUrl(url)):
                    opened += 1
                    self.append_log(f"{label}: {url}")
                else:
                    self.append_log(f"Could not open external URL for {label}: {url}")

            if opened == len(unique_urls):
                noun = "URL" if opened == 1 else "URLs"
                self.set_status_message(f"Opened {opened} external {noun} for {label}.")
                return
            if opened > 0:
                self.set_status_message(f"Opened some external URLs for {label}. Check the log for details.", error=True)
                return
            self.set_status_message(f"Could not open any external URLs for {label}.", error=True)

        def _format_ncnn_catalog_details(self, entry) -> str:
            file_list = "\n".join(f"- {name}" for name in sorted(entry.model_files))
            download_urls = "\n".join(f"- {name}: {url}" for name, url in sorted(entry.model_files.items()))
            return (
                f"Model: {entry.model_name}\n"
                f"Native scale: {entry.native_scale}x\n"
                f"Category: {entry.usage_group}\n"
                f"Best for: {entry.content_type}\n"
                f"Short description: {entry.short_description}\n"
                f"Source: {entry.source_name}\n"
                f"Source page: {entry.source_page_url}\n\n"
                f"Required files:\n{file_list}\n\n"
                f"Model pages:\n{download_urls}\n\n"
                f"Texture guidance: treat these built-in NCNN recommendations as visible color/albedo/UI texture models. "
                f"Do not assume they are safe for normal maps, masks, height, displacement, or other technical DDS data."
            )

        def _format_local_ncnn_model_details(self, model_name: str, model_dir: Path) -> str:
            stem = model_name.strip()
            return (
                f"Detected local model: {stem}\n"
                f"Model folder: {model_dir}\n\n"
                f"Expected files:\n"
                f"- {stem}.param\n"
                f"- {stem}.bin\n\n"
                f"This model was found in the configured NCNN model folder, not in the built-in catalog.\n"
                f"Manual imports are fully supported, but the app does not know this model's intended content type, "
                f"preferred scale, or whether it is safe for normals, masks, or other technical textures."
            )

        def _open_ncnn_catalog_entry_urls(self, entry) -> None:
            self._open_external_urls(
                [url for _file_name, url in sorted(entry.model_files.items())],
                label=f"NCNN model '{entry.model_name}'",
            )

        def open_ncnn_model_catalog(self) -> None:
            dialog = QDialog(self)
            dialog.setWindowTitle("NCNN Model Catalog")
            dialog.resize(780, 540)

            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)

            intro = QLabel(
                "Browse NCNN model categories on the left, then expand a category to review its recommended models. "
                "Built-in entries include source links, non-downloading model pages, and purpose notes so users do not assume every model is interchangeable."
            )
            intro.setWordWrap(True)
            intro.setObjectName("HintLabel")
            layout.addWidget(intro)

            safety_hint = QLabel(
                "Technical DDS maps such as normals, packed masks, height, displacement, bump, and other precision-sensitive textures "
                "do not currently have built-in NCNN model recommendations here. Keep relying on Texture Policy to preserve those safely."
            )
            safety_hint.setWordWrap(True)
            safety_hint.setObjectName("HintLabel")
            layout.addWidget(safety_hint)

            sources_label = QLabel(
                "Popular sources: "
                + " | ".join(
                    f'<a href="{url}">{label}</a>' for label, url in NCNN_CATALOG_SOURCE_LINKS
                )
            )
            sources_label.setOpenExternalLinks(True)
            sources_label.setObjectName("HintLabel")
            sources_label.setWordWrap(True)
            layout.addWidget(sources_label)

            content_row = QHBoxLayout()
            content_row.setSpacing(10)
            layout.addLayout(content_row, 1)

            catalog_tree = QTreeWidget()
            catalog_tree.setHeaderHidden(True)
            catalog_tree.setRootIsDecorated(True)
            catalog_tree.setUniformRowHeights(True)
            catalog_tree.setIndentation(18)
            catalog_tree.setMinimumWidth(280)
            details_view = QPlainTextEdit()
            details_view.setReadOnly(True)
            details_view.setMinimumWidth(340)
            content_row.addWidget(catalog_tree, stretch=1)
            content_row.addWidget(details_view, stretch=2)

            curated_names = {entry.model_name for entry in NCNN_MODEL_CATALOG}
            grouped_catalog: Dict[str, list] = {}
            for entry in NCNN_MODEL_CATALOG:
                grouped_catalog.setdefault(entry.usage_group, []).append(entry)

            first_model_item: Optional[QTreeWidgetItem] = None
            for group_name, group_entries in grouped_catalog.items():
                group_item = QTreeWidgetItem([f"{group_name} ({len(group_entries)} models)"])
                group_item.setData(
                    0,
                    Qt.UserRole,
                    {"kind": "group", "group_name": group_name, "count": len(group_entries)},
                )
                group_item.setToolTip(0, f"Expand to view {len(group_entries)} recommended models.")
                group_font = group_item.font(0)
                group_font.setBold(True)
                group_item.setFont(0, group_font)
                catalog_tree.addTopLevelItem(group_item)
                for entry in group_entries:
                    item = QTreeWidgetItem(group_item, [f"{entry.model_name} ({entry.native_scale}x)"])
                    item.setData(0, Qt.UserRole, {"kind": "catalog", "model_name": entry.model_name})
                    item.setToolTip(0, f"{entry.content_type}: {entry.short_description}")
                    if first_model_item is None:
                        first_model_item = item

            exe_text = self.ncnn_exe_path_edit.text().strip()
            model_dir_text = self.ncnn_model_dir_edit.text().strip()
            exe_path = Path(exe_text).expanduser() if exe_text else None
            if exe_path is not None and not exe_path.exists():
                exe_path = None
            explicit_model_dir = Path(model_dir_text).expanduser() if model_dir_text else None
            if explicit_model_dir is not None and not explicit_model_dir.exists():
                explicit_model_dir = None
            detected_local_models = [
                (model_name, model_dir)
                for model_name, model_dir in discover_realesrgan_ncnn_models(exe_path, explicit_model_dir)
                if model_name not in curated_names
            ]
            if detected_local_models:
                local_group = QTreeWidgetItem([f"Detected local models ({len(detected_local_models)})"])
                local_group.setData(
                    0,
                    Qt.UserRole,
                    {
                        "kind": "group",
                        "group_name": "Detected local models",
                        "count": len(detected_local_models),
                    },
                )
                local_group.setToolTip(0, "Expand to view additional models found in your configured NCNN model folder.")
                local_group_font = local_group.font(0)
                local_group_font.setBold(True)
                local_group.setFont(0, local_group_font)
                catalog_tree.addTopLevelItem(local_group)
                for model_name, model_dir in detected_local_models:
                    item = QTreeWidgetItem(local_group, [f"{model_name} (Local)"])
                    item.setData(
                        0,
                        Qt.UserRole,
                        {"kind": "local", "model_name": model_name, "model_dir": str(model_dir)},
                    )
                    item.setToolTip(0, f"Detected from {model_dir}")
                    if first_model_item is None:
                        first_model_item = item

            button_row = QHBoxLayout()
            button_row.setSpacing(8)
            open_source_button = QPushButton("Open Source")
            use_selected_button = QPushButton("Use Selected")
            open_download_urls_button = QPushButton("Open Model Pages")
            close_button = QPushButton("Close")
            button_row.addWidget(open_source_button)
            button_row.addWidget(use_selected_button)
            button_row.addStretch(1)
            button_row.addWidget(open_download_urls_button)
            button_row.addWidget(close_button)
            layout.addLayout(button_row)

            def current_item_data() -> Optional[dict]:
                item = catalog_tree.currentItem()
                if item is None:
                    return None
                data = item.data(0, Qt.UserRole)
                return data if isinstance(data, dict) else None

            def current_entry():
                item_data = current_item_data()
                if not item_data or item_data.get("kind") != "catalog":
                    return None
                return get_ncnn_catalog_entry(str(item_data.get("model_name") or ""))

            def update_details() -> None:
                item_data = current_item_data()
                entry = current_entry()
                if item_data is None:
                    details_view.setPlainText(
                        "Expand a category on the left, then select a built-in or detected local NCNN model to review it."
                    )
                    open_source_button.setEnabled(False)
                    use_selected_button.setEnabled(False)
                    open_download_urls_button.setEnabled(False)
                    return
                if item_data.get("kind") == "group":
                    group_name = str(item_data.get("group_name") or "Category")
                    count = int(item_data.get("count") or 0)
                    details_view.setPlainText(
                        f"Category: {group_name}\n"
                        f"Models: {count}\n\n"
                        "Expand this category and select a model to review its purpose, source, and non-downloading model pages."
                    )
                    open_source_button.setEnabled(False)
                    use_selected_button.setEnabled(False)
                    open_download_urls_button.setEnabled(False)
                    return
                if item_data.get("kind") == "catalog" and entry is not None:
                    details_view.setPlainText(self._format_ncnn_catalog_details(entry))
                    open_source_button.setEnabled(True)
                    use_selected_button.setEnabled(True)
                    open_download_urls_button.setEnabled(True)
                    return
                model_name = str(item_data.get("model_name") or "")
                model_dir = Path(str(item_data.get("model_dir") or ""))
                details_view.setPlainText(self._format_local_ncnn_model_details(model_name, model_dir))
                open_source_button.setEnabled(False)
                use_selected_button.setEnabled(bool(model_name))
                open_download_urls_button.setEnabled(False)

            def open_source() -> None:
                entry = current_entry()
                if entry is None:
                    return
                QDesktopServices.openUrl(QUrl(entry.source_page_url))

            def use_selected() -> None:
                item_data = current_item_data()
                if item_data is None:
                    return
                model_name = str(item_data.get("model_name") or "")
                if not model_name:
                    return
                preferred_scale = 4
                entry = get_ncnn_catalog_entry(model_name)
                if entry is not None:
                    preferred_scale = entry.native_scale
                self._refresh_ncnn_model_picker(preferred_name=model_name)
                self.ncnn_scale_spin.setValue(
                    max(self.ncnn_scale_spin.minimum(), min(self.ncnn_scale_spin.maximum(), int(preferred_scale)))
                )
                dialog.accept()

            def open_download_urls() -> None:
                entry = current_entry()
                if entry is None:
                    return
                self._open_ncnn_catalog_entry_urls(entry)

            def handle_tree_item_activated(item: QTreeWidgetItem, _column: int) -> None:
                item_data = item.data(0, Qt.UserRole)
                if not isinstance(item_data, dict):
                    return
                if item_data.get("kind") == "group":
                    item.setExpanded(not item.isExpanded())
                    return
                use_selected()

            catalog_tree.currentItemChanged.connect(lambda *_args: update_details())
            catalog_tree.itemActivated.connect(handle_tree_item_activated)
            open_source_button.clicked.connect(open_source)
            use_selected_button.clicked.connect(use_selected)
            open_download_urls_button.clicked.connect(open_download_urls)
            close_button.clicked.connect(dialog.reject)

            if catalog_tree.topLevelItemCount() > 0:
                for index in range(catalog_tree.topLevelItemCount()):
                    group_item = catalog_tree.topLevelItem(index)
                    group_item.setExpanded(index == 0)
                if first_model_item is not None:
                    catalog_tree.setCurrentItem(first_model_item)
                else:
                    catalog_tree.setCurrentItem(catalog_tree.topLevelItem(0))
            else:
                update_details()

            dialog.exec()

        def _refresh_ncnn_model_picker(self, *_args, preferred_name: str = "") -> None:
            current_value = preferred_name or self._combo_value(self.ncnn_model_combo)
            exe_text = self.ncnn_exe_path_edit.text().strip()
            model_dir_text = self.ncnn_model_dir_edit.text().strip()

            exe_path = Path(exe_text).expanduser() if exe_text else None
            if exe_path is not None and not exe_path.exists():
                exe_path = None
            explicit_model_dir = Path(model_dir_text).expanduser() if model_dir_text else None
            if explicit_model_dir is not None and not explicit_model_dir.exists():
                explicit_model_dir = None

            resolved_model_dir = resolve_ncnn_model_dir(exe_path, explicit_model_dir)
            if not model_dir_text and resolved_model_dir is not None and resolved_model_dir.exists():
                self.ncnn_model_dir_edit.blockSignals(True)
                self.ncnn_model_dir_edit.setText(str(resolved_model_dir))
                self.ncnn_model_dir_edit.blockSignals(False)

            discovered_models = discover_realesrgan_ncnn_models(exe_path, resolved_model_dir)
            self.ncnn_model_combo.blockSignals(True)
            self.ncnn_model_combo.clear()
            for model_name, _model_dir in discovered_models:
                self._add_combo_choice(self.ncnn_model_combo, model_name, model_name)
            if not discovered_models:
                self._add_combo_choice(self.ncnn_model_combo, "No models detected", "")
            target_name = current_value or (discovered_models[0][0] if discovered_models else "")
            self._set_combo_by_value(self.ncnn_model_combo, target_name)
            self.ncnn_model_combo.blockSignals(False)
            self._apply_upscale_backend_state()

        def _apply_mod_ready_export_state(self) -> None:
            enabled = self.enable_mod_ready_loose_export_checkbox.isChecked()
            self.mod_ready_export_root_edit.setEnabled(enabled)
            self.mod_ready_export_browse_button.setEnabled(enabled)
            self.mod_ready_package_group.setVisible(enabled)
            self.mod_ready_create_no_encrypt_checkbox.setEnabled(enabled)
            self.mod_ready_package_title_edit.setEnabled(enabled)
            self.mod_ready_package_version_edit.setEnabled(enabled)
            self.mod_ready_package_author_edit.setEnabled(enabled)
            self.mod_ready_package_description_edit.setEnabled(enabled)
            self.mod_ready_package_nexus_url_edit.setEnabled(enabled)
            if enabled and not self.mod_ready_export_root_edit.text().strip():
                output_text = self.output_root_edit.text().strip()
                if output_text:
                    default_root = resolve_default_mod_ready_export_root(Path(output_text).expanduser())
                    self.mod_ready_export_root_edit.setText(str(default_root))
            if enabled and not self.mod_ready_package_title_edit.text().strip():
                self.mod_ready_package_title_edit.setText(MOD_READY_PACKAGE_TITLE)
            if enabled and not self.mod_ready_package_version_edit.text().strip():
                self.mod_ready_package_version_edit.setText(MOD_READY_PACKAGE_VERSION)
            self._save_settings()

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

        def _browse_texture_editor_png_root(self) -> None:
            self._browse_directory(self.texture_editor_png_root_edit, "Select Texture Editor PNG Root")

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

        def _browse_ncnn_exe_path(self) -> None:
            self._browse_file(
                self.ncnn_exe_path_edit,
                "Select Real-ESRGAN NCNN executable",
                "Executable (*.exe);;All files (*.*)",
            )

        def _browse_ncnn_model_dir(self) -> None:
            self._browse_directory(self.ncnn_model_dir_edit, "Select Real-ESRGAN NCNN model folder")

        def _browse_mod_ready_export_root(self) -> None:
            self._browse_directory(self.mod_ready_export_root_edit, "Select Ready Mod Package Parent Root")

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
            show_archive_progress: bool = False,
        ) -> None:
            if self._background_task_active():
                if self.worker_thread is not None:
                    self.set_status_message(
                        "Another background task is still running. Wait for it to finish before starting this action.",
                        error=True,
                    )
                return

            self.set_status_message(status_message)
            self.append_log(status_message)
            self._utility_updates_archive_progress = bool(show_archive_progress)
            if self._utility_updates_archive_progress:
                self.archive_scan_progress_label.setText(status_message)
                self.archive_scan_progress_bar.setRange(0, 0)
                self.archive_scan_progress_bar.setFormat("Working...")
                self.append_archive_log(status_message)

            worker = UtilityWorker(task)
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.log_message.connect(self._handle_utility_log_message)
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

        def _handle_utility_log_message(self, message: str) -> None:
            self.append_log(message)
            if not self._utility_updates_archive_progress:
                return
            self.archive_scan_progress_label.setText(message)
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Working...")
            self.append_archive_log(message)
            self.set_status_message(message)

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

            targets = self._workflow_start_cleanup_targets(
                config,
                include_output_root=include_output_root,
            )

            seen_paths: set[str] = set()
            unique_targets: List[Tuple[str, str, Path]] = []
            for key, label, path in targets:
                try:
                    normalized_key = str(path.resolve())
                except OSError:
                    normalized_key = str(path)
                if normalized_key in seen_paths:
                    continue
                seen_paths.add(normalized_key)
                unique_targets.append((key, label, path))

            cleared_target_keys: set[str] = set()
            for key, label, path in unique_targets:
                if not self._preference_bool("confirm_workflow_output_cleanup", True):
                    self.append_log(f"Keeping existing contents in {label}: {path} (cleanup confirmation disabled)")
                    continue
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
                cleared_target_keys.add(key)

            if "input_dds" in cleared_target_keys:
                self._apply_pending_archive_workflow_extract_if_needed(force=True)
            if "texture_editor_png_root" in cleared_target_keys:
                self._apply_pending_texture_editor_workflow_export_if_needed(force=True)

            return True

        def clear_workflow_roots(self) -> None:
            targets = self._manual_workflow_cleanup_targets()
            lines: List[str] = []
            configured_targets: List[Tuple[str, Path]] = []
            seen_paths: set[str] = set()
            for key, label, path in targets:
                if path is None:
                    lines.append(f"- {label}: not configured")
                    continue
                try:
                    resolved_path = path.resolve()
                except OSError:
                    resolved_path = path
                lines.append(f"- {label}: {resolved_path}")
                normalized_key = str(resolved_path)
                if normalized_key in seen_paths:
                    continue
                seen_paths.add(normalized_key)
                configured_targets.append((label, resolved_path))

            box = QMessageBox(self)
            box.setWindowTitle("Clear Workflow Roots")
            box.setIcon(QMessageBox.Warning)
            box.setText("This will clear the configured workflow staging/output folders listed below.")
            box.setInformativeText("\n".join(lines))
            clear_button = box.addButton("Clear Folders", QMessageBox.AcceptRole)
            cancel_button = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(cancel_button)
            box.exec()
            if box.clickedButton() != clear_button:
                self.set_status_message("Workflow root cleanup cancelled.")
                return

            cleared_count = 0
            for label, resolved_path in configured_targets:
                resolved_path.mkdir(parents=True, exist_ok=True)
                clear_directory_contents(resolved_path)
                self.append_log(f"Cleared {label}: {resolved_path}")
                cleared_count += 1

            self.set_status_message(f"Cleared {cleared_count} configured workflow folder(s).")

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
                if not self.texture_editor_png_root_edit.text().strip():
                    self.texture_editor_png_root_edit.setText(str(result["texture_editor_png_root"]))
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
                if not self.ncnn_exe_path_edit.text().strip():
                    self.ncnn_exe_path_edit.setText(str(result["ncnn_exe_path"]))
                if not self.ncnn_model_dir_edit.text().strip():
                    self.ncnn_model_dir_edit.setText(str(result["ncnn_model_dir"]))
                if not self.mod_ready_export_root_edit.text().strip():
                    self.mod_ready_export_root_edit.setText(str(result["mod_ready_export_root"]))
                self._refresh_ncnn_model_picker()
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

        def open_chainner_download_page(self) -> None:
            self._open_external_urls([CHAINNER_DOWNLOAD_PAGE_URL], label="chaiNNer")

        def open_texconv_download_page(self) -> None:
            self._open_external_urls([DIRECTXTEX_RELEASES_PAGE_URL], label="texconv")

        def open_realesrgan_ncnn_download_page(self) -> None:
            self._open_external_urls([REALESRGAN_NCNN_RELEASES_PAGE_URL], label="Real-ESRGAN NCNN")

        def _confirm_model_import_expectations(self, model_kind: str) -> bool:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Import NCNN Models")
            box.setText("Expected NCNN model contents")
            box.setInformativeText(
                "Choose a folder, zip, or file set that contains at least one matching "
                ".param + .bin pair with the same base name."
            )
            box.setDetailedText(
                "Example:\n"
                "  realesr-animevideov3.param\n"
                "  realesr-animevideov3.bin\n\n"
                "Nested folders inside a zip are fine.\n"
                "Unsupported examples include a single .param without its .bin partner,\n"
                "random checkpoint formats, or the NCNN executable folder without model files."
            )
            continue_button = box.addButton("Continue", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            return box.clickedButton() is continue_button

        def _choose_model_import_sources(self, title: str, *, model_kind: str) -> List[Path]:
            if not self._confirm_model_import_expectations(model_kind):
                return []
            mode, accepted = QInputDialog.getItem(
                self,
                title,
                "Import from:",
                ["Folder", "Files or zip"],
                0,
                False,
            )
            if not accepted or not mode:
                return []
            if mode == "Folder":
                selected = QFileDialog.getExistingDirectory(self, title, self._suggest_workspace_base_dir())
                return [Path(selected)] if selected else []
            selected_files, _ = QFileDialog.getOpenFileNames(
                self,
                title,
                self._suggest_workspace_base_dir(),
                "NCNN model files (*.param *.bin *.zip);;All files (*.*)",
            )
            return [Path(path) for path in selected_files]

        def _choose_model_destination(self, title: str, current_text: str) -> Optional[Path]:
            start_dir = self._pick_existing_directory(current_text) if current_text else self._suggest_workspace_base_dir()
            selected = QFileDialog.getExistingDirectory(self, title, start_dir)
            if not selected:
                return None
            return Path(selected)

        def import_ncnn_models(self) -> None:
            sources = self._choose_model_import_sources("Import NCNN Models", model_kind="ncnn")
            if not sources:
                return
            destination = self._choose_model_destination(
                "Select NCNN Model Folder",
                self.ncnn_model_dir_edit.text().strip(),
            )
            if destination is None:
                return

            def task(on_log: Callable[[str], None]) -> List[str]:
                pairs = validate_ncnn_model_import_sources(sources)
                on_log(f"Detected {len(pairs)} valid NCNN model pair(s): {', '.join(pairs[:5])}")
                imported = import_model_assets_to_directory(
                    sources,
                    destination,
                    allowed_suffixes=(".param", ".bin"),
                    on_log=on_log,
                )
                return [str(path) for path in imported]

            def on_complete(result: object) -> None:
                imported = result if isinstance(result, list) else []
                self.ncnn_model_dir_edit.setText(str(destination))
                self._refresh_ncnn_model_picker()
                self.set_status_message(f"Imported {len(imported)} NCNN model file(s).")

            self._run_utility_task(
                status_message="Importing NCNN models...",
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

        def scan_archives(self, force_refresh: bool = False, *, activate_archive_tab: bool = True) -> None:
            self._cancel_archive_tree_population()
            if self._background_task_active():
                return
            package_root_text = self.archive_package_root_edit.text().strip()
            if not package_root_text:
                self.set_status_message("Set an archive package root first.", error=True)
                return

            package_root = Path(package_root_text).expanduser()
            self._activate_archive_browser_on_scan_complete = activate_archive_tab
            if activate_archive_tab:
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
                build_tree_index=self._archive_tree_view_enabled(),
                filter_text=self.archive_filter_edit.text().strip(),
                exclude_filter_text=self.archive_exclude_filter_edit.text().strip(),
                extension_filter=self._combo_value(self.archive_extension_filter_combo),
                package_filter_text=self.archive_package_filter_edit.text().strip(),
                structure_filter=self._current_archive_structure_filter_value(),
                role_filter=self._combo_value(self.archive_role_filter_combo),
                exclude_common_technical_suffixes=self.archive_exclude_common_technical_checkbox.isChecked(),
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
            self._clear_archive_preview_cache()
            self.archive_entries = payload.get("entries", []) if isinstance(payload.get("entries"), list) else []
            self.archive_entries_by_normalized_path = (
                payload.get("path_index", {})
                if isinstance(payload.get("path_index"), dict)
                else {}
            )
            if not self.archive_entries_by_normalized_path and self.archive_entries:
                self.archive_entries_by_normalized_path = build_archive_entry_path_index(self.archive_entries)
            self.archive_entries_by_basename = (
                payload.get("basename_index", {})
                if isinstance(payload.get("basename_index"), dict)
                else {}
            )
            self.archive_entries_by_extension = (
                payload.get("extension_index", {})
                if isinstance(payload.get("extension_index"), dict)
                else {}
            )
            if not self.archive_entries_by_extension and self.archive_entries:
                self.archive_entries_by_extension = build_archive_entry_extension_index(self.archive_entries)
            package_root_text = self.archive_package_root_edit.text().strip()
            QTimer.singleShot(
                0,
                lambda entries=self.archive_entries, package_root_text=package_root_text: self.text_search_tab.set_archive_entries(
                    entries,
                    package_root_text,
                ),
            )
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
            self.archive_tree_index_ready = bool(browser_state.get("tree_index_ready", True))
            self._rebuild_archive_extension_filter_choices()
            self.archive_tree_items_by_folder_key = {}
            self.archive_filtered_dds_count = int(browser_state.get("dds_count", 0))
            self.archive_filters_dirty = False
            self._update_archive_filter_button_state()
            QTimer.singleShot(
                0,
                lambda entries=self.archive_entries, package_root_text=package_root_text: self.replace_assistant_tab.set_archive_entries(
                    entries,
                    package_root_text,
                ),
            )
            source = str(payload.get("source", "scan"))
            cache_path_text = str(payload.get("cache_path", "")).strip()
            rendering_archive_view = (
                self._activate_archive_browser_on_scan_complete
                or self.main_tabs.currentWidget() is self.archive_browser_tab
            )
            self.archive_scan_progress_label.setText(
                "Rendering archive browser view..." if rendering_archive_view else "Finalizing archive load..."
            )
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Rendering..." if rendering_archive_view else "Finalizing...")
            self.set_status_message("Rendering archive browser view..." if rendering_archive_view else "Finalizing archive load...")
            self.append_archive_log("Rendering archive browser view..." if rendering_archive_view else "Finalizing archive load...")
            self.archive_scan_finalize_pending = True
            QTimer.singleShot(
                0,
                lambda source=source, cache_path_text=cache_path_text: self._finalize_archive_scan_complete(
                    source,
                    cache_path_text,
                ),
            )

        def _finalize_archive_scan_complete(self, source: str, cache_path_text: str) -> None:
            try:
                self._refresh_or_defer_archive_browser_view(
                    activate_tab=self._activate_archive_browser_on_scan_complete,
                )
                self._activate_archive_browser_on_scan_complete = False
                self._refresh_or_defer_research_archive_picker()
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
            finally:
                self.archive_scan_finalize_pending = False
                if self.worker_thread is None:
                    self.set_busy(False, build_mode=False)

        def _current_archive_filter_signature(self) -> Tuple[object, ...]:
            return (
                self.archive_filter_edit.text().strip(),
                self.archive_exclude_filter_edit.text().strip(),
                normalize_archive_extension_filter(self._combo_value(self.archive_extension_filter_combo)),
                self.archive_package_filter_edit.text().strip(),
                self._current_archive_structure_filter_value(),
                self._combo_value(self.archive_role_filter_combo),
                bool(self.archive_exclude_common_technical_checkbox.isChecked()),
                int(self.archive_min_size_spin.value()),
                bool(self.archive_previewable_only_checkbox.isChecked()),
                bool(self._archive_tree_view_enabled()),
            )

        def _mark_archive_filters_dirty(self) -> None:
            self.archive_filters_dirty = True
            if self.archive_tree_population_active:
                self._cancel_archive_tree_population()
                pause_text = "Archive list update paused while filters are being edited. Press Apply Filters to refresh."
                self.archive_scan_progress_label.setText(pause_text)
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(0)
                self.archive_scan_progress_bar.setFormat("Paused")
                self.set_status_message(pause_text)
            if self.archive_filter_worker is not None:
                try:
                    self.archive_filter_worker.stop()
                except Exception:
                    pass
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

        def _archive_tree_view_enabled(self) -> bool:
            return self.archive_tree_view_checkbox.isChecked()

        def _handle_archive_tree_view_toggled(self, checked: bool) -> None:
            self._cancel_archive_tree_population()
            self.archive_tree.setRootIsDecorated(bool(checked))
            if self.worker_thread is not None:
                return
            current_entry = self._current_archive_entry()
            current_entry_path = current_entry.path if current_entry is not None else ""
            rebuild_tree_index = bool(checked and not self.archive_tree_index_ready and self.archive_filtered_entries)
            self._populate_archive_tree(current_entry_path, rebuild_index=rebuild_tree_index)

        def _update_archive_filter_button_state(self) -> None:
            button_label = "Apply Filters*" if self.archive_filters_dirty else "Apply Filters"
            self.archive_filter_apply_button.setText(button_label)
            can_apply = self.worker_thread is None and self.archive_filters_dirty
            self.archive_filter_apply_button.setEnabled(can_apply)
            self.archive_filter_clear_button.setEnabled(self.worker_thread is None)

        def _clear_archive_filters(self) -> None:
            self.archive_filter_edit.clear()
            self.archive_exclude_filter_edit.clear()
            self._set_combo_by_value(self.archive_extension_filter_combo, ARCHIVE_EXTENSION_FILTER)
            self.archive_package_filter_edit.clear()
            self.archive_structure_filter_pending_value = ARCHIVE_STRUCTURE_FILTER
            self._rebuild_archive_structure_filter_controls(ARCHIVE_STRUCTURE_FILTER)
            self._set_combo_by_value(self.archive_role_filter_combo, ARCHIVE_ROLE_FILTER)
            self.archive_exclude_common_technical_checkbox.setChecked(ARCHIVE_EXCLUDE_COMMON_TECHNICAL_SUFFIXES)
            self.archive_min_size_spin.setValue(ARCHIVE_MIN_SIZE_KB)
            self.archive_previewable_only_checkbox.setChecked(ARCHIVE_PREVIEWABLE_ONLY)
            self._save_settings()
            self._apply_archive_filter()

        def _apply_archive_filter(self) -> None:
            self._cancel_archive_tree_population()
            if self.worker_thread is not None:
                if self.archive_filter_worker is not None:
                    self.archive_filter_apply_pending = True
                    self.archive_filter_worker.stop()
                    self.archive_scan_progress_label.setText("Stopping previous archive filter...")
                    self.archive_scan_progress_bar.setRange(0, 0)
                    self.archive_scan_progress_bar.setFormat("Stopping...")
                    self.set_status_message("Stopping previous archive filter...")
                return
            if not self.archive_entries:
                self.archive_filtered_entries = []
                self.archive_filtered_dds_count = 0
                self.archive_filters_dirty = False
                self._update_archive_filter_button_state()
                self._populate_archive_tree("")
                self._refresh_or_defer_research_archive_picker()
                return
            current_entry = self._current_archive_entry()
            current_entry_path = current_entry.path if current_entry is not None else ""
            self._start_archive_filter_worker(current_entry_path)

        def _start_archive_filter_worker(self, preferred_path: str = "") -> None:
            filter_text = self.archive_filter_edit.text().strip()
            exclude_filter_text = self.archive_exclude_filter_edit.text().strip()
            extension_filter = self._combo_value(self.archive_extension_filter_combo)
            package_filter_text = self.archive_package_filter_edit.text().strip()
            structure_filter = self._current_archive_structure_filter_value()
            self.archive_structure_filter_pending_value = structure_filter
            role_filter = self._combo_value(self.archive_role_filter_combo)
            exclude_common_technical_suffixes = self.archive_exclude_common_technical_checkbox.isChecked()
            min_size_kb = self.archive_min_size_spin.value()
            previewable_only = self.archive_previewable_only_checkbox.isChecked()
            request_signature = self._current_archive_filter_signature()
            self.archive_filter_requested_signature = request_signature
            self.archive_filter_apply_pending = False
            self.archive_scan_progress_label.setText("Preparing archive filter...")
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Working...")
            self.set_status_message("Applying archive filters...")
            self.append_archive_log("Applying archive filters...")

            worker = ArchiveFilterWorker(
                self.archive_entries,
                entries_by_extension=self.archive_entries_by_extension,
                request_signature=request_signature,
                preferred_path=preferred_path,
                build_tree_index=self._archive_tree_view_enabled(),
                filter_text=filter_text,
                exclude_filter_text=exclude_filter_text,
                extension_filter=extension_filter,
                package_filter_text=package_filter_text,
                structure_filter=structure_filter,
                role_filter=role_filter,
                exclude_common_technical_suffixes=exclude_common_technical_suffixes,
                min_size_kb=min_size_kb,
                previewable_only=previewable_only,
            )
            thread = QThread(self)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.progress_changed.connect(self._handle_archive_scan_progress)
            worker.completed.connect(self._handle_archive_filter_complete)
            worker.error.connect(self._handle_worker_error)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(self._cleanup_worker_refs)

            self.archive_filter_worker = worker
            self.worker_thread = thread
            self.set_busy(True, build_mode=False)
            thread.start()

        def _handle_archive_filter_complete(self, result: object) -> None:
            payload = result if isinstance(result, dict) else {}
            browser_state = payload.get("browser_state") if isinstance(payload.get("browser_state"), dict) else {}
            request_signature = tuple(payload.get("request_signature") or ())
            preferred_path = str(payload.get("preferred_path", "") or "").strip()
            if request_signature and request_signature != self._current_archive_filter_signature():
                self.archive_filters_dirty = True
                self._update_archive_filter_button_state()
                stale_text = "Archive filter inputs changed while results were still loading. Press Apply Filters to refresh."
                self.archive_scan_progress_label.setText(stale_text)
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(0)
                self.archive_scan_progress_bar.setFormat("Stale")
                self.set_status_message(stale_text)
                return
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
            self.archive_tree_index_ready = bool(browser_state.get("tree_index_ready", True))
            self.archive_tree_items_by_folder_key = {}
            self.archive_filtered_dds_count = int(browser_state.get("dds_count", 0))
            self.archive_filters_dirty = False
            self._update_archive_filter_button_state()
            self.archive_scan_progress_label.setText("Rendering archive browser view...")
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Rendering...")
            self.set_status_message("Rendering archive browser view...")
            QTimer.singleShot(
                0,
                lambda preferred_path=preferred_path: self._finalize_archive_filter_complete(preferred_path),
            )

        def _finalize_archive_filter_complete(self, preferred_path: str) -> None:
            def finish_filter_render() -> None:
                self._refresh_or_defer_research_archive_picker()
                filtered_entries = len(self.archive_filtered_entries)
                completion_text = f"Applied archive filters. Showing {filtered_entries:,} entries."
                self.archive_scan_progress_label.setText(completion_text)
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(1)
                self.archive_scan_progress_bar.setFormat("Ready")
                self.set_status_message(completion_text)
                self.append_archive_log(completion_text)

            self._populate_archive_tree(preferred_path, rebuild_index=False, on_complete=finish_filter_render)

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
            self.archive_tree_index_ready = True

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
            *,
            show_full_path: bool = False,
        ) -> QTreeWidgetItem:
            item = QTreeWidgetItem(parent)
            self._populate_archive_file_item(item, entry_index, show_full_path=show_full_path)
            return item

        def _populate_archive_file_item(
            self,
            item: QTreeWidgetItem,
            entry_index: int,
            *,
            show_full_path: bool = False,
        ) -> None:
            entry = self.archive_filtered_entries[entry_index]
            normalized_parts = tuple(part for part in PurePosixPath(entry.path.replace("\\", "/")).parts if part)
            item.setText(0, entry.path if show_full_path else (normalized_parts[-1] if normalized_parts else entry.basename))
            item.setText(1, entry.extension or "-")
            item.setText(2, format_byte_size(entry.orig_size))
            item.setText(3, format_byte_size(entry.comp_size))
            item.setText(4, entry.compression_label)
            item.setText(5, entry.package_label)
            item.setData(0, Qt.UserRole, "file")
            item.setData(0, Qt.UserRole + 1, entry_index)
            item.setToolTip(0, entry.path)

        def _cancel_archive_tree_population(self) -> None:
            self.archive_tree_population_timer.stop()
            self.archive_tree_population_active = False
            self.archive_tree_population_next_index = 0
            self.archive_tree_population_preferred_path = ""
            self.archive_tree_population_target_item = None
            self.archive_tree_population_batch_size = self.archive_tree_population_default_batch_size
            self.archive_tree_population_continue_batch_size = self.archive_tree_population_default_batch_size
            self.archive_tree_population_last_progress_update = 0.0
            self._archive_tree_population_on_complete = None
            self.archive_tree.blockSignals(False)
            self.archive_tree.setEnabled(True)

        def _suggest_archive_tree_population_batch_sizes(self, total_entries: int) -> Tuple[int, int]:
            if total_entries >= 100_000:
                return 1800, 520
            if total_entries >= 50_000:
                return 1500, 460
            if total_entries >= 20_000:
                return 1200, 360
            if total_entries >= 5_000:
                return 900, 260
            base = max(self.archive_tree_population_default_batch_size, 500)
            return base, base

        def _build_archive_tree_population_chunk(
            self,
            start_index: int,
            max_items: int,
            *,
            preferred_path: str,
            target_item: Optional[QTreeWidgetItem],
        ) -> Tuple[List[QTreeWidgetItem], int, Optional[QTreeWidgetItem]]:
            total_entries = len(self.archive_filtered_entries)
            end_limit = min(total_entries, start_index + max(1, max_items))
            batch_items: List[QTreeWidgetItem] = []
            resolved_target = target_item
            budget_seconds = max(0.002, float(self.archive_tree_population_time_budget_ms) / 1000.0)
            started_at = time.perf_counter()

            for entry_index in range(start_index, end_limit):
                item = QTreeWidgetItem()
                self._populate_archive_file_item(item, entry_index, show_full_path=True)
                batch_items.append(item)
                if (
                    resolved_target is None
                    and preferred_path
                    and self.archive_filtered_entries[entry_index].path == preferred_path
                ):
                    resolved_target = item
                if batch_items and (time.perf_counter() - started_at) >= budget_seconds:
                    break

            return batch_items, start_index + len(batch_items), resolved_target

        def _continue_archive_tree_population(self) -> None:
            if not self.archive_tree_population_active:
                return

            total_entries = len(self.archive_filtered_entries)
            start_index = self.archive_tree_population_next_index
            batch_items, end_index, target_item = self._build_archive_tree_population_chunk(
                start_index,
                self.archive_tree_population_continue_batch_size,
                preferred_path=self.archive_tree_population_preferred_path,
                target_item=self.archive_tree_population_target_item,
            )

            if batch_items:
                self.archive_tree.addTopLevelItems(batch_items)
            self.archive_tree_population_target_item = target_item
            self.archive_tree_population_next_index = end_index

            now = time.perf_counter()
            if end_index >= total_entries or (now - self.archive_tree_population_last_progress_update) >= 0.12:
                progress_text = (
                    f"Rendering archive browser view in batches to keep the UI responsive... "
                    f"{end_index:,} / {total_entries:,}"
                )
                self.archive_scan_progress_label.setText(progress_text)
                self.archive_scan_progress_bar.setRange(0, max(1, total_entries))
                self.archive_scan_progress_bar.setValue(end_index)
                self.archive_scan_progress_bar.setFormat("%v / %m")
                self.set_status_message(progress_text)
                self.archive_tree_population_last_progress_update = now

            if end_index < total_entries:
                self.archive_tree_population_timer.start()
                return

            preferred_path = self.archive_tree_population_preferred_path
            target_item = self.archive_tree_population_target_item
            callback = self._archive_tree_population_on_complete

            self.archive_tree_population_active = False
            self.archive_tree_population_next_index = 0
            self.archive_tree_population_preferred_path = ""
            self.archive_tree_population_target_item = None
            self.archive_tree_population_batch_size = self.archive_tree_population_default_batch_size
            self.archive_tree_population_continue_batch_size = self.archive_tree_population_default_batch_size
            self.archive_tree_population_last_progress_update = 0.0
            self._archive_tree_population_on_complete = None
            self.archive_tree.blockSignals(False)
            self.archive_tree.setEnabled(True)

            self._finalize_archive_tree_render(preferred_path, target_item=target_item)
            if callback is not None:
                callback()

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

            if not self._archive_tree_view_enabled():
                return self._find_archive_file_item(self.archive_tree, entry_index)

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

        def _finalize_archive_tree_render(
            self,
            preferred_path: str = "",
            *,
            target_item: Optional[QTreeWidgetItem] = None,
        ) -> None:
            total_entries = len(self.archive_entries)
            filtered_entries = len(self.archive_filtered_entries)
            self.archive_stats_label.setText(
                f"{filtered_entries:,} shown / {total_entries:,} total entries. DDS in current view: {self.archive_filtered_dds_count:,}."
            )

            current_item = self.archive_tree.currentItem()
            if target_item is None and current_item is not None:
                target_item = current_item
            if target_item is None:
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

        def _populate_archive_tree(
            self,
            preferred_path: str = "",
            *,
            rebuild_index: bool = True,
            on_complete: Optional[Callable[[], None]] = None,
        ) -> None:
            self._cancel_archive_tree_population()
            if rebuild_index:
                self._rebuild_archive_tree_index()
            self.archive_tree.blockSignals(True)
            self.archive_tree.clear()
            self.archive_tree_items_by_folder_key = {}
            tree_view_enabled = self._archive_tree_view_enabled()
            self.archive_tree.setRootIsDecorated(tree_view_enabled)
            if tree_view_enabled:
                for _leaf, child_key in self.archive_tree_child_folders.get((), []):
                    self._create_archive_folder_item(self.archive_tree, child_key)
                for entry_index in self.archive_tree_direct_files.get((), []):
                    self._create_archive_file_item(self.archive_tree, entry_index)
            else:
                if len(self.archive_filtered_entries) > self.archive_tree_population_batch_size:
                    total_entries = len(self.archive_filtered_entries)
                    self.archive_tree_population_active = True
                    self.archive_tree_population_next_index = 0
                    self.archive_tree_population_preferred_path = preferred_path
                    self.archive_tree_population_target_item = None
                    (
                        self.archive_tree_population_batch_size,
                        self.archive_tree_population_continue_batch_size,
                    ) = self._suggest_archive_tree_population_batch_sizes(
                        total_entries
                    )
                    self.archive_tree_population_last_progress_update = time.perf_counter()
                    self._archive_tree_population_on_complete = on_complete
                    initial_items, initial_end_index, target_item = self._build_archive_tree_population_chunk(
                        0,
                        self.archive_tree_population_batch_size,
                        preferred_path=preferred_path,
                        target_item=None,
                    )
                    if initial_items:
                        self.archive_tree.addTopLevelItems(initial_items)
                    self.archive_tree_population_next_index = initial_end_index
                    self.archive_tree_population_target_item = target_item
                    self.archive_tree.blockSignals(False)
                    self.archive_tree.setEnabled(True)
                    self._finalize_archive_tree_render(preferred_path, target_item=target_item)
                    if initial_end_index >= total_entries:
                        self.archive_tree_population_active = False
                        self.archive_tree_population_next_index = 0
                        self.archive_tree_population_preferred_path = ""
                        self.archive_tree_population_target_item = None
                        self.archive_tree_population_batch_size = self.archive_tree_population_default_batch_size
                        self.archive_tree_population_continue_batch_size = self.archive_tree_population_default_batch_size
                        self._archive_tree_population_on_complete = None
                        if on_complete is not None:
                            on_complete()
                        return
                    progress_text = (
                        "Rendering remaining archive browser items in batches... "
                        f"{initial_end_index:,} / {total_entries:,}"
                    )
                    self.archive_scan_progress_label.setText(progress_text)
                    self.archive_scan_progress_bar.setRange(0, max(1, total_entries))
                    self.archive_scan_progress_bar.setValue(initial_end_index)
                    self.archive_scan_progress_bar.setFormat("%v / %m")
                    self.set_status_message(progress_text)
                    self.archive_tree_population_timer.start()
                    return
                for entry_index in range(len(self.archive_filtered_entries)):
                    self._create_archive_file_item(self.archive_tree, entry_index, show_full_path=True)
            self.archive_tree.blockSignals(False)
            self._finalize_archive_tree_render(preferred_path)
            if on_complete is not None:
                on_complete()

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

        def _archive_entries_for_workflow_extract(self) -> Tuple[List[ArchiveEntry], bool]:
            selected_entries = self._selected_archive_entries()
            if selected_entries:
                selected_dds = [entry for entry in selected_entries if entry.extension == ".dds"]
                return selected_dds, True
            filtered_dds = [entry for entry in self.archive_filtered_entries if entry.extension == ".dds"]
            return filtered_dds, False

        def _current_archive_entry(self) -> Optional[ArchiveEntry]:
            item = self.archive_tree.currentItem()
            if item is None:
                return None
            kind = self._archive_tree_item_kind(item)
            value = self._archive_tree_item_value(item)
            if kind == "file" and isinstance(value, int) and 0 <= value < len(self.archive_filtered_entries):
                return self.archive_filtered_entries[value]
            return None

        def _normalize_archive_entry_path(self, path: str) -> str:
            return path.replace("\\", "/").strip().lower()

        def _build_archive_entry_path_index(self, entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
            index: Dict[str, List[ArchiveEntry]] = {}
            for archive_entry in entries:
                normalized_path = self._normalize_archive_entry_path(archive_entry.path)
                index.setdefault(normalized_path, []).append(archive_entry)
            return index

        def _build_archive_entry_basename_index(self, entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
            index: Dict[str, List[ArchiveEntry]] = {}
            for archive_entry in entries:
                basename = Path(archive_entry.path).name.strip().lower()
                if not basename:
                    continue
                index.setdefault(basename, []).append(archive_entry)
            return index

        def _build_archive_entry_extension_index(self, entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
            index: Dict[str, List[ArchiveEntry]] = {}
            for archive_entry in entries:
                extension = normalize_archive_extension_filter(archive_entry.extension)
                if not extension:
                    continue
                index.setdefault(extension, []).append(archive_entry)
            return index

        def _find_archive_preview_companion_entry(self, entry: Optional[ArchiveEntry]) -> Optional[ArchiveEntry]:
            if entry is None or entry.extension not in {".pam", ".pamlod"}:
                return None
            normalized_path = self._normalize_archive_entry_path(entry.path)
            companion_paths: List[str] = []
            if entry.extension == ".pam" and normalized_path.endswith(".pam"):
                companion_paths.append(f"{normalized_path[:-4]}.pamlod")
                stem = normalized_path[:-4]
                if stem.endswith("_breakable"):
                    companion_paths.append(f"{stem[:-10]}.pamlod")
            elif entry.extension == ".pamlod" and normalized_path.endswith(".pamlod"):
                companion_paths.append(f"{normalized_path[:-7]}.pam")

            for companion_path in companion_paths:
                candidates = self.archive_entries_by_normalized_path.get(companion_path, [])
                if not candidates:
                    continue
                for candidate in candidates:
                    if candidate.pamt_path == entry.pamt_path:
                        return candidate
                return candidates[0]
            return None

        def current_archive_path_for_research(self) -> str:
            entry = self._current_archive_entry()
            return entry.path if entry is not None else ""

        def _build_texture_editor_binding_for_loose_path(
            self,
            source_path: Path,
            *,
            launch_origin: str,
            original_dds_path: Optional[Path] = None,
        ) -> TextureEditorSourceBinding:
            resolved = source_path.expanduser().resolve()
            relative_path = ""
            package_root = ""
            archive_relative_path = ""
            original_root_text = self.original_dds_edit.text().strip()
            png_root_text = self.png_root_edit.text().strip()
            texture_editor_png_root_text = self.texture_editor_png_root_edit.text().strip()
            original_root = Path(original_root_text).expanduser().resolve() if original_root_text else None
            png_root = Path(png_root_text).expanduser().resolve() if png_root_text else None
            texture_editor_png_root = (
                Path(texture_editor_png_root_text).expanduser().resolve()
                if texture_editor_png_root_text
                else None
            )

            for root in (original_root, png_root, texture_editor_png_root):
                if root is None:
                    continue
                try:
                    relative = resolved.relative_to(root)
                except Exception:
                    continue
                relative_path = PurePosixPath(relative.as_posix()).as_posix()
                parts = [part for part in PurePosixPath(relative_path).parts if part]
                if parts:
                    package_root = parts[0]
                    archive_relative_path = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else parts[0]
                break

            chosen_original = original_dds_path
            if chosen_original is None and resolved.suffix.lower() == ".dds":
                chosen_original = resolved
            if chosen_original is None and original_root is not None and relative_path:
                candidate = (original_root / Path(PurePosixPath(relative_path))).with_suffix(".dds")
                if candidate.exists():
                    chosen_original = candidate

            return TextureEditorSourceBinding(
                launch_origin=launch_origin,
                display_name=resolved.name,
                source_path=str(resolved),
                relative_path=relative_path,
                package_root=package_root,
                archive_relative_path=archive_relative_path,
                original_dds_path=str(chosen_original) if chosen_original is not None else "",
            )

        def _suggest_workflow_root_path(self, key: str) -> Optional[Path]:
            try:
                common = common_workspace_root_from_config(self.collect_config())
            except Exception:
                common = None
            if common is not None:
                suggested = suggested_workspace_paths(common).get(key)
                if suggested is not None:
                    return suggested

            sibling_name_by_key = {
                "original_dds_root": "input_dds",
                "texture_editor_png_root": "png_texture_editor",
            }
            sibling_name = sibling_name_by_key.get(key)
            if not sibling_name:
                return None
            known_root_names = {
                "input_dds",
                "png_upscaled",
                "png_texture_editor",
                "png_staged_input",
                "dds_final",
                "archive_extract",
            }
            for text in (
                self.original_dds_edit.text().strip(),
                self.png_root_edit.text().strip(),
                self.texture_editor_png_root_edit.text().strip(),
                self.dds_staging_root_edit.text().strip(),
                self.output_root_edit.text().strip(),
                self.archive_extract_root_edit.text().strip(),
            ):
                if not text:
                    continue
                candidate = Path(text).expanduser()
                base = candidate.parent if candidate.name.lower() in known_root_names else candidate
                return base / sibling_name
            return None

        def _ensure_workflow_root_path(
            self,
            edit: QLineEdit,
            *,
            key: str,
            label: str,
        ) -> Optional[Path]:
            existing_text = edit.text().strip()
            if existing_text:
                return Path(existing_text).expanduser()
            suggested = self._suggest_workflow_root_path(key)
            if suggested is None:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    f"{label} is not configured.\n\nInitialize a workspace or set the path manually before sending editor output to Texture Workflow.",
                )
                self.set_status_message(f"{label} is not configured.", error=True)
                return None
            suggested.mkdir(parents=True, exist_ok=True)
            edit.setText(str(suggested))
            self.append_log(f"Auto-configured {label}: {suggested}")
            self.set_status_message(f"Auto-configured {label}: {suggested}")
            return suggested

        def _open_source_in_texture_editor(self, source_path_text: str, binding: object) -> None:
            if not source_path_text:
                self.set_status_message("No source file was provided for Texture Editor.", error=True)
                return
            source_path = Path(source_path_text).expanduser()
            if not source_path.exists():
                self.set_status_message(f"Texture Editor source not found: {source_path}", error=True)
                return
            texture_binding = binding if isinstance(binding, TextureEditorSourceBinding) else None
            self.main_tabs.setCurrentWidget(self.texture_editor_tab)
            self.texture_editor_tab.open_source_path(source_path, binding=texture_binding)

        def _show_archive_browser_from_texture_editor(self, archive_relative_path: str = "") -> None:
            self.main_tabs.setCurrentWidget(self.archive_browser_tab)
            normalized_path = PurePosixPath(str(archive_relative_path or "").replace("\\", "/")).as_posix().strip()
            if not self.archive_entries:
                QMessageBox.information(
                    self,
                    "Archive Browser",
                    "Archive packages are not loaded yet. Open Archive Browser and scan or load the archive cache first.",
                )
                self.set_status_message("Archive Browser is open. Load or refresh archive packages to browse DDS files.")
                return
            if normalized_path:
                preferred_index = next(
                    (index for index, entry in enumerate(self.archive_filtered_entries) if entry.path == normalized_path),
                    -1,
                )
                if preferred_index >= 0:
                    target_item = self._select_archive_tree_entry(preferred_index)
                    if target_item is not None:
                        self.archive_tree.setCurrentItem(target_item)
                        target_item.setSelected(True)
                        self.archive_tree.scrollToItem(target_item, QAbstractItemView.PositionAtCenter)
                        self.set_status_message(f"Focused Archive Browser on {normalized_path}.")
                        return
                if any(entry.path == normalized_path for entry in self.archive_entries):
                    self.set_status_message(
                        "Archive Browser is open, but the current archive filters hide this file. Clear or adjust the filters to reveal it.",
                        error=True,
                    )
                else:
                    self.set_status_message(
                        f"Archive Browser is open. Could not find {normalized_path} in the loaded archive index.",
                        error=True,
                    )
            else:
                self.set_status_message("Archive Browser is open. Select a DDS file and use 'Open in Texture Editor'.")

        def _open_archive_current_in_texture_editor(self) -> None:
            entry = self._current_archive_entry()
            if entry is None:
                self.set_status_message("Select an archive file first.", error=True)
                return
            try:
                source_path, _note = ensure_archive_preview_source(entry)
            except Exception as exc:
                self.set_status_message(f"Could not open archive file in Texture Editor: {exc}", error=True)
                return
            package_root = entry.pamt_path.parent.name.strip() or "package"
            archive_relative_path = PurePosixPath(entry.path.replace("\\", "/")).as_posix()
            binding = TextureEditorSourceBinding(
                launch_origin="archive_browser",
                display_name=entry.basename,
                source_path=str(source_path),
                relative_path=str(Path(package_root) / Path(PurePosixPath(archive_relative_path))),
                package_root=package_root,
                archive_relative_path=archive_relative_path,
                original_dds_path=str(source_path) if source_path.suffix.lower() == ".dds" else "",
            )
            self._open_source_in_texture_editor(str(source_path), binding)

        def _resolve_archive_current_in_research(self) -> None:
            entry = self._current_archive_entry()
            if entry is None or entry.extension != ".dds":
                self.set_status_message("Select a single archive DDS file first.", error=True)
                return
            self.main_tabs.setCurrentWidget(self.research_tab)
            self.research_tab.focus_references_for_path(entry.path, auto_resolve=True)

        def _open_compare_in_texture_editor(self) -> None:
            relative_path = self.current_compare_path_for_research().strip()
            if not relative_path:
                self.set_status_message("Select a DDS file in Compare first.", error=True)
                return
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()
            relative = Path(PurePosixPath(relative_path))
            original_path = Path(original_root_text).expanduser() / relative if original_root_text else None
            output_path = Path(output_root_text).expanduser() / relative if output_root_text else None
            source_path = output_path if output_path is not None and output_path.exists() else original_path
            if source_path is None or not source_path.exists():
                self.set_status_message("Could not find a compare source file to open in Texture Editor.", error=True)
                return
            binding = self._build_texture_editor_binding_for_loose_path(
                source_path,
                launch_origin="compare",
                original_dds_path=original_path if original_path is not None and original_path.exists() else None,
            )
            self._open_source_in_texture_editor(str(source_path), binding)

        def _browse_texture_editor_source(self) -> None:
            initial_dir = self.png_root_edit.text().strip() or self.original_dds_edit.text().strip() or str(self.settings_file_path.parent)
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Open image or DDS in Texture Editor",
                initial_dir,
                "Supported files (*.png *.dds *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
            )
            if not file_path:
                return
            source_path = Path(file_path)
            binding = self._build_texture_editor_binding_for_loose_path(source_path, launch_origin="texture_workflow")
            self._open_source_in_texture_editor(str(source_path), binding)

        def _set_texture_editor_export_progress(self, detail: str) -> None:
            self.reset_progress()
            self.phase_value.setText("Texture Editor Export")
            self.phase_progress_value.setText(detail)
            self.current_file_value.setText(detail)
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Working...")

        def _set_replace_assistant_pending_status(self, detail: str) -> None:
            self.replace_assistant_tab.progress_bar.setRange(0, 0)
            self.replace_assistant_tab.progress_bar.setValue(0)
            self.replace_assistant_tab.progress_bar.setFormat("Working...")
            self.replace_assistant_tab.status_label.setText(detail)

        def _set_replace_assistant_ready_status(self, detail: str) -> None:
            self.replace_assistant_tab.progress_bar.setRange(0, 1)
            self.replace_assistant_tab.progress_bar.setValue(1)
            self.replace_assistant_tab.progress_bar.setFormat("Ready")
            self.replace_assistant_tab.status_label.setText(detail)

        def _normalize_texture_workflow_relative_path(self, raw_text: str) -> str:
            normalized = str(raw_text or "").strip().replace("\\", "/").strip()
            if not normalized:
                raise ValueError("Relative game path is required.")
            if normalized.startswith("/"):
                raise ValueError("Relative game path must not be absolute.")
            pure_path = PurePosixPath(normalized)
            if any(part in {"", ".", ".."} for part in pure_path.parts):
                raise ValueError("Relative game path must not contain '.' or '..' segments.")
            if pure_path.suffix.lower() != ".dds":
                pure_path = pure_path.with_suffix(".dds")
            return pure_path.as_posix()

        def _find_archive_entry_for_workflow_relative_path(self, relative_path_text: str) -> Optional[ArchiveEntry]:
            if not self.archive_entries:
                return None
            try:
                normalized_relative = self._normalize_texture_workflow_relative_path(relative_path_text)
            except ValueError:
                return None
            pure_path = PurePosixPath(normalized_relative)
            parts = [part for part in pure_path.parts if part]
            if not parts:
                return None
            package_root = parts[0] if len(parts) > 1 else ""
            archive_relative = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else pure_path.as_posix()
            normalized_archive_relative = archive_relative.replace("\\", "/").strip().casefold()
            normalized_package_root = package_root.strip().casefold()
            for entry in self.archive_entries:
                if entry.extension != ".dds":
                    continue
                if entry.path.replace("\\", "/").strip().casefold() != normalized_archive_relative:
                    continue
                if normalized_package_root and entry.pamt_path.parent.name.strip().casefold() != normalized_package_root:
                    continue
                return entry
            return None

        def _resolve_original_dds_from_archive_cache(self, relative_path_text: str) -> Optional[Path]:
            entry = self._find_archive_entry_for_workflow_relative_path(relative_path_text)
            if entry is None:
                return None
            try:
                try:
                    source_path, _note = ensure_archive_preview_source(entry)
                except Exception:
                    return None
                if source_path.exists() and source_path.is_file():
                    return source_path.expanduser().resolve()
                return None
            except Exception:
                return None

        def _prompt_texture_editor_workflow_target(
            self,
            source_path: Path,
            *,
            initial_relative_path: str,
            initial_original_dds_path: str,
        ) -> Optional[Tuple[str, Path]]:
            dialog = QDialog(self)
            dialog.setWindowTitle("Texture Workflow Target")
            dialog.setModal(True)
            dialog.resize(620, 180)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(10)

            info_label = QLabel(
                "This image was opened as a loose external file, so Texture Workflow needs an explicit game-relative target path and the original DDS source."
            )
            info_label.setWordWrap(True)
            layout.addWidget(info_label)

            form_layout = QGridLayout()
            form_layout.setHorizontalSpacing(8)
            form_layout.setVerticalSpacing(8)
            normalized_initial_relative = initial_relative_path.strip() or f"{source_path.stem}.dds"
            try:
                normalized_initial_relative = self._normalize_texture_workflow_relative_path(normalized_initial_relative)
            except ValueError:
                normalized_initial_relative = initial_relative_path.strip() or f"{source_path.stem}.dds"
            relative_edit = QLineEdit(normalized_initial_relative)
            original_edit = QLineEdit(initial_original_dds_path.strip())
            browse_original_button = QPushButton("Browse...")
            find_archive_button = QPushButton("Find In Archive")
            form_layout.addWidget(QLabel("Relative game path"), 0, 0)
            form_layout.addWidget(relative_edit, 0, 1, 1, 3)
            form_layout.addWidget(QLabel("Original DDS path"), 1, 0)
            form_layout.addWidget(original_edit, 1, 1)
            form_layout.addWidget(browse_original_button, 1, 2)
            form_layout.addWidget(find_archive_button, 1, 3)
            form_layout.setColumnStretch(1, 1)
            layout.addLayout(form_layout)
            match_hint_label = QLabel("")
            match_hint_label.setWordWrap(True)
            match_hint_label.setObjectName("HintLabel")
            layout.addWidget(match_hint_label)

            button_row = QHBoxLayout()
            button_row.setSpacing(8)
            button_row.addStretch(1)
            cancel_button = QPushButton("Cancel")
            continue_button = QPushButton("Send To Workflow")
            continue_button.setDefault(True)
            button_row.addWidget(cancel_button)
            button_row.addWidget(continue_button)
            layout.addLayout(button_row)

            result: List[object] = []

            def _browse_original() -> None:
                initial_dir = original_edit.text().strip() or self.original_dds_edit.text().strip() or str(source_path.parent)
                selected, _ = QFileDialog.getOpenFileName(
                    dialog,
                    "Select Original DDS",
                    initial_dir,
                    "DDS files (*.dds);;All files (*.*)",
                )
                if selected:
                    original_edit.setText(selected)

            def _try_fill_original_from_archive(*, show_feedback: bool) -> bool:
                resolved_original = self._resolve_original_dds_from_archive_cache(relative_edit.text())
                if resolved_original is None:
                    if show_feedback:
                        if not self.archive_entries:
                            match_hint_label.setText(
                                "Archive cache is not loaded. Load archives first if you want automatic DDS lookup."
                            )
                        else:
                            match_hint_label.setText(
                                "No exact DDS match was found in the loaded archive cache for the current relative path."
                            )
                    return False
                original_edit.setText(str(resolved_original))
                match_hint_label.setText(f"Matched original DDS from loaded archive cache: {resolved_original}")
                return True

            def _accept() -> None:
                try:
                    normalized_relative_path = self._normalize_texture_workflow_relative_path(relative_edit.text())
                except ValueError as exc:
                    QMessageBox.warning(dialog, "Texture Workflow Target", str(exc))
                    return
                original_text = original_edit.text().strip()
                if not original_text:
                    _try_fill_original_from_archive(show_feedback=False)
                    original_text = original_edit.text().strip()
                if not original_text:
                    QMessageBox.warning(dialog, "Texture Workflow Target", "Original DDS path is required.")
                    return
                original_path = Path(original_text).expanduser()
                if not original_path.exists() or not original_path.is_file():
                    QMessageBox.warning(dialog, "Texture Workflow Target", f"Original DDS file was not found:\n{original_path}")
                    return
                result[:] = [normalized_relative_path, original_path.resolve()]
                dialog.accept()

            browse_original_button.clicked.connect(_browse_original)
            find_archive_button.clicked.connect(lambda: _try_fill_original_from_archive(show_feedback=True))
            cancel_button.clicked.connect(dialog.reject)
            continue_button.clicked.connect(_accept)
            if not original_edit.text().strip():
                _try_fill_original_from_archive(show_feedback=bool(relative_edit.text().strip()))

            if dialog.exec() != QDialog.Accepted or len(result) != 2:
                return None
            return str(result[0]), Path(result[1])

        def _confirm_texture_editor_workflow_overwrite(self, destination: Path) -> bool:
            if not destination.exists():
                return True
            box = QMessageBox(self)
            box.setWindowTitle("Texture Editor PNG Root Already Contains This File")
            box.setIcon(QMessageBox.Question)
            box.setText("The matching PNG path already exists in the Texture Editor PNG root.")
            box.setInformativeText(
                f"{destination}\n\n"
                "Texture Workflow needs this exact relative path, so the export cannot be renamed here. "
                "Choose whether to overwrite the existing PNG or cancel."
            )
            overwrite_button = box.addButton("Overwrite Existing", QMessageBox.AcceptRole)
            cancel_button = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(overwrite_button)
            box.exec()
            return box.clickedButton() != cancel_button

        def _prompt_texture_editor_workflow_root_action(
            self,
            root_path: Path,
            *,
            root_label: str,
            staged_item_label: str,
            keep_existing_note: str = "",
        ) -> Optional[bool]:
            if not self._directory_has_contents(root_path):
                return False
            box = QMessageBox(self)
            box.setWindowTitle(f"{root_label} Already Contains Files")
            box.setIcon(QMessageBox.Question)
            box.setText(f"{root_label} already contains files or folders.")
            info_lines = [
                str(root_path),
                "",
                f"Choose whether to clear it before staging this {staged_item_label}.",
            ]
            note_text = str(keep_existing_note or "").strip()
            if note_text:
                info_lines.append(note_text)
            info_lines.append("Choose Keep Existing to leave the current contents in place, or Cancel to stop.")
            box.setInformativeText("\n".join(info_lines))
            clear_button = box.addButton("Clear Root", QMessageBox.DestructiveRole)
            keep_button = box.addButton("Keep Existing", QMessageBox.AcceptRole)
            cancel_button = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(keep_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked == cancel_button:
                return None
            return clicked == clear_button

        def _set_pending_texture_editor_workflow_export(
            self,
            *,
            source_png: Path,
            destination_png: Path,
            relative_path: str,
        ) -> None:
            self._pending_texture_editor_workflow_export = {
                "source_png": str(source_png.expanduser().resolve()),
                "destination_png": str(destination_png.expanduser().resolve()),
                "relative_path": relative_path,
            }

        def _has_pending_texture_editor_workflow_export_for_root(self, root_path: Path) -> bool:
            payload = self._pending_texture_editor_workflow_export
            if not isinstance(payload, dict):
                return False
            destination_text = str(payload.get("destination_png", "")).strip()
            if not destination_text:
                return False
            try:
                return Path(destination_text).expanduser().resolve().is_relative_to(root_path.expanduser().resolve())
            except Exception:
                return False

        def _apply_pending_texture_editor_workflow_export_if_needed(self, *, force: bool = False) -> bool:
            payload = self._pending_texture_editor_workflow_export
            if not isinstance(payload, dict):
                return False
            source_text = str(payload.get("source_png", "")).strip()
            destination_text = str(payload.get("destination_png", "")).strip()
            if not source_text or not destination_text:
                return False
            source_path = Path(source_text).expanduser()
            destination_path = Path(destination_text).expanduser()
            if not source_path.exists():
                return False
            if not force and destination_path.exists():
                return False
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            self.append_log(f"Restored pending Texture Editor PNG into workflow root: {destination_path}")
            return True

        def _set_pending_archive_workflow_extract(
            self,
            *,
            entries: Sequence[ArchiveEntry],
            output_root: Path,
        ) -> None:
            self._pending_archive_workflow_extract = {
                "entries": [entry for entry in entries if isinstance(entry, ArchiveEntry)],
                "output_root": str(output_root.expanduser().resolve()),
            }

        def _has_pending_archive_workflow_extract_for_root(self, root_path: Path) -> bool:
            payload = self._pending_archive_workflow_extract
            if not isinstance(payload, dict):
                return False
            output_root_text = str(payload.get("output_root", "")).strip()
            if not output_root_text:
                return False
            try:
                return Path(output_root_text).expanduser().resolve() == root_path.expanduser().resolve()
            except Exception:
                return False

        def _apply_pending_archive_workflow_extract_if_needed(self, *, force: bool = False) -> bool:
            payload = self._pending_archive_workflow_extract
            if not isinstance(payload, dict):
                return False
            output_root_text = str(payload.get("output_root", "")).strip()
            entries = payload.get("entries", [])
            if not output_root_text or not isinstance(entries, list) or not entries:
                return False
            output_root = Path(output_root_text).expanduser()
            if not force and output_root.exists() and directory_has_contents(output_root):
                return False
            output_root.mkdir(parents=True, exist_ok=True)
            self.append_log(f"Restoring pending archive DDS handoff into workflow source root: {output_root}")
            extract_archive_entries(entries, output_root, collision_mode="overwrite", on_log=self.append_log)
            return True

        def _workflow_start_cleanup_targets(
            self,
            config: AppConfig,
            *,
            include_output_root: bool,
        ) -> List[Tuple[str, str, Path]]:
            targets: List[Tuple[str, str, Path]] = []
            should_prompt_for_png_root = config.enable_dds_staging or config.upscale_backend == UPSCALE_BACKEND_CHAINNER
            if should_prompt_for_png_root:
                png_root_text = config.png_root.strip()
                if png_root_text:
                    targets.append(("png_root", "PNG root", Path(png_root_text).expanduser()))
            if include_output_root:
                output_root_text = config.output_root.strip()
                if output_root_text:
                    targets.append(("output_root", "Output root", Path(output_root_text).expanduser()))
            if getattr(config, "enable_mod_ready_loose_export", False):
                export_root_text = str(getattr(config, "mod_ready_export_root", "") or "").strip()
                if export_root_text:
                    package_info = ModPackageInfo(
                        title=str(getattr(config, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE) or "").strip() or MOD_READY_PACKAGE_TITLE,
                        version=str(getattr(config, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION) or "").strip() or MOD_READY_PACKAGE_VERSION,
                        author=str(getattr(config, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR) or "").strip(),
                        description=str(getattr(config, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION) or "").strip(),
                        nexus_url=str(getattr(config, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL) or "").strip(),
                    )
                    targets.append(
                        (
                            "mod_ready_output",
                            "Ready mod package output",
                            resolve_mod_package_root(Path(export_root_text).expanduser(), package_info),
                        )
                    )
            return targets

        def _manual_workflow_cleanup_targets(self) -> List[Tuple[str, str, Optional[Path]]]:
            targets: List[Tuple[str, str, Optional[Path]]] = []
            for key, label, text in (
                ("dds_final", "dds_final", self.output_root_edit.text().strip()),
                ("input_dds", "input_dds", self.original_dds_edit.text().strip()),
                ("png_staged_input", "png_staged_input", self.dds_staging_root_edit.text().strip()),
                ("png_upscaled", "png_upscaled", self.png_root_edit.text().strip()),
                ("png_texture_editor", "png_texture_editor", self.texture_editor_png_root_edit.text().strip()),
            ):
                targets.append((key, label, Path(text).expanduser() if text else None))
            return targets

        def _handle_texture_editor_send_to_replace_assistant(self, png_path_text: str, binding: object) -> None:
            source_path = Path(png_path_text).expanduser()
            if not source_path.exists():
                self.set_status_message(f"Texture Editor export not found: {source_path}", error=True)
                return
            del binding
            self.main_tabs.setCurrentWidget(self.replace_assistant_tab)
            self.replace_assistant_tab.import_external_sources(
                [source_path],
                select_path=source_path,
            )
            self.set_status_message(
                f"Texture Editor export imported into Replace Assistant: {source_path.name}"
            )

        def _handle_texture_editor_send_to_texture_workflow(self, png_path_text: str, binding: object) -> None:
            texture_editor_png_root = self._ensure_workflow_root_path(
                self.texture_editor_png_root_edit,
                key="texture_editor_png_root",
                label="Texture Editor PNG root",
            )
            if texture_editor_png_root is None:
                return
            source_path = Path(png_path_text).expanduser()
            if not source_path.exists():
                self.set_status_message(f"Texture Editor export not found: {source_path}", error=True)
                return
            texture_binding = binding if isinstance(binding, TextureEditorSourceBinding) else TextureEditorSourceBinding()
            original_root = self._ensure_workflow_root_path(
                self.original_dds_edit,
                key="original_dds_root",
                label="Original DDS root",
            )
            if original_root is None:
                return
            relative_path = texture_binding.relative_path.strip()
            original_dds_source_text = texture_binding.original_dds_path.strip()
            original_dds_source: Optional[Path] = None

            if relative_path and original_dds_source_text:
                original_dds_source = Path(original_dds_source_text).expanduser()
                if not original_dds_source.exists():
                    self.set_status_message(
                        f"Texture Editor original DDS source not found: {original_dds_source}",
                        error=True,
                    )
                    return
                relative_path = self._normalize_texture_workflow_relative_path(relative_path)
            else:
                target_result = self._prompt_texture_editor_workflow_target(
                    source_path,
                    initial_relative_path=relative_path,
                    initial_original_dds_path=original_dds_source_text,
                )
                if target_result is None:
                    self.set_status_message("Texture Editor export to Texture Workflow cancelled.")
                    return
                relative_path, original_dds_source = target_result

            resolved_destination = (
                texture_editor_png_root.expanduser()
                / Path(PurePosixPath(relative_path)).with_suffix(".png")
            )
            clear_texture_editor_root = self._prompt_texture_editor_workflow_root_action(
                texture_editor_png_root.expanduser(),
                root_label="Texture Editor PNG root",
                staged_item_label="Texture Editor export",
            )
            if clear_texture_editor_root is None:
                self.set_status_message("Texture Editor export to Texture Workflow cancelled.")
                return
            if not clear_texture_editor_root and not self._confirm_texture_editor_workflow_overwrite(resolved_destination):
                self.set_status_message("Texture Editor export to Texture Workflow cancelled.")
                return
            original_destination = original_root.expanduser() / Path(PurePosixPath(relative_path)).with_suffix(".dds")
            clear_original_root = self._prompt_texture_editor_workflow_root_action(
                original_root.expanduser(),
                root_label="Original DDS root",
                staged_item_label="matching original DDS",
                keep_existing_note=(
                    "Choose Clear Root if you want to remove stale DDS files before sending this texture to Texture Workflow."
                ),
            )
            if clear_original_root is None:
                self.set_status_message("Texture Editor export to Texture Workflow cancelled.")
                return
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(0)
            self._set_texture_editor_export_progress("Staging flattened PNG for Texture Workflow...")
            self.set_status_message("Staging Texture Editor export for Texture Workflow...")

            def task(on_log: Callable[[str], None]) -> Dict[str, str]:
                resolved_source = source_path.expanduser().resolve()
                if not resolved_source.exists():
                    raise FileNotFoundError(f"Texture Editor export not found: {resolved_source}")
                assert original_dds_source is not None
                resolved_original_dds = original_dds_source.expanduser().resolve()
                archive_entry = self._find_archive_entry_for_workflow_relative_path(relative_path)
                original_root_resolved = original_root.expanduser().resolve()
                original_bytes_from_source: Optional[bytes] = None
                if clear_original_root and resolved_original_dds.exists():
                    try:
                        if resolved_original_dds.is_relative_to(original_root_resolved):
                            original_bytes_from_source = resolved_original_dds.read_bytes()
                    except Exception:
                        original_bytes_from_source = None
                if not resolved_original_dds.exists() and archive_entry is None and original_bytes_from_source is None:
                    raise FileNotFoundError(f"Texture Editor original DDS source not found: {resolved_original_dds}")
                final_destination = resolved_destination.expanduser()
                if clear_texture_editor_root:
                    final_root = texture_editor_png_root.expanduser()
                    final_root.mkdir(parents=True, exist_ok=True)
                    on_log(f"Clearing Texture Editor PNG root before staging export: {final_root}")
                    clear_directory_contents(final_root)
                final_destination.parent.mkdir(parents=True, exist_ok=True)
                on_log(f"Copying Texture Editor export into Texture Editor PNG root: {resolved_source.name} -> {final_destination}")
                shutil.copy2(resolved_source, final_destination)
                final_original_destination = original_destination.expanduser()
                if clear_original_root:
                    final_original_root = original_root.expanduser()
                    final_original_root.mkdir(parents=True, exist_ok=True)
                    on_log(f"Clearing Original DDS root before staging source DDS: {final_original_root}")
                    clear_directory_contents(final_original_root)
                final_original_destination.parent.mkdir(parents=True, exist_ok=True)
                if final_original_destination.exists():
                    on_log(
                        f"Refreshing matching original DDS in workflow source root: {resolved_original_dds.name} -> {final_original_destination}"
                    )
                else:
                    on_log(
                        f"Staging matching original DDS into workflow source root: {resolved_original_dds.name} -> {final_original_destination}"
                    )
                if original_bytes_from_source is not None:
                    final_original_destination.write_bytes(original_bytes_from_source)
                elif resolved_original_dds.exists():
                    shutil.copy2(resolved_original_dds, final_original_destination)
                elif archive_entry is not None:
                    extract_archive_entry(archive_entry, final_original_destination)
                else:
                    raise FileNotFoundError(f"Texture Editor original DDS source not found: {resolved_original_dds}")
                return {
                    "destination": str(final_destination),
                    "source": str(resolved_source),
                    "original_destination": str(final_original_destination),
                    "original_source": str(resolved_original_dds),
                }

            def on_complete(result: object) -> None:
                payload = result if isinstance(result, dict) else {}
                destination_text = str(payload.get("destination", "")).strip()
                destination_path = Path(destination_text).expanduser() if destination_text else resolved_destination
                self._set_pending_texture_editor_workflow_export(
                    source_png=source_path.expanduser(),
                    destination_png=destination_path,
                    relative_path=relative_path,
                )
                self._pending_archive_workflow_extract = None
                self.filters_edit.setPlainText(relative_path)
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(1)
                self.progress_bar.setFormat("Ready")
                self.phase_value.setText("Idle")
                self.phase_progress_value.setText("Ready")
                self.current_file_value.setText("Idle")
                self.main_tabs.setCurrentWidget(self.workflow_tab)
                self.content_tabs.setCurrentIndex(0)
                self.set_status_message(
                    f"Texture Editor export staged for Workflow in Texture Editor PNG root and filter focused on {relative_path}.",
                    error=False,
                )

            self._run_utility_task(
                status_message="Staging Texture Editor export for Texture Workflow...",
                task=task,
                on_complete=on_complete,
            )

        def _show_compare_from_texture_editor(self, relative_path_text: str, binding: object) -> None:
            texture_binding = binding if isinstance(binding, TextureEditorSourceBinding) else TextureEditorSourceBinding()
            compare_path = str(relative_path_text or "").strip()
            if not compare_path:
                compare_path = (texture_binding.relative_path or texture_binding.archive_relative_path).strip()
            if not compare_path:
                self.set_status_message(
                    "Texture Editor could not determine a relative game path for Compare.",
                    error=True,
                )
                return
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentWidget(self.compare_tab)
            self.refresh_compare_list(select_current=True)
            target_item = None
            for row in range(self.compare_list.count()):
                item = self.compare_list.item(row)
                if item is not None and str(item.data(Qt.UserRole) or "").strip() == compare_path:
                    target_item = item
                    break
            if target_item is None:
                self.set_status_message(
                    "Compare is open, but the current compare roots do not contain this texture yet.",
                    error=True,
                )
                return
            self.compare_list.setCurrentItem(target_item)
            self.compare_list.scrollToItem(target_item, QAbstractItemView.PositionAtCenter)
            self.set_status_message(f"Focused Compare on {compare_path}.", error=False)

        def extract_related_archive_set_from_paths(self, raw_paths: object, description: str) -> None:
            if not isinstance(raw_paths, list):
                self.set_status_message("No related archive paths were supplied for extraction.", error=True)
                return
            lookup = {
                entry.path.replace("\\", "/").lower(): entry
                for entry in self.archive_entries
            }
            entries: List[ArchiveEntry] = []
            seen_paths: set[str] = set()
            for raw_path in raw_paths:
                if not isinstance(raw_path, str):
                    continue
                normalized = raw_path.strip().replace("\\", "/").lower()
                if not normalized or normalized in seen_paths:
                    continue
                entry = lookup.get(normalized)
                if entry is None:
                    continue
                seen_paths.add(normalized)
                entries.append(entry)
            if not entries:
                self.set_status_message("No matching archive entries were found for the related-set extraction.", error=True)
                return
            self._run_archive_extract(
                entries,
                allow_original_dds_root=False,
                description=description,
            )

        def _clear_archive_preview(self, message: str) -> None:
            self.archive_preview_request_id += 1
            self.archive_preview_cache_keys.clear()
            self.pending_archive_preview_request = None
            self.scheduled_archive_preview_request = None
            self.archive_preview_debounce_timer.stop()
            if self.archive_preview_worker is not None:
                self.archive_preview_worker.stop()
            self._stop_archive_preview_loading_indicator(success=None)
            self.current_archive_preview_result = None
            self.archive_preview_requested_loose = False
            self.archive_preview_showing_loose = False
            self.archive_preview_title_label.setText("Select an archive file")
            self.archive_preview_meta_label.setText(message)
            self._populate_archive_texture_reference_list(())
            self.archive_preview_warning_badge.clear()
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_warning_label.clear()
            self.archive_preview_warning_label.setVisible(False)
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_loose_toggle_button.setEnabled(False)
            self.archive_preview_label.clear_preview(message)
            self.archive_model_preview.clear_model(message)
            self.archive_media_preview.clear_media(message)
            self._update_archive_model_action_controls(None)
            self.archive_preview_text_edit.clear()
            self.archive_preview_info_edit.setPlainText(message)
            self.archive_preview_details_edit.clear()
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self._set_archive_preview_image_controls_enabled(False)

        def _start_archive_preview_loading_indicator(self, entry: Optional[ArchiveEntry]) -> None:
            self.archive_preview_loading_started_at = time.perf_counter()
            self.archive_preview_loading_entry_name = entry.basename if entry is not None else "selected file"
            self.archive_preview_loading_loose = bool(self.archive_preview_requested_loose)
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Loading preview...")
            self.set_status_message(
                f"Loading {'loose-file ' if self.archive_preview_loading_loose else ''}preview for {self.archive_preview_loading_entry_name}..."
            )
            self._update_archive_preview_loading_indicator()
            self.archive_preview_loading_timer.start()

        def _update_archive_preview_loading_indicator(self) -> None:
            if self.archive_preview_loading_started_at <= 0.0:
                return
            elapsed = max(0.0, time.perf_counter() - self.archive_preview_loading_started_at)
            prefix = "Loading loose-file preview" if self.archive_preview_loading_loose else "Loading preview"
            detail = f"{prefix} for {self.archive_preview_loading_entry_name}... {elapsed:.1f}s"
            self.archive_scan_progress_label.setText(detail)
            self.archive_scan_progress_bar.setRange(0, 0)
            self.archive_scan_progress_bar.setFormat("Loading preview...")
            self.archive_preview_meta_label.setText(f"{prefix}... {elapsed:.1f}s")
            loading_text = (
                f"{detail}\n\n"
                "Large .pam/.pamlod files and textured model previews can take a few seconds. "
                "The preview worker is still running."
            )
            if self.archive_preview_stack.currentWidget() is self.archive_preview_info_edit:
                self.archive_preview_info_edit.setPlainText(loading_text)
            self.archive_preview_details_edit.setPlainText(loading_text)

        def _stop_archive_preview_loading_indicator(self, *, success: Optional[bool]) -> None:
            elapsed = (
                max(0.0, time.perf_counter() - self.archive_preview_loading_started_at)
                if self.archive_preview_loading_started_at > 0.0
                else 0.0
            )
            entry_name = self.archive_preview_loading_entry_name
            self.archive_preview_loading_timer.stop()
            self.archive_preview_loading_started_at = 0.0
            self.archive_preview_loading_entry_name = ""
            self.archive_preview_loading_loose = False
            if success is None:
                return
            if success:
                label = f"Preview ready for {entry_name}."
                if elapsed >= 1.0:
                    label = f"{label} ({elapsed:.1f}s)"
                self.archive_scan_progress_label.setText(label)
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(1)
                self.archive_scan_progress_bar.setFormat("Ready")
            else:
                label = f"Preview failed for {entry_name}."
                if elapsed >= 1.0:
                    label = f"{label} ({elapsed:.1f}s)"
                self.archive_scan_progress_label.setText(label)
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(0)
                self.archive_scan_progress_bar.setFormat("Failed")

        def _show_archive_folder_preview(self, item: Optional[QTreeWidgetItem]) -> None:
            self.archive_preview_request_id += 1
            self.archive_preview_cache_keys.clear()
            self.pending_archive_preview_request = None
            self.scheduled_archive_preview_request = None
            self.archive_preview_debounce_timer.stop()
            if self.archive_preview_worker is not None:
                self.archive_preview_worker.stop()
            self._stop_archive_preview_loading_indicator(success=None)
            self.current_archive_preview_result = None
            self.archive_preview_requested_loose = False
            self.archive_preview_showing_loose = False
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
            self._populate_archive_texture_reference_list(())
            self.archive_preview_info_edit.setPlainText(preview_text)
            self.archive_preview_details_edit.setPlainText(preview_text)
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self.archive_preview_label.clear_preview("Select a file to preview it here.")
            self.archive_model_preview.clear_model("Select a file to preview it here.")
            self.archive_media_preview.clear_media("Select a file to preview it here.")
            self._update_archive_model_action_controls(None)
            self._set_archive_preview_image_controls_enabled(False)

        def _clear_archive_preview_cache(self) -> None:
            self.archive_preview_cache.clear()
            self.archive_preview_cache_keys.clear()

        def _archive_preview_cache_key(
            self,
            entry: Optional[ArchiveEntry],
            texconv_path: Optional[Path],
            loose_search_roots: Sequence[Path],
            *,
            include_loose_preview_assets: bool = False,
        ) -> str:
            if entry is None:
                return ""
            texconv_key = str(texconv_path).strip().lower() if texconv_path is not None else ""
            loose_roots_key = ""
            if include_loose_preview_assets:
                loose_roots_key = "|".join(str(path).strip().lower() for path in loose_search_roots)
            return "::".join(
                [
                    entry.path.strip().lower(),
                    str(entry.pamt_path).strip().lower(),
                    str(entry.paz_file).strip().lower(),
                    str(entry.offset),
                    str(entry.comp_size),
                    str(entry.orig_size),
                    texconv_key,
                    "loose" if include_loose_preview_assets else "archive",
                    loose_roots_key,
                ]
            )

        def _get_cached_archive_preview_result(self, cache_key: str) -> Optional[ArchivePreviewResult]:
            if not cache_key:
                return None
            cached = self.archive_preview_cache.get(cache_key)
            if cached is None:
                return None
            self.archive_preview_cache.move_to_end(cache_key)
            return cached

        def _store_cached_archive_preview_result(self, cache_key: str, result: ArchivePreviewResult) -> None:
            if not cache_key:
                return
            self.archive_preview_cache[cache_key] = result
            self.archive_preview_cache.move_to_end(cache_key)
            while len(self.archive_preview_cache) > self.archive_preview_cache_limit:
                self.archive_preview_cache.popitem(last=False)

        def _handle_archive_current_item_change(
            self,
            current: Optional[QTreeWidgetItem],
            previous: Optional[QTreeWidgetItem],
        ) -> None:
            del previous
            try:
                if self.archive_tree_population_active:
                    self.archive_tree_population_timer.start(90)
                if current is None:
                    self._clear_archive_preview("Select an archive file to preview it here.")
                    self._schedule_archive_selection_state_update()
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
                self._schedule_archive_selection_state_update()
            except Exception as exc:
                _write_crash_report(
                    "archive_selection_error",
                    "Archive Browser selection error",
                    str(exc),
                    context=_collect_crash_context(),
                )
                self._clear_archive_preview(f"Preview failed: {exc}")
                self.set_status_message(f"Archive preview failed: {exc}", error=True)

        def _schedule_archive_selection_state_update(self) -> None:
            self.archive_selection_state_timer.start()

        def _render_archive_preview(
            self,
            entry: Optional[ArchiveEntry],
            *,
            include_loose_preview_assets: bool = False,
            prefer_loose_preview: bool = False,
        ) -> None:
            request_id = self.archive_preview_request_id + 1
            self.archive_preview_request_id = request_id
            self.archive_preview_cache_keys = {
                existing_request_id: cache_key
                for existing_request_id, cache_key in self.archive_preview_cache_keys.items()
                if existing_request_id >= request_id
            }
            self.archive_preview_requested_loose = bool(entry is not None and prefer_loose_preview)
            self.archive_preview_title_label.setText(entry.basename if entry is not None else "Select an archive file")
            self.archive_preview_meta_label.setText(
                "Loading loose-file preview..." if self.archive_preview_requested_loose else "Loading preview..."
            )
            self._populate_archive_texture_reference_list(())
            self.archive_preview_warning_badge.clear()
            self.archive_preview_warning_badge.setVisible(False)
            self.archive_preview_warning_label.clear()
            self.archive_preview_warning_label.setVisible(False)
            self.archive_preview_loose_toggle_button.setVisible(False)
            self.archive_preview_loose_toggle_button.setEnabled(False)
            self.archive_preview_details_edit.setPlainText(
                "Preparing loose-file preview..." if self.archive_preview_requested_loose else "Preparing archive preview..."
            )
            self.archive_preview_info_edit.setPlainText(
                "Preparing loose-file preview..." if self.archive_preview_requested_loose else "Preparing archive preview..."
            )
            self.archive_preview_text_edit.clear()
            self.archive_preview_label.clear_preview(
                "Preparing loose-file preview..." if self.archive_preview_requested_loose else "Preparing archive preview..."
            )
            self.archive_model_preview.clear_model(
                "Preparing loose-file preview..." if self.archive_preview_requested_loose else "Preparing archive preview..."
            )
            self.archive_media_preview.clear_media(
                "Preparing loose-file preview..." if self.archive_preview_requested_loose else "Preparing archive preview..."
            )
            self._update_archive_model_action_controls(None)
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self._set_archive_preview_image_controls_enabled(False)
            self._start_archive_preview_loading_indicator(entry)
            self.pending_archive_preview_request = None
            self.scheduled_archive_preview_request = (request_id, entry, include_loose_preview_assets)
            self.archive_preview_debounce_timer.start()

        def _flush_scheduled_archive_preview_request(self) -> None:
            if self.scheduled_archive_preview_request is None:
                return
            request_id, entry, include_loose_preview_assets = self.scheduled_archive_preview_request
            self.scheduled_archive_preview_request = None

            texconv_text = self.texconv_path_edit.text().strip()
            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            loose_search_roots = self._collect_archive_preview_loose_roots()
            cache_key = self._archive_preview_cache_key(
                entry,
                texconv_path,
                loose_search_roots,
                include_loose_preview_assets=include_loose_preview_assets,
            )
            self.archive_preview_cache_keys[request_id] = cache_key

            cached_result = self._get_cached_archive_preview_result(cache_key)
            if cached_result is not None:
                self._handle_archive_preview_ready(request_id, cached_result)
                return

            if self.archive_preview_thread is not None:
                self.pending_archive_preview_request = (request_id, entry, include_loose_preview_assets)
                if self.archive_preview_worker is not None:
                    self.archive_preview_worker.stop()
                return

            self._start_archive_preview_worker(
                request_id,
                texconv_path,
                entry,
                loose_search_roots,
                include_loose_preview_assets=include_loose_preview_assets,
            )

        def _start_archive_preview_worker(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            entry: Optional[ArchiveEntry],
            loose_search_roots: Sequence[Path],
            *,
            include_loose_preview_assets: bool = False,
        ) -> None:
            companion_entry = self._find_archive_preview_companion_entry(entry)
            worker = ArchivePreviewWorker(
                request_id,
                texconv_path,
                entry,
                companion_entry,
                self.archive_entries_by_normalized_path,
                self.archive_entries_by_basename,
                loose_search_roots,
                include_loose_preview_assets=include_loose_preview_assets,
            )
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
            cache_key = self.archive_preview_cache_keys.pop(request_id, "")
            if self._shutting_down or request_id != self.archive_preview_request_id:
                return
            try:
                if isinstance(payload, ArchivePreviewResult):
                    self._stop_archive_preview_loading_indicator(success=True)
                    self._store_cached_archive_preview_result(cache_key, payload)
                    self._apply_archive_preview_result(payload)
            except Exception as exc:
                _write_crash_report(
                    "archive_preview_ready_error",
                    "Archive preview apply error",
                    str(exc),
                    context=_collect_crash_context(),
                )
                self._clear_archive_preview(f"Preview failed: {exc}")
                self.set_status_message(f"Archive preview failed: {exc}", error=True)

        def _handle_archive_preview_error(self, request_id: int, message: str) -> None:
            self.archive_preview_cache_keys.pop(request_id, None)
            if self._shutting_down or request_id != self.archive_preview_request_id:
                return
            self._stop_archive_preview_loading_indicator(success=False)
            _write_crash_report(
                "archive_preview_error",
                "Archive preview error",
                str(message),
                context=_collect_crash_context(),
            )
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
                    "Archive Preview" if self.archive_preview_showing_loose else "Loose File"
                )

        def _archive_model_preview_supports_textures(self, preview_model: Optional[object]) -> bool:
            if preview_model is None:
                return False
            meshes = getattr(preview_model, "meshes", None)
            if not meshes:
                return False
            for mesh in meshes:
                positions = list(getattr(mesh, "positions", []) or [])
                texture_coordinates = list(getattr(mesh, "texture_coordinates", []) or [])
                has_texture_reference = bool(
                    str(getattr(mesh, "preview_texture_path", "") or "").strip()
                    or getattr(mesh, "preview_texture_image", None) is not None
                )
                if positions and len(texture_coordinates) == len(positions) and has_texture_reference:
                    return True
            return False

        def _current_archive_mesh_entry(self) -> Optional[ArchiveEntry]:
            if self.archive_preview_showing_loose:
                return None
            current_entry = self._current_archive_entry()
            if current_entry is None or current_entry.extension not in ARCHIVE_MESH_EXTENSIONS:
                return None
            return current_entry

        def _update_archive_model_action_controls(self, preview_model: Optional[object]) -> None:
            mesh_entry = self._current_archive_mesh_entry()
            can_mesh_actions = mesh_entry is not None
            can_export_preview = preview_model is not None and not self.archive_preview_showing_loose
            supports_textures = can_export_preview and self._archive_model_preview_supports_textures(preview_model)
            self.archive_model_export_obj_button.setEnabled(can_mesh_actions or can_export_preview)
            self.archive_model_export_fbx_button.setEnabled(can_mesh_actions)
            self.archive_model_import_preview_button.setEnabled(can_mesh_actions)
            self.archive_model_import_patch_button.setEnabled(can_mesh_actions)
            self.archive_model_texture_toggle.setEnabled(supports_textures)
            self.archive_model_preview.set_use_textures(
                bool(self.archive_model_texture_toggle.isChecked() and supports_textures)
            )

        def _handle_archive_model_texture_toggle(self, checked: bool) -> None:
            preview_model = None
            if self.current_archive_preview_result is not None and not self.archive_preview_showing_loose:
                preview_model = self.current_archive_preview_result.preview_model
            supports_textures = self._archive_model_preview_supports_textures(preview_model)
            self.archive_model_preview.set_use_textures(bool(checked and supports_textures))

        def _archive_texture_reference_status_text(self, reference: ArchiveModelTextureReference) -> str:
            status = str(getattr(reference, "resolution_status", "") or "").strip().lower()
            resolved_entry = getattr(reference, "resolved_entry", None)
            if status == "resolved":
                if isinstance(resolved_entry, ArchiveEntry) and resolved_entry.extension == ".dds" and resolved_entry.compression_type == 1:
                    return "Resolved (Partial)"
                return "Resolved"
            if status == "technical_only":
                return "Technical only"
            return "Missing"

        def _populate_archive_texture_reference_list(
            self,
            references: Sequence[ArchiveModelTextureReference],
        ) -> None:
            self.current_archive_model_texture_references = list(references)
            self.archive_texture_refs_tree.clear()
            for index, reference in enumerate(self.current_archive_model_texture_references):
                resolved_archive_path = str(getattr(reference, "resolved_archive_path", "") or "").strip()
                resolved_package_label = str(getattr(reference, "resolved_package_label", "") or "").strip()
                item = QTreeWidgetItem(
                    [
                        str(getattr(reference, "reference_name", "") or "").strip() or "-",
                        self._archive_texture_reference_status_text(reference),
                        str(getattr(reference, "semantic_label", "") or "").strip() or "-",
                        resolved_archive_path or "-",
                        resolved_package_label or "-",
                        str(max(1, int(getattr(reference, "usage_count", 0) or 0))),
                    ]
                )
                material_name = str(getattr(reference, "material_name", "") or "").strip()
                if material_name:
                    item.setToolTip(0, f"Material: {material_name}")
                if resolved_archive_path:
                    item.setToolTip(3, resolved_archive_path)
                item.setData(0, Qt.UserRole, index)
                self.archive_texture_refs_tree.addTopLevelItem(item)
            self.archive_texture_refs_group.setVisible(bool(self.current_archive_model_texture_references))
            if self.current_archive_model_texture_references:
                if self.archive_preview_content_splitter.sizes()[0] <= 8:
                    self.archive_preview_content_splitter.setSizes([320, 960])
                self._layout_archive_texture_reference_columns()
                QTimer.singleShot(0, self._layout_archive_texture_reference_columns)
            else:
                self.archive_preview_content_splitter.setSizes([0, 1])
            self._update_archive_texture_reference_action_controls()

        def _layout_archive_texture_reference_columns(self, *_args) -> None:
            if not hasattr(self, "archive_texture_refs_tree"):
                return
            tree = self.archive_texture_refs_tree
            if tree.columnCount() < 6:
                return

            viewport_width = max(0, tree.viewport().width())
            available_width = max(viewport_width, 320)
            reference_width = 190
            status_width = 132
            semantic_width = 108
            package_width = 112
            uses_width = 56
            archive_width = max(
                240,
                available_width - (reference_width + status_width + semantic_width + package_width + uses_width + 12),
            )

            tree.setColumnWidth(0, reference_width)
            tree.setColumnWidth(1, status_width)
            tree.setColumnWidth(2, semantic_width)
            tree.setColumnWidth(3, archive_width)
            tree.setColumnWidth(4, package_width)
            tree.setColumnWidth(5, uses_width)

        def _current_archive_texture_reference(self) -> Optional[ArchiveModelTextureReference]:
            current_item = self.archive_texture_refs_tree.currentItem()
            if current_item is None:
                return None
            raw_index = current_item.data(0, Qt.UserRole)
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                return None
            if 0 <= index < len(self.current_archive_model_texture_references):
                return self.current_archive_model_texture_references[index]
            return None

        def _update_archive_texture_reference_action_controls(self) -> None:
            reference = self._current_archive_texture_reference()
            resolved_entry = getattr(reference, "resolved_entry", None) if reference is not None else None
            can_open = isinstance(resolved_entry, ArchiveEntry)
            can_export_or_replace = can_open and resolved_entry.extension == ".dds"
            self.archive_texture_open_button.setEnabled(can_open)
            self.archive_texture_export_button.setEnabled(can_export_or_replace)
            self.archive_texture_replace_dds_button.setEnabled(can_export_or_replace)
            self.archive_texture_replace_png_button.setEnabled(can_export_or_replace)

        def _open_selected_archive_texture_reference(self) -> None:
            reference = self._current_archive_texture_reference()
            resolved_entry = getattr(reference, "resolved_entry", None) if reference is not None else None
            if not isinstance(resolved_entry, ArchiveEntry):
                self.set_status_message("Select a resolved texture reference first.", error=True)
                return
            self._show_archive_browser_from_texture_editor(resolved_entry.path)

        def _export_selected_archive_texture_reference(self) -> None:
            reference = self._current_archive_texture_reference()
            resolved_entry = getattr(reference, "resolved_entry", None) if reference is not None else None
            if not isinstance(resolved_entry, ArchiveEntry) or resolved_entry.extension != ".dds":
                self.set_status_message("Select a resolved DDS texture reference first.", error=True)
                return

            default_dir = self.settings_file_path.parent / "archive_texture_export"
            default_target = default_dir / resolved_entry.basename
            output_path, _selected = QFileDialog.getSaveFileName(
                self,
                "Export Resolved DDS",
                str(default_target),
                "DDS (*.dds)",
            )
            if not output_path:
                return

            def _task(log: Callable[[str], None]) -> Path:
                log(f"Exporting resolved DDS for {resolved_entry.path}...")
                source_path, _note = ensure_archive_preview_source(resolved_entry)
                target_path = Path(output_path).expanduser()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                return target_path.resolve()

            def _handle_complete(result: object) -> None:
                if not isinstance(result, Path):
                    self.set_status_message("Texture export finished with an unexpected result payload.", error=True)
                    return
                QMessageBox.information(self, "Texture Export Complete", f"Exported DDS:\n{result}")
                self.set_status_message(f"Exported {resolved_entry.basename}.")

            self._run_utility_task(
                status_message=f"Exporting {resolved_entry.basename}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _choose_archive_write_target(
            self,
            *,
            title: str,
            message: str,
            allow_archive_patch: bool,
        ) -> str:
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Question)
            dialog.setWindowTitle(title)
            dialog.setText(message)
            dialog.setInformativeText(
                "Patch Game Archives updates the installed package files and recalculates checksums. "
                "Write Mod-Ready Loose File creates a loose mod package instead."
            )
            patch_button = None
            if allow_archive_patch:
                patch_button = dialog.addButton("Patch Game Archives", QMessageBox.AcceptRole)
            loose_button = dialog.addButton("Write Mod-Ready Loose File", QMessageBox.ActionRole)
            cancel_button = dialog.addButton(QMessageBox.Cancel)
            dialog.exec()
            clicked = dialog.clickedButton()
            if patch_button is not None and clicked is patch_button:
                return "patch"
            if clicked is loose_button:
                return "loose"
            if clicked is cancel_button:
                return ""
            return ""

        def _collect_archive_mod_ready_export_target(
            self,
            *,
            browse_title: str,
        ) -> Optional[Tuple[Path, ModPackageInfo, bool]]:
            config = self.collect_config()
            export_root_text = str(getattr(config, "mod_ready_export_root", "") or "").strip()
            if export_root_text:
                export_root = Path(export_root_text).expanduser()
            else:
                output_root_text = str(getattr(config, "output_root", "") or "").strip()
                default_root = (
                    resolve_default_mod_ready_export_root(Path(output_root_text).expanduser())
                    if output_root_text
                    else (self.settings_file_path.parent / MOD_READY_EXPORT_DIRNAME)
                )
                selected_dir = QFileDialog.getExistingDirectory(self, browse_title, str(default_root))
                if not selected_dir:
                    return None
                export_root = Path(selected_dir)
            package_info = ModPackageInfo(
                title=str(getattr(config, "mod_ready_package_title", MOD_READY_PACKAGE_TITLE) or "").strip() or MOD_READY_PACKAGE_TITLE,
                version=str(getattr(config, "mod_ready_package_version", MOD_READY_PACKAGE_VERSION) or "").strip() or MOD_READY_PACKAGE_VERSION,
                author=str(getattr(config, "mod_ready_package_author", MOD_READY_PACKAGE_AUTHOR) or "").strip(),
                description=str(getattr(config, "mod_ready_package_description", MOD_READY_PACKAGE_DESCRIPTION) or "").strip(),
                nexus_url=str(getattr(config, "mod_ready_package_nexus_url", MOD_READY_PACKAGE_NEXUS_URL) or "").strip(),
            )
            return (
                export_root,
                package_info,
                bool(getattr(config, "mod_ready_create_no_encrypt_file", True)),
            )

        def _replace_selected_archive_texture_reference_from_dds(self) -> None:
            reference = self._current_archive_texture_reference()
            resolved_entry = getattr(reference, "resolved_entry", None) if reference is not None else None
            if not isinstance(resolved_entry, ArchiveEntry) or resolved_entry.extension != ".dds":
                self.set_status_message("Select a resolved DDS texture reference first.", error=True)
                return

            source_path, _selected = QFileDialog.getOpenFileName(
                self,
                "Select Replacement DDS",
                str(self.settings_file_path.parent),
                "DDS (*.dds)",
            )
            if not source_path:
                return

            patch_blocker = get_archive_texture_patch_blocker(resolved_entry)
            destination = self._choose_archive_write_target(
                title="Replace Texture",
                message=f"Replace {resolved_entry.path} using {Path(source_path).name}?",
                allow_archive_patch=not bool(patch_blocker),
            )
            if not destination:
                return
            if destination == "patch" and patch_blocker:
                QMessageBox.warning(self, "Replace Texture", patch_blocker)
                return

            loose_export_settings: Optional[Tuple[Path, ModPackageInfo, bool]] = None
            if destination == "loose":
                loose_export_settings = self._collect_archive_mod_ready_export_target(
                    browse_title="Select Mod-Ready Export Parent Root"
                )
                if loose_export_settings is None:
                    return

            def _task(log: Callable[[str], None]) -> object:
                log(f"Preparing replacement DDS payload for {resolved_entry.path}...")
                payload = build_archive_texture_payload_from_dds(resolved_entry, Path(source_path))
                requests = [ArchivePatchRequest(entry=resolved_entry, payload_data=payload)]
                if destination == "patch":
                    log(f"Patching {resolved_entry.path} back into the game archives...")
                    return patch_archive_entries(requests, on_log=log)
                if loose_export_settings is None:
                    raise RuntimeError("Mod-ready export target is not available.")
                parent_root, package_info, create_no_encrypt = loose_export_settings
                log(f"Writing loose mod payload for {resolved_entry.path}...")
                return export_archive_payloads_to_mod_ready_loose(
                    requests,
                    parent_root=parent_root,
                    package_info=package_info,
                    create_no_encrypt_file=create_no_encrypt,
                    on_log=log,
                )

            def _handle_complete(result: object) -> None:
                if destination == "patch":
                    if not isinstance(result, ArchivePatchResult):
                        self.set_status_message("Texture replacement finished with an unexpected result payload.", error=True)
                        return
                    self._apply_archive_patch_result(result)
                    current_entry = self._current_archive_entry()
                    if current_entry is not None:
                        self._render_archive_preview(current_entry)
                    QMessageBox.information(
                        self,
                        "Texture Patch Complete",
                        f"Patched {resolved_entry.path}\n\nBackup: {result.backup_dir}",
                    )
                    self.set_status_message(f"Patched {resolved_entry.basename}.")
                    return
                if not isinstance(result, ArchiveLooseExportResult):
                    self.set_status_message("Loose texture export finished with an unexpected result payload.", error=True)
                    return
                QMessageBox.information(
                    self,
                    "Loose Texture Export Complete",
                    f"Wrote replacement DDS into:\n{result.package_root}",
                )
                self.set_status_message(f"Wrote loose replacement for {resolved_entry.basename}.")

            self._run_utility_task(
                status_message=f"Replacing {resolved_entry.basename}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _replace_selected_archive_texture_reference_from_png(self) -> None:
            reference = self._current_archive_texture_reference()
            resolved_entry = getattr(reference, "resolved_entry", None) if reference is not None else None
            if not isinstance(resolved_entry, ArchiveEntry) or resolved_entry.extension != ".dds":
                self.set_status_message("Select a resolved DDS texture reference first.", error=True)
                return

            source_path, _selected = QFileDialog.getOpenFileName(
                self,
                "Select Replacement PNG",
                str(self.settings_file_path.parent),
                "PNG (*.png)",
            )
            if not source_path:
                return

            texconv_text = self.texconv_path_edit.text().strip()
            if not texconv_text:
                self.set_status_message("Set texconv.exe before replacing an archive DDS from PNG.", error=True)
                return

            patch_blocker = get_archive_texture_patch_blocker(resolved_entry)
            destination = self._choose_archive_write_target(
                title="Replace Texture From PNG",
                message=f"Rebuild and replace {resolved_entry.path} using {Path(source_path).name}?",
                allow_archive_patch=not bool(patch_blocker),
            )
            if not destination:
                return
            if destination == "patch" and patch_blocker:
                QMessageBox.warning(self, "Replace Texture From PNG", patch_blocker)
                return

            loose_export_settings: Optional[Tuple[Path, ModPackageInfo, bool]] = None
            if destination == "loose":
                loose_export_settings = self._collect_archive_mod_ready_export_target(
                    browse_title="Select Mod-Ready Export Parent Root"
                )
                if loose_export_settings is None:
                    return

            def _task(log: Callable[[str], None]) -> object:
                log(f"Preparing PNG-based DDS rebuild for {resolved_entry.path}...")
                payload = build_archive_texture_payload_from_png(
                    resolved_entry,
                    Path(source_path),
                    texconv_path=Path(texconv_text),
                    on_log=log,
                )
                requests = [ArchivePatchRequest(entry=resolved_entry, payload_data=payload)]
                if destination == "patch":
                    log(f"Patching rebuilt DDS for {resolved_entry.path} back into the game archives...")
                    return patch_archive_entries(requests, on_log=log)
                if loose_export_settings is None:
                    raise RuntimeError("Mod-ready export target is not available.")
                parent_root, package_info, create_no_encrypt = loose_export_settings
                log(f"Writing rebuilt DDS for {resolved_entry.path} into a loose mod package...")
                return export_archive_payloads_to_mod_ready_loose(
                    requests,
                    parent_root=parent_root,
                    package_info=package_info,
                    create_no_encrypt_file=create_no_encrypt,
                    on_log=log,
                )

            def _handle_complete(result: object) -> None:
                if destination == "patch":
                    if not isinstance(result, ArchivePatchResult):
                        self.set_status_message("Texture rebuild finished with an unexpected result payload.", error=True)
                        return
                    self._apply_archive_patch_result(result)
                    current_entry = self._current_archive_entry()
                    if current_entry is not None:
                        self._render_archive_preview(current_entry)
                    QMessageBox.information(
                        self,
                        "Texture Patch Complete",
                        f"Patched {resolved_entry.path}\n\nBackup: {result.backup_dir}",
                    )
                    self.set_status_message(f"Patched {resolved_entry.basename} from PNG.")
                    return
                if not isinstance(result, ArchiveLooseExportResult):
                    self.set_status_message("Loose texture export finished with an unexpected result payload.", error=True)
                    return
                QMessageBox.information(
                    self,
                    "Loose Texture Export Complete",
                    f"Wrote rebuilt DDS into:\n{result.package_root}",
                )
                self.set_status_message(f"Wrote loose replacement for {resolved_entry.basename}.")

            self._run_utility_task(
                status_message=f"Rebuilding {resolved_entry.basename} from PNG...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _archive_entry_at_tree_position(self, position) -> Optional[ArchiveEntry]:
            item = self.archive_tree.itemAt(position)
            if item is None:
                return None
            kind = self._archive_tree_item_kind(item)
            value = self._archive_tree_item_value(item)
            if kind != "file" or not isinstance(value, int):
                return None
            if 0 <= value < len(self.archive_filtered_entries):
                return self.archive_filtered_entries[value]
            return None

        def _find_archive_entry_by_virtual_path(self, virtual_path: str) -> Optional[ArchiveEntry]:
            normalized = virtual_path.replace("\\", "/").strip().lower()
            matches = self.archive_entries_by_normalized_path.get(normalized, [])
            return matches[0] if matches else None

        def _show_archive_tree_context_menu(self, position) -> None:
            entry = self._archive_entry_at_tree_position(position)
            if entry is None:
                return

            menu = QMenu(self)
            if entry.extension in ARCHIVE_MESH_EXTENSIONS:
                export_obj_action = menu.addAction("Export OBJ...")
                export_obj_action.triggered.connect(lambda _checked=False, current_entry=entry: self._start_archive_mesh_export(current_entry, "obj"))
                export_fbx_action = menu.addAction("Export FBX...")
                export_fbx_action.triggered.connect(lambda _checked=False, current_entry=entry: self._start_archive_mesh_export(current_entry, "fbx"))
                menu.addSeparator()
                import_preview_action = menu.addAction("Import OBJ (preview rebuilt mesh)...")
                import_preview_action.triggered.connect(
                    lambda _checked=False, current_entry=entry: self._start_archive_mesh_import_preview(current_entry)
                )
                import_patch_action = menu.addAction("Import OBJ...")
                import_patch_action.triggered.connect(
                    lambda _checked=False, current_entry=entry: self._start_archive_mesh_patch(current_entry)
                )

            if entry.extension in ARCHIVE_AUDIO_EXPORT_EXTENSIONS or entry.extension in ARCHIVE_AUDIO_PATCH_EXTENSIONS:
                if not menu.isEmpty():
                    menu.addSeparator()
                export_audio_action = menu.addAction("Export WAV...")
                export_audio_action.triggered.connect(
                    lambda _checked=False, current_entry=entry: self._start_archive_audio_export(current_entry)
                )
                if entry.extension in ARCHIVE_AUDIO_PATCH_EXTENSIONS:
                    import_audio_action = menu.addAction("Import WAV + Patch to Game...")
                    import_audio_action.triggered.connect(
                        lambda _checked=False, current_entry=entry: self._start_archive_audio_patch(current_entry)
                    )

            if menu.isEmpty():
                return
            menu.exec(self.archive_tree.viewport().mapToGlobal(position))

        def _apply_archive_patch_result(self, patch_result: ArchivePatchResult) -> None:
            for normalized_path, updated_entry in patch_result.changed_entries.items():
                for existing_entry in self.archive_entries_by_normalized_path.get(normalized_path, []):
                    if existing_entry.pamt_path.resolve() != updated_entry.pamt_path.resolve():
                        continue
                    existing_entry.paz_file = updated_entry.paz_file
                    existing_entry.offset = updated_entry.offset
                    existing_entry.comp_size = updated_entry.comp_size
                    existing_entry.orig_size = updated_entry.orig_size
                    existing_entry.flags = updated_entry.flags
                    existing_entry.paz_index = updated_entry.paz_index
            self.archive_preview_cache.clear()

        def _show_archive_import_preview(
            self,
            entry: ArchiveEntry,
            import_result: MeshImportPreviewResult,
            *,
            patched: bool,
            backup_dir: Optional[Path] = None,
            loose_package_root: Optional[Path] = None,
        ) -> None:
            self._attach_archive_model_preview_images(import_result.preview_model)
            detail_lines = list(import_result.summary_lines)
            if patched and backup_dir is not None:
                detail_lines.append(f"Backup: {backup_dir}")
            if not patched and loose_package_root is not None:
                detail_lines.append(f"Loose export: {loose_package_root}")
            warning_badge = ""
            warning_text = ""
            if not patched and loose_package_root is not None:
                warning_badge = "Loose export"
                warning_text = f"This rebuilt mesh has been written to the mod-ready package at {loose_package_root}."
            elif not patched:
                warning_badge = "Import preview"
                warning_text = "This rebuilt mesh preview has not been written back to the game archives yet."
            preview_result = ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=(
                    f"{entry.extension} | {import_result.parsed_mesh.total_vertices:,} vertices"
                    f" | {import_result.parsed_mesh.total_faces:,} faces"
                ),
                detail_text="\n".join(detail_lines),
                preview_model=import_result.preview_model,
                model_texture_references=tuple(import_result.texture_references),
                preferred_view="model",
                warning_badge=warning_badge,
                warning_text=warning_text,
            )
            self.archive_preview_requested_loose = False
            self.current_archive_preview_result = preview_result
            self._show_archive_preview_result(preview_result, use_loose=False)

        def _attach_archive_model_preview_images(self, preview_model: Optional[object]) -> None:
            if preview_model is None:
                return
            meshes = getattr(preview_model, "meshes", None)
            if not meshes:
                return
            for mesh in meshes:
                preview_texture_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
                if not preview_texture_path or getattr(mesh, "preview_texture_image", None) is not None:
                    continue
                reader = QImageReader(preview_texture_path)
                image = reader.read()
                if image.isNull():
                    continue
                mesh.preview_texture_image = image

        def _start_archive_mesh_export(self, entry: ArchiveEntry, export_format: str) -> None:
            default_dir = self.settings_file_path.parent / "mesh_export"
            output_dir = QFileDialog.getExistingDirectory(
                self,
                f"Export {export_format.upper()}",
                str(default_dir),
            )
            if not output_dir:
                return

            def _task(log: Callable[[str], None]) -> MeshExportResult:
                return export_archive_mesh(
                    entry,
                    Path(output_dir),
                    export_format,
                    archive_entries_by_normalized_path=self.archive_entries_by_normalized_path,
                    on_log=log,
                )

            def _handle_complete(result: object) -> None:
                if not isinstance(result, MeshExportResult):
                    self.set_status_message("Mesh export finished with an unexpected result payload.", error=True)
                    return
                exported_files = "\n".join(str(path) for path in result.output_paths)
                summary_text = "\n".join(result.summary_lines)
                QMessageBox.information(
                    self,
                    "Mesh Export Complete",
                    f"{summary_text}\n\nExported files:\n{exported_files}",
                )
                self.set_status_message(f"Exported {entry.basename} as {export_format.upper()}.")

            self._run_utility_task(
                status_message=f"Exporting {entry.basename} as {export_format.upper()}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _start_archive_mesh_import_preview(self, entry: ArchiveEntry) -> None:
            obj_path, _selected = QFileDialog.getOpenFileName(
                self,
                "Select OBJ File",
                str(self.settings_file_path.parent),
                "Wavefront OBJ (*.obj)",
            )
            if not obj_path:
                return
            texconv_text = self.texconv_path_edit.text().strip()

            def _task(log: Callable[[str], None]) -> MeshImportPreviewResult:
                log(f"Rebuilding {entry.path} from {Path(obj_path).name}...")
                return build_mesh_import_preview(
                    entry,
                    Path(obj_path),
                    archive_entries_by_normalized_path=self.archive_entries_by_normalized_path,
                    texconv_path=(Path(texconv_text).expanduser() if texconv_text else None),
                    texture_entries_by_normalized_path=self.archive_entries_by_normalized_path,
                    texture_entries_by_basename=self.archive_entries_by_basename,
                )

            def _handle_complete(result: object) -> None:
                if not isinstance(result, MeshImportPreviewResult):
                    self.set_status_message("Mesh import preview finished with an unexpected result payload.", error=True)
                    return
                self._show_archive_import_preview(entry, result, patched=False)
                self.set_status_message(f"Rebuilt preview for {entry.basename}.")

            self._run_utility_task(
                status_message=f"Rebuilding mesh preview for {entry.basename}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _start_archive_mesh_patch(self, entry: ArchiveEntry) -> None:
            obj_path, _selected = QFileDialog.getOpenFileName(
                self,
                "Select OBJ File",
                str(self.settings_file_path.parent),
                "Wavefront OBJ (*.obj)",
            )
            if not obj_path:
                return

            destination = self._choose_archive_write_target(
                title="Import OBJ",
                message=f"Import {Path(obj_path).name} for {entry.path}?",
                allow_archive_patch=True,
            )
            if not destination:
                return

            if destination == "patch":
                confirmation = QMessageBox.question(
                    self,
                    "Patch Mesh To Game",
                    (
                        f"Patch {entry.path} using {Path(obj_path).name}?\n\n"
                        "A backup of the touched archive files will be created before anything is written."
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if confirmation != QMessageBox.Yes:
                    return

            loose_export_settings: Optional[Tuple[Path, ModPackageInfo, bool]] = None
            if destination == "loose":
                loose_export_settings = self._collect_archive_mod_ready_export_target(
                    browse_title="Select Mod-Ready Export Parent Root"
                )
                if loose_export_settings is None:
                    return

            paired_entry = None
            if entry.extension == ".pam":
                paired_entry = self._find_archive_entry_by_virtual_path(str(PurePosixPath(entry.path).with_suffix(".pamlod")))
            texconv_text = self.texconv_path_edit.text().strip()

            def _task(log: Callable[[str], None]) -> object:
                log(f"Rebuilding {entry.path} from {Path(obj_path).name}...")
                preview_result = build_mesh_import_preview(
                    entry,
                    Path(obj_path),
                    archive_entries_by_normalized_path=self.archive_entries_by_normalized_path,
                    texconv_path=(Path(texconv_text).expanduser() if texconv_text else None),
                    texture_entries_by_normalized_path=self.archive_entries_by_normalized_path,
                    texture_entries_by_basename=self.archive_entries_by_basename,
                )
                requests = [ArchivePatchRequest(entry=entry, payload_data=preview_result.rebuilt_data)]
                if paired_entry is not None and preview_result.paired_lod_data is not None:
                    requests.append(ArchivePatchRequest(entry=paired_entry, payload_data=preview_result.paired_lod_data))
                if destination == "patch":
                    log(f"Patching {len(requests)} archive entrie(s) back into the game files...")
                    patch_result = patch_archive_entries(requests, on_log=log)
                    return {
                        "preview": preview_result,
                        "patch": patch_result,
                    }
                if loose_export_settings is None:
                    raise RuntimeError("Mod-ready export target is not available.")
                parent_root, package_info, create_no_encrypt = loose_export_settings
                log(f"Writing {len(requests)} rebuilt entrie(s) into a mod-ready loose package...")
                loose_result = export_archive_payloads_to_mod_ready_loose(
                    requests,
                    parent_root=parent_root,
                    package_info=package_info,
                    create_no_encrypt_file=create_no_encrypt,
                    on_log=log,
                )
                return {
                    "preview": preview_result,
                    "loose": loose_result,
                }

            def _handle_complete(result: object) -> None:
                if not isinstance(result, dict):
                    self.set_status_message("Mesh import finished with an unexpected result payload.", error=True)
                    return
                preview_result = result.get("preview")
                patch_result = result.get("patch")
                loose_result = result.get("loose")
                if not isinstance(preview_result, MeshImportPreviewResult):
                    self.set_status_message("Mesh import finished with an incomplete result payload.", error=True)
                    return
                if destination == "patch":
                    if not isinstance(patch_result, ArchivePatchResult):
                        self.set_status_message("Mesh patch finished with an incomplete result payload.", error=True)
                        return
                    self._apply_archive_patch_result(patch_result)
                    self._show_archive_import_preview(entry, preview_result, patched=True, backup_dir=patch_result.backup_dir)
                    QMessageBox.information(
                        self,
                        "Patch Complete",
                        f"Patched {entry.path}\n\nBackup: {patch_result.backup_dir}",
                    )
                    self.set_status_message(f"Patched {entry.basename} to the game archives.")
                    return
                if not isinstance(loose_result, ArchiveLooseExportResult):
                    self.set_status_message("Mesh loose export finished with an incomplete result payload.", error=True)
                    return
                self._show_archive_import_preview(
                    entry,
                    preview_result,
                    patched=False,
                    loose_package_root=loose_result.package_root,
                )
                QMessageBox.information(
                    self,
                    "Loose Export Complete",
                    f"Wrote rebuilt mesh payload(s) into:\n{loose_result.package_root}",
                )
                self.set_status_message(f"Wrote rebuilt {entry.basename} into a mod-ready loose package.")

            self._run_utility_task(
                status_message=(
                    f"Patching {entry.basename} into the game archives..."
                    if destination == "patch"
                    else f"Writing {entry.basename} into a mod-ready loose package..."
                ),
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _start_archive_audio_export(self, entry: ArchiveEntry) -> None:
            default_dir = self.settings_file_path.parent / "audio_export"
            default_target = default_dir / f"{Path(entry.basename).stem}.wav"
            output_path, _selected = QFileDialog.getSaveFileName(
                self,
                "Export Audio As WAV",
                str(default_target),
                "WAV (*.wav)",
            )
            if not output_path:
                return

            def _task(log: Callable[[str], None]) -> Path:
                log(f"Exporting {entry.path} as WAV...")
                return export_archive_audio_as_wav(entry, Path(output_path))

            def _handle_complete(result: object) -> None:
                if not isinstance(result, Path):
                    self.set_status_message("Audio export finished with an unexpected result payload.", error=True)
                    return
                QMessageBox.information(self, "Audio Export Complete", f"Exported WAV:\n{result}")
                self.set_status_message(f"Exported {entry.basename} as WAV.")

            self._run_utility_task(
                status_message=f"Exporting {entry.basename} as WAV...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _start_archive_audio_patch(self, entry: ArchiveEntry) -> None:
            source_path, _selected = QFileDialog.getOpenFileName(
                self,
                "Select Replacement Audio",
                str(self.settings_file_path.parent),
                "Audio Files (*.wav *.ogg *.mp3)",
            )
            if not source_path:
                return

            confirmation = QMessageBox.question(
                self,
                "Patch Audio To Game",
                (
                    f"Patch {entry.path} using {Path(source_path).name}?\n\n"
                    "A backup of the touched archive files will be created before anything is written."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmation != QMessageBox.Yes:
                return

            def _task(log: Callable[[str], None]) -> ArchivePatchResult:
                log(f"Preparing replacement audio for {entry.path}...")
                replacement_payload = build_archive_audio_patch_payload(entry, Path(source_path))
                return patch_archive_entries([ArchivePatchRequest(entry=entry, payload_data=replacement_payload)], on_log=log)

            def _handle_complete(result: object) -> None:
                if not isinstance(result, ArchivePatchResult):
                    self.set_status_message("Audio patch finished with an unexpected result payload.", error=True)
                    return
                self._apply_archive_patch_result(result)
                current_entry = self._current_archive_entry()
                if current_entry is not None and current_entry.path == entry.path:
                    self._render_archive_preview(current_entry)
                QMessageBox.information(
                    self,
                    "Audio Patch Complete",
                    f"Patched {entry.path}\n\nBackup: {result.backup_dir}",
                )
                self.set_status_message(f"Patched audio entry {entry.basename}.")

            self._run_utility_task(
                status_message=f"Patching audio for {entry.basename}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _restore_archive_patch_backup_from_ui(self) -> None:
            backups = list_archive_patch_backups()
            if not backups:
                self.set_status_message(
                    f"No archive patch backups were found under {ARCHIVE_PATCH_BACKUP_ROOT}.",
                    error=True,
                )
                return

            selected_dir = QFileDialog.getExistingDirectory(
                self,
                "Select Archive Patch Backup",
                str(backups[0]),
            )
            if not selected_dir:
                return

            backup_dir = Path(selected_dir)
            manifest_path = backup_dir / "backup_manifest.json"
            if not manifest_path.is_file():
                QMessageBox.warning(
                    self,
                    "Restore Backup",
                    f"{backup_dir} does not contain a backup_manifest.json file.",
                )
                return

            confirmation = QMessageBox.question(
                self,
                "Restore Archive Patch Backup",
                (
                    f"Restore files from:\n{backup_dir}\n\n"
                    "This will overwrite the current archive files with the selected backup copy."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirmation != QMessageBox.Yes:
                return

            def _task(log: Callable[[str], None]) -> Path:
                log(f"Restoring archive patch backup from {backup_dir}...")
                return restore_archive_patch_backup(backup_dir, on_log=log)

            def _handle_complete(result: object) -> None:
                restored_dir = result if isinstance(result, Path) else backup_dir
                self.set_status_message(f"Restored archive backup from {restored_dir}.")
                QMessageBox.information(
                    self,
                    "Backup Restored",
                    f"Restored archive files from:\n{restored_dir}",
                )
                QTimer.singleShot(150, lambda: self.scan_archives(force_refresh=True))

            self._run_utility_task(
                status_message=f"Restoring archive backup from {backup_dir.name}...",
                task=_task,
                on_complete=_handle_complete,
                show_archive_progress=True,
            )

        def _export_current_archive_model(self) -> None:
            result = self.current_archive_preview_result
            preview_model = result.preview_model if result is not None else None
            if preview_model is None or self.archive_preview_showing_loose:
                self.set_status_message("No model preview is available to export.", error=True)
                return

            current_entry = self._current_archive_mesh_entry()
            if current_entry is not None:
                self._start_archive_mesh_export(current_entry, "obj")
                return

            preview_path = str(getattr(preview_model, "path", "") or "").strip()
            if preview_path:
                default_stem = Path(PurePosixPath(preview_path).name).stem
            else:
                default_stem = Path(current_entry.basename).stem if current_entry is not None else "archive_model"

            default_dir = self.settings_file_path.parent / "model_export"
            default_target = default_dir / f"{default_stem}.obj"
            selected, _ = QFileDialog.getSaveFileName(
                self,
                "Export Model Preview",
                str(default_target),
                "Wavefront OBJ (*.obj)",
            )
            if not selected:
                return

            try:
                exported_path = export_model_preview_to_obj(preview_model, Path(selected))
            except Exception as exc:
                QMessageBox.warning(self, "Model Export", str(exc))
                self.set_status_message(f"Model export failed: {exc}", error=True)
                return

            self.set_status_message(f"Exported model preview to {exported_path}")

        def _export_current_archive_mesh(self, export_format: str) -> None:
            current_entry = self._current_archive_mesh_entry()
            if current_entry is None:
                self.set_status_message("Select a supported archive mesh to export.", error=True)
                return
            self._start_archive_mesh_export(current_entry, export_format)

        def _preview_current_archive_mesh_import(self) -> None:
            current_entry = self._current_archive_mesh_entry()
            if current_entry is None:
                self.set_status_message("Select a supported archive mesh before importing an OBJ preview.", error=True)
                return
            self._start_archive_mesh_import_preview(current_entry)

        def _patch_current_archive_mesh_from_obj(self) -> None:
            current_entry = self._current_archive_mesh_entry()
            if current_entry is None:
                self.set_status_message("Select a supported archive mesh before importing an OBJ.", error=True)
                return
            self._start_archive_mesh_patch(current_entry)

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
                preview_image = result.loose_preview_image
                preview_media_path = result.loose_preview_media_path
                preview_media_kind = result.loose_preview_media_kind
                if preview_image is not None or preview_image_path:
                    preferred_view = "image"
                elif preview_media_path:
                    preferred_view = "media"
                else:
                    preferred_view = "info"
            else:
                title = result.title or "Archive Preview"
                metadata_summary = result.metadata_summary or "Preview ready."
                detail_text = result.detail_text or metadata_summary
                warning_badge = result.warning_badge
                warning_text = result.warning_text
                preview_image_path = result.preview_image_path
                preview_image = result.preview_image
                preview_media_path = result.preview_media_path
                preview_media_kind = result.preview_media_kind
                preferred_view = result.preferred_view

            self.archive_preview_title_label.setText(title)
            self.archive_preview_meta_label.setText(metadata_summary)
            self.archive_preview_details_edit.setPlainText(detail_text)
            self._update_archive_preview_warning_controls(
                badge_text=warning_badge,
                warning_text=warning_text,
                can_toggle_loose=bool(result.loose_file_path),
            )
            if not self.archive_preview_showing_loose:
                self._populate_archive_texture_reference_list(result.model_texture_references)
            else:
                self._populate_archive_texture_reference_list(())

            if preferred_view == "image" and (preview_image is not None or preview_image_path):
                if preview_image is not None:
                    self.archive_preview_label.set_preview_image(preview_image, title or "Preview image")
                else:
                    self.archive_preview_label.set_preview_image_path(preview_image_path, title or "Preview image")
                self.archive_media_preview.clear_media("No media preview available.")
                self.archive_model_preview.clear_model("No model preview available.")
                self.archive_preview_stack.setCurrentWidget(self.archive_preview_scroll)
                self.archive_preview_tabs.setCurrentIndex(0)
                self._update_archive_model_action_controls(None)
                self._set_archive_preview_image_controls_enabled(True)
                self._apply_archive_preview_zoom()
                return

            if preferred_view == "model" and result.preview_model is not None and not self.archive_preview_showing_loose:
                self.archive_model_preview.set_model(result.preview_model)
                self.archive_media_preview.clear_media("No media preview available.")
                self.archive_preview_label.clear_preview("No image preview available.")
                self.archive_preview_stack.setCurrentWidget(self.archive_model_preview)
                self.archive_preview_tabs.setCurrentIndex(0)
                self._update_archive_model_action_controls(result.preview_model)
                self._set_archive_preview_image_controls_enabled(True)
                self._apply_archive_preview_zoom()
                return

            if preferred_view == "media" and preview_media_path:
                self.archive_preview_label.clear_preview("No image preview available.")
                self.archive_model_preview.clear_model("No model preview available.")
                self.archive_media_preview.set_media(
                    preview_media_path,
                    media_kind=preview_media_kind,
                    detail_text=detail_text,
                )
                self.archive_preview_stack.setCurrentWidget(self.archive_media_preview)
                self.archive_preview_tabs.setCurrentIndex(0)
                self._update_archive_model_action_controls(None)
                self._set_archive_preview_image_controls_enabled(False)
                return

            if preferred_view == "text":
                preview_text = result.preview_text or "No text preview available."
                self.archive_preview_text_edit.set_language_for_extension(
                    self._archive_preview_text_language_extension(preview_text)
                )
                self.archive_preview_text_edit.setPlainText(preview_text)
                self.archive_preview_stack.setCurrentWidget(self.archive_preview_text_edit)
                self.archive_preview_tabs.setCurrentIndex(0)
                self.archive_preview_label.clear_preview("No image preview available.")
                self.archive_model_preview.clear_model("No model preview available.")
                self.archive_media_preview.clear_media("No media preview available.")
                self._update_archive_model_action_controls(None)
                self._set_archive_preview_image_controls_enabled(False)
                return

            self.archive_preview_info_edit.setPlainText(detail_text or metadata_summary or "No preview available.")
            self.archive_preview_stack.setCurrentWidget(self.archive_preview_info_edit)
            self.archive_preview_tabs.setCurrentIndex(0)
            self.archive_preview_label.clear_preview("No image preview available.")
            self.archive_model_preview.clear_model("No model preview available.")
            self.archive_media_preview.clear_media("No media preview available.")
            self._update_archive_model_action_controls(None)
            self._set_archive_preview_image_controls_enabled(False)

        def _toggle_archive_loose_preview(self) -> None:
            if self.current_archive_preview_result is None or not self.current_archive_preview_result.loose_file_path:
                return
            if self.archive_preview_showing_loose:
                self.archive_preview_requested_loose = False
                self._show_archive_preview_result(self.current_archive_preview_result, use_loose=False)
                return

            if (
                self.current_archive_preview_result.loose_preview_image is not None
                or self.current_archive_preview_result.loose_preview_image_path
                or self.current_archive_preview_result.loose_preview_media_path
            ):
                self.archive_preview_requested_loose = True
                self._show_archive_preview_result(self.current_archive_preview_result, use_loose=True)
                return

            entry = self._current_archive_entry()
            if entry is None:
                return
            self._render_archive_preview(
                entry,
                include_loose_preview_assets=True,
                prefer_loose_preview=True,
            )

        def _apply_archive_preview_result(self, result: ArchivePreviewResult) -> None:
            try:
                self.current_archive_preview_result = result
                self._show_archive_preview_result(result, use_loose=self.archive_preview_requested_loose)
            except Exception as exc:
                _write_crash_report(
                    "archive_preview_result_error",
                    "Archive preview result error",
                    str(exc),
                    context=_collect_crash_context(),
                )
                self._clear_archive_preview(f"Preview failed: {exc}")
                self.set_status_message(f"Archive preview failed: {exc}", error=True)

        def _cleanup_archive_preview_refs(self) -> None:
            self.archive_preview_thread = None
            self.archive_preview_worker = None
            if self._shutting_down:
                self.pending_archive_preview_request = None
                self.scheduled_archive_preview_request = None
                return
            if self.pending_archive_preview_request is None:
                return
            request_id, entry, include_loose_preview_assets = self.pending_archive_preview_request
            self.pending_archive_preview_request = None
            texconv_text = self.texconv_path_edit.text().strip()
            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            self._start_archive_preview_worker(
                request_id,
                texconv_path,
                entry,
                self._collect_archive_preview_loose_roots(),
                include_loose_preview_assets=include_loose_preview_assets,
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
            selected_entries = self._selected_archive_entries()
            selected_count = len(selected_entries)
            has_filtered_entries = bool(self.archive_filtered_entries)
            has_filtered_dds = self.archive_filtered_dds_count > 0
            selected_has_dds = any(entry.extension == ".dds" for entry in selected_entries)
            workflow_extract_enabled = selected_has_dds if selected_count > 0 else has_filtered_dds
            self.archive_extract_selected_button.setEnabled(self.worker_thread is None and selected_count > 0)
            self.archive_extract_filtered_button.setEnabled(self.worker_thread is None and has_filtered_entries)
            self.archive_extract_to_workflow_button.setEnabled(self.worker_thread is None and workflow_extract_enabled)
            current_entry = self._current_archive_entry()
            self.archive_open_in_editor_button.setEnabled(self.worker_thread is None and current_entry is not None)
            self.archive_resolve_in_research_button.setEnabled(
                self.worker_thread is None
                and current_entry is not None
                and current_entry.extension == ".dds"
            )
            if not self.archive_entries:
                self.archive_stats_label.setText("No archives scanned.")
                return
            self.archive_stats_label.setText(
                f"{len(self.archive_filtered_entries):,} shown / {len(self.archive_entries):,} total entries. "
                f"DDS in current view: {self.archive_filtered_dds_count:,}. "
                f"Selected files: {selected_count:,}."
            )

        def _active_archive_preview_zoom_widget(self):
            current_widget = self.archive_preview_stack.currentWidget()
            if current_widget is self.archive_preview_scroll:
                return self.archive_preview_label
            if current_widget is self.archive_model_preview:
                return self.archive_model_preview
            return None

        def _handle_archive_model_view_state_changed(self, zoom_factor: float, fit_to_view: bool) -> None:
            if self.archive_preview_stack.currentWidget() is not self.archive_model_preview:
                return
            self.archive_preview_zoom_factor = min(max(float(zoom_factor), 0.1), 16.0)
            self.archive_preview_fit_to_view = bool(fit_to_view)
            self._update_archive_preview_zoom_label()

        def _update_archive_preview_zoom_label(self) -> None:
            if self.archive_preview_fit_to_view:
                self.archive_preview_zoom_value.setText("Fit")
            else:
                self.archive_preview_zoom_value.setText(f"{int(round(self.archive_preview_zoom_factor * 100))}%")

        def _apply_archive_preview_zoom(self) -> None:
            target = self._active_archive_preview_zoom_widget()
            if target is not None:
                target.set_fit_to_view(self.archive_preview_fit_to_view)
                target.set_zoom_factor(self.archive_preview_zoom_factor)
            self._update_archive_preview_zoom_label()

        def _set_archive_preview_fit_mode(self) -> None:
            self.archive_preview_fit_to_view = True
            self._apply_archive_preview_zoom()

        def _set_archive_preview_zoom_factor(self, zoom_factor: float) -> None:
            self.archive_preview_fit_to_view = False
            self.archive_preview_zoom_factor = min(max(zoom_factor, 0.1), 16.0)
            self._apply_archive_preview_zoom()

        def _adjust_archive_preview_zoom(self, step: int) -> None:
            target = self._active_archive_preview_zoom_widget()
            current_zoom = (
                target.current_display_scale()
                if self.archive_preview_fit_to_view and target is not None
                else self.archive_preview_zoom_factor
            )
            zoom_steps = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0]
            closest_index = min(range(len(zoom_steps)), key=lambda idx: abs(zoom_steps[idx] - current_zoom))
            next_index = min(max(closest_index + step, 0), len(zoom_steps) - 1)
            self._set_archive_preview_zoom_factor(zoom_steps[next_index])

        def _prompt_archive_extract_options(
            self,
            entries: Sequence[ArchiveEntry],
            output_root: Path,
        ) -> Optional[Tuple[bool, str]]:
            summary_box = QMessageBox(self)
            summary_box.setWindowTitle("Archive Extraction Target")
            summary_box.setIcon(QMessageBox.Information)
            summary_box.setText(f"{len(entries):,} archive file(s) will be extracted to:")
            summary_box.setInformativeText(
                f"{output_root}\n\n"
                "If this folder does not exist yet, the app will create it.\n"
                "If files already exist there, you will be asked whether to clear the folder, "
                "overwrite matching files, or keep both by renaming the new copies."
            )
            continue_button = summary_box.addButton("Continue", QMessageBox.AcceptRole)
            summary_cancel_button = summary_box.addButton(QMessageBox.Cancel)
            summary_box.setDefaultButton(continue_button)
            summary_box.exec()
            if summary_box.clickedButton() == summary_cancel_button:
                return None

            if not self._preference_bool("confirm_archive_extract_cleanup", True):
                return False, "overwrite"

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
                            f"Target folder:\n{output_root}\n\n"
                            "Choose whether to overwrite existing files or keep both by renaming the newly extracted copies."
                        )
                        overwrite_button = collision_box.addButton("Overwrite Existing", QMessageBox.AcceptRole)
                        rename_button = collision_box.addButton("Keep Both (Rename New Files)", QMessageBox.ActionRole)
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
                    self._set_pending_archive_workflow_extract(
                        entries=entries,
                        output_root=Path(output_root_value).expanduser(),
                    )
                    self._pending_texture_editor_workflow_export = None
                    workflow_filters: List[str] = []
                    for entry in entries:
                        if not isinstance(entry, ArchiveEntry):
                            continue
                        package_root = entry.pamt_path.parent.name.strip() or "package"
                        relative_path = PurePosixPath(package_root, *PurePosixPath(entry.path.replace("\\", "/")).parts).as_posix()
                        workflow_filters.append(relative_path)
                    if workflow_filters and len(workflow_filters) <= 256:
                        self.filters_edit.setPlainText("\n".join(workflow_filters))
                    self.main_tabs.setCurrentWidget(self.workflow_tab)
                    if workflow_filters and len(workflow_filters) == 1:
                        self.set_status_message(
                            f"Extracted {extracted} archive DDS file(s) to {output_root_value}, set Original DDS root, and focused the workflow filter on {workflow_filters[0]}."
                        )
                    elif workflow_filters and len(workflow_filters) <= 256:
                        self.set_status_message(
                            f"Extracted {extracted} archive DDS file(s) to {output_root_value}, set Original DDS root, and focused the workflow filter on the extracted DDS set."
                        )
                    else:
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
            dds_entries, used_selection = self._archive_entries_for_workflow_extract()
            if used_selection and not dds_entries:
                self.set_status_message(
                    "The current archive selection does not include any DDS files. Select DDS files or clear the selection to use the filtered view.",
                    error=True,
                )
                return
            self._run_archive_extract(
                dds_entries,
                set_original_dds_root=True,
                allow_original_dds_root=True,
                description=(
                    "Extracting selected DDS archive entries to workflow root..."
                    if used_selection
                    else "Extracting filtered DDS archive entries to workflow root..."
                ),
            )

        def collect_config(self) -> AppConfig:
            return AppConfig(
                original_dds_root=self.original_dds_edit.text().strip(),
                png_root=self.png_root_edit.text().strip(),
                texture_editor_png_root=self.texture_editor_png_root_edit.text().strip(),
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
                texture_rules_text=self.texture_rules_legacy_text,
                texture_rules=tuple(self.texture_rules_state),
                workflow_profiles=tuple(self.workflow_profiles_state),
                dry_run=self.dry_run_checkbox.isChecked(),
                csv_log_enabled=self.csv_log_enabled_checkbox.isChecked(),
                csv_log_path=self.csv_log_path_edit.text().strip(),
                allow_unique_basename_fallback=self.unique_basename_checkbox.isChecked(),
                overwrite_existing_dds=self.overwrite_existing_checkbox.isChecked(),
                include_filters=self.filters_edit.toPlainText(),
                upscale_backend=self._current_upscale_backend(),
                enable_chainner=self._current_upscale_backend() == UPSCALE_BACKEND_CHAINNER,
                chainner_exe_path=self.chainner_exe_path_edit.text().strip(),
                chainner_chain_path=self.chainner_chain_path_edit.text().strip(),
                chainner_override_json=self.chainner_override_edit.toPlainText(),
                ncnn_exe_path=self.ncnn_exe_path_edit.text().strip(),
                ncnn_model_dir=self.ncnn_model_dir_edit.text().strip(),
                ncnn_model_name=self._combo_value(self.ncnn_model_combo),
                ncnn_scale=self.ncnn_scale_spin.value(),
                ncnn_tile_size=self.ncnn_tile_size_spin.value(),
                ncnn_extra_args=self.ncnn_extra_args_edit.text().strip(),
                upscale_post_correction_mode=self._combo_value(self.upscale_post_correction_combo),
                upscale_texture_preset=self._combo_value(self.upscale_texture_preset_combo),
                enable_automatic_texture_rules=self.enable_automatic_texture_rules_checkbox.isChecked(),
                enable_unsafe_technical_override=self.enable_unsafe_technical_override_checkbox.isChecked(),
                retry_smaller_tile_on_failure=self.retry_smaller_tile_checkbox.isChecked(),
                enable_mod_ready_loose_export=self.enable_mod_ready_loose_export_checkbox.isChecked(),
                mod_ready_export_root=self.mod_ready_export_root_edit.text().strip(),
                mod_ready_create_no_encrypt_file=self.mod_ready_create_no_encrypt_checkbox.isChecked(),
                mod_ready_package_title=self.mod_ready_package_title_edit.text().strip(),
                mod_ready_package_version=self.mod_ready_package_version_edit.text().strip(),
                mod_ready_package_author=self.mod_ready_package_author_edit.text().strip(),
                mod_ready_package_description=self.mod_ready_package_description_edit.text().strip(),
                mod_ready_package_nexus_url=self.mod_ready_package_nexus_url_edit.text().strip(),
                archive_package_root=self.archive_package_root_edit.text().strip(),
                archive_extract_root=self.archive_extract_root_edit.text().strip(),
                archive_filter_text=self.archive_filter_edit.text().strip(),
                archive_exclude_filter_text=self.archive_exclude_filter_edit.text().strip(),
                archive_extension_filter=self._combo_value(self.archive_extension_filter_combo),
                archive_package_filter_text=self.archive_package_filter_edit.text().strip(),
                archive_structure_filter=self._current_archive_structure_filter_value(),
                archive_role_filter=self._combo_value(self.archive_role_filter_combo),
                archive_exclude_common_technical_suffixes=self.archive_exclude_common_technical_checkbox.isChecked(),
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
            self.export_diagnostics_action.setEnabled(not busy)
            self.quick_start_menu_action.setEnabled(not busy)
            self.open_documentation_action.setEnabled(not busy)
            self.left_panel.setEnabled(not busy)
            self.scan_button.setEnabled(not busy)
            self.preview_policy_button.setEnabled(not busy)
            self.clear_workflow_roots_button.setEnabled(not busy)
            self.start_button.setEnabled(not busy)
            self.stop_button.setEnabled(busy and build_mode)
            self.refresh_compare_button.setEnabled(not busy)
            self.compare_list.setEnabled(not busy)
            self.compare_previous_button.setEnabled(not busy and self.compare_list.currentRow() > 0)
            self.compare_next_button.setEnabled(
                not busy and 0 <= self.compare_list.currentRow() < self.compare_list.count() - 1
            )
            self.compare_mip_details_button.setEnabled(
                not busy and 0 <= self.compare_list.currentRow() < self.compare_list.count()
            )
            self.compare_open_in_editor_button.setEnabled(
                not busy and 0 <= self.compare_list.currentRow() < self.compare_list.count()
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
            self.archive_exclude_filter_edit.setEnabled(not busy)
            self.archive_extension_filter_combo.setEnabled(not busy)
            self.archive_package_filter_edit.setEnabled(not busy)
            self._set_archive_structure_filter_enabled(not busy)
            self.archive_role_filter_combo.setEnabled(not busy)
            self.archive_exclude_common_technical_checkbox.setEnabled(not busy)
            self.archive_min_size_spin.setEnabled(not busy)
            self.archive_previewable_only_checkbox.setEnabled(not busy)
            self.archive_tree_view_checkbox.setEnabled(not busy)
            selected_entries = self._selected_archive_entries()
            self.archive_extract_selected_button.setEnabled(not busy and len(selected_entries) > 0)
            self.archive_extract_filtered_button.setEnabled(not busy and bool(self.archive_filtered_entries))
            selected_has_dds = any(entry.extension == ".dds" for entry in selected_entries)
            filtered_has_dds = any(entry.extension == ".dds" for entry in self.archive_filtered_entries)
            workflow_extract_enabled = selected_has_dds if selected_entries else filtered_has_dds
            self.archive_extract_to_workflow_button.setEnabled(not busy and workflow_extract_enabled)
            self.archive_open_in_editor_button.setEnabled(not busy and self._current_archive_entry() is not None)
            self.archive_resolve_in_research_button.setEnabled(
                not busy
                and self._current_archive_entry() is not None
                and self._current_archive_entry().extension == ".dds"
            )
            self.archive_tree.setEnabled(not busy)
            self.archive_model_preview.setEnabled(not busy)
            self.archive_media_preview.setEnabled(not busy)
            self.archive_preview_text_edit.setEnabled(not busy)
            self.archive_preview_info_edit.setEnabled(not busy)
            self.text_search_tab.set_external_busy(busy)
            self.research_tab.setEnabled(not busy)
            self.replace_assistant_tab.set_external_busy(busy)
            self.texture_editor_tab.setEnabled(not busy)
            self.settings_tab.setEnabled(not busy)
            self.archive_preview_loose_toggle_button.setEnabled(
                not busy and self.archive_preview_loose_toggle_button.isVisible()
            )
            zoomable_preview_enabled = not busy and self._active_archive_preview_zoom_widget() is not None
            self.archive_preview_zoom_out_button.setEnabled(zoomable_preview_enabled)
            self.archive_preview_zoom_fit_button.setEnabled(zoomable_preview_enabled)
            self.archive_preview_zoom_100_button.setEnabled(zoomable_preview_enabled)
            self.archive_preview_zoom_in_button.setEnabled(zoomable_preview_enabled)
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

        def preview_texture_policy(self) -> None:
            if self._background_task_active():
                return

            config = self.collect_config()

            def task(on_log: Callable[[str], None]) -> Dict[str, object]:
                on_log("Building per-texture policy preview...")
                normalized = normalize_config(config, validate_backend_runtime=False)
                dds_files = collect_dds_files(
                    normalized.original_dds_root,
                    normalized.include_filter_patterns,
                )
                if not dds_files:
                    raise ValueError("No DDS files were found under the original root with the current filter.")
                processing_plan = build_texture_processing_plan(normalized, dds_files)
                payload = build_texture_policy_preview_payload(
                    normalized,
                    dds_files,
                    processing_plan=processing_plan,
                )
                requires_png_processing = any(entry.requires_png_processing for entry in processing_plan)
                if normalized.upscale_backend != UPSCALE_BACKEND_NONE and requires_png_processing:
                    try:
                        validate_backend_runtime_requirements(normalized)
                    except Exception as exc:
                        payload["runtime_validation_warning"] = (
                            "Runtime/config validation warning: "
                            + str(exc)
                            + "\nThe semantic policy preview below is still valid, but Start would fail until this is fixed."
                        )
                elif normalized.upscale_backend != UPSCALE_BACKEND_NONE:
                    payload["runtime_validation_warning"] = (
                        "Current preset and automatic rules keep every matched DDS out of the PNG/upscale path, "
                        "so backend/runtime validation was intentionally skipped for this preview."
                    )
                return payload

            def on_complete(result: object) -> None:
                if not isinstance(result, dict):
                    self.set_status_message("Texture policy preview returned an unexpected result.", error=True)
                    return
                dialog = TexturePolicyPreviewDialog(theme_key=self.current_theme_key, parent=self)
                dialog.set_payload(result)
                self.set_status_message("Texture policy preview is ready.")
                dialog.exec()

            self._run_utility_task(
                status_message="Building texture policy preview...",
                task=task,
                on_complete=on_complete,
            )

        def start_dds_to_png(self) -> None:
            if self._background_task_active():
                return

            config = self.collect_config()
            if not self._prepare_workflow_output_roots_for_start(config, include_output_root=False):
                return
            self._apply_pending_archive_workflow_extract_if_needed()
            self._apply_pending_texture_editor_workflow_export_if_needed()
            self.set_status_message("Preparing DDS to PNG conversion...")
            self.append_log("Starting DDS -> PNG conversion.")
            if config.upscale_backend == UPSCALE_BACKEND_NONE:
                self.append_log(
                    "Warning: DDS-to-PNG conversion is enabled while the upscaling backend is disabled, so Start will convert DDS files to PNG and stop."
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
            if config.enable_dds_staging and config.upscale_backend == UPSCALE_BACKEND_NONE:
                self.start_dds_to_png()
                return
            if not self._prepare_workflow_output_roots_for_start(config, include_output_root=True):
                return
            self._apply_pending_archive_workflow_extract_if_needed()
            self._apply_pending_texture_editor_workflow_export_if_needed()
            self._last_build_unknown_review_result = None
            if config.upscale_backend != UPSCALE_BACKEND_NONE:
                self._check_unclassified_files_before_build(config)
                return
            self._begin_build_with_config(config)

        def _begin_build_with_config(self, config: AppConfig) -> None:
            if self._background_task_active():
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

        def _begin_build_when_idle(self, config: AppConfig, *, attempt: int = 0) -> None:
            if not self._background_task_active():
                self._begin_build_with_config(config)
                return
            if self.utility_worker is not None and attempt < 100:
                QTimer.singleShot(
                    10,
                    lambda config=config, attempt=attempt + 1: self._begin_build_when_idle(
                        config,
                        attempt=attempt,
                    ),
                )
                return
            self.set_status_message("Build could not start after the pre-run classification check.", error=True)
            self.append_log(
                "ERROR: Build start was blocked after the pre-run classification check did not fully release its worker state."
            )

        def _open_classification_review_for_paths(self, paths: Sequence[str]) -> None:
            path_list = [str(path).strip() for path in paths if str(path).strip()]
            self.main_tabs.setCurrentWidget(self.research_tab)
            if not path_list:
                self.set_status_message(
                    "Build paused so you can review DDS files that still need a saved local classification in Research -> Classification Review."
                )
                return
            self.research_tab.focus_classification_review_for_paths(
                path_list,
                include_classified=True,
                refresh_if_needed=not bool(getattr(self.research_tab, "research_payload", {})),
            )
            self.set_status_message(
                f"Build paused so you can review/save classification for {len(path_list):,} DDS file(s) in Research -> Classification Review."
            )

        def _review_reference_in_text_search(self, source_path: str, highlight_query: str) -> None:
            normalized_path = source_path.strip().replace("\\", "/").strip("/")
            query = highlight_query.strip()
            if not normalized_path or not query:
                self.set_status_message("The selected reference row is missing its source path or highlight query.", error=True)
                return
            entry: Optional[ArchiveEntry] = None
            for candidate in self.archive_entries:
                if not isinstance(candidate, ArchiveEntry):
                    continue
                candidate_path = candidate.path.replace("\\", "/").strip("/")
                if candidate_path.casefold() == normalized_path.casefold():
                    entry = candidate
                    break
            if entry is None:
                self.set_status_message(
                    f"Could not find the archive text entry for {normalized_path}. Refresh archives and try again.",
                    error=True,
                )
                return
            if not self.text_search_tab.review_archive_entry(entry, highlight_query=query):
                return
            self.main_tabs.setCurrentWidget(self.text_search_tab)

        def _check_unclassified_files_before_build(self, config: AppConfig) -> None:
            def task(on_log: Callable[[str], None]) -> Dict[str, object]:
                normalized = normalize_config(config, validate_backend_runtime=False)
                dds_files = collect_dds_files(
                    normalized.original_dds_root,
                    normalized.include_filter_patterns,
                )
                total = len(dds_files)
                if total <= 0:
                    raise ValueError("No DDS files were found under the original root with the current filter.")
                processing_plan = build_texture_processing_plan(
                    normalized,
                    dds_files,
                )
                unknown_entries = [
                    entry
                    for entry in processing_plan
                    if entry.decision.texture_type == "unknown"
                    and get_registered_texture_classification(entry.relative_path.as_posix()) is None
                ]
                unknown_paths = [entry.relative_path.as_posix() for entry in unknown_entries]
                processed_unknowns = sum(1 for entry in unknown_entries if entry.requires_png_processing)
                preserved_unknowns = len(unknown_entries) - processed_unknowns
                example_names: List[str] = []
                seen_examples: set[str] = set()
                for rel_path in unknown_paths:
                    basename = PurePosixPath(rel_path).name
                    if basename.casefold() in seen_examples:
                        continue
                    seen_examples.add(basename.casefold())
                    example_names.append(basename)
                    if len(example_names) >= 6:
                        break
                on_log(
                    f"Pre-run classification check: {len(unknown_entries):,} matched DDS file(s) are still unclassified."
                )
                return {
                    "total_files": total,
                    "unknown_total": len(unknown_entries),
                    "processed_unknowns": processed_unknowns,
                    "preserved_unknowns": preserved_unknowns,
                    "unknown_paths": unknown_paths,
                    "example_names": example_names,
                    "preset_label": get_texture_preset_definition(normalized.upscale_texture_preset).label,
                }

            def on_complete(result: object) -> None:
                payload = result if isinstance(result, dict) else {}
                unknown_total = int(payload.get("unknown_total", 0) or 0)
                if unknown_total <= 0:
                    QTimer.singleShot(0, lambda config=config: self._begin_build_when_idle(config))
                    return

                processed_unknowns = int(payload.get("processed_unknowns", 0) or 0)
                preserved_unknowns = int(payload.get("preserved_unknowns", 0) or 0)
                example_names = [
                    str(item) for item in payload.get("example_names", [])
                    if str(item).strip()
                ]
                preset_label = str(payload.get("preset_label", "") or "").strip()

                box = QMessageBox(self)
                box.setWindowTitle("DDS Files Need Saved Classification")
                box.setIcon(QMessageBox.Question)
                box.setText(
                    f"{unknown_total:,} matched DDS file(s) still lack a saved local classification approval for this workflow input."
                )
                detail_lines = []
                if preset_label:
                    detail_lines.append(f"Current texture preset: {preset_label}.")
                detail_lines.append(
                    "Research may still show an inferred family classification from archive context, but Texture Workflow only stops warning once the DDS has a saved local approval."
                )
                if processed_unknowns <= 0:
                    detail_lines.append(
                        "Under the current preset and policy rules, these files will likely be left unchanged."
                    )
                elif preserved_unknowns <= 0:
                    detail_lines.append(
                        "Under the current preset and policy rules, these files will likely still be processed."
                    )
                else:
                    detail_lines.append(
                        f"Under the current preset and policy rules, about {processed_unknowns:,} would be processed and {preserved_unknowns:,} would likely be left unchanged."
                    )
                detail_lines.append(
                    "Review them now if you want to approve classifications before the run starts."
                )
                if example_names:
                    detail_lines.extend(
                        [
                            "",
                            "Examples:",
                            ", ".join(example_names[:5]),
                        ]
                    )
                box.setInformativeText("\n".join(detail_lines))
                review_button = box.addButton("Review Classifications", QMessageBox.ActionRole)
                continue_button = box.addButton("Continue Anyway", QMessageBox.AcceptRole)
                cancel_button = box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(review_button)
                box.exec()

                clicked = box.clickedButton()
                if clicked == review_button:
                    unknown_paths = [
                        str(path) for path in payload.get("unknown_paths", [])
                        if str(path).strip()
                    ]
                    self.append_log(
                        f"Build paused so Research -> Classification Review can focus on {len(unknown_paths):,} unclassified DDS file(s)."
                    )
                    QTimer.singleShot(0, lambda paths=unknown_paths: self._open_classification_review_for_paths(paths))
                    return
                if clicked != continue_button:
                    self.set_status_message("Build cancelled before start.")
                    return

                self._last_build_unknown_review_result = payload
                self.append_log(
                    f"Continuing build with {unknown_total:,} unclassified DDS file(s)."
                )
                QTimer.singleShot(0, lambda config=config: self._begin_build_when_idle(config))

            self._run_utility_task(
                status_message="Checking for unclassified DDS files before build...",
                task=task,
                on_complete=on_complete,
            )

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
                unknown_total = int(self._last_build_unknown_review_result.get("unknown_total", 0) or 0) if isinstance(self._last_build_unknown_review_result, dict) else 0
                if unknown_total > 0:
                    self.set_status_message(
                        f"Build completed. {unknown_total:,} matched DDS file(s) were still unclassified in this run."
                    )
                else:
                    self.set_status_message("Build completed successfully.")
            self.append_log(
                f"Finished. Converted/planned={summary.converted}, skipped={summary.skipped}, failed={summary.failed}."
            )
            if isinstance(self._last_build_unknown_review_result, dict):
                unknown_total = int(self._last_build_unknown_review_result.get("unknown_total", 0) or 0)
                processed_unknowns = int(self._last_build_unknown_review_result.get("processed_unknowns", 0) or 0)
                preserved_unknowns = int(self._last_build_unknown_review_result.get("preserved_unknowns", 0) or 0)
                if unknown_total > 0:
                    self.append_log(
                        "Note: "
                        f"{unknown_total:,} matched DDS file(s) were still unclassified in this run. "
                        f"Current-policy estimate before start: {processed_unknowns:,} would be processed and {preserved_unknowns:,} would likely be left unchanged. "
                        "Open Research -> Classification Review if you want to review them."
                    )
            if summary.log_csv_path:
                self.append_log(f"CSV log saved to {summary.log_csv_path}")
            self.refresh_compare_list(select_current=True)
            self.main_tabs.setCurrentWidget(self.workflow_tab)
            self.content_tabs.setCurrentIndex(1)
            self._last_build_unknown_review_result = None

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
            self._last_build_unknown_review_result = None

        def _handle_utility_completed(self, result: object) -> None:
            if self._utility_completion_handler is not None:
                self._utility_completion_handler(result)

        def _handle_worker_error(self, message: str) -> None:
            _write_crash_report(
                "worker_error",
                "Background worker error",
                str(message),
                context=_collect_crash_context(),
            )
            self.set_status_message(message, error=True)
            self.append_log(f"ERROR: {message}")
            if self.archive_scan_worker is not None or self.archive_filter_worker is not None or self._utility_updates_archive_progress:
                self.append_archive_log(f"ERROR: {message}")
                self.archive_scan_progress_label.setText(f"Archive browser task failed: {message}")
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
                if abs(self.compare_preview_fit_scale - 1.0) < 0.01:
                    value_label.setText("Fit")
                else:
                    value_label.setText(f"Fit {int(round(self.compare_preview_fit_scale * 100))}%")
            else:
                value_label.setText(f"{int(round(zoom_factor * 100))}%")

        def _apply_compare_zoom(self, side: str) -> None:
            preview_label, fit_to_view, zoom_factor, _value_label = self._get_compare_zoom_state(side)
            preview_label.set_fit_scale(self.compare_preview_fit_scale)
            preview_label.set_fit_to_view(fit_to_view)
            preview_label.set_zoom_factor(zoom_factor)
            self._update_compare_zoom_label(side)

        def _parse_compare_preview_size_mode(self) -> float:
            raw_value = self._combo_value(self.compare_preview_size_combo).strip()
            if raw_value.startswith("fit:"):
                try:
                    return max(0.5, min(4.0, float(raw_value.split(":", 1)[1])))
                except ValueError:
                    return 1.25
            return 1.25

        def _apply_compare_preview_size_mode(self, *_args) -> None:
            self.compare_preview_fit_scale = self._parse_compare_preview_size_mode()
            self.original_compare_fit_to_view = True
            self.output_compare_fit_to_view = True
            self._apply_compare_zoom("original")
            self._apply_compare_zoom("output")
            self._sync_compare_scroll_positions()

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
            preview_label, fit_to_view, zoom_factor, _value_label = self._get_compare_zoom_state(side)
            current = zoom_factor if not fit_to_view else preview_label.current_display_scale()
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
            self.compare_mip_details_button.setEnabled(count > 0 and 0 <= current_row < count)
            self.compare_open_in_editor_button.setEnabled(count > 0 and 0 <= current_row < count)

        def _open_compare_in_texture_analysis(self) -> None:
            relative_path = self.current_compare_path_for_research().strip()
            if not relative_path:
                self.set_status_message("Select a DDS file in Compare first.", error=True)
                return
            self.main_tabs.setCurrentWidget(self.research_tab)
            self.research_tab.focus_texture_analysis_for_compare_path(relative_path, refresh_snapshot=True)

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
                self._compare_preview_timer.stop()
                self.pending_compare_preview_selection = None
                self.compare_preview_request_id += 1
                self.original_preview_meta_label.setText("")
                self.output_preview_meta_label.setText("")
                self.original_preview_label.clear_preview("Select a DDS file to preview.")
                self.output_preview_label.clear_preview("Select a DDS file to preview.")
                return

            relative_path = Path(current.data(Qt.UserRole))
            self.pending_compare_preview_selection = relative_path
            self._compare_preview_timer.start()

        def _flush_pending_compare_preview_selection(self) -> None:
            if self._shutting_down:
                self.pending_compare_preview_selection = None
                return
            relative_path = self.pending_compare_preview_selection
            self.pending_compare_preview_selection = None
            if relative_path is None:
                return
            self._render_compare_preview(relative_path)

        def current_compare_path_for_research(self) -> str:
            current_item = self.compare_list.currentItem()
            if current_item is None:
                return ""
            raw = current_item.data(Qt.UserRole)
            return str(raw) if raw else ""

        def _summarize_compare_planner(self, relative_path: Path) -> Tuple[str, str]:
            try:
                normalized = normalize_config_for_planning(self.collect_config())
            except Exception:
                return "", ""
            ui_warning = ""
            try:
                target_key = relative_path.as_posix().replace("\\", "/").strip("/").casefold()
                ui_rows = self.research_tab.research_payload.get("ui_constraint_rows", [])
                if isinstance(ui_rows, list):
                    for row in ui_rows:
                        related_path = getattr(row, "related_path", "")
                        if str(related_path or "").replace("\\", "/").strip("/").casefold() != target_key:
                            continue
                        ui_warning = str(getattr(row, "warning_text", "") or "")
                        if ui_warning:
                            break
            except Exception:
                ui_warning = ""
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()
            original_path = Path(original_root_text).expanduser() / relative_path if original_root_text else None
            output_path = Path(output_root_text).expanduser() / relative_path if output_root_text else None

            summaries: List[str] = []
            details: List[str] = []
            for label, path in (("Original", original_path), ("Output", output_path)):
                if path is None or not path.exists():
                    continue
                try:
                    entry = build_single_texture_processing_plan(
                        normalized,
                        path,
                        relative_path=relative_path,
                    )
                except Exception:
                    continue
                summary = f"{label}: {entry.action} | {entry.profile.key} | {entry.path_kind}"
                if entry.preserve_reason:
                    summary += f" | {entry.preserve_reason}"
                if ui_warning:
                    summary += f" | UI note: {ui_warning}"
                summaries.append(summary)
                details.append(summary)

            return " ; ".join(summaries), "\n".join(details)

        def _render_compare_preview(self, relative_path: Path) -> None:
            if self._shutting_down:
                return
            texconv_text = self.texconv_path_edit.text().strip()
            original_root_text = self.original_dds_edit.text().strip()
            output_root_text = self.output_root_edit.text().strip()

            texconv_path = Path(texconv_text).expanduser() if texconv_text else None
            original_path = Path(original_root_text).expanduser() / relative_path if original_root_text else None
            output_path = Path(output_root_text).expanduser() / relative_path if output_root_text else None
            original_planner_summary, output_planner_summary = self._summarize_compare_planner(relative_path)
            request_id = self.compare_preview_request_id + 1
            self.compare_preview_request_id = request_id

            self.original_preview_meta_label.setText("")
            self.output_preview_meta_label.setText("")
            self.original_preview_label.clear_preview("Loading preview...")
            self.output_preview_label.clear_preview("Loading preview...")

            if self.compare_preview_thread is not None:
                self.pending_compare_preview_request = (request_id, relative_path)
                if self.compare_preview_worker is not None:
                    self.compare_preview_worker.stop()
                return

            self._start_compare_preview_worker(
                request_id,
                texconv_path,
                original_path,
                output_path,
                original_planner_summary,
                output_planner_summary or original_planner_summary,
            )

        def _start_compare_preview_worker(
            self,
            request_id: int,
            texconv_path: Optional[Path],
            original_path: Optional[Path],
            output_path: Optional[Path],
            original_planner_summary: str = "",
            output_planner_summary: str = "",
        ) -> None:
            if self._shutting_down:
                return
            worker = ComparePreviewWorker(
                request_id,
                texconv_path,
                original_path,
                output_path,
                original_planner_summary,
                output_planner_summary,
            )
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
            if self._shutting_down or request_id != self.compare_preview_request_id:
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
            if self._shutting_down or request_id != self.compare_preview_request_id:
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

            preview_image_path = str(result.preview_png_path)
            preview_image = result.preview_image
            if preview_image is not None:
                meta_label.setText(result.metadata_summary)
                label.set_preview_image(preview_image, result.title)
                return
            if not preview_image_path:
                meta_label.setText("")
                label.clear_preview("Qt could not load the generated PNG preview.")
                return
            meta_label.setText(result.metadata_summary)
            label.set_preview_image_path(preview_image_path, result.title)

        def _cleanup_compare_preview_refs(self) -> None:
            self.compare_preview_thread = None
            self.compare_preview_worker = None
            if self._shutting_down:
                self.pending_compare_preview_request = None
                return
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
            original_planner_summary, output_planner_summary = self._summarize_compare_planner(relative_path)
            self._start_compare_preview_worker(
                request_id,
                texconv_path,
                original_path,
                output_path,
                original_planner_summary,
                output_planner_summary or original_planner_summary,
            )

        def _cleanup_worker_refs(self) -> None:
            rerun_archive_filter = bool(self.archive_filter_apply_pending and not self._shutting_down and self.archive_entries)
            archive_finalize_pending = bool(self.archive_scan_finalize_pending)
            utility_updates_archive_progress = bool(self._utility_updates_archive_progress)
            self.worker_thread = None
            self.scan_worker = None
            self.archive_scan_worker = None
            self.archive_filter_worker = None
            self.build_worker = None
            self.dds_to_png_worker = None
            self.utility_worker = None
            self._utility_completion_handler = None
            self._utility_updates_archive_progress = False
            self.archive_filter_apply_pending = False
            if not archive_finalize_pending:
                self.set_busy(False, build_mode=False)
            if utility_updates_archive_progress and self.archive_scan_worker is None and self.archive_filter_worker is None:
                self.archive_scan_progress_bar.setRange(0, 1)
                self.archive_scan_progress_bar.setValue(1)
                self.archive_scan_progress_bar.setFormat("Ready")
            if rerun_archive_filter:
                QTimer.singleShot(0, self._apply_archive_filter)

        def closeEvent(self, event) -> None:  # type: ignore[override]
            def _stop_thread(thread, worker=None, *, wait_ms: int = 1200) -> None:
                if worker is not None:
                    try:
                        worker.stop()
                    except Exception:
                        pass
                if thread is None:
                    return
                try:
                    if thread.isRunning():
                        thread.quit()
                        thread.wait(wait_ms)
                except Exception:
                    pass

            self._shutting_down = True
            nonlocal _active_main_window
            _active_main_window = None
            self._cancel_archive_tree_population()
            self._settings_save_timer.stop()
            self._chainner_analysis_timer.stop()
            self._compare_preview_timer.stop()
            self.archive_preview_debounce_timer.stop()
            self.archive_preview_loading_timer.stop()
            self.archive_selection_state_timer.stop()
            self.archive_media_preview.shutdown()
            self.pending_compare_preview_selection = None
            self.pending_compare_preview_request = None
            self.pending_archive_preview_request = None
            self.scheduled_archive_preview_request = None
            self.compare_preview_request_id += 1
            self.archive_preview_request_id += 1
            self.settings.setValue("window/geometry", self.saveGeometry())
            self.flush_settings_save()
            if self.scan_worker is not None:
                self.scan_worker.stop()
            if self.archive_scan_worker is not None:
                self.archive_scan_worker.stop()
            if self.archive_filter_worker is not None:
                self.archive_filter_worker.stop()
            if self.build_worker is not None:
                self.build_worker.stop()
            if self.dds_to_png_worker is not None:
                self.dds_to_png_worker.stop()
            self.settings_tab.flush_settings_save()
            self.replace_assistant_tab.flush_settings_save()
            self.texture_editor_tab.flush_settings_save()
            self.text_search_tab.shutdown()
            self.research_tab.shutdown()
            self.replace_assistant_tab.shutdown()
            self.texture_editor_tab.shutdown()
            _stop_thread(self.worker_thread)
            _stop_thread(self.compare_preview_thread, self.compare_preview_worker)
            _stop_thread(self.archive_preview_thread, self.archive_preview_worker)
            super().closeEvent(event)

    apply_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setOrganizationName(APP_ORGANIZATION)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    ensure_app_wheel_guard(app)
    icon_path = resolve_app_icon_path()
    if icon_path is not None:
        app_icon = QIcon(str(icon_path))
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)
    startup_settings = create_settings()
    startup_theme = str(startup_settings.value("appearance/theme", DEFAULT_UI_THEME))
    apply_app_theme(app, startup_settings, startup_theme)

    window = MainWindow()
    if not app.windowIcon().isNull():
        window.setWindowIcon(app.windowIcon())
    apply_window_data_fonts(window)
    window.show()
    return app.exec()

__all__ = ["run_gui"]
