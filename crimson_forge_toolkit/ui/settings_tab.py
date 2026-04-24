from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from crimson_forge_toolkit.constants import (
    DEFAULT_UI_DATA_FONT_SIZE,
    DEFAULT_UI_DENSITY,
    DEFAULT_UI_FONT_SIZE,
    DEFAULT_UI_FONT_FAMILY,
    DEFAULT_UI_LOG_FONT_BOLD,
    DEFAULT_UI_LOG_FONT_FAMILY,
    DEFAULT_UI_LOG_FONT_SIZE,
    DEFAULT_UI_THEME,
    LOG_FONT_FAMILY_OPTIONS,
    UI_FONT_SIZE_MAX,
    UI_FONT_SIZE_MIN,
    UI_FONT_FAMILY_OPTIONS,
)
from crimson_forge_toolkit.models import (
    MODEL_PREVIEW_RENDER_LIMITS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS,
    MODEL_PREVIEW_VISIBLE_TEXTURE_MODES,
    ModelPreviewRenderSettings,
    clamp_model_preview_render_settings,
)
from crimson_forge_toolkit.ui.localization import BUILTIN_LANGUAGES
from crimson_forge_toolkit.ui.themes import UI_THEME_SCHEMES


class SettingsTab(QWidget):
    theme_changed = Signal(str)
    crash_capture_changed = Signal(bool)
    model_preview_settings_changed = Signal(object)
    language_changed = Signal(str)
    export_language_requested = Signal()
    import_language_requested = Signal()

    def __init__(self, *, settings, theme_key: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._settings_ready = False
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(250)
        self._settings_save_timer.timeout.connect(self._save_settings)
        self._appearance_apply_timer = QTimer(self)
        self._appearance_apply_timer.setSingleShot(True)
        self._appearance_apply_timer.setInterval(140)
        self._appearance_apply_timer.timeout.connect(self._apply_pending_appearance_change)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        root_layout.addWidget(scroll_area)

        content = QWidget()
        scroll_area.setWidget(content)

        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(10)

        summary = QLabel(
            "Persistent global preferences for startup behavior, archive loading, UI layout memory, safety prompts, and 3D preview rendering."
        )
        summary.setWordWrap(True)
        summary.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        summary.setObjectName("HintLabel")
        content_layout.addWidget(summary)

        columns_widget = QWidget()
        columns_layout = QHBoxLayout(columns_widget)
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(12)
        self.left_column = QVBoxLayout()
        self.left_column.setContentsMargins(0, 0, 0, 0)
        self.left_column.setSpacing(10)
        self.right_column = QVBoxLayout()
        self.right_column.setContentsMargins(0, 0, 0, 0)
        self.right_column.setSpacing(10)
        columns_layout.addLayout(self.left_column, stretch=1)
        columns_layout.addLayout(self.right_column, stretch=1)
        content_layout.addWidget(columns_widget)

        appearance_group = QGroupBox("Appearance")
        appearance_layout = QFormLayout(appearance_group)
        appearance_layout.setContentsMargins(12, 14, 12, 12)
        appearance_layout.setHorizontalSpacing(12)
        appearance_layout.setVerticalSpacing(10)
        self.theme_combo = QComboBox()
        for key, theme in UI_THEME_SCHEMES.items():
            self.theme_combo.addItem(theme["label"], key)
        appearance_layout.addRow("Theme", self.theme_combo)
        language_controls = QWidget()
        language_controls_layout = QHBoxLayout(language_controls)
        language_controls_layout.setContentsMargins(0, 0, 0, 0)
        language_controls_layout.setSpacing(8)
        self.language_combo = QComboBox()
        self.language_combo.setProperty("_i18n_skip_combo_items", True)
        self._populate_language_combo()
        self.export_language_button = QPushButton("Export Language File...")
        self.import_language_button = QPushButton("Import Language File...")
        self.export_language_button.setToolTip(
            "Export a simple JSON file where translators only edit the values under translations. Longer words can make buttons and tabs look crowded."
        )
        self.import_language_button.setToolTip(
            "Import a translated JSON language file. Custom languages are stored beside the app settings and can be selected here."
        )
        language_controls_layout.addWidget(self.language_combo, stretch=1)
        language_controls_layout.addWidget(self.export_language_button)
        language_controls_layout.addWidget(self.import_language_button)
        appearance_layout.addRow("Language", language_controls)
        self.language_warning_label = QLabel(
            "Language files map English UI text to translated text. Longer translations can make buttons, tabs, and dialogs look crowded or clipped."
        )
        self.language_warning_label.setWordWrap(True)
        self.language_warning_label.setObjectName("HintLabel")
        appearance_layout.addRow("", self.language_warning_label)
        self.ui_font_family_combo = QComboBox()
        for family in UI_FONT_FAMILY_OPTIONS:
            self.ui_font_family_combo.addItem(family, family)
        appearance_layout.addRow("Global font family", self.ui_font_family_combo)
        self.density_combo = QComboBox()
        self.density_combo.addItem("Compact", "compact")
        self.density_combo.addItem("Normal", "normal")
        self.density_combo.addItem("Comfortable", "comfortable")
        appearance_layout.addRow("Density", self.density_combo)
        self.ui_font_size_spin = QSpinBox()
        self.ui_font_size_spin.setRange(UI_FONT_SIZE_MIN, UI_FONT_SIZE_MAX)
        self.ui_font_size_spin.setSuffix(" px")
        self.ui_font_size_spin.setKeyboardTracking(False)
        self.ui_font_size_spin.setAccelerated(True)
        self.ui_font_size_spin.setToolTip(
            f"Global UI font size. Minimum {UI_FONT_SIZE_MIN} px, maximum {UI_FONT_SIZE_MAX} px."
        )
        appearance_layout.addRow(
            f"Global font size ({UI_FONT_SIZE_MIN}-{UI_FONT_SIZE_MAX} px)",
            self.ui_font_size_spin,
        )
        self.data_font_size_spin = QSpinBox()
        self.data_font_size_spin.setRange(UI_FONT_SIZE_MIN, UI_FONT_SIZE_MAX)
        self.data_font_size_spin.setSuffix(" px")
        self.data_font_size_spin.setKeyboardTracking(False)
        self.data_font_size_spin.setAccelerated(True)
        self.data_font_size_spin.setToolTip(
            f"Used for dense lists, trees, tables, and column-heavy views. Minimum {UI_FONT_SIZE_MIN} px, maximum {UI_FONT_SIZE_MAX} px."
        )
        appearance_layout.addRow(
            f"Lists / columns font size ({UI_FONT_SIZE_MIN}-{UI_FONT_SIZE_MAX} px)",
            self.data_font_size_spin,
        )
        self.log_font_family_combo = QComboBox()
        for family in LOG_FONT_FAMILY_OPTIONS:
            self.log_font_family_combo.addItem(family, family)
        self.log_font_family_combo.setToolTip("Used for logs and code/text preview panes.")
        appearance_layout.addRow("Log / code font", self.log_font_family_combo)
        self.log_font_size_spin = QSpinBox()
        self.log_font_size_spin.setRange(8, 18)
        self.log_font_size_spin.setSuffix(" px")
        self.log_font_size_spin.setKeyboardTracking(False)
        self.log_font_size_spin.setAccelerated(True)
        appearance_layout.addRow("Log / code size", self.log_font_size_spin)
        self.log_font_bold_checkbox = QCheckBox("Bold emphasis in logs / code")
        self.log_font_bold_checkbox.setToolTip(
            "Controls whether highlighted log/code tokens use bold emphasis."
        )
        appearance_layout.addRow("", self.log_font_bold_checkbox)
        self.left_column.addWidget(appearance_group)

        startup_group = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup_group)
        startup_layout.setContentsMargins(12, 14, 12, 12)
        startup_layout.setSpacing(8)
        self.auto_load_archive_checkbox = QCheckBox("Auto-load Archive Browser on startup")
        self.prefer_cache_checkbox = QCheckBox("Prefer archive cache on startup")
        self.restore_last_tab_checkbox = QCheckBox("Restore last active tab")
        self.prefer_cache_checkbox.setToolTip(
            "When enabled, startup archive loading uses the saved cache when possible. Disable it to force a refresh."
        )
        startup_layout.addWidget(self.auto_load_archive_checkbox)
        startup_layout.addWidget(self.prefer_cache_checkbox)
        startup_layout.addWidget(self.restore_last_tab_checkbox)
        self.right_column.addWidget(startup_group)

        layout_group = QGroupBox("Layout")
        layout_layout = QVBoxLayout(layout_group)
        layout_layout.setContentsMargins(12, 14, 12, 12)
        layout_layout.setSpacing(8)
        self.remember_splitters_checkbox = QCheckBox("Remember pane sizes and splitters")
        layout_layout.addWidget(self.remember_splitters_checkbox)
        self.right_column.addWidget(layout_group)

        preview_group = QGroupBox("3D Preview / Graphics")
        preview_layout = QFormLayout(preview_group)
        preview_layout.setContentsMargins(12, 14, 12, 12)
        preview_layout.setHorizontalSpacing(12)
        preview_layout.setVerticalSpacing(10)
        self.model_preview_use_textures_checkbox = QCheckBox("Use textures by default when available")
        self.model_preview_high_quality_checkbox = QCheckBox("Enable high-quality shading by default")
        preview_layout.addRow("", self.model_preview_use_textures_checkbox)
        preview_layout.addRow("", self.model_preview_high_quality_checkbox)
        self.visible_texture_mode_combo = QComboBox()
        for mode in MODEL_PREVIEW_VISIBLE_TEXTURE_MODES:
            self.visible_texture_mode_combo.addItem(MODEL_PREVIEW_VISIBLE_TEXTURE_MODE_LABELS.get(mode, mode), mode)
        self.visible_texture_mode_combo.setToolTip(
            "Controls whether the preview prefers mesh-derived base textures or lets sidecar-visible layered textures replace them."
        )
        preview_layout.addRow("Visible texture mode", self.visible_texture_mode_combo)
        self.preview_texture_max_dimension_spin = self._create_int_spin(
            minimum=int(MODEL_PREVIEW_RENDER_LIMITS["preview_texture_max_dimension"][0]),
            maximum=int(MODEL_PREVIEW_RENDER_LIMITS["preview_texture_max_dimension"][1]),
            step=512,
            suffix=" px",
        )
        self.preview_texture_max_dimension_spin.setToolTip(
            "Maximum DDS preview image size used for 3D model texture preview generation. Higher values preserve more source detail but increase cache size, VRAM use, and upload time."
        )
        preview_layout.addRow("Preview texture max size", self.preview_texture_max_dimension_spin)
        self.low_quality_texture_max_dimension_spin = self._create_int_spin(
            minimum=int(MODEL_PREVIEW_RENDER_LIMITS["low_quality_texture_max_dimension"][0]),
            maximum=int(MODEL_PREVIEW_RENDER_LIMITS["low_quality_texture_max_dimension"][1]),
            step=128,
            suffix=" px",
        )
        self.low_quality_texture_max_dimension_spin.setToolTip(
            "Texture size cap used when High-quality is off."
        )
        preview_layout.addRow("Low-quality texture size", self.low_quality_texture_max_dimension_spin)
        self.max_anisotropy_spin = self._create_int_spin(
            minimum=int(MODEL_PREVIEW_RENDER_LIMITS["max_anisotropy"][0]),
            maximum=int(MODEL_PREVIEW_RENDER_LIMITS["max_anisotropy"][1]),
            step=1,
            suffix="x",
        )
        self.max_anisotropy_spin.setToolTip(
            "Higher anisotropy can improve texture sharpness at grazing angles, but increases GPU work."
        )
        preview_layout.addRow("Anisotropy", self.max_anisotropy_spin)
        self.orbit_sensitivity_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["orbit_sensitivity"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["orbit_sensitivity"][1],
            step=0.01,
            decimals=2,
        )
        preview_layout.addRow("Orbit sensitivity", self.orbit_sensitivity_spin)
        self.pan_sensitivity_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["pan_sensitivity"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["pan_sensitivity"][1],
            step=0.05,
            decimals=2,
        )
        preview_layout.addRow("Pan sensitivity", self.pan_sensitivity_spin)
        invert_controls = QWidget()
        invert_controls_layout = QVBoxLayout(invert_controls)
        invert_controls_layout.setContentsMargins(0, 0, 0, 0)
        invert_controls_layout.setSpacing(6)
        invert_row_one = QHBoxLayout()
        invert_row_one.setContentsMargins(0, 0, 0, 0)
        invert_row_one.setSpacing(10)
        self.invert_orbit_x_checkbox = QCheckBox("Invert orbit X")
        self.invert_orbit_y_checkbox = QCheckBox("Invert orbit Y")
        invert_row_one.addWidget(self.invert_orbit_x_checkbox)
        invert_row_one.addWidget(self.invert_orbit_y_checkbox)
        invert_row_one.addStretch(1)
        invert_controls_layout.addLayout(invert_row_one)
        invert_row_two = QHBoxLayout()
        invert_row_two.setContentsMargins(0, 0, 0, 0)
        invert_row_two.setSpacing(10)
        self.invert_pan_x_checkbox = QCheckBox("Invert pan X")
        self.invert_pan_y_checkbox = QCheckBox("Invert pan Y")
        invert_row_two.addWidget(self.invert_pan_x_checkbox)
        invert_row_two.addWidget(self.invert_pan_y_checkbox)
        invert_row_two.addStretch(1)
        invert_controls_layout.addLayout(invert_row_two)
        preview_layout.addRow("Control inversion", invert_controls)
        preview_hint = QLabel(
            "These values control default 3D preview quality and interaction. Larger texture sizes can improve detail but also increase cache rebuild time and GPU memory use."
        )
        preview_hint.setWordWrap(True)
        preview_hint.setObjectName("HintLabel")
        preview_layout.addRow("", preview_hint)
        preview_group.hide()
        self.left_column.addWidget(preview_group)

        shading_group = QGroupBox("Advanced Preview Shading")
        shading_layout = QFormLayout(shading_group)
        shading_layout.setContentsMargins(12, 14, 12, 12)
        shading_layout.setHorizontalSpacing(12)
        shading_layout.setVerticalSpacing(10)
        self.ambient_strength_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["ambient_strength"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["ambient_strength"][1],
            step=0.02,
            decimals=2,
        )
        shading_layout.addRow("Ambient strength", self.ambient_strength_spin)
        self.diffuse_wrap_bias_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["diffuse_wrap_bias"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["diffuse_wrap_bias"][1],
            step=0.02,
            decimals=2,
        )
        shading_layout.addRow("Wrap-light bias", self.diffuse_wrap_bias_spin)
        self.diffuse_light_scale_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["diffuse_light_scale"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["diffuse_light_scale"][1],
            step=0.02,
            decimals=2,
        )
        shading_layout.addRow("Diffuse light scale", self.diffuse_light_scale_spin)
        self.normal_strength_cap_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["normal_strength_cap"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["normal_strength_cap"][1],
            step=0.01,
            decimals=2,
        )
        shading_layout.addRow("Normal-map strength cap", self.normal_strength_cap_spin)
        self.normal_strength_floor_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["normal_strength_floor"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["normal_strength_floor"][1],
            step=0.01,
            decimals=2,
        )
        shading_layout.addRow("Normal-map minimum", self.normal_strength_floor_spin)
        self.height_effect_max_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["height_effect_max"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["height_effect_max"][1],
            step=0.01,
            decimals=2,
        )
        shading_layout.addRow("Height effect max", self.height_effect_max_spin)
        self.cavity_clamp_min_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["cavity_clamp_min"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["cavity_clamp_min"][1],
            step=0.01,
            decimals=2,
        )
        shading_layout.addRow("Cavity clamp min", self.cavity_clamp_min_spin)
        self.cavity_clamp_max_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["cavity_clamp_max"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["cavity_clamp_max"][1],
            step=0.01,
            decimals=2,
        )
        shading_layout.addRow("Cavity clamp max", self.cavity_clamp_max_spin)
        self.specular_base_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["specular_base"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["specular_base"][1],
            step=0.005,
            decimals=3,
        )
        shading_layout.addRow("Specular base", self.specular_base_spin)
        self.specular_min_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["specular_min"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["specular_min"][1],
            step=0.005,
            decimals=3,
        )
        shading_layout.addRow("Specular min", self.specular_min_spin)
        self.specular_max_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["specular_max"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["specular_max"][1],
            step=0.005,
            decimals=3,
        )
        shading_layout.addRow("Specular max", self.specular_max_spin)
        self.shininess_base_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["shininess_base"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["shininess_base"][1],
            step=1.0,
            decimals=1,
        )
        shading_layout.addRow("Shininess base", self.shininess_base_spin)
        self.shininess_min_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["shininess_min"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["shininess_min"][1],
            step=1.0,
            decimals=1,
        )
        shading_layout.addRow("Shininess min", self.shininess_min_spin)
        self.shininess_max_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["shininess_max"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["shininess_max"][1],
            step=1.0,
            decimals=1,
        )
        shading_layout.addRow("Shininess max", self.shininess_max_spin)
        self.height_shininess_boost_spin = self._create_float_spin(
            minimum=MODEL_PREVIEW_RENDER_LIMITS["height_shininess_boost"][0],
            maximum=MODEL_PREVIEW_RENDER_LIMITS["height_shininess_boost"][1],
            step=1.0,
            decimals=1,
        )
        shading_layout.addRow("Height shininess boost", self.height_shininess_boost_spin)
        reset_row = QHBoxLayout()
        reset_row.setContentsMargins(0, 0, 0, 0)
        reset_row.setSpacing(8)
        self.reset_model_preview_settings_button = QPushButton("Reset Preview Settings")
        self.reset_model_preview_settings_button.setToolTip(
            "Restore all 3D preview graphics and control settings to their default values."
        )
        reset_row.addWidget(self.reset_model_preview_settings_button)
        reset_row.addStretch(1)
        reset_row_widget = QWidget()
        reset_row_widget.setLayout(reset_row)
        shading_layout.addRow("", reset_row_widget)
        shading_hint = QLabel(
            "Changes apply to 3D archive previews. The ranges are intentionally capped to keep the preview usable and avoid values that are likely to look broken or exceed practical GPU limits."
        )
        shading_hint.setWordWrap(True)
        shading_hint.setObjectName("HintLabel")
        shading_layout.addRow("", shading_hint)
        shading_group.hide()
        self.left_column.addWidget(shading_group)

        safety_group = QGroupBox("Safety")
        safety_layout = QVBoxLayout(safety_group)
        safety_layout.setContentsMargins(12, 14, 12, 12)
        safety_layout.setSpacing(8)
        self.confirm_workflow_cleanup_checkbox = QCheckBox("Confirm clearing PNG / DDS output folders before Start")
        self.confirm_archive_cleanup_checkbox = QCheckBox("Confirm clearing archive extraction target")
        self.capture_crash_details_checkbox = QCheckBox(
            "Capture crash details to local report files on unhandled exceptions"
        )
        safety_layout.addWidget(self.confirm_workflow_cleanup_checkbox)
        safety_layout.addWidget(self.confirm_archive_cleanup_checkbox)
        safety_layout.addWidget(self.capture_crash_details_checkbox)
        self.right_column.addWidget(safety_group)

        notes = QLabel(
            "These preferences are stored in the local config beside the EXE and apply across sessions."
        )
        notes.setWordWrap(True)
        notes.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        notes.setObjectName("HintLabel")
        self.right_column.addWidget(notes)
        self.left_column.addStretch(1)
        self.right_column.addStretch(1)
        content_layout.addStretch(1)

        self.theme_combo.currentIndexChanged.connect(self._handle_appearance_changed)
        self.language_combo.currentIndexChanged.connect(self._handle_language_changed)
        self.export_language_button.clicked.connect(self.export_language_requested.emit)
        self.import_language_button.clicked.connect(self.import_language_requested.emit)
        self.ui_font_family_combo.currentIndexChanged.connect(self._handle_appearance_changed)
        self.density_combo.currentIndexChanged.connect(self._handle_appearance_changed)
        self.ui_font_size_spin.valueChanged.connect(self._handle_appearance_changed)
        self.data_font_size_spin.valueChanged.connect(self._handle_appearance_changed)
        self.log_font_family_combo.currentIndexChanged.connect(self._handle_appearance_changed)
        self.log_font_size_spin.valueChanged.connect(self._handle_appearance_changed)
        self.log_font_bold_checkbox.toggled.connect(self._handle_appearance_changed)
        for checkbox in (
            self.auto_load_archive_checkbox,
            self.prefer_cache_checkbox,
            self.restore_last_tab_checkbox,
            self.remember_splitters_checkbox,
            self.confirm_workflow_cleanup_checkbox,
            self.confirm_archive_cleanup_checkbox,
            self.capture_crash_details_checkbox,
        ):
            checkbox.toggled.connect(self.schedule_settings_save)
        for widget in self._model_preview_setting_widgets():
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self._handle_model_preview_changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._handle_model_preview_changed)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._handle_model_preview_changed)
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._handle_model_preview_changed)
        self.reset_model_preview_settings_button.clicked.connect(self._reset_model_preview_settings)

        self._load_settings(theme_key)
        self._settings_ready = True

    def add_setup_paths_sections(self, setup_section: QWidget, paths_section: QWidget) -> None:
        """Place app setup and path controls at the top of Settings without duplicating state."""
        self.left_column.insertWidget(0, setup_section)
        self.right_column.insertWidget(0, paths_section)

    def _create_int_spin(self, *, minimum: int, maximum: int, step: int, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(int(minimum), int(maximum))
        spin.setSingleStep(max(1, int(step)))
        spin.setKeyboardTracking(False)
        spin.setAccelerated(True)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    def _populate_language_combo(self, language_options: Optional[tuple[tuple[str, str], ...]] = None) -> None:
        current_code = self.current_language_code() if hasattr(self, "language_combo") else "en"
        options = language_options or tuple(
            (code, str(payload.get("language_name", code)))
            for code, payload in BUILTIN_LANGUAGES.items()
            if code in {"en", "es", "de"}
        )
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for code, name in options:
            self.language_combo.addItem(str(name), str(code))
        index = self.language_combo.findData(current_code)
        if index < 0:
            index = self.language_combo.findData("en")
        self.language_combo.setCurrentIndex(max(0, index))
        self.language_combo.blockSignals(False)

    def set_language_options(
        self,
        language_options: tuple[tuple[str, str], ...],
        *,
        current_code: str = "",
    ) -> None:
        self._populate_language_combo(language_options)
        if current_code:
            self.set_language_selection(current_code)

    def _create_float_spin(
        self,
        *,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(float(minimum), float(maximum))
        spin.setSingleStep(float(step))
        spin.setDecimals(int(decimals))
        spin.setKeyboardTracking(False)
        spin.setAccelerated(True)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    def _model_preview_setting_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.model_preview_use_textures_checkbox,
            self.model_preview_high_quality_checkbox,
            self.visible_texture_mode_combo,
            self.preview_texture_max_dimension_spin,
            self.low_quality_texture_max_dimension_spin,
            self.max_anisotropy_spin,
            self.orbit_sensitivity_spin,
            self.pan_sensitivity_spin,
            self.invert_orbit_x_checkbox,
            self.invert_orbit_y_checkbox,
            self.invert_pan_x_checkbox,
            self.invert_pan_y_checkbox,
            self.ambient_strength_spin,
            self.diffuse_wrap_bias_spin,
            self.diffuse_light_scale_spin,
            self.normal_strength_cap_spin,
            self.normal_strength_floor_spin,
            self.height_effect_max_spin,
            self.cavity_clamp_min_spin,
            self.cavity_clamp_max_spin,
            self.specular_base_spin,
            self.specular_min_spin,
            self.specular_max_spin,
            self.shininess_base_spin,
            self.shininess_min_spin,
            self.shininess_max_spin,
            self.height_shininess_boost_spin,
        )

    def _read_bool(self, key: str, default: bool) -> bool:
        value = self.settings.value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _read_int(self, key: str, default: int) -> int:
        value = self.settings.value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _read_float(self, key: str, default: float) -> float:
        value = self.settings.value(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _load_settings(self, theme_key: str) -> None:
        self.sync_appearance_controls(theme_key)
        self.set_language_selection(str(self.settings.value("appearance/language", "en") or "en"))
        self.auto_load_archive_checkbox.setChecked(
            self._read_bool("preferences/auto_load_archive_on_startup", False)
        )
        self.prefer_cache_checkbox.setChecked(
            self._read_bool("preferences/prefer_archive_cache_on_startup", True)
        )
        self.restore_last_tab_checkbox.setChecked(
            self._read_bool("preferences/restore_last_active_tab", True)
        )
        self.remember_splitters_checkbox.setChecked(
            self._read_bool("preferences/remember_splitter_sizes", True)
        )
        self.confirm_workflow_cleanup_checkbox.setChecked(
            self._read_bool("preferences/confirm_workflow_output_cleanup", True)
        )
        self.confirm_archive_cleanup_checkbox.setChecked(
            self._read_bool("preferences/confirm_archive_extract_cleanup", True)
        )
        self.capture_crash_details_checkbox.setChecked(
            self._read_bool("preferences/capture_crash_details", False)
        )
        self.sync_model_preview_controls()
        self._apply_checkbox_states()

    def _save_settings(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("appearance/theme", self.current_theme_key())
        self.settings.setValue("appearance/language", self.current_language_code())
        self.settings.setValue("appearance/ui_font_family", self.current_ui_font_family())
        self.settings.setValue("appearance/ui_density", self.current_density_key())
        self.settings.setValue("appearance/ui_font_size", self.current_ui_font_size())
        self.settings.setValue("appearance/data_font_size", self.current_data_font_size())
        self.settings.setValue("appearance/log_font_family", self.current_log_font_family())
        self.settings.setValue("appearance/log_font_size", self.current_log_font_size())
        self.settings.setValue("appearance/log_font_bold", self.current_log_font_bold())
        self.settings.setValue("preferences/auto_load_archive_on_startup", self.auto_load_archive_checkbox.isChecked())
        self.settings.setValue(
            "preferences/prefer_archive_cache_on_startup",
            self.prefer_cache_checkbox.isChecked(),
        )
        self.settings.setValue(
            "preferences/restore_last_active_tab",
            self.restore_last_tab_checkbox.isChecked(),
        )
        self.settings.setValue(
            "preferences/remember_splitter_sizes",
            self.remember_splitters_checkbox.isChecked(),
        )
        self.settings.setValue(
            "preferences/confirm_workflow_output_cleanup",
            self.confirm_workflow_cleanup_checkbox.isChecked(),
        )
        self.settings.setValue(
            "preferences/confirm_archive_extract_cleanup",
            self.confirm_archive_cleanup_checkbox.isChecked(),
        )
        previous_capture_value = self._read_bool("preferences/capture_crash_details", False)
        current_capture_value = self.capture_crash_details_checkbox.isChecked()
        self.settings.setValue("preferences/capture_crash_details", current_capture_value)
        self.settings.sync()
        self._apply_checkbox_states()
        if previous_capture_value != current_capture_value:
            self.crash_capture_changed.emit(current_capture_value)

    def schedule_settings_save(self, *_args) -> None:
        if not self._settings_ready:
            return
        self._settings_save_timer.start()

    def flush_settings_save(self) -> None:
        if self._appearance_apply_timer.isActive():
            self._appearance_apply_timer.stop()
            self._apply_pending_appearance_change()
            return
        if self._settings_save_timer.isActive():
            self._settings_save_timer.stop()
        self._save_settings()

    def _apply_checkbox_states(self) -> None:
        self.prefer_cache_checkbox.setEnabled(self.auto_load_archive_checkbox.isChecked())

    def _handle_appearance_changed(self) -> None:
        if not self._settings_ready:
            return
        self._appearance_apply_timer.start()

    def _handle_language_changed(self) -> None:
        if not self._settings_ready:
            return
        self.settings.setValue("appearance/language", self.current_language_code())
        self.settings.sync()
        self.language_changed.emit(self.current_language_code())

    def _apply_pending_appearance_change(self) -> None:
        if not self._settings_ready:
            return
        self._save_settings()
        self.theme_changed.emit(self.current_theme_key())

    def _handle_model_preview_changed(self, *_args) -> None:
        if not self._settings_ready:
            return
        self.schedule_settings_save()
        self.model_preview_settings_changed.emit(self.current_model_preview_render_settings())

    def _reset_model_preview_settings(self) -> None:
        defaults = clamp_model_preview_render_settings()
        self._apply_model_preview_controls(defaults)
        if not self._settings_ready:
            return
        self.schedule_settings_save()
        self.model_preview_settings_changed.emit(self.current_model_preview_render_settings())

    def set_theme_selection(self, theme_key: str) -> None:
        resolved_theme_key = theme_key if theme_key in UI_THEME_SCHEMES else DEFAULT_UI_THEME
        index = self.theme_combo.findData(resolved_theme_key)
        if index < 0:
            index = self.theme_combo.findData(DEFAULT_UI_THEME)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(max(0, index))
        self.theme_combo.blockSignals(False)

    def sync_appearance_controls(self, theme_key: str) -> None:
        resolved_theme_key = theme_key if theme_key in UI_THEME_SCHEMES else DEFAULT_UI_THEME
        ui_font_family = str(self.settings.value("appearance/ui_font_family", DEFAULT_UI_FONT_FAMILY) or DEFAULT_UI_FONT_FAMILY)
        density_key = str(self.settings.value("appearance/ui_density", DEFAULT_UI_DENSITY) or DEFAULT_UI_DENSITY)
        ui_font_size = self._read_int("appearance/ui_font_size", DEFAULT_UI_FONT_SIZE)
        data_font_size = self._read_int("appearance/data_font_size", DEFAULT_UI_DATA_FONT_SIZE)
        log_font_family = str(
            self.settings.value("appearance/log_font_family", DEFAULT_UI_LOG_FONT_FAMILY)
            or DEFAULT_UI_LOG_FONT_FAMILY
        )
        log_font_size = self._read_int("appearance/log_font_size", DEFAULT_UI_LOG_FONT_SIZE)
        log_font_bold = self._read_bool("appearance/log_font_bold", DEFAULT_UI_LOG_FONT_BOLD)
        self.set_theme_selection(resolved_theme_key)
        family_index = self.ui_font_family_combo.findData(ui_font_family)
        if family_index < 0:
            family_index = self.ui_font_family_combo.findData(DEFAULT_UI_FONT_FAMILY)
        self.ui_font_family_combo.blockSignals(True)
        self.ui_font_family_combo.setCurrentIndex(max(0, family_index))
        self.ui_font_family_combo.blockSignals(False)
        density_index = self.density_combo.findData(density_key)
        if density_index < 0:
            density_index = self.density_combo.findData(DEFAULT_UI_DENSITY)
        self.density_combo.blockSignals(True)
        self.density_combo.setCurrentIndex(max(0, density_index))
        self.density_combo.blockSignals(False)
        self.ui_font_size_spin.blockSignals(True)
        self.ui_font_size_spin.setValue(max(UI_FONT_SIZE_MIN, min(UI_FONT_SIZE_MAX, ui_font_size)))
        self.ui_font_size_spin.blockSignals(False)
        self.data_font_size_spin.blockSignals(True)
        self.data_font_size_spin.setValue(max(UI_FONT_SIZE_MIN, min(UI_FONT_SIZE_MAX, data_font_size)))
        self.data_font_size_spin.blockSignals(False)
        log_family_index = self.log_font_family_combo.findData(log_font_family)
        if log_family_index < 0:
            log_family_index = self.log_font_family_combo.findData(DEFAULT_UI_LOG_FONT_FAMILY)
        self.log_font_family_combo.blockSignals(True)
        self.log_font_family_combo.setCurrentIndex(max(0, log_family_index))
        self.log_font_family_combo.blockSignals(False)
        self.log_font_size_spin.blockSignals(True)
        self.log_font_size_spin.setValue(max(8, min(18, log_font_size)))
        self.log_font_size_spin.blockSignals(False)
        self.log_font_bold_checkbox.blockSignals(True)
        self.log_font_bold_checkbox.setChecked(bool(log_font_bold))
        self.log_font_bold_checkbox.blockSignals(False)

    def set_language_selection(self, language_code: str) -> None:
        code = str(language_code or "en").strip() or "en"
        index = self.language_combo.findData(code)
        if index < 0:
            index = self.language_combo.findData("en")
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(max(0, index))
        self.language_combo.blockSignals(False)

    def _read_model_preview_render_settings(self) -> ModelPreviewRenderSettings:
        defaults = clamp_model_preview_render_settings()
        return clamp_model_preview_render_settings(
            ModelPreviewRenderSettings(
                use_textures_by_default=self._read_bool("archive/model_use_textures", defaults.use_textures_by_default),
                high_quality_by_default=self._read_bool("archive/model_high_quality", defaults.high_quality_by_default),
                visible_texture_mode=str(
                    self.settings.value("preview/visible_texture_mode", defaults.visible_texture_mode)
                    or defaults.visible_texture_mode
                ),
                preview_texture_max_dimension=self._read_int(
                    "preview/texture_max_dimension",
                    defaults.preview_texture_max_dimension,
                ),
                low_quality_texture_max_dimension=self._read_int(
                    "preview/low_quality_texture_max_dimension",
                    defaults.low_quality_texture_max_dimension,
                ),
                max_anisotropy=self._read_int("preview/max_anisotropy", defaults.max_anisotropy),
                ambient_strength=self._read_float("preview/ambient_strength", defaults.ambient_strength),
                diffuse_wrap_bias=self._read_float("preview/diffuse_wrap_bias", defaults.diffuse_wrap_bias),
                diffuse_light_scale=self._read_float("preview/diffuse_light_scale", defaults.diffuse_light_scale),
                orbit_sensitivity=self._read_float("preview/orbit_sensitivity", defaults.orbit_sensitivity),
                pan_sensitivity=self._read_float("preview/pan_sensitivity", defaults.pan_sensitivity),
                invert_orbit_x=self._read_bool("preview/invert_orbit_x", defaults.invert_orbit_x),
                invert_orbit_y=self._read_bool("preview/invert_orbit_y", defaults.invert_orbit_y),
                invert_pan_x=self._read_bool("preview/invert_pan_x", defaults.invert_pan_x),
                invert_pan_y=self._read_bool("preview/invert_pan_y", defaults.invert_pan_y),
                normal_strength_cap=self._read_float("preview/normal_strength_cap", defaults.normal_strength_cap),
                normal_strength_floor=self._read_float("preview/normal_strength_floor", defaults.normal_strength_floor),
                height_effect_max=self._read_float("preview/height_effect_max", defaults.height_effect_max),
                cavity_clamp_min=self._read_float("preview/cavity_clamp_min", defaults.cavity_clamp_min),
                cavity_clamp_max=self._read_float("preview/cavity_clamp_max", defaults.cavity_clamp_max),
                specular_base=self._read_float("preview/specular_base", defaults.specular_base),
                specular_min=self._read_float("preview/specular_min", defaults.specular_min),
                specular_max=self._read_float("preview/specular_max", defaults.specular_max),
                shininess_base=self._read_float("preview/shininess_base", defaults.shininess_base),
                shininess_min=self._read_float("preview/shininess_min", defaults.shininess_min),
                shininess_max=self._read_float("preview/shininess_max", defaults.shininess_max),
                height_shininess_boost=self._read_float(
                    "preview/height_shininess_boost",
                    defaults.height_shininess_boost,
                ),
            )
        )

    def _apply_model_preview_controls(self, settings: ModelPreviewRenderSettings) -> None:
        clamped = clamp_model_preview_render_settings(settings)
        for widget in self._model_preview_setting_widgets():
            widget.blockSignals(True)
        try:
            self.model_preview_use_textures_checkbox.setChecked(clamped.use_textures_by_default)
            self.model_preview_high_quality_checkbox.setChecked(clamped.high_quality_by_default)
            visible_texture_mode_index = self.visible_texture_mode_combo.findData(clamped.visible_texture_mode)
            self.visible_texture_mode_combo.setCurrentIndex(max(0, visible_texture_mode_index))
            self.preview_texture_max_dimension_spin.setValue(clamped.preview_texture_max_dimension)
            self.low_quality_texture_max_dimension_spin.setValue(clamped.low_quality_texture_max_dimension)
            self.max_anisotropy_spin.setValue(clamped.max_anisotropy)
            self.orbit_sensitivity_spin.setValue(clamped.orbit_sensitivity)
            self.pan_sensitivity_spin.setValue(clamped.pan_sensitivity)
            self.invert_orbit_x_checkbox.setChecked(clamped.invert_orbit_x)
            self.invert_orbit_y_checkbox.setChecked(clamped.invert_orbit_y)
            self.invert_pan_x_checkbox.setChecked(clamped.invert_pan_x)
            self.invert_pan_y_checkbox.setChecked(clamped.invert_pan_y)
            self.ambient_strength_spin.setValue(clamped.ambient_strength)
            self.diffuse_wrap_bias_spin.setValue(clamped.diffuse_wrap_bias)
            self.diffuse_light_scale_spin.setValue(clamped.diffuse_light_scale)
            self.normal_strength_cap_spin.setValue(clamped.normal_strength_cap)
            self.normal_strength_floor_spin.setValue(clamped.normal_strength_floor)
            self.height_effect_max_spin.setValue(clamped.height_effect_max)
            self.cavity_clamp_min_spin.setValue(clamped.cavity_clamp_min)
            self.cavity_clamp_max_spin.setValue(clamped.cavity_clamp_max)
            self.specular_base_spin.setValue(clamped.specular_base)
            self.specular_min_spin.setValue(clamped.specular_min)
            self.specular_max_spin.setValue(clamped.specular_max)
            self.shininess_base_spin.setValue(clamped.shininess_base)
            self.shininess_min_spin.setValue(clamped.shininess_min)
            self.shininess_max_spin.setValue(clamped.shininess_max)
            self.height_shininess_boost_spin.setValue(clamped.height_shininess_boost)
        finally:
            for widget in self._model_preview_setting_widgets():
                widget.blockSignals(False)

    def sync_model_preview_controls(self) -> None:
        self._apply_model_preview_controls(self._read_model_preview_render_settings())

    def set_model_preview_toggle_defaults(
        self,
        *,
        use_textures: bool,
        high_quality: bool,
        persist: bool = False,
    ) -> None:
        current = self.current_model_preview_render_settings()
        current.use_textures_by_default = bool(use_textures)
        current.high_quality_by_default = bool(high_quality)
        self._apply_model_preview_controls(current)
        if persist and self._settings_ready:
            self.schedule_settings_save()

    def current_model_preview_render_settings(self) -> ModelPreviewRenderSettings:
        return clamp_model_preview_render_settings(
            ModelPreviewRenderSettings(
                use_textures_by_default=self.model_preview_use_textures_checkbox.isChecked(),
                high_quality_by_default=self.model_preview_high_quality_checkbox.isChecked(),
                visible_texture_mode=str(
                    self.visible_texture_mode_combo.currentData() or ModelPreviewRenderSettings().visible_texture_mode
                ),
                preview_texture_max_dimension=self.preview_texture_max_dimension_spin.value(),
                low_quality_texture_max_dimension=self.low_quality_texture_max_dimension_spin.value(),
                max_anisotropy=self.max_anisotropy_spin.value(),
                ambient_strength=self.ambient_strength_spin.value(),
                diffuse_wrap_bias=self.diffuse_wrap_bias_spin.value(),
                diffuse_light_scale=self.diffuse_light_scale_spin.value(),
                orbit_sensitivity=self.orbit_sensitivity_spin.value(),
                pan_sensitivity=self.pan_sensitivity_spin.value(),
                invert_orbit_x=self.invert_orbit_x_checkbox.isChecked(),
                invert_orbit_y=self.invert_orbit_y_checkbox.isChecked(),
                invert_pan_x=self.invert_pan_x_checkbox.isChecked(),
                invert_pan_y=self.invert_pan_y_checkbox.isChecked(),
                normal_strength_cap=self.normal_strength_cap_spin.value(),
                normal_strength_floor=self.normal_strength_floor_spin.value(),
                height_effect_max=self.height_effect_max_spin.value(),
                cavity_clamp_min=self.cavity_clamp_min_spin.value(),
                cavity_clamp_max=self.cavity_clamp_max_spin.value(),
                specular_base=self.specular_base_spin.value(),
                specular_min=self.specular_min_spin.value(),
                specular_max=self.specular_max_spin.value(),
                shininess_base=self.shininess_base_spin.value(),
                shininess_min=self.shininess_min_spin.value(),
                shininess_max=self.shininess_max_spin.value(),
                height_shininess_boost=self.height_shininess_boost_spin.value(),
            )
        )

    def current_theme_key(self) -> str:
        data = self.theme_combo.currentData()
        return str(data) if data is not None else DEFAULT_UI_THEME

    def current_language_code(self) -> str:
        data = self.language_combo.currentData()
        return str(data) if data is not None else "en"

    def current_density_key(self) -> str:
        data = self.density_combo.currentData()
        return str(data) if data is not None else DEFAULT_UI_DENSITY

    def current_ui_font_family(self) -> str:
        data = self.ui_font_family_combo.currentData()
        return str(data) if data is not None else DEFAULT_UI_FONT_FAMILY

    def current_ui_font_size(self) -> int:
        return int(self.ui_font_size_spin.value())

    def current_data_font_size(self) -> int:
        return int(self.data_font_size_spin.value())

    def current_log_font_family(self) -> str:
        data = self.log_font_family_combo.currentData()
        return str(data) if data is not None else DEFAULT_UI_LOG_FONT_FAMILY

    def current_log_font_size(self) -> int:
        return int(self.log_font_size_spin.value())

    def current_log_font_bold(self) -> bool:
        return bool(self.log_font_bold_checkbox.isChecked())
