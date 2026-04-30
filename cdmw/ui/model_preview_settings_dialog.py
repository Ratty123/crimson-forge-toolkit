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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cdmw.models import (
    ArchivePerformanceSettings,
    MODEL_PREVIEW_ALPHA_HANDLING_LABELS,
    MODEL_PREVIEW_ALPHA_HANDLING_MODES,
    MODEL_PREVIEW_DIFFUSE_SWIZZLE_LABELS,
    MODEL_PREVIEW_DIFFUSE_SWIZZLE_MODES,
    MODEL_PREVIEW_RENDER_LIMITS,
    MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODE_LABELS,
    MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODES,
    MODEL_PREVIEW_SAMPLER_PROBE_LABELS,
    MODEL_PREVIEW_SAMPLER_PROBE_MODES,
    MODEL_PREVIEW_TEXTURE_PROBE_SOURCE_LABELS,
    MODEL_PREVIEW_TEXTURE_PROBE_SOURCES,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODES,
    ModelPreviewRenderSettings,
    clamp_archive_performance_settings,
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
    archive_performance_changed = Signal(object)
    clear_preview_cache_requested = Signal()

    def __init__(
        self,
        *,
        settings: Optional[ModelPreviewRenderSettings] = None,
        archive_performance_settings: Optional[ArchivePerformanceSettings] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("3D Preview Settings")
        self.setModal(False)
        self.resize(560, 420)
        self._applying_settings = False
        self._base_settings = clamp_model_preview_render_settings(settings)
        self._archive_performance_settings = clamp_archive_performance_settings(archive_performance_settings)
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
        advanced_warning_label = QLabel(
            "Advanced diagnostics and render options can be expensive, visually incorrect, asset-dependent, or have no visible effect on some previews. Use them for inspection rather than as guaranteed final rendering."
        )
        advanced_warning_label.setObjectName("WarningText")
        advanced_warning_label.setWordWrap(True)
        root_layout.addWidget(advanced_warning_label)

        self.tabs = QTabWidget()
        root_layout.addWidget(self.tabs, stretch=1)

        general_tab, general_layout = self._create_scroll_tab()
        controls_tab, controls_layout = self._create_scroll_tab()
        diagnostics_tab, diagnostics_layout = self._create_scroll_tab()
        performance_tab, performance_layout = self._create_scroll_tab()

        self.tabs.addTab(general_tab, "General")
        self.tabs.addTab(diagnostics_tab, "Render Diagnostics")
        self.tabs.addTab(controls_tab, "Controls")
        self.tabs.addTab(performance_tab, "Archive Performance")

        general_form = QFormLayout()
        general_form.setContentsMargins(0, 0, 0, 0)
        general_form.setHorizontalSpacing(12)
        general_form.setVerticalSpacing(10)
        self.use_textures_checkbox = QCheckBox("Use textures when available")
        self.high_quality_checkbox = QCheckBox("Use support-map preview shading")
        self.visible_texture_mode_combo = QComboBox()
        for mode in MODEL_PREVIEW_VISIBLE_TEXTURE_MODES:
            self.visible_texture_mode_combo.addItem(
                MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS.get(mode, mode),
                mode,
            )
        self.render_diagnostic_mode_combo = QComboBox()
        for mode in MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODES:
            self.render_diagnostic_mode_combo.addItem(
                MODEL_PREVIEW_RENDER_DIAGNOSTIC_MODE_LABELS.get(mode, mode),
                mode,
            )
        general_form.addRow("", self.use_textures_checkbox)
        general_form.addRow("", self.high_quality_checkbox)
        general_form.addRow("Visible texture mode", self.visible_texture_mode_combo)
        general_form.addRow("Diagnostic render mode", self.render_diagnostic_mode_combo)
        general_layout.addLayout(general_form)
        general_hint = QLabel(
            "Use textures applies resolved preview DDS files when available. Support-map preview shading can sample resolved normal, material, or height maps for an approximate asset-dependent preview. Visible texture mode controls how aggressively sidecar-visible layers are allowed to replace the mesh-derived base texture."
        )
        general_hint.setObjectName("HintLabel")
        general_hint.setWordWrap(True)
        general_layout.addWidget(general_hint)
        general_layout.addStretch(1)

        diagnostics_form = QFormLayout()
        diagnostics_form.setContentsMargins(0, 0, 0, 0)
        diagnostics_form.setHorizontalSpacing(12)
        diagnostics_form.setVerticalSpacing(10)
        self.alpha_handling_combo = QComboBox()
        for mode in MODEL_PREVIEW_ALPHA_HANDLING_MODES:
            self.alpha_handling_combo.addItem(MODEL_PREVIEW_ALPHA_HANDLING_LABELS.get(mode, mode), mode)
        self.texture_probe_source_combo = QComboBox()
        for source in MODEL_PREVIEW_TEXTURE_PROBE_SOURCES:
            self.texture_probe_source_combo.addItem(MODEL_PREVIEW_TEXTURE_PROBE_SOURCE_LABELS.get(source, source), source)
        self.texture_probe_source_combo.setToolTip(
            "Selects the texture shown by Selected Texture Probe. Changing this value switches the diagnostic render mode to Selected Texture Probe."
        )
        self.sampler_probe_combo = QComboBox()
        for mode in MODEL_PREVIEW_SAMPLER_PROBE_MODES:
            self.sampler_probe_combo.addItem(MODEL_PREVIEW_SAMPLER_PROBE_LABELS.get(mode, mode), mode)
        self.diffuse_swizzle_combo = QComboBox()
        for mode in MODEL_PREVIEW_DIFFUSE_SWIZZLE_MODES:
            self.diffuse_swizzle_combo.addItem(MODEL_PREVIEW_DIFFUSE_SWIZZLE_LABELS.get(mode, mode), mode)
        diagnostics_form.addRow("Alpha handling", self.alpha_handling_combo)
        diagnostics_form.addRow("Probe texture", self.texture_probe_source_combo)
        diagnostics_form.addRow("Sampler probe", self.sampler_probe_combo)
        diagnostics_form.addRow("Diffuse swizzle", self.diffuse_swizzle_combo)
        self.disable_tint_checkbox = QCheckBox("Disable base tint")
        self.disable_brightness_checkbox = QCheckBox("Disable brightness")
        self.disable_uv_scale_checkbox = QCheckBox("Disable UV scale")
        self.force_nearest_no_mipmaps_checkbox = QCheckBox("Force nearest filtering / no mipmaps")
        self.disable_normal_map_checkbox = QCheckBox("Disable normal map")
        self.disable_material_map_checkbox = QCheckBox("Disable material map")
        self.disable_height_map_checkbox = QCheckBox("Disable height map")
        self.disable_all_support_maps_checkbox = QCheckBox("Disable all support maps")
        self.disable_lighting_checkbox = QCheckBox("Disable lighting")
        self.disable_depth_test_checkbox = QCheckBox("Disable depth test")
        self.show_texture_debug_strip_checkbox = QCheckBox("Show texture debug strip")
        for checkbox in (
            self.disable_tint_checkbox,
            self.disable_brightness_checkbox,
            self.disable_uv_scale_checkbox,
            self.force_nearest_no_mipmaps_checkbox,
            self.disable_normal_map_checkbox,
            self.disable_material_map_checkbox,
            self.disable_height_map_checkbox,
            self.disable_all_support_maps_checkbox,
            self.disable_lighting_checkbox,
            self.disable_depth_test_checkbox,
            self.show_texture_debug_strip_checkbox,
        ):
            diagnostics_form.addRow("", checkbox)
        self.solo_batch_spin = QSpinBox()
        self.solo_batch_spin.setRange(-1, 4096)
        self.solo_batch_spin.setSingleStep(1)
        self.solo_batch_spin.setToolTip("-1 draws all batches. Any other value draws only that batch index.")
        diagnostics_form.addRow("Solo batch index", self.solo_batch_spin)
        diagnostics_layout.addLayout(diagnostics_form)
        diagnostics_hint = QLabel(
            "Use Selected Texture Probe with Probe texture to inspect Base, Normal, Material, or Height bindings directly. Base Texture Raw always samples the base/color binding. Normal, material, and height toggles only change previews with resolved support-map slots."
        )
        diagnostics_hint.setObjectName("HintLabel")
        diagnostics_hint.setWordWrap(True)
        diagnostics_layout.addWidget(diagnostics_hint)
        diagnostics_layout.addStretch(1)

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

        performance_form = QFormLayout()
        performance_form.setContentsMargins(0, 0, 0, 0)
        performance_form.setHorizontalSpacing(12)
        performance_form.setVerticalSpacing(10)
        self.sidecar_indexing_enabled_checkbox = QCheckBox("Index texture sidecars for DDS related-file discovery")
        self.sidecar_indexing_enabled_checkbox.setToolTip(
            "Builds a whole-archive .pami/.pac_xml lookup used for DDS reverse references and richer related-file lists. "
            "Selected .pam/.pac previews still parse their direct sidecar lazily when this is off."
        )
        self.sidecar_worker_mode_combo = QComboBox()
        self.sidecar_worker_mode_combo.addItem("Auto Balanced", 0)
        self.sidecar_worker_mode_combo.addItem("Manual", 1)
        self.sidecar_worker_spin = QSpinBox()
        self.sidecar_worker_spin.setRange(1, 16)
        self.sidecar_worker_spin.setSingleStep(1)
        self.sidecar_worker_spin.setToolTip("Manual worker count for indexing .pami and .pac_xml texture sidecars.")
        worker_row = QWidget()
        worker_layout = QHBoxLayout(worker_row)
        worker_layout.setContentsMargins(0, 0, 0, 0)
        worker_layout.setSpacing(8)
        worker_layout.addWidget(self.sidecar_worker_mode_combo)
        worker_layout.addWidget(self.sidecar_worker_spin)
        worker_layout.addStretch(1)
        self.preview_cache_limit_spin = QSpinBox()
        self.preview_cache_limit_spin.setRange(12, 256)
        self.preview_cache_limit_spin.setSingleStep(4)
        self.preview_cache_limit_spin.setToolTip("Number of archive preview results kept in memory while browsing.")
        self.quick_then_full_checkbox = QCheckBox("Show quick metadata first, then full 3D preview")
        self.maximum_indexing_priority_checkbox = QCheckBox("Prioritize indexing over UI responsiveness")
        self.maximum_indexing_priority_checkbox.setToolTip(
            "Runs texture sidecar indexing at normal thread priority instead of low priority. This applies to the next sidecar indexing run."
        )
        self.clear_preview_cache_button = QPushButton("Clear Preview Cache")
        self.clear_preview_cache_button.setToolTip("Clears the in-memory Archive Browser preview cache. Sidecar scan caches on disk are not removed.")
        performance_form.addRow("", self.sidecar_indexing_enabled_checkbox)
        performance_form.addRow("Sidecar workers (1-16)", worker_row)
        performance_form.addRow("Preview cache size (12-256)", self.preview_cache_limit_spin)
        performance_form.addRow("", self.quick_then_full_checkbox)
        performance_form.addRow("", self.maximum_indexing_priority_checkbox)
        performance_form.addRow("", self.clear_preview_cache_button)
        performance_layout.addLayout(performance_form)
        performance_hint = QLabel(
            "Global sidecar indexing is optional because it can be expensive on full archives. Worker count range: 1-16. Preview cache range: 12-256 entries. Auto Balanced scales by CPU count and PAZ grouping; high manual values can become disk-bound."
        )
        performance_hint.setObjectName("HintLabel")
        performance_hint.setWordWrap(True)
        performance_layout.addWidget(performance_hint)
        performance_layout.addStretch(1)

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
        self.render_diagnostic_mode_combo.currentIndexChanged.connect(self._handle_render_diagnostic_mode_changed)
        for combo in (
            self.alpha_handling_combo,
            self.sampler_probe_combo,
            self.diffuse_swizzle_combo,
        ):
            combo.currentIndexChanged.connect(self._emit_settings_changed)
        self.texture_probe_source_combo.currentIndexChanged.connect(self._handle_texture_probe_source_changed)
        for checkbox in (
            self.disable_tint_checkbox,
            self.disable_brightness_checkbox,
            self.disable_uv_scale_checkbox,
            self.force_nearest_no_mipmaps_checkbox,
            self.disable_normal_map_checkbox,
            self.disable_material_map_checkbox,
            self.disable_height_map_checkbox,
            self.disable_all_support_maps_checkbox,
            self.disable_lighting_checkbox,
            self.disable_depth_test_checkbox,
            self.show_texture_debug_strip_checkbox,
        ):
            checkbox.toggled.connect(self._emit_settings_changed)
        self.solo_batch_spin.valueChanged.connect(self._emit_settings_changed)
        for control in self._slider_controls.values():
            control.valueChanged.connect(self._emit_settings_changed)
        self.sidecar_worker_mode_combo.currentIndexChanged.connect(self._handle_archive_performance_changed)
        self.sidecar_indexing_enabled_checkbox.toggled.connect(self._handle_archive_performance_changed)
        self.sidecar_worker_spin.valueChanged.connect(self._handle_archive_performance_changed)
        self.preview_cache_limit_spin.valueChanged.connect(self._handle_archive_performance_changed)
        self.quick_then_full_checkbox.toggled.connect(self._handle_archive_performance_changed)
        self.maximum_indexing_priority_checkbox.toggled.connect(self._handle_archive_performance_changed)
        self.clear_preview_cache_button.clicked.connect(self.clear_preview_cache_requested.emit)
        self.reset_button.clicked.connect(self._reset_defaults)
        self.close_button.clicked.connect(self.close)

        self.set_settings(self._base_settings)
        self.set_archive_performance_settings(self._archive_performance_settings)

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
        current.render_diagnostic_mode = str(
            self.render_diagnostic_mode_combo.currentData() or current.render_diagnostic_mode
        )
        current.alpha_handling_mode = str(self.alpha_handling_combo.currentData() or current.alpha_handling_mode)
        current.texture_probe_source = str(self.texture_probe_source_combo.currentData() or current.texture_probe_source)
        current.sampler_probe_mode = str(self.sampler_probe_combo.currentData() or current.sampler_probe_mode)
        current.diffuse_swizzle_mode = str(self.diffuse_swizzle_combo.currentData() or current.diffuse_swizzle_mode)
        current.disable_tint = self.disable_tint_checkbox.isChecked()
        current.disable_brightness = self.disable_brightness_checkbox.isChecked()
        current.disable_uv_scale = self.disable_uv_scale_checkbox.isChecked()
        current.force_nearest_no_mipmaps = self.force_nearest_no_mipmaps_checkbox.isChecked()
        current.disable_normal_map = self.disable_normal_map_checkbox.isChecked()
        current.disable_material_map = self.disable_material_map_checkbox.isChecked()
        current.disable_height_map = self.disable_height_map_checkbox.isChecked()
        current.disable_all_support_maps = self.disable_all_support_maps_checkbox.isChecked()
        current.disable_lighting = self.disable_lighting_checkbox.isChecked()
        current.disable_depth_test = self.disable_depth_test_checkbox.isChecked()
        current.show_texture_debug_strip = self.show_texture_debug_strip_checkbox.isChecked()
        current.solo_batch_index = self.solo_batch_spin.value()
        current.orbit_sensitivity = self._slider_controls["orbit_sensitivity"].value()
        current.pan_sensitivity = self._slider_controls["pan_sensitivity"].value()
        current.invert_orbit_x = self.invert_orbit_x_checkbox.isChecked()
        current.invert_orbit_y = self.invert_orbit_y_checkbox.isChecked()
        current.invert_pan_x = self.invert_pan_x_checkbox.isChecked()
        current.invert_pan_y = self.invert_pan_y_checkbox.isChecked()
        for key, control in self._slider_controls.items():
            if hasattr(current, key):
                setattr(current, key, control.value())
        return clamp_model_preview_render_settings(current)

    def current_archive_performance_settings(self) -> ArchivePerformanceSettings:
        worker_count = self.sidecar_worker_spin.value() if int(self.sidecar_worker_mode_combo.currentData() or 0) else 0
        return clamp_archive_performance_settings(
            ArchivePerformanceSettings(
                enable_sidecar_indexing=self.sidecar_indexing_enabled_checkbox.isChecked(),
                sidecar_worker_count=worker_count,
                preview_cache_limit=self.preview_cache_limit_spin.value(),
                quick_then_full_preview=self.quick_then_full_checkbox.isChecked(),
                maximum_indexing_priority=self.maximum_indexing_priority_checkbox.isChecked(),
            )
        )

    def set_settings(self, settings: ModelPreviewRenderSettings) -> None:
        clamped = clamp_model_preview_render_settings(settings)
        self._base_settings = clamped
        self._applying_settings = True
        try:
            self.use_textures_checkbox.setChecked(clamped.use_textures_by_default)
            self.high_quality_checkbox.setChecked(clamped.high_quality_by_default)
            visible_texture_mode_index = self.visible_texture_mode_combo.findData(clamped.visible_texture_mode)
            self.visible_texture_mode_combo.setCurrentIndex(max(0, visible_texture_mode_index))
            render_diagnostic_mode_index = self.render_diagnostic_mode_combo.findData(clamped.render_diagnostic_mode)
            self.render_diagnostic_mode_combo.setCurrentIndex(max(0, render_diagnostic_mode_index))
            alpha_index = self.alpha_handling_combo.findData(clamped.alpha_handling_mode)
            self.alpha_handling_combo.setCurrentIndex(max(0, alpha_index))
            source_index = self.texture_probe_source_combo.findData(clamped.texture_probe_source)
            self.texture_probe_source_combo.setCurrentIndex(max(0, source_index))
            sampler_index = self.sampler_probe_combo.findData(clamped.sampler_probe_mode)
            self.sampler_probe_combo.setCurrentIndex(max(0, sampler_index))
            swizzle_index = self.diffuse_swizzle_combo.findData(clamped.diffuse_swizzle_mode)
            self.diffuse_swizzle_combo.setCurrentIndex(max(0, swizzle_index))
            self.disable_tint_checkbox.setChecked(clamped.disable_tint)
            self.disable_brightness_checkbox.setChecked(clamped.disable_brightness)
            self.disable_uv_scale_checkbox.setChecked(clamped.disable_uv_scale)
            self.force_nearest_no_mipmaps_checkbox.setChecked(clamped.force_nearest_no_mipmaps)
            self.disable_normal_map_checkbox.setChecked(clamped.disable_normal_map)
            self.disable_material_map_checkbox.setChecked(clamped.disable_material_map)
            self.disable_height_map_checkbox.setChecked(clamped.disable_height_map)
            self.disable_all_support_maps_checkbox.setChecked(clamped.disable_all_support_maps)
            self.disable_lighting_checkbox.setChecked(clamped.disable_lighting)
            self.disable_depth_test_checkbox.setChecked(clamped.disable_depth_test)
            self.show_texture_debug_strip_checkbox.setChecked(clamped.show_texture_debug_strip)
            self.solo_batch_spin.setValue(clamped.solo_batch_index)
            self.invert_orbit_x_checkbox.setChecked(clamped.invert_orbit_x)
            self.invert_orbit_y_checkbox.setChecked(clamped.invert_orbit_y)
            self.invert_pan_x_checkbox.setChecked(clamped.invert_pan_x)
            self.invert_pan_y_checkbox.setChecked(clamped.invert_pan_y)
            for key, control in self._slider_controls.items():
                control.set_value(float(getattr(clamped, key)))
        finally:
            self._applying_settings = False
        self._sync_probe_controls_enabled()

    def set_archive_performance_settings(self, settings: Optional[ArchivePerformanceSettings]) -> None:
        clamped = clamp_archive_performance_settings(settings)
        self._archive_performance_settings = clamped
        self._applying_settings = True
        try:
            self.sidecar_indexing_enabled_checkbox.setChecked(clamped.enable_sidecar_indexing)
            self.sidecar_worker_mode_combo.setCurrentIndex(1 if clamped.sidecar_worker_count > 0 else 0)
            self.sidecar_worker_spin.setValue(max(1, clamped.sidecar_worker_count or 4))
            worker_controls_enabled = clamped.enable_sidecar_indexing
            self.sidecar_worker_mode_combo.setEnabled(worker_controls_enabled)
            self.sidecar_worker_spin.setEnabled(worker_controls_enabled and clamped.sidecar_worker_count > 0)
            self.maximum_indexing_priority_checkbox.setEnabled(worker_controls_enabled)
            self.preview_cache_limit_spin.setValue(clamped.preview_cache_limit)
            self.quick_then_full_checkbox.setChecked(clamped.quick_then_full_preview)
            self.maximum_indexing_priority_checkbox.setChecked(
                clamped.enable_sidecar_indexing and clamped.maximum_indexing_priority
            )
        finally:
            self._applying_settings = False

    def _emit_settings_changed(self, *_args) -> None:
        if self._applying_settings:
            return
        self._sync_probe_controls_enabled()
        self.settings_changed.emit(self.current_settings())

    def _handle_render_diagnostic_mode_changed(self, *_args) -> None:
        self._sync_probe_controls_enabled()
        self._emit_settings_changed()

    def _handle_texture_probe_source_changed(self, *_args) -> None:
        if self._applying_settings:
            return
        if str(self.render_diagnostic_mode_combo.currentData() or "").strip().lower() != "texture_probe":
            texture_probe_index = self.render_diagnostic_mode_combo.findData("texture_probe")
            if texture_probe_index >= 0:
                self.render_diagnostic_mode_combo.blockSignals(True)
                try:
                    self.render_diagnostic_mode_combo.setCurrentIndex(texture_probe_index)
                finally:
                    self.render_diagnostic_mode_combo.blockSignals(False)
        self._sync_probe_controls_enabled()
        self._emit_settings_changed()

    def _sync_probe_controls_enabled(self) -> None:
        mode = str(self.render_diagnostic_mode_combo.currentData() or "").strip().lower()
        self.texture_probe_source_combo.setEnabled(True)
        if mode == "texture_probe":
            self.texture_probe_source_combo.setToolTip(
                "Selects which resolved texture slot is drawn directly: Base, Normal, Material, or Height."
            )
        else:
            self.texture_probe_source_combo.setToolTip(
                "Selecting a value switches Diagnostic render mode to Selected Texture Probe, where this control directly changes the preview."
            )
        relief_control_modes = {"rich_lit", "height_calibrated", "relief_control_test"}
        relief_controls_enabled = bool(
            mode in relief_control_modes
            and self.use_textures_checkbox.isChecked()
            and self.high_quality_checkbox.isChecked()
        )
        relief_tooltip = (
            "Controls Enhanced Relief Preview using true height maps or base-derived relief."
            if relief_controls_enabled
            else "Select Enhanced Relief Preview or a relief diagnostic mode, then enable textures plus support-map preview shading."
        )
        for key in ("height_effect_max", "specular_max", "shininess_max"):
            control = self._slider_controls.get(key)
            if control is not None:
                control.setEnabled(relief_controls_enabled)
                control.setToolTip(relief_tooltip)

    def _handle_archive_performance_changed(self, *_args) -> None:
        manual = int(self.sidecar_worker_mode_combo.currentData() or 0) == 1
        enabled = self.sidecar_indexing_enabled_checkbox.isChecked()
        if not enabled and self.maximum_indexing_priority_checkbox.isChecked():
            self.maximum_indexing_priority_checkbox.blockSignals(True)
            self.maximum_indexing_priority_checkbox.setChecked(False)
            self.maximum_indexing_priority_checkbox.blockSignals(False)
        self.sidecar_worker_mode_combo.setEnabled(enabled)
        self.sidecar_worker_spin.setEnabled(enabled and manual)
        self.maximum_indexing_priority_checkbox.setEnabled(enabled)
        if self._applying_settings:
            return
        self.archive_performance_changed.emit(self.current_archive_performance_settings())

    def _reset_defaults(self) -> None:
        current = self.current_settings()
        defaults = clamp_model_preview_render_settings()
        defaults.invert_orbit_x = current.invert_orbit_x
        defaults.invert_orbit_y = current.invert_orbit_y
        defaults.invert_pan_x = current.invert_pan_x
        defaults.invert_pan_y = current.invert_pan_y
        self.set_settings(defaults)
        self.set_archive_performance_settings(ArchivePerformanceSettings())
        self.settings_changed.emit(self.current_settings())
        self.archive_performance_changed.emit(self.current_archive_performance_settings())
