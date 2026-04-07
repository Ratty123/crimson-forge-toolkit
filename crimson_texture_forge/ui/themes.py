from __future__ import annotations

from typing import Dict

from PySide6.QtGui import QColor, QPalette

from crimson_texture_forge.constants import DEFAULT_UI_THEME

UI_THEME_SCHEMES: Dict[str, Dict[str, str]] = {
    "graphite": {
        "label": "Dark",
        "window": "#1e1e1e",
        "surface": "#252526",
        "surface_alt": "#2d2d30",
        "field": "#1f1f1f",
        "field_alt": "#252526",
        "border": "#2a2d2e",
        "border_strong": "#3c3c3c",
        "text": "#cccccc",
        "text_muted": "#9da0a6",
        "text_strong": "#f3f3f3",
        "button": "#2d2d30",
        "button_hover": "#37373d",
        "button_pressed": "#252526",
        "button_border": "#45494a",
        "button_disabled": "#252526",
        "button_disabled_text": "#6f7680",
        "accent": "#007acc",
        "accent_soft": "#094771",
        "warning_text": "#e4be78",
        "warning_bg": "#4b3b1f",
        "warning_border": "#8c7340",
        "error": "#f48771",
        "preview_bg": "#1b1b1c",
    },
    "light": {
        "label": "Light",
        "window": "#f4f6f8",
        "surface": "#ffffff",
        "surface_alt": "#eef2f6",
        "field": "#ffffff",
        "field_alt": "#f7f9fb",
        "border": "#d5dde6",
        "border_strong": "#c6d0dc",
        "text": "#1f2933",
        "text_muted": "#5f6c7b",
        "text_strong": "#111827",
        "button": "#eef2f6",
        "button_hover": "#e2e8f0",
        "button_pressed": "#d7dfe8",
        "button_border": "#c6d0dc",
        "button_disabled": "#f2f4f7",
        "button_disabled_text": "#8b97a4",
        "accent": "#2563eb",
        "accent_soft": "#dbeafe",
        "warning_text": "#8a5a00",
        "warning_bg": "#fff4d8",
        "warning_border": "#e6c47a",
        "error": "#c0362c",
        "preview_bg": "#f7f9fb",
    },
    "nord": {
        "label": "Nord",
        "window": "#2e3440",
        "surface": "#3b4252",
        "surface_alt": "#434c5e",
        "field": "#2b303b",
        "field_alt": "#313744",
        "border": "#4c566a",
        "border_strong": "#596377",
        "text": "#e5e9f0",
        "text_muted": "#c0c8d6",
        "text_strong": "#eceff4",
        "button": "#434c5e",
        "button_hover": "#4c566a",
        "button_pressed": "#3b4252",
        "button_border": "#596377",
        "button_disabled": "#353b47",
        "button_disabled_text": "#8e98aa",
        "accent": "#88c0d0",
        "accent_soft": "#4c5f73",
        "warning_text": "#ebcb8b",
        "warning_bg": "#4c432c",
        "warning_border": "#8d7850",
        "error": "#bf616a",
        "preview_bg": "#2b303b",
    },
    "one_dark": {
        "label": "One Dark",
        "window": "#282c34",
        "surface": "#2f343f",
        "surface_alt": "#353b45",
        "field": "#21252b",
        "field_alt": "#262b33",
        "border": "#3d4451",
        "border_strong": "#474f5d",
        "text": "#d7dae0",
        "text_muted": "#abb2bf",
        "text_strong": "#eceff4",
        "button": "#313844",
        "button_hover": "#3b4452",
        "button_pressed": "#2a3039",
        "button_border": "#475062",
        "button_disabled": "#252932",
        "button_disabled_text": "#7f8896",
        "accent": "#61afef",
        "accent_soft": "#33455c",
        "warning_text": "#e5c07b",
        "warning_bg": "#4b3d24",
        "warning_border": "#8d7442",
        "error": "#e06c75",
        "preview_bg": "#21252b",
    },
    "tokyo_night": {
        "label": "Tokyo Night",
        "window": "#1a1b26",
        "surface": "#1f2335",
        "surface_alt": "#24283b",
        "field": "#16161e",
        "field_alt": "#1b1d2a",
        "border": "#2f334d",
        "border_strong": "#3a3f5f",
        "text": "#c0caf5",
        "text_muted": "#9aa5ce",
        "text_strong": "#e6edf7",
        "button": "#252b40",
        "button_hover": "#2d3550",
        "button_pressed": "#1f2435",
        "button_border": "#3a4364",
        "button_disabled": "#1c2130",
        "button_disabled_text": "#7580a6",
        "accent": "#7aa2f7",
        "accent_soft": "#2c3553",
        "warning_text": "#e0af68",
        "warning_bg": "#4c3d27",
        "warning_border": "#896a3b",
        "error": "#f7768e",
        "preview_bg": "#16161e",
    },
    "solarized_dark": {
        "label": "Solarized Dark",
        "window": "#002b36",
        "surface": "#073642",
        "surface_alt": "#0a3c4a",
        "field": "#00212b",
        "field_alt": "#062e38",
        "border": "#1f4a57",
        "border_strong": "#285766",
        "text": "#93a1a1",
        "text_muted": "#839496",
        "text_strong": "#eee8d5",
        "button": "#0b3b46",
        "button_hover": "#124652",
        "button_pressed": "#08323c",
        "button_border": "#2d5a67",
        "button_disabled": "#082c35",
        "button_disabled_text": "#5f7c82",
        "accent": "#268bd2",
        "accent_soft": "#173e4d",
        "warning_text": "#b58900",
        "warning_bg": "#3d3300",
        "warning_border": "#7c6a1d",
        "error": "#dc322f",
        "preview_bg": "#00212b",
    },
    "catppuccin_mocha": {
        "label": "Catppuccin Mocha",
        "window": "#1e1e2e",
        "surface": "#24273a",
        "surface_alt": "#2b3046",
        "field": "#181825",
        "field_alt": "#1f2030",
        "border": "#45475a",
        "border_strong": "#585b70",
        "text": "#cdd6f4",
        "text_muted": "#a6adc8",
        "text_strong": "#f5e0dc",
        "button": "#313244",
        "button_hover": "#3c3f57",
        "button_pressed": "#2a2b3c",
        "button_border": "#585b70",
        "button_disabled": "#232434",
        "button_disabled_text": "#7d8296",
        "accent": "#89b4fa",
        "accent_soft": "#35405a",
        "warning_text": "#f9e2af",
        "warning_bg": "#4a4130",
        "warning_border": "#8a7d5a",
        "error": "#f38ba8",
        "preview_bg": "#181825",
    },
}


