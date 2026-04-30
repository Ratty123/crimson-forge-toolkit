from __future__ import annotations

from typing import Dict

from PySide6.QtGui import QColor, QPalette

from cdmw.constants import (
    DEFAULT_UI_DATA_FONT_SIZE,
    DEFAULT_UI_DENSITY,
    DEFAULT_UI_FONT_SIZE,
    DEFAULT_UI_THEME,
)

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
    "github_dark": {
        "label": "GitHub Dark",
        "window": "#0d1117",
        "surface": "#161b22",
        "surface_alt": "#21262d",
        "field": "#0d1117",
        "field_alt": "#161b22",
        "border": "#30363d",
        "border_strong": "#484f58",
        "text": "#c9d1d9",
        "text_muted": "#8b949e",
        "text_strong": "#f0f6fc",
        "button": "#21262d",
        "button_hover": "#30363d",
        "button_pressed": "#161b22",
        "button_border": "#484f58",
        "button_disabled": "#161b22",
        "button_disabled_text": "#6e7681",
        "accent": "#58a6ff",
        "accent_soft": "#1f3a5f",
        "warning_text": "#d29922",
        "warning_bg": "#3b2f13",
        "warning_border": "#9e6a03",
        "error": "#ff7b72",
        "preview_bg": "#010409",
    },
    "dracula": {
        "label": "Dracula",
        "window": "#282a36",
        "surface": "#343746",
        "surface_alt": "#3b3f51",
        "field": "#21222c",
        "field_alt": "#2b2d3a",
        "border": "#44475a",
        "border_strong": "#6272a4",
        "text": "#f8f8f2",
        "text_muted": "#b6b9cf",
        "text_strong": "#ffffff",
        "button": "#3b3f51",
        "button_hover": "#44475a",
        "button_pressed": "#303341",
        "button_border": "#6272a4",
        "button_disabled": "#2b2d3a",
        "button_disabled_text": "#7f849d",
        "accent": "#bd93f9",
        "accent_soft": "#49396a",
        "warning_text": "#f1fa8c",
        "warning_bg": "#4a4726",
        "warning_border": "#9d9550",
        "error": "#ff5555",
        "preview_bg": "#21222c",
    },
    "everforest": {
        "label": "Everforest",
        "window": "#2b3339",
        "surface": "#323c41",
        "surface_alt": "#3a454a",
        "field": "#272e33",
        "field_alt": "#2f383e",
        "border": "#4f5b58",
        "border_strong": "#5f6c69",
        "text": "#d3c6aa",
        "text_muted": "#a7b0a0",
        "text_strong": "#fdf6e3",
        "button": "#3a454a",
        "button_hover": "#465258",
        "button_pressed": "#30383d",
        "button_border": "#5f6c69",
        "button_disabled": "#30383d",
        "button_disabled_text": "#7f897d",
        "accent": "#a7c080",
        "accent_soft": "#3f4e36",
        "warning_text": "#dbbc7f",
        "warning_bg": "#4b422b",
        "warning_border": "#8a7447",
        "error": "#e67e80",
        "preview_bg": "#272e33",
    },
    "midnight_ember": {
        "label": "Midnight Ember",
        "window": "#151719",
        "surface": "#1d2125",
        "surface_alt": "#272c31",
        "field": "#101214",
        "field_alt": "#181b1f",
        "border": "#343b42",
        "border_strong": "#4a525b",
        "text": "#d7dde3",
        "text_muted": "#9aa6b2",
        "text_strong": "#f4f7fa",
        "button": "#252a30",
        "button_hover": "#303741",
        "button_pressed": "#1e2328",
        "button_border": "#4a525b",
        "button_disabled": "#1b1f24",
        "button_disabled_text": "#737f8a",
        "accent": "#e06f3e",
        "accent_soft": "#4b2f24",
        "warning_text": "#f3c26f",
        "warning_bg": "#46351f",
        "warning_border": "#8a6534",
        "error": "#ff8270",
        "preview_bg": "#0d0f11",
    },
    "glacier": {
        "label": "Glacier",
        "window": "#e8eef2",
        "surface": "#f8fbfd",
        "surface_alt": "#dfe8ee",
        "field": "#ffffff",
        "field_alt": "#edf3f7",
        "border": "#c3d0da",
        "border_strong": "#aebdc9",
        "text": "#20303b",
        "text_muted": "#60707c",
        "text_strong": "#10202b",
        "button": "#edf3f7",
        "button_hover": "#dce8f0",
        "button_pressed": "#ccdbe5",
        "button_border": "#aebdc9",
        "button_disabled": "#f2f6f8",
        "button_disabled_text": "#8796a2",
        "accent": "#1677a8",
        "accent_soft": "#cce8f4",
        "warning_text": "#8b5e12",
        "warning_bg": "#fff2cf",
        "warning_border": "#ddb867",
        "error": "#b83a32",
        "preview_bg": "#f3f7fa",
    },
    "black_gold": {
        "label": "Black Gold",
        "window": "#11100d",
        "surface": "#191713",
        "surface_alt": "#242017",
        "field": "#0d0c0a",
        "field_alt": "#15130f",
        "border": "#383124",
        "border_strong": "#5c4b2f",
        "text": "#d9d2c0",
        "text_muted": "#a59b86",
        "text_strong": "#fff6dc",
        "button": "#242017",
        "button_hover": "#302919",
        "button_pressed": "#1b1812",
        "button_border": "#5c4b2f",
        "button_disabled": "#191713",
        "button_disabled_text": "#776d5a",
        "accent": "#d3a23b",
        "accent_soft": "#463719",
        "warning_text": "#ffd479",
        "warning_bg": "#433315",
        "warning_border": "#8a6524",
        "error": "#f06b55",
        "preview_bg": "#090806",
    },
    "pine": {
        "label": "Pine",
        "window": "#17201c",
        "surface": "#202b26",
        "surface_alt": "#293630",
        "field": "#111915",
        "field_alt": "#19231e",
        "border": "#34483f",
        "border_strong": "#456258",
        "text": "#d7e3dc",
        "text_muted": "#9fb0a7",
        "text_strong": "#f0faf4",
        "button": "#293630",
        "button_hover": "#34443d",
        "button_pressed": "#202b26",
        "button_border": "#456258",
        "button_disabled": "#1b2520",
        "button_disabled_text": "#75877e",
        "accent": "#46a778",
        "accent_soft": "#244738",
        "warning_text": "#e7c66f",
        "warning_bg": "#44381c",
        "warning_border": "#826a31",
        "error": "#ec7d70",
        "preview_bg": "#0d130f",
    },
    "violet_steel": {
        "label": "Violet Steel",
        "window": "#202129",
        "surface": "#292b35",
        "surface_alt": "#333642",
        "field": "#191b22",
        "field_alt": "#22242d",
        "border": "#444859",
        "border_strong": "#5d6479",
        "text": "#d8dbea",
        "text_muted": "#a9aec2",
        "text_strong": "#f5f6ff",
        "button": "#333642",
        "button_hover": "#3f4352",
        "button_pressed": "#282b35",
        "button_border": "#5d6479",
        "button_disabled": "#22242d",
        "button_disabled_text": "#7b8298",
        "accent": "#9d87f5",
        "accent_soft": "#40365e",
        "warning_text": "#e8c677",
        "warning_bg": "#44361f",
        "warning_border": "#806437",
        "error": "#f07884",
        "preview_bg": "#15171d",
    },
    "crimson_desert": {
        "label": "Crimson Desert",
        "window": "#211814",
        "surface": "#2a1d18",
        "surface_alt": "#35241c",
        "field": "#1b130f",
        "field_alt": "#241914",
        "border": "#513929",
        "border_strong": "#6b4932",
        "text": "#d9c0aa",
        "text_muted": "#a98c77",
        "text_strong": "#f4ddc4",
        "button": "#35241c",
        "button_hover": "#443025",
        "button_pressed": "#2b1d17",
        "button_border": "#6b4932",
        "button_disabled": "#241914",
        "button_disabled_text": "#765d4d",
        "accent": "#c56d43",
        "accent_soft": "#543322",
        "warning_text": "#e8b66d",
        "warning_bg": "#4a321f",
        "warning_border": "#8b6237",
        "error": "#f07b61",
        "preview_bg": "#160f0c",
    },
}


