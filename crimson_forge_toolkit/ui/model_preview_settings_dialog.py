from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from crimson_forge_toolkit.models import (
    MODEL_PREVIEW_RENDER_LIMITS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODES,
    ModelPreviewRenderSettings,
    clamp_model_preview_render_settings,
)


class _PreviewSliderControl(QWidget):
    valueChanged = Signal()

    def __init__(
        self,
        *,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        suffix: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._step = max(float(step), 1e-6)
        self._decimals = max(0, int(decimals))
        self._suffix = str(suffix)
        self._slider_steps = max(1, int(round((self._maximum - self._minimum) / self._step)))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self._slider_steps)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, self._slider_steps // 10))
        self.slider.setTickInterval(max(1, self._slider_steps // 8))
        self.slider.setTickPosition(QSlider.NoTicks)
        self.value_label = QLabel("")
        self.value_label.setMinimumWidth(72)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setObjectName("HintLabel")

        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.value_label)

        self.slider.valueChanged.connect(self._handle_slider_changed)
        self._handle_slider_changed(self.slider.value())

    def _slider_to_value(self, slider_value: int) -> float:
        return self._minimum + (int(slider_value) * self._step)

    def _value_to_slider(self, value: float) -> int:
        normalized = max(self._minimum, min(self._maximum, float(value)))
        return max(0, min(self._slider_steps, int(round((normalized - self._minimum) / self._step))))

    def _format_value(self, value: float) -> str:
        if self._decimals <= 0:
            return f"{int(round(value))}{self._suffix}"
        return f"{value:.{self._decimals}f}{self._suffix}"

    def _handle_slider_changed(self, slider_value: int) -> None:
        self.value_label.setText(self._format_value(self._slider_to_value(slider_value)))
        self.valueChanged.emit()

    def set_value(self, value: float) -> None:
        slider_value = self._value_to_slider(value)
        self.slider.blockSignals(True)
        self.slider.setValue(slider_value)
        self.slider.blockSignals(False)
        self.value_label.setText(self._format_value(self._slider_to_value(slider_value)))

    def value(self) -> float:
        current = self._slider_to_value(self.slider.value())
        if self._decimals <= 0:
            return float(int(round(current)))
        return float(round(current, self._decimals))


class ModelPreviewSettingsDialog(QDialog):
    settings_changed = Signal(object)

    def __init__(
        self,
        *,
        settings: Optional[ModelPreviewRenderSettings] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("3D Preview Settings")
        self.setModal(False)
        self.resize(560, 420)
        self._applying_settings = False
        self._base_settings = clamp_model_preview_render_settings(settings)
        self._slider_controls: Dict[str, _PreviewSliderControl] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        intro_label = QLabel(
            "Realtime model-preview controls for the Archive Browser. Adjust these while the preview is visible to see the result immediately."
        )
        intro_label.setObjectName("HintLabel")
        intro_label.setWordWrap(True)
        root_layout.addWidget(intro_label)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, stretch=1)

        general_tab, general_layout = self._create_scroll_tab()
        controls_tab, controls_layout = self._create_scroll_tab()

        self.tabs.addTab(general_tab, "General")
        self.tabs.addTab(controls_tab, "Controls")

        general_form = QFormLayout()
        general_form.setContentsMargins(0, 0, 0, 0)
        general_form.setHorizontalSpacing(12)
        general_form.setVerticalSpacing(10)
        self.use_textures_checkbox = QCheckBox("Use textures when available")
        self.high_quality_checkbox = QCheckBox("Enable high-quality shading")
        self.visible_texture_mode_combo = QComboBox()
        for mode in MODEL_PREVIEW_VISIBLE_TEXTURE_MODES:
            self.visible_texture_mode_combo.addItem(
                MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS.get(mode, mode),
                mode,
            )
        general_form.addRow("", self.use_textures_checkbox)
        general_form.addRow("", self.high_quality_checkbox)
        general_form.addRow("Visible texture mode", self.visible_texture_mode_combo)
        general_layout.addLayout(general_form)
        general_hint = QLabel(
            "Use textures applies resolved preview DDS files when available. High-quality enables richer shaded preview when normal, material, or height support maps can be resolved for the selected model. Visible texture mode controls how aggressively sidecar-visible layers are allowed to replace the mesh-derived base texture."
        )
        general_hint.setObjectName("HintLabel")
        general_hint.setWordWrap(True)
        general_layout.addWidget(general_hint)
        general_layout.addStretch(1)

        controls_form = QFormLayout()
        controls_form.setContentsMargins(0, 0, 0, 0)
        controls_form.setHorizontalSpacing(12)
        controls_form.setVerticalSpacing(10)
        self._add_slider_row(
            controls_form,
            "Orbit sensitivity",
            "orbit_sensitivity",
            step=0.01,
            decimals=2,
        )
        self._add_slider_row(
            controls_form,
            "Pan sensitivity",
            "pan_sensitivity",
            step=0.05,
            decimals=2,
        )
        invert_widget = QWidget()
        invert_layout = QVBoxLayout(invert_widget)
        invert_layout.setContentsMargins(0, 0, 0, 0)
        invert_layout.setSpacing(6)
        self.invert_orbit_x_checkbox = QCheckBox("Invert orbit X")
        self.invert_orbit_y_checkbox = QCheckBox("Invert orbit Y")
        self.invert_pan_x_checkbox = QCheckBox("Invert pan X")
        self.invert_pan_y_checkbox = QCheckBox("Invert pan Y")
        invert_row_one = QHBoxLayout()
        invert_row_one.setContentsMargins(0, 0, 0, 0)
        invert_row_one.setSpacing(10)
        invert_row_one.addWidget(self.invert_orbit_x_checkbox)
        invert_row_one.addWidget(self.invert_orbit_y_checkbox)
        invert_row_one.addStretch(1)
        invert_row_two = QHBoxLayout()
        invert_row_two.setContentsMargins(0, 0, 0, 0)
        invert_row_two.setSpacing(10)
        invert_row_two.addWidget(self.invert_pan_x_checkbox)
        invert_row_two.addWidget(self.invert_pan_y_checkbox)
        invert_row_two.addStretch(1)
        invert_layout.addLayout(invert_row_one)
        invert_layout.addLayout(invert_row_two)
        controls_form.addRow("Control inversion", invert_widget)
        controls_layout.addLayout(controls_form)
        controls_hint = QLabel(
            "Reset keeps the inversion checkboxes as-is so you do not lose your preferred camera controls."
        )
        controls_hint.setObjectName("HintLabel")
        controls_hint.setWordWrap(True)
        controls_layout.addWidget(controls_hint)
        controls_layout.addStretch(1)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.reset_button = QPushButton("Reset to Defaults")
        self.close_button = QPushButton("Close")
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        root_layout.addLayout(button_row)

        for checkbox in (
            self.use_textures_checkbox,
            self.high_quality_checkbox,
            self.invert_orbit_x_checkbox,
            self.invert_orbit_y_checkbox,
            self.invert_pan_x_checkbox,
            self.invert_pan_y_checkbox,
        ):
            checkbox.toggled.connect(self._emit_settings_changed)
        self.visible_texture_mode_combo.currentIndexChanged.connect(self._emit_settings_changed)
        for control in self._slider_controls.values():
            control.valueChanged.connect(self._emit_settings_changed)
        self.reset_button.clicked.connect(self._reset_defaults)
        self.close_button.clicked.connect(self.close)

        self.set_settings(self._base_settings)

    def _create_scroll_tab(self) -> tuple[QWidget, QVBoxLayout]:
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        scroll_area.setWidget(content)
        return scroll_area, layout

    def _add_slider_row(
        self,
        layout: QFormLayout,
        label: str,
        key: str,
        *,
        step: float,
        decimals: int,
        suffix: str = "",
    ) -> None:
        minimum, maximum = MODEL_PREVIEW_RENDER_LIMITS[key]
        control = _PreviewSliderControl(
            minimum=float(minimum),
            maximum=float(maximum),
            step=float(step),
            decimals=int(decimals),
            suffix=suffix,
        )
        self._slider_controls[key] = control
        layout.addRow(label, control)

    def current_settings(self) -> ModelPreviewRenderSettings:
        current = clamp_model_preview_render_settings(self._base_settings)
        current.use_textures_by_default = self.use_textures_checkbox.isChecked()
        current.high_quality_by_default = self.high_quality_checkbox.isChecked()
        current.visible_texture_mode = str(self.visible_texture_mode_combo.currentData() or current.visible_texture_mode)
        current.orbit_sensitivity = self._slider_controls["orbit_sensitivity"].value()
        current.pan_sensitivity = self._slider_controls["pan_sensitivity"].value()
        current.invert_orbit_x = self.invert_orbit_x_checkbox.isChecked()
        current.invert_orbit_y = self.invert_orbit_y_checkbox.isChecked()
        current.invert_pan_x = self.invert_pan_x_checkbox.isChecked()
        current.invert_pan_y = self.invert_pan_y_checkbox.isChecked()
        return clamp_model_preview_render_settings(current)

    def set_settings(self, settings: ModelPreviewRenderSettings) -> None:
        clamped = clamp_model_preview_render_settings(settings)
        self._base_settings = clamped
        self._applying_settings = True
        try:
            self.use_textures_checkbox.setChecked(clamped.use_textures_by_default)
            self.high_quality_checkbox.setChecked(clamped.high_quality_by_default)
            visible_texture_mode_index = self.visible_texture_mode_combo.findData(clamped.visible_texture_mode)
            self.visible_texture_mode_combo.setCurrentIndex(max(0, visible_texture_mode_index))
            self.invert_orbit_x_checkbox.setChecked(clamped.invert_orbit_x)
            self.invert_orbit_y_checkbox.setChecked(clamped.invert_orbit_y)
            self.invert_pan_x_checkbox.setChecked(clamped.invert_pan_x)
            self.invert_pan_y_checkbox.setChecked(clamped.invert_pan_y)
            for key, control in self._slider_controls.items():
                control.set_value(float(getattr(clamped, key)))
        finally:
            self._applying_settings = False

    def _emit_settings_changed(self, *_args) -> None:
        if self._applying_settings:
            return
        self.settings_changed.emit(self.current_settings())

    def _reset_defaults(self) -> None:
        current = self.current_settings()
        defaults = clamp_model_preview_render_settings()
        defaults.invert_orbit_x = current.invert_orbit_x
        defaults.invert_orbit_y = current.invert_orbit_y
        defaults.invert_pan_x = current.invert_pan_x
        defaults.invert_pan_y = current.invert_pan_y
        self.set_settings(defaults)
        self.settings_changed.emit(self.current_settings())
