from __future__ import annotations

import dataclasses
import threading
from html import escape
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QImageReader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from crimson_forge_toolkit.constants import (
    APP_TITLE,
    DEFAULT_UPSCALE_POST_CORRECTION,
    DEFAULT_UPSCALE_TEXTURE_PRESET,
    REALESRGAN_NCNN_EXTRA_ARGS,
    REALESRGAN_NCNN_MODEL_DIR,
    REALESRGAN_NCNN_MODEL_NAME,
    REALESRGAN_NCNN_SCALE,
    REALESRGAN_NCNN_TILE_SIZE,
    UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    UPSCALE_POST_CORRECTION_MATCH_LEVELS,
    UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
    UPSCALE_POST_CORRECTION_NONE,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL,
    UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED,
    UPSCALE_TEXTURE_PRESET_ALL,
    UPSCALE_TEXTURE_PRESET_BALANCED,
    UPSCALE_TEXTURE_PRESET_COLOR_UI,
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
)
from crimson_forge_toolkit.core.archive import ArchiveEntry
from crimson_forge_toolkit.core.pipeline import build_compare_preview_pane_result, parse_dds
from crimson_forge_toolkit.core.replace_assistant import (
    ReplaceAssistantArchiveIndex,
    build_replace_assistant_archive_index,
    build_replace_assistant_items,
    build_replace_assistant_package,
    build_replace_assistant_preview_assets,
    match_replace_assistant_item_to_archive_entry,
    match_replace_assistant_item_to_local_original,
    match_replace_assistant_original,
)
from crimson_forge_toolkit.core.research import summarize_ui_reference_constraints
from crimson_forge_toolkit.core.realesrgan_ncnn import discover_realesrgan_ncnn_models
from crimson_forge_toolkit.models import (
    ArchivePreviewResult,
    MatchedOriginalTexture,
    ModPackageInfo,
    ReplaceAssistantBuildOptions,
    ReplaceAssistantBuildSummary,
    ReplaceAssistantItem,
    ReplaceAssistantReviewItem,
    RunCancelled,
    TextureEditorSourceBinding,
)
from crimson_forge_toolkit.ui.widgets import (
    FlatSectionPanel,
    PreviewLabel,
    PreviewScrollArea,
    clamp_splitter_sizes,
    build_responsive_splitter_sizes,
)


def _shutdown_thread(thread: Optional[QThread], *, grace_ms: int = 1200) -> None:
    if thread is None:
        return
    thread.quit()
    thread.wait(grace_ms)


class ReplaceAssistantPreviewWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(
        self,
        request_id: int,
        texconv_path: Optional[Path],
        source_path: Path,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.texconv_path = texconv_path
        self.source_path = source_path
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            preview_png_path, metadata_summary, detail_text = build_replace_assistant_preview_assets(
                self.texconv_path,
                self.source_path,
            )
            result = ArchivePreviewResult(
                status="ok" if preview_png_path or self.source_path.exists() else "missing",
                title=self.source_path.name,
                metadata_summary=metadata_summary,
                detail_text=detail_text,
                preview_image_path=preview_png_path,
                preferred_view="preview",
            )
            if not self.stop_event.is_set() and result.preview_image_path:
                reader = QImageReader(result.preview_image_path)
                image = reader.read()
                if not image.isNull():
                    result = dataclasses.replace(result, preview_image=image)
            if not self.stop_event.is_set():
                self.completed.emit(self.request_id, result)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantBuildWorker(QObject):
    log_message = Signal(str)
    current_file = Signal(str)
    progress = Signal(int, int, str)
    completed = Signal(object)
    cancelled = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        items: Sequence[ReplaceAssistantItem],
        options: ReplaceAssistantBuildOptions,
        *,
        archive_entries: Sequence[ArchiveEntry],
        original_dds_root: Optional[Path],
    ) -> None:
        super().__init__()
        self.items = list(items)
        self.options = options
        self.archive_entries = list(archive_entries)
        self.original_dds_root = original_dds_root
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            summary = build_replace_assistant_package(
                self.items,
                self.options,
                archive_entries=self.archive_entries,
                original_dds_root=self.original_dds_root,
                stop_event=self.stop_event,
                on_log=self.log_message.emit,
                on_progress=self.progress.emit,
                on_current_file=self.current_file.emit,
            )
            if summary.cancelled:
                self.cancelled.emit("Replace Assistant build stopped by user.")
            else:
                self.completed.emit(summary)
        except RunCancelled as exc:
            self.cancelled.emit(str(exc) or "Replace Assistant build stopped by user.")
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantImportWorker(QObject):
    stage_message = Signal(str)
    progress = Signal(int, int, str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        paths: Sequence[Path | str],
        *,
        archive_entries: Sequence[ArchiveEntry],
        original_dds_root: Optional[Path],
        archive_index: Optional[ReplaceAssistantArchiveIndex],
    ) -> None:
        super().__init__()
        self.paths = list(paths)
        self.archive_entries = archive_entries
        self.original_dds_root = original_dds_root
        self.archive_index = archive_index
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            items = build_replace_assistant_items(
                self.paths,
                on_stage=self.stage_message.emit,
                on_progress=self.progress.emit,
                perform_matching=False,
            )
            if not self.stop_event.is_set():
                self.completed.emit(
                    {
                        "items": items,
                        "archive_index": self.archive_index,
                        "original_dds_root": self.original_dds_root,
                    }
                )
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantAutoMatchWorker(QObject):
    stage_message = Signal(str)
    progress = Signal(int, int, str)
    completed = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        items: Sequence[ReplaceAssistantItem],
        *,
        archive_entries: Sequence[ArchiveEntry],
        original_dds_root: Optional[Path],
        archive_index: Optional[ReplaceAssistantArchiveIndex],
    ) -> None:
        super().__init__()
        self.items = [dataclasses.replace(item) for item in items]
        self.archive_entries = archive_entries
        self.original_dds_root = original_dds_root
        self.archive_index = archive_index
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            archive_entries = list(self.archive_entries)
            active_index = self.archive_index
            if active_index is None:
                self.stage_message.emit("Indexing archive and original DDS files...")
                active_index = build_replace_assistant_archive_index(
                    archive_entries,
                    original_dds_root=self.original_dds_root,
                    on_progress=self.progress.emit,
                )
            if self.stop_event.is_set():
                return
            self.stage_message.emit("Matching imported files to original DDS entries...")
            total = len(self.items)
            for index, item in enumerate(self.items, start=1):
                if self.stop_event.is_set():
                    return
                source_path = item.source_path.expanduser().resolve()
                self.progress.emit(index - 1, total, f"[{index}/{total}] Matching {source_path.name}")
                matched = match_replace_assistant_original(source_path, active_index)
                if matched.archive_entry is not None or matched.original_dds_path is not None:
                    item.matched_original = matched
                    item.detected_package_root = matched.package_root
                    item.detected_relative_path = matched.archive_relative_path
                    item.status = "matched"
                    item.status_detail = matched.match_reason
                    item.warning = matched.match_reason if matched.match_reason.startswith("ambiguous") else ""
                else:
                    item.matched_original = None
                    item.status = "unresolved"
                    item.status_detail = matched.match_reason or "unmatched"
                    item.warning = matched.match_reason if matched.match_reason.startswith("ambiguous") else ""
            if not self.stop_event.is_set():
                self.progress.emit(total, total, f"{total} / {total}")
                self.completed.emit(
                    {
                        "items": self.items,
                        "archive_index": active_index,
                        "original_dds_root": self.original_dds_root,
                    }
                )
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantReviewCompareWorker(QObject):
    completed = Signal(int, object)
    error = Signal(int, str)
    finished = Signal()

    def __init__(self, request_id: int, texconv_path: Optional[Path], item: ReplaceAssistantReviewItem) -> None:
        super().__init__()
        self.request_id = request_id
        self.texconv_path = texconv_path
        self.item = item
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def _collect_source_metadata(self, path: Path) -> Dict[str, str]:
        resolved = path.expanduser().resolve()
        suffix = resolved.suffix.lower()
        metadata: Dict[str, str] = {
            "kind": suffix.lstrip(".").upper() or "FILE",
            "path": str(resolved),
        }
        if suffix == ".dds":
            try:
                dds_info = parse_dds(resolved)
                metadata.update(
                    {
                        "format": dds_info.texconv_format,
                        "size": f"{dds_info.width}x{dds_info.height}",
                        "mips": str(dds_info.mip_count),
                    }
                )
            except Exception:
                pass
            return metadata

        reader = QImageReader(str(resolved))
        size = reader.size()
        if size.isValid():
            metadata["size"] = f"{size.width()}x{size.height()}"
        format_bytes = reader.format()
        image_format = bytes(format_bytes).decode("ascii", errors="ignore").upper().strip()
        if image_format:
            metadata["format"] = image_format
        return metadata

    def _collect_dds_metadata(self, path: Optional[Path]) -> Optional[Dict[str, str]]:
        if path is None:
            return None
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return None
        try:
            dds_info = parse_dds(resolved)
        except Exception:
            return None
        return {
            "path": str(resolved),
            "format": dds_info.texconv_format,
            "size": f"{dds_info.width}x{dds_info.height}",
            "mips": str(dds_info.mip_count),
        }

    def _build_comparison_rows(
        self,
        *,
        source_metadata: Dict[str, str],
        original_metadata: Optional[Dict[str, str]],
        output_metadata: Optional[Dict[str, str]],
    ) -> List[tuple[str, str]]:
        rows: List[tuple[str, str]] = [
            ("Build mode", "Upscale with NCNN, then rebuild" if self.item.build_mode == "upscale_then_rebuild" else "Rebuild only"),
            ("Size mode", "Match original size" if self.item.size_mode == "match_original" else "Use edited size"),
        ]
        if original_metadata is not None and output_metadata is not None:
            rows.append(
                (
                    "Format",
                    "Matches original" if original_metadata.get("format") == output_metadata.get("format") else f"{original_metadata.get('format', '?')} -> {output_metadata.get('format', '?')}",
                )
            )
            rows.append(
                (
                    "Resolution",
                    "Matches original" if original_metadata.get("size") == output_metadata.get("size") else f"{original_metadata.get('size', '?')} -> {output_metadata.get('size', '?')}",
                )
            )
            rows.append(
                (
                    "Mip count",
                    "Matches original" if original_metadata.get("mips") == output_metadata.get("mips") else f"{original_metadata.get('mips', '?')} -> {output_metadata.get('mips', '?')}",
                )
            )
        if source_metadata.get("size") and output_metadata is not None:
            rows.append(
                (
                    "Edited source vs output",
                    "Same size" if source_metadata.get("size") == output_metadata.get("size") else f"{source_metadata.get('size', '?')} -> {output_metadata.get('size', '?')}",
                )
            )
        return rows

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            source_metadata = self._collect_source_metadata(self.item.source_path)
            original_metadata = self._collect_dds_metadata(self.item.original_dds_path)
            source_preview_path, source_meta, source_detail = build_replace_assistant_preview_assets(
                self.texconv_path,
                self.item.source_path,
            )
            source_image = None
            if source_preview_path and not self.stop_event.is_set():
                reader = QImageReader(source_preview_path)
                image = reader.read()
                if not image.isNull():
                    source_image = image
            output_result = build_compare_preview_pane_result(
                self.texconv_path,
                self.item.output_dds_path,
                "Rebuilt DDS not found.",
                stop_event=self.stop_event,
            )
            output_metadata = self._collect_dds_metadata(self.item.output_dds_path)
            output_image = None
            if output_result.preview_png_path and not self.stop_event.is_set():
                reader = QImageReader(output_result.preview_png_path)
                image = reader.read()
                if not image.isNull():
                    output_image = image
            payload = {
                "item": self.item,
                "source_preview_path": source_preview_path,
                "source_meta": source_meta,
                "source_detail": source_detail,
                "source_image": source_image,
                "source_metadata": source_metadata,
                "original_metadata": original_metadata,
                "output_result": output_result,
                "output_image": output_image,
                "output_metadata": output_metadata,
                "comparison_rows": self._build_comparison_rows(
                    source_metadata=source_metadata,
                    original_metadata=original_metadata,
                    output_metadata=output_metadata,
                ),
            }
            if not self.stop_event.is_set():
                self.completed.emit(self.request_id, payload)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantUIConstraintWorker(QObject):
    completed = Signal(int, str, str)
    error = Signal(int, str)
    finished = Signal()

    def __init__(self, request_id: int, entries: Sequence[ArchiveEntry], target_path: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.entries = entries
        self.target_path = target_path
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.stop_event.is_set():
                return
            summary = summarize_ui_reference_constraints(self.entries, self.target_path, stop_event=self.stop_event)
            if not self.stop_event.is_set():
                self.completed.emit(self.request_id, self.target_path, str(summary.get("warning_text", "") or ""))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error.emit(self.request_id, str(exc))
        finally:
            self.finished.emit()


class ReplaceAssistantReviewDialog(QDialog):
    def __init__(self, texconv_path: Optional[Path], review_items: Sequence[ReplaceAssistantReviewItem], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.texconv_path = texconv_path
        self.review_items = list(review_items)
        self.request_id = 0
        self.worker: Optional[ReplaceAssistantReviewCompareWorker] = None
        self.thread: Optional[QThread] = None
        self.pending_item: Optional[ReplaceAssistantReviewItem] = None

        self.setWindowTitle("Replace Assistant Review")
        self.resize(1320, 820)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)

        queue_group = QGroupBox("Built Items")
        queue_layout = QVBoxLayout(queue_group)
        queue_layout.setContentsMargins(10, 12, 10, 10)
        queue_layout.setSpacing(8)
        queue_hint = QLabel("Review each rebuilt DDS against the edited source before shipping the package. Select a built item to load its previews.")
        queue_hint.setWordWrap(True)
        queue_hint.setObjectName("HintLabel")
        queue_layout.addWidget(queue_hint)
        self.item_list = QListWidget()
        self.item_list.setMinimumWidth(280)
        self.item_list.setAlternatingRowColors(True)
        self.item_list.setTextElideMode(Qt.ElideMiddle)
        for item in self.review_items:
            list_item = QListWidgetItem(item.relative_path.as_posix(), self.item_list)
            list_item.setToolTip(item.relative_path.as_posix())
        queue_layout.addWidget(self.item_list, stretch=1)
        root_layout.addWidget(queue_group, stretch=0)

        compare_splitter = QSplitter(Qt.Horizontal)
        compare_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(compare_splitter, stretch=1)

        source_panel = QGroupBox("Edited Input")
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(10, 12, 10, 10)
        source_layout.setSpacing(8)
        self.source_title = QLabel("Edited input")
        self.source_title.setWordWrap(True)
        self.source_meta = QLabel("")
        self.source_meta.setWordWrap(True)
        self.source_meta.setObjectName("HintLabel")
        self.source_label = PreviewLabel("Select a built item to review.")
        self.source_label.setMinimumSize(360, 360)
        self.source_scroll = PreviewScrollArea()
        self.source_scroll.setWidgetResizable(False)
        self.source_scroll.setAlignment(Qt.AlignCenter)
        self.source_scroll.setWidget(self.source_label)
        self.source_label.attach_scroll_area(self.source_scroll)
        source_layout.addWidget(self.source_title)
        source_layout.addWidget(self.source_meta)
        source_layout.addWidget(self.source_scroll, stretch=1)
        compare_splitter.addWidget(source_panel)

        output_panel = QGroupBox("Rebuilt DDS Review")
        output_layout = QVBoxLayout(output_panel)
        output_layout.setContentsMargins(10, 12, 10, 10)
        output_layout.setSpacing(8)
        self.output_title = QLabel("Rebuilt DDS")
        self.output_title.setWordWrap(True)
        self.output_meta = QLabel("")
        self.output_meta.setWordWrap(True)
        self.output_meta.setObjectName("HintLabel")
        self.output_label = PreviewLabel("Select a built item to review.")
        self.output_label.setMinimumSize(360, 360)
        self.output_scroll = PreviewScrollArea()
        self.output_scroll.setWidgetResizable(False)
        self.output_scroll.setAlignment(Qt.AlignCenter)
        self.output_scroll.setWidget(self.output_label)
        self.output_label.attach_scroll_area(self.output_scroll)
        self.metadata_browser = QTextBrowser()
        self.metadata_browser.setOpenExternalLinks(False)
        self.metadata_browser.setMaximumHeight(220)
        self.metadata_browser.setPlaceholderText("Reference and rebuild metadata appear here.")
        self.details_browser = QTextBrowser()
        self.details_browser.setOpenExternalLinks(False)
        self.details_browser.setPlaceholderText("Detailed paths and notes appear here.")
        output_layout.addWidget(self.output_title)
        output_layout.addWidget(self.output_meta)
        output_layout.addWidget(self.output_scroll, stretch=1)
        output_layout.addWidget(self.metadata_browser, stretch=0)
        output_layout.addWidget(self.details_browser, stretch=1)
        compare_splitter.addWidget(output_panel)
        compare_splitter.setSizes(build_responsive_splitter_sizes(1480, [47, 53], [320, 360]))

        self.item_list.currentRowChanged.connect(self._handle_row_changed)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_worker()
        super().closeEvent(event)

    def _stop_worker(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(2000)
        self.worker = None
        self.thread = None

    def _handle_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.review_items):
            return
        item = self.review_items[row]
        self.request_id += 1
        request_id = self.request_id
        self.source_title.setText(item.source_path.name)
        self.source_meta.setText("Preparing edited input preview...")
        self.output_title.setText(item.output_dds_path.name)
        self.output_meta.setText("Preparing rebuilt DDS preview...")
        self.metadata_browser.setHtml("<p>Preparing metadata...</p>")
        self.details_browser.setHtml(f"<p>{escape(item.relative_path.as_posix())}</p>")
        if self.thread is not None:
            self.pending_item = item
            if self.worker is not None:
                self.worker.stop()
            return
        worker = ReplaceAssistantReviewCompareWorker(request_id, self.texconv_path, item)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_payload_ready)
        worker.error.connect(self._handle_payload_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_worker_refs)
        self.worker = worker
        self.thread = thread
        thread.start()

    def _cleanup_worker_refs(self) -> None:
        self.thread = None
        self.worker = None
        if self.pending_item is not None:
            pending = self.pending_item
            self.pending_item = None
            try:
                row = self.review_items.index(pending)
            except ValueError:
                return
            self.item_list.setCurrentRow(row)

    def _format_metadata_block(self, title: str, metadata: Optional[Dict[str, str]]) -> str:
        if not metadata:
            return f"<h3>{escape(title)}</h3><p>Unavailable.</p>"
        rows = []
        for label in ("format", "size", "mips", "kind"):
            value = metadata.get(label)
            if value:
                pretty_label = {
                    "format": "Format",
                    "size": "Size",
                    "mips": "Mips",
                    "kind": "Kind",
                }[label]
                rows.append(f"<tr><td><b>{pretty_label}</b></td><td>{escape(value)}</td></tr>")
        return (
            f"<h3>{escape(title)}</h3>"
            "<table cellspacing='6' cellpadding='0'>"
            + "".join(rows)
            + "</table>"
        )

    def _format_comparison_html(
        self,
        source_metadata: Dict[str, str],
        original_metadata: Optional[Dict[str, str]],
        output_metadata: Optional[Dict[str, str]],
        comparison_rows: Sequence[tuple[str, str]],
    ) -> str:
        parts = [
            "<html><body>",
            self._format_metadata_block("Edited Source", source_metadata),
            self._format_metadata_block("Original Reference DDS", original_metadata),
            self._format_metadata_block("Rebuilt DDS", output_metadata),
            "<h3>Comparison</h3>",
            "<table cellspacing='6' cellpadding='0'>",
        ]
        for label, value in comparison_rows:
            parts.append(f"<tr><td><b>{escape(label)}</b></td><td>{escape(value)}</td></tr>")
        parts.append("</table></body></html>")
        return "".join(parts)

    def _format_details_html(self, item: ReplaceAssistantReviewItem, source_detail: str) -> str:
        rows = [
            ("Package relative path", item.relative_path.as_posix()),
            ("Edited source", str(item.source_path)),
            ("Original reference DDS", str(item.original_dds_path) if item.original_dds_path is not None else "Unavailable"),
            ("Built DDS", str(item.output_dds_path)),
        ]
        html_rows = "".join(
            f"<tr><td><b>{escape(label)}</b></td><td>{escape(value)}</td></tr>"
            for label, value in rows
        )
        notes = escape(source_detail or "")
        notes = notes.replace("\n", "<br>")
        return (
            "<html><body>"
            "<h3>Paths</h3>"
            "<table cellspacing='6' cellpadding='0'>"
            f"{html_rows}"
            "</table>"
            "<h3>Notes</h3>"
            f"<p>{notes}</p>"
            "</body></html>"
        )

    def _handle_payload_ready(self, request_id: int, payload: object) -> None:
        if request_id != self.request_id or not isinstance(payload, dict):
            return
        item = payload.get("item")
        if not isinstance(item, ReplaceAssistantReviewItem):
            return
        self.source_title.setText(item.source_path.name)
        source_metadata = payload.get("source_metadata") if isinstance(payload.get("source_metadata"), dict) else {}
        source_meta_text = str(payload.get("source_meta", "") or "")
        if source_metadata:
            summary_parts = []
            if source_metadata.get("kind"):
                summary_parts.append(source_metadata["kind"])
            if source_metadata.get("size"):
                summary_parts.append(source_metadata["size"])
            if source_metadata.get("format"):
                summary_parts.append(source_metadata["format"])
            if source_meta_text:
                summary_parts.append(source_meta_text)
            self.source_meta.setText(" | ".join(summary_parts))
        else:
            self.source_meta.setText(source_meta_text)
        source_image = payload.get("source_image")
        source_preview_path = str(payload.get("source_preview_path", "") or "")
        if source_image is not None:
            self.source_label.set_preview_image(source_image, item.source_path.name)
        elif source_preview_path:
            self.source_label.set_preview_image_path(source_preview_path, item.source_path.name)
        else:
            self.source_label.clear_preview("No input preview available.")
        output_result = payload.get("output_result")
        output_image = payload.get("output_image")
        if isinstance(output_result, object) and hasattr(output_result, "title"):
            self.output_title.setText(getattr(output_result, "title", item.output_dds_path.name) or item.output_dds_path.name)
            self.output_meta.setText(getattr(output_result, "metadata_summary", "") or getattr(output_result, "message", ""))
            if output_image is not None:
                self.output_label.set_preview_image(output_image, self.output_title.text())
            elif getattr(output_result, "preview_png_path", ""):
                self.output_label.set_preview_image_path(getattr(output_result, "preview_png_path"), self.output_title.text())
            else:
                self.output_label.clear_preview(getattr(output_result, "message", "No rebuilt DDS preview available."))
        original_metadata = payload.get("original_metadata") if isinstance(payload.get("original_metadata"), dict) else None
        output_metadata = payload.get("output_metadata") if isinstance(payload.get("output_metadata"), dict) else None
        comparison_rows = payload.get("comparison_rows") if isinstance(payload.get("comparison_rows"), list) else []
        self.metadata_browser.setHtml(
            self._format_comparison_html(
                source_metadata=source_metadata,
                original_metadata=original_metadata,
                output_metadata=output_metadata,
                comparison_rows=comparison_rows,
            )
        )
        self.details_browser.setHtml(self._format_details_html(item, str(payload.get("source_detail", ""))))

    def _handle_payload_error(self, request_id: int, message: str) -> None:
        if request_id != self.request_id:
            return
        self.source_meta.setText(message)
        self.output_meta.setText(message)
        self.source_label.clear_preview("Preview failed.")
        self.output_label.clear_preview("Preview failed.")
        self.metadata_browser.setHtml(f"<p>{escape(message)}</p>")
        self.details_browser.setHtml(f"<p>{escape(message)}</p>")


