from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from crimson_texture_forge.core.upscale_profiles import get_texture_preset_definition
from crimson_texture_forge.constants import (
    DEFAULT_UI_THEME,
    UPSCALE_BACKEND_CHAINNER,
    UPSCALE_BACKEND_NONE,
    UPSCALE_BACKEND_ONNX_RUNTIME,
    DEFAULT_UPSCALE_POST_CORRECTION,
    UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
    UPSCALE_POST_CORRECTION_MATCH_LEVELS,
    UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
    UPSCALE_POST_CORRECTION_NONE,
    UPSCALE_BACKEND_REALESRGAN_NCNN,
    UPSCALE_TEXTURE_PRESET_ALL,
    UPSCALE_TEXTURE_PRESET_BALANCED,
    UPSCALE_TEXTURE_PRESET_COLOR_UI,
    UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
)
from crimson_texture_forge.ui.themes import build_app_stylesheet, get_theme

DIRECT_BACKEND_VALUES = {
    UPSCALE_BACKEND_REALESRGAN_NCNN,
    UPSCALE_BACKEND_ONNX_RUNTIME,
}


@dataclass(slots=True)
class SafeUpscaleWizardState:
    source_summary: dict[str, str] = field(default_factory=dict)
    backend: str = UPSCALE_BACKEND_NONE
    preset: str = UPSCALE_TEXTURE_PRESET_BALANCED
    scale: float = 4.0
    tile_size: int = 256
    post_correction_mode: str = DEFAULT_UPSCALE_POST_CORRECTION
    use_automatic_rules: bool = True
    retry_smaller_tile: bool = True
    loose_export: bool = True
    notes: str = ""