def get_theme(key: str) -> Dict[str, str]:
    return UI_THEME_SCHEMES.get(key, UI_THEME_SCHEMES[DEFAULT_UI_THEME])


def build_app_palette(theme_key: str) -> QPalette:
    theme = get_theme(theme_key)
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(theme["window"]))
    palette.setColor(QPalette.WindowText, QColor(theme["text"]))
    palette.setColor(QPalette.Base, QColor(theme["field"]))
    palette.setColor(QPalette.AlternateBase, QColor(theme["field_alt"]))
    palette.setColor(QPalette.ToolTipBase, QColor(theme["surface"]))
    palette.setColor(QPalette.ToolTipText, QColor(theme["text_strong"]))
    palette.setColor(QPalette.Text, QColor(theme["text"]))
    palette.setColor(QPalette.Button, QColor(theme["button"]))
    palette.setColor(QPalette.ButtonText, QColor(theme["text_strong"]))
    palette.setColor(QPalette.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.Highlight, QColor(theme["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.PlaceholderText, QColor(theme["text_muted"]))
    palette.setColor(QPalette.Link, QColor(theme["accent"]))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(theme["button_disabled_text"]))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(theme["button_disabled_text"]))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(theme["button_disabled_text"]))
    return palette


def build_app_stylesheet(theme_key: str) -> str:
    theme = get_theme(theme_key)
    return f"""
    QWidget {{
        font-size: 12px;
        color: {theme["text"]};
    }}
    QMainWindow, QWidget#AppRoot {{
        background: {theme["window"]};
    }}
    QMenuBar {{
        background: {theme["surface"]};
        color: {theme["text"]};
        border-bottom: 1px solid {theme["border"]};
        padding: 0 4px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QMenuBar::item:selected {{
        background: {theme["button_hover"]};
    }}
    QMenu {{
        background: {theme["surface"]};
        color: {theme["text"]};
        border: 1px solid {theme["border_strong"]};
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 18px 6px 12px;
        border-radius: 4px;
    }}
    QMenu::item:selected {{
        background: {theme["accent_soft"]};
        color: {theme["text_strong"]};
    }}
    QLabel, QCheckBox, QToolButton {{
        background: transparent;
    }}
    QGroupBox {{
        border: 1px solid {theme["border"]};
        border-radius: 5px;
        margin-top: 13px;
        padding-top: 10px;
        font-weight: 600;
        background: {theme["surface"]};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {theme["text_strong"]};
        background: {theme["surface"]};
    }}
    QToolButton#SectionToggle {{
        text-align: left;
        background: {theme["surface_alt"]};
        color: {theme["text_strong"]};
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        padding: 8px 10px;
        font-weight: 600;
    }}
    QToolButton#SectionToggle:hover {{
        background: {theme["button_hover"]};
    }}
    QToolButton#SectionToggle:checked {{
        background: {theme["button"]};
    }}
    QFrame#SectionBody {{
        border: 1px solid {theme["border"]};
        border-radius: 5px;
        background: {theme["surface"]};
    }}
    QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox {{
        background: {theme["field"]};
        border: 1px solid {theme["border_strong"]};
        border-radius: 4px;
        padding: 6px 9px;
        selection-background-color: {theme["accent"]};
        selection-color: #ffffff;
    }}
    QComboBox {{
        padding-right: 24px;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background: {theme["field"]};
        color: {theme["text"]};
        border: 1px solid {theme["border_strong"]};
        selection-background-color: {theme["accent_soft"]};
        selection-color: {theme["text_strong"]};
    }}
    QListWidget, QTreeWidget {{
        background: {theme["field"]};
        border: 1px solid {theme["border_strong"]};
        border-radius: 4px;
        padding: 2px;
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QAbstractScrollArea {{
        background: transparent;
    }}
    QListWidget::item {{
        padding: 4px 6px;
        border-radius: 3px;
    }}
    QListWidget::item:selected, QTreeWidget::item:selected {{
        background: {theme["accent_soft"]};
        color: {theme["text_strong"]};
    }}
    QTreeWidget::item {{
        padding: 3px 6px;
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus, QComboBox:focus, QSpinBox:focus,
    QListWidget:focus, QTreeWidget:focus {{
        border: 1px solid {theme["accent"]};
    }}
    QHeaderView::section {{
        background: {theme["surface_alt"]};
        color: {theme["text_muted"]};
        border: none;
        border-right: 1px solid {theme["border"]};
        padding: 6px 8px;
    }}
    QPushButton {{
        background: {theme["button"]};
        border: 1px solid {theme["button_border"]};
        border-radius: 4px;
        padding: 6px 12px;
        min-height: 18px;
    }}
    QPushButton:hover {{
        background: {theme["button_hover"]};
    }}
    QPushButton:pressed {{
        background: {theme["button_pressed"]};
    }}
    QPushButton:disabled {{
        color: {theme["button_disabled_text"]};
        background: {theme["button_disabled"]};
        border-color: {theme["border"]};
    }}
    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 4px;
        border: 1px solid {theme["button_border"]};
        background: {theme["field"]};
    }}
    QCheckBox::indicator:checked {{
        background: {theme["accent"]};
        border: 1px solid {theme["accent"]};
    }}
    QProgressBar {{
        border: 1px solid {theme["border_strong"]};
        border-radius: 4px;
        background: {theme["field"]};
        text-align: center;
        min-height: 18px;
    }}
    QProgressBar::chunk {{
        border-radius: 3px;
        background: {theme["accent"]};
    }}
    QLabel#HintLabel {{
        color: {theme["text_muted"]};
        background: transparent;
    }}
    QLabel#WarningBadge {{
        color: {theme["warning_text"]};
        background: {theme["warning_bg"]};
        border: 1px solid {theme["warning_border"]};
        border-radius: 4px;
        padding: 4px 8px;
        font-weight: 600;
    }}
    QLabel#WarningText {{
        color: {theme["warning_text"]};
        background: transparent;
    }}
    QLabel#StatusLabel {{
        color: {theme["text_muted"]};
        background: transparent;
    }}
    QLabel#StatusLabel[error="true"] {{
        color: {theme["error"]};
    }}
    QLabel#PreviewLabel {{
        border: 1px solid {theme["border_strong"]};
        border-radius: 5px;
        background: {theme["preview_bg"]};
        color: {theme["text_muted"]};
        padding: 12px;
    }}
    QTabWidget::pane {{
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background: {theme["surface"]};
        top: 0px;
    }}
    QTabBar::tab {{
        background: {theme["surface_alt"]};
        color: {theme["text_muted"]};
        padding: 7px 14px;
        border: 1px solid {theme["border"]};
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 1px;
    }}
    QTabBar::tab:selected {{
        background: {theme["surface"]};
        color: {theme["text_strong"]};
        border-color: {theme["border_strong"]};
    }}
    QTabBar::tab:hover:!selected {{
        background: {theme["button_hover"]};
    }}
    QSplitter::handle {{
        background: {theme["surface_alt"]};
        width: 4px;
    }}
    QScrollBar:vertical {{
        background: {theme["surface"]};
        width: 10px;
        margin: 2px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {theme["button_border"]};
        min-height: 24px;
        border-radius: 4px;
    }}
    QScrollBar:horizontal {{
        background: {theme["surface"]};
        height: 10px;
        margin: 2px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {theme["button_border"]};
        min-width: 24px;
        border-radius: 4px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        background: transparent;
        border: none;
    }}
    QToolTip {{
        background: {theme["surface_alt"]};
        color: {theme["text_strong"]};
        border: 1px solid {theme["border"]};
        padding: 6px 8px;
    }}
    """
