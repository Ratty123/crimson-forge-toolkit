from __future__ import annotations

import re
import threading
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QProgressBar,
    QPushButton,
    QSplitter,
    QSizePolicy,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cdmw.constants import APP_TITLE
from cdmw.core.text_search import (
    DEFAULT_TEXT_SEARCH_EXTENSIONS,
    TextSearchPreview,
    TextSearchResult,
    TextSearchRunStats,
    export_text_search_results,
    load_text_search_preview,
    normalize_text_search_extensions,
    search_archive_text_entries,
    search_loose_text_files,
)
from cdmw.models import ArchiveEntry, RunCancelled
from cdmw.ui.themes import get_theme
from cdmw.ui.widgets import (
    CodePreviewEditor,
    FlatSectionPanel,
    LogHighlighter,
    build_responsive_splitter_sizes,
    clamp_splitter_sizes,
)


def _shutdown_thread(thread: Optional[QThread], *, grace_ms: int = 2000, force_ms: int = 2000) -> None:
    if thread is None:
        return
    thread.quit()
    if thread.wait(grace_ms):
        return
    thread.wait(force_ms)


class TextSearchWorker(QObject):
    log_message = Signal(str)
    progress_changed = Signal(int, int, str)
    completed = Signal(object)
    cancelled = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        source_kind: str,
        query: str,
        extension_text: str,
        path_filter: str,
        case_sensitive: bool,
        regex_enabled: bool,
        archive_entries: Sequence[ArchiveEntry],
        loose_root: Optional[Path],
    ) -> None:
        super().__init__()
        self.source_kind = source_kind
        self.query = query
        self.extension_text = extension_text
        self.path_filter = path_filter
        self.case_sensitive = case_sensitive
        self.regex_enabled = regex_enabled
        self.archive_entries = archive_entries if isinstance(archive_entries, list) else list(archive_entries)
        self.loose_root = loose_root
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            extension_filters = normalize_text_search_extensions(self.extension_text)
            if self.source_kind == "archive":
                results, stats = search_archive_text_entries(
                    self.archive_entries,
                    self.query,
                    extension_filters=extension_filters,
                    path_filter=self.path_filter,
                    regex=self.regex_enabled,
                    case_sensitive=self.case_sensitive,
                    on_progress=self.progress_changed.emit,
                    on_log=self.log_message.emit,
                    stop_event=self.stop_event,
                )
            else:
                if self.loose_root is None:
                    raise ValueError("Select a loose root folder before searching loose files.")
                results, stats = search_loose_text_files(
                    self.loose_root,
                    self.query,
                    extension_filters=extension_filters,
                    path_filter=self.path_filter,
                    regex=self.regex_enabled,
                    case_sensitive=self.case_sensitive,
                    on_progress=self.progress_changed.emit,
                    on_log=self.log_message.emit,
                    stop_event=self.stop_event,
                )
            self.completed.emit(
                {
                    "results": results,
                    "stats": stats,
                    "source_kind": self.source_kind,
                }
            )
        except RunCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class TextSearchPreviewWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(
        self,
        *,
        request_id: int,
        result: TextSearchResult,
        query: str,
        regex_enabled: bool,
        case_sensitive: bool,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.result = result
        self.query = query
        self.regex_enabled = regex_enabled
        self.case_sensitive = case_sensitive
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            preview = load_text_search_preview(
                self.result,
                self.query,
                regex=self.regex_enabled,
                case_sensitive=self.case_sensitive,
                stop_event=self.stop_event,
            )
            if self.stop_event.is_set():
                return
            self.completed.emit(self.request_id, preview)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class TextSearchTab(QWidget):
    status_message_requested = Signal(str, bool)
    SYNTAX_HIGHLIGHT_CHAR_LIMIT = 2_000_000
    AUTO_PREVIEW_RESULT_LIMIT = 4000
    RESULT_POPULATION_BATCH_SIZE = 300

    def __init__(
        self,
        *,
        settings,
        base_dir: Path,
        theme_key: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.base_dir = base_dir
        self.archive_entries: List[ArchiveEntry] = []
        self.archive_package_root_text = ""
        self.external_busy = False
        self._settings_ready = False
        self.current_theme_key = theme_key
        self.search_thread: Optional[QThread] = None
        self.search_worker: Optional[TextSearchWorker] = None
        self.preview_thread: Optional[QThread] = None
        self.preview_worker: Optional[TextSearchPreviewWorker] = None
        self.preview_request_id = 0
        self.pending_preview_result: Optional[TextSearchResult] = None
        self.scheduled_preview_result: Optional[TextSearchResult] = None
        self.search_results: List[TextSearchResult] = []
        self.current_preview_result: Optional[TextSearchResult] = None
        self._pending_result_indexes: List[int] = []
        self._pending_result_total = 0
        self._pending_auto_preview_enabled = False
        self.last_search_query = ""
        self.last_search_case_sensitive = False
        self.last_search_regex_enabled = False
        self.last_search_stats = TextSearchRunStats(source_kind="archive", candidate_count=0, searched_count=0)
        self.preview_search_spans: List[tuple[int, int]] = []
        self.preview_find_spans: List[tuple[int, int]] = []
        self.preview_find_active_index = -1
        self.preview_text_cache = ""
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(250)
        self._settings_save_timer.timeout.connect(self._save_settings)
        self._preview_debounce_timer = QTimer(self)
        self._preview_debounce_timer.setSingleShot(True)
        self._preview_debounce_timer.setInterval(90)
        self._preview_debounce_timer.timeout.connect(self._flush_scheduled_preview_request)
        self._results_population_timer = QTimer(self)
        self._results_population_timer.setSingleShot(True)
        self._results_population_timer.setInterval(0)
        self._results_population_timer.timeout.connect(self._flush_result_population_batch)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(8)
        root_layout.addWidget(self.main_splitter, stretch=1)

        controls_group = FlatSectionPanel("Text Search")
        controls_layout = controls_group.body_layout
        controls_layout.setSpacing(8)

        summary_label = QLabel(
            "Read-only search across archive or loose text-like files. Search for strings or regex patterns, preview "
            "the matched file with highlights, and export matches while preserving folder structure."
        )
        summary_label.setWordWrap(True)
        summary_label.setObjectName("HintLabel")
        controls_layout.addWidget(summary_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        self.source_combo = QComboBox()
        self.source_combo.addItem("Archive files", "archive")
        self.source_combo.addItem("Loose folder", "loose")

        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Search string or regex, e.g. material or <Texture")
        self.path_filter_edit = QLineEdit()
        self.path_filter_edit.setPlaceholderText("Optional path filter, e.g. object/ or *.xml naming fragment")
        self.extensions_edit = QLineEdit(DEFAULT_TEXT_SEARCH_EXTENSIONS)
        self.extensions_edit.setPlaceholderText(".xml;.txt;.json")
        self.case_sensitive_checkbox = QCheckBox("Case sensitive")
        self.regex_checkbox = QCheckBox("Regex")

        self.loose_root_edit = QLineEdit()
        self.loose_root_edit.setPlaceholderText("Loose root folder for non-archive text search")
        self.loose_root_browse_button = QPushButton("Browse")

        self.export_root_edit = QLineEdit(str((base_dir / "text_search_export").resolve()))
        self.export_root_browse_button = QPushButton("Browse")

        grid.addWidget(QLabel("Source"), 0, 0)
        grid.addWidget(self.source_combo, 0, 1)
        grid.addWidget(QLabel("Extensions"), 0, 2)
        grid.addWidget(self.extensions_edit, 0, 3)

        grid.addWidget(QLabel("Search"), 1, 0)
        grid.addWidget(self.query_edit, 1, 1, 1, 3)

        grid.addWidget(QLabel("Path filter"), 2, 0)
        grid.addWidget(self.path_filter_edit, 2, 1, 1, 3)

        self.loose_root_label = QLabel("Loose root")
        grid.addWidget(self.loose_root_label, 3, 0)
        grid.addWidget(self.loose_root_edit, 3, 1, 1, 2)
        grid.addWidget(self.loose_root_browse_button, 3, 3)

        grid.addWidget(QLabel("Export root"), 4, 0)
        grid.addWidget(self.export_root_edit, 4, 1, 1, 2)
        grid.addWidget(self.export_root_browse_button, 4, 3)

        option_row = QHBoxLayout()
        option_row.setSpacing(8)
        option_row.addWidget(self.case_sensitive_checkbox)
        option_row.addWidget(self.regex_checkbox)
        option_row.addStretch(1)
        grid.addLayout(option_row, 5, 1, 1, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 1)
        controls_layout.addLayout(grid)

        button_row = QGridLayout()
        button_row.setHorizontalSpacing(8)
        button_row.setVerticalSpacing(8)
        self.search_button = QPushButton("Search")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.export_selected_button = QPushButton("Export Selected")
        self.export_all_button = QPushButton("Export Results")
        self.clear_log_button = QPushButton("Clear Log")
        for button in (
            self.search_button,
            self.stop_button,
            self.export_selected_button,
            self.export_all_button,
            self.clear_log_button,
        ):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button_row.addWidget(self.search_button, 0, 0)
        button_row.addWidget(self.stop_button, 0, 1)
        button_row.addWidget(self.export_selected_button, 0, 2)
        button_row.addWidget(self.export_all_button, 1, 0, 1, 2)
        button_row.addWidget(self.clear_log_button, 1, 2)
        controls_layout.addLayout(button_row)

        self.results_summary_label = QLabel("No text search has been run yet.")
        self.results_summary_label.setObjectName("HintLabel")
        self.search_progress_label = QLabel("Ready.")
        self.search_progress_label.setObjectName("HintLabel")
        self.search_progress_bar = QProgressBar()
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(0)
        self.search_progress_bar.setFormat("Ready")
        controls_layout.addWidget(self.results_summary_label)
        controls_layout.addWidget(self.search_progress_label)
        controls_layout.addWidget(self.search_progress_bar)
        controls_layout.addSpacing(8)
        log_group = FlatSectionPanel("Search Log")
        log_layout = log_group.body_layout
        log_layout.setSpacing(8)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_view)
        controls_layout.addWidget(log_group, stretch=1)
        self.main_splitter.addWidget(controls_group)

        results_group = FlatSectionPanel("Results")
        results_layout = results_group.body_layout
        results_layout.setSpacing(8)
        self.results_tree = QTreeWidget()
        self.results_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)
        self.results_tree.setHeaderLabels(["File Name", "Matches", "Package", "Path", "Ext"])
        self.results_tree.header().setStretchLastSection(False)
        self.results_tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.results_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.results_tree.header().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.results_tree.header().resizeSection(0, 260)
        self.results_tree.header().resizeSection(3, 360)
        results_layout.addWidget(self.results_tree, stretch=1)
        self.main_splitter.addWidget(results_group)

        preview_group = FlatSectionPanel("Preview")
        preview_layout = preview_group.body_layout
        preview_layout.setSpacing(8)
        self.preview_title_label = QLabel("Select a matching file")
        self.preview_title_label.setWordWrap(True)
        self.preview_meta_label = QLabel("Matched files will be previewed here with highlights.")
        self.preview_meta_label.setObjectName("HintLabel")
        self.preview_meta_label.setWordWrap(True)
        self.preview_detail_label = QLabel("")
        self.preview_detail_label.setObjectName("HintLabel")
        self.preview_detail_label.setWordWrap(True)
        preview_toolbar = QVBoxLayout()
        preview_toolbar.setSpacing(6)
        preview_search_row = QHBoxLayout()
        preview_search_row.setSpacing(8)
        preview_options_row = QHBoxLayout()
        preview_options_row.setSpacing(8)
        self.preview_find_edit = QLineEdit()
        self.preview_find_edit.setPlaceholderText("Find in preview")
        self.preview_find_prev_button = QPushButton("Prev")
        self.preview_find_next_button = QPushButton("Next")
        self.preview_find_case_checkbox = QCheckBox("Aa")
        self.preview_find_case_checkbox.setToolTip("Case-sensitive preview search")
        self.preview_wrap_checkbox = QCheckBox("Wrap")
        self.preview_wrap_checkbox.setToolTip("Wrap long lines in the preview editor")
        self.preview_font_smaller_button = QPushButton("A-")
        self.preview_font_larger_button = QPushButton("A+")
        self.preview_find_status_label = QLabel("No preview loaded.")
        self.preview_find_status_label.setObjectName("HintLabel")
        self.preview_find_status_label.setWordWrap(True)
        for button in (
            self.preview_find_prev_button,
            self.preview_find_next_button,
            self.preview_font_smaller_button,
            self.preview_font_larger_button,
        ):
            button.setMinimumWidth(42)
        preview_search_row.addWidget(self.preview_find_edit, stretch=1)
        preview_search_row.addWidget(self.preview_find_prev_button)
        preview_search_row.addWidget(self.preview_find_next_button)
        preview_options_row.addWidget(self.preview_find_case_checkbox)
        preview_options_row.addWidget(self.preview_wrap_checkbox)
        preview_options_row.addWidget(self.preview_font_smaller_button)
        preview_options_row.addWidget(self.preview_font_larger_button)
        preview_options_row.addWidget(self.preview_find_status_label, stretch=1)
        preview_toolbar.addLayout(preview_search_row)
        preview_toolbar.addLayout(preview_options_row)
        self.preview_text_edit = CodePreviewEditor(theme_key=theme_key)
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_meta_label)
        preview_layout.addWidget(self.preview_detail_label)
        preview_layout.addLayout(preview_toolbar)
        preview_layout.addWidget(self.preview_text_edit, stretch=1)
        self.main_splitter.addWidget(preview_group)
        controls_group.setMinimumWidth(300)
        results_group.setMinimumWidth(260)
        preview_group.setMinimumWidth(360)
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setStretchFactor(2, 4)
        self.main_splitter.setSizes(build_responsive_splitter_sizes(1670, [26, 22, 52], [300, 260, 360]))

        self.log_highlighter = LogHighlighter(self.log_view.document(), theme_key)
        log_font = QFont("Consolas")
        if not log_font.exactMatch():
            log_font = QFont("Courier New")
        inherited_size = self.font().pointSize()
        if inherited_size > 0:
            log_font.setPointSize(inherited_size)
        self.log_view.setFont(log_font)

        self.loose_root_browse_button.clicked.connect(self._browse_loose_root)
        self.export_root_browse_button.clicked.connect(self._browse_export_root)
        self.search_button.clicked.connect(self.start_search)
        self.stop_button.clicked.connect(self.stop_search)
        self.export_selected_button.clicked.connect(self.export_selected_results)
        self.export_all_button.clicked.connect(self.export_all_results)
        self.clear_log_button.clicked.connect(self.clear_log)
        self.results_tree.currentItemChanged.connect(self._handle_result_selection_changed)
        self.query_edit.returnPressed.connect(self.start_search)
        self.path_filter_edit.returnPressed.connect(self.start_search)
        self.source_combo.currentIndexChanged.connect(self._handle_source_changed)
        self.preview_find_edit.textChanged.connect(self._handle_preview_find_changed)
        self.preview_find_edit.returnPressed.connect(self._jump_to_next_preview_find_match)
        self.preview_find_prev_button.clicked.connect(self._jump_to_previous_preview_find_match)
        self.preview_find_next_button.clicked.connect(self._jump_to_next_preview_find_match)
        self.preview_find_case_checkbox.toggled.connect(self._handle_preview_find_changed)
        self.preview_wrap_checkbox.toggled.connect(self._handle_preview_wrap_changed)
        self.preview_font_smaller_button.clicked.connect(lambda: self._adjust_preview_font(-1))
        self.preview_font_larger_button.clicked.connect(lambda: self._adjust_preview_font(1))

        for widget in (
            self.query_edit,
            self.path_filter_edit,
            self.extensions_edit,
            self.loose_root_edit,
            self.export_root_edit,
            self.preview_find_edit,
        ):
            widget.textChanged.connect(self.schedule_settings_save)
        self.source_combo.currentIndexChanged.connect(self.schedule_settings_save)
        self.case_sensitive_checkbox.toggled.connect(self.schedule_settings_save)
        self.regex_checkbox.toggled.connect(self.schedule_settings_save)
        self.preview_wrap_checkbox.toggled.connect(self.schedule_settings_save)
        self.preview_find_case_checkbox.toggled.connect(self.schedule_settings_save)

        self._load_settings()
        self._settings_ready = True
        self._apply_source_state()
        self._update_controls()

    def set_theme(self, theme_key: str) -> None:
        self.current_theme_key = theme_key
        self.log_highlighter.set_theme(theme_key)
        self.preview_text_edit.set_theme(theme_key)
        self._refresh_preview_selections(focus_current=False)

    def set_splitter_sizes(self, sizes: Sequence[int]) -> None:
        if sizes:
            total_width = max(self.width() - 32, sum([300, 260, 360]))
            self.main_splitter.setSizes(
                clamp_splitter_sizes(total_width, sizes, [300, 260, 360], fallback_weights=[26, 22, 52])
            )

    def splitter_sizes(self) -> List[int]:
        return self.main_splitter.sizes()

    def apply_responsive_splitter_sizes(self, total_width: Optional[int] = None) -> None:
        available_width = total_width or max(self.width() - 32, sum([300, 260, 360]))
        self.main_splitter.setSizes(
            build_responsive_splitter_sizes(available_width, [26, 22, 52], [300, 260, 360])
        )

    def auto_fit_columns(self) -> None:
        header = self.results_tree.header()
        if header is None or self.results_tree.columnCount() <= 0:
            return
        viewport_width = max(self.results_tree.viewport().width(), self.results_tree.width() - 24, 0)
        if viewport_width <= 0:
            return
        minimums = {
            0: 220,
            1: 74,
            2: 96,
            3: 280,
            4: 60,
        }
        self.results_tree.setUpdatesEnabled(False)
        try:
            for column in (1, 2, 4):
                self.results_tree.resizeColumnToContents(column)
                header.resizeSection(column, max(minimums[column], header.sectionSize(column)))
            reserved = sum(header.sectionSize(column) for column in (1, 2, 4))
            remaining = max(0, viewport_width - reserved - 12)
            file_width = max(minimums[0], int(remaining * 0.38))
            path_width = max(minimums[3], remaining - file_width)
            header.resizeSection(0, file_width)
            header.resizeSection(3, path_width)
        finally:
            self.results_tree.setUpdatesEnabled(True)

    def set_external_busy(self, busy: bool) -> None:
        self.external_busy = busy
        self._update_controls()

    def is_busy(self) -> bool:
        return self.search_thread is not None

    def set_archive_entries(self, entries: Sequence[ArchiveEntry], package_root_text: str = "") -> None:
        self.archive_entries = entries if isinstance(entries, list) else list(entries)
        self.archive_package_root_text = package_root_text.strip()
        if self.source_combo.currentData() == "archive" and not self.search_results:
            self.results_summary_label.setText(
                f"Archive source ready: {len(self.archive_entries):,} scanned entry(s) available for text search."
            )

    def review_archive_entry(
        self,
        entry: ArchiveEntry,
        *,
        highlight_query: str,
    ) -> bool:
        query = highlight_query.strip()
        if not query:
            self.status_message_requested.emit("No highlight query was provided for the selected reference.", True)
            return False
        if self.search_thread is not None:
            self.status_message_requested.emit("Text Search is busy. Wait for the current search to finish first.", True)
            return False

        self._preview_debounce_timer.stop()
        self.pending_preview_result = None
        self.scheduled_preview_result = None
        self.preview_request_id += 1
        if self.preview_worker is not None:
            self.preview_worker.stop()

        archive_index = self.source_combo.findData("archive")
        if archive_index >= 0:
            self.source_combo.setCurrentIndex(archive_index)
        self.query_edit.setText(query)

        result = TextSearchResult(
            source_kind="archive",
            relative_path=entry.path.replace("\\", "/"),
            extension=entry.extension,
            match_count=1,
            snippet="Opened from Research -> References for targeted XML/material review.",
            package_label=entry.package_label,
            archive_entry=entry,
        )
        self.search_results = [result]
        self.current_preview_result = result
        self.last_search_query = query
        self.last_search_case_sensitive = False
        self.last_search_regex_enabled = False
        self.last_search_stats = TextSearchRunStats(source_kind="archive", candidate_count=1, searched_count=1)

        self.results_tree.blockSignals(True)
        self.results_tree.clear()
        item = self._build_result_item(0, result)
        self.results_tree.addTopLevelItem(item)
        self.results_tree.blockSignals(False)
        self.results_tree.setCurrentItem(item)

        file_name = PurePosixPath(result.relative_path).name or result.relative_path
        self.results_summary_label.setText("Opened 1 archive text file from Research for focused review.")
        self.search_progress_label.setText("Reference review ready.")
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(1)
        self.search_progress_bar.setFormat("Ready")
        self.append_log(f"Opened {result.relative_path} in Text Search for reference review (highlight: {query}).")
        self.status_message_requested.emit(f"Opened {file_name} in Text Search and highlighted '{query}'.", False)
        self._update_controls()
        self._schedule_preview(result)
        return True

    def diagnostic_entries(self) -> Dict[str, str]:
        return {
            "text_search_log.txt": self.log_view.toPlainText(),
        }

    def shutdown(self) -> None:
        self.flush_settings_save()
        self._preview_debounce_timer.stop()
        self._clear_pending_result_population()
        if self.search_worker is not None:
            self.search_worker.stop()
        if self.preview_worker is not None:
            self.preview_worker.stop()
        _shutdown_thread(self.search_thread)
        _shutdown_thread(self.preview_thread)

    def clear_log(self) -> None:
        self.log_view.clear()
        self.search_progress_label.setText("Search log cleared.")
        self.status_message_requested.emit("Text search log cleared.", False)

    def append_log(self, message: str) -> None:
        from time import strftime

        self.log_view.appendPlainText(f"[{strftime('%H:%M:%S')}] {message}")
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _browse_loose_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Loose Root", self.loose_root_edit.text() or str(self.base_dir))
        if selected:
            self.loose_root_edit.setText(selected)

    def _browse_export_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Export Root",
            self.export_root_edit.text() or str(self.base_dir),
        )
        if selected:
            self.export_root_edit.setText(selected)

    def _handle_source_changed(self) -> None:
        self._apply_source_state()
        self.schedule_settings_save()

    def _apply_source_state(self) -> None:
        loose_mode = self.source_combo.currentData() == "loose"
        self.loose_root_label.setVisible(loose_mode)
        self.loose_root_edit.setVisible(loose_mode)
        self.loose_root_browse_button.setVisible(loose_mode)

    def _save_settings(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("text_search/source_kind", str(self.source_combo.currentData()))
        self.settings.setValue("text_search/query", self.query_edit.text())
        self.settings.setValue("text_search/path_filter", self.path_filter_edit.text())
        self.settings.setValue("text_search/extensions", self.extensions_edit.text())
        self.settings.setValue("text_search/loose_root", self.loose_root_edit.text())
        self.settings.setValue("text_search/export_root", self.export_root_edit.text())
        self.settings.setValue("text_search/case_sensitive", self.case_sensitive_checkbox.isChecked())
        self.settings.setValue("text_search/regex_enabled", self.regex_checkbox.isChecked())
        self.settings.setValue("text_search/preview_wrap", self.preview_wrap_checkbox.isChecked())
        self.settings.setValue("text_search/preview_find_case_sensitive", self.preview_find_case_checkbox.isChecked())
        self.settings.setValue("text_search/preview_font_size", self.preview_text_edit.font().pointSize())
        self.settings.sync()

    def schedule_settings_save(self, *_args) -> None:
        if not self._settings_ready:
            return
        self._settings_save_timer.start()

    def flush_settings_save(self) -> None:
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
        self._save_settings()

    def _load_settings(self) -> None:
        self._settings_ready = False
        source_kind = str(self.settings.value("text_search/source_kind", "archive"))
        index = self.source_combo.findData(source_kind)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        self.query_edit.setText(str(self.settings.value("text_search/query", "")))
        self.path_filter_edit.setText(str(self.settings.value("text_search/path_filter", "")))
        self.extensions_edit.setText(str(self.settings.value("text_search/extensions", DEFAULT_TEXT_SEARCH_EXTENSIONS)))
        self.loose_root_edit.setText(str(self.settings.value("text_search/loose_root", "")))
        self.export_root_edit.setText(
            str(self.settings.value("text_search/export_root", str((self.base_dir / "text_search_export").resolve())))
        )
        self.case_sensitive_checkbox.setChecked(str(self.settings.value("text_search/case_sensitive", "false")).lower() in {"1", "true", "yes"})
        self.regex_checkbox.setChecked(str(self.settings.value("text_search/regex_enabled", "false")).lower() in {"1", "true", "yes"})
        self.preview_wrap_checkbox.setChecked(str(self.settings.value("text_search/preview_wrap", "false")).lower() in {"1", "true", "yes"})
        self.preview_find_case_checkbox.setChecked(
            str(self.settings.value("text_search/preview_find_case_sensitive", "false")).lower() in {"1", "true", "yes"}
        )
        try:
            preview_font_size = int(self.settings.value("text_search/preview_font_size", 10))
        except (TypeError, ValueError):
            preview_font_size = 10
        self.preview_text_edit.set_font_size(preview_font_size)

    def _update_controls(self) -> None:
        busy = self.search_thread is not None
        can_interact = not busy and not self.external_busy
        self.source_combo.setEnabled(can_interact)
        self.query_edit.setEnabled(can_interact)
        self.path_filter_edit.setEnabled(can_interact)
        self.extensions_edit.setEnabled(can_interact)
        self.loose_root_edit.setEnabled(can_interact and self.source_combo.currentData() == "loose")
        self.loose_root_browse_button.setEnabled(can_interact and self.source_combo.currentData() == "loose")
        self.export_root_edit.setEnabled(can_interact)
        self.export_root_browse_button.setEnabled(can_interact)
        self.case_sensitive_checkbox.setEnabled(can_interact)
        self.regex_checkbox.setEnabled(can_interact)
        self.search_button.setEnabled(can_interact)
        self.stop_button.setEnabled(busy)
        has_results = bool(self.search_results)
        has_selection = bool(self.selected_results())
        self.export_selected_button.setEnabled(can_interact and has_selection)
        self.export_all_button.setEnabled(can_interact and has_results)
        self.results_tree.setEnabled(not busy)
        self.clear_log_button.setEnabled(not busy)
        has_preview_text = bool(self.preview_text_cache)
        self.preview_find_edit.setEnabled(has_preview_text)
        self.preview_find_prev_button.setEnabled(has_preview_text and bool(self.preview_find_spans))
        self.preview_find_next_button.setEnabled(has_preview_text and bool(self.preview_find_spans))
        self.preview_find_case_checkbox.setEnabled(has_preview_text)
        self.preview_wrap_checkbox.setEnabled(has_preview_text)
        self.preview_font_smaller_button.setEnabled(has_preview_text)
        self.preview_font_larger_button.setEnabled(has_preview_text)

    def selected_results(self) -> List[TextSearchResult]:
        results: List[TextSearchResult] = []
        for item in self.results_tree.selectedItems():
            raw = item.data(0, Qt.UserRole)
            if isinstance(raw, int) and 0 <= raw < len(self.search_results):
                results.append(self.search_results[raw])
        return results

    def current_result(self) -> Optional[TextSearchResult]:
        item = self.results_tree.currentItem()
        if item is None:
            return None
        raw = item.data(0, Qt.UserRole)
        if isinstance(raw, int) and 0 <= raw < len(self.search_results):
            return self.search_results[raw]
        return None

    def current_result_path(self) -> str:
        result = self.current_result()
        return result.relative_path if result is not None else ""

    def current_results(self) -> List[TextSearchResult]:
        return list(self.search_results)

    def apply_regex_preset(self, pattern: str, extensions_text: str = "", path_hint: str = "") -> None:
        self.regex_checkbox.setChecked(True)
        self.query_edit.setText(pattern)
        if extensions_text.strip():
            self.extensions_edit.setText(extensions_text.strip())
        if path_hint.strip():
            self.path_filter_edit.setText(path_hint.strip())
        self.source_combo.setCurrentIndex(max(0, self.source_combo.findData("archive")))
        self.flush_settings_save()
        self.status_message_requested.emit("Regex preset applied to Text Search.", False)

    def start_search(self) -> None:
        if self.external_busy or self.search_thread is not None:
            return
        self._preview_debounce_timer.stop()
        self.pending_preview_result = None
        self.scheduled_preview_result = None
        self.preview_request_id += 1
        if self.preview_worker is not None:
            self.preview_worker.stop()

        query = self.query_edit.text().strip()
        source_kind = str(self.source_combo.currentData())
        if not self.regex_checkbox.isChecked() and query in {".", "*", "?"}:
            self.append_log(
                f"Note: Regex is off, so '{query}' is treated as a literal character. Enable Regex for wildcard-style matching."
            )
        loose_root = None
        if source_kind == "archive":
            if not self.archive_entries:
                message = "Scan archives first, or switch the source to a loose folder."
                self.status_message_requested.emit(message, True)
                self.append_log(f"ERROR: {message}")
                return
        else:
            loose_root_text = self.loose_root_edit.text().strip()
            if not loose_root_text:
                message = "Select a loose root folder before searching loose files."
                self.status_message_requested.emit(message, True)
                self.append_log(f"ERROR: {message}")
                return
            loose_root = Path(loose_root_text).expanduser()
            if not loose_root.exists() or not loose_root.is_dir():
                message = f"Loose root does not exist or is not a folder: {loose_root}"
                self.status_message_requested.emit(message, True)
                self.append_log(f"ERROR: {message}")
                return

        self.search_results = []
        self._clear_pending_result_population()
        self.results_tree.clear()
        self.current_preview_result = None
        self.last_search_stats = TextSearchRunStats(source_kind=source_kind, candidate_count=0, searched_count=0)
        self.last_search_query = query
        self.last_search_case_sensitive = self.case_sensitive_checkbox.isChecked()
        self.last_search_regex_enabled = self.regex_checkbox.isChecked()
        self.preview_title_label.setText("Searching...")
        self.preview_meta_label.setText("Working...")
        self.preview_detail_label.setText("")
        self.preview_text_edit.setPlainText("")
        self.preview_text_edit.set_match_selections([])
        self.preview_search_spans = []
        self.preview_find_spans = []
        self.preview_find_active_index = -1
        self.preview_text_cache = ""
        self.preview_find_status_label.setText("Searching...")
        self.results_summary_label.setText("Search in progress...")
        self.search_progress_label.setText("Preparing search...")
        self.search_progress_bar.setRange(0, 0)
        self.search_progress_bar.setFormat("Working...")

        worker = TextSearchWorker(
            source_kind=source_kind,
            query=query,
            extension_text=self.extensions_edit.text().strip(),
            path_filter=self.path_filter_edit.text().strip(),
            case_sensitive=self.case_sensitive_checkbox.isChecked(),
            regex_enabled=self.regex_checkbox.isChecked(),
            archive_entries=self.archive_entries,
            loose_root=loose_root,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log_message.connect(self.append_log)
        worker.progress_changed.connect(self._handle_progress)
        worker.completed.connect(self._handle_search_complete)
        worker.cancelled.connect(self._handle_search_cancelled)
        worker.error.connect(self._handle_search_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_search_refs)
        self.search_worker = worker
        self.search_thread = thread
        self._update_controls()
        self.append_log(f"Starting text search in {'archive entries' if source_kind == 'archive' else 'loose files'}.")
        self.status_message_requested.emit("Starting text search...", False)
        thread.start()

    def stop_search(self) -> None:
        if self.search_worker is not None:
            self.search_worker.stop()

    def _clear_pending_result_population(self) -> None:
        self._results_population_timer.stop()
        self._pending_result_indexes = []
        self._pending_result_total = 0
        self._pending_auto_preview_enabled = False

    def _handle_progress(self, current: int, total: int, detail: str) -> None:
        self.search_progress_label.setText(detail)
        if total > 0:
            self.search_progress_bar.setRange(0, total)
            self.search_progress_bar.setValue(min(max(current, 0), total))
            display_value = min(max(current, 0), total)
            self.search_progress_bar.setFormat(f"{display_value} / {total}")
        else:
            self.search_progress_bar.setRange(0, 0)
            self.search_progress_bar.setFormat("Working...")
        self.status_message_requested.emit(detail, False)

    def _handle_search_complete(self, payload: object) -> None:
        self._clear_pending_result_population()
        data = payload if isinstance(payload, dict) else {}
        self.search_results = data.get("results", []) if isinstance(data.get("results"), list) else []
        stats = data.get("stats")
        self.last_search_stats = stats if isinstance(stats, TextSearchRunStats) else TextSearchRunStats(source_kind="archive", candidate_count=0, searched_count=0)
        auto_preview_enabled = len(self.search_results) <= self.AUTO_PREVIEW_RESULT_LIMIT
        self.results_tree.clear()
        summary = (
            f"Scanned {self.last_search_stats.candidate_count:,} candidate file(s). "
            f"Searched {self.last_search_stats.searched_count:,} readable file(s). "
            f"Found {len(self.search_results):,} matching file(s)."
        )
        if self.last_search_stats.decrypted_count:
            summary += f" Decrypted {self.last_search_stats.decrypted_count:,} archive file(s) during search."
        if self.last_search_stats.skipped_read_error_count:
            summary += f" {self.last_search_stats.skipped_read_error_count:,} file(s) could not be read."
        if self.search_results and not auto_preview_enabled:
            summary += " Auto-preview was skipped because the result set is very large."
        self.results_summary_label.setText(summary)
        self._pending_result_indexes = list(range(len(self.search_results)))
        self._pending_result_total = len(self._pending_result_indexes)
        self._pending_auto_preview_enabled = auto_preview_enabled
        if self._pending_result_total:
            self.search_progress_label.setText(f"Populating results... 0 / {self._pending_result_total}")
            self.search_progress_bar.setRange(0, self._pending_result_total)
            self.search_progress_bar.setValue(0)
            self.search_progress_bar.setFormat(f"0 / {self._pending_result_total}")
            self._results_population_timer.start()
        else:
            self._finalize_result_population()
        self.append_log(summary)
        self.status_message_requested.emit(summary, False)

    def _finalize_result_population(self) -> None:
        if self.search_results and self._pending_auto_preview_enabled:
            first_item = self.results_tree.topLevelItem(0)
            if first_item is not None:
                self.results_tree.setCurrentItem(first_item)
        else:
            if self.search_results:
                self.preview_title_label.setText("Large result set")
                self.preview_meta_label.setText("Select a file to preview. Auto-preview is disabled for large result sets to keep the UI responsive.")
            else:
                self.preview_title_label.setText("No matches")
                self.preview_meta_label.setText("No matching file was found for the current query.")
            self.preview_detail_label.setText("")
            self.preview_text_edit.setPlainText("")
            self.preview_text_edit.set_match_selections([])
            self.preview_text_cache = ""
            self.preview_search_spans = []
            self.preview_find_spans = []
            self.preview_find_active_index = -1
            self.preview_find_status_label.setText("No preview loaded.")
        self.search_progress_label.setText("Search complete.")
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(1)
        self.search_progress_bar.setFormat("Ready")
        self._clear_pending_result_population()

    def _flush_result_population_batch(self) -> None:
        if not self._pending_result_indexes:
            self._finalize_result_population()
            return
        batch_indexes = self._pending_result_indexes[: self.RESULT_POPULATION_BATCH_SIZE]
        del self._pending_result_indexes[: self.RESULT_POPULATION_BATCH_SIZE]
        batch = [self._build_result_item(index, self.search_results[index]) for index in batch_indexes]
        self.results_tree.setUpdatesEnabled(False)
        self.results_tree.addTopLevelItems(batch)
        self.results_tree.setUpdatesEnabled(True)
        populated = self._pending_result_total - len(self._pending_result_indexes)
        self.search_progress_label.setText(f"Populating results... {populated} / {self._pending_result_total}")
        self.search_progress_bar.setRange(0, max(1, self._pending_result_total))
        self.search_progress_bar.setValue(populated)
        self.search_progress_bar.setFormat(f"{populated} / {self._pending_result_total}")
        if self._pending_result_indexes:
            self._results_population_timer.start()
            return
        self._finalize_result_population()

    def _build_result_item(self, index: int, result: TextSearchResult) -> QTreeWidgetItem:
        file_name = PurePosixPath(result.relative_path).name or result.relative_path
        item = QTreeWidgetItem(
            [
                file_name,
                f"{result.match_count:,}",
                result.package_label if result.source_kind == "archive" else "Loose file",
                result.relative_path,
                result.extension,
            ]
        )
        item.setToolTip(0, file_name)
        item.setToolTip(2, item.text(2))
        item.setToolTip(3, result.relative_path)
        item.setToolTip(4, result.extension)
        item.setData(0, Qt.UserRole, index)
        return item

    def _handle_search_cancelled(self, message: str) -> None:
        self._clear_pending_result_population()
        self.search_progress_label.setText(message)
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(0)
        self.search_progress_bar.setFormat("Stopped")
        self.append_log(message)
        self.status_message_requested.emit(message, True)

    def _handle_search_error(self, message: str) -> None:
        self._clear_pending_result_population()
        self.search_progress_label.setText(message)
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(0)
        self.search_progress_bar.setFormat("Error")
        self.append_log(f"ERROR: {message}")
        self.status_message_requested.emit(message, True)

    def _cleanup_search_refs(self) -> None:
        self.search_thread = None
        self.search_worker = None
        self._update_controls()

    def _handle_result_selection_changed(self, current: Optional[QTreeWidgetItem], _previous: Optional[QTreeWidgetItem]) -> None:
        if current is None:
            return
        raw = current.data(0, Qt.UserRole)
        if not isinstance(raw, int) or raw < 0 or raw >= len(self.search_results):
            return
        result = self.search_results[raw]
        self.current_preview_result = result
        self._schedule_preview(result)

    def _schedule_preview(self, result: TextSearchResult) -> None:
        self.preview_request_id += 1
        self.preview_title_label.setText(result.relative_path)
        self.preview_meta_label.setText("Loading preview...")
        self.preview_detail_label.setText("Preparing preview...")
        self.preview_text_edit.setPlainText("")
        self.preview_text_edit.set_match_selections([])
        self.preview_search_spans = []
        self.preview_find_spans = []
        self.preview_find_active_index = -1
        self.preview_text_cache = ""
        self.preview_find_status_label.setText("Loading preview...")
        if self.preview_worker is not None:
            self.preview_worker.stop()
        self.scheduled_preview_result = result
        self._preview_debounce_timer.start()

    def _flush_scheduled_preview_request(self) -> None:
        if self.scheduled_preview_result is None:
            return
        result = self.scheduled_preview_result
        self.scheduled_preview_result = None
        if self.preview_thread is not None:
            self.pending_preview_result = result
            if self.preview_worker is not None:
                self.preview_worker.stop()
            return
        request_id = self.preview_request_id + 1
        self.preview_request_id = request_id
        self._start_preview_worker(request_id, result)

    def _start_preview_worker(self, request_id: int, result: TextSearchResult) -> None:
        worker = TextSearchPreviewWorker(
            request_id=request_id,
            result=result,
            query=self.last_search_query,
            regex_enabled=self.last_search_regex_enabled,
            case_sensitive=self.last_search_case_sensitive,
        )
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
        thread.start()

    def _handle_preview_ready(self, request_id: int, payload: object) -> None:
        if request_id != self.preview_request_id or not isinstance(payload, TextSearchPreview):
            return
        preview = payload
        self.preview_title_label.setText(preview.title)
        self.preview_meta_label.setText(preview.metadata)
        preview_detail_text = preview.detail_text
        syntax_extension = Path(preview.title).suffix.lower()
        if len(preview.preview_text) > self.SYNTAX_HIGHLIGHT_CHAR_LIMIT:
            syntax_extension = ""
            preview_detail_text = "\n".join(
                part
                for part in [
                    preview_detail_text.strip(),
                    "Syntax colors disabled for this very large preview to keep the editor responsive.",
                ]
                if part
            )
        self.preview_detail_label.setText(preview_detail_text)
        self.preview_text_edit.set_language_for_extension(syntax_extension)
        self._apply_preview_content(preview)

    def _handle_preview_error(self, request_id: int, message: str) -> None:
        if request_id != self.preview_request_id:
            return
        result = self.current_preview_result
        self.preview_title_label.setText(result.relative_path if result is not None else "Preview failed.")
        self.preview_meta_label.setText("Preview failed.")
        self.preview_detail_label.setText(message)
        self.preview_text_edit.setPlainText("")
        self.preview_text_edit.set_match_selections([])
        self.preview_search_spans = []
        self.preview_find_spans = []
        self.preview_find_active_index = -1
        self.preview_text_cache = ""
        self.preview_find_status_label.setText("Preview failed.")

    def _cleanup_preview_refs(self) -> None:
        self.preview_thread = None
        self.preview_worker = None
        if self.pending_preview_result is None:
            return
        result = self.pending_preview_result
        self.pending_preview_result = None
        request_id = self.preview_request_id + 1
        self.preview_request_id = request_id
        self._start_preview_worker(request_id, result)

    def _apply_preview_content(self, preview: TextSearchPreview) -> None:
        self.preview_text_edit.setPlainText(preview.preview_text)
        self.preview_text_cache = preview.preview_text
        self.preview_search_spans = list(preview.match_spans)
        self.preview_find_active_index = -1
        self._handle_preview_find_changed(reset_focus=True)
        if not self.preview_find_spans and self.preview_search_spans:
            first_start, first_end = self.preview_search_spans[0]
            self.preview_text_edit.center_on_span(first_start, first_end)
        elif not self.preview_search_spans:
            self.preview_text_edit.moveCursor(QTextCursor.Start)
            self.preview_text_edit.verticalScrollBar().setValue(0)
            self.preview_text_edit.horizontalScrollBar().setValue(0)

    def _adjust_preview_font(self, delta: int) -> None:
        new_size = self.preview_text_edit.adjust_font_size(delta)
        self.preview_find_status_label.setText(
            f"{self._preview_match_status_text()} | Font {new_size} pt"
            if self.preview_text_cache
            else f"Font {new_size} pt"
        )
        self.schedule_settings_save()

    def _handle_preview_wrap_changed(self, enabled: bool) -> None:
        self.preview_text_edit.set_wrap_enabled(enabled)
        self.schedule_settings_save()

    def _handle_preview_find_changed(self, _text: str = "", *, reset_focus: bool = True) -> None:
        query = self.preview_find_edit.text()
        self.preview_find_spans = self._find_preview_spans(query, self.preview_find_case_checkbox.isChecked())
        self.preview_find_active_index = 0 if self.preview_find_spans else -1
        self._refresh_preview_selections(focus_current=bool(self.preview_find_spans and reset_focus))
        self._update_preview_find_status()
        self._update_controls()

    def _find_preview_spans(self, query: str, case_sensitive: bool) -> List[tuple[int, int]]:
        query = query or ""
        if not query or not self.preview_text_cache:
            return []
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(query), flags)
        return [
            match.span()
            for match in pattern.finditer(self.preview_text_cache)
            if match.end() > match.start()
        ]

    def _make_selection(self, start: int, end: int, fmt: QTextCharFormat) -> QTextEdit.ExtraSelection:
        cursor = QTextCursor(self.preview_text_edit.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format = fmt
        return selection

    def _refresh_preview_selections(self, *, focus_current: bool) -> None:
        selections: List[QTextEdit.ExtraSelection] = []
        theme = get_theme(self.current_theme_key)
        search_format = QTextCharFormat()
        search_bg = QColor("#e3b341" if QColor(theme["window"]).lightnessF() < 0.55 else "#ffd866")
        search_bg.setAlpha(185)
        search_format.setBackground(search_bg)
        search_format.setForeground(QColor("#111111"))
        search_format.setFontWeight(QFont.DemiBold)

        find_format = QTextCharFormat()
        find_bg = QColor(theme["accent_soft"])
        if find_bg.alpha() == 255:
            find_bg.setAlpha(210)
        find_format.setBackground(find_bg)
        find_format.setForeground(QColor(theme["text_strong"]))

        active_find_format = QTextCharFormat()
        active_find_format.setBackground(QColor(theme["accent"]))
        active_find_format.setForeground(QColor("#ffffff"))
        active_find_format.setFontWeight(QFont.Bold)

        for start, end in self.preview_search_spans:
            if end > start:
                selections.append(self._make_selection(start, end, search_format))

        active_span: Optional[tuple[int, int]] = None
        for index, (start, end) in enumerate(self.preview_find_spans):
            if end <= start:
                continue
            if index == self.preview_find_active_index:
                active_span = (start, end)
                selections.append(self._make_selection(start, end, active_find_format))
            else:
                selections.append(self._make_selection(start, end, find_format))

        self.preview_text_edit.set_match_selections(selections)
        if focus_current and active_span is not None:
            self.preview_text_edit.center_on_span(*active_span)

    def _preview_match_status_text(self) -> str:
        if not self.preview_text_cache:
            return "No preview loaded."
        if self.preview_find_spans:
            return f"Find matches: {self.preview_find_active_index + 1} / {len(self.preview_find_spans):,}"
        if self.preview_search_spans:
            return f"Search highlights: {len(self.preview_search_spans):,}"
        return "No highlighted matches."

    def _update_preview_find_status(self) -> None:
        self.preview_find_status_label.setText(self._preview_match_status_text())

    def _jump_to_preview_find_match(self, direction: int) -> None:
        if not self.preview_find_spans:
            return
        self.preview_find_active_index = (self.preview_find_active_index + direction) % len(self.preview_find_spans)
        self._refresh_preview_selections(focus_current=True)
        self._update_preview_find_status()

    def _jump_to_previous_preview_find_match(self) -> None:
        self._jump_to_preview_find_match(-1)

    def _jump_to_next_preview_find_match(self) -> None:
        self._jump_to_preview_find_match(1)

    def _resolve_export_root(self) -> Optional[Path]:
        text = self.export_root_edit.text().strip()
        if not text:
            self.status_message_requested.emit("Select an export root first.", True)
            return None
        return Path(text).expanduser()

    def _confirm_export(self, results: Sequence[TextSearchResult]) -> bool:
        answer = QMessageBox.question(
            self,
            "Export Files",
            f"Export {len(results):,} matched file(s) while preserving folder structure?",
        )
        return answer == QMessageBox.Yes

    def export_selected_results(self) -> None:
        selected = self.selected_results()
        if not selected:
            self.status_message_requested.emit("Select one or more results to export.", True)
            return
        self._export_results(selected, label="selected")

    def export_all_results(self) -> None:
        if not self.search_results:
            self.status_message_requested.emit("There are no search results to export.", True)
            return
        self._export_results(self.search_results, label="all results")

    def _export_results(self, results: Sequence[TextSearchResult], *, label: str) -> None:
        export_root = self._resolve_export_root()
        if export_root is None:
            return
        if not self._confirm_export(results):
            return
        try:
            stats = export_text_search_results(results, export_root, on_log=self.append_log)
            message = (
                f"Exported {stats['exported']:,} file(s) from {label}. "
                f"Renamed {stats['renamed']:,}, failed {stats['failed']:,}."
            )
            self.status_message_requested.emit(message, False)
            self.append_log(message)
        except Exception as exc:
            self.status_message_requested.emit(str(exc), True)
            self.append_log(f"ERROR: {exc}")
