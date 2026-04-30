from pathlib import Path

from cdmw.ui.localization import UiLocalizer


def test_reviewed_gui_translations_are_available_for_spanish_and_german() -> None:
    spanish = UiLocalizer(language_dir=Path("__unused__"), language_code="es")
    german = UiLocalizer(language_dir=Path("__unused__"), language_code="de")

    assert spanish.translate("Apply Texture Plan to Overrides...") == "Aplicar plan de texturas a anulaciones..."
    assert german.translate("Apply Texture Plan to Overrides...") == "Texturplan auf Overrides anwenden..."
    assert spanish.translate("Texture source probe") == "Sonda de origen de textura"
    assert german.translate("Texture source probe") == "Texturquellen-Probe"
    assert spanish.translate("Exact Item Name") == "Nombre exacto de item"
    assert german.translate("Exact Item Name") == "Exakter Item-Name"
    assert spanish.translate("Name Match") == "Coincidencia de nombre"
    assert german.translate("Name Match") == "Namensabgleich"
    assert spanish.translate("Window") == "Ventana"
    assert german.translate("Window") == "Fenster"
    assert spanish.translate("Detach Current Tab") == "Separar pestana actual"
    assert german.translate("Detach Current Tab") == "Aktuellen Tab abtrennen"
    assert spanish.translate("Show Text Search") == "Mostrar busqueda de texto"
    assert german.translate("Show Text Search") == "Textsuche anzeigen"
    assert spanish.translate("Global font size (8-15 px)") == "Tamano de fuente global (8-15 px)"
    assert german.translate("Lists / columns font size (8-15 px)") == "Schriftgroesse fuer Listen / Spalten (8-15 px)"
    assert spanish.translate("Existing PNG folder") == "Carpeta PNG existente"
    assert german.translate("Rebuilt DDS folder") == "Neu erstellter DDS-Ordner"
    assert spanish.translate("Shortcuts") == "Atajos"
    assert german.translate("Shortcuts") == "Tastenkurzel"
    assert spanish.translate(
        "Paint tool active. Brush presets, image stamps, patterns, and symmetry are available here. Alt+click samples a color into the paint swatch."
    ).startswith("Herramienta de pintura activa.")


def test_builtin_fallback_translates_short_unlisted_gui_labels() -> None:
    spanish = UiLocalizer(language_dir=Path("__unused__"), language_code="es")
    german = UiLocalizer(language_dir=Path("__unused__"), language_code="de")

    assert spanish.translate("Custom") == "Personalizado"
    assert german.translate("Custom") == "Benutzerdefiniert"
    assert spanish.translate("Expected NCNN model contents") == "Contenido esperado del modelo NCNN"
    assert german.translate("Expected NCNN model contents") == "Erwarteter NCNN-Modellinhalt"
    assert spanish.translate("Swap With In-Game Mesh...") == "Intercambiar con malla del juego..."
    assert german.translate("Swap With In-Game Mesh...") == "Mit Ingame-Mesh tauschen..."


def test_builtin_fallback_leaves_code_like_text_alone() -> None:
    spanish = UiLocalizer(language_dir=Path("__unused__"), language_code="es")
    german = UiLocalizer(language_dir=Path("__unused__"), language_code="de")

    code_like = "{value}\\path"
    assert spanish.translate(code_like) == code_like
    assert german.translate(code_like) == code_like


def test_quick_start_and_documentation_cover_mesh_import_and_swap() -> None:
    widgets_source = Path("cdmw/ui/widgets.py").read_text(encoding="utf-8")
    main_window_source = Path("cdmw/ui/main_window.py").read_text(encoding="utf-8")

    assert "Mesh Quick Guide" in widgets_source
    assert "Guia rapida de mallas" in widgets_source
    assert "Schnellguide fuer Meshes" in widgets_source
    assert "Import DDS Preview" in widgets_source
    assert "Vista previa de importar DDS" in widgets_source
    assert "DDS-Importvorschau" in widgets_source
    assert "Swap With In-Game Mesh" in main_window_source
    assert "Intercambiar con malla del juego" in main_window_source
    assert "Mit Ingame-Mesh tauschen" in main_window_source