class SafeUpscaleWizard(QDialog):
    def __init__(
        self,
        *,
        theme_key: str = DEFAULT_UI_THEME,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SafeUpscaleWizard")
        self.setWindowTitle("Safe Upscale Wizard")
        self.setModal(True)
        self.resize(880, 720)
        self.setMinimumSize(760, 640)

        self._theme_key = theme_key or DEFAULT_UI_THEME
        self._accepted_state: Optional[SafeUpscaleWizardState] = None
        self._footer_summary_override_text = ""

        self._build_ui()
        self.set_theme(theme_key)
        self._refresh_state_summary()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(10)

        title = QLabel("Safe Upscale Wizard")
        title.setObjectName("WizardTitle")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 4)
        title_font.setWeight(QFont.DemiBold)
        title.setFont(title_font)
        root_layout.addWidget(title)

        subtitle = QLabel(
            "A guided path for choosing how files are upscaled, which texture types are allowed through the backend, and how safely the final DDS files are rebuilt."
        )
        subtitle.setWordWrap(True)
        subtitle.setObjectName("HintLabel")
        root_layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        root_layout.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 2, 2)
        content_layout.setSpacing(10)

        self.source_group = QGroupBox("1. Source Summary")
        source_layout = QFormLayout(self.source_group)
        source_layout.setContentsMargins(12, 14, 12, 12)
        source_layout.setHorizontalSpacing(12)
        source_layout.setVerticalSpacing(8)
        self.source_summary_labels: dict[str, QLabel] = {}
        for key, label_text in (
            ("source_root", "Source root"),
            ("archive_root", "Archive root"),
            ("original_dds_root", "Original DDS root"),
            ("png_root", "PNG root"),
            ("output_root", "Output root"),
            ("staging_png_root", "Staging PNG root"),
        ):
            value_label = QLabel("Not set")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.source_summary_labels[key] = value_label
            source_layout.addRow(label_text, value_label)
        self.source_notes_label = QLabel("No source summary has been populated yet.")
        self.source_notes_label.setWordWrap(True)
        self.source_notes_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        source_layout.addRow("Notes", self.source_notes_label)
        content_layout.addWidget(self.source_group)

        self.backend_group = QGroupBox("2. Backend Choice")
        backend_layout = QVBoxLayout(self.backend_group)
        backend_layout.setContentsMargins(12, 14, 12, 12)
        backend_layout.setSpacing(8)
        backend_row = QHBoxLayout()
        backend_row.setSpacing(10)
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("Disabled", UPSCALE_BACKEND_NONE)
        self.backend_combo.addItem("chaiNNer", UPSCALE_BACKEND_CHAINNER)
        self.backend_combo.addItem("Real-ESRGAN NCNN (direct)", UPSCALE_BACKEND_REALESRGAN_NCNN)
        self.backend_combo.addItem("ONNX Runtime (direct)", UPSCALE_BACKEND_ONNX_RUNTIME)
        backend_row.addWidget(QLabel("Backend"))
        backend_row.addWidget(self.backend_combo, 1)
        backend_layout.addLayout(backend_row)
        self.backend_summary_label = QLabel()
        self.backend_summary_label.setWordWrap(True)
        self.backend_summary_label.setObjectName("HintLabel")
        backend_layout.addWidget(self.backend_summary_label)
        content_layout.addWidget(self.backend_group)

        self.preset_group = QGroupBox("3. Texture Preset")
        preset_layout = QVBoxLayout(self.preset_group)
        preset_layout.setContentsMargins(12, 14, 12, 12)
        preset_layout.setSpacing(8)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Balanced mixed textures (recommended)", UPSCALE_TEXTURE_PRESET_BALANCED)
        self.preset_combo.addItem("Color + UI only (safer)", UPSCALE_TEXTURE_PRESET_COLOR_UI)
        self.preset_combo.addItem("Color + UI + emissive", UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE)
        self.preset_combo.addItem("All textures (advanced)", UPSCALE_TEXTURE_PRESET_ALL)
        self.preset_combo.setToolTip(
            "Controls which texture types are actually sent to the upscaler. It does not guarantee that the selected model will preserve brightness, contrast, or shading correctly."
        )
        preset_row.addWidget(QLabel("Preset"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_layout.addLayout(preset_row)
        self.preset_summary_label = QLabel()
        self.preset_summary_label.setWordWrap(True)
        self.preset_summary_label.setObjectName("HintLabel")
        preset_layout.addWidget(self.preset_summary_label)
        self.preset_warning_label = QLabel()
        self.preset_warning_label.setWordWrap(True)
        self.preset_warning_label.setObjectName("HintLabel")
        preset_layout.addWidget(self.preset_warning_label)
        content_layout.addWidget(self.preset_group)

        self.backend_controls_group = QGroupBox("4. Direct Backend Controls (NCNN / ONNX only)")
        backend_controls_layout = QFormLayout(self.backend_controls_group)
        backend_controls_layout.setContentsMargins(12, 14, 12, 12)
        backend_controls_layout.setHorizontalSpacing(12)
        backend_controls_layout.setVerticalSpacing(8)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(1.0, 16.0)
        self.scale_spin.setSingleStep(0.5)
        self.scale_spin.setDecimals(1)
        self.scale_spin.setValue(4.0)
        self.scale_spin.setToolTip(
            "Output scale used by direct backends. For predictable results, keep this close to the selected model's native scale."
        )
        self.tile_spin = QSpinBox()
        self.tile_spin.setRange(0, 4096)
        self.tile_spin.setSingleStep(16)
        self.tile_spin.setSuffix(" px")
        self.tile_spin.setValue(256)
        self.tile_spin.setToolTip(
            "Tile size for direct backends. 0 means no manual tiling. Smaller values use less VRAM and can recover from failures, but run slower."
        )
        self.post_correction_combo = QComboBox()
        self.post_correction_combo.addItem("Off (recommended)", UPSCALE_POST_CORRECTION_NONE)
        self.post_correction_combo.addItem("Match Mean Luma", UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA)
        self.post_correction_combo.addItem("Match Levels", UPSCALE_POST_CORRECTION_MATCH_LEVELS)
        self.post_correction_combo.addItem("Match Histogram", UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM)
        self.post_correction_combo.setToolTip(
            "Optional post-upscale color correction applied after the direct backend writes PNG output. "
            "Only visible color, UI, emissive, and impostor textures are corrected automatically."
        )
        backend_controls_layout.addRow("Scale", self.scale_spin)
        backend_controls_layout.addRow("Tile size", self.tile_spin)
        backend_controls_layout.addRow("Post correction", self.post_correction_combo)
        self.backend_controls_note = QLabel(
            "These controls only affect direct backends like Real-ESRGAN NCNN and ONNX Runtime. "
            "chaiNNer keeps using its own chain settings. Direct models can still shift brightness, contrast, or sharpness, "
            "so use Compare before bulk runs."
        )
        self.backend_controls_note.setWordWrap(True)
        self.backend_controls_note.setObjectName("HintLabel")
        backend_controls_layout.addRow(self.backend_controls_note)
        self.backend_controls_detail = QLabel()
        self.backend_controls_detail.setWordWrap(True)
        self.backend_controls_detail.setObjectName("HintLabel")
        backend_controls_layout.addRow(self.backend_controls_detail)
        content_layout.addWidget(self.backend_controls_group)

        self.safety_group = QGroupBox("5. Safety Summary")
        safety_layout = QVBoxLayout(self.safety_group)
        safety_layout.setContentsMargins(12, 14, 12, 12)
        safety_layout.setSpacing(8)
        self.automatic_rules_checkbox = QCheckBox("Use automatic color / format rules")
        self.retry_smaller_tile_checkbox = QCheckBox("Retry with smaller tile on failure")
        self.automatic_rules_checkbox.setChecked(True)
        self.retry_smaller_tile_checkbox.setChecked(True)
        safety_layout.addWidget(self.automatic_rules_checkbox)
        safety_layout.addWidget(self.retry_smaller_tile_checkbox)
        self.safety_summary_label = QLabel()
        self.safety_summary_label.setWordWrap(True)
        self.safety_summary_label.setObjectName("HintLabel")
        safety_layout.addWidget(self.safety_summary_label)
        content_layout.addWidget(self.safety_group)

        self.export_group = QGroupBox("6. Mod-Ready Loose Export")
        export_layout = QVBoxLayout(self.export_group)
        export_layout.setContentsMargins(12, 14, 12, 12)
        export_layout.setSpacing(8)
        self.loose_export_checkbox = QCheckBox("Create loose export output")
        self.loose_export_checkbox.setChecked(True)
        export_layout.addWidget(self.loose_export_checkbox)
        self.export_summary_label = QLabel(
            "When enabled, the wizard is intended to produce a loose folder tree that is easy to inspect, archive, or use as a mod-ready handoff."
        )
        self.export_summary_label.setWordWrap(True)
        self.export_summary_label.setObjectName("HintLabel")
        export_layout.addWidget(self.export_summary_label)
        content_layout.addWidget(self.export_group)

        self.footer_summary = QLabel()
        self.footer_summary.setWordWrap(True)
        self.footer_summary.setObjectName("HintLabel")
        content_layout.addWidget(self.footer_summary)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root_layout.addWidget(buttons)

        self.backend_combo.currentIndexChanged.connect(self._refresh_state_summary)
        self.preset_combo.currentIndexChanged.connect(self._refresh_state_summary)
        self.scale_spin.valueChanged.connect(self._refresh_state_summary)
        self.tile_spin.valueChanged.connect(self._refresh_state_summary)
        self.post_correction_combo.currentIndexChanged.connect(self._refresh_state_summary)
        self.automatic_rules_checkbox.toggled.connect(self._refresh_state_summary)
        self.retry_smaller_tile_checkbox.toggled.connect(self._refresh_state_summary)
        self.loose_export_checkbox.toggled.connect(self._refresh_state_summary)

    def set_theme(self, theme_key: str) -> None:
        resolved = theme_key or DEFAULT_UI_THEME
        self._theme_key = resolved
        theme = get_theme(resolved)
        extra_styles = f"""
        QDialog#SafeUpscaleWizard {{
            background: {theme["window"]};
        }}
        QLabel#WizardTitle {{
            color: {theme["text_strong"]};
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
        QLabel#HintLabel {{
            color: {theme["text_muted"]};
        }}
        """
        self.setStyleSheet(build_app_stylesheet(resolved) + extra_styles)

    def populate_from_config(self, config: Mapping[str, Any]) -> None:
        self.set_source_summary(config)

        backend = self._coerce_backend(
            config.get("upscale_backend")
            or config.get("backend")
            or config.get("backend_mode")
            or config.get("selected_backend")
        )
        if backend is not None:
            self.set_selected_backend(backend)

        preset = str(
            config.get("texture_preset")
            or config.get("preset")
            or config.get("texture_type_preset")
            or UPSCALE_TEXTURE_PRESET_BALANCED
        )
        self.set_texture_preset(preset)

        scale = self._coerce_float(config.get("scale"), self.scale_spin.value())
        self.set_scale(scale)

        tile = self._coerce_int(config.get("tile_size"), self.tile_spin.value())
        self.set_tile_size(tile)
        self.set_post_correction_mode(
            self._coerce_post_correction_mode(
                config.get("post_correction_mode"),
                self.selected_post_correction_mode(),
            )
        )

        self.set_use_automatic_rules(
            self._coerce_bool(config.get("use_automatic_rules"), self.automatic_rules_checkbox.isChecked())
        )
        self.set_retry_smaller_tile(
            self._coerce_bool(config.get("retry_smaller_tile"), self.retry_smaller_tile_checkbox.isChecked())
        )
        self.set_loose_export(
            self._coerce_bool(config.get("loose_export"), self.loose_export_checkbox.isChecked())
        )
        notes = str(config.get("notes") or config.get("summary") or "")
        self._footer_summary_override_text = notes.strip()
        self._refresh_state_summary()

    def set_source_summary(self, summary: Mapping[str, Any]) -> None:
        source_root = self._as_display_text(summary.get("source_root") or summary.get("package_root"))
        archive_root = self._as_display_text(summary.get("archive_root") or summary.get("package_root"))
        original_dds_root = self._as_display_text(summary.get("original_dds_root"))
        png_root = self._as_display_text(summary.get("png_root"))
        output_root = self._as_display_text(summary.get("output_root"))
        staging_root = self._as_display_text(summary.get("staging_png_root"))
        notes = self._as_display_text(summary.get("notes") or summary.get("summary"))

        self.source_summary_labels["source_root"].setText(source_root)
        self.source_summary_labels["archive_root"].setText(archive_root)
        self.source_summary_labels["original_dds_root"].setText(original_dds_root)
        self.source_summary_labels["png_root"].setText(png_root)
        self.source_summary_labels["output_root"].setText(output_root)
        self.source_summary_labels["staging_png_root"].setText(staging_root)
        self.source_notes_label.setText(notes)
        self._refresh_state_summary()

    def selected_backend(self) -> str:
        data = self.backend_combo.currentData()
        return str(data) if data is not None else UPSCALE_BACKEND_NONE

    def selected_preset(self) -> str:
        data = self.preset_combo.currentData()
        return str(data) if data is not None else UPSCALE_TEXTURE_PRESET_BALANCED

    def selected_scale(self) -> float:
        return float(self.scale_spin.value())

    def selected_tile_size(self) -> int:
        return int(self.tile_spin.value())

    def selected_post_correction_mode(self) -> str:
        data = self.post_correction_combo.currentData()
        return str(data) if data is not None else DEFAULT_UPSCALE_POST_CORRECTION

    def export_enabled(self) -> bool:
        return self.loose_export_checkbox.isChecked()

    def use_automatic_rules(self) -> bool:
        return self.automatic_rules_checkbox.isChecked()

    def retry_smaller_tile(self) -> bool:
        return self.retry_smaller_tile_checkbox.isChecked()

    def accepted_configuration(self) -> Optional[SafeUpscaleWizardState]:
        return self._accepted_state

    def current_configuration(self) -> SafeUpscaleWizardState:
        return SafeUpscaleWizardState(
            source_summary={key: label.text() for key, label in self.source_summary_labels.items()},
            backend=self.selected_backend(),
            preset=self.selected_preset(),
            scale=self.selected_scale(),
            tile_size=self.selected_tile_size(),
            post_correction_mode=self.selected_post_correction_mode(),
            use_automatic_rules=self.use_automatic_rules(),
            retry_smaller_tile=self.retry_smaller_tile(),
            loose_export=self.export_enabled(),
            notes=self.footer_summary.text(),
        )

    def set_selected_backend(self, backend: str) -> None:
        normalized = self._coerce_backend(backend) or UPSCALE_BACKEND_NONE
        index = self.backend_combo.findData(normalized)
        if index < 0:
            index = self.backend_combo.findData(UPSCALE_BACKEND_NONE)
        self.backend_combo.blockSignals(True)
        self.backend_combo.setCurrentIndex(max(0, index))
        self.backend_combo.blockSignals(False)
        self._refresh_state_summary()

    def set_texture_preset(self, preset: str) -> None:
        normalized = self._coerce_preset(preset)
        index = self.preset_combo.findData(normalized)
        if index < 0:
            index = self.preset_combo.findData(UPSCALE_TEXTURE_PRESET_BALANCED)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(max(0, index))
        self.preset_combo.blockSignals(False)
        self._refresh_state_summary()

    def set_scale(self, scale: float) -> None:
        self.scale_spin.setValue(max(self.scale_spin.minimum(), min(self.scale_spin.maximum(), float(scale))))
        self._refresh_state_summary()

    def set_tile_size(self, tile_size: int) -> None:
        self.tile_spin.setValue(max(self.tile_spin.minimum(), min(self.tile_spin.maximum(), int(tile_size))))
        self._refresh_state_summary()

    def set_post_correction_mode(self, mode: str) -> None:
        normalized = self._coerce_post_correction_mode(mode, DEFAULT_UPSCALE_POST_CORRECTION)
        index = self.post_correction_combo.findData(normalized)
        if index < 0:
            index = self.post_correction_combo.findData(DEFAULT_UPSCALE_POST_CORRECTION)
        self.post_correction_combo.blockSignals(True)
        self.post_correction_combo.setCurrentIndex(max(0, index))
        self.post_correction_combo.blockSignals(False)
        self._refresh_state_summary()

    def set_loose_export(self, enabled: bool) -> None:
        self.loose_export_checkbox.setChecked(bool(enabled))
        self._refresh_state_summary()

    def set_use_automatic_rules(self, enabled: bool) -> None:
        self.automatic_rules_checkbox.setChecked(bool(enabled))
        self._refresh_state_summary()

    def set_retry_smaller_tile(self, enabled: bool) -> None:
        self.retry_smaller_tile_checkbox.setChecked(bool(enabled))
        self._refresh_state_summary()

    def accept(self) -> None:  # type: ignore[override]
        self._accepted_state = self.current_configuration()
        super().accept()

    def _refresh_state_summary(self) -> None:
        backend = self.selected_backend()
        preset = self.selected_preset()
        if backend == UPSCALE_BACKEND_NONE:
            backend_text = "Upscaling is disabled. The wizard will only capture the chosen safety rules and loose export behavior."
        elif backend == UPSCALE_BACKEND_CHAINNER:
            backend_text = "chaiNNer is selected. The chain remains the source of truth for scale, tiling, models, and file flow. The direct controls below do not override the chain."
        elif backend == UPSCALE_BACKEND_REALESRGAN_NCNN:
            backend_text = "Real-ESRGAN NCNN is selected. The direct controls below decide the PNG upscale pass before DDS rebuild happens."
        else:
            backend_text = "ONNX Runtime is selected. The direct controls below decide the PNG upscale pass before DDS rebuild happens."
        self.backend_summary_label.setText(backend_text)

        preset_definition = get_texture_preset_definition(preset)
        upscale_list = ", ".join(preset_definition.upscale_types)
        copy_list = ", ".join(preset_definition.copy_types) if preset_definition.copy_types else "nothing"
        preset_text = (
            f"{preset_definition.description}\n"
            f"Sent to the upscaler: {upscale_list}.\n"
            f"Copied through unchanged: {copy_list}."
        )
        self.preset_summary_label.setText(preset_text)
        warning_text = (
            preset_definition.warning
            or "Even with a good preset, the selected model can still shift brightness or contrast. Use Compare on a small sample before running a large batch."
        )
        self.preset_warning_label.setText(warning_text)

        direct_enabled = backend in DIRECT_BACKEND_VALUES
        self.scale_spin.setEnabled(direct_enabled)
        self.tile_spin.setEnabled(direct_enabled)
        self.post_correction_combo.setEnabled(direct_enabled)
        if direct_enabled:
            self.backend_controls_detail.setText(
                "Scale controls final PNG size for the direct backend. Tile size controls memory usage: 0 means no manual tiling, while smaller tiles are slower but safer on low-VRAM systems. "
                "Post correction can optionally pull visible color textures back toward the source luma or tonal range before DDS rebuild. "
                "DDS format and mip handling are still decided later by the DDS Output settings in the main workflow."
            )
        else:
            self.backend_controls_detail.setText(
                "Because chaiNNer or Disabled mode is selected, these direct-backend controls are informational only right now."
            )

        safety_lines = []
        if self.automatic_rules_checkbox.isChecked():
            safety_lines.append("Automatic color and format rules are enabled for DDS rebuild.")
        else:
            safety_lines.append("Automatic color and format rules are disabled, so format and color mistakes are more likely.")
        if self.retry_smaller_tile_checkbox.isChecked():
            safety_lines.append("Retry with smaller tile is enabled for direct backends.")
        else:
            safety_lines.append("Retry with smaller tile is disabled.")
        if backend == UPSCALE_BACKEND_NONE:
            safety_lines.append("No backend is selected, so nothing will be upscaled until a backend is chosen.")
        elif backend == UPSCALE_BACKEND_CHAINNER:
            safety_lines.append("chaiNNer chain behavior still controls the final file flow.")
        else:
            safety_lines.append("Direct backend mode is active, so model choice matters. Brightness, contrast, and detail character come from the selected model, not from the preset alone.")
        self.safety_summary_label.setText(" ".join(safety_lines))

        export_text = (
            "Loose export is enabled. The workflow should end in a mod-ready loose folder tree with preserved paths."
            if self.loose_export_checkbox.isChecked()
            else "Loose export is disabled. The wizard will only represent the processing plan."
        )
        self.export_summary_label.setText(export_text)

        if self._footer_summary_override_text:
            self.footer_summary.setText(self._footer_summary_override_text)
            return

        active_sources = [
            label.text()
            for label in self.source_summary_labels.values()
            if label.text() and label.text() != "Not set"
        ]
        if active_sources:
            self.footer_summary.setText("Configured sources are present. Start with the recommended preset and compare a small sample before running a full batch.")
        else:
            self.footer_summary.setText("No source summary has been populated yet. Populate the wizard from a selected archive or workflow config later.")

    def _as_display_text(self, value: Any) -> str:
        if value is None:
            return "Not set"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        text = str(value).strip()
        return text if text else "Not set"

    def _coerce_backend(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {
            UPSCALE_BACKEND_NONE,
            UPSCALE_BACKEND_CHAINNER,
            UPSCALE_BACKEND_REALESRGAN_NCNN,
            UPSCALE_BACKEND_ONNX_RUNTIME,
        }:
            return text
        if text in {"none", "off", "false", "0"}:
            return UPSCALE_BACKEND_NONE
        if text in {"chainner", "chaiNNer".lower()}:
            return UPSCALE_BACKEND_CHAINNER
        if text in {"realesrgan", "realesrgan_ncnn", "real-esrgan", "real_esrgan_ncnn"}:
            return UPSCALE_BACKEND_REALESRGAN_NCNN
        if text in {"onnx", "onnx_runtime", "onnxruntime"}:
            return UPSCALE_BACKEND_ONNX_RUNTIME
        return None

    def _coerce_preset(self, value: Any) -> str:
        if value is None:
            return UPSCALE_TEXTURE_PRESET_BALANCED
        text = str(value).strip().lower()
        mapping = {
            "balanced": UPSCALE_TEXTURE_PRESET_BALANCED,
            "safe": UPSCALE_TEXTURE_PRESET_BALANCED,
            "color_ui": UPSCALE_TEXTURE_PRESET_COLOR_UI,
            "color-ui": UPSCALE_TEXTURE_PRESET_COLOR_UI,
            "color_ui_emissive": UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
            "color-ui-emissive": UPSCALE_TEXTURE_PRESET_COLOR_UI_EMISSIVE,
            "all": UPSCALE_TEXTURE_PRESET_ALL,
        }
        return mapping.get(text, UPSCALE_TEXTURE_PRESET_BALANCED)

    def _coerce_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _coerce_post_correction_mode(self, value: Any, default: str) -> str:
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {
            UPSCALE_POST_CORRECTION_NONE,
            UPSCALE_POST_CORRECTION_MATCH_MEAN_LUMA,
            UPSCALE_POST_CORRECTION_MATCH_LEVELS,
            UPSCALE_POST_CORRECTION_MATCH_HISTOGRAM,
        }:
            return text
        return default

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            if value is None:
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default