class ReplaceAssistantTab(QWidget):
    status_message_requested = Signal(str, bool)
    open_in_texture_editor_requested = Signal(str, object)

    def __init__(
        self,
        *,
        settings: QSettings,
        base_dir: Path,
        get_archive_entries: Callable[[], Sequence[ArchiveEntry]],
        get_original_root: Callable[[], str],
        get_texconv_path: Callable[[], str],
        get_current_config: Callable[[], object],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.base_dir = base_dir
        self.get_archive_entries = get_archive_entries
        self.get_original_root = get_original_root
        self.get_texconv_path = get_texconv_path
        self.get_current_config = get_current_config

        self.archive_entries: List[ArchiveEntry] = []
        self.archive_index: ReplaceAssistantArchiveIndex = build_replace_assistant_archive_index([])
        self.archive_index_original_root: Optional[Path] = None
        self.items: List[ReplaceAssistantItem] = []
        self.last_built_output_root: Optional[Path] = None
        self.review_dialog: Optional[ReplaceAssistantReviewDialog] = None
        self.external_busy = False
        self._settings_ready = False
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(250)
        self._settings_save_timer.timeout.connect(self._save_settings)
        self.preview_thread: Optional[QThread] = None
        self.preview_worker: Optional[ReplaceAssistantPreviewWorker] = None
        self.preview_request_id = 0
        self.pending_preview_item: Optional[ReplaceAssistantItem] = None
        self.preview_refresh_suspended = False
        self._pending_import_select_path: str = ""
        self.import_thread: Optional[QThread] = None
        self.import_worker: Optional[ReplaceAssistantImportWorker] = None
        self.match_thread: Optional[QThread] = None
        self.match_worker: Optional[ReplaceAssistantAutoMatchWorker] = None
        self.ui_constraint_thread: Optional[QThread] = None
        self.ui_constraint_worker: Optional[ReplaceAssistantUIConstraintWorker] = None
        self.ui_constraint_request_id = 0
        self._active_ui_constraint_target: str = ""
        self._pending_ui_constraint_target: str = ""
        self.build_thread: Optional[QThread] = None
        self.build_worker: Optional[ReplaceAssistantBuildWorker] = None
        self.pending_review_items: Optional[tuple[ReplaceAssistantReviewItem, ...]] = None
        self._ui_constraint_warning_cache: Dict[str, str] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(6)

        self.summary_label = QLabel("No files imported yet.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("HintLabel")

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.add_files_button = QPushButton("Add Files")
        self.add_folder_button = QPushButton("Add Folder")
        self.auto_match_button = QPushButton("Auto-Match")
        self.open_in_editor_button = QPushButton("Open In Texture Editor")
        self.choose_local_original_button = QPushButton("Choose Local Original")
        self.choose_archive_original_button = QPushButton("Choose Archive Original")
        self.remove_selected_button = QPushButton("Remove Selected")
        self.clear_all_button = QPushButton("Clear All")
        button_row.addWidget(self.add_files_button)
        button_row.addWidget(self.add_folder_button)
        button_row.addWidget(self.auto_match_button)
        button_row.addWidget(self.open_in_editor_button)
        button_row.addWidget(self.choose_local_original_button)
        button_row.addWidget(self.choose_archive_original_button)
        button_row.addWidget(self.remove_selected_button)
        button_row.addWidget(self.clear_all_button)
        button_row.addStretch(1)
        root_layout.addLayout(button_row)
        root_layout.addWidget(self.summary_label)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(8)
        root_layout.addWidget(self.main_splitter, stretch=1)

        self.queue_panel = QWidget()
        queue_layout = QVBoxLayout(self.queue_panel)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(8)
        queue_group = FlatSectionPanel("Replace Queue")
        queue_group_layout = queue_group.body_layout
        self.queue_tree = QTreeWidget()
        self.queue_tree.setRootIsDecorated(False)
        self.queue_tree.setAlternatingRowColors(True)
        self.queue_tree.setUniformRowHeights(True)
        self.queue_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.queue_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.queue_tree.setTextElideMode(Qt.ElideMiddle)
        self.queue_tree.setHeaderLabels(["Edited File", "Original", "Package", "Kind", "Status"])
        queue_header = self.queue_tree.header()
        queue_header.setStretchLastSection(False)
        queue_header.setSectionsMovable(True)
        queue_header.setSectionsClickable(True)
        queue_header.setMinimumSectionSize(72)
        queue_header.setSectionResizeMode(0, QHeaderView.Interactive)
        queue_header.setSectionResizeMode(1, QHeaderView.Interactive)
        queue_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        queue_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        queue_header.setSectionResizeMode(4, QHeaderView.Interactive)
        queue_header.resizeSection(0, 320)
        queue_header.resizeSection(1, 260)
        queue_header.resizeSection(2, 90)
        queue_header.resizeSection(3, 70)
        queue_header.resizeSection(4, 220)
        self.queue_tree.setToolTip(
            "Columns can be resized or reordered. Use the horizontal scrollbar when the queue is narrower than the full column set."
        )
        queue_group_layout.addWidget(self.queue_tree)
        queue_layout.addWidget(queue_group)
        self.main_splitter.addWidget(self.queue_panel)

        self.preview_panel = QWidget()
        preview_layout = QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(8)
        preview_group = FlatSectionPanel("Preview")
        preview_group_layout = preview_group.body_layout
        preview_title_row = QHBoxLayout()
        preview_title_row.setSpacing(8)
        self.preview_title_label = QLabel("Select an imported file")
        self.preview_title_label.setWordWrap(True)
        self.preview_zoom_out_button = QPushButton("-")
        self.preview_zoom_fit_button = QPushButton("Fit")
        self.preview_zoom_100_button = QPushButton("100%")
        self.preview_zoom_in_button = QPushButton("+")
        self.preview_zoom_value = QLabel("-")
        self.preview_zoom_value.setObjectName("HintLabel")
        preview_title_row.addWidget(self.preview_title_label, stretch=1)
        preview_title_row.addWidget(self.preview_zoom_out_button)
        preview_title_row.addWidget(self.preview_zoom_fit_button)
        preview_title_row.addWidget(self.preview_zoom_100_button)
        preview_title_row.addWidget(self.preview_zoom_in_button)
        preview_title_row.addWidget(self.preview_zoom_value)
        preview_group_layout.addLayout(preview_title_row)
        self.preview_meta_label = QLabel("Select a file to preview it here.")
        self.preview_meta_label.setWordWrap(True)
        self.preview_meta_label.setObjectName("HintLabel")
        preview_group_layout.addWidget(self.preview_meta_label)
        self.preview_warning_label = QLabel("")
        self.preview_warning_label.setWordWrap(True)
        self.preview_warning_label.setObjectName("WarningText")
        self.preview_warning_label.setVisible(False)
        preview_group_layout.addWidget(self.preview_warning_label)
        self.preview_label = PreviewLabel("Select a file to preview it here.")
        self.preview_label.setMinimumHeight(320)
        self.preview_label.setMinimumWidth(320)
        self.preview_scroll = PreviewScrollArea()
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignCenter)
        self.preview_scroll.setWidget(self.preview_label)
        self.preview_label.attach_scroll_area(self.preview_scroll)
        self.preview_label.set_wheel_zoom_handler(self._adjust_preview_zoom)
        preview_group_layout.addWidget(self.preview_scroll, stretch=1)
        self.preview_details_edit = QPlainTextEdit()
        self.preview_details_edit.setReadOnly(True)
        self.preview_details_edit.setPlaceholderText("Selected item details appear here.")
        preview_group_layout.addWidget(self.preview_details_edit)
        preview_layout.addWidget(preview_group, stretch=1)
        self.main_splitter.addWidget(self.preview_panel)

        self.settings_panel = QWidget()
        self.settings_panel.setMinimumWidth(420)
        settings_layout = QVBoxLayout(self.settings_panel)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(8)

        build_group = FlatSectionPanel("Build Settings", body_margins=(10, 10, 10, 10), body_spacing=0)
        build_layout = QGridLayout()
        build_layout.setHorizontalSpacing(10)
        build_layout.setVerticalSpacing(8)
        build_layout.setColumnMinimumWidth(0, 136)
        build_layout.setColumnStretch(1, 1)

        self.build_mode_combo = QComboBox()
        self.build_mode_combo.addItem("Rebuild only", "rebuild_only")
        self.build_mode_combo.addItem("Upscale with NCNN, then rebuild", "upscale_then_rebuild")
        self.size_mode_combo = QComboBox()
        self.size_mode_combo.addItem("Use edited size", "use_edited_size")
        self.size_mode_combo.addItem("Match original size", "match_original")
        self.package_output_root_edit = QLineEdit(str((self.base_dir / "replace_assistant_export").resolve()))
        self.package_output_browse_button = QPushButton("Browse")
        self.overwrite_package_checkbox = QCheckBox("Overwrite existing package files")
        self.overwrite_package_checkbox.setChecked(True)
        self.create_no_encrypt_checkbox = QCheckBox("Create .no_encrypt file")
        self.create_no_encrypt_checkbox.setChecked(True)
        self.build_package_button = QPushButton("Build Package")
        self.open_output_folder_button = QPushButton("Open Output Folder")
        self.mirror_workflow_button = QPushButton("Mirror Texture Workflow")

        build_layout.addWidget(QLabel("Build mode"), 0, 0)
        build_layout.addWidget(self.build_mode_combo, 0, 1)
        build_layout.addWidget(QLabel("Size mode"), 1, 0)
        build_layout.addWidget(self.size_mode_combo, 1, 1)
        build_layout.addWidget(QLabel("Package parent root"), 2, 0)
        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(8)
        output_row.addWidget(self.package_output_root_edit, stretch=1)
        output_row.addWidget(self.package_output_browse_button)
        build_layout.addLayout(output_row, 2, 1)
        build_layout.addWidget(self.overwrite_package_checkbox, 3, 0, 1, 2)
        build_layout.addWidget(self.create_no_encrypt_checkbox, 4, 0, 1, 2)
        build_layout.addWidget(self.mirror_workflow_button, 5, 0)
        build_layout.addWidget(self.build_package_button, 5, 1)
        build_layout.addWidget(self.open_output_folder_button, 6, 0, 1, 2)
        build_group.body_layout.addLayout(build_layout)
        settings_layout.addWidget(build_group)

        package_group = FlatSectionPanel("Package Info", body_margins=(10, 10, 10, 10), body_spacing=0)
        package_layout = QGridLayout()
        package_layout.setHorizontalSpacing(10)
        package_layout.setVerticalSpacing(8)
        package_layout.setColumnMinimumWidth(0, 110)
        package_layout.setColumnStretch(1, 1)
        self.package_title_edit = QLineEdit("Crimson Forge Toolkit Mod")
        self.package_version_edit = QLineEdit("1.0")
        self.package_author_edit = QLineEdit("")
        self.package_description_edit = QLineEdit("")
        self.package_nexus_edit = QLineEdit("")
        package_layout.addWidget(QLabel("Title"), 0, 0)
        package_layout.addWidget(self.package_title_edit, 0, 1)
        package_layout.addWidget(QLabel("Version"), 1, 0)
        package_layout.addWidget(self.package_version_edit, 1, 1)
        package_layout.addWidget(QLabel("Author"), 2, 0)
        package_layout.addWidget(self.package_author_edit, 2, 1)
        package_layout.addWidget(QLabel("Description"), 3, 0)
        package_layout.addWidget(self.package_description_edit, 3, 1)
        package_layout.addWidget(QLabel("Nexus URL"), 4, 0)
        package_layout.addWidget(self.package_nexus_edit, 4, 1)
        package_group.body_layout.addLayout(package_layout)
        settings_layout.addWidget(package_group)

        self.ncnn_group = FlatSectionPanel("Direct Upscale Controls (NCNN only)", body_margins=(10, 10, 10, 10), body_spacing=0)
        ncnn_layout = QGridLayout()
        ncnn_layout.setHorizontalSpacing(10)
        ncnn_layout.setVerticalSpacing(8)
        ncnn_layout.setColumnMinimumWidth(0, 136)
        ncnn_layout.setColumnStretch(1, 1)
        self.ncnn_exe_path_edit = QLineEdit()
        self.ncnn_model_dir_edit = QLineEdit(str((self.base_dir / "tools" / "realesrgan_ncnn" / "models").resolve()))
        self.ncnn_model_combo = QComboBox()
        self.ncnn_refresh_models_button = QPushButton("Refresh Models")
        self.ncnn_scale_spin = QSpinBox()
        self.ncnn_scale_spin.setRange(1, 8)
        self.ncnn_scale_spin.setValue(REALESRGAN_NCNN_SCALE)
        self.ncnn_tile_size_spin = QSpinBox()
        self.ncnn_tile_size_spin.setRange(0, 32768)
        self.ncnn_tile_size_spin.setSingleStep(32)
        self.ncnn_tile_size_spin.setValue(REALESRGAN_NCNN_TILE_SIZE)
        self.ncnn_extra_args_edit = QLineEdit(REALESRGAN_NCNN_EXTRA_ARGS)
        self.upscale_post_correction_combo = QComboBox()
        self._add_combo_choice(self.upscale_post_correction_combo, "Off", UPSCALE_POST_CORRECTION_NONE)
        self._add_combo_choice(self.upscale_post_correction_combo, "Match Mean Luma", UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA)
        self._add_combo_choice(self.upscale_post_correction_combo, "Match Levels", UPSCALE_POST_CORRECTION_MATCH_LEVELS)
        self._add_combo_choice(self.upscale_post_correction_combo, "Match Histogram", UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM)
        self._add_combo_choice(
            self.upscale_post_correction_combo,
            "Source Match Balanced (recommended)",
            UPSCALE_POST_CORRECTION_SOURCE_MATCH_BALANCED,
        )
        self._add_combo_choice(self.upscale_post_correction_combo, "Source Match Extended", UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXTENDED)
        self._add_combo_choice(self.upscale_post_correction_combo, "Source Match Experimental", UPSCALE_POST_CORRECTION_SOURCE_MATCH_EXPERIMENTAL)
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

        ncnn_layout.addWidget(QLabel("NCNN exe path"), 0, 0)
        exe_row = QHBoxLayout()
        exe_row.setContentsMargins(0, 0, 0, 0)
        exe_row.setSpacing(8)
        self.ncnn_exe_browse_button = QPushButton("Browse")
        exe_row.addWidget(self.ncnn_exe_path_edit, stretch=1)
        exe_row.addWidget(self.ncnn_exe_browse_button)
        ncnn_layout.addLayout(exe_row, 0, 1)
        ncnn_layout.addWidget(QLabel("Model folder"), 1, 0)
        model_dir_row = QHBoxLayout()
        model_dir_row.setContentsMargins(0, 0, 0, 0)
        model_dir_row.setSpacing(8)
        self.ncnn_model_dir_browse_button = QPushButton("Browse")
        model_dir_row.addWidget(self.ncnn_model_dir_edit, stretch=1)
        model_dir_row.addWidget(self.ncnn_model_dir_browse_button)
        ncnn_layout.addLayout(model_dir_row, 1, 1)
        ncnn_layout.addWidget(QLabel("Model"), 2, 0)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(self.ncnn_model_combo, stretch=1)
        model_row.addWidget(self.ncnn_refresh_models_button)
        ncnn_layout.addLayout(model_row, 2, 1)
        ncnn_layout.addWidget(QLabel("Scale"), 3, 0)
        ncnn_layout.addWidget(self.ncnn_scale_spin, 3, 1)
        ncnn_layout.addWidget(QLabel("Tile size"), 4, 0)
        ncnn_layout.addWidget(self.ncnn_tile_size_spin, 4, 1)
        ncnn_layout.addWidget(QLabel("NCNN extra args"), 5, 0)
        ncnn_layout.addWidget(self.ncnn_extra_args_edit, 5, 1)
        ncnn_layout.addWidget(QLabel("Post correction"), 6, 0)
        ncnn_layout.addWidget(self.upscale_post_correction_combo, 6, 1)
        ncnn_layout.addWidget(QLabel("Texture preset"), 7, 0)
        ncnn_layout.addWidget(self.upscale_texture_preset_combo, 7, 1)
        ncnn_layout.addWidget(self.enable_automatic_texture_rules_checkbox, 8, 0, 1, 2)
        ncnn_layout.addWidget(self.enable_unsafe_technical_override_checkbox, 9, 0, 1, 2)
        ncnn_layout.addWidget(self.retry_smaller_tile_checkbox, 10, 0, 1, 2)
        self.ncnn_group.body_layout.addLayout(ncnn_layout)
        settings_layout.addWidget(self.ncnn_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        self.progress_bar.setMaximumHeight(18)
        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        settings_layout.addWidget(self.progress_bar)
        settings_layout.addWidget(self.status_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setPlaceholderText("Replace Assistant log will appear here.")
        settings_layout.addWidget(self.log_view, stretch=1)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(self.settings_panel)
        self.settings_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.settings_scroll.setMinimumWidth(420)
        self.main_splitter.addWidget(self.settings_scroll)
        self.queue_panel.setMinimumWidth(280)
        self.preview_panel.setMinimumWidth(360)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes(build_responsive_splitter_sizes(1800, [24, 34, 42], [280, 360, 420]))

        self.add_files_button.clicked.connect(self.import_files)
        self.add_folder_button.clicked.connect(self.import_folder)
        self.auto_match_button.clicked.connect(self.auto_match_all_items)
        self.open_in_editor_button.clicked.connect(self.open_current_item_in_texture_editor)
        self.choose_local_original_button.clicked.connect(self.choose_local_original_for_selected)
        self.choose_archive_original_button.clicked.connect(self.choose_archive_original_for_selected)
        self.remove_selected_button.clicked.connect(self.remove_selected_items)
        self.clear_all_button.clicked.connect(self.clear_all_items)
        self.build_package_button.clicked.connect(self.start_build)
        self.open_output_folder_button.clicked.connect(self.open_output_folder)
        self.package_output_browse_button.clicked.connect(self._browse_package_output_root)
        self.mirror_workflow_button.clicked.connect(self.mirror_texture_workflow_settings)
        self.queue_tree.currentItemChanged.connect(self._handle_selection_changed)
        self.queue_tree.itemSelectionChanged.connect(self._update_controls)
        self.preview_zoom_out_button.clicked.connect(lambda: self._adjust_preview_zoom(-1))
        self.preview_zoom_fit_button.clicked.connect(lambda: self._set_preview_fit(True))
        self.preview_zoom_100_button.clicked.connect(lambda: self._set_preview_zoom_factor(1.0))
        self.preview_zoom_in_button.clicked.connect(lambda: self._adjust_preview_zoom(1))
        self.ncnn_exe_browse_button.clicked.connect(self._browse_ncnn_exe)
        self.ncnn_model_dir_browse_button.clicked.connect(self._browse_ncnn_model_dir)
        self.ncnn_refresh_models_button.clicked.connect(self.refresh_ncnn_models)
        self.build_mode_combo.currentIndexChanged.connect(self._sync_build_mode_visibility)
        for widget in (
            self.build_mode_combo,
            self.size_mode_combo,
            self.package_output_root_edit,
            self.overwrite_package_checkbox,
            self.create_no_encrypt_checkbox,
            self.package_title_edit,
            self.package_version_edit,
            self.package_author_edit,
            self.package_description_edit,
            self.package_nexus_edit,
            self.ncnn_exe_path_edit,
            self.ncnn_model_dir_edit,
            self.ncnn_scale_spin,
            self.ncnn_tile_size_spin,
            self.ncnn_extra_args_edit,
            self.upscale_post_correction_combo,
            self.upscale_texture_preset_combo,
            self.enable_automatic_texture_rules_checkbox,
            self.enable_unsafe_technical_override_checkbox,
            self.retry_smaller_tile_checkbox,
        ):
            if hasattr(widget, "textChanged"):
                widget.textChanged.connect(self.schedule_settings_save)  # type: ignore[attr-defined]
            elif hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self.schedule_settings_save)  # type: ignore[attr-defined]
            elif hasattr(widget, "currentIndexChanged"):
                widget.currentIndexChanged.connect(self.schedule_settings_save)  # type: ignore[attr-defined]
            elif hasattr(widget, "toggled"):
                widget.toggled.connect(self.schedule_settings_save)  # type: ignore[attr-defined]

        self._refresh_ncnn_models()
        self._load_settings()
        self._settings_ready = True
        self._sync_build_mode_visibility()
        self._update_summary()
        self._update_controls()
        QTimer.singleShot(0, self._apply_responsive_splitter_defaults)

    def _apply_responsive_splitter_defaults(self) -> None:
        total_width = max(self.width() - 32, sum([280, 360, 420]))
        self.main_splitter.setSizes(
            build_responsive_splitter_sizes(total_width, [24, 34, 42], [280, 360, 420])
        )

    def set_splitter_sizes(self, sizes: Sequence[int], *, total_width: Optional[int] = None) -> None:
        if not sizes:
            return
        available_width = total_width or max(self.width() - 32, sum([280, 360, 420]))
        self.main_splitter.setSizes(
            clamp_splitter_sizes(
                available_width,
                sizes,
                [280, 360, 420],
                fallback_weights=[24, 34, 42],
            )
        )

    def splitter_sizes(self) -> List[int]:
        return self.main_splitter.sizes()

    def apply_responsive_splitter_sizes(self, total_width: Optional[int] = None) -> None:
        available_width = total_width or max(self.width() - 32, sum([280, 360, 420]))
        self.main_splitter.setSizes(
            build_responsive_splitter_sizes(available_width, [24, 34, 42], [280, 360, 420])
        )

    def auto_fit_columns(self) -> None:
        header = self.queue_tree.header()
        if header is None or self.queue_tree.columnCount() <= 0:
            return
        viewport_width = max(self.queue_tree.viewport().width(), self.queue_tree.width() - 24, 0)
        if viewport_width <= 0:
            return
        minimums = {
            0: 260,
            1: 220,
            2: 96,
            3: 72,
            4: 160,
        }
        self.queue_tree.setUpdatesEnabled(False)
        try:
            for column in (2, 3):
                self.queue_tree.resizeColumnToContents(column)
                header.resizeSection(column, max(minimums[column], header.sectionSize(column)))
            reserved = header.sectionSize(2) + header.sectionSize(3)
            remaining = max(0, viewport_width - reserved - 12)
            preferred = {
                0: max(minimums[0], int(remaining * 0.42)),
                1: max(minimums[1], int(remaining * 0.31)),
                4: max(minimums[4], remaining - int(remaining * 0.42) - int(remaining * 0.31)),
            }
            for column in (0, 1, 4):
                header.resizeSection(column, preferred[column])
        finally:
            self.queue_tree.setUpdatesEnabled(True)

    def _add_combo_choice(self, combo: QComboBox, label: str, value: str) -> None:
        combo.addItem(label, value)

    def _combo_value(self, combo: QComboBox) -> str:
        data = combo.currentData()
        return str(data) if data is not None else ""

    def _set_combo_by_value(self, combo: QComboBox, value: str) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def _current_original_root_path(self) -> Optional[Path]:
        original_root_text = self.get_original_root().strip()
        return Path(original_root_text).expanduser() if original_root_text else None

    def _ensure_archive_index_current(self) -> ReplaceAssistantArchiveIndex:
        current_original_root = self._current_original_root_path()
        active_entries = self.archive_entries or list(self.get_archive_entries())
        self.archive_entries = list(active_entries)
        entries_missing_from_index = bool(active_entries) and not self.archive_index.entries_by_relative_path
        root_changed = self.archive_index_original_root != current_original_root
        if entries_missing_from_index or root_changed:
            self.archive_entries = list(active_entries)
            self.archive_index = build_replace_assistant_archive_index(
                self.archive_entries,
                original_dds_root=current_original_root,
            )
            self.archive_index_original_root = current_original_root
        return self.archive_index

    def set_archive_entries(self, entries: Sequence[ArchiveEntry], package_root_text: str = "") -> None:
        self.archive_entries = entries if isinstance(entries, list) else list(entries)
        del package_root_text
        self.archive_index = build_replace_assistant_archive_index([])
        self.archive_index_original_root = None
        self._update_summary()

    def set_external_busy(self, busy: bool) -> None:
        self.external_busy = busy
        self._update_controls()

    def is_busy(self) -> bool:
        return (
            self.preview_thread is not None
            or self.import_thread is not None
            or self.match_thread is not None
            or self.build_thread is not None
        )

    def shutdown(self) -> None:
        if self.review_dialog is not None:
            self.review_dialog.close()
            self.review_dialog = None
        if self.preview_worker is not None:
            self.preview_worker.stop()
        if self.import_worker is not None:
            self.import_worker.stop()
        if self.match_worker is not None:
            self.match_worker.stop()
        if self.ui_constraint_worker is not None:
            self.ui_constraint_worker.stop()
        if self.build_worker is not None:
            self.build_worker.stop()
        for thread in (self.preview_thread, self.import_thread, self.match_thread, self.ui_constraint_thread, self.build_thread):
            _shutdown_thread(thread)

    def schedule_settings_save(self) -> None:
        if not self._settings_ready:
            return
        self._settings_save_timer.start()

    def flush_settings_save(self) -> None:
        if self._settings_ready:
            self._save_settings()

    def _save_settings(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("replace_assistant/build_mode", self._combo_value(self.build_mode_combo))
        self.settings.setValue("replace_assistant/size_mode", self._combo_value(self.size_mode_combo))
        self.settings.setValue("replace_assistant/package_output_root", self.package_output_root_edit.text())
        self.settings.setValue("replace_assistant/overwrite_existing", self.overwrite_package_checkbox.isChecked())
        self.settings.setValue("replace_assistant/create_no_encrypt", self.create_no_encrypt_checkbox.isChecked())
        self.settings.setValue("replace_assistant/package_title", self.package_title_edit.text())
        self.settings.setValue("replace_assistant/package_version", self.package_version_edit.text())
        self.settings.setValue("replace_assistant/package_author", self.package_author_edit.text())
        self.settings.setValue("replace_assistant/package_description", self.package_description_edit.text())
        self.settings.setValue("replace_assistant/package_nexus", self.package_nexus_edit.text())
        self.settings.setValue("replace_assistant/ncnn_exe_path", self.ncnn_exe_path_edit.text())
        self.settings.setValue("replace_assistant/ncnn_model_dir", self.ncnn_model_dir_edit.text())
        self.settings.setValue("replace_assistant/ncnn_model_name", self._combo_value(self.ncnn_model_combo))
        self.settings.setValue("replace_assistant/ncnn_scale", self.ncnn_scale_spin.value())
        self.settings.setValue("replace_assistant/ncnn_tile_size", self.ncnn_tile_size_spin.value())
        self.settings.setValue("replace_assistant/ncnn_extra_args", self.ncnn_extra_args_edit.text())
        self.settings.setValue("replace_assistant/post_correction", self._combo_value(self.upscale_post_correction_combo))
        self.settings.setValue("replace_assistant/texture_preset", self._combo_value(self.upscale_texture_preset_combo))
        self.settings.setValue("replace_assistant/automatic_rules", self.enable_automatic_texture_rules_checkbox.isChecked())
        self.settings.setValue("replace_assistant/unsafe_override", self.enable_unsafe_technical_override_checkbox.isChecked())
        self.settings.setValue("replace_assistant/retry_smaller_tile", self.retry_smaller_tile_checkbox.isChecked())

    def _load_settings(self) -> None:
        self._set_combo_by_value(self.build_mode_combo, str(self.settings.value("replace_assistant/build_mode", "rebuild_only")))
        self._set_combo_by_value(self.size_mode_combo, str(self.settings.value("replace_assistant/size_mode", "use_edited_size")))
        self.package_output_root_edit.setText(
            str(self.settings.value("replace_assistant/package_output_root", str((self.base_dir / "replace_assistant_export").resolve())))
        )
        self.overwrite_package_checkbox.setChecked(bool(self.settings.value("replace_assistant/overwrite_existing", True)))
        self.create_no_encrypt_checkbox.setChecked(bool(self.settings.value("replace_assistant/create_no_encrypt", True)))
        self.package_title_edit.setText(str(self.settings.value("replace_assistant/package_title", "Crimson Forge Toolkit Mod")))
        self.package_version_edit.setText(str(self.settings.value("replace_assistant/package_version", "1.0")))
        self.package_author_edit.setText(str(self.settings.value("replace_assistant/package_author", "")))
        self.package_description_edit.setText(str(self.settings.value("replace_assistant/package_description", "")))
        self.package_nexus_edit.setText(str(self.settings.value("replace_assistant/package_nexus", "")))
        self.ncnn_exe_path_edit.setText(str(self.settings.value("replace_assistant/ncnn_exe_path", "")))
        self.ncnn_model_dir_edit.setText(str(self.settings.value("replace_assistant/ncnn_model_dir", REALESRGAN_NCNN_MODEL_DIR)))
        self._set_combo_by_value(
            self.ncnn_model_combo,
            str(self.settings.value("replace_assistant/ncnn_model_name", REALESRGAN_NCNN_MODEL_NAME)),
        )
        self.ncnn_scale_spin.setValue(int(self.settings.value("replace_assistant/ncnn_scale", REALESRGAN_NCNN_SCALE)))
        self.ncnn_tile_size_spin.setValue(int(self.settings.value("replace_assistant/ncnn_tile_size", REALESRGAN_NCNN_TILE_SIZE)))
        self.ncnn_extra_args_edit.setText(str(self.settings.value("replace_assistant/ncnn_extra_args", REALESRGAN_NCNN_EXTRA_ARGS)))
        self._set_combo_by_value(
            self.upscale_post_correction_combo,
            str(self.settings.value("replace_assistant/post_correction", DEFAULT_UPSCALE_POST_CORRECTION)),
        )
        self._set_combo_by_value(
            self.upscale_texture_preset_combo,
            str(self.settings.value("replace_assistant/texture_preset", DEFAULT_UPSCALE_TEXTURE_PRESET)),
        )
        self.enable_automatic_texture_rules_checkbox.setChecked(
            bool(self.settings.value("replace_assistant/automatic_rules", False))
        )
        self.enable_unsafe_technical_override_checkbox.setChecked(bool(self.settings.value("replace_assistant/unsafe_override", False)))
        self.retry_smaller_tile_checkbox.setChecked(bool(self.settings.value("replace_assistant/retry_smaller_tile", True)))

    def _refresh_ncnn_models(self) -> None:
        current_name = self._combo_value(self.ncnn_model_combo) or self.ncnn_model_combo.currentText()
        exe_path_text = self.ncnn_exe_path_edit.text().strip()
        exe_path = Path(exe_path_text).expanduser() if exe_path_text else None
        model_dir = Path(self.ncnn_model_dir_edit.text().strip() or REALESRGAN_NCNN_MODEL_DIR)
        try:
            discovered = discover_realesrgan_ncnn_models(exe_path, model_dir)
        except Exception as exc:
            self.ncnn_model_combo.blockSignals(True)
            self.ncnn_model_combo.clear()
            self.ncnn_model_combo.addItem(f"No models found: {exc}", "")
            self.ncnn_model_combo.blockSignals(False)
            self.append_log(f"ERROR: {exc}")
            self.status_label.setText("NCNN model scan failed.")
            self._update_controls()
            return

        self.ncnn_model_combo.blockSignals(True)
        self.ncnn_model_combo.clear()
        for model_name, _model_dir in discovered:
            self.ncnn_model_combo.addItem(model_name, model_name)
        self.ncnn_model_combo.blockSignals(False)
        if discovered:
            self._set_combo_by_value(self.ncnn_model_combo, current_name)
            if self.ncnn_model_combo.currentIndex() < 0:
                self.ncnn_model_combo.setCurrentIndex(0)
            self.status_label.setText(f"Loaded {len(discovered):,} NCNN model(s).")
        else:
            self.ncnn_model_combo.addItem("No models found", "")
            self.status_label.setText("No NCNN models found.")
        self._update_summary()
        self._update_controls()

    def _set_summary_text(self, text: str) -> None:
        self.summary_label.setText(text)

    def _update_summary(self) -> None:
        total = len(self.items)
        matched = sum(1 for item in self.items if item.status == "matched")
        unresolved = sum(1 for item in self.items if item.status == "unresolved")
        failed = sum(1 for item in self.items if item.status == "failed")
        kind_counts: Dict[str, int] = {}
        for item in self.items:
            kind_counts[item.source_kind] = kind_counts.get(item.source_kind, 0) + 1
        kinds_text = ", ".join(f"{kind}:{count}" for kind, count in sorted(kind_counts.items())) if kind_counts else "none"
        self._set_summary_text(
            f"{total:,} edited file(s) loaded. Matched: {matched:,}. Unresolved: {unresolved:,}. Failed: {failed:,}. "
            f"Kinds: {kinds_text}."
        )
        if unresolved:
            self.status_label.setText(f"{unresolved:,} item(s) still need an original DDS.")
        elif total:
            self.status_label.setText("All imported files are matched.")
        else:
            self.status_label.setText("Ready.")

    def _sync_build_mode_visibility(self) -> None:
        show_ncnn = self._combo_value(self.build_mode_combo) == "upscale_then_rebuild"
        self.ncnn_group.setVisible(show_ncnn)

    def _update_controls(self) -> None:
        busy = self.external_busy or self.is_busy()
        has_items = bool(self.items)
        selected_count = len(self.queue_tree.selectedItems())
        show_ncnn = self._combo_value(self.build_mode_combo) == "upscale_then_rebuild"
        self.add_files_button.setEnabled(not busy)
        self.add_folder_button.setEnabled(not busy)
        self.auto_match_button.setEnabled(not busy and has_items)
        self.choose_local_original_button.setEnabled(not busy and selected_count == 1)
        self.choose_archive_original_button.setEnabled(
            not busy and selected_count == 1 and bool(self.archive_entries or self.get_archive_entries())
        )
        self.remove_selected_button.setEnabled(not busy and selected_count > 0)
        self.clear_all_button.setEnabled(not busy and has_items)
        self.build_package_button.setEnabled(not busy and has_items)
        self.open_output_folder_button.setEnabled(not busy and bool(self.package_output_root_edit.text().strip()))
        self.mirror_workflow_button.setEnabled(not busy)
        self.ncnn_refresh_models_button.setEnabled(not busy)
        self.preview_zoom_out_button.setEnabled(has_items and not self.preview_label.text().startswith("Preparing"))
        self.preview_zoom_fit_button.setEnabled(has_items)
        self.preview_zoom_100_button.setEnabled(has_items)
        self.preview_zoom_in_button.setEnabled(has_items)
        self.build_mode_combo.setEnabled(not busy)
        self.size_mode_combo.setEnabled(not busy)
        self.package_output_root_edit.setEnabled(not busy)
        self.package_output_browse_button.setEnabled(not busy)
        self.overwrite_package_checkbox.setEnabled(not busy)
        self.create_no_encrypt_checkbox.setEnabled(not busy)
        self.package_title_edit.setEnabled(not busy)
        self.package_version_edit.setEnabled(not busy)
        self.package_author_edit.setEnabled(not busy)
        self.package_description_edit.setEnabled(not busy)
        self.package_nexus_edit.setEnabled(not busy)
        self.open_in_editor_button.setEnabled(not busy and selected_count == 1)
        self.ncnn_group.setVisible(show_ncnn)
        self.ncnn_exe_path_edit.setEnabled(not busy and show_ncnn)
        self.ncnn_model_dir_edit.setEnabled(not busy and show_ncnn)
        self.ncnn_model_combo.setEnabled(not busy and show_ncnn)
        self.ncnn_scale_spin.setEnabled(not busy and show_ncnn)
        self.ncnn_tile_size_spin.setEnabled(not busy and show_ncnn)
        self.ncnn_extra_args_edit.setEnabled(not busy and show_ncnn)
        self.upscale_post_correction_combo.setEnabled(not busy and show_ncnn)
        self.upscale_texture_preset_combo.setEnabled(not busy and show_ncnn)
        self.enable_automatic_texture_rules_checkbox.setEnabled(not busy and show_ncnn)
        self.enable_unsafe_technical_override_checkbox.setEnabled(not busy and show_ncnn)
        self.retry_smaller_tile_checkbox.setEnabled(not busy and show_ncnn)

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)
        self.status_message_requested.emit(message, message.startswith("ERROR:"))

    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import edited PNG or DDS files",
            self.base_dir.as_posix(),
            "Images (*.png *.dds);;All files (*.*)",
        )
        if not paths:
            return
        self._add_sources(paths)

    def import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import a folder of edited textures", self.base_dir.as_posix())
        if not folder:
            return
        self._add_sources([folder])

    def import_external_sources(self, paths: Sequence[str | Path], *, select_path: Optional[str | Path] = None) -> None:
        self._add_sources(paths, select_path=select_path)

    def _add_sources(self, paths: Sequence[str | Path], *, select_path: Optional[str | Path] = None) -> None:
        if self.is_busy():
            return
        self._pending_import_select_path = ""
        if select_path is not None:
            try:
                self._pending_import_select_path = Path(select_path).expanduser().resolve().as_posix().lower()
            except Exception:
                self._pending_import_select_path = str(select_path).strip().lower()
        current_original_root = self._current_original_root_path()
        active_entries = self.archive_entries or self.get_archive_entries()
        entries_missing_from_index = bool(active_entries) and not self.archive_index.entries_by_relative_path
        root_changed = self.archive_index_original_root != current_original_root
        archive_index: Optional[ReplaceAssistantArchiveIndex]
        if entries_missing_from_index or root_changed:
            archive_index = None
        else:
            archive_index = self.archive_index
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Importing...")
        self.status_label.setText("Importing edited files...")
        self.append_log("Importing edited files into Replace Assistant queue...")
        worker = ReplaceAssistantImportWorker(
            paths,
            archive_entries=active_entries,
            original_dds_root=current_original_root,
            archive_index=archive_index,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage_message.connect(self._handle_import_stage)
        worker.progress.connect(self._handle_import_progress)
        worker.completed.connect(self._handle_import_complete)
        worker.error.connect(self._handle_import_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_import_refs)
        self.import_worker = worker
        self.import_thread = thread
        self._update_controls()
        thread.start()

    def _handle_import_stage(self, message: str) -> None:
        self.status_label.setText(message)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("Importing...")

    def _handle_import_progress(self, current: int, total: int, detail: str) -> None:
        self.status_label.setText(detail)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(max(current, 0), total))
            self.progress_bar.setFormat(f"{min(max(current, 0), total)} / {total}")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Importing...")

    def _handle_import_complete(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        new_items = payload.get("items", [])
        archive_index = payload.get("archive_index")
        original_dds_root = payload.get("original_dds_root")
        if isinstance(archive_index, ReplaceAssistantArchiveIndex):
            self.archive_index = archive_index
            self.archive_index_original_root = original_dds_root if isinstance(original_dds_root, Path) or original_dds_root is None else None
        if not isinstance(new_items, list) or not new_items:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Ready")
            self.status_label.setText("No importable PNG or DDS files were found.")
            self.append_log("No importable PNG or DDS files were found.")
            return
        existing_paths = {item.source_path.resolve().as_posix().lower() for item in self.items}
        added_count = 0
        for item in new_items:
            if not isinstance(item, ReplaceAssistantItem):
                continue
            resolved = item.source_path.resolve().as_posix().lower()
            if resolved in existing_paths:
                continue
            self.items.append(item)
            existing_paths.add(resolved)
            added_count += 1
        self._refresh_queue_tree()
        if self._pending_import_select_path:
            for row_index, item in enumerate(self.items):
                resolved_path = item.source_path.expanduser().resolve().as_posix().lower()
                if resolved_path != self._pending_import_select_path:
                    continue
                row = self.queue_tree.topLevelItem(row_index)
                if row is not None:
                    self.queue_tree.setCurrentItem(row)
                break
        self._pending_import_select_path = ""
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat("Ready")
        self.status_label.setText(
            f"Imported {added_count:,} edited file(s) into Replace Assistant. Use Auto-Match when you want to search originals."
        )
        self.append_log(
            f"Imported {added_count:,} edited file(s) into Replace Assistant without auto-matching."
        )

    def _handle_import_error(self, message: str) -> None:
        self._pending_import_select_path = ""
        self.append_log(f"ERROR: {message}")
        self.status_label.setText(message)
        self.status_message_requested.emit(message, True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Error")

    def _cleanup_import_refs(self) -> None:
        self.import_thread = None
        self.import_worker = None
        self._update_controls()

    def _cleanup_match_refs(self) -> None:
        self.match_thread = None
        self.match_worker = None
        self.preview_refresh_suspended = False
        self._update_controls()

    def _cleanup_ui_constraint_refs(self) -> None:
        self.ui_constraint_thread = None
        self.ui_constraint_worker = None
        self._active_ui_constraint_target = ""
        pending_target = self._pending_ui_constraint_target.strip()
        if pending_target and pending_target.casefold() not in self._ui_constraint_warning_cache:
            self._pending_ui_constraint_target = ""
            self._start_ui_constraint_worker(pending_target)

    def _refresh_queue_tree(self) -> None:
        current_item = self.queue_tree.currentItem()
        current_path = ""
        if current_item is not None:
            raw = current_item.data(0, Qt.UserRole)
            if isinstance(raw, int) and 0 <= raw < len(self.items):
                current_path = self.items[raw].source_path.expanduser().resolve().as_posix().lower()
        selected_paths = {
            item.source_path.expanduser().resolve().as_posix().lower()
            for item in self._selected_items()
        }
        resolved_item_paths = [
            item.source_path.expanduser().resolve().as_posix().lower()
            for item in self.items
        ]
        self.queue_tree.blockSignals(True)
        self.queue_tree.setUpdatesEnabled(False)
        self.queue_tree.clear()
        current_row: Optional[QTreeWidgetItem] = None
        for index, item in enumerate(self.items):
            original_text = ""
            if item.matched_original is not None:
                if item.matched_original.archive_relative_path:
                    original_text = item.matched_original.archive_relative_path
                elif item.matched_original.original_dds_path is not None:
                    original_text = item.matched_original.original_dds_path.name
            row = QTreeWidgetItem(
                [
                    item.detected_relative_path or item.source_path.name,
                    original_text or "Unmatched",
                    item.detected_package_root or (item.matched_original.package_root if item.matched_original else ""),
                    item.source_kind,
                    item.status_detail or item.status,
                ]
            )
            row.setData(0, Qt.UserRole, index)
            row.setToolTip(0, str(item.source_path))
            row.setToolTip(1, original_text or "Unmatched")
            row.setToolTip(4, item.warning or item.status_detail or item.status)
            self.queue_tree.addTopLevelItem(row)
            resolved_path = resolved_item_paths[index]
            if resolved_path in selected_paths:
                row.setSelected(True)
            if resolved_path == current_path:
                current_row = row
        self.queue_tree.setUpdatesEnabled(True)
        self.queue_tree.blockSignals(False)
        if current_row is not None:
            self.queue_tree.setCurrentItem(current_row)
        elif self.queue_tree.topLevelItemCount() and self.queue_tree.currentItem() is None:
            self.queue_tree.setCurrentItem(self.queue_tree.topLevelItem(0))
        self._update_summary()
        self._update_controls()

    def _refresh_queue_tree_rows_only(self) -> None:
        row_count = self.queue_tree.topLevelItemCount()
        if row_count != len(self.items):
            self._refresh_queue_tree()
            return
        self.queue_tree.blockSignals(True)
        self.queue_tree.setUpdatesEnabled(False)
        try:
            for index, item in enumerate(self.items):
                row = self.queue_tree.topLevelItem(index)
                if row is None:
                    continue
                original_text = ""
                if item.matched_original is not None:
                    if item.matched_original.archive_relative_path:
                        original_text = item.matched_original.archive_relative_path
                    elif item.matched_original.original_dds_path is not None:
                        original_text = item.matched_original.original_dds_path.name
                row.setText(0, item.detected_relative_path or item.source_path.name)
                row.setText(1, original_text or "Unmatched")
                row.setText(2, item.detected_package_root or (item.matched_original.package_root if item.matched_original else ""))
                row.setText(3, item.source_kind)
                row.setText(4, item.status_detail or item.status)
                row.setData(0, Qt.UserRole, index)
                row.setToolTip(0, str(item.source_path))
                row.setToolTip(1, original_text or "Unmatched")
                row.setToolTip(4, item.warning or item.status_detail or item.status)
        finally:
            self.queue_tree.setUpdatesEnabled(True)
            self.queue_tree.blockSignals(False)
        self._update_summary()
        self._update_controls()

    def _selected_item_indices(self) -> List[int]:
        indices: List[int] = []
        for item in self.queue_tree.selectedItems():
            raw = item.data(0, Qt.UserRole)
            if isinstance(raw, int) and 0 <= raw < len(self.items):
                indices.append(raw)
        return indices

    def _selected_items(self) -> List[ReplaceAssistantItem]:
        return [self.items[index] for index in self._selected_item_indices()]

    def _current_item(self) -> Optional[ReplaceAssistantItem]:
        current = self.queue_tree.currentItem()
        if current is None:
            return None
        raw = current.data(0, Qt.UserRole)
        if isinstance(raw, int) and 0 <= raw < len(self.items):
            return self.items[raw]
        return None

    def _handle_selection_changed(self, current: Optional[QTreeWidgetItem], _previous: Optional[QTreeWidgetItem]) -> None:
        self._update_controls()
        if self.preview_refresh_suspended:
            return
        if current is None:
            return
        raw = current.data(0, Qt.UserRole)
        if not isinstance(raw, int) or raw < 0 or raw >= len(self.items):
            return
        item = self.items[raw]
        self._ensure_ui_constraint_warning(item)
        self._schedule_preview(item)

    def _build_texture_editor_binding(self, item: ReplaceAssistantItem) -> TextureEditorSourceBinding:
        matched = item.matched_original
        package_root = item.detected_package_root or (matched.package_root if matched else "")
        archive_relative_path = matched.archive_relative_path if matched is not None else (item.detected_relative_path or "")
        relative_path = archive_relative_path
        if package_root and archive_relative_path:
            relative_path = str(Path(package_root) / Path(PurePosixPath(archive_relative_path)))
        return TextureEditorSourceBinding(
            launch_origin="replace_assistant",
            display_name=item.source_path.name,
            source_path=str(item.source_path),
            source_identity_path=str(item.source_path),
            relative_path=relative_path,
            package_root=package_root,
            archive_relative_path=archive_relative_path,
            original_dds_path=str(matched.original_dds_path) if matched is not None and matched.original_dds_path is not None else "",
        )

    def _matched_original_from_binding(self, binding: TextureEditorSourceBinding) -> Optional[MatchedOriginalTexture]:
        archive_relative_path = (binding.archive_relative_path or "").strip()
        package_root = (binding.package_root or "").strip()
        relative_path_text = (binding.relative_path or "").strip()
        original_dds_path = Path(binding.original_dds_path).expanduser().resolve() if binding.original_dds_path else None
        if original_dds_path is not None and not original_dds_path.exists():
            original_dds_path = None
        if not archive_relative_path and not relative_path_text and original_dds_path is None:
            return None
        if not relative_path_text and package_root and archive_relative_path:
            relative_path_text = str(Path(package_root) / Path(PurePosixPath(archive_relative_path)))
        if relative_path_text:
            loose_relative = Path(PurePosixPath(relative_path_text))
        elif package_root and archive_relative_path:
            loose_relative = Path(package_root) / Path(PurePosixPath(archive_relative_path))
        elif original_dds_path is not None:
            loose_relative = Path(original_dds_path.name)
        else:
            return None
        return MatchedOriginalTexture(
            package_root=package_root,
            archive_relative_path=archive_relative_path or PurePosixPath(loose_relative.as_posix()).as_posix(),
            loose_relative_path=Path(loose_relative),
            original_dds_path=original_dds_path,
            archive_entry=None,
            match_reason="preserved from Texture Editor binding",
        )

    def open_current_item_in_texture_editor(self) -> None:
        item = self._current_item()
        if item is None:
            self.status_label.setText("Select one imported file first.")
            return
        self.open_in_texture_editor_requested.emit(str(item.source_path), self._build_texture_editor_binding(item))

    def _apply_editor_export(
        self,
        resolved_output: Path,
        binding: TextureEditorSourceBinding,
        matched_original: Optional[MatchedOriginalTexture],
    ) -> None:
        binding_identity = binding.source_identity_path or binding.source_path
        binding_source = Path(binding_identity).expanduser().resolve() if binding_identity else None
        updated_existing = False
        if binding_source is not None:
            for index, item in enumerate(self.items):
                if item.source_path.expanduser().resolve() != binding_source:
                    continue
                matched = item.matched_original or matched_original
                self.items[index] = dataclasses.replace(
                    item,
                    source_path=resolved_output,
                    source_kind=resolved_output.suffix.lower().lstrip("."),
                    detected_relative_path=binding.archive_relative_path or binding.relative_path or item.detected_relative_path,
                    detected_package_root=binding.package_root or item.detected_package_root,
                    matched_original=matched,
                    warning=item.warning,
                    status="matched" if matched is not None else item.status,
                    status_detail="edited in Texture Editor",
                )
                updated_existing = True
                break
        if not updated_existing:
            self.items.append(
                ReplaceAssistantItem(
                    source_path=resolved_output,
                    source_kind=resolved_output.suffix.lower().lstrip("."),
                    detected_relative_path=binding.archive_relative_path or binding.relative_path,
                    detected_package_root=binding.package_root,
                    matched_original=matched,
                    status="matched" if matched is not None else "pending",
                    status_detail="edited in Texture Editor",
                )
            )
            self._refresh_queue_tree()
            self.status_label.setText(f"Added Texture Editor export: {resolved_output.name}")
            self.append_log(f"Texture Editor export added to Replace Assistant: {resolved_output}")
            return
        self._refresh_queue_tree()
        for row_index, item in enumerate(self.items):
            if item.source_path.expanduser().resolve() != resolved_output:
                continue
            row = self.queue_tree.topLevelItem(row_index)
            if row is not None:
                self.queue_tree.setCurrentItem(row)
            break
        self.status_label.setText(f"Updated Replace Assistant item from Texture Editor: {resolved_output.name}")
        self.append_log(f"Texture Editor export applied to Replace Assistant: {resolved_output}")

    def accept_editor_export_prepared(
        self,
        exported_png_path: Path,
        binding: TextureEditorSourceBinding,
        matched_original: Optional[MatchedOriginalTexture],
    ) -> None:
        resolved_output = exported_png_path.expanduser().resolve()
        if not resolved_output.exists():
            self.status_label.setText(f"Texture Editor export not found: {resolved_output}")
            return
        self._apply_editor_export(resolved_output, binding, matched_original)

    def accept_editor_export(self, exported_png_path: Path, binding: TextureEditorSourceBinding) -> None:
        resolved_output = exported_png_path.expanduser().resolve()
        if not resolved_output.exists():
            self.status_label.setText(f"Texture Editor export not found: {resolved_output}")
            return
        matched_original = self._matched_original_from_binding(binding)
        self._apply_editor_export(resolved_output, binding, matched_original)

    def _schedule_preview(self, item: ReplaceAssistantItem) -> None:
        if self.preview_refresh_suspended:
            return
        self.preview_request_id += 1
        request_id = self.preview_request_id
        combined_warning = self._combined_item_warning(item)
        self.preview_title_label.setText(item.source_path.name)
        self.preview_meta_label.setText("Preparing preview...")
        self.preview_warning_label.setVisible(bool(combined_warning))
        self.preview_warning_label.setText(combined_warning)
        self._set_preview_details_text(item)
        if self.preview_worker is not None:
            self.preview_worker.stop()
        if self.preview_thread is not None:
            self.pending_preview_item = item
            return
        self._start_preview_worker(request_id, item)

    def _start_preview_worker(self, request_id: int, item: ReplaceAssistantItem) -> None:
        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        worker = ReplaceAssistantPreviewWorker(
            request_id,
            texconv_path,
            item.source_path,
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
        self._update_controls()

    def _handle_preview_ready(self, request_id: int, payload: object) -> None:
        if self.preview_refresh_suspended:
            return
        if request_id != self.preview_request_id or not isinstance(payload, ArchivePreviewResult):
            return
        item = self._current_item()
        ui_warning = self._ui_constraint_warning_for_item(item) if item is not None else ""
        combined_warning = "\n".join(part for part in [payload.warning_text, ui_warning] if part).strip()
        self.preview_title_label.setText(payload.title or "Preview")
        self.preview_meta_label.setText(payload.metadata_summary or "")
        self.preview_warning_label.setVisible(bool(combined_warning))
        self.preview_warning_label.setText(combined_warning)
        self._set_preview_details_text(item, payload.detail_text or "")
        if payload.preview_image is not None:
            self.preview_label.set_preview_image(payload.preview_image, payload.metadata_summary or payload.title or "Preview")
        elif payload.preview_image_path:
            self.preview_label.set_preview_image_path(payload.preview_image_path, payload.metadata_summary or payload.title or "Preview")
        else:
            self.preview_label.clear_preview("No preview available.")
        self.preview_zoom_value.setText("Fit" if self.preview_label.current_display_scale() >= 0.999 else f"{self.preview_label.current_display_scale():.0%}")

    def _handle_preview_error(self, request_id: int, message: str) -> None:
        if self.preview_refresh_suspended:
            return
        if request_id != self.preview_request_id:
            return
        self.preview_title_label.setText("Preview failed")
        self.preview_meta_label.setText(message)
        self.preview_warning_label.setVisible(True)
        self.preview_warning_label.setText(message)
        self.preview_label.clear_preview("Preview failed.")
        self.preview_details_edit.setPlainText(message)

    def _ui_constraint_target_path_for_item(self, item: Optional[ReplaceAssistantItem]) -> str:
        if item is None or item.matched_original is None:
            return ""
        return (
            item.matched_original.archive_relative_path
            or item.detected_relative_path
            or ""
        ).strip()

    def _set_preview_details_text(self, item: Optional[ReplaceAssistantItem], base_detail_text: str = "") -> None:
        lines: List[str] = []
        if base_detail_text.strip():
            for raw_line in base_detail_text.strip().splitlines():
                if raw_line.startswith("UI constraint warning:"):
                    continue
                lines.append(raw_line)
        elif item is not None:
            lines.extend(
                [
                    f"Source: {item.source_path}",
                    f"Type: {item.source_kind}",
                    f"Matched original: {item.matched_original.archive_relative_path if item.matched_original else 'Unmatched'}",
                    f"Package: {item.matched_original.package_root if item.matched_original else item.detected_package_root}",
                    f"Status: {item.status}",
                    f"Detail: {item.status_detail}",
                ]
            )
        target_path = self._ui_constraint_target_path_for_item(item)
        ui_warning = self._ui_constraint_warning_for_item(item)
        if target_path:
            cache_key = target_path.casefold()
            display_warning = ui_warning if ui_warning else ("none" if cache_key in self._ui_constraint_warning_cache else "checking...")
            lines.append(f"UI constraint warning: {display_warning}")
        else:
            lines.append("UI constraint warning: none")
        self.preview_details_edit.setPlainText("\n".join(line for line in lines if line))

    def _start_ui_constraint_worker(self, target_path: str) -> None:
        self.ui_constraint_request_id += 1
        request_id = self.ui_constraint_request_id
        self._active_ui_constraint_target = target_path
        worker = ReplaceAssistantUIConstraintWorker(request_id, self.get_archive_entries(), target_path)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._handle_ui_constraint_ready)
        worker.error.connect(self._handle_ui_constraint_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_ui_constraint_refs)
        self.ui_constraint_worker = worker
        self.ui_constraint_thread = thread
        thread.start()

    def _looks_like_ui_constraint_candidate(self, target_path: str) -> bool:
        normalized = str(target_path or "").replace("\\", "/").lower()
        if not normalized:
            return False
        ui_tokens = ("/ui/", "/icon/", "/hud/", "/menu/", "/widget/")
        name_tokens = ("itemicon", "ui_", "icon_", "hud_", "menu_")
        name = PurePosixPath(normalized).name
        return any(token in normalized for token in ui_tokens) or any(token in name for token in name_tokens)

    def _ensure_ui_constraint_warning(self, item: Optional[ReplaceAssistantItem]) -> None:
        target_path = self._ui_constraint_target_path_for_item(item)
        if not target_path:
            self._pending_ui_constraint_target = ""
            return
        cache_key = target_path.casefold()
        if cache_key in self._ui_constraint_warning_cache:
            self._pending_ui_constraint_target = ""
            return
        if not self._looks_like_ui_constraint_candidate(target_path):
            self._ui_constraint_warning_cache[cache_key] = ""
            self._pending_ui_constraint_target = ""
            return
        if self._active_ui_constraint_target.casefold() == cache_key:
            self._pending_ui_constraint_target = ""
            return
        pending_target = self._pending_ui_constraint_target.strip()
        if pending_target and pending_target.casefold() == cache_key:
            return
        if self.ui_constraint_thread is not None:
            self._pending_ui_constraint_target = target_path
            return
        self._pending_ui_constraint_target = ""
        self._start_ui_constraint_worker(target_path)

    def _handle_ui_constraint_ready(self, request_id: int, target_path: str, warning_text: str) -> None:
        if request_id != self.ui_constraint_request_id:
            return
        self._ui_constraint_warning_cache[target_path.casefold()] = warning_text
        current_item = self._current_item()
        if self._ui_constraint_target_path_for_item(current_item).casefold() != target_path.casefold():
            return
        combined_warning = self._combined_item_warning(current_item)
        self.preview_warning_label.setVisible(bool(combined_warning))
        self.preview_warning_label.setText(combined_warning)
        self._set_preview_details_text(current_item, self.preview_details_edit.toPlainText())

    def _handle_ui_constraint_error(self, request_id: int, _message: str) -> None:
        if request_id != self.ui_constraint_request_id:
            return

    def _ui_constraint_warning_for_item(self, item: Optional[ReplaceAssistantItem]) -> str:
        target_path = self._ui_constraint_target_path_for_item(item)
        if not target_path:
            return ""
        return self._ui_constraint_warning_cache.get(target_path.casefold(), "")

    def _combined_item_warning(self, item: Optional[ReplaceAssistantItem]) -> str:
        if item is None:
            return ""
        return "\n".join(part for part in [item.warning, self._ui_constraint_warning_for_item(item)] if part).strip()

    def _cleanup_preview_refs(self) -> None:
        self.preview_thread = None
        self.preview_worker = None
        if self.preview_refresh_suspended:
            self.pending_preview_item = None
        elif hasattr(self, "pending_preview_item") and self.pending_preview_item is not None:
            item = self.pending_preview_item
            self.pending_preview_item = None
            self.preview_request_id += 1
            self._start_preview_worker(self.preview_request_id, item)
        self._update_controls()

    def _adjust_preview_zoom(self, step: int) -> None:
        current = self.preview_label.current_display_scale()
        factor = max(0.1, current * (1.15 if step > 0 else 0.87))
        self._set_preview_zoom_factor(factor)

    def _set_preview_fit(self, fit_to_view: bool) -> None:
        self.preview_label.set_fit_to_view(fit_to_view)
        self.preview_zoom_value.setText("Fit" if fit_to_view else f"{self.preview_label.current_display_scale():.0%}")

    def _set_preview_zoom_factor(self, factor: float) -> None:
        self.preview_label.set_fit_to_view(False)
        self.preview_label.set_zoom_factor(factor)
        self.preview_zoom_value.setText(f"{factor:.0%}")

    def auto_match_all_items(self, *, refresh_preview: bool = True) -> None:
        if self.is_busy() or not self.items:
            return
        if self.preview_worker is not None:
            self.preview_worker.stop()
        self.preview_refresh_suspended = True
        self.pending_preview_item = None
        self.preview_request_id += 1
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Matching...")
        self.status_label.setText("Auto-matching edited files...")
        self.append_log("Auto-matching edited files against archive/original DDS paths...")
        try:
            self._ensure_archive_index_current()
            ambiguous_indices: List[int] = []
            for index, item in enumerate(self.items):
                matched = match_replace_assistant_original(item.source_path, self.archive_index)
                if matched.archive_entry is not None or matched.original_dds_path is not None:
                    item.matched_original = matched
                    item.detected_package_root = matched.package_root
                    item.detected_relative_path = matched.archive_relative_path
                    item.status = "matched"
                    item.status_detail = matched.match_reason
                    item.warning = matched.match_reason if matched.match_reason.startswith("ambiguous") else ""
                else:
                    item.matched_original = None
                    item.status = "unresolved"
                    item.status_detail = matched.match_reason or "unmatched"
                    item.warning = matched.match_reason if matched.match_reason.startswith("ambiguous") else ""
                    if matched.match_reason.startswith("ambiguous"):
                        ambiguous_indices.append(index)
            self._refresh_queue_tree()
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.progress_bar.setFormat("Ready")
            matched_count = sum(1 for item in self.items if item.status == "matched")
            unresolved_count = sum(1 for item in self.items if item.status == "unresolved")
            self.status_label.setText(
                f"Auto-match complete. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
            )
            self.append_log(
                f"Auto-match complete. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
            )
            if ambiguous_indices:
                self._prompt_resolve_ambiguous_items(ambiguous_indices)
            self.preview_refresh_suspended = False
            if refresh_preview and self.queue_tree.currentItem() is not None:
                self._handle_selection_changed(self.queue_tree.currentItem(), None)
        except Exception as exc:
            self.preview_refresh_suspended = False
            self._handle_import_error(str(exc))
        finally:
            self._update_controls()

    def _prompt_resolve_ambiguous_items(self, indices: Sequence[int]) -> None:
        ambiguous_indices = [index for index in indices if 0 <= index < len(self.items)]
        if not ambiguous_indices:
            return
        count = len(ambiguous_indices)
        box = QMessageBox(self)
        box.setWindowTitle("Choose Original DDS")
        box.setIcon(QMessageBox.Question)
        if count == 1:
            item = self.items[ambiguous_indices[0]]
            box.setText("Multiple possible original DDS files were found for this edited file.")
            box.setInformativeText(
                f"{item.source_path.name}\n\n"
                "The imported file does not contain a unique path match, so you need to choose the correct original DDS."
            )
        else:
            box.setText(f"{count:,} imported file(s) matched multiple possible original DDS files.")
            box.setInformativeText(
                "These files do not contain a unique path match, so you need to choose the correct original DDS for each one."
            )
        choose_button = box.addButton("Choose Now", QMessageBox.AcceptRole)
        later_button = box.addButton("Later", QMessageBox.RejectRole)
        box.setDefaultButton(choose_button)
        box.exec()
        if box.clickedButton() != choose_button:
            return
        current_tree_item = self.queue_tree.currentItem()
        current_source_key = str(current_tree_item.data(0, Qt.UserRole) or "") if current_tree_item is not None else ""
        changed = False
        for index in ambiguous_indices:
            if not (0 <= index < len(self.items)):
                continue
            item = self.items[index]
            entry = self._pick_archive_original(item)
            if entry is None:
                break
            match_replace_assistant_item_to_archive_entry(item, entry)
            changed = True
        if not changed:
            return
        self._refresh_queue_tree()
        if current_source_key:
            for row in range(self.queue_tree.topLevelItemCount()):
                row_item = self.queue_tree.topLevelItem(row)
                if row_item is None:
                    continue
                if str(row_item.data(0, Qt.UserRole) or "") == current_source_key:
                    self.queue_tree.setCurrentItem(row_item)
                    break
        matched_count = sum(1 for item in self.items if item.status == "matched")
        unresolved_count = sum(1 for item in self.items if item.status == "unresolved")
        self.status_label.setText(
            f"Auto-match complete. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
        )
        self.append_log(
            f"Ambiguous match review updated. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
        )

    def _handle_auto_match_complete(self, payload: object, refresh_preview: bool) -> None:
        if not isinstance(payload, dict):
            return
        updated_items = payload.get("items", [])
        archive_index = payload.get("archive_index")
        original_dds_root = payload.get("original_dds_root")
        if isinstance(archive_index, ReplaceAssistantArchiveIndex):
            self.archive_index = archive_index
            self.archive_index_original_root = (
                original_dds_root if isinstance(original_dds_root, Path) or original_dds_root is None else None
            )
        if isinstance(updated_items, list):
            self.items = [item for item in updated_items if isinstance(item, ReplaceAssistantItem)]
        self._refresh_queue_tree_rows_only()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setFormat("Ready")
        matched_count = sum(1 for item in self.items if item.status == "matched")
        unresolved_count = sum(1 for item in self.items if item.status == "unresolved")
        self.status_label.setText(
            f"Auto-match complete. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
        )
        self.append_log(
            f"Auto-match complete. Matched {matched_count:,} item(s), unresolved {unresolved_count:,}."
        )
        if refresh_preview:
            current_item = self._current_item()
            if current_item is not None:
                combined_warning = self._combined_item_warning(current_item)
                self.preview_title_label.setText(current_item.source_path.name)
                self.preview_meta_label.setText("Auto-match complete. Click the item to refresh preview.")
                self.preview_warning_label.setVisible(bool(combined_warning))
                self.preview_warning_label.setText(combined_warning)
                self._set_preview_details_text(current_item, self.preview_details_edit.toPlainText())

    def choose_local_original_for_selected(self) -> None:
        indices = self._selected_item_indices()
        if len(indices) != 1:
            return
        original_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose original DDS",
            self.get_original_root().strip() or self.base_dir.as_posix(),
            "DDS files (*.dds);;All files (*.*)",
        )
        if not original_path:
            return
        try:
            original_root_text = self.get_original_root().strip()
            original_root = Path(original_root_text).expanduser() if original_root_text else None
            match_replace_assistant_item_to_local_original(
                self.items[indices[0]],
                Path(original_path),
                original_dds_root=original_root,
            )
        except Exception as exc:
            QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._refresh_queue_tree()
        self._handle_selection_changed(self.queue_tree.currentItem(), None)

    def choose_archive_original_for_selected(self) -> None:
        indices = self._selected_item_indices()
        if len(indices) != 1:
            return
        item = self.items[indices[0]]
        entry = self._pick_archive_original(item)
        if entry is None:
            return
        match_replace_assistant_item_to_archive_entry(item, entry)
        self._refresh_queue_tree()
        self._handle_selection_changed(self.queue_tree.currentItem(), None)

    def remove_selected_items(self) -> None:
        indices = sorted(set(self._selected_item_indices()), reverse=True)
        if not indices:
            return
        for index in indices:
            self.items.pop(index)
        self._refresh_queue_tree()
        self.append_log(f"Removed {len(indices):,} item(s) from Replace Assistant.")

    def clear_all_items(self) -> None:
        if not self.items:
            return
        self.items.clear()
        self.last_built_output_root = None
        self.queue_tree.clear()
        self.preview_label.clear_preview("Select a file to preview it here.")
        self.preview_title_label.setText("Select an imported file")
        self.preview_meta_label.setText("Select a file to preview it here.")
        self.preview_warning_label.setVisible(False)
        self.preview_details_edit.clear()
        self._update_summary()
        self._update_controls()

    def mirror_texture_workflow_settings(self) -> None:
        config = self.get_current_config()
        self.ncnn_exe_path_edit.setText(str(getattr(config, "ncnn_exe_path", "")))
        self.ncnn_model_dir_edit.setText(str(getattr(config, "ncnn_model_dir", self.ncnn_model_dir_edit.text())))
        self._refresh_ncnn_models()
        self._set_combo_by_value(self.ncnn_model_combo, str(getattr(config, "ncnn_model_name", "")))
        self.ncnn_scale_spin.setValue(int(getattr(config, "ncnn_scale", self.ncnn_scale_spin.value())))
        self.ncnn_tile_size_spin.setValue(int(getattr(config, "ncnn_tile_size", self.ncnn_tile_size_spin.value())))
        self.ncnn_extra_args_edit.setText(str(getattr(config, "ncnn_extra_args", "")))
        self._set_combo_by_value(self.upscale_post_correction_combo, str(getattr(config, "upscale_post_correction_mode", "")))
        self._set_combo_by_value(self.upscale_texture_preset_combo, str(getattr(config, "upscale_texture_preset", "")))
        self.enable_automatic_texture_rules_checkbox.setChecked(bool(getattr(config, "enable_automatic_texture_rules", False)))
        self.enable_unsafe_technical_override_checkbox.setChecked(bool(getattr(config, "enable_unsafe_technical_override", False)))
        self.retry_smaller_tile_checkbox.setChecked(bool(getattr(config, "retry_smaller_tile_on_failure", True)))
        self.package_output_root_edit.setText(
            str(getattr(config, "mod_ready_export_root", self.package_output_root_edit.text()))
        )
        self.append_log("Mirrored Texture Workflow NCNN and policy settings into Replace Assistant.")
        self._update_controls()

    def _current_build_options(self) -> ReplaceAssistantBuildOptions:
        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser()
        ncnn_exe_text = self.ncnn_exe_path_edit.text().strip()
        ncnn_exe_path = Path(ncnn_exe_text).expanduser() if ncnn_exe_text else None
        ncnn_model_dir_text = self.ncnn_model_dir_edit.text().strip()
        ncnn_model_dir = Path(ncnn_model_dir_text).expanduser() if ncnn_model_dir_text else None
        return ReplaceAssistantBuildOptions(
            package_output_root=Path(self.package_output_root_edit.text().strip()).expanduser(),
            overwrite_existing_package_files=self.overwrite_package_checkbox.isChecked(),
            create_no_encrypt_file=self.create_no_encrypt_checkbox.isChecked(),
            build_mode=self._combo_value(self.build_mode_combo),
            size_mode=self._combo_value(self.size_mode_combo),
            texconv_path=texconv_path,
            ncnn_exe_path=ncnn_exe_path,
            ncnn_model_dir=ncnn_model_dir,
            ncnn_model_name=self._combo_value(self.ncnn_model_combo),
            ncnn_scale=self.ncnn_scale_spin.value(),
            ncnn_tile_size=self.ncnn_tile_size_spin.value(),
            ncnn_extra_args=self.ncnn_extra_args_edit.text().strip(),
            retry_smaller_tile_on_failure=self.retry_smaller_tile_checkbox.isChecked(),
            upscale_post_correction_mode=self._combo_value(self.upscale_post_correction_combo),
            upscale_texture_preset=self._combo_value(self.upscale_texture_preset_combo),
            enable_automatic_texture_rules=self.enable_automatic_texture_rules_checkbox.isChecked(),
            enable_unsafe_technical_override=self.enable_unsafe_technical_override_checkbox.isChecked(),
            package_info=ModPackageInfo(
                title=self.package_title_edit.text().strip() or "Crimson Forge Toolkit Mod",
                version=self.package_version_edit.text().strip() or "1.0",
                author=self.package_author_edit.text().strip(),
                description=self.package_description_edit.text().strip(),
                nexus_url=self.package_nexus_edit.text().strip(),
            ),
        )

    def start_build(self) -> None:
        if self.is_busy():
            return
        if not self.items:
            QMessageBox.information(self, APP_TITLE, "Add edited PNG or DDS files before building a mod package.")
            return
        if any(item.status == "unresolved" for item in self.items):
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Some items are still unresolved. Choose the original DDS for each of them before building.",
            )
            return
        options = self._current_build_options()
        self.last_built_output_root = None
        self.progress_bar.setRange(0, len(self.items))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Working...")
        self.status_label.setText("Building replace package...")
        self.append_log("Starting Replace Assistant build.")
        worker = ReplaceAssistantBuildWorker(
            self.items,
            options,
            archive_entries=self.archive_entries or self.get_archive_entries(),
            original_dds_root=Path(self.get_original_root().strip()).expanduser() if self.get_original_root().strip() else None,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log_message.connect(self.append_log)
        worker.current_file.connect(lambda text: self.status_label.setText(text))
        worker.progress.connect(self._handle_build_progress)
        worker.completed.connect(self._handle_build_complete)
        worker.cancelled.connect(self._handle_build_cancelled)
        worker.error.connect(self._handle_build_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_build_refs)
        self.build_worker = worker
        self.build_thread = thread
        self._update_controls()
        thread.start()

    def stop_build(self) -> None:
        if self.build_worker is not None:
            self.build_worker.stop()

    def _handle_build_progress(self, current: int, total: int, detail: str) -> None:
        self.status_label.setText(detail)
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(max(current, 0), total))
            self.progress_bar.setFormat(f"{min(max(current, 0), total)} / {total}")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("Working...")

    def _handle_build_complete(self, payload: object) -> None:
        summary = payload if isinstance(payload, ReplaceAssistantBuildSummary) else None
        if summary is None:
            return
        self.append_log(
            f"Build complete: built={summary.built_items:,}, unresolved={summary.unresolved_items:,}, failed={summary.failed_items:,}."
        )
        if summary.output_root is not None:
            self.last_built_output_root = summary.output_root
            self.status_label.setText(f"Replace package written to: {summary.output_root}")
            self.status_message_requested.emit(f"Replace package written to: {summary.output_root}", False)
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.progress_bar.setFormat("Ready")
            if summary.review_items:
                self.pending_review_items = tuple(summary.review_items)
                self.append_log(
                    f"Prepared {len(summary.review_items):,} built item(s) for review. "
                    "Opening the review window after cleanup."
                )
        else:
            self.last_built_output_root = None
            self.status_label.setText("Replace package was not written because some items failed or were unresolved.")
            self.status_message_requested.emit(
                "Replace package was not written because some items failed or were unresolved.",
                True,
            )
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Failed")

    def _handle_build_cancelled(self, message: str) -> None:
        self.append_log(message)
        self.status_label.setText(message)
        self.status_message_requested.emit(message, True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Stopped")

    def _handle_build_error(self, message: str) -> None:
        self.append_log(f"ERROR: {message}")
        self.status_label.setText(message)
        self.status_message_requested.emit(message, True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Error")

    def _cleanup_build_refs(self) -> None:
        self.build_thread = None
        self.build_worker = None
        self._update_controls()
        if self.pending_review_items:
            pending_review_items = self.pending_review_items
            self.pending_review_items = None
            QTimer.singleShot(0, lambda items=pending_review_items: self._open_review_dialog(items))

    def _browse_package_output_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose replace package parent root",
            self.package_output_root_edit.text().strip() or self.base_dir.as_posix(),
        )
        if folder:
            self.package_output_root_edit.setText(folder)

    def _browse_ncnn_exe(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Real-ESRGAN NCNN executable",
            self.ncnn_exe_path_edit.text().strip() or self.base_dir.as_posix(),
            "Executables (*.exe);;All files (*.*)",
        )
        if file_path:
            self.ncnn_exe_path_edit.setText(file_path)
            self._refresh_ncnn_models()

    def _browse_ncnn_model_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose NCNN model folder",
            self.ncnn_model_dir_edit.text().strip() or self.base_dir.as_posix(),
        )
        if folder:
            self.ncnn_model_dir_edit.setText(folder)
            self._refresh_ncnn_models()

    def refresh_ncnn_models(self) -> None:
        self._refresh_ncnn_models()

    def open_output_folder(self) -> None:
        output_root = self.last_built_output_root
        if output_root is None:
            output_root_text = self.package_output_root_edit.text().strip()
            if not output_root_text:
                return
            output_root = Path(output_root_text).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_root)))

    def _open_review_dialog(self, review_items: Sequence[ReplaceAssistantReviewItem]) -> None:
        if self.review_dialog is not None:
            self.review_dialog.close()
        texconv_text = self.get_texconv_path().strip()
        texconv_path = Path(texconv_text).expanduser() if texconv_text else None
        dialog = ReplaceAssistantReviewDialog(texconv_path, review_items, self)
        self.review_dialog = dialog
        dialog.finished.connect(self._clear_review_dialog_ref)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _clear_review_dialog_ref(self) -> None:
        self.review_dialog = None

    def _pick_archive_original(self, item: ReplaceAssistantItem) -> Optional[ArchiveEntry]:
        archive_entries = [entry for entry in (self.archive_entries or self.get_archive_entries()) if entry.extension == ".dds"]
        if not archive_entries:
            QMessageBox.information(self, APP_TITLE, "No archive DDS entries are currently loaded.")
            return None

        dialog = QDialog(self)
        dialog.setWindowTitle("Choose archive original DDS")
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        hint_label = QLabel(
            "Filter the loaded archive DDS entries, then choose the original that matches the edited texture."
        )
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        filter_edit = QLineEdit(item.source_path.stem)
        filter_edit.setPlaceholderText("Filter by basename or relative path...")
        layout.addWidget(filter_edit)

        results_list = QListWidget()
        results_list.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(results_list, stretch=1)

        button_row = QHBoxLayout()
        choose_button = QPushButton("Choose")
        cancel_button = QPushButton("Cancel")
        button_row.addStretch(1)
        button_row.addWidget(choose_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

        def populate_results(filter_text: str) -> None:
            needle = filter_text.strip().lower()
            results_list.clear()
            ranked: List[ArchiveEntry]
            if needle:
                basename_matches = [entry for entry in archive_entries if needle in entry.basename.lower()]
                path_matches = [entry for entry in archive_entries if needle in entry.path.lower() and entry not in basename_matches]
                ranked = basename_matches + path_matches
            else:
                ranked = list(archive_entries)
            for entry in ranked[:500]:
                list_item = QListWidgetItem(f"{entry.package_label} | {entry.path}")
                list_item.setData(Qt.UserRole, entry)
                results_list.addItem(list_item)
            if results_list.count():
                results_list.setCurrentRow(0)
            choose_button.setEnabled(results_list.currentItem() is not None)

        def accept_current() -> None:
            if results_list.currentItem() is not None:
                dialog.accept()

        filter_edit.textChanged.connect(populate_results)
        results_list.itemSelectionChanged.connect(lambda: choose_button.setEnabled(results_list.currentItem() is not None))
        results_list.itemDoubleClicked.connect(lambda _item: accept_current())
        choose_button.clicked.connect(accept_current)
        cancel_button.clicked.connect(dialog.reject)

        populate_results(filter_edit.text())
        filter_edit.selectAll()
        filter_edit.setFocus()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        current_item = results_list.currentItem()
        selected = current_item.data(Qt.UserRole) if current_item is not None else None
        return selected if isinstance(selected, ArchiveEntry) else None
