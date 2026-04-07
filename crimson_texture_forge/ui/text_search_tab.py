from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
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
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from crimson_texture_forge.constants import APP_TITLE
from crimson_texture_forge.core.text_search import (
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
from crimson_texture_forge.models import ArchiveEntry, RunCancelled
from crimson_texture_forge.ui.widgets import LogHighlighter


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
        self.archive_entries = list(archive_entries)
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


class TextSearchTab(QWidget):
    status_message_requested = Signal(str, bool)

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
        self.search_thread: Optional[QThread] = None
        self.search_worker: Optional[TextSearchWorker] = None
        self.search_results: List[TextSearchResult] = []
        self.current_preview_result: Optional[TextSearchResult] = None
        self.last_search_query = ""
        self.last_search_case_sensitive = False
        self.last_search_regex_enabled = False
        self.last_search_stats = TextSearchRunStats(source_kind="archive", candidate_count=0, searched_count=0)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(main_splitter, stretch=1)

        controls_group = QGroupBox("Text Search")
        controls_layout = QVBoxLayout(controls_group)
        controls_layout.setContentsMargins(10, 12, 10, 10)
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

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.search_button = QPushButton("Search")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.export_selected_button = QPushButton("Export Selected")
        self.export_all_button = QPushButton("Export Results")
        self.clear_log_button = QPushButton("Clear Log")
        button_row.addWidget(self.search_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.export_selected_button)
        button_row.addWidget(self.export_all_button)
        button_row.addStretch(1)
        button_row.addWidget(self.clear_log_button)
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
        log_group = QGroupBox("Search Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(10, 12, 10, 10)
        log_layout.setSpacing(8)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_view)
        controls_layout.addWidget(log_group, stretch=1)
        main_splitter.addWidget(controls_group)

        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(results_group)
        results_layout.setContentsMargins(10, 12, 10, 10)
        results_layout.setSpacing(8)
        self.results_tree = QTreeWidget()
        self.results_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)
        self.results_tree.setHeaderLabels(["Path", "Ext", "Matches", "Package"])
        self.results_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.results_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        results_layout.addWidget(self.results_tree, stretch=1)
        main_splitter.addWidget(results_group)

        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(10, 12, 10, 10)
        preview_layout.setSpacing(8)
        self.preview_title_label = QLabel("Select a matching file")
        self.preview_title_label.setWordWrap(True)
        self.preview_meta_label = QLabel("Matched files will be previewed here with highlights.")
        self.preview_meta_label.setObjectName("HintLabel")
        self.preview_meta_label.setWordWrap(True)
        self.preview_detail_label = QLabel("")
        self.preview_detail_label.setObjectName("HintLabel")
        self.preview_detail_label.setWordWrap(True)
        self.preview_text_edit = QPlainTextEdit()
        self.preview_text_edit.setReadOnly(True)
        self.preview_text_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_meta_label)
        preview_layout.addWidget(self.preview_detail_label)
        preview_layout.addWidget(self.preview_text_edit, stretch=1)
        main_splitter.addWidget(preview_group)
        controls_group.setMinimumWidth(420)
        results_group.setMinimumWidth(420)
        preview_group.setMinimumWidth(620)
        main_splitter.setStretchFactor(0, 2)
        main_splitter.setStretchFactor(1, 2)
        main_splitter.setStretchFactor(2, 4)
        main_splitter.setSizes([520, 520, 980])

        self.log_highlighter = LogHighlighter(self.log_view.document(), theme_key)
        log_font = QFont("Consolas")
        if not log_font.exactMatch():
            log_font = QFont("Courier New")
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

        for widget in (
            self.query_edit,
            self.path_filter_edit,
            self.extensions_edit,
            self.loose_root_edit,
            self.export_root_edit,
        ):
            widget.textChanged.connect(self._save_settings)
        self.source_combo.currentIndexChanged.connect(self._save_settings)
        self.case_sensitive_checkbox.toggled.connect(self._save_settings)
        self.regex_checkbox.toggled.connect(self._save_settings)

        self._load_settings()
        self._settings_ready = True
        self._apply_source_state()
        self._update_controls()

    def set_theme(self, theme_key: str) -> None:
        self.log_highlighter.set_theme(theme_key)

    def set_external_busy(self, busy: bool) -> None:
        self.external_busy = busy
        self._update_controls()

    def is_busy(self) -> bool:
        return self.search_thread is not None

    def set_archive_entries(self, entries: Sequence[ArchiveEntry], package_root_text: str = "") -> None:
        self.archive_entries = list(entries)
        self.archive_package_root_text = package_root_text.strip()
        if self.source_combo.currentData() == "archive" and not self.search_results:
            self.results_summary_label.setText(
                f"Archive source ready: {len(self.archive_entries):,} scanned entry(s) available for text search."
            )

    def diagnostic_entries(self) -> Dict[str, str]:
        return {
            "text_search_log.txt": self.log_view.toPlainText(),
        }

    def shutdown(self) -> None:
        if self.search_worker is not None:
            self.search_worker.stop()
        if self.search_thread is not None:
            self.search_thread.quit()
            self.search_thread.wait(3000)

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
        self._save_settings()

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
        self.settings.sync()

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

    def selected_results(self) -> List[TextSearchResult]:
        results: List[TextSearchResult] = []
        for item in self.results_tree.selectedItems():
            raw = item.data(0, Qt.UserRole)
            if isinstance(raw, int) and 0 <= raw < len(self.search_results):
                results.append(self.search_results[raw])
        return results

    def start_search(self) -> None:
        if self.external_busy or self.search_thread is not None:
            return

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
        data = payload if isinstance(payload, dict) else {}
        self.search_results = data.get("results", []) if isinstance(data.get("results"), list) else []
        stats = data.get("stats")
        self.last_search_stats = stats if isinstance(stats, TextSearchRunStats) else TextSearchRunStats(source_kind="archive", candidate_count=0, searched_count=0)
        self.results_tree.clear()
        for index, result in enumerate(self.search_results):
            item = QTreeWidgetItem(
                [
                    result.relative_path,
                    result.extension,
                    f"{result.match_count:,}",
                    result.package_label if result.source_kind == "archive" else "Loose file",
                ]
            )
            item.setToolTip(0, result.snippet or result.relative_path)
            item.setData(0, Qt.UserRole, index)
            self.results_tree.addTopLevelItem(item)
        if self.search_results:
            self.results_tree.setCurrentItem(self.results_tree.topLevelItem(0))
        else:
            self.preview_title_label.setText("No matches")
            self.preview_meta_label.setText("No matching file was found for the current query.")
            self.preview_detail_label.setText("")
            self.preview_text_edit.setPlainText("")
        summary = (
            f"Scanned {self.last_search_stats.candidate_count:,} candidate file(s). "
            f"Searched {self.last_search_stats.searched_count:,} readable file(s). "
            f"Found {len(self.search_results):,} matching file(s)."
        )
        if self.last_search_stats.decrypted_count:
            summary += f" Decrypted {self.last_search_stats.decrypted_count:,} archive file(s) during search."
        if self.last_search_stats.skipped_read_error_count:
            summary += f" {self.last_search_stats.skipped_read_error_count:,} file(s) could not be read."
        self.results_summary_label.setText(summary)
        self.search_progress_label.setText("Search complete.")
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(1)
        self.search_progress_bar.setFormat("Ready")
        self.append_log(summary)
        self.status_message_requested.emit(summary, False)

    def _handle_search_cancelled(self, message: str) -> None:
        self.search_progress_label.setText(message)
        self.search_progress_bar.setRange(0, 1)
        self.search_progress_bar.setValue(0)
        self.search_progress_bar.setFormat("Stopped")
        self.append_log(message)
        self.status_message_requested.emit(message, True)

    def _handle_search_error(self, message: str) -> None:
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
        self._render_preview(result)

    def _render_preview(self, result: TextSearchResult) -> None:
        try:
            preview = load_text_search_preview(
                result,
                self.last_search_query,
                regex=self.last_search_regex_enabled,
                case_sensitive=self.last_search_case_sensitive,
            )
        except Exception as exc:
            self.preview_title_label.setText(result.relative_path)
            self.preview_meta_label.setText("Preview failed.")
            self.preview_detail_label.setText(str(exc))
            self.preview_text_edit.setPlainText("")
            self.preview_text_edit.setExtraSelections([])
            return

        self.preview_title_label.setText(preview.title)
        self.preview_meta_label.setText(preview.metadata)
        self.preview_detail_label.setText(preview.detail_text)
        self._apply_preview_content(preview)

    def _apply_preview_content(self, preview: TextSearchPreview) -> None:
        self.preview_text_edit.setPlainText(preview.preview_text)
        extra_selections = []
        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor("#ffcc33"))
        highlight_format.setForeground(QColor("#111111"))
        highlight_format.setFontWeight(QFont.Bold)
        document = self.preview_text_edit.document()
        for start, end in sorted(preview.match_spans, key=lambda item: item[0]):
            if end <= start:
                continue
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format = highlight_format
            extra_selections.append(selection)
        self.preview_text_edit.setExtraSelections(extra_selections)
        if preview.match_spans:
            first_start, first_end = preview.match_spans[0]
            cursor = self.preview_text_edit.textCursor()
            cursor.setPosition(first_start)
            cursor.setPosition(first_end, QTextCursor.KeepAnchor)
            self.preview_text_edit.setTextCursor(cursor)
            self.preview_text_edit.centerCursor()
        else:
            self.preview_text_edit.moveCursor(QTextCursor.Start)
        self.preview_text_edit.verticalScrollBar().setValue(0)
        self.preview_text_edit.horizontalScrollBar().setValue(0)

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
