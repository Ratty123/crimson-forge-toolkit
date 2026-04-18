from __future__ import annotations

from typing import Mapping, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from crimson_forge_toolkit.constants import DEFAULT_UI_THEME
from crimson_forge_toolkit.ui.themes import build_app_stylesheet, get_theme


class TexturePolicyPreviewDialog(QDialog):
    def __init__(self, *, theme_key: str = DEFAULT_UI_THEME, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TexturePolicyPreviewDialog")
        self.setWindowTitle("Texture Policy Preview")
        self.resize(1260, 780)
        self.setMinimumSize(980, 640)
        self._theme_key = theme_key or DEFAULT_UI_THEME
        self._build_ui()
        self.set_theme(self._theme_key)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(10)

        title = QLabel("Per-Texture Processing Plan")
        title.setObjectName("WizardTitle")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setWeight(QFont.DemiBold)
        title.setFont(title_font)
        root_layout.addWidget(title)

        self.summary_label = QLabel(
            "This preview shows what the current workflow settings would do for each matched DDS file before Start is run."
        )
        self.summary_label.setWordWrap(True)
        self.summary_label.setObjectName("HintLabel")
        root_layout.addWidget(self.summary_label)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setObjectName("HintLabel")
        self.warning_label.setVisible(False)
        root_layout.addWidget(self.warning_label)

        content_row = QHBoxLayout()
        content_row.setSpacing(10)
        root_layout.addLayout(content_row, stretch=1)

        list_group = QGroupBox("Policy Rows")
        list_layout = QVBoxLayout(list_group)
        list_layout.setContentsMargins(10, 12, 10, 10)
        list_layout.setSpacing(8)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setHeaderLabels(
            ["Path", "Action", "Workflow", "Rule", "Planned DDS", "NCNN", "Semantic", "Path"]
        )
        self.tree.header().resizeSection(0, 360)
        self.tree.header().resizeSection(1, 140)
        self.tree.header().resizeSection(2, 170)
        self.tree.header().resizeSection(3, 220)
        self.tree.header().resizeSection(4, 200)
        self.tree.header().resizeSection(5, 240)
        self.tree.header().resizeSection(6, 110)
        self.tree.header().resizeSection(7, 150)
        list_layout.addWidget(self.tree, stretch=1)
        content_row.addWidget(list_group, stretch=3)

        detail_group = QGroupBox("Selected Row Details")
        detail_layout = QVBoxLayout(detail_group)
        detail_layout.setContentsMargins(10, 12, 10, 10)
        detail_layout.setSpacing(8)
        self.detail_label = QLabel(
            "Select a row to inspect the inferred semantic subtype, evidence, and planned DDS policy."
        )
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("HintLabel")
        detail_layout.addWidget(self.detail_label)
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setPlaceholderText("Detailed notes, evidence, and policy reasons will appear here.")
        detail_layout.addWidget(self.detail_edit, stretch=1)
        content_row.addWidget(detail_group, stretch=2)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root_layout.addWidget(buttons)

        self.tree.currentItemChanged.connect(self._handle_selection_changed)

    def set_theme(self, theme_key: str) -> None:
        resolved = theme_key or DEFAULT_UI_THEME
        self._theme_key = resolved
        theme = get_theme(resolved)
        extra_styles = f"""
        QDialog#TexturePolicyPreviewDialog {{
            background: {theme["window"]};
        }}
        QLabel#WizardTitle {{
            color: {theme["text_strong"]};
        }}
        QLabel#HintLabel {{
            color: {theme["text_muted"]};
        }}
        QGroupBox {{
            border: 1px solid {theme["border"]};
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 6px;
            background: {theme["surface"]};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 10px;
            padding: 0 6px;
            color: {theme["text_strong"]};
        }}
        """
        self.setStyleSheet(build_app_stylesheet(resolved) + extra_styles)

    def set_payload(self, payload: Mapping[str, object]) -> None:
        summary = payload.get("summary", {})
        summary_dict = summary if isinstance(summary, Mapping) else {}
        actions = summary_dict.get("actions", {})
        action_map = actions if isinstance(actions, Mapping) else {}
        action_summary = ", ".join(
            f"{str(key)}={int(value)}" for key, value in action_map.items()
        ) or "none"
        total_files = int(summary_dict.get("total_files", 0) or 0)
        backend = str(summary_dict.get("backend", "") or "disabled")
        correction_mode = str(summary_dict.get("correction_mode", "") or "Off")
        path_kinds = summary_dict.get("path_kinds", {})
        path_summary = ", ".join(
            f"{str(key)}={int(value)}" for key, value in path_kinds.items()
        ) if isinstance(path_kinds, Mapping) else ""
        png_root = str(summary_dict.get("png_root", "") or "(not set)")
        output_root = str(summary_dict.get("output_root", "") or "(not set)")
        staging_root = str(summary_dict.get("staging_root", "") or "")
        summary_lines = [
            f"Matched DDS files: {total_files:,}",
            f"Backend: {backend}",
            f"Correction mode: {correction_mode}",
            f"Action summary: {action_summary}",
            f"Planner paths: {path_summary or 'none'}",
            "Visible-color path backend: "
            f"{'allowed' if bool(summary_dict.get('backend_visible_path_allowed', False)) else 'preserve'} "
            f"({summary_dict.get('backend_visible_path_mode', '')})",
            "Technical high-precision path backend: "
            f"{'allowed' if bool(summary_dict.get('backend_high_precision_path_allowed', False)) else 'preserve'} "
            f"({summary_dict.get('backend_high_precision_path_mode', '')})",
            f"PNG root: {png_root}",
            f"Output root: {output_root}",
        ]
        if staging_root:
            summary_lines.append(f"Staging PNG root: {staging_root}")
        planner_notes = summary_dict.get("backend_planner_notes", [])
        if isinstance(planner_notes, Sequence) and not isinstance(planner_notes, (str, bytes)) and planner_notes:
            summary_lines.append("Planner backend notes:")
            summary_lines.extend(f"- {note}" for note in planner_notes[:3])
        self.summary_label.setText("\n".join(summary_lines))

        runtime_warning = str(payload.get("runtime_validation_warning", "") or "").strip()
        self.warning_label.setVisible(bool(runtime_warning))
        self.warning_label.setText(runtime_warning)

        self.tree.clear()
        rows = payload.get("rows", [])
        row_list: Sequence[object] = rows if isinstance(rows, Sequence) else ()
        first_item: Optional[QTreeWidgetItem] = None
        for row in row_list:
            if not isinstance(row, Mapping):
                continue
            item = QTreeWidgetItem(
                [
                    str(row.get("path", "")),
                    str(row.get("action", "")),
                    str(row.get("workflow_profile_label", "")),
                    str(row.get("matched_rule", "")),
                    str(row.get("effective_dds_override", "")),
                    str(row.get("effective_ncnn_summary", "")),
                    f"{row.get('texture_type', '')}/{row.get('semantic_subtype', '')}",
                    str(row.get("path_kind", "")),
                ]
            )
            item.setData(0, Qt.UserRole, dict(row))
            self.tree.addTopLevelItem(item)
            if first_item is None:
                first_item = item
        if first_item is not None:
            self.tree.setCurrentItem(first_item)
        else:
            self.detail_edit.clear()
            self.detail_label.setText("No rows matched the current filter.")

    def _handle_selection_changed(
        self,
        current: Optional[QTreeWidgetItem],
        _previous: Optional[QTreeWidgetItem],
    ) -> None:
        if current is None:
            self.detail_label.setText("No row selected.")
            self.detail_edit.clear()
            return
        row = current.data(0, Qt.UserRole)
        if not isinstance(row, dict):
            self.detail_label.setText("No row details available.")
            self.detail_edit.clear()
            return

        packed = row.get("packed_channels", [])
        packed_text = ", ".join(str(value) for value in packed) if isinstance(packed, Sequence) and not isinstance(packed, (str, bytes)) else ""
        evidence = row.get("source_evidence", [])
        evidence_text = "\n".join(f"- {value}" for value in evidence) if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)) and evidence else "- none"
        notes = row.get("notes", [])
        notes_text = "\n".join(f"- {value}" for value in notes) if isinstance(notes, Sequence) and not isinstance(notes, (str, bytes)) and notes else "- none"

        self.detail_label.setText(
            f"{row.get('path', '')}\n"
            f"Action: {row.get('action', '')} | Semantic: {row.get('texture_type', '')}/{row.get('semantic_subtype', '')} | Confidence: {row.get('semantic_confidence', '')}%"
        )
        self.detail_edit.setPlainText(
            "\n".join(
                [
                    f"Path: {row.get('path', '')}",
                    f"Action: {row.get('action', '')}",
                    f"Reason: {row.get('action_reason', '')}",
                    f"Workflow profile: {row.get('workflow_profile_label', '')} ({row.get('workflow_profile_id', '')})",
                    f"Matched rule: {row.get('matched_rule', '')}",
                    f"Original format: {row.get('original_format', '')}",
                    f"Planned format: {row.get('planned_format', '')}",
                    f"Workflow DDS override: {row.get('effective_dds_override', '')}",
                    f"Profile: {row.get('profile_key', '')} ({row.get('profile_label', '')})",
                    f"Planner path: {row.get('path_kind', '')}",
                    f"Planner path detail: {row.get('path_description', '')}",
                    f"Size policy: {row.get('size_policy', '')}",
                    f"Mip policy: {row.get('mip_policy', '')}",
                    f"Alpha mode: {row.get('alpha_mode', '')}",
                    f"Alpha policy: {row.get('alpha_policy', '')}",
                    f"Backend execution: {row.get('backend_execution_mode', '')}",
                    f"Backend compatibility: {'yes' if row.get('backend_compatible', False) else 'no'}",
                    f"Backend reason: {row.get('backend_reason', '')}",
                    f"Effective NCNN: {row.get('effective_ncnn_summary', '')}",
                    f"NCNN model: {row.get('effective_ncnn_model', '')}",
                    f"NCNN scale: {row.get('effective_ncnn_scale', '')}",
                    f"NCNN tile: {row.get('effective_ncnn_tile', '')}",
                    f"NCNN extra args: {row.get('effective_ncnn_extra_args', '')}",
                    f"Preserve reason: {row.get('preserve_reason', '')}",
                    f"Correction mode: {row.get('correction_mode', '')}",
                    f"NCNN correction override: {row.get('effective_ncnn_correction', '')}",
                    f"Correction action: {row.get('correction_action', '')}",
                    f"Correction eligibility: {row.get('correction_eligibility', '')}",
                    f"Correction reason: {row.get('correction_reason', '')}",
                    f"Intermediate policy: {row.get('intermediate_policy', '')}",
                    f"Packed channels: {packed_text or 'none'}",
                    "",
                    "Semantic evidence:",
                    evidence_text,
                    "",
                    "Notes:",
                    notes_text,
                ]
            )
        )
