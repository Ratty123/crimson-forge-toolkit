from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPixmap, QSyntaxHighlighter, QTextCharFormat, QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QFrame,
    QWidget,
)

from crimson_texture_forge.ui.themes import get_theme


class PreviewLabel(QLabel):
    def __init__(self, title: str):
        super().__init__(title)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(280, 220)
        self.setWordWrap(True)
        self.setObjectName("PreviewLabel")
        self._source_pixmap: Optional[QPixmap] = None
        self._zoom_factor = 1.0
        self._fit_to_view = True
        self._scroll_area = None
        self._drag_active = False
        self._drag_start_global_pos = None
        self._drag_start_h = 0
        self._drag_start_v = 0

    def clear_preview(self, message: str) -> None:
        self._source_pixmap = None
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

    def set_zoom_factor(self, zoom_factor: float) -> None:
        self._zoom_factor = max(0.1, zoom_factor)
        if self._source_pixmap is not None:
            self._apply_scaled_pixmap(self.text())

    def set_fit_to_view(self, fit_to_view: bool) -> None:
        self._fit_to_view = fit_to_view
        if self._source_pixmap is not None:
            self._apply_scaled_pixmap(self.text())

    def set_preview_pixmap(self, pixmap: QPixmap, fallback_text: str) -> None:
        self._source_pixmap = pixmap
        self._apply_scaled_pixmap(fallback_text)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._source_pixmap is not None:
            self._apply_scaled_pixmap(self.text())

    def _handle_viewport_resize(self) -> None:
        if self._source_pixmap is not None and self._fit_to_view:
            self._apply_scaled_pixmap(self.text())

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
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

    def _can_pan(self) -> bool:
        if self._source_pixmap is None or self._source_pixmap.isNull() or self._scroll_area is None:
            return False
        if self._fit_to_view:
            return False
        viewport = self._scroll_area.viewport().size()
        return self.width() > viewport.width() or self.height() > viewport.height()

    def _update_cursor(self) -> None:
        if self._drag_active:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._can_pan():
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()

    def _apply_scaled_pixmap(self, fallback_text: str) -> None:
        if self._source_pixmap is None or self._source_pixmap.isNull():
            self.setPixmap(QPixmap())
            self.setText(fallback_text)
            self._update_cursor()
            return

        if self._fit_to_view and self._scroll_area is not None:
            viewport = self._scroll_area.viewport().size()
            width = max(1, viewport.width() - 6)
            height = max(1, viewport.height() - 6)
        else:
            width = max(1, int(round(self._source_pixmap.width() * self._zoom_factor)))
            height = max(1, int(round(self._source_pixmap.height() * self._zoom_factor)))

        scaled = self._source_pixmap.scaled(
            width,
            height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.setText("")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(0, 0)
        self.resize(scaled.size())
        self.setFixedSize(scaled.size())
        self.setPixmap(scaled)
        self._update_cursor()


class PreviewScrollArea(QScrollArea):
    resized = Signal()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.resized.emit()


class LogHighlighter(QSyntaxHighlighter):
    _timestamp_re = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]")
    _error_re = re.compile(r"\b(ERROR|Traceback|Exception|FAILED)\b", re.IGNORECASE)
    _warning_re = re.compile(r"\b(warning|preflight)\b", re.IGNORECASE)
    _success_re = re.compile(r"\b(complete|completed|finished|ready|successfully)\b", re.IGNORECASE)
    _phase_re = re.compile(r"\bPhase\s+\d+/\d+\b", re.IGNORECASE)
    _path_re = re.compile(r"[A-Za-z]:\\[^\r\n<>|\"*?]+")

    def __init__(self, document, theme_key: str):
        super().__init__(document)
        self.timestamp_format = QTextCharFormat()
        self.error_format = QTextCharFormat()
        self.warning_format = QTextCharFormat()
        self.success_format = QTextCharFormat()
        self.phase_format = QTextCharFormat()
        self.path_format = QTextCharFormat()
        self.set_theme(theme_key)

    def set_theme(self, theme_key: str) -> None:
        theme = get_theme(theme_key)

        def make_format(color: str, *, bold: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            return fmt

        self.timestamp_format = make_format(theme["text_muted"])
        self.error_format = make_format(theme["error"], bold=True)
        self.warning_format = make_format(theme["warning_text"], bold=True)
        self.success_format = make_format(theme["accent"], bold=False)
        self.phase_format = make_format(theme["accent"], bold=True)
        self.path_format = make_format(theme["text_strong"], bold=False)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        timestamp_match = self._timestamp_re.match(text)
        if timestamp_match:
            self.setFormat(timestamp_match.start(), timestamp_match.end() - timestamp_match.start(), self.timestamp_format)

        for match in self._path_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.path_format)

        for match in self._phase_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.phase_format)

        for match in self._warning_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.warning_format)

        for match in self._error_re.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self.error_format)

        if "completed successfully" in text.lower():
            match = self._success_re.search(text)
            if match:
                self.setFormat(match.start(), match.end() - match.start(), self.success_format)


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


class QuickStartDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("Quick Start")
        self.resize(780, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel("First-run guide")
        title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(title_label)

        intro_label = QLabel(
            "This app is a workspace manager for archive extraction, optional PNG upscaling, and DDS rebuild."
        )
        intro_label.setObjectName("HintLabel")
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setReadOnly(True)
        self.browser.setHtml(
            """
            <h3>What this app does</h3>
            <p><b>Crimson Texture Forge</b> is a read-only archive browser plus a loose-file texture workflow for DDS-to-PNG conversion, optional external upscaling, DDS rebuild, and comparison.</p>
            <ul>
              <li><b>Archive Browser</b>: scan <b>.pamt/.paz</b>, preview supported assets, filter, and extract to normal folders.</li>
              <li><b>Workflow</b>: scan loose DDS files, optionally convert DDS to PNG with <b>texconv</b>, optionally run <b>chaiNNer</b>, rebuild DDS, and compare results.</li>
              <li><b>Text Search</b>: search archive or loose text-like files such as <b>.xml</b>, preview matching files with highlights, and export matched files while preserving folder structure.</li>
            </ul>
            <h3>Recommended first setup</h3>
            <ol>
              <li><b>Setup</b>: Click <b>Init Workspace</b> to create a clean folder layout. If you later change paths manually, use <b>Create Folders</b> to create any missing directories.</li>
              <li><b>texconv</b>: Set the <b>texconv.exe</b> path or use the built-in download button. DDS preview, DDS-to-PNG conversion, compare previews, and final DDS rebuild all depend on texconv.</li>
              <li><b>Paths</b>: Set <b>Original DDS root</b>, <b>PNG root</b>, and <b>Output root</b>. If you plan to convert DDS to PNG before <b>chaiNNer</b>, also set <b>Staging PNG root</b> or let the app choose its default.</li>
              <li><b>Scan</b>: In the Workflow tab, click <b>Scan</b> so the app can count the DDS files that match the current filter.</li>
              <li><b>DDS Output</b>: Enable <b>Convert DDS to PNG before processing</b> if you want texconv to create PNG files first. If <b>chaiNNer</b> is disabled, <b>Start</b> will stop after PNG conversion.</li>
              <li><b>Start small</b>: Run a small folder or filtered subset first, then verify the result in <b>Compare</b>.</li>
            </ol>
            <h3>Optional chaiNNer stage</h3>
            <p><b>chaiNNer</b> is optional and external. If you enable it, this app runs <b>chaiNNer</b> first and only starts the DDS rebuild after it has finished.</p>
            <ul>
              <li>Download or point the app at a working <b>chaiNNer.exe</b>.</li>
              <li>Open <b>chaiNNer</b> separately at least once and install the packages your chain needs, such as <b>PyTorch</b>, <b>NCNN</b>, or <b>ONNX/ONNX Runtime</b>, depending on the nodes you use.</li>
              <li>Create and test your own <b>.chn</b> chain in <b>chaiNNer</b> first.</li>
              <li>Your chain must include its own upscaler model and nodes. This app does not build the chain for you.</li>
              <li>If your chain reads loose PNG files, enable <b>Convert DDS to PNG before processing</b> and make the chain read <b>${staging_png_root}</b> or another PNG folder.</li>
              <li>If your chain is already set up to read DDS directly and works in <b>chaiNNer</b>, you can keep that workflow, but test it in <b>chaiNNer</b> first.</li>
              <li>The app can pass path tokens through chaiNNer overrides: <b>${original_dds_root}</b>, <b>${staging_png_root}</b>, <b>${png_root}</b>, <b>${output_root}</b>, <b>${texconv_path}</b>.</li>
            </ul>
            <h3>Archive browser</h3>
            <p>The archive browser is read-only. It can scan <b>.pamt/.paz</b>, filter files, preview supported assets, and extract selected files or DDS trees into normal workspace folders.</p>
            <ul>
              <li><b>Scan</b> uses a saved archive cache when it is valid.</li>
              <li><b>Refresh</b> ignores the cache and rebuilds it from the current <b>.pamt</b> files.</li>
              <li><b>DDS To Workflow</b> extracts archive DDS files into your loose workflow so you can scan and rebuild them like normal files.</li>
            </ul>
            <h3>Compare and review</h3>
            <p>Use the <b>Compare</b> tab to review original vs rebuilt DDS side by side. You can zoom, pan, inspect metadata, and refresh the compare list after new output is written.</p>
            <h3>Text search utility</h3>
            <p>Use the <b>Text Search</b> tab when you want to inspect text-like files outside the texture workflow. It can search archive entries or loose folders, highlight matches in preview, and export matched files with their folder structure intact.</p>
            <h3>Where settings are stored</h3>
            <p>The app auto-saves settings to a local config file beside the EXE and also keeps archive scan cache data in a local cache folder beside it.</p>
            <h3>Common causes of failure</h3>
            <ul>
              <li><b>Missing texconv</b>: previews, DDS-to-PNG conversion, and DDS rebuild will fail until <b>texconv.exe</b> is configured.</li>
              <li><b>Wrong chaiNNer chain paths</b>: hardcoded folders inside the chain can make chaiNNer read or save to the wrong place.</li>
              <li><b>No matching PNG outputs</b>: if chaiNNer finishes but nothing lands in <b>PNG root</b>, the DDS rebuild step has nothing to convert.</li>
              <li><b>Wrong chaiNNer input type</b>: if DDS-to-PNG conversion is enabled but the chain still reads DDS from the original folder, the workflow will not behave as expected.</li>
            </ul>
            """
        )
        layout.addWidget(self.browser, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.open_setup_button = QPushButton("Open Setup")
        self.open_chainner_button = QPushButton("Open chaiNNer Setup")
        self.close_button = QPushButton("Close")
        button_row.addWidget(self.open_setup_button)
        button_row.addWidget(self.open_chainner_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.open_setup_button.clicked.connect(self._open_setup)
        self.open_chainner_button.clicked.connect(self._open_chainner_setup)
        self.close_button.clicked.connect(self.accept)

    def _open_setup(self) -> None:
        self.parent_window.focus_quick_start_sections(include_chainner=False)
        self.accept()

    def _open_chainner_setup(self) -> None:
        self.parent_window.focus_quick_start_sections(include_chainner=True)
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent, *, title: str, html: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(title_label)

        browser = QTextBrowser()
        browser.setReadOnly(True)
        browser.setOpenExternalLinks(True)
        browser.setHtml(html)
        layout.addWidget(browser, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)