def get_theme(key: str) -> Dict[str, str]:
    return UI_THEME_SCHEMES.get(key, UI_THEME_SCHEMES[DEFAULT_UI_THEME])


def _clamp_font_size(value: int, default: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(9, min(16, numeric))


def _density_metrics(density_key: str) -> Dict[str, int]:
    density = (density_key or DEFAULT_UI_DENSITY).strip().lower()
    if density == "comfortable":
        return {
            "menu_pad_y": 5,
            "menu_pad_x": 10,
            "menu_item_pad_y": 6,
            "menu_item_pad_x": 12,
            "group_margin_top": 18,
            "group_pad_top": 12,
            "group_title_pad_y": 1,
            "group_title_pad_x": 8,
            "section_pad_y": 8,
            "section_pad_x": 10,
            "field_pad_y": 6,
            "field_pad_x": 9,
            "list_pad_y": 4,
            "list_pad_x": 6,
            "header_pad_y": 6,
            "header_pad_x": 8,
            "button_pad_y": 7,
            "button_pad_x": 12,
            "button_min_h": 22,
            "progress_min_h": 24,
            "tab_pad_top": 8,
            "tab_pad_bottom": 9,
            "tab_pad_x": 14,
            "tab_min_h": 24,
        }
    if density == "normal":
        return {
            "menu_pad_y": 4,
            "menu_pad_x": 9,
            "menu_item_pad_y": 5,
            "menu_item_pad_x": 10,
            "group_margin_top": 15,
            "group_pad_top": 10,
            "group_title_pad_y": 0,
            "group_title_pad_x": 7,
            "section_pad_y": 6,
            "section_pad_x": 9,
            "field_pad_y": 5,
            "field_pad_x": 8,
            "list_pad_y": 3,
            "list_pad_x": 5,
            "header_pad_y": 5,
            "header_pad_x": 7,
            "button_pad_y": 5,
            "button_pad_x": 10,
            "button_min_h": 18,
            "progress_min_h": 20,
            "tab_pad_top": 6,
            "tab_pad_bottom": 7,
            "tab_pad_x": 12,
            "tab_min_h": 20,
        }
    return {
        "menu_pad_y": 3,
        "menu_pad_x": 8,
        "menu_item_pad_y": 4,
        "menu_item_pad_x": 9,
        "group_margin_top": 13,
        "group_pad_top": 8,
        "group_title_pad_y": 0,
        "group_title_pad_x": 6,
        "section_pad_y": 5,
        "section_pad_x": 8,
        "field_pad_y": 4,
        "field_pad_x": 7,
        "list_pad_y": 2,
        "list_pad_x": 5,
        "header_pad_y": 4,
        "header_pad_x": 6,
        "button_pad_y": 4,
        "button_pad_x": 8,
        "button_min_h": 16,
        "progress_min_h": 18,
        "tab_pad_top": 5,
        "tab_pad_bottom": 6,
        "tab_pad_x": 10,
        "tab_min_h": 18,
    }


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


def build_app_stylesheet(
    theme_key: str,
    *,
    base_font_size: int = DEFAULT_UI_FONT_SIZE,
    data_font_size: int = DEFAULT_UI_DATA_FONT_SIZE,
    density_key: str = DEFAULT_UI_DENSITY,
) -> str:
    theme = get_theme(theme_key)
    base_size = _clamp_font_size(base_font_size, DEFAULT_UI_FONT_SIZE)
    table_size = _clamp_font_size(data_font_size, base_size)
    hint_size = max(9, base_size - 1)
    metrics = _density_metrics(density_key)
    role_text = {
        "identity": "#b45309" if theme_key == "crimson_desert" else "#0369a1" if theme_key == "light" else "#7dd3fc",
        "dds": "#c56d43" if theme_key == "crimson_desert" else "#047857" if theme_key == "light" else "#86efac",
        "ncnn": "#d89a5f" if theme_key == "crimson_desert" else "#6d28d9" if theme_key == "light" else "#c4b5fd",
        "correction": "#e8b66d" if theme_key == "crimson_desert" else "#b45309" if theme_key == "light" else "#fbbf24",
    }
    return f"""
    QWidget {{
        font-size: {base_size}px;
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
        padding: {metrics["menu_pad_y"]}px {metrics["menu_pad_x"]}px;
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
        padding: {metrics["menu_item_pad_y"]}px 16px {metrics["menu_item_pad_y"]}px {metrics["menu_item_pad_x"]}px;
        border-radius: 4px;
    }}
    QMenu::item:selected {{
        background: {theme["accent_soft"]};
        color: {theme["text_strong"]};
    }}
    QLabel, QCheckBox, QToolButton {{
        background: transparent;
    }}
    QWidget#FlatSectionPanel {{
        background: {theme["surface"]};
    }}
    QWidget#FlatSectionHeader {{
        background: transparent;
    }}
    QLabel#FlatSectionTitle {{
        color: {theme["text_strong"]};
        font-weight: 600;
        background: transparent;
        padding: 0px {metrics["group_title_pad_x"] + 2}px 1px {metrics["group_title_pad_x"] + 2}px;
        border: none;
    }}
    QFrame#FlatSectionBody {{
        background: {theme["surface"]};
        border: 1px solid {theme["border"]};
        border-radius: 5px;
    }}
    QWidget#EmptyStatePanel {{
        background: {theme["preview_bg"]};
        border: 1px dashed {theme["border_strong"]};
        border-radius: 5px;
    }}
    QLabel#EmptyStateTitle {{
        color: {theme["text_strong"]};
        font-weight: 600;
        background: transparent;
    }}
    QLabel#EmptyStateDetail {{
        color: {theme["text_muted"]};
        background: transparent;
    }}
    QGroupBox {{
        border: 1px solid {theme["border"]};
        border-radius: 5px;
        margin-top: {max(18, metrics["group_margin_top"] + 5)}px;
        padding-top: {max(10, metrics["group_pad_top"] + 1)}px;
        font-weight: 600;
        background: {theme["surface"]};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 14px;
        top: 0px;
        margin: 0px;
        padding: 0px {metrics["group_title_pad_x"] + 2}px 1px {metrics["group_title_pad_x"] + 2}px;
        color: {theme["text_strong"]};
        background: transparent;
    }}
    QToolButton#SectionToggle {{
        text-align: left;
        background: {theme["surface_alt"]};
        color: {theme["text_strong"]};
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        padding: {metrics["section_pad_y"]}px {metrics["section_pad_x"]}px;
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
    QFrame#WorkflowProfilePanel {{
        background: {theme["field_alt"]};
        border: 1px solid {theme["border"]};
        border-radius: 4px;
    }}
    QFrame#WorkflowProfilePanel[profileRole="identity"] {{
        border-left: 3px solid #38bdf8;
    }}
    QFrame#WorkflowProfilePanel[profileRole="dds"] {{
        border-left: 3px solid #22c55e;
    }}
    QFrame#WorkflowProfilePanel[profileRole="ncnn"] {{
        border-left: 3px solid #a78bfa;
    }}
    QFrame#WorkflowProfilePanel[profileRole="correction"] {{
        border-left: 3px solid #f59e0b;
    }}
    QLabel#WorkflowProfilePanelTitle {{
        font-weight: 700;
        background: transparent;
    }}
    QLabel#WorkflowProfilePanelTitle[profileRole="identity"] {{
        color: {role_text["identity"]};
    }}
    QLabel#WorkflowProfilePanelTitle[profileRole="dds"] {{
        color: {role_text["dds"]};
    }}
    QLabel#WorkflowProfilePanelTitle[profileRole="ncnn"] {{
        color: {role_text["ncnn"]};
    }}
    QLabel#WorkflowProfilePanelTitle[profileRole="correction"] {{
        color: {role_text["correction"]};
    }}
    QLabel#WorkflowProfileFieldLabel {{
        color: {theme["text_strong"]};
        background: transparent;
        font-weight: 600;
    }}
    QFrame#DdsFlowPanel {{
        background: {theme["field_alt"]};
        border: 1px solid {theme["border_strong"]};
        border-radius: 5px;
    }}
    QFrame#DdsFlowRow {{
        background: {theme["surface"]};
        border: 1px solid {theme["border"]};
        border-radius: 4px;
    }}
    QLabel#DdsFlowChip {{
        border-radius: 5px;
        background: {theme["text_muted"]};
    }}
    QLabel#DdsFlowChip[flowRole="source"] {{
        background: #38bdf8;
    }}
    QLabel#DdsFlowChip[flowRole="final"] {{
        background: #22c55e;
    }}
    QLabel#DdsFlowChip[flowRole="dds"] {{
        background: #f59e0b;
    }}
    QLabel#DdsFlowChip[flowRole="note"] {{
        background: #f87171;
    }}
    QLabel#DdsFlowTitle {{
        color: {theme["text_strong"]};
        background: transparent;
        font-weight: 700;
    }}
    QLabel#DdsFlowValue {{
        color: {theme["text"]};
        background: {theme["field"]};
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        padding: 4px 6px;
    }}
    QFrame#GuidancePanel {{
        background: transparent;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
    }}
    QFrame#GuidanceRow {{
        background: transparent;
        border: none;
        border-radius: 0px;
    }}
    QFrame#GuidanceRow[guidanceRole="warning"],
    QFrame#GuidanceRow[guidanceRole="override"] {{
        background: {theme["warning_bg"]};
        border: 1px solid {theme["warning_border"]};
        border-radius: 4px;
    }}
    QLabel#GuidanceChip {{
        border-radius: 5px;
        background: {theme["text_muted"]};
    }}
    QLabel#GuidanceChip[guidanceRole="summary"],
    QLabel#GuidanceChip[guidanceRole="scope"] {{
        background: #38bdf8;
    }}
    QLabel#GuidanceChip[guidanceRole="upscaled"],
    QLabel#GuidanceChip[guidanceRole="scale"] {{
        background: #22c55e;
    }}
    QLabel#GuidanceChip[guidanceRole="copied"],
    QLabel#GuidanceChip[guidanceRole="tile"] {{
        background: #a78bfa;
    }}
    QLabel#GuidanceChip[guidanceRole="rules"],
    QLabel#GuidanceChip[guidanceRole="correction"] {{
        background: #f59e0b;
    }}
    QLabel#GuidanceChip[guidanceRole="override"],
    QLabel#GuidanceChip[guidanceRole="warning"] {{
        background: #f87171;
    }}
    QLabel#GuidanceTitle {{
        color: {theme["text_strong"]};
        background: transparent;
        font-weight: 700;
    }}
    QLabel#GuidanceValue {{
        color: {theme["text"]};
        background: transparent;
        border: none;
        padding: 1px 0px;
    }}
    QFrame#GuidanceRow[guidanceRole="warning"] QLabel#GuidanceValue,
    QFrame#GuidanceRow[guidanceRole="override"] QLabel#GuidanceValue {{
        color: {theme["warning_text"]};
        background: transparent;
        border: none;
    }}
    QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox {{
        background: {theme["field"]};
        border: 1px solid {theme["border_strong"]};
        border-radius: 4px;
        padding: {metrics["field_pad_y"]}px {metrics["field_pad_x"]}px;
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
        font-size: {table_size}px;
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
        padding: {metrics["list_pad_y"] + 1}px {metrics["list_pad_x"]}px;
        border-radius: 3px;
    }}
    QListWidget::item:selected, QTreeWidget::item:selected {{
        background: {theme["accent_soft"]};
        color: {theme["text_strong"]};
    }}
    QTreeWidget::item {{
        padding: {metrics["list_pad_y"]}px {metrics["list_pad_x"]}px;
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus, QComboBox:focus, QSpinBox:focus,
    QListWidget:focus, QTreeWidget:focus {{
        border: 1px solid {theme["accent"]};
    }}
    QHeaderView::section {{
        font-size: {table_size}px;
        background: {theme["surface_alt"]};
        color: {theme["text_muted"]};
        border: none;
        border-right: 1px solid {theme["border"]};
        padding: {metrics["header_pad_y"]}px {metrics["header_pad_x"]}px;
    }}
    QPushButton {{
        background: {theme["button"]};
        border: 1px solid {theme["button_border"]};
        border-radius: 4px;
        padding: {metrics["button_pad_y"]}px {metrics["button_pad_x"]}px;
        min-height: {metrics["button_min_h"]}px;
    }}
    QPushButton:hover {{
        background: {theme["button_hover"]};
    }}
    QPushButton:pressed {{
        background: {theme["button_pressed"]};
    }}
    QPushButton:checked {{
        color: #ffffff;
        background: #16803c;
        border-color: #2fbf64;
        font-weight: 600;
    }}
    QPushButton:checked:hover {{
        background: #1f9a4d;
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
        color: #ffffff;
        font-weight: 600;
        text-align: center;
        min-height: {metrics["progress_min_h"]}px;
    }}
    QProgressBar::chunk {{
        border-radius: 3px;
        background: {theme["accent"]};
    }}
    QLabel#HintLabel {{
        font-size: {hint_size}px;
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
        padding: 8px;
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
        padding: {metrics["tab_pad_top"]}px {metrics["tab_pad_x"]}px {metrics["tab_pad_bottom"]}px {metrics["tab_pad_x"]}px;
        min-height: {metrics["tab_min_h"]}px;
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
        background: {theme["field"]};
        width: 12px;
        margin: 1px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {theme["button_border"]};
        border: 1px solid {theme["border_strong"]};
        min-height: 24px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {theme["accent_soft"]};
        border: 1px solid {theme["accent"]};
    }}
    QScrollBar::handle:vertical:pressed {{
        background: {theme["accent"]};
        border: 1px solid {theme["accent"]};
    }}
    QScrollBar:horizontal {{
        background: {theme["field"]};
        height: 12px;
        margin: 1px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {theme["button_border"]};
        border: 1px solid {theme["border_strong"]};
        min-width: 24px;
        border-radius: 4px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {theme["accent_soft"]};
        border: 1px solid {theme["accent"]};
    }}
    QScrollBar::handle:horizontal:pressed {{
        background: {theme["accent"]};
        border: 1px solid {theme["accent"]};
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: transparent;
        border-radius: 4px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        background: transparent;
        border: none;
        width: 0px;
        height: 0px;
    }}
    QToolTip {{
        background: {theme["surface_alt"]};
        color: {theme["text_strong"]};
        border: 1px solid {theme["border"]};
        padding: 6px 8px;
    }}
    """
