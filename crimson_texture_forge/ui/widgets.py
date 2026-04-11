from __future__ import annotations

import re
from typing import Callable, Optional

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QImageReader,
    QPainter,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
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
        self._source_image_path: str = ""
        self._source_image_size = QSize()
        self._zoom_factor = 1.0
        self._fit_to_view = True
        self._fit_scale = 1.0
        self._scroll_area = None
        self._wheel_zoom_handler: Optional[Callable[[int], None]] = None
        self._drag_active = False
        self._drag_start_global_pos = None
        self._drag_start_h = 0
        self._drag_start_v = 0

    def clear_preview(self, message: str) -> None:
        self._source_pixmap = None
        self._source_image_path = ""
        self._source_image_size = QSize()
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

    def set_zoom_factor(self, zoom_factor: float) -> None:
        self._zoom_factor = max(0.1, zoom_factor)
        if self._source_pixmap is not None:
            self._apply_scaled_pixmap(self.text())

    def set_fit_to_view(self, fit_to_view: bool) -> None:
        self._fit_to_view = fit_to_view
        if self._source_pixmap is not None:
            self._apply_scaled_pixmap(self.text())

    def set_fit_scale(self, fit_scale: float) -> None:
        self._fit_scale = max(0.5, min(4.0, fit_scale))
        if self._source_pixmap is not None and self._fit_to_view:
            self._apply_scaled_pixmap(self.text())

    def set_preview_pixmap(self, pixmap: QPixmap, fallback_text: str) -> None:
        self._source_pixmap = pixmap
        self._source_image_path = ""
        self._source_image_size = pixmap.size()
        self._apply_scaled_pixmap(fallback_text)

    def set_preview_image_path(self, image_path: str, fallback_text: str) -> None:
        self._source_pixmap = None
        self._source_image_path = image_path
        reader = QImageReader(image_path)
        size = reader.size()
        self._source_image_size = size if size.isValid() else QSize()
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

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta_y = event.angleDelta().y()
        if (
            self._wheel_zoom_handler is not None
            and self._source_pixmap is not None
            and not self._source_pixmap.isNull()
            and delta_y != 0
        ):
            step = 1 if delta_y > 0 else -1
            self._wheel_zoom_handler(step)
            event.accept()
            return
        super().wheelEvent(event)

    def _can_pan(self) -> bool:
        if self._source_pixmap is None or self._source_pixmap.isNull() or self._scroll_area is None:
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
        has_source_pixmap = self._source_pixmap is not None and not self._source_pixmap.isNull()
        has_source_path = bool(self._source_image_path)
        if not has_source_pixmap and not has_source_path:
            self.setPixmap(QPixmap())
            self.setText(fallback_text)
            self._update_cursor()
            return

        if self._fit_to_view and self._scroll_area is not None:
            viewport = self._scroll_area.viewport().size()
            width = max(1, int(round((viewport.width() - 6) * self._fit_scale)))
            height = max(1, int(round((viewport.height() - 6) * self._fit_scale)))
        else:
            if has_source_pixmap:
                source_size = self._source_pixmap.size()
            else:
                source_size = self._source_image_size
            width = max(1, int(round(source_size.width() * self._zoom_factor)))
            height = max(1, int(round(source_size.height() * self._zoom_factor)))

        if has_source_pixmap:
            scaled = self._source_pixmap.scaled(
                width,
                height,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        else:
            reader = QImageReader(self._source_image_path)
            if self._source_image_size.isValid():
                target_size = self._source_image_size.scaled(width, height, Qt.KeepAspectRatio)
                if target_size.isValid():
                    reader.setScaledSize(target_size)
            image = reader.read()
            if image.isNull():
                self.setPixmap(QPixmap())
                self.setText(fallback_text)
                self._update_cursor()
                return
            scaled = QPixmap.fromImage(image)
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


def _theme_is_light(theme_key: str) -> bool:
    theme = get_theme(theme_key)
    color = QColor(theme["window"])
    return color.lightnessF() >= 0.55


class PreviewSyntaxHighlighter(QSyntaxHighlighter):
    XML_TEXT_EXTENSIONS = {".xml", ".html", ".thtml", ".material", ".shader"}
    JSON_TEXT_EXTENSIONS = {".json", ".yaml", ".yml"}
    INI_TEXT_EXTENSIONS = {".ini", ".cfg"}
    LUA_TEXT_EXTENSIONS = {".lua"}

    LUA_KEYWORDS = {
        "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
        "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
    }

    def __init__(self, document, theme_key: str):
        super().__init__(document)
        self.language = "plain"
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
        light = _theme_is_light(theme_key)

        def make(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            return fmt

        if light:
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

    def set_language_for_extension(self, extension: str) -> None:
        suffix = (extension or "").lower()
        if suffix in self.XML_TEXT_EXTENSIONS:
            self.language = "xml"
        elif suffix in self.JSON_TEXT_EXTENSIONS:
            self.language = "json"
        elif suffix in self.INI_TEXT_EXTENSIONS:
            self.language = "ini"
        elif suffix in self.LUA_TEXT_EXTENSIONS:
            self.language = "lua"
        else:
            self.language = "plain"
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        if self.language == "xml":
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
    def __init__(self, *, theme_key: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme_key = theme_key
        self._match_selections: list[QTextEdit.ExtraSelection] = []
        self._editor_font_size = 10
        self.line_number_area = _LineNumberArea(self)
        self.syntax_highlighter = PreviewSyntaxHighlighter(self.document(), theme_key)
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
        self.syntax_highlighter.set_theme(theme_key)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme['preview_bg']}; color: {theme['text']}; border: 1px solid {theme['border_strong']}; border-radius: 4px; selection-background-color: {theme['accent']}; selection-color: #ffffff; }}"
        )
        self.viewport().update()
        self.line_number_area.update()
        self._apply_combined_selections()

    def set_language_for_extension(self, extension: str) -> None:
        self.syntax_highlighter.set_language_for_extension(extension)

    def set_wrap_enabled(self, enabled: bool) -> None:
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if enabled else QPlainTextEdit.NoWrap)

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
    _backend_re = re.compile(r"\b(Real-ESRGAN NCNN|ONNX Runtime|chaiNNer|texconv(?:\.exe)?)\b", re.IGNORECASE)
    _correction_mode_re = re.compile(r"\b(Match Mean Luma|Match Levels|Match Histogram)\b", re.IGNORECASE)
    _texture_type_re = re.compile(r"\[(color|ui|emissive|impostor|normal|height|vector|roughness|mask|unknown)\]")
    _key_value_re = re.compile(r"\b([a-z_]+)=([^\s,;()]+)", re.IGNORECASE)
    _label_re = re.compile(
        r"\b(scale|tile|preset|model|format|mips|output|png|backend|correction|mean|range|source|providers?|folder|executable|input|root)\b",
        re.IGNORECASE,
    )
    _dimension_re = re.compile(r"\b\d+x\d+\b")
    _number_re = re.compile(r"(?<![\w./\\-])\d+(?:[.,]\d+)?\b")
    _arrow_re = re.compile(r"->")

    def __init__(self, document, theme_key: str):
        super().__init__(document)
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
        theme = get_theme(theme_key)
        light = _theme_is_light(theme_key)

        def make_format(
            color: str,
            *,
            bold: bool = False,
            italic: bool = False,
            background: Optional[QColor] = None,
        ) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            fmt.setFontItalic(italic)
            if background is not None:
                fmt.setBackground(background)
            return fmt

        self.timestamp_format = make_format(theme["text_muted"])
        self.error_format = make_format(theme["error"], bold=True)
        self.warning_format = make_format(theme["warning_text"], bold=True)
        self.success_format = make_format("#098658" if light else "#6a9955", bold=True)
        self.phase_format = make_format(theme["accent"], bold=True)
        self.path_format = make_format(theme["text_strong"], bold=True)
        self.progress_format = make_format(theme["accent"], bold=True)
        self.action_format = make_format("#0451a5" if light else "#569cd6", bold=True)
        self.backend_format = make_format(theme["accent"], bold=True)
        self.key_format = make_format("#795e26" if light else "#d7ba7d", bold=True)
        self.value_format = make_format("#a31515" if light else "#ce9178")
        self.number_format = make_format("#098658" if light else "#b5cea8")
        self.separator_format = make_format(theme["text_muted"], bold=True)

        warning_bg = QColor(theme["warning_bg"])
        warning_bg.setAlpha(70 if light else 48)
        error_bg = QColor(theme["error"])
        error_bg.setAlpha(42 if light else 34)
        success_bg = QColor(theme["accent_soft"])
        success_bg.setAlpha(120 if light else 90)
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

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
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
            <h3>Overview</h3>
            <p><b>Crimson Texture Forge</b> is a read-only archive and loose-file workflow tool for Crimson Desert. Its main jobs are archive extraction, DDS-to-PNG conversion, optional upscaling, DDS rebuild, compare review, texture research, and text search.</p>
            <ul>
              <li><b>Archive Browser</b>: scan <b>.pamt/.paz</b>, preview supported assets, filter, and extract to normal folders.</li>
              <li><b>Workflow</b>: scan loose DDS files, optionally convert DDS to PNG with <b>texconv</b>, optionally upscale with <b>chaiNNer</b>, <b>Real-ESRGAN NCNN</b>, or <b>ONNX Runtime</b>, rebuild DDS, and compare results.</li>
              <li><b>Research</b>: inspect grouped texture sets, classification, references, DDS QA results, exported reports, and local notes.</li>
              <li><b>Text Search</b>: search archive or loose text-like files such as <b>.xml</b>, preview matches with syntax colors, and export results while preserving folder structure.</li>
              <li><b>Settings</b>: store persistent global preferences such as theme, startup cache behavior, remembered layouts, and cleanup confirmations.</li>
            </ul>
            <h3>Recommended first run</h3>
            <ol>
              <li>Open <b>Setup</b> and click <b>Init Workspace</b>.</li>
              <li>Configure <b>texconv.exe</b> or use the external download page link in <b>Setup</b>. DDS preview, DDS-to-PNG conversion, compare previews, and final DDS rebuild depend on it.</li>
              <li>Set <b>Original DDS root</b>, <b>PNG root</b>, and <b>Output root</b>.</li>
              <li>Choose an upscaling mode in <b>Upscaling</b>: disabled, direct <b>Real-ESRGAN NCNN</b>, direct <b>ONNX Runtime</b>, or <b>chaiNNer</b>.</li>
              <li>Keep a safer <b>Texture Policy</b> preset first and leave automatic rules enabled so risky technical DDS files are preserved instead of pushed through the PNG path.</li>
              <li>Use <b>Preview Policy</b> before <b>Start</b> if you want to inspect the planned per-texture action.</li>
              <li>Click <b>Scan</b> in the Workflow tab.</li>
              <li>Run a small subset first, then review the output in <b>Compare</b> before trying a larger batch.</li>
            </ol>
            <h3>Backend chooser</h3>
            <p><b>Safe Upscale Wizard</b> gives you a guided way to pick a backend, choose a safer preset, set direct scale/tile values, enable retry behavior, and configure optional loose export.</p>
            <ul>
              <li><b>Disabled</b>: rebuild DDS from existing PNGs or test DDS output settings without upscaling.</li>
              <li><b>Real-ESRGAN NCNN</b>: easiest direct in-app route if you want scale, tile, retry, and optional post correction controlled from the app.</li>
              <li><b>ONNX Runtime</b>: direct in-app route for local <b>.onnx</b> models with the same direct controls.</li>
              <li><b>chaiNNer</b>: use only if you already have a tested chain. The chain remains the source of truth; direct NCNN / ONNX controls do not override it.</li>
            </ul>
            <h3>Before you upscale</h3>
            <p>Visible color textures are not the same as technical maps. Height, displacement, normals, masks, vectors, and other precision-sensitive DDS files are riskier to push through PNG intermediates.</p>
            <ul>
              <li>Start with a safer preset.</li>
              <li>Keep automatic rules enabled.</li>
              <li>Remember that presets decide what enters the upscale path, but model choice can still shift brightness, contrast, and detail.</li>
              <li>Optional post correction such as <b>match_levels</b> or <b>match_histogram</b> only applies to direct NCNN / ONNX runs and only to visible texture classes.</li>
            </ul>
            <h3>Compare and review</h3>
            <p><b>Compare</b> is meant to be the review step before large runs. When the Compare tab is active, the layout gives more room to the previews.</p>
            <ul>
              <li>Use <b>Preview size</b> to scale both panes together.</li>
              <li>Use the mouse wheel while hovering a preview to zoom.</li>
              <li>Drag to pan when a preview is larger than the viewport.</li>
              <li>Use <b>Sync Pan</b> to keep both previews aligned.</li>
            </ul>
            <h3>Research and Text Search</h3>
            <ul>
              <li>Use <b>Research</b> for grouped texture sets, classifier output, references, DDS analysis, reports, heatmaps, and local notes.</li>
              <li>Use <b>Text Search</b> for archive or loose text-like files such as <b>.xml</b>, <b>.json</b>, <b>.cfg</b>, and <b>.lua</b>, including preview, regex search, and export.</li>
            </ul>
            <h3>Common failure causes</h3>
            <ul>
              <li><b>Missing texconv</b>: previews, DDS-to-PNG conversion, compare previews, and DDS rebuild all depend on <b>texconv.exe</b>.</li>
              <li><b>Missing NCNN models or ONNX runtime/models</b>: direct backends need working runtime setup plus compatible models.</li>
              <li><b>No matching PNG outputs</b>: if a chain or backend produces no usable PNG output, DDS rebuild has nothing to convert.</li>
              <li><b>Wrong chaiNNer paths</b>: hardcoded chain folders can make chaiNNer read from or write to the wrong place.</li>
              <li><b>Brightness drift</b>: review in <b>Compare</b>, try a different model, or test direct post correction for visible textures.</li>
            </ul>
            <h3>Local state</h3>
            <p>The app auto-saves its settings beside the EXE and also stores archive scan cache beside it.</p>
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
