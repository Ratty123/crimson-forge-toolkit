from __future__ import annotations

import ast
import html
import json
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Set, Tuple

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QMainWindow,
    QMenu,
    QTabWidget,
    QTableWidget,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QWidget,
)


LANGUAGE_WARNING = (
    "Translate only the values in the translations object. Keep the English keys unchanged. "
    "Longer text can make buttons, tabs, and dialogs look crowded or clipped."
)

_HTML_TAG_RE = re.compile(r"(<[^>]+>)")
_HTML_NON_TEXT_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_TRANSLATABLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'&/()\-+.,:;!? ]*")


def _looks_like_translatable_text(value: str) -> bool:
    text = _WHITESPACE_RE.sub(" ", str(value or "").strip())
    if not text:
        return False
    if len(text) < 2 or len(text) > 1000:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if text.startswith(("http://", "https://", "file://")):
        return False
    if text.startswith(("#", ".", "*.", "(", ")", ":", ";", "{", "}", "[", "]", "<", "%")):
        return False
    if ";;" in text:
        return False
    if "\\" in text:
        return False
    if re.search(r"#[0-9a-fA-F]{3,8}\b", text):
        return False
    if re.search(r"\b(?:rgba?|hsla?)\s*\(", text, re.IGNORECASE):
        return False
    if re.search(r"\b(?:border|padding|margin|background|font-size|min-height|text-align)\s*:", text, re.IGNORECASE):
        return False
    if re.search(r"\(\?[:=!<iP]", text):
        return False
    compact = text.replace(":", "").replace("/", "").replace("\\", "").replace(".", "").replace("_", "")
    if compact.isdigit():
        return False
    if re.fullmatch(r"[A-Z0-9_./\\:-]+", text) and " " not in text:
        return False
    if re.fullmatch(r"[{}()[\].,;:+\\/<>=_*|#%$@!?\-0-9 ]+", text):
        return False
    return True


def _normalize_translation_key(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", html.unescape(str(value or "")).strip())


def _extract_html_text_segments(value: str) -> Tuple[str, ...]:
    text = str(value or "")
    if "<" not in text or ">" not in text:
        normalized = _normalize_translation_key(text)
        return (normalized,) if _looks_like_translatable_text(normalized) else ()
    text = _HTML_NON_TEXT_BLOCK_RE.sub("", text)
    segments: Set[str] = set()
    for segment in _HTML_TAG_RE.split(text):
        if not segment or segment.startswith("<"):
            continue
        normalized = _normalize_translation_key(segment)
        if _looks_like_translatable_text(normalized):
            segments.add(normalized)
    return tuple(sorted(segments))


def _iter_python_string_literals(path: Path) -> Iterable[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return ()
    values: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
    return tuple(values)


def collect_translatable_source_strings(source_roots: Iterable[Path]) -> Dict[str, str]:
    """Collect English UI/help strings from Python source so export is not limited to visible widgets."""
    strings: Dict[str, str] = {}

    def add(value: str) -> None:
        for candidate in _extract_html_text_segments(value):
            if _looks_like_translatable_text(candidate):
                strings.setdefault(candidate, "")
        normalized = _normalize_translation_key(value)
        if "<" not in str(value) and ">" not in str(value) and _looks_like_translatable_text(normalized):
            strings.setdefault(normalized, "")

    for root in source_roots:
        root_path = Path(root)
        if root_path.is_file() and root_path.suffix.lower() == ".py":
            for value in _iter_python_string_literals(root_path):
                add(value)
            continue
        if not root_path.is_dir():
            continue
        for path in sorted(root_path.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.name == "localization.py":
                continue
            for value in _iter_python_string_literals(path):
                add(value)
    return dict(sorted(strings.items()))


def _translate_html_text(value: str, translate: Callable[[str], str]) -> str:
    text = str(value or "")
    if "<" not in text or ">" not in text:
        return translate(text)
    text = _HTML_NON_TEXT_BLOCK_RE.sub("", text)
    parts: list[str] = []
    for segment in _HTML_TAG_RE.split(text):
        if not segment or segment.startswith("<"):
            parts.append(segment)
            continue
        leading_len = len(segment) - len(segment.lstrip())
        trailing_len = len(segment) - len(segment.rstrip())
        leading = segment[:leading_len]
        trailing = segment[len(segment) - trailing_len :] if trailing_len else ""
        body = segment[leading_len : len(segment) - trailing_len if trailing_len else len(segment)]
        key = _normalize_translation_key(body)
        translated = translate(key)
        parts.append(leading + html.escape(translated, quote=False) + trailing)
    return "".join(parts)


BUILTIN_LANGUAGES: Dict[str, Dict[str, object]] = {
    "en": {
        "language_name": "English",
        "translations": {},
    },
    "es": {
        "language_name": "Spanish",
        "translations": {
            "Texture Workflow": "Flujo de texturas",
            "Archive Browser": "Explorador de archivos",
            "Replace Assistant": "Asistente de reemplazo",
            "Texture Editor": "Editor de texturas",
            "Research": "Investigacion",
            "Text Search": "Busqueda de texto",
            "Settings": "Configuracion",
            "Setup": "Configuracion inicial",
            "Paths": "Rutas",
            "Appearance": "Apariencia",
            "Startup": "Inicio",
            "Layout": "Diseno",
            "Safety": "Seguridad",
            "3D Preview / Graphics": "Vista 3D / Graficos",
            "Advanced Preview Shading": "Sombreado avanzado",
            "Theme": "Tema",
            "Language": "Idioma",
            "Export Language File...": "Exportar archivo de idioma...",
            "Import Language File...": "Importar archivo de idioma...",
            "Language files map English UI text to translated text. Longer translations can make buttons, tabs, and dialogs look crowded or clipped.": "Los archivos de idioma asignan texto ingles de la interfaz a texto traducido. Las traducciones largas pueden hacer que botones, pestanas y dialogos se vean llenos o cortados.",
            "Global font family": "Fuente global",
            "Global font size": "Tamano de fuente global",
            "Lists / columns font size": "Tamano de listas / columnas",
            "Log / code font": "Fuente de registro / codigo",
            "Log / code size": "Tamano de registro / codigo",
            "Bold emphasis in logs / code": "Enfasis en negrita en registros / codigo",
            "Auto-load Archive Browser on startup": "Cargar explorador de archivos al iniciar",
            "Prefer archive cache on startup": "Preferir cache de archivos al iniciar",
            "Restore last active tab": "Restaurar ultima pestana activa",
            "Remember pane sizes and splitters": "Recordar tamanos de paneles",
            "Confirm clearing PNG / DDS output folders before Start": "Confirmar limpieza de carpetas PNG / DDS antes de iniciar",
            "Confirm clearing archive extraction target": "Confirmar limpieza del destino de extraccion",
            "Capture crash details to local report files on unhandled exceptions": "Guardar detalles de fallos en informes locales",
            "Use textures by default when available": "Usar texturas por defecto si estan disponibles",
            "Use support-map preview shading by default": "Usar sombreado de mapas de soporte por defecto",
            "Visible texture mode": "Modo de textura visible",
            "Preview texture max size": "Tamano maximo de textura de vista previa",
            "Low-quality texture size": "Tamano de textura de baja calidad",
            "Anisotropy": "Anisotropia",
            "Orbit sensitivity": "Sensibilidad de orbita",
            "Pan sensitivity": "Sensibilidad de desplazamiento",
            "Control inversion": "Inversion de controles",
            "Invert orbit X": "Invertir orbita X",
            "Invert orbit Y": "Invertir orbita Y",
            "Invert pan X": "Invertir desplazamiento X",
            "Invert pan Y": "Invertir desplazamiento Y",
            "Reset Preview Settings": "Restablecer vista previa",
            "Original DDS root": "Raiz DDS original",
            "PNG root": "Raiz PNG",
            "Texture Editor PNG root": "Raiz PNG del editor",
            "Staging PNG root": "Raiz PNG temporal",
            "Output root": "Raiz de salida",
            "texconv.exe path": "Ruta de texconv.exe",
            "Browse": "Examinar",
            "Init Workspace": "Inicializar espacio",
            "Create Folders": "Crear carpetas",
            "Open File In Texture Editor": "Abrir archivo en editor",
            "Open chaiNNer Download Page": "Abrir descarga de chaiNNer",
            "Open texconv Download Page": "Abrir descarga de texconv",
            "Open Real-ESRGAN NCNN Download Page": "Abrir descarga de Real-ESRGAN NCNN",
            "Import NCNN Models": "Importar modelos NCNN",
            "Dry run": "Simulacion",
            "Enable incremental resume": "Activar reanudacion incremental",
            "Write CSV log": "Escribir registro CSV",
            "Allow unique basename fallback": "Permitir coincidencia por nombre unico",
            "Overwrite existing DDS": "Sobrescribir DDS existente",
            "DDS Output": "Salida DDS",
            "Format": "Formato",
            "Size": "Tamano",
            "Mipmaps": "Mipmaps",
            "Custom format": "Formato personalizado",
            "Custom size": "Tamano personalizado",
            "Custom mip count": "Numero personalizado de mips",
            "Folder / file filter": "Filtro de carpeta / archivo",
            "Workflow Profiles": "Perfiles de flujo",
            "Selected Profile": "Perfil seleccionado",
            "Ordered Rules": "Reglas ordenadas",
            "Selected Rule": "Regla seleccionada",
            "Matched Files": "Archivos coincidentes",
            "Add": "Agregar",
            "Duplicate": "Duplicar",
            "Delete": "Eliminar",
            "Move Up": "Subir",
            "Move Down": "Bajar",
            "Refresh": "Actualizar",
            "Assign Profile": "Asignar perfil",
            "Name": "Nombre",
            "Action": "Accion",
            "Enabled": "Activado",
            "Match": "Coincidencia",
            "Pattern": "Patron",
            "Semantic": "Semantica",
            "Colorspace": "Espacio de color",
            "Alpha Policy": "Politica alfa",
            "Planner Profile": "Perfil del planificador",
            "Planner Path": "Ruta del planificador",
            "Upscaling": "Escalado",
            "Backend": "Motor",
            "Texture Policy": "Politica de texturas",
            "Preset": "Preajuste",
            "Progress": "Progreso",
            "Phase": "Fase",
            "Phase progress": "Progreso de fase",
            "Total files found": "Total de archivos",
            "Current file": "Archivo actual",
            "Converted / planned": "Convertidos / planificados",
            "Skipped": "Omitidos",
            "Failed": "Fallidos",
            "Status": "Estado",
            "Live Log": "Registro en vivo",
            "Clear Log": "Limpiar registro",
            "Compare": "Comparar",
            "Previous": "Anterior",
            "Next": "Siguiente",
            "Sync Pan": "Sincronizar desplazamiento",
            "Preview": "Vista previa",
            "Details": "Detalles",
            "Referenced Files": "Archivos referenciados",
            "Open": "Abrir",
            "Export Selected...": "Exportar seleccionados...",
            "Export All...": "Exportar todo...",
            "Export OBJ...": "Exportar OBJ...",
            "Export FBX...": "Exportar FBX...",
            "Import OBJ Preview...": "Vista previa de importar OBJ...",
            "Import DDS Preview...": "Vista previa de importar DDS...",
            "Import OBJ...": "Importar OBJ...",
            "Restore Backup...": "Restaurar copia...",
            "3D Preview Settings...": "Configuracion de vista 3D...",
            "Flip Base V": "Invertir base V",
            "Disable Support Maps": "Desactivar mapas de soporte",
            "Reset Preview Overrides": "Restablecer cambios de vista",
            "Quick Start": "Inicio rapido",
            "Documentation": "Documentacion",
            "Open Documentation": "Abrir documentacion",
            "Export Diagnostics...": "Exportar diagnosticos...",
            "Close": "Cerrar",
        },
    },
    "de": {
        "language_name": "German",
        "translations": {
            "Texture Workflow": "Textur-Workflow",
            "Archive Browser": "Archiv-Browser",
            "Replace Assistant": "Ersetzungsassistent",
            "Texture Editor": "Textur-Editor",
            "Research": "Recherche",
            "Text Search": "Textsuche",
            "Settings": "Einstellungen",
            "Setup": "Einrichtung",
            "Paths": "Pfade",
            "Appearance": "Darstellung",
            "Startup": "Start",
            "Layout": "Layout",
            "Safety": "Sicherheit",
            "3D Preview / Graphics": "3D-Vorschau / Grafik",
            "Advanced Preview Shading": "Erweiterte Vorschau-Schattierung",
            "Theme": "Theme",
            "Language": "Sprache",
            "Export Language File...": "Sprachdatei exportieren...",
            "Import Language File...": "Sprachdatei importieren...",
            "Language files map English UI text to translated text. Longer translations can make buttons, tabs, and dialogs look crowded or clipped.": "Sprachdateien ordnen englischen UI-Text uebersetztem Text zu. Laengere Uebersetzungen koennen Buttons, Tabs und Dialoge voll oder abgeschnitten wirken lassen.",
            "Global font family": "Globale Schriftart",
            "Global font size": "Globale Schriftgroesse",
            "Lists / columns font size": "Schriftgroesse fuer Listen / Spalten",
            "Log / code font": "Log- / Code-Schrift",
            "Log / code size": "Log- / Code-Groesse",
            "Bold emphasis in logs / code": "Fette Hervorhebung in Logs / Code",
            "Auto-load Archive Browser on startup": "Archiv-Browser beim Start laden",
            "Prefer archive cache on startup": "Archiv-Cache beim Start bevorzugen",
            "Restore last active tab": "Letzten aktiven Tab wiederherstellen",
            "Remember pane sizes and splitters": "Panelgroessen und Trenner merken",
            "Confirm clearing PNG / DDS output folders before Start": "Leeren von PNG-/DDS-Ausgabeordnern vor Start bestaetigen",
            "Confirm clearing archive extraction target": "Leeren des Archiv-Extraktionsziels bestaetigen",
            "Capture crash details to local report files on unhandled exceptions": "Absturzinformationen lokal speichern",
            "Use textures by default when available": "Texturen standardmaessig verwenden",
            "Use support-map preview shading by default": "Support-Map-Vorschau-Shading standardmaessig verwenden",
            "Visible texture mode": "Sichtbarer Texturmodus",
            "Preview texture max size": "Max. Texturgroesse der Vorschau",
            "Low-quality texture size": "Texturgroesse bei niedriger Qualitaet",
            "Anisotropy": "Anisotropie",
            "Orbit sensitivity": "Orbit-Empfindlichkeit",
            "Pan sensitivity": "Pan-Empfindlichkeit",
            "Control inversion": "Steuerungsumkehr",
            "Invert orbit X": "Orbit X umkehren",
            "Invert orbit Y": "Orbit Y umkehren",
            "Invert pan X": "Pan X umkehren",
            "Invert pan Y": "Pan Y umkehren",
            "Reset Preview Settings": "Vorschau-Einstellungen zuruecksetzen",
            "Original DDS root": "Original-DDS-Stamm",
            "PNG root": "PNG-Stamm",
            "Texture Editor PNG root": "PNG-Stamm des Editors",
            "Staging PNG root": "Temporaerer PNG-Stamm",
            "Output root": "Ausgabe-Stamm",
            "texconv.exe path": "Pfad zu texconv.exe",
            "Browse": "Durchsuchen",
            "Init Workspace": "Arbeitsbereich einrichten",
            "Create Folders": "Ordner erstellen",
            "Open File In Texture Editor": "Datei im Textur-Editor oeffnen",
            "Open chaiNNer Download Page": "chaiNNer-Download oeffnen",
            "Open texconv Download Page": "texconv-Download oeffnen",
            "Open Real-ESRGAN NCNN Download Page": "Real-ESRGAN-NCNN-Download oeffnen",
            "Import NCNN Models": "NCNN-Modelle importieren",
            "Dry run": "Probelauf",
            "Enable incremental resume": "Inkrementelles Fortsetzen aktivieren",
            "Write CSV log": "CSV-Log schreiben",
            "Allow unique basename fallback": "Eindeutigen Dateinamen-Fallback erlauben",
            "Overwrite existing DDS": "Vorhandene DDS ueberschreiben",
            "DDS Output": "DDS-Ausgabe",
            "Format": "Format",
            "Size": "Groesse",
            "Mipmaps": "Mipmaps",
            "Custom format": "Benutzerdefiniertes Format",
            "Custom size": "Benutzerdefinierte Groesse",
            "Custom mip count": "Benutzerdefinierte Mip-Anzahl",
            "Folder / file filter": "Ordner- / Dateifilter",
            "Workflow Profiles": "Workflow-Profile",
            "Selected Profile": "Ausgewaehltes Profil",
            "Ordered Rules": "Geordnete Regeln",
            "Selected Rule": "Ausgewaehlte Regel",
            "Matched Files": "Gefundene Dateien",
            "Add": "Hinzufuegen",
            "Duplicate": "Duplizieren",
            "Delete": "Loeschen",
            "Move Up": "Nach oben",
            "Move Down": "Nach unten",
            "Refresh": "Aktualisieren",
            "Assign Profile": "Profil zuweisen",
            "Name": "Name",
            "Action": "Aktion",
            "Enabled": "Aktiviert",
            "Match": "Treffer",
            "Pattern": "Muster",
            "Semantic": "Semantik",
            "Colorspace": "Farbraum",
            "Alpha Policy": "Alpha-Richtlinie",
            "Planner Profile": "Planerprofil",
            "Planner Path": "Planerpfad",
            "Upscaling": "Hochskalierung",
            "Backend": "Backend",
            "Texture Policy": "Textur-Richtlinie",
            "Preset": "Voreinstellung",
            "Progress": "Fortschritt",
            "Phase": "Phase",
            "Phase progress": "Phasenfortschritt",
            "Total files found": "Dateien insgesamt",
            "Current file": "Aktuelle Datei",
            "Converted / planned": "Konvertiert / geplant",
            "Skipped": "Uebersprungen",
            "Failed": "Fehlgeschlagen",
            "Status": "Status",
            "Live Log": "Live-Log",
            "Clear Log": "Log leeren",
            "Compare": "Vergleichen",
            "Previous": "Zurueck",
            "Next": "Weiter",
            "Sync Pan": "Pan synchronisieren",
            "Preview": "Vorschau",
            "Details": "Details",
            "Referenced Files": "Referenzierte Dateien",
            "Open": "Oeffnen",
            "Export Selected...": "Auswahl exportieren...",
            "Export All...": "Alles exportieren...",
            "Export OBJ...": "OBJ exportieren...",
            "Export FBX...": "FBX exportieren...",
            "Import OBJ Preview...": "OBJ-Importvorschau...",
            "Import DDS Preview...": "DDS-Importvorschau...",
            "Import OBJ...": "OBJ importieren...",
            "Restore Backup...": "Backup wiederherstellen...",
            "3D Preview Settings...": "3D-Vorschau einstellen...",
            "Flip Base V": "Basis V spiegeln",
            "Disable Support Maps": "Support-Maps deaktivieren",
            "Reset Preview Overrides": "Vorschau-Aenderungen zuruecksetzen",
            "Quick Start": "Schnellstart",
            "Documentation": "Dokumentation",
            "Open Documentation": "Dokumentation oeffnen",
            "Export Diagnostics...": "Diagnose exportieren...",
            "Close": "Schliessen",
        },
    },
}


_EXTRA_BUILTIN_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "Archive Browser Performance": "Rendimiento del explorador de archivos",
        "Archive Locations": "Ubicaciones de archivo",
        "Game And Extraction Paths": "Rutas del juego y extraccion",
        "Game / Package": "Juego / paquete",
        "Open Archive Locations": "Abrir ubicaciones de archivo",
        "Highlight intensity": "Intensidad de resaltado",
        "Log color scheme": "Esquema de color del registro",
        "Preview/details color scheme": "Esquema de color de vista/detalles",
        "In-game Name": "Nombre en juego",
        "Follow app theme": "Usar tema de la app",
        "VS Code classic": "VS Code clasico",
        "Terminal contrast": "Contraste de terminal",
        "Accessible contrast": "Contraste accesible",
        "Solarized": "Solarized",
        "3D Preview Settings": "Configuracion de vista 3D",
        "Aa": "Aa",
        "Add Rule": "Agregar regla",
        "Affected": "Afectados",
        "All textures (advanced)": "Todas las texturas (avanzado)",
        "Allow archive patching": "Permitir parcheo de archivos",
        "Also show already classified DDS families": "Mostrar tambien familias DDS ya clasificadas",
        "Analysis": "Analisis",
        "Archive Extraction Target": "Destino de extraccion de archivo",
        "Archive Files": "Archivos del archivo",
        "Archive Insights": "Informacion de archivos",
        "Archive Preview": "Vista de archivo",
        "Archive files": "Archivos de paquete",
        "Archive scan": "Escaneo de archivos",
        "Archive tree": "Arbol de archivos",
        "Archives": "Archivos",
        "Assign": "Asignar",
        "Assign Profile": "Asignar perfil",
        "Audio Export Complete": "Exportacion de audio completada",
        "Avg Ratio": "Relacion media",
        "Avg Risk": "Riesgo medio",
        "Balanced mixed textures (recommended)": "Texturas mixtas equilibradas (recomendado)",
        "Band": "Banda",
        "Base": "Base",
        "Browse...": "Examinar...",
        "Byte Delta": "Diferencia de bytes",
        "Cancel": "Cancelar",
        "Case sensitive": "Distinguir mayusculas",
        "Choose a documentation topic or search by feature name.": "Elige un tema de documentacion o busca por nombre de funcion.",
        "Classification": "Clasificacion",
        "Classification Review": "Revision de clasificacion",
        "Class Risk": "Riesgo por clase",
        "Clear": "Limpiar",
        "Clear Current Family": "Limpiar familia actual",
        "Clear Current File": "Limpiar archivo actual",
        "Clear Folders": "Limpiar carpetas",
        "Clear Log": "Limpiar registro",
        "Clear Root": "Limpiar raiz",
        "Clear Selection": "Limpiar seleccion",
        "Clear Selected Families": "Limpiar familias seleccionadas",
        "Close": "Cerrar",
        "Color + UI + emissive": "Color + UI + emisivo",
        "Color + UI only (safer)": "Solo color + UI (mas seguro)",
        "Column": "Columna",
        "Common Failure Causes": "Causas comunes de fallo",
        "Compare and Review": "Comparar y revisar",
        "Confidence": "Confianza",
        "Configured sources are present. Use this summary to verify paths and policy before starting a batch run.": "Las fuentes configuradas estan presentes. Usa este resumen para verificar rutas y politicas antes de iniciar un lote.",
        "Continue": "Continuar",
        "Continue Anyway": "Continuar de todos modos",
        "Controls": "Controles",
        "Copy": "Copiar",
        "Create source PNGs from DDS before processing": "Crear PNG de origen desde DDS antes de procesar",
        "Current": "Actual",
        "DDS Extraction Target": "Destino de extraccion DDS",
        "DDS Files Need Saved Classification": "Los DDS necesitan clasificacion guardada",
        "DDS Import Preview:": "Vista previa de importacion DDS:",
        "Default": "Predeterminado",
        "Delta": "Diferencia",
        "Detailed notes, evidence, and policy reasons will appear here.": "Aqui apareceran notas detalladas, evidencia y razones de politica.",
        "Disabled": "Desactivado",
        "Documentation": "Documentacion",
        "Done": "Listo",
        "Export Complete": "Exportacion completa",
        "Export Language File": "Exportar archivo de idioma",
        "Export Results": "Exportar resultados",
        "Export Selected": "Exportar seleccionados",
        "Exported file:": "Archivo exportado:",
        "Ext": "Ext",
        "Extensions": "Extensiones",
        "Extract Root": "Raiz de extraccion",
        "Extract Selected Set": "Extraer conjunto seleccionado",
        "Failed": "Fallidos",
        "File": "Archivo",
        "File Name": "Nombre de archivo",
        "Files": "Archivos",
        "Find in preview": "Buscar en vista previa",
        "Folder": "Carpeta",
        "General": "General",
        "Group": "Grupo",
        "Groups": "Grupos",
        "Heatmap": "Mapa de calor",
        "Height": "Altura",
        "Idle": "Inactivo",
        "Import Language File": "Importar archivo de idioma",
        "Imported language:": "Idioma importado:",
        "Include": "Incluir",
        "Issues": "Problemas",
        "Keep Both (Rename New Files)": "Mantener ambos (renombrar nuevos)",
        "Keep Existing": "Mantener existente",
        "Kinds": "Tipos",
        "Label": "Etiqueta",
        "Live-Log": "Registro en vivo",
        "Load or filter archives first to browse related files here.": "Carga o filtra archivos primero para explorar archivos relacionados aqui.",
        "Loading preview...": "Cargando vista previa...",
        "Local": "Local",
        "Local Approval": "Aprobacion local",
        "Loose File": "Archivo suelto",
        "Loose File Preview": "Vista de archivo suelto",
        "Loose folder": "Carpeta suelta",
        "Loose root": "Raiz suelta",
        "Main Workflow Areas": "Areas principales de flujo",
        "Match Histogram": "Igualar histograma",
        "Match Levels": "Igualar niveles",
        "Match Mean Luma": "Igualar luma media",
        "Match original DDS format": "Igualar formato DDS original",
        "Match original DDS mip count": "Igualar cantidad de mips DDS original",
        "Matched files will be previewed here with highlights.": "Los archivos coincidentes se previsualizaran aqui con resaltado.",
        "Matches": "Coincidencias",
        "Material": "Material",
        "Members": "Miembros",
        "Mip": "Mip",
        "Mips": "Mips",
        "Missing": "Faltante",
        "Model Export": "Exportacion de modelo",
        "Name filter, supports * and ?": "Filtro de nombre, admite * y ?",
        "No archives scanned.": "No se escanearon archivos.",
        "No grouped texture sets are available in the current Research snapshot.": "No hay conjuntos de texturas agrupados en la instantanea actual de Research.",
        "No preview available.": "No hay vista previa disponible.",
        "No preview loaded.": "No hay vista previa cargada.",
        "No row details available.": "No hay detalles de fila disponibles.",
        "No row selected.": "No hay fila seleccionada.",
        "No rows matched the current filter.": "Ninguna fila coincide con el filtro actual.",
        "No source summary has been populated yet. Open this summary after setting workflow roots to verify the run context.": "Aun no hay resumen de origen. Abre este resumen despues de configurar las raices para verificar el contexto.",
        "No text search has been run yet.": "Aun no se ha ejecutado una busqueda de texto.",
        "None": "Ninguno",
        "Normal": "Normal",
        "Notes": "Notas",
        "Off": "Desactivado",
        "Open Setup && Paths": "Abrir Configuracion y Rutas",
        "Open chaiNNer Setup": "Abrir configuracion de chaiNNer",
        "Original": "Original",
        "Overwrite Existing": "Sobrescribir existente",
        "Package": "Paquete",
        "Package filter, for example 0000 or 0015*": "Filtro de paquete, por ejemplo 0000 o 0015*",
        "Packages": "Paquetes",
        "Path": "Ruta",
        "Path filter": "Filtro de ruta",
        "Pause": "Pausar",
        "Play": "Reproducir",
        "Preview Current": "Vista actual",
        "Preview Slot": "Ranura de vista previa",
        "Preview failed:": "Vista previa fallida:",
        "Preview image": "Imagen de vista previa",
        "Preview ready.": "Vista previa lista.",
        "Preparing preview...": "Preparando vista previa...",
        "Profile": "Perfil",
        "Profiles, Rules & Matches": "Perfiles, reglas y coincidencias",
        "Quick Start": "Inicio rapido",
        "Ratio": "Relacion",
        "Ready": "Listo",
        "Ready.": "Listo.",
        "Rebuilt": "Reconstruido",
        "Reason": "Razon",
        "Reference Results": "Resultados de referencia",
        "References": "Referencias",
        "Refresh List": "Actualizar lista",
        "Refresh Research": "Actualizar investigacion",
        "Regex": "Regex",
        "Related": "Relacionado",
        "Related File": "Archivo relacionado",
        "Relation": "Relacion",
        "Research rows in this tab are built from the current Archive Browser view/filter.": "Las filas de Research se crean desde la vista/filtro actual del explorador de archivos.",
        "Reset": "Restablecer",
        "Restore Selected": "Restaurar seleccionado",
        "Review Classifications": "Revisar clasificaciones",
        "Risk": "Riesgo",
        "Role": "Rol",
        "Root": "Raiz",
        "Run Summary": "Resumen de ejecucion",
        "Save": "Guardar",
        "Save Current Role Locally": "Guardar rol actual localmente",
        "Search Log": "Registro de busqueda",
        "Search string or regex, e.g. material or <Texture": "Texto o regex, p. ej. material o <Texture",
        "Search topics, fields, tabs, planner paths, planner profiles...": "Buscar temas, campos, pestanas, rutas y perfiles del planificador...",
        "Select All Shown": "Seleccionar todo visible",
        "Select a DDS file to preview it here.": "Selecciona un DDS para previsualizarlo aqui.",
        "Select a file in Archive Files to preview it here.": "Selecciona un archivo en Archivos para previsualizarlo aqui.",
        "Select a matching file": "Selecciona un archivo coincidente",
        "Select an archive file": "Selecciona un archivo de archivo",
        "Select an unknown DDS file to preview it here.": "Selecciona un DDS desconocido para previsualizarlo aqui.",
        "Select an unknown family member": "Selecciona un miembro de familia desconocida",
        "Selected Preview": "Vista seleccionada",
        "Show the preview at 100% zoom.": "Mostrar la vista al 100% de zoom.",
        "Size mode: the rebuilt DDS uses the final PNG dimensions from PNG root. This changes DDS size only. It does not decide where PNG files are written.": "Modo de tamano: el DDS reconstruido usa las dimensiones del PNG final desde la raiz PNG. Solo cambia el tamano DDS. No decide donde se escriben los PNG.",
        "Source": "Origen",
        "Source Match Balanced (recommended)": "Coincidencia de origen equilibrada (recomendado)",
        "Source Match Experimental": "Coincidencia de origen experimental",
        "Source Match Extended": "Coincidencia de origen extendida",
        "Startup Setup": "Configuracion de inicio",
        "Startup setup guide": "Guia de configuracion inicial",
        "Stop": "Detener",
        "Stored at:": "Guardado en:",
        "Target": "Destino",
        "Target Folder Already Contains Files": "La carpeta de destino ya contiene archivos",
        "Terrain-Like Groups": "Grupos tipo terreno",
        "Text Search": "Busqueda de texto",
        "Texture Analysis": "Analisis de texturas",
        "Texture Policy Preview": "Vista de politica de texturas",
        "Texture Set Grouper": "Agrupador de conjuntos de texturas",
        "Texture Type": "Tipo de textura",
        "Texture-Type Classifier": "Clasificador por tipo de textura",
        "Textures": "Texturas",
        "The selected extraction target already contains files or folders.": "El destino de extraccion seleccionado ya contiene archivos o carpetas.",
        "Topic": "Tema",
        "Total Delta": "Diferencia total",
        "Total Ratio": "Relacion total",
        "Troubleshooting & Limits": "Solucion de problemas y limites",
        "Type": "Tipo",
        "UI Constraints": "Restricciones UI",
        "Unknown": "Desconocido",
        "Updated": "Actualizado",
        "Use Extract Root": "Usar raiz de extraccion",
        "Use In Notes": "Usar en notas",
        "Use In References": "Usar en referencias",
        "Use Original DDS Root": "Usar raiz DDS original",
        "Use final PNG size for rebuilt DDS": "Usar tamano final PNG para DDS reconstruido",
        "Warnings": "Advertencias",
        "What This App Covers": "Que cubre esta app",
        "Wrap": "Ajustar lineas",
        "Write Mod-Ready Loose File": "Escribir archivo suelto listo para mod",
        "Zoom in.": "Acercar.",
        "Zoom out.": "Alejar.",
        "comma,separated,tags": "etiquetas,separadas,por,coma",
        "file": "archivo",
    },
    "de": {
        "Archive Browser Performance": "Archiv-Browser-Leistung",
        "Archive Locations": "Archiv-Orte",
        "Game And Extraction Paths": "Spiel- und Extraktionspfade",
        "Game / Package": "Spiel / Paket",
        "Open Archive Locations": "Archiv-Orte oeffnen",
        "Highlight intensity": "Hervorhebungsstaerke",
        "Log color scheme": "Farbschema fuer Logs",
        "Preview/details color scheme": "Farbschema fuer Vorschau/Details",
        "In-game Name": "Ingame-Name",
        "Follow app theme": "App-Theme folgen",
        "VS Code classic": "VS-Code-Klassik",
        "Terminal contrast": "Terminal-Kontrast",
        "Accessible contrast": "Barrierearmer Kontrast",
        "Solarized": "Solarized",
        "3D Preview Settings": "3D-Vorschau-Einstellungen",
        "Aa": "Aa",
        "Add Rule": "Regel hinzufuegen",
        "Affected": "Betroffen",
        "All textures (advanced)": "Alle Texturen (erweitert)",
        "Allow archive patching": "Archiv-Patching erlauben",
        "Also show already classified DDS families": "Bereits klassifizierte DDS-Familien auch anzeigen",
        "Analysis": "Analyse",
        "Archive Extraction Target": "Archiv-Extraktionsziel",
        "Archive Files": "Archivdateien",
        "Archive Insights": "Archiv-Einblicke",
        "Archive Preview": "Archivvorschau",
        "Archive files": "Archivdateien",
        "Archive scan": "Archivscan",
        "Archive tree": "Archivbaum",
        "Archives": "Archive",
        "Assign": "Zuweisen",
        "Assign Profile": "Profil zuweisen",
        "Audio Export Complete": "Audioexport abgeschlossen",
        "Avg Ratio": "Durchschn. Verhaeltnis",
        "Avg Risk": "Durchschn. Risiko",
        "Balanced mixed textures (recommended)": "Ausgewogene gemischte Texturen (empfohlen)",
        "Band": "Band",
        "Base": "Basis",
        "Browse...": "Durchsuchen...",
        "Byte Delta": "Byte-Differenz",
        "Cancel": "Abbrechen",
        "Case sensitive": "Gross-/Kleinschreibung",
        "Choose a documentation topic or search by feature name.": "Waehle ein Dokumentationsthema oder suche nach Funktionsnamen.",
        "Classification": "Klassifizierung",
        "Classification Review": "Klassifizierungspruefung",
        "Class Risk": "Klassenrisiko",
        "Clear": "Leeren",
        "Clear Current Family": "Aktuelle Familie leeren",
        "Clear Current File": "Aktuelle Datei leeren",
        "Clear Folders": "Ordner leeren",
        "Clear Log": "Log leeren",
        "Clear Root": "Stamm leeren",
        "Clear Selection": "Auswahl leeren",
        "Clear Selected Families": "Ausgewaehlte Familien leeren",
        "Close": "Schliessen",
        "Color + UI + emissive": "Farbe + UI + Emissiv",
        "Color + UI only (safer)": "Nur Farbe + UI (sicherer)",
        "Column": "Spalte",
        "Common Failure Causes": "Haeufige Fehlerursachen",
        "Compare and Review": "Vergleichen und pruefen",
        "Confidence": "Vertrauen",
        "Configured sources are present. Use this summary to verify paths and policy before starting a batch run.": "Die konfigurierten Quellen sind vorhanden. Verwende diese Zusammenfassung, um Pfade und Richtlinien vor einem Stapellauf zu pruefen.",
        "Continue": "Fortfahren",
        "Continue Anyway": "Trotzdem fortfahren",
        "Controls": "Steuerung",
        "Copy": "Kopieren",
        "Create source PNGs from DDS before processing": "Quell-PNGs vor der Verarbeitung aus DDS erstellen",
        "Current": "Aktuell",
        "DDS Extraction Target": "DDS-Extraktionsziel",
        "DDS Files Need Saved Classification": "DDS-Dateien brauchen gespeicherte Klassifizierung",
        "DDS Import Preview:": "DDS-Importvorschau:",
        "Default": "Standard",
        "Delta": "Differenz",
        "Detailed notes, evidence, and policy reasons will appear here.": "Detaillierte Notizen, Belege und Richtliniengruende erscheinen hier.",
        "Disabled": "Deaktiviert",
        "Documentation": "Dokumentation",
        "Done": "Fertig",
        "Export Complete": "Export abgeschlossen",
        "Export Language File": "Sprachdatei exportieren",
        "Export Results": "Ergebnisse exportieren",
        "Export Selected": "Auswahl exportieren",
        "Exported file:": "Exportierte Datei:",
        "Ext": "Ext",
        "Extensions": "Erweiterungen",
        "Extract Root": "Extraktionsstamm",
        "Extract Selected Set": "Ausgewaehltes Set extrahieren",
        "Failed": "Fehlgeschlagen",
        "File": "Datei",
        "File Name": "Dateiname",
        "Files": "Dateien",
        "Find in preview": "In Vorschau suchen",
        "Folder": "Ordner",
        "General": "Allgemein",
        "Group": "Gruppe",
        "Groups": "Gruppen",
        "Heatmap": "Heatmap",
        "Height": "Hoehe",
        "Idle": "Leerlauf",
        "Import Language File": "Sprachdatei importieren",
        "Imported language:": "Importierte Sprache:",
        "Include": "Einschliessen",
        "Issues": "Probleme",
        "Keep Both (Rename New Files)": "Beide behalten (neue Dateien umbenennen)",
        "Keep Existing": "Bestehendes behalten",
        "Kinds": "Arten",
        "Label": "Label",
        "Live-Log": "Live-Log",
        "Load or filter archives first to browse related files here.": "Lade oder filtere zuerst Archive, um verwandte Dateien hier zu durchsuchen.",
        "Loading preview...": "Vorschau wird geladen...",
        "Local": "Lokal",
        "Local Approval": "Lokale Freigabe",
        "Loose File": "Lose Datei",
        "Loose File Preview": "Lose-Datei-Vorschau",
        "Loose folder": "Loser Ordner",
        "Loose root": "Loser Stamm",
        "Main Workflow Areas": "Hauptbereiche des Workflows",
        "Match Histogram": "Histogramm anpassen",
        "Match Levels": "Levels anpassen",
        "Match Mean Luma": "Mittlere Luma anpassen",
        "Match original DDS format": "Originales DDS-Format verwenden",
        "Match original DDS mip count": "Originale DDS-Mip-Anzahl verwenden",
        "Matched files will be previewed here with highlights.": "Gefundene Dateien werden hier mit Hervorhebungen angezeigt.",
        "Matches": "Treffer",
        "Material": "Material",
        "Members": "Mitglieder",
        "Mip": "Mip",
        "Mips": "Mips",
        "Missing": "Fehlt",
        "Model Export": "Modellexport",
        "Name filter, supports * and ?": "Namensfilter, unterstuetzt * und ?",
        "No archives scanned.": "Keine Archive gescannt.",
        "No grouped texture sets are available in the current Research snapshot.": "Keine gruppierten Textursets im aktuellen Research-Snapshot verfuegbar.",
        "No preview available.": "Keine Vorschau verfuegbar.",
        "No preview loaded.": "Keine Vorschau geladen.",
        "No row details available.": "Keine Zeilendetails verfuegbar.",
        "No row selected.": "Keine Zeile ausgewaehlt.",
        "No rows matched the current filter.": "Keine Zeilen passen zum aktuellen Filter.",
        "No source summary has been populated yet. Open this summary after setting workflow roots to verify the run context.": "Noch keine Quellenzusammenfassung vorhanden. Oeffne sie nach dem Setzen der Workflow-Staemme, um den Kontext zu pruefen.",
        "No text search has been run yet.": "Es wurde noch keine Textsuche ausgefuehrt.",
        "None": "Keine",
        "Normal": "Normal",
        "Notes": "Notizen",
        "Off": "Aus",
        "Open Setup && Paths": "Einrichtung und Pfade oeffnen",
        "Open chaiNNer Setup": "chaiNNer-Einrichtung oeffnen",
        "Original": "Original",
        "Overwrite Existing": "Bestehendes ueberschreiben",
        "Package": "Paket",
        "Package filter, for example 0000 or 0015*": "Paketfilter, z. B. 0000 oder 0015*",
        "Packages": "Pakete",
        "Path": "Pfad",
        "Path filter": "Pfadfilter",
        "Pause": "Pause",
        "Play": "Abspielen",
        "Preview Current": "Aktuelle Vorschau",
        "Preview Slot": "Vorschau-Slot",
        "Preview failed:": "Vorschau fehlgeschlagen:",
        "Preview image": "Vorschaubild",
        "Preview ready.": "Vorschau bereit.",
        "Preparing preview...": "Vorschau wird vorbereitet...",
        "Profile": "Profil",
        "Profiles, Rules & Matches": "Profile, Regeln und Treffer",
        "Quick Start": "Schnellstart",
        "Ratio": "Verhaeltnis",
        "Ready": "Bereit",
        "Ready.": "Bereit.",
        "Rebuilt": "Neu erstellt",
        "Reason": "Grund",
        "Reference Results": "Referenzergebnisse",
        "References": "Referenzen",
        "Refresh List": "Liste aktualisieren",
        "Refresh Research": "Research aktualisieren",
        "Regex": "Regex",
        "Related": "Verwandt",
        "Related File": "Verwandte Datei",
        "Relation": "Beziehung",
        "Research rows in this tab are built from the current Archive Browser view/filter.": "Research-Zeilen in diesem Tab werden aus der aktuellen Archiv-Browser-Ansicht/dem Filter erstellt.",
        "Reset": "Zuruecksetzen",
        "Restore Selected": "Auswahl wiederherstellen",
        "Review Classifications": "Klassifizierungen pruefen",
        "Risk": "Risiko",
        "Role": "Rolle",
        "Root": "Stamm",
        "Run Summary": "Ausfuehrungszusammenfassung",
        "Save": "Speichern",
        "Save Current Role Locally": "Aktuelle Rolle lokal speichern",
        "Search Log": "Such-Log",
        "Search string or regex, e.g. material or <Texture": "Suchtext oder Regex, z. B. material oder <Texture",
        "Search topics, fields, tabs, planner paths, planner profiles...": "Themen, Felder, Tabs, Planerpfade und Planerprofile suchen...",
        "Select All Shown": "Alle sichtbaren auswaehlen",
        "Select a DDS file to preview it here.": "Waehle eine DDS-Datei fuer die Vorschau.",
        "Select a file in Archive Files to preview it here.": "Waehle eine Datei in Archivdateien fuer die Vorschau.",
        "Select a matching file": "Waehle eine passende Datei",
        "Select an archive file": "Waehle eine Archivdatei",
        "Select an unknown DDS file to preview it here.": "Waehle eine unbekannte DDS-Datei fuer die Vorschau.",
        "Select an unknown family member": "Waehle ein unbekanntes Familienmitglied",
        "Selected Preview": "Ausgewaehlte Vorschau",
        "Show the preview at 100% zoom.": "Vorschau mit 100% Zoom anzeigen.",
        "Size mode: the rebuilt DDS uses the final PNG dimensions from PNG root. This changes DDS size only. It does not decide where PNG files are written.": "Groessenmodus: Die neu erstellte DDS nutzt die finalen PNG-Abmessungen aus dem PNG-Stamm. Das aendert nur die DDS-Groesse und nicht, wo PNG-Dateien geschrieben werden.",
        "Source": "Quelle",
        "Source Match Balanced (recommended)": "Quellabgleich ausgewogen (empfohlen)",
        "Source Match Experimental": "Quellabgleich experimentell",
        "Source Match Extended": "Quellabgleich erweitert",
        "Startup Setup": "Starteinrichtung",
        "Startup setup guide": "Starteinrichtungs-Anleitung",
        "Stop": "Stopp",
        "Stored at:": "Gespeichert unter:",
        "Target": "Ziel",
        "Target Folder Already Contains Files": "Zielordner enthaelt bereits Dateien",
        "Terrain-Like Groups": "Gelaendeartige Gruppen",
        "Text Search": "Textsuche",
        "Texture Analysis": "Texturanalyse",
        "Texture Policy Preview": "Textur-Richtlinienvorschau",
        "Texture Set Grouper": "Texturset-Gruppierer",
        "Texture Type": "Texturtyp",
        "Texture-Type Classifier": "Texturtyp-Klassifizierer",
        "Textures": "Texturen",
        "The selected extraction target already contains files or folders.": "Das ausgewaehlte Extraktionsziel enthaelt bereits Dateien oder Ordner.",
        "Topic": "Thema",
        "Total Delta": "Gesamtdifferenz",
        "Total Ratio": "Gesamtverhaeltnis",
        "Troubleshooting & Limits": "Fehlerbehebung und Grenzen",
        "Type": "Typ",
        "UI Constraints": "UI-Beschraenkungen",
        "Unknown": "Unbekannt",
        "Updated": "Aktualisiert",
        "Use Extract Root": "Extraktionsstamm verwenden",
        "Use In Notes": "In Notizen verwenden",
        "Use In References": "In Referenzen verwenden",
        "Use Original DDS Root": "Original-DDS-Stamm verwenden",
        "Use final PNG size for rebuilt DDS": "Finale PNG-Groesse fuer neu erstellte DDS verwenden",
        "Warnings": "Warnungen",
        "What This App Covers": "Was diese App abdeckt",
        "Wrap": "Zeilenumbruch",
        "Write Mod-Ready Loose File": "Mod-fertige lose Datei schreiben",
        "Zoom in.": "Hineinzoomen.",
        "Zoom out.": "Herauszoomen.",
        "comma,separated,tags": "komma,getrennte,tags",
        "file": "Datei",
    },
}

for _language_code, _translations in _EXTRA_BUILTIN_TRANSLATIONS.items():
    _payload = BUILTIN_LANGUAGES.get(_language_code)
    if isinstance(_payload, dict) and isinstance(_payload.get("translations"), dict):
        _payload["translations"].update(_translations)

_ADDITIONAL_BUILTIN_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "Accepted current Research role for file": "Rol actual de Research aceptado para el archivo",
        "Action summary:": "Resumen de accion:",
        "Action:": "Accion:",
        "Actionable findings:": "Hallazgos accionables:",
        "Actions": "Acciones",
        "Actual size (100%)": "Tamano real (100%)",
        "Add Adjustment": "Agregar ajuste",
        "Add Files": "Agregar archivos",
        "Add Folder": "Agregar carpeta",
        "Add Layer": "Agregar capa",
        "Add Layer Mask": "Agregar mascara de capa",
        "Add Mask": "Agregar mascara",
        "Add a layer mask before switching the editor into mask paint mode.": "Agrega una mascara de capa antes de cambiar el editor al modo de pintura de mascara.",
        "Add edited PNG or DDS files before building a mod package.": "Agrega archivos PNG o DDS editados antes de crear un paquete mod.",
        "Add freeform notes, discoveries, unresolved questions, or file relationships here.": "Agrega aqui notas libres, descubrimientos, preguntas sin resolver o relaciones entre archivos.",
        "Adjustments": "Ajustes",
        "Affected textures:": "Texturas afectadas:",
        "All": "Todo",
        "All Files (*)": "Todos los archivos (*)",
        "All files": "Todos los archivos",
        "All imported files are matched.": "Todos los archivos importados tienen coincidencia.",
        "All packages": "Todos los paquetes",
        "All requested folders already existed.": "Todas las carpetas solicitadas ya existian.",
        "All roles": "Todos los roles",
        "Alpha": "Alfa",
        "Alpha Only": "Solo alfa",
        "Alpha mode:": "Modo alfa:",
        "Alpha policy:": "Politica alfa:",
        "Already Contains Files": "Ya contiene archivos",
        "Ambient Occlusion": "Oclusion ambiental",
        "Ambient strength": "Intensidad ambiental",
        "Animation / Motion": "Animacion / movimiento",
        "Another background task is still running. Wait for it to finish before starting this action.": "Otra tarea en segundo plano sigue ejecutandose. Espera a que termine antes de iniciar esta accion.",
        "Apply": "Aplicar",
        "Apply Filters": "Aplicar filtros",
        "Apply Filters*": "Aplicar filtros*",
        "Apply Guides": "Aplicar guias",
        "Apply Recolor To Active Layer": "Aplicar recolor a la capa activa",
        "Apply To Current Family": "Aplicar a la familia actual",
        "Apply To Current File": "Aplicar al archivo actual",
        "Apply To Selected Families": "Aplicar a familias seleccionadas",
        "Apply To Unknown Files In Current Family": "Aplicar a archivos desconocidos en la familia actual",
        "Apply To Unknown Files In Selected Families": "Aplicar a archivos desconocidos en familias seleccionadas",
        "Apply correction and rebuild logic with current output settings.": "Aplicar correccion y reconstruccion con la configuracion de salida actual.",
        "Apply one local DDS onto the current archive mesh preview as a temporary import preview without patching the game files.": "Aplicar un DDS local a la vista de malla actual como vista temporal de importacion sin parchear archivos del juego.",
        "Archive Browser is busy. Wait for the current task to finish, then try again.": "El explorador de archivos esta ocupado. Espera a que termine la tarea actual e intentalo de nuevo.",
        "Archive Controls": "Controles de archivo",
        "Archive Path": "Ruta de archivo",
        "Archive Scan Log": "Registro de escaneo de archivos",
        "Archive browser task failed:": "La tarea del explorador de archivos fallo:",
        "Archive cache": "Cache de archivos",
        "Archive cache is not loaded. Load archives first if you want automatic DDS lookup.": "La cache de archivos no esta cargada. Carga archivos primero si quieres busqueda DDS automatica.",
        "Archive cache ready:": "Cache de archivos lista:",
        "Archive extract root:": "Raiz de extraccion de archivos:",
        "Archive extraction cancelled.": "Extraccion de archivos cancelada.",
        "Archive package root auto-detect cancelled.": "Autodeteccion de raiz de paquetes cancelada.",
        "Archive preview error": "Error de vista de archivo",
        "Archive preview failed:": "Vista de archivo fallida:",
        "Archive root": "Raiz de archivos",
        "Archive scan complete. Found": "Escaneo de archivos completado. Encontrados",
        "Archive scan log cleared.": "Registro de escaneo limpiado.",
        "Archive source ready:": "Fuente de archivos lista:",
        "Archive-Side Sidecar Discovery": "Descubrimiento de sidecars del archivo",
        "Assign Workflow Profile": "Asignar perfil de flujo",
        "Atlas": "Atlas",
        "Audio Patch Complete": "Parche de audio completado",
        "Author": "Autor",
        "Authoring notes": "Notas de autoria",
        "Auto-Match": "Auto-coincidir",
        "Auto-configured": "Auto-configurado",
        "Auto-detect": "Autodetectar",
        "Auto-detected archive package root:": "Raiz de paquetes detectada:",
        "Auto-match complete. Click the item to refresh preview.": "Auto-coincidencia completada. Haz clic en el elemento para refrescar la vista.",
        "Auto-matching edited files...": "Auto-coincidiendo archivos editados...",
        "Auto-preview was skipped because the result set is very large.": "La vista previa automatica se omitio porque el conjunto de resultados es muy grande.",
        "Automatic color and format rules are disabled, so format and color mistakes are more likely.": "Las reglas automaticas de color y formato estan desactivadas, por lo que son mas probables errores de formato y color.",
        "Automatic color and format rules are enabled for DDS rebuild.": "Las reglas automaticas de color y formato estan activadas para reconstruccion DDS.",
        "Average dimensions:": "Dimensiones medias:",
        "Average ratio:": "Relacion media:",
        "Average risk:": "Riesgo medio:",
        "Backend Choice": "Eleccion de backend",
        "Backend behavior": "Comportamiento del backend",
        "Backend compatibility:": "Compatibilidad de backend:",
        "Backend execution:": "Ejecucion de backend:",
        "Backend reason:": "Razon del backend:",
        "Backend:": "Backend:",
        "Background worker error": "Error de tarea en segundo plano",
        "Backup Restored": "Copia restaurada",
        "Backup:": "Copia:",
        "Base / likely albedo images": "Base / posibles imagenes albedo",
        "Base Override": "Sustitucion de base",
        "Base:": "Base:",
        "Best for:": "Mejor para:",
        "Binary Header Preview": "Vista de cabecera binaria",
        "Binary header preview:": "Vista de cabecera binaria:",
        "Black": "Negro",
        "Blacks": "Negros",
        "Blend mode": "Modo de mezcla",
        "Blue": "Azul",
        "Blue / Yellow": "Azul / amarillo",
        "Both axes": "Ambos ejes",
        "Brightness": "Brillo",
        "Brightness / Contrast": "Brillo / contraste",
        "Brightness drift": "Desviacion de brillo",
        "Brightness or detail drift": "Desviacion de brillo o detalle",
        "Browse Archive": "Explorar archivo",
        "Brush larger": "Pincel mas grande",
        "Brush preset name": "Nombre de preajuste de pincel",
        "Brush size": "Tamano de pincel",
        "Brush smaller": "Pincel mas pequeno",
        "Brush tip": "Punta de pincel",
        "Budget Analysis": "Analisis de presupuesto",
        "Budget class summary": "Resumen de presupuesto por clase",
        "Budget file details": "Detalles de presupuesto por archivo",
        "Budget profile summary": "Resumen de presupuesto por perfil",
        "Build Package": "Crear paquete",
        "Build Settings": "Configuracion de creacion",
        "Build cancelled before start.": "Creacion cancelada antes de iniciar.",
        "Build completed successfully.": "Creacion completada correctamente.",
        "Build completed.": "Creacion completada.",
        "Build mode": "Modo de creacion",
        "Building archive research snapshot...": "Creando instantanea de investigacion de archivos...",
        "Building budget and residency risk analysis...": "Creando analisis de presupuesto y riesgo de residencia...",
        "Building per-texture policy preview...": "Creando vista de politica por textura...",
        "Building replace package...": "Creando paquete de reemplazo...",
        "Building texture policy preview...": "Creando vista de politica de texturas...",
        "Built DDS": "DDS creado",
        "Built Items": "Elementos creados",
        "Bulk Normal Validator": "Validador masivo de normales",
        "Burn Highlights": "Quemar luces",
        "Burn Midtones": "Quemar medios tonos",
        "Burn Shadows": "Quemar sombras",
        "Byte delta:": "Diferencia de bytes:",
        "Byte ratio:": "Relacion de bytes:",
        "CSV log saved to": "Registro CSV guardado en",
        "Cancel Floating Selection": "Cancelar seleccion flotante",
        "Cancelled": "Cancelado",
        "Canvas Size": "Tamano de lienzo",
        "Canvas Size...": "Tamano de lienzo...",
        "Case-sensitive preview search": "Busqueda de vista con mayusculas",
        "Catalog": "Catalogo",
        "Category": "Categoria",
        "Category:": "Categoria:",
        "Cavity clamp max": "Maximo de cavidad",
        "Cavity clamp min": "Minimo de cavidad",
        "Center": "Centro",
        "Chain file not found:": "Archivo de cadena no encontrado:",
        "Chain inspection": "Inspeccion de cadena",
        "Changed": "Cambiado",
        "Changed textures:": "Texturas cambiadas:",
        "Channel": "Canal",
        "Channel Data": "Datos de canal",
        "Channels": "Canales",
        "Choose": "Elegir",
        "Choose Archive Original": "Elegir original del archivo",
        "Choose Local Original": "Elegir original local",
        "Choose NCNN model folder": "Elegir carpeta de modelos NCNN",
        "Choose Now": "Elegir ahora",
        "Choose Original DDS": "Elegir DDS original",
        "Choose Real-ESRGAN NCNN executable": "Elegir ejecutable Real-ESRGAN NCNN",
        "Choose color": "Elegir color",
        "Choose original DDS": "Elegir DDS original",
        "Choose replace package parent root": "Elegir raiz padre del paquete de reemplazo",
        "Choose where to extract these DDS files.": "Elige donde extraer estos DDS.",
        "Classified": "Clasificado",
        "Clear Adjustment Mask": "Limpiar mascara de ajuste",
        "Clear All": "Limpiar todo",
        "Clear Folder": "Limpiar carpeta",
        "Clear Guides": "Limpiar guias",
        "Clear History": "Limpiar historial",
        "Clear Mask": "Limpiar mascara",
        "Clear Source": "Limpiar origen",
        "Clear Workflow Roots": "Limpiar raices de flujo",
        "Clear Workflow Roots...": "Limpiar raices de flujo...",
        "Clear selection": "Limpiar seleccion",
        "Click": "Clic",
        "Clone": "Clonar",
        "Clone / Heal": "Clonar / curar",
        "Clone tool": "Herramienta de clonado",
        "CodePreviewEditor": "Editor de vista de codigo",
        "Color": "Color",
        "Color Balance": "Balance de color",
        "Columns": "Columnas",
        "Comfortable": "Comodo",
        "Commit": "Confirmar",
        "Commit Floating Selection": "Confirmar seleccion flotante",
        "Common failure cases and current limitations.": "Casos comunes de fallo y limitaciones actuales.",
        "Compact": "Compacto",
        "Compare & Review": "Comparar y revisar",
        "Comparison": "Comparacion",
        "Completed": "Completado",
        "Compression:": "Compresion:",
        "Config file": "Archivo de configuracion",
        "Configure": "Configurar",
        "Constraint": "Restriccion",
        "Context:": "Contexto:",
        "Continue anyway?": "Continuar de todos modos?",
        "Continue with the import?": "Continuar con la importacion?",
        "Contrast": "Contraste",
        "Converted PNG folder:": "Carpeta PNG convertida:",
    },
    "de": {
        "Accepted current Research role for file": "Aktuelle Research-Rolle fuer Datei akzeptiert",
        "Action summary:": "Aktionszusammenfassung:",
        "Action:": "Aktion:",
        "Actionable findings:": "Umsetzbare Befunde:",
        "Actions": "Aktionen",
        "Actual size (100%)": "Originalgroesse (100%)",
        "Add Adjustment": "Anpassung hinzufuegen",
        "Add Files": "Dateien hinzufuegen",
        "Add Folder": "Ordner hinzufuegen",
        "Add Layer": "Ebene hinzufuegen",
        "Add Layer Mask": "Ebenenmaske hinzufuegen",
        "Add Mask": "Maske hinzufuegen",
        "Add a layer mask before switching the editor into mask paint mode.": "Fuege eine Ebenenmaske hinzu, bevor der Editor in den Maskenmalmodus wechselt.",
        "Add edited PNG or DDS files before building a mod package.": "Fuege bearbeitete PNG- oder DDS-Dateien hinzu, bevor ein Mod-Paket erstellt wird.",
        "Add freeform notes, discoveries, unresolved questions, or file relationships here.": "Fuege hier freie Notizen, Entdeckungen, offene Fragen oder Dateibeziehungen hinzu.",
        "Adjustments": "Anpassungen",
        "Affected textures:": "Betroffene Texturen:",
        "All": "Alle",
        "All Files (*)": "Alle Dateien (*)",
        "All files": "Alle Dateien",
        "All imported files are matched.": "Alle importierten Dateien sind zugeordnet.",
        "All packages": "Alle Pakete",
        "All requested folders already existed.": "Alle angeforderten Ordner waren bereits vorhanden.",
        "All roles": "Alle Rollen",
        "Alpha": "Alpha",
        "Alpha Only": "Nur Alpha",
        "Alpha mode:": "Alpha-Modus:",
        "Alpha policy:": "Alpha-Richtlinie:",
        "Already Contains Files": "Enthaelt bereits Dateien",
        "Ambient Occlusion": "Ambient Occlusion",
        "Ambient strength": "Umgebungsstaerke",
        "Animation / Motion": "Animation / Bewegung",
        "Another background task is still running. Wait for it to finish before starting this action.": "Eine andere Hintergrundaufgabe laeuft noch. Warte, bis sie beendet ist, bevor du diese Aktion startest.",
        "Apply": "Anwenden",
        "Apply Filters": "Filter anwenden",
        "Apply Filters*": "Filter anwenden*",
        "Apply Guides": "Hilfslinien anwenden",
        "Apply Recolor To Active Layer": "Umfaerbung auf aktive Ebene anwenden",
        "Apply To Current Family": "Auf aktuelle Familie anwenden",
        "Apply To Current File": "Auf aktuelle Datei anwenden",
        "Apply To Selected Families": "Auf ausgewaehlte Familien anwenden",
        "Apply To Unknown Files In Current Family": "Auf unbekannte Dateien in aktueller Familie anwenden",
        "Apply To Unknown Files In Selected Families": "Auf unbekannte Dateien in ausgewaehlten Familien anwenden",
        "Apply correction and rebuild logic with current output settings.": "Korrektur- und Neuaufbau-Logik mit aktuellen Ausgabeeinstellungen anwenden.",
        "Apply one local DDS onto the current archive mesh preview as a temporary import preview without patching the game files.": "Eine lokale DDS temporaer auf die aktuelle Archiv-Mesh-Vorschau anwenden, ohne Spieldateien zu patchen.",
        "Archive Browser is busy. Wait for the current task to finish, then try again.": "Der Archiv-Browser ist beschaeftigt. Warte auf das Ende der aktuellen Aufgabe und versuche es erneut.",
        "Archive Controls": "Archivsteuerung",
        "Archive Path": "Archivpfad",
        "Archive Scan Log": "Archivscan-Log",
        "Archive browser task failed:": "Archiv-Browser-Aufgabe fehlgeschlagen:",
        "Archive cache": "Archiv-Cache",
        "Archive cache is not loaded. Load archives first if you want automatic DDS lookup.": "Der Archiv-Cache ist nicht geladen. Lade zuerst Archive, wenn du automatische DDS-Suche moechtest.",
        "Archive cache ready:": "Archiv-Cache bereit:",
        "Archive extract root:": "Archiv-Extraktionsstamm:",
        "Archive extraction cancelled.": "Archivextraktion abgebrochen.",
        "Archive package root auto-detect cancelled.": "Automatische Paketsuche abgebrochen.",
        "Archive preview error": "Archivvorschau-Fehler",
        "Archive preview failed:": "Archivvorschau fehlgeschlagen:",
        "Archive root": "Archivstamm",
        "Archive scan complete. Found": "Archivscan abgeschlossen. Gefunden",
        "Archive scan log cleared.": "Archivscan-Log geleert.",
        "Archive source ready:": "Archivquelle bereit:",
        "Archive-Side Sidecar Discovery": "Archivseitige Sidecar-Erkennung",
        "Assign Workflow Profile": "Workflow-Profil zuweisen",
        "Atlas": "Atlas",
        "Audio Patch Complete": "Audio-Patch abgeschlossen",
        "Author": "Autor",
        "Authoring notes": "Autoren-Notizen",
        "Auto-Match": "Automatisch zuordnen",
        "Auto-configured": "Automatisch konfiguriert",
        "Auto-detect": "Automatisch erkennen",
        "Auto-detected archive package root:": "Archivpaket-Stamm automatisch erkannt:",
        "Auto-match complete. Click the item to refresh preview.": "Automatische Zuordnung abgeschlossen. Klicke das Element, um die Vorschau zu aktualisieren.",
        "Auto-matching edited files...": "Bearbeitete Dateien werden automatisch zugeordnet...",
        "Auto-preview was skipped because the result set is very large.": "Automatische Vorschau wurde uebersprungen, weil die Ergebnisliste sehr gross ist.",
        "Automatic color and format rules are disabled, so format and color mistakes are more likely.": "Automatische Farb- und Formatregeln sind deaktiviert; Format- und Farbfehler sind wahrscheinlicher.",
        "Automatic color and format rules are enabled for DDS rebuild.": "Automatische Farb- und Formatregeln sind fuer DDS-Neuaufbau aktiv.",
        "Average dimensions:": "Durchschnittliche Abmessungen:",
        "Average ratio:": "Durchschnittliches Verhaeltnis:",
        "Average risk:": "Durchschnittliches Risiko:",
        "Backend Choice": "Backend-Auswahl",
        "Backend behavior": "Backend-Verhalten",
        "Backend compatibility:": "Backend-Kompatibilitaet:",
        "Backend execution:": "Backend-Ausfuehrung:",
        "Backend reason:": "Backend-Grund:",
        "Backend:": "Backend:",
        "Background worker error": "Hintergrundworker-Fehler",
        "Backup Restored": "Backup wiederhergestellt",
        "Backup:": "Backup:",
        "Base / likely albedo images": "Basis / wahrscheinliche Albedo-Bilder",
        "Base Override": "Basis-Ueberschreibung",
        "Base:": "Basis:",
        "Best for:": "Geeignet fuer:",
        "Binary Header Preview": "Binaerheader-Vorschau",
        "Binary header preview:": "Binaerheader-Vorschau:",
        "Black": "Schwarz",
        "Blacks": "Schwarzwerte",
        "Blend mode": "Mischmodus",
        "Blue": "Blau",
        "Blue / Yellow": "Blau / Gelb",
        "Both axes": "Beide Achsen",
        "Brightness": "Helligkeit",
        "Brightness / Contrast": "Helligkeit / Kontrast",
        "Brightness drift": "Helligkeitsdrift",
        "Brightness or detail drift": "Helligkeits- oder Detaildrift",
        "Browse Archive": "Archiv durchsuchen",
        "Brush larger": "Pinsel groesser",
        "Brush preset name": "Pinselprofilname",
        "Brush size": "Pinselgroesse",
        "Brush smaller": "Pinsel kleiner",
        "Brush tip": "Pinselspitze",
        "Budget Analysis": "Budgetanalyse",
        "Budget class summary": "Budget-Klassenzusammenfassung",
        "Budget file details": "Budget-Dateidetails",
        "Budget profile summary": "Budget-Profilzusammenfassung",
        "Build Package": "Paket erstellen",
        "Build Settings": "Build-Einstellungen",
        "Build cancelled before start.": "Build vor Start abgebrochen.",
        "Build completed successfully.": "Build erfolgreich abgeschlossen.",
        "Build completed.": "Build abgeschlossen.",
        "Build mode": "Build-Modus",
        "Building archive research snapshot...": "Archiv-Research-Snapshot wird erstellt...",
        "Building budget and residency risk analysis...": "Budget- und Residenzrisikoanalyse wird erstellt...",
        "Building per-texture policy preview...": "Pro-Textur-Richtlinienvorschau wird erstellt...",
        "Building replace package...": "Ersatzpaket wird erstellt...",
        "Building texture policy preview...": "Textur-Richtlinienvorschau wird erstellt...",
        "Built DDS": "DDS erstellt",
        "Built Items": "Erstellte Elemente",
        "Bulk Normal Validator": "Massen-Normalenvalidator",
        "Burn Highlights": "Lichter nachbelichten",
        "Burn Midtones": "Mitteltone nachbelichten",
        "Burn Shadows": "Schatten nachbelichten",
        "Byte delta:": "Byte-Differenz:",
        "Byte ratio:": "Byte-Verhaeltnis:",
        "CSV log saved to": "CSV-Log gespeichert unter",
        "Cancel Floating Selection": "Schwebende Auswahl abbrechen",
        "Cancelled": "Abgebrochen",
        "Canvas Size": "Leinwandgroesse",
        "Canvas Size...": "Leinwandgroesse...",
        "Case-sensitive preview search": "Gross-/Kleinschreibung in Vorschau",
        "Catalog": "Katalog",
        "Category": "Kategorie",
        "Category:": "Kategorie:",
        "Cavity clamp max": "Cavity-Maximum",
        "Cavity clamp min": "Cavity-Minimum",
        "Center": "Mitte",
        "Chain file not found:": "Chain-Datei nicht gefunden:",
        "Chain inspection": "Chain-Pruefung",
        "Changed": "Geaendert",
        "Changed textures:": "Geaenderte Texturen:",
        "Channel": "Kanal",
        "Channel Data": "Kanaldaten",
        "Channels": "Kanaele",
        "Choose": "Waehlen",
        "Choose Archive Original": "Archiv-Original waehlen",
        "Choose Local Original": "Lokales Original waehlen",
        "Choose NCNN model folder": "NCNN-Modellordner waehlen",
        "Choose Now": "Jetzt waehlen",
        "Choose Original DDS": "Original-DDS waehlen",
        "Choose Real-ESRGAN NCNN executable": "Real-ESRGAN-NCNN-Programm waehlen",
        "Choose color": "Farbe waehlen",
        "Choose original DDS": "Original-DDS waehlen",
        "Choose replace package parent root": "Elternstamm fuer Ersatzpaket waehlen",
        "Choose where to extract these DDS files.": "Waehle, wohin diese DDS-Dateien extrahiert werden.",
        "Classified": "Klassifiziert",
        "Clear Adjustment Mask": "Anpassungsmaske leeren",
        "Clear All": "Alles leeren",
        "Clear Folder": "Ordner leeren",
        "Clear Guides": "Hilfslinien leeren",
        "Clear History": "Verlauf leeren",
        "Clear Mask": "Maske leeren",
        "Clear Source": "Quelle leeren",
        "Clear Workflow Roots": "Workflow-Staemme leeren",
        "Clear Workflow Roots...": "Workflow-Staemme leeren...",
        "Clear selection": "Auswahl leeren",
        "Click": "Klick",
        "Clone": "Klonen",
        "Clone / Heal": "Klonen / Heilen",
        "Clone tool": "Klonwerkzeug",
        "CodePreviewEditor": "Code-Vorschaueditor",
        "Color": "Farbe",
        "Color Balance": "Farbbalance",
        "Columns": "Spalten",
        "Comfortable": "Komfortabel",
        "Commit": "Uebernehmen",
        "Commit Floating Selection": "Schwebende Auswahl uebernehmen",
        "Common failure cases and current limitations.": "Haeufige Fehlerfaelle und aktuelle Grenzen.",
        "Compact": "Kompakt",
        "Compare & Review": "Vergleichen und pruefen",
        "Comparison": "Vergleich",
        "Completed": "Abgeschlossen",
        "Compression:": "Kompression:",
        "Config file": "Konfigurationsdatei",
        "Configure": "Konfigurieren",
        "Constraint": "Beschraenkung",
        "Context:": "Kontext:",
        "Continue anyway?": "Trotzdem fortfahren?",
        "Continue with the import?": "Mit dem Import fortfahren?",
        "Contrast": "Kontrast",
        "Converted PNG folder:": "Konvertierter PNG-Ordner:",
    },
}

for _language_code, _translations in _ADDITIONAL_BUILTIN_TRANSLATIONS.items():
    _payload = BUILTIN_LANGUAGES.get(_language_code)
    if isinstance(_payload, dict) and isinstance(_payload.get("translations"), dict):
        _payload["translations"].update(_translations)

_VISIBLE_UI_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "Help": "Ayuda",
        "Profile": "Perfil",
        "Export Profile...": "Exportar perfil...",
        "Import Profile...": "Importar perfil...",
        "Read-only package browser for scan, filter, preview, and extraction.": "Explorador de paquetes de solo lectura para escanear, filtrar, previsualizar y extraer.",
        "Package root": "Raiz de paquetes",
        "Extract root": "Raiz de extraccion",
        "Scan": "Escanear",
        "Refresh": "Actualizar",
        "Include path filter": "Filtro de ruta incluida",
        "Package filter, e.g. 0000/0.pamt or 0012": "Filtro de paquete, p. ej. 0000/0.pamt o 0012",
        "Exclude": "Excluir",
        "Exclude substrings or globs, ...": "Excluir subcadenas o globs, ...",
        "Hide common companion DDS suffixes": "Ocultar sufijos DDS complementarios comunes",
        "Folders": "Carpetas",
        "All packages": "Todos los paquetes",
        "All roles": "Todos los roles",
        "Previewable": "Previsualizable",
        "Tree View": "Vista de arbol",
        "Apply Filters": "Aplicar filtros",
        "Scan uses a saved archive cache when valid. Refresh ignores the cache and rebuilds it from the .pamt files. Exclude accepts semicolon-separated substrings or globs, so you can search for broad names like 'wood' while hiding suffix variants.": "Escanear usa una cache de archivos guardada si es valida. Actualizar ignora la cache y la reconstruye desde los .pamt. Excluir acepta subcadenas o globs separados por punto y coma, para buscar nombres amplios como 'wood' ocultando variantes de sufijo.",
        "Extract Selected": "Extraer seleccionados",
        "Extract Filtered": "Extraer filtrados",
        "DDS To Workflow": "DDS al flujo",
        "Open in Texture Editor": "Abrir en editor de texturas",
        "Resolve In Research": "Resolver en investigacion",
        "Archive Scan Log": "Registro de escaneo de archivos",
        "Ready. Research lists follow the current Archive Browser view, while DDS semantics can still use loaded .pac.xml / .pami sidecars when available.": "Listo. Las listas de investigacion siguen la vista actual del explorador de archivos, mientras que las semanticas DDS pueden usar sidecars .pac.xml / .pami cargados cuando esten disponibles.",
        "Research rows in this tab are built from the current Archive Browser view/filter. When available, DDS classification can still consult loaded archive sidecars such as .pac.xml and .pami so color/albedo and other roles do not depend only on filename guessing.": "Las filas de investigacion de esta pestana se crean desde la vista/filtro actual del explorador de archivos. Cuando esta disponible, la clasificacion DDS tambien consulta sidecars como .pac.xml y .pami para que color/albedo y otros roles no dependan solo del nombre de archivo.",
        "Select a grouped texture set to extract its related files and sidecars.": "Selecciona un conjunto de texturas agrupado para extraer sus archivos relacionados y sidecars.",
        "Bundles related texture members and sidecars such as base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML, and material files.": "Agrupa miembros de textura relacionados y sidecars como base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML y archivos de material.",
        "Classifies archive textures as color, normal, mask, roughness, emissive, UI, impostor, or unknown using naming/path heuristics plus exact sidecar bindings from files such as .pac.xml and .pami when available.": "Clasifica texturas de archivo como color, normal, mascara, rugosidad, emisiva, UI, impostor o desconocida usando heuristicas de nombre/ruta y enlaces exactos de sidecars como .pac.xml y .pami.",
        "Uses the current Archive Browser scan/filter state so you can pick files for Research without leaving this tab.": "Usa el estado actual de escaneo/filtro del explorador de archivos para elegir archivos para investigacion sin salir de esta pestana.",
        "Persistent global preferences for startup behavior, archive loading, UI layout memory, safety prompts, and 3D preview rendering.": "Preferencias globales persistentes para inicio, carga de archivos, memoria del layout, avisos de seguridad y renderizado 3D.",
        "Direct backends can be prepared here. The setup buttons open official external download or install pages in your browser instead of downloading files inside the app. NCNN models can still be imported from files you already downloaded locally.": "Los backends directos se preparan aqui. Los botones abren paginas externas oficiales de descarga o instalacion en el navegador en vez de descargar archivos dentro de la app. Los modelos NCNN aun pueden importarse desde archivos locales ya descargados.",
        "Per-file workflow matching": "Coincidencia de flujo por archivo",
        "Build reusable per-file workflow profiles, assign them with ordered rules, and inspect the live matched DDS set.": "Crea perfiles reutilizables por archivo, asignales reglas ordenadas e inspecciona el conjunto DDS coincidente en vivo.",
        "This table follows the current Original DDS root and workflow filter. Exact-path assignments append new last-match rules.": "Esta tabla sigue la raiz DDS original y el filtro de flujo actuales. Las asignaciones por ruta exacta agregan nuevas reglas al final.",
        "Choose one optional upscaling backend. Texture Policy below still applies before DDS rebuild, while scale/tile controls only appear for the direct NCNN backend.": "Elige un backend de escalado opcional. La politica de texturas inferior se aplica antes de reconstruir DDS, y los controles de escala/tile solo aparecen para NCNN directo.",
        "Use automatic texture safety rules": "Usar reglas automaticas de seguridad de texturas",
        "Expert override: force technical maps through PNG/upscale path (unsafe)": "Anulacion experta: forzar mapas tecnicos por ruta PNG/escalado (inseguro)",
        "Create ready mod package after rebuild": "Crear paquete mod listo despues de reconstruir",
        "Mod package parent root": "Raiz padre del paquete mod",
        "Recommended first test. Upscale visible color/UI-style maps only; leave normals, masks, grayscale technical maps, vectors, and unknown maps unchanged.": "Primera prueba recomendada. Escala solo mapas visibles de color/estilo UI; deja normales, mascaras, mapas tecnicos en escala de grises, vectores y mapas desconocidos sin cambios.",
        "Safer visible-only preset. Upscale color and UI textures only; leave technical maps unchanged.": "Preajuste visible mas seguro. Escala solo texturas de color y UI; deja mapas tecnicos sin cambios.",
        "Upscale color, UI, emissive, and impostor textures; leave technical maps unchanged.": "Escala texturas de color, UI, emisivas e impostoras; deja mapas tecnicos sin cambios.",
        "Advanced/debug preset. Broadens eligibility to almost every image-like file, but planner/backend safety can still preserve technical maps unless you explicitly force an unsafe override.": "Preajuste avanzado/debug. Amplia la elegibilidad a casi cualquier archivo tipo imagen, pero la seguridad del planificador/backend puede preservar mapas tecnicos salvo que fuerces una anulacion insegura.",
        "Upscaled": "Escalado",
        "Copied unchanged": "Copiado sin cambios",
        "nothing": "nada",
        "This policy applies before DDS rebuild for every backend. Files kept out of the PNG path are copied through as original DDS when the current rules say they are safer untouched.": "Esta politica se aplica antes de reconstruir DDS para todos los backends. Los archivos que quedan fuera de la ruta PNG se copian como DDS originales cuando las reglas actuales indican que es mas seguro no tocarlos.",
        "Automatic rules still control final color space, compression, alpha-aware hints, and technical-map preservation after that policy is applied.": "Las reglas automaticas siguen controlando el espacio de color final, la compresion, las pistas con alpha y la preservacion de mapas tecnicos despues de aplicar esa politica.",
        "Expert override is enabled: technical textures can be forced through the generic visible-color PNG/upscale path even when the planner would normally preserve them.": "La anulacion experta esta activada: las texturas tecnicas pueden forzarse por la ruta PNG/escalado generica de color visible aunque el planificador normalmente las preservaria.",
        "This preset broadens technical-map eligibility, but unsafe technical upscaling still depends on planner/backend rules unless the expert override is enabled. Expect more failures, darker output, or broken shading unless you verify the results carefully.": "Este preajuste amplia la elegibilidad de mapas tecnicos, pero el escalado tecnico inseguro aun depende de las reglas del planificador/backend salvo que la anulacion experta este activada. Espera mas fallos, salidas mas oscuras o sombreado roto si no verificas los resultados cuidadosamente.",
        "chaiNNer uses its own chain settings for the actual upscale step. The Texture Policy above still decides which files are allowed into the PNG/upscale path and which ones stay original.": "chaiNNer usa sus propios ajustes de cadena para el paso real de escalado. La politica de texturas superior aun decide que archivos pueden entrar en la ruta PNG/escalado y cuales se mantienen originales.",
        "Direct upscale controls are only used when Real-ESRGAN NCNN is selected. With no backend selected, the Texture Policy still affects how existing PNG or preserve-original paths are handled.": "Los controles de escalado directo solo se usan cuando Real-ESRGAN NCNN esta seleccionado. Sin backend seleccionado, la politica de texturas aun afecta como se manejan PNG existentes o rutas de preservar original.",
        "Direct Upscale Controls (NCNN only)": "Controles de escalado directo (solo NCNN)",
        "Tile size": "Tamano de tile",
        "NCNN extra args": "Argumentos extra NCNN",
        "Post correction": "Post-correccion",
        "Retry with smaller tile on failure": "Reintentar con tile mas pequeno al fallar",
        "These controls only affect the direct Real-ESRGAN NCNN PNG upscale pass. Scale should stay close to the selected model's intended native scale, smaller tile sizes trade speed for lower VRAM use, and post correction can automatically decide per texture how aggressively to pull safe outputs back toward the source before DDS rebuild.": "Estos controles solo afectan el pase PNG de Real-ESRGAN NCNN directo. La escala debe mantenerse cerca de la escala nativa del modelo; tiles menores reducen VRAM a costa de velocidad; la post-correccion decide por textura cuanto acercar salidas seguras al origen antes de reconstruir DDS.",
        "Start always uses the current settings shown in Texture Workflow. Run Summary is optional and shows the current sources, backend, and policy without duplicating those controls.": "Iniciar siempre usa la configuracion actual del flujo de texturas. Resumen de ejecucion es opcional y muestra origenes, backend y politica sin duplicar controles.",
        "Add Files": "Agregar archivos",
        "Add Folder": "Agregar carpeta",
        "Auto-Match": "Asignar automaticamente",
        "Open In Texture Editor": "Abrir en editor de texturas",
        "Choose Local Original": "Elegir original local",
        "Choose Archive Original": "Elegir original de archivo",
        "Remove Selected": "Quitar seleccionados",
        "Clear All": "Limpiar todo",
        "Replace Queue": "Cola de reemplazo",
        "Edited File": "Archivo editado",
        "Original": "Original",
        "Select an imported file": "Selecciona un archivo importado",
        "Select a file to preview it here.": "Selecciona un archivo para previsualizarlo aqui.",
        "Selected item details appear here.": "Los detalles del elemento seleccionado apareceran aqui.",
        "Build Settings": "Configuracion de creacion",
        "Build mode": "Modo de creacion",
        "Size mode": "Modo de tamano",
        "Package parent root": "Raiz padre del paquete",
        "Rebuild only": "Solo reconstruir",
        "Upscale with NCNN, then rebuild": "Escalar con NCNN y reconstruir",
        "Use edited size": "Usar tamano editado",
        "Match original size": "Coincidir con tamano original",
        "Overwrite existing package files": "Sobrescribir archivos de paquete existentes",
        "Create .no_encrypt file": "Crear archivo .no_encrypt",
        "Build Package": "Crear paquete",
        "Open Output Folder": "Abrir carpeta de salida",
        "Mirror Texture Workflow": "Copiar ajustes del flujo de texturas",
        "Package Info": "Informacion del paquete",
        "Title": "Titulo",
        "Version": "Version",
        "Author": "Autor",
        "Description": "Descripcion",
        "Nexus URL": "URL de Nexus",
        "Open Image...": "Abrir imagen...",
        "Open Project...": "Abrir proyecto...",
        "Save Project": "Guardar proyecto",
        "Export PNG": "Exportar PNG",
        "Send To Replace Assistant": "Enviar al asistente de reemplazo",
        "Send To Texture Workflow": "Enviar al flujo de texturas",
        "Tools": "Herramientas",
        "Edit": "Editar",
        "Paint": "Pintar",
        "Erase": "Borrar",
        "Fill": "Rellenar",
        "Gradient": "Degradado",
        "Sharpen": "Enfocar",
        "Soften": "Suavizar",
        "Smudge": "Difuminar",
        "Dodge/Burn": "Aclarar/Oscurecer",
        "Heal": "Corregir",
        "Patch": "Parche",
        "Move": "Mover",
        "Rect Select": "Seleccion rectangular",
        "Lasso": "Lazo",
        "Recolor": "Recolorear",
    },
    "de": {
        "Help": "Hilfe",
        "Profile": "Profil",
        "Export Profile...": "Profil exportieren...",
        "Import Profile...": "Profil importieren...",
        "Read-only package browser for scan, filter, preview, and extraction.": "Schreibgeschuetzter Paketbrowser zum Scannen, Filtern, Anzeigen und Extrahieren.",
        "Package root": "Paketstamm",
        "Extract root": "Extraktionsstamm",
        "Scan": "Scannen",
        "Refresh": "Aktualisieren",
        "Include path filter": "Einschluss-Pfadfilter",
        "Package filter, e.g. 0000/0.pamt or 0012": "Paketfilter, z. B. 0000/0.pamt oder 0012",
        "Exclude": "Ausschliessen",
        "Exclude substrings or globs, ...": "Teilstrings oder Globs ausschliessen, ...",
        "Hide common companion DDS suffixes": "Uebliche begleitende DDS-Suffixe ausblenden",
        "Folders": "Ordner",
        "All packages": "Alle Pakete",
        "All roles": "Alle Rollen",
        "Previewable": "Vorschaufahig",
        "Tree View": "Baumansicht",
        "Apply Filters": "Filter anwenden",
        "Scan uses a saved archive cache when valid. Refresh ignores the cache and rebuilds it from the .pamt files. Exclude accepts semicolon-separated substrings or globs, so you can search for broad names like 'wood' while hiding suffix variants.": "Scannen nutzt einen gespeicherten Archiv-Cache, wenn er gueltig ist. Aktualisieren ignoriert den Cache und baut ihn aus den .pamt-Dateien neu. Ausschliessen akzeptiert Teilstrings oder Globs mit Semikolon, damit breite Namen wie 'wood' gesucht und Suffixvarianten verborgen werden koennen.",
        "Extract Selected": "Auswahl extrahieren",
        "Extract Filtered": "Gefilterte extrahieren",
        "DDS To Workflow": "DDS zum Workflow",
        "Open in Texture Editor": "Im Textur-Editor oeffnen",
        "Resolve In Research": "In Research aufloesen",
        "Archive Scan Log": "Archivscan-Log",
        "Ready. Research lists follow the current Archive Browser view, while DDS semantics can still use loaded .pac.xml / .pami sidecars when available.": "Bereit. Research-Listen folgen der aktuellen Archiv-Browser-Ansicht; DDS-Semantik kann weiterhin geladene .pac.xml-/.pami-Sidecars nutzen, wenn verfuegbar.",
        "Research rows in this tab are built from the current Archive Browser view/filter. When available, DDS classification can still consult loaded archive sidecars such as .pac.xml and .pami so color/albedo and other roles do not depend only on filename guessing.": "Research-Zeilen in diesem Tab werden aus der aktuellen Archiv-Browser-Ansicht/dem Filter erstellt. Wenn verfuegbar, nutzt die DDS-Klassifizierung auch Sidecars wie .pac.xml und .pami, damit Farbe/Albedo und andere Rollen nicht nur vom Dateinamen abhangen.",
        "Select a grouped texture set to extract its related files and sidecars.": "Waehle ein gruppiertes Texturset, um verwandte Dateien und Sidecars zu extrahieren.",
        "Bundles related texture members and sidecars such as base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML, and material files.": "Buendelt verwandte Texturmitglieder und Sidecars wie base/_color, _n/_wn, _sp, _m/_ma/_mg, _d/_dmap/_disp, _op/_dr, XML und Materialdateien.",
        "Classifies archive textures as color, normal, mask, roughness, emissive, UI, impostor, or unknown using naming/path heuristics plus exact sidecar bindings from files such as .pac.xml and .pami when available.": "Klassifiziert Archivtexturen als Farbe, Normal, Maske, Rauheit, Emissiv, UI, Impostor oder unbekannt anhand von Namens-/Pfadheuristik plus exakten Sidecar-Bindungen aus .pac.xml und .pami.",
        "Uses the current Archive Browser scan/filter state so you can pick files for Research without leaving this tab.": "Nutzt den aktuellen Scan-/Filterstatus des Archiv-Browsers, damit Dateien fuer Research ohne Tabwechsel gewaehlt werden koennen.",
        "Persistent global preferences for startup behavior, archive loading, UI layout memory, safety prompts, and 3D preview rendering.": "Persistente globale Einstellungen fuer Startverhalten, Archivladen, Layoutspeicher, Sicherheitsabfragen und 3D-Vorschau.",
        "Direct backends can be prepared here. The setup buttons open official external download or install pages in your browser instead of downloading files inside the app. NCNN models can still be imported from files you already downloaded locally.": "Direkte Backends koennen hier vorbereitet werden. Die Setup-Buttons oeffnen offizielle externe Download- oder Installationsseiten im Browser, statt Dateien in der App herunterzuladen. NCNN-Modelle koennen aus bereits lokal geladenen Dateien importiert werden.",
        "Per-file workflow matching": "Workflow-Zuordnung pro Datei",
        "Build reusable per-file workflow profiles, assign them with ordered rules, and inspect the live matched DDS set.": "Erstelle wiederverwendbare Profile pro Datei, weise sie mit geordneten Regeln zu und pruefe das live gefundene DDS-Set.",
        "This table follows the current Original DDS root and workflow filter. Exact-path assignments append new last-match rules.": "Diese Tabelle folgt dem aktuellen Original-DDS-Stamm und Workflow-Filter. Exakte Pfadzuweisungen fuegen neue letzte Regeln hinzu.",
        "Choose one optional upscaling backend. Texture Policy below still applies before DDS rebuild, while scale/tile controls only appear for the direct NCNN backend.": "Waehle ein optionales Upscaling-Backend. Die Textur-Richtlinie unten gilt weiterhin vor dem DDS-Neuaufbau; Scale/Tile-Regler erscheinen nur fuer direktes NCNN.",
        "Use automatic texture safety rules": "Automatische Textur-Sicherheitsregeln verwenden",
        "Expert override: force technical maps through PNG/upscale path (unsafe)": "Expertenoverride: technische Maps durch PNG/Upscale-Pfad erzwingen (unsicher)",
        "Create ready mod package after rebuild": "Nach Neuaufbau mod-fertiges Paket erstellen",
        "Mod package parent root": "Elternstamm des Mod-Pakets",
        "Recommended first test. Upscale visible color/UI-style maps only; leave normals, masks, grayscale technical maps, vectors, and unknown maps unchanged.": "Empfohlener erster Test. Skaliert nur sichtbare Farb-/UI-Maps hoch; Normalen, Masken, technische Graustufenmaps, Vektoren und unbekannte Maps bleiben unveraendert.",
        "Safer visible-only preset. Upscale color and UI textures only; leave technical maps unchanged.": "Sichereres Nur-sichtbar-Preset. Skaliert nur Farb- und UI-Texturen hoch; technische Maps bleiben unveraendert.",
        "Upscale color, UI, emissive, and impostor textures; leave technical maps unchanged.": "Skaliert Farb-, UI-, Emissiv- und Impostor-Texturen hoch; technische Maps bleiben unveraendert.",
        "Advanced/debug preset. Broadens eligibility to almost every image-like file, but planner/backend safety can still preserve technical maps unless you explicitly force an unsafe override.": "Erweitertes/Debug-Preset. Erweitert die Eignung auf fast jede bildartige Datei, aber Planer-/Backend-Sicherheit kann technische Maps weiter erhalten, sofern kein unsicherer Override erzwungen wird.",
        "Upscaled": "Hochskaliert",
        "Copied unchanged": "Unveraendert kopiert",
        "nothing": "nichts",
        "This policy applies before DDS rebuild for every backend. Files kept out of the PNG path are copied through as original DDS when the current rules say they are safer untouched.": "Diese Richtlinie gilt vor dem DDS-Neuaufbau fuer jedes Backend. Dateien ausserhalb des PNG-Pfads werden als Original-DDS kopiert, wenn die aktuellen Regeln sagen, dass sie unveraendert sicherer sind.",
        "Automatic rules still control final color space, compression, alpha-aware hints, and technical-map preservation after that policy is applied.": "Automatische Regeln steuern danach weiterhin endgueltigen Farbraum, Kompression, Alpha-Hinweise und Erhaltung technischer Maps.",
        "Expert override is enabled: technical textures can be forced through the generic visible-color PNG/upscale path even when the planner would normally preserve them.": "Expertenoverride ist aktiviert: technische Texturen koennen durch den generischen sichtbaren PNG/Upscale-Pfad gezwungen werden, auch wenn der Planer sie normalerweise erhalten wuerde.",
        "This preset broadens technical-map eligibility, but unsafe technical upscaling still depends on planner/backend rules unless the expert override is enabled. Expect more failures, darker output, or broken shading unless you verify the results carefully.": "Dieses Preset erweitert die Eignung technischer Maps, aber unsicheres technisches Upscaling haengt weiter von Planer-/Backend-Regeln ab, sofern der Expertenoverride nicht aktiv ist. Rechne mit mehr Fehlern, dunklerer Ausgabe oder kaputtem Shading, wenn du Ergebnisse nicht sorgfaeltig pruefst.",
        "chaiNNer uses its own chain settings for the actual upscale step. The Texture Policy above still decides which files are allowed into the PNG/upscale path and which ones stay original.": "chaiNNer nutzt fuer den eigentlichen Upscale-Schritt seine eigenen Ketteneinstellungen. Die Textur-Richtlinie oben entscheidet weiterhin, welche Dateien in den PNG/Upscale-Pfad duerfen und welche original bleiben.",
        "Direct upscale controls are only used when Real-ESRGAN NCNN is selected. With no backend selected, the Texture Policy still affects how existing PNG or preserve-original paths are handled.": "Direkte Upscale-Regler werden nur genutzt, wenn Real-ESRGAN NCNN ausgewaehlt ist. Ohne ausgewaehltes Backend beeinflusst die Textur-Richtlinie weiterhin, wie vorhandene PNGs oder Original-erhalten-Pfade behandelt werden.",
        "Direct Upscale Controls (NCNN only)": "Direkte Upscale-Steuerung (nur NCNN)",
        "Tile size": "Tile-Groesse",
        "NCNN extra args": "NCNN-Zusatzargumente",
        "Post correction": "Nachkorrektur",
        "Retry with smaller tile on failure": "Bei Fehler mit kleinerem Tile wiederholen",
        "These controls only affect the direct Real-ESRGAN NCNN PNG upscale pass. Scale should stay close to the selected model's intended native scale, smaller tile sizes trade speed for lower VRAM use, and post correction can automatically decide per texture how aggressively to pull safe outputs back toward the source before DDS rebuild.": "Diese Regler betreffen nur den direkten Real-ESRGAN-NCNN-PNG-Upscale. Die Skalierung sollte nahe an der nativen Modellskala bleiben; kleinere Tiles senken VRAM-Nutzung auf Kosten von Geschwindigkeit; Nachkorrektur kann pro Textur entscheiden, wie stark sichere Ausgaben vor dem DDS-Neuaufbau an die Quelle angenaehert werden.",
        "Start always uses the current settings shown in Texture Workflow. Run Summary is optional and shows the current sources, backend, and policy without duplicating those controls.": "Start verwendet immer die im Textur-Workflow gezeigten aktuellen Einstellungen. Die Ausfuehrungszusammenfassung ist optional und zeigt Quellen, Backend und Richtlinie ohne diese Regler zu duplizieren.",
        "Add Files": "Dateien hinzufuegen",
        "Add Folder": "Ordner hinzufuegen",
        "Auto-Match": "Automatisch zuordnen",
        "Open In Texture Editor": "Im Textur-Editor oeffnen",
        "Choose Local Original": "Lokales Original waehlen",
        "Choose Archive Original": "Archiv-Original waehlen",
        "Remove Selected": "Auswahl entfernen",
        "Clear All": "Alles loeschen",
        "Replace Queue": "Ersetzungswarteschlange",
        "Edited File": "Bearbeitete Datei",
        "Original": "Original",
        "Select an imported file": "Waehle eine importierte Datei",
        "Select a file to preview it here.": "Waehle eine Datei, um sie hier anzuzeigen.",
        "Selected item details appear here.": "Details zum ausgewaehlten Element erscheinen hier.",
        "Build Settings": "Build-Einstellungen",
        "Build mode": "Build-Modus",
        "Size mode": "Groessenmodus",
        "Package parent root": "Elternstamm des Pakets",
        "Rebuild only": "Nur neu aufbauen",
        "Upscale with NCNN, then rebuild": "Mit NCNN hochskalieren, dann neu aufbauen",
        "Use edited size": "Bearbeitete Groesse verwenden",
        "Match original size": "Originalgroesse uebernehmen",
        "Overwrite existing package files": "Vorhandene Paketdateien ueberschreiben",
        "Create .no_encrypt file": ".no_encrypt-Datei erstellen",
        "Build Package": "Paket erstellen",
        "Open Output Folder": "Ausgabeordner oeffnen",
        "Mirror Texture Workflow": "Textur-Workflow spiegeln",
        "Package Info": "Paketinformationen",
        "Title": "Titel",
        "Version": "Version",
        "Author": "Autor",
        "Description": "Beschreibung",
        "Nexus URL": "Nexus-URL",
        "Open Image...": "Bild oeffnen...",
        "Open Project...": "Projekt oeffnen...",
        "Save Project": "Projekt speichern",
        "Export PNG": "PNG exportieren",
        "Send To Replace Assistant": "An Ersetzungsassistent senden",
        "Send To Texture Workflow": "An Textur-Workflow senden",
        "Tools": "Werkzeuge",
        "Edit": "Bearbeiten",
        "Paint": "Malen",
        "Erase": "Radieren",
        "Fill": "Fuellen",
        "Gradient": "Verlauf",
        "Sharpen": "Schaerfen",
        "Soften": "Weichzeichnen",
        "Smudge": "Verwischen",
        "Dodge/Burn": "Abwedeln/Nachbelichten",
        "Heal": "Heilen",
        "Patch": "Patchen",
        "Move": "Verschieben",
        "Rect Select": "Rechteckauswahl",
        "Lasso": "Lasso",
        "Recolor": "Umfaerben",
    },
}

for _language_code, _translations in _VISIBLE_UI_TRANSLATIONS.items():
    _payload = BUILTIN_LANGUAGES.get(_language_code)
    if isinstance(_payload, dict) and isinstance(_payload.get("translations"), dict):
        _payload["translations"].update(_translations)

_REVIEWED_UI_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "-1 draws all batches. Any other value draws only that batch index.": "-1 dibuja todos los lotes. Cualquier otro valor dibuja solo ese indice de lote.",
        "1. Source Summary": "1. Resumen de origen",
        "2. Backend Summary": "2. Resumen del backend",
        "3. Texture Policy Summary": "3. Resumen de politica de texturas",
        "4. Direct Backend Summary (NCNN only)": "4. Resumen de backend directo (solo NCNN)",
        "5. Safety And Export Summary": "5. Resumen de seguridad y exportacion",
        "6. Export Summary": "6. Resumen de exportacion",
        "A read-only summary of the current workflow: source roots, selected backend, texture policy, direct-backend controls, and the main safety/export behavior that will be used when you run.": "Resumen de solo lectura del flujo actual: raices de origen, backend seleccionado, politica de texturas, controles del backend directo y el comportamiento principal de seguridad/exportacion que se usara al ejecutar.",
        "Active targets": "Destinos activos",
        "Add PNG/DDS texture sources that were not included when the dialog opened.": "Agrega origenes de textura PNG/DDS que no estaban incluidos al abrir el dialogo.",
        "Add To Target": "Agregar al destino",
        "Add a folder of PNG/DDS texture sources and rescan suggestions.": "Agrega una carpeta de origenes PNG/DDS y vuelve a escanear sugerencias.",
        "Add texture folder...": "Agregar carpeta de texturas...",
        "Add textures...": "Agregar texturas...",
        "Add this selected source to the chosen target without removing any existing source indexes.": "Agrega este origen seleccionado al destino elegido sin quitar indices de origen existentes.",
        "Adjustment opacity": "Opacidad del ajuste",
        "Advanced Original DDS Slot Overrides": "Anulaciones avanzadas de ranuras DDS originales",
        "Advanced compatibility view. These rows come from the original material sidecar and are not the default replacement workflow. Use them only when you intentionally need to override a specific original DDS slot.": "Vista avanzada de compatibilidad. Estas filas vienen del sidecar de material original y no son el flujo de reemplazo predeterminado. Usalas solo cuando necesites anular una ranura DDS original concreta.",
        "Advanced hidden": "Avanzados ocultos",
        "Advanced routing": "Enrutamiento avanzado",
        "Aligned sampling": "Muestreo alineado",
        "Alignment Summary": "Resumen de alineacion",
        "Alignment behavior, safety options, and export values.": "Comportamiento de alineacion, opciones de seguridad y valores de exportacion.",
        "Alignment mode": "Modo de alineacion",
        "Alpha handling": "Manejo de alfa",
        "Anchor": "Ancla",
        "Append the selected replacement source index to the selected target row.": "Anexa el indice del origen de reemplazo seleccionado a la fila de destino elegida.",
        "Applies safer DDS rebuild recommendations for format flags, alpha handling, and technical-map preservation. This is a safety/policy feature, not a brightness correction feature.": "Aplica recomendaciones mas seguras para reconstruir DDS: flags de formato, manejo de alfa y preservacion de mapas tecnicos. Es una funcion de seguridad/politica, no de correccion de brillo.",
        "Apply Texture Plan to Overrides...": "Aplicar plan de texturas a anulaciones...",
        "Apply These Overrides": "Aplicar estas anulaciones",
        "Apply best guesses": "Aplicar mejores estimaciones",
        "Apply compatible recommended replacement texture sources to the Advanced original-DDS override rows.": "Aplica origenes de textura de reemplazo recomendados y compatibles a las filas avanzadas de anulacion de DDS original.",
        "Apply the path and extension search filters.": "Aplica los filtros de busqueda por ruta y extension.",
        "Apply the same preview size to both compare panes. Larger fit sizes keep the side-by-side view but let you pan if the image exceeds the viewport.": "Aplica el mismo tamano de vista previa a ambos paneles de comparacion. Los tamanos mayores mantienen la vista lado a lado y permiten desplazar si la imagen excede el area visible.",
        "Apply the selected orientation preset once. Manual rotation fields remain editable afterward.": "Aplica una vez el preajuste de orientacion seleccionado. Los campos de rotacion manual siguen editables despues.",
        "Apply the suggested source texture to the selected original DDS slot.": "Aplica la textura de origen sugerida a la ranura DDS original seleccionada.",
        "Archive Files is waiting for the Archive Browser tree index. Open or refresh the Archive Browser view, or narrow the current filter.": "Archivos de archivo espera el indice de arbol del explorador. Abre o actualiza la vista del explorador de archivos, o reduce el filtro actual.",
        "Archive Performance": "Rendimiento del archivo",
        "Archive entries loaded. Texture sidecar cache is tracked in the compact status indicator.": "Entradas de archivo cargadas. La cache de sidecars de textura se muestra en el indicador compacto de estado.",
        "Archive packages are not loaded yet. Open Archive Browser and scan or load the archive cache first.": "Los paquetes de archivo aun no estan cargados. Abre el explorador de archivos y escanea o carga primero la cache.",
        "Archive path to resolve, e.g. object/texture/example_diffuse.dds": "Ruta de archivo a resolver, p. ej. object/texture/example_diffuse.dds",
        "Armor/helmet: rotate X +90": "Armadura/casco: rotar X +90",
        "Armor/helmet: rotate X -90": "Armadura/casco: rotar X -90",
        "Asset Compatibility": "Compatibilidad de recurso",
        "Assign Override Source": "Asignar origen de anulacion",
        "Assign Source": "Asignar origen",
        "Assigned": "Asignado",
        "Attach Back": "Volver a acoplar",
        "Auto / keep original": "Auto / mantener original",
        "Auto Balanced": "Auto equilibrado",
        "Auto: handheld/grip anchor": "Auto: ancla de mano/agarre",
        "Auto: preserve original placement": "Auto: conservar posicion original",
        "Blues": "Azules",
        "Browse NCNN model categories on the left, then expand a category to review its recommended models. Built-in entries include source links, non-downloading model pages, and purpose notes so users do not assume every model is interchangeable.": "Explora las categorias de modelos NCNN a la izquierda y expande una categoria para revisar sus modelos recomendados. Las entradas integradas incluyen enlaces de origen, paginas de modelos sin descarga y notas de uso para que no se asuma que todos los modelos son intercambiables.",
        "Browse grouped NCNN model recommendations with short descriptions, source pages, and non-downloading model pages.": "Explora recomendaciones agrupadas de modelos NCNN con descripciones breves, paginas de origen y paginas de modelos sin descarga.",
        "Builds a whole-archive .pami/.pac_xml lookup used for DDS reverse references and richer related-file lists. Selected .pam/.pac previews still parse direct sidecars when this is off.": "Crea una busqueda global de .pami/.pac_xml del archivo para referencias inversas DDS y listas de archivos relacionados mas completas. Las vistas .pam/.pac seleccionadas siguen leyendo sidecars directos cuando esta desactivado.",
        "Builds a whole-archive .pami/.pac_xml lookup used for DDS reverse references and richer related-file lists. Selected .pam/.pac previews still parse their direct sidecar lazily when this is off.": "Crea una busqueda global de .pami/.pac_xml del archivo para referencias inversas DDS y listas de archivos relacionados mas completas. Las vistas .pam/.pac seleccionadas siguen leyendo su sidecar directo bajo demanda cuando esta desactivado.",
        "Bulk Normal Validator details": "Detalles del validador masivo de normales",
        "Cancel Swap Target": "Cancelar destino de intercambio",
        "Change at least one material value before exporting.": "Cambia al menos un valor de material antes de exportar.",
        "Changes apply to 3D archive previews. The ranges are intentionally capped to keep the preview usable and avoid values that are likely to look broken or exceed practical GPU limits.": "Los cambios se aplican a las vistas 3D de archivos. Los rangos estan limitados para mantener la vista util y evitar valores que probablemente se vean rotos o excedan limites practicos de GPU.",
        "Check": "Comprobar",
        "Checked files are included with the import. Add local DDS/images or material sidecars only when needed.": "Los archivos marcados se incluyen con la importacion. Agrega DDS/imagenes locales o sidecars de material solo cuando sea necesario.",
        "Choose Mesh Import Mode": "Elegir modo de importacion de malla",
        "Choose a folder, zip, or file set that contains at least one matching .param + .bin pair with the same base name.": "Elige una carpeta, zip o conjunto de archivos que contenga al menos un par .param + .bin coincidente con el mismo nombre base.",
        "Choose archive original DDS": "Elegir DDS original del archivo",
        "Choose how the filtered archive rows are grouped visually. This does not change extraction, preview, or patch behavior.": "Elige como se agrupan visualmente las filas de archivo filtradas. Esto no cambia la extraccion, vista previa ni parcheo.",
        "Choose how to import this mesh file.": "Elige como importar este archivo de malla.",
        "Choose replacement textures for mapped draw slots. Hover a row to highlight the affected part in the preview.": "Elige texturas de reemplazo para ranuras de dibujo mapeadas. Pasa el cursor sobre una fila para resaltar la parte afectada en la vista previa.",
        "Choose the PNG/DDS source that should replace this texture slot.": "Elige el origen PNG/DDS que debe reemplazar esta ranura de textura.",
        "Choose the dimensions for generated DDS textures. Source image size preserves imported 4K textures; Original DDS size keeps the old template dimensions.": "Elige las dimensiones de las texturas DDS generadas. Tamano de imagen de origen conserva texturas 4K importadas; tamano DDS original conserva las dimensiones de la plantilla antigua.",
        "Choose the original draw/material target that this selected source should feed.": "Elige el destino original de dibujo/material que debe recibir este origen seleccionado.",
        "Choose which channels paint, fill, gradient, recolor, and retouch tools are allowed to modify.": "Elige que canales pueden modificar las herramientas de pintura, relleno, degradado, recoloracion y retoque.",
        "Clear Original": "Limpiar original",
        "Clear Preview Cache": "Limpiar cache de vistas",
        "Clear Replacement": "Limpiar reemplazo",
        "Clear Target": "Limpiar destino",
        "Clear all guesses": "Limpiar estimaciones",
        "Clear existing output package before build": "Limpiar paquete de salida existente antes de crear",
        "Clear only the original reference part selection and preview highlight.": "Limpia solo la seleccion de parte de referencia original y el resaltado de vista previa.",
        "Clear only the replacement source selection and preview highlight without changing mappings.": "Limpia solo la seleccion de origen de reemplazo y el resaltado sin cambiar mapeos.",
        "Clear original, replacement, and target row selections/highlighting without changing mappings.": "Limpia selecciones/resaltados de filas de original, reemplazo y destino sin cambiar mapeos.",
        "Clear the temporary Flip Base V and Disable Support Maps preview overrides.": "Limpia las anulaciones temporales de vista Flip Base V y Desactivar mapas de soporte.",
        "Clears the in-memory Archive Browser preview cache. Sidecar scan caches on disk are not removed.": "Limpia la cache de vista previa del explorador en memoria. No se eliminan las caches de escaneo de sidecars en disco.",
        "Color palette used for code/text previews and archive Details metadata panes.": "Paleta de color usada en vistas de codigo/texto y paneles de metadatos Detalles del archivo.",
        "Color palette used for timestamps, warnings, errors, paths, numbers, backend names, and progress values in log panes.": "Paleta de color usada para marcas de tiempo, advertencias, errores, rutas, numeros, nombres de backend y valores de progreso en paneles de registro.",
        "Columns can be resized or reordered. Use the horizontal scrollbar when the queue is narrower than the full column set.": "Las columnas se pueden redimensionar o reordenar. Usa la barra horizontal cuando la cola sea mas estrecha que el conjunto completo de columnas.",
        "Compatibility": "Compatibilidad",
        "Compatibility/manual repair.": "Compatibilidad/reparacion manual.",
        "Conflict mode": "Modo de conflicto",
        "Contiguous fill only": "Solo relleno contiguo",
        "Controls highlighting intensity for logs, archive details, preview details, and referenced-file detail panes.": "Controla la intensidad del resaltado en registros, detalles de archivo, detalles de vista y paneles de detalle de archivos referenciados.",
        "Controls whether highlighted log/code tokens use bold emphasis.": "Controla si los tokens resaltados de registro/codigo usan enfasis en negrita.",
        "Controls whether the preview prefers mesh-derived base textures or lets sidecar-visible layered textures replace them.": "Controla si la vista prefiere texturas base derivadas de la malla o permite que texturas visibles por sidecar las reemplacen.",
        "Controls which texture types are actually sent to the upscaler. It does not guarantee that the selected model will preserve brightness, contrast, or shading correctly.": "Controla que tipos de textura se envian realmente al escalador. No garantiza que el modelo seleccionado conserve brillo, contraste o sombreado correctamente.",
        "Controls which texture types are allowed into the PNG/upscale path and which ones are copied through unchanged.": "Controla que tipos de textura pueden entrar en la ruta PNG/escalado y cuales se copian sin cambios.",
        "Copied as": "Copiado como",
        "Copy + Assign To Target": "Copiar + asignar al destino",
        "Copy Alpha": "Copiar alfa",
        "Copy Blue": "Copiar azul",
        "Copy Green": "Copiar verde",
        "Copy Original As Source": "Copiar original como origen",
        "Copy Red": "Copiar rojo",
        "Copy To New Layer": "Copiar a nueva capa",
        "Create ready mod package output": "Crear salida de paquete mod listo",
        "Crop To Selection": "Recortar a seleccion",
        "Crop, resize, trim, flip, or rotate the current document while keeping layer positions aligned.": "Recorta, redimensiona, ajusta, voltea o rota el documento actual manteniendo alineadas las posiciones de capas.",
        "Current DDS": "DDS actual",
        "Custom compact paths": "Rutas compactas personalizadas",
        "Cyans": "Cianes",
        "DDS Size": "Tamano DDS",
        "DDS files are converted to PNG first. With no backend selected, Start stops after PNG conversion and does not rebuild DDS.": "Los archivos DDS se convierten primero a PNG. Sin backend seleccionado, Iniciar se detiene despues de la conversion PNG y no reconstruye DDS.",
        "DDS files are converted to source PNGs first. PNG-input chaiNNer chains should read the staging PNG root. DDS-direct chains can ignore the staged PNGs if the chain already reads DDS.": "Los DDS se convierten primero a PNG de origen. Las cadenas chaiNNer con entrada PNG deben leer la raiz PNG temporal. Las cadenas directas DDS pueden ignorar esos PNG si ya leen DDS.",
        "DDS files are converted to source PNGs first. Real-ESRGAN NCNN reads the staged PNGs and writes the final upscaled PNGs into PNG root.": "Los DDS se convierten primero a PNG de origen. Real-ESRGAN NCNN lee los PNG temporales y escribe los PNG escalados finales en la raiz PNG.",
        "DDS rebuild uses the existing PNG root directly.": "La reconstruccion DDS usa directamente la raiz PNG existente.",
        "DMM texture folder": "Carpeta de texturas DMM",
        "Dark Preview": "Vista oscura",
        "Default to generated/retargeted material sidecar in Mesh Replacement Alignment": "Usar por defecto el sidecar de material generado/retargeteado en alineacion de reemplazo de malla",
        "Definitive Mod Manager": "Definitive Mod Manager",
        "Delete Mask": "Eliminar mascara",
        "Delete Note": "Eliminar nota",
        "Density": "Densidad",
        "Detailed analysis context and warnings will appear here.": "Aqui apareceran contexto de analisis detallado y advertencias.",
        "Detailed paths and notes appear here.": "Aqui apareceran rutas detalladas y notas.",
        "Diagnostic render mode": "Modo de render diagnostico",
        "Diffuse light scale": "Escala de luz difusa",
        "Diffuse swizzle": "Reordenamiento difuso",
        "Disable UV scale": "Desactivar escala UV",
        "Disable all support maps": "Desactivar todos los mapas de soporte",
        "Disable base tint": "Desactivar tinte base",
        "Disable brightness": "Desactivar brillo",
        "Disable depth test": "Desactivar prueba de profundidad",
        "Disable height map": "Desactivar mapa de altura",
        "Disable lighting": "Desactivar iluminacion",
        "Disable material map": "Desactivar mapa de material",
        "Disable normal map": "Desactivar mapa normal",
        "Disable replacement texture assignments for the selected target.": "Desactiva asignaciones de textura de reemplazo para el destino seleccionado.",
        "Docs": "Documentos",
        "Dodge Highlights": "Aclarar altas luces",
        "Dodge Midtones": "Aclarar medios tonos",
        "Dodge Shadows": "Aclarar sombras",
        "Down": "Abajo",
        "Drag columns to reorder. Right-click the header to show, hide, or reset columns.": "Arrastra columnas para reordenar. Haz clic derecho en el encabezado para mostrar, ocultar o restablecer columnas.",
        "Edit Material Values...": "Editar valores de material...",
        "Edit mask": "Editar mascara",
        "Edited": "Editado",
        "Edited Input": "Entrada editada",
        "Edited input": "Entrada editada",
        "Empty Target": "Destino vacio",
        "Empty every target slot so you can rebuild the mapping manually.": "Vacia cada ranura de destino para reconstruir el mapeo manualmente.",
        "Empty targets": "Destinos vacios",
        "Use support-map preview shading": "Usar sombreado de mapas de soporte",
        "Enable mask": "Activar mascara",
        "Enable resolved normal, material/mask, and height maps in the preview.": "Activa mapas normal, material/mascara y altura resueltos en la vista previa.",
        "Existing Files Found": "Archivos existentes encontrados",
        "Export Files": "Exportar archivos",
        "Export Grid Slices...": "Exportar cortes de cuadricula...",
        "Export Report CSV": "Exportar informe CSV",
        "Export Report JSON": "Exportar informe JSON",
        "Export Selection...": "Exportar seleccion...",
        "Export Values": "Valores de exportacion",
        "Export root": "Raiz de exportacion",
        "External Tools": "Herramientas externas",
        "Extract": "Extraer",
        "Extract Alpha": "Extraer alfa",
        "Extract Blue": "Extraer azul",
        "Extract Green": "Extraer verde",
        "Extract Red": "Extraer rojo",
        "Extract Related Set": "Extraer conjunto relacionado",
        "Family Members": "Miembros de familia",
        "Fatal startup, crash, and hang reports are always written locally. Enable this to also save additional context for recoverable preview/worker errors.": "Los informes de inicio fatal, fallos y bloqueos siempre se escriben localmente. Activa esto para guardar tambien contexto adicional de errores recuperables de vista/trabajador.",
        "Filter": "Filtro",
        "Filter by basename or relative path...": "Filtrar por nombre base o ruta relativa...",
        "Filters": "Filtros",
        "Final Path": "Ruta final",
        "Find": "Buscar",
        "Find In Archive": "Buscar en archivo",
        "Fit Size": "Tamano ajustado",
        "Fit the preview to the available space.": "Ajusta la vista previa al espacio disponible.",
        "Flip H": "Voltear H",
        "Flip V": "Voltear V",
        "Folder structure": "Estructura de carpetas",
        "Force nearest filtering / no mipmaps": "Forzar filtro nearest / sin mipmaps",
        "From Channel": "Desde canal",
        "Game-relative folders": "Carpetas relativas al juego",
        "Generate": "Generar",
        "Geometry": "Geometria",
        "Global sidecar indexing is optional. Leave it off for faster startup and browsing; enable it when you need DDS reverse references across the archive. Worker count range: 1-16. Preview cache range: 12-256 entries.": "La indexacion global de sidecars es opcional. Dejala desactivada para iniciar y navegar mas rapido; activala cuando necesites referencias inversas DDS en todo el archivo. Rango de trabajadores: 1-16. Rango de cache de vistas: 12-256 entradas.",
        "Good for many swords/weapons when placement is correct but the tip points the wrong way.": "Util para muchas espadas/armas cuando la posicion es correcta pero la punta apunta en la direccion equivocada.",
        "Green": "Verde",
        "Grid": "Cuadricula",
        "Grid color": "Color de cuadricula",
        "Grid opacity": "Opacidad de cuadricula",
        "Guidance": "Guia",
        "Height effect max": "Efecto maximo de altura",
        "Height shininess boost": "Aumento de brillo por altura",
        "Hide companion suffixes": "Ocultar sufijos complementarios",
        "High Pass": "Paso alto",
        "Higher anisotropy can improve texture sharpness at grazing angles, but increases GPU work.": "Mayor anisotropia puede mejorar la nitidez de texturas en angulos oblicuos, pero aumenta el trabajo de GPU.",
        "Highlight the currently selected target slot in the preview.": "Resalta en la vista previa la ranura de destino seleccionada.",
        "Horizontal guides": "Guias horizontales",
        "Horizontal mirror": "Espejo horizontal",
        "Hue / Saturation": "Tono / saturacion",
        "Image Size...": "Tamano de imagen...",
        "Import Mesh Preview...": "Importar vista de malla...",
        "Import Mesh...": "Importar malla...",
        "Import Mode": "Modo de importacion",
        "Import a translated JSON language file. Custom languages are stored beside the app settings and can be selected here.": "Importa un archivo JSON de idioma traducido. Los idiomas personalizados se guardan junto a los ajustes de la app y se pueden seleccionar aqui.",
        "Include extra local context in diagnostic reports": "Incluir contexto local adicional en informes de diagnostico",
        "Index texture sidecars for DDS related-file discovery": "Indexar sidecars de textura para descubrir archivos relacionados con DDS",
        "Inject missing base/color parameter (risky)": "Inyectar parametro base/color faltante (riesgoso)",
        "Invert Mask": "Invertir mascara",
        "JSON Mod Manager": "JSON Mod Manager",
        "Keep Original": "Mantener original",
    },
    "de": {
        "-1 draws all batches. Any other value draws only that batch index.": "-1 zeichnet alle Batches. Jeder andere Wert zeichnet nur diesen Batch-Index.",
        "1. Source Summary": "1. Quellenzusammenfassung",
        "2. Backend Summary": "2. Backend-Zusammenfassung",
        "3. Texture Policy Summary": "3. Textur-Richtlinienzusammenfassung",
        "4. Direct Backend Summary (NCNN only)": "4. Direkt-Backend-Zusammenfassung (nur NCNN)",
        "5. Safety And Export Summary": "5. Sicherheits- und Exportzusammenfassung",
        "6. Export Summary": "6. Exportzusammenfassung",
        "A read-only summary of the current workflow: source roots, selected backend, texture policy, direct-backend controls, and the main safety/export behavior that will be used when you run.": "Eine schreibgeschuetzte Zusammenfassung des aktuellen Workflows: Quellstaemme, ausgewaehltes Backend, Texturrichtlinie, Direkt-Backend-Regler und das wichtigste Sicherheits-/Exportverhalten fuer den Lauf.",
        "Active targets": "Aktive Ziele",
        "Add PNG/DDS texture sources that were not included when the dialog opened.": "PNG-/DDS-Texturquellen hinzufuegen, die beim Oeffnen des Dialogs nicht enthalten waren.",
        "Add To Target": "Zum Ziel hinzufuegen",
        "Add a folder of PNG/DDS texture sources and rescan suggestions.": "Einen Ordner mit PNG-/DDS-Texturquellen hinzufuegen und Vorschlaege neu scannen.",
        "Add texture folder...": "Texturordner hinzufuegen...",
        "Add textures...": "Texturen hinzufuegen...",
        "Add this selected source to the chosen target without removing any existing source indexes.": "Diese ausgewaehlte Quelle zum gewaehlten Ziel hinzufuegen, ohne vorhandene Quellenindizes zu entfernen.",
        "Adjustment opacity": "Anpassungsdeckkraft",
        "Advanced Original DDS Slot Overrides": "Erweiterte Overrides fuer Original-DDS-Slots",
        "Advanced compatibility view. These rows come from the original material sidecar and are not the default replacement workflow. Use them only when you intentionally need to override a specific original DDS slot.": "Erweiterte Kompatibilitaetsansicht. Diese Zeilen stammen aus dem originalen Material-Sidecar und gehoeren nicht zum Standard-Ersetzungsworkflow. Nur verwenden, wenn ein bestimmter Original-DDS-Slot gezielt ueberschrieben werden soll.",
        "Advanced hidden": "Erweiterte ausgeblendet",
        "Advanced routing": "Erweitertes Routing",
        "Aligned sampling": "Ausgerichtetes Sampling",
        "Alignment Summary": "Ausrichtungszusammenfassung",
        "Alignment behavior, safety options, and export values.": "Ausrichtungsverhalten, Sicherheitsoptionen und Exportwerte.",
        "Alignment mode": "Ausrichtungsmodus",
        "Alpha handling": "Alpha-Behandlung",
        "Anchor": "Anker",
        "Append the selected replacement source index to the selected target row.": "Den ausgewaehlten Ersetzungsquellenindex an die gewaehlte Zielzeile anhaengen.",
        "Applies safer DDS rebuild recommendations for format flags, alpha handling, and technical-map preservation. This is a safety/policy feature, not a brightness correction feature.": "Wendet sicherere Empfehlungen fuer den DDS-Neuaufbau an: Format-Flags, Alpha-Behandlung und Erhalt technischer Maps. Das ist eine Sicherheits-/Richtlinienfunktion, keine Helligkeitskorrektur.",
        "Apply Texture Plan to Overrides...": "Texturplan auf Overrides anwenden...",
        "Apply These Overrides": "Diese Overrides anwenden",
        "Apply best guesses": "Beste Schaetzungen anwenden",
        "Apply compatible recommended replacement texture sources to the Advanced original-DDS override rows.": "Kompatible empfohlene Ersetzungstexturquellen auf die erweiterten Original-DDS-Override-Zeilen anwenden.",
        "Apply the path and extension search filters.": "Pfad- und Erweiterungssuchfilter anwenden.",
        "Apply the same preview size to both compare panes. Larger fit sizes keep the side-by-side view but let you pan if the image exceeds the viewport.": "Dieselbe Vorschaugroesse auf beide Vergleichsansichten anwenden. Groessere Einpassungen behalten die Seitenansicht, erlauben aber Schwenken, wenn das Bild den Viewport ueberschreitet.",
        "Apply the selected orientation preset once. Manual rotation fields remain editable afterward.": "Das ausgewaehlte Ausrichtungspreset einmal anwenden. Manuelle Rotationsfelder bleiben danach editierbar.",
        "Apply the suggested source texture to the selected original DDS slot.": "Die vorgeschlagene Quelltextur auf den ausgewaehlten Original-DDS-Slot anwenden.",
        "Archive Files is waiting for the Archive Browser tree index. Open or refresh the Archive Browser view, or narrow the current filter.": "Archivdateien wartet auf den Baumindex des Archiv-Browsers. Oeffne oder aktualisiere die Archiv-Browser-Ansicht oder grenze den aktuellen Filter ein.",
        "Archive Performance": "Archivleistung",
        "Archive entries loaded. Texture sidecar cache is tracked in the compact status indicator.": "Archiveintraege geladen. Der Textur-Sidecar-Cache wird in der kompakten Statusanzeige verfolgt.",
        "Archive packages are not loaded yet. Open Archive Browser and scan or load the archive cache first.": "Archivpakete sind noch nicht geladen. Oeffne den Archiv-Browser und scanne oder lade zuerst den Archiv-Cache.",
        "Archive path to resolve, e.g. object/texture/example_diffuse.dds": "Aufzuloesender Archivpfad, z. B. object/texture/example_diffuse.dds",
        "Armor/helmet: rotate X +90": "Ruestung/Helm: X +90 drehen",
        "Armor/helmet: rotate X -90": "Ruestung/Helm: X -90 drehen",
        "Asset Compatibility": "Asset-Kompatibilitaet",
        "Assign Override Source": "Override-Quelle zuweisen",
        "Assign Source": "Quelle zuweisen",
        "Assigned": "Zugewiesen",
        "Attach Back": "Wieder andocken",
        "Auto / keep original": "Auto / Original behalten",
        "Auto Balanced": "Automatisch ausgewogen",
        "Auto: handheld/grip anchor": "Auto: Hand-/Griffanker",
        "Auto: preserve original placement": "Auto: Originalplatzierung erhalten",
        "Blues": "Blautoene",
        "Browse NCNN model categories on the left, then expand a category to review its recommended models. Built-in entries include source links, non-downloading model pages, and purpose notes so users do not assume every model is interchangeable.": "Links NCNN-Modellkategorien durchsuchen und dann eine Kategorie erweitern, um empfohlene Modelle zu pruefen. Integrierte Eintraege enthalten Quelllinks, nicht herunterladende Modellseiten und Zweckhinweise, damit nicht angenommen wird, dass jedes Modell austauschbar ist.",
        "Browse grouped NCNN model recommendations with short descriptions, source pages, and non-downloading model pages.": "Gruppierte NCNN-Modell-Empfehlungen mit Kurzbeschreibungen, Quellseiten und nicht herunterladenden Modellseiten durchsuchen.",
        "Builds a whole-archive .pami/.pac_xml lookup used for DDS reverse references and richer related-file lists. Selected .pam/.pac previews still parse direct sidecars when this is off.": "Erstellt eine archivweite .pami/.pac_xml-Suche fuer DDS-Rueckverweise und reichere Listen verwandter Dateien. Ausgewaehlte .pam/.pac-Vorschauen parsen weiterhin direkte Sidecars, wenn dies aus ist.",
        "Builds a whole-archive .pami/.pac_xml lookup used for DDS reverse references and richer related-file lists. Selected .pam/.pac previews still parse their direct sidecar lazily when this is off.": "Erstellt eine archivweite .pami/.pac_xml-Suche fuer DDS-Rueckverweise und reichere Listen verwandter Dateien. Ausgewaehlte .pam/.pac-Vorschauen parsen ihren direkten Sidecar weiterhin bei Bedarf, wenn dies aus ist.",
        "Bulk Normal Validator details": "Details des Massen-Normalenvalidators",
        "Cancel Swap Target": "Swap-Ziel abbrechen",
        "Change at least one material value before exporting.": "Vor dem Export mindestens einen Materialwert aendern.",
        "Changes apply to 3D archive previews. The ranges are intentionally capped to keep the preview usable and avoid values that are likely to look broken or exceed practical GPU limits.": "Aenderungen gelten fuer 3D-Archivvorschauen. Die Bereiche sind bewusst begrenzt, damit die Vorschau nutzbar bleibt und Werte vermieden werden, die wahrscheinlich defekt aussehen oder praktische GPU-Grenzen ueberschreiten.",
        "Check": "Pruefen",
        "Checked files are included with the import. Add local DDS/images or material sidecars only when needed.": "Markierte Dateien werden beim Import eingeschlossen. Lokale DDS/Bilder oder Material-Sidecars nur bei Bedarf hinzufuegen.",
        "Choose Mesh Import Mode": "Mesh-Importmodus waehlen",
        "Choose a folder, zip, or file set that contains at least one matching .param + .bin pair with the same base name.": "Waehle einen Ordner, ein ZIP oder einen Dateisatz mit mindestens einem passenden .param- + .bin-Paar mit demselben Basisnamen.",
        "Choose archive original DDS": "Original-DDS aus Archiv waehlen",
        "Choose how the filtered archive rows are grouped visually. This does not change extraction, preview, or patch behavior.": "Waehle, wie die gefilterten Archivzeilen visuell gruppiert werden. Das aendert Extraktion, Vorschau oder Patch-Verhalten nicht.",
        "Choose how to import this mesh file.": "Waehle, wie diese Mesh-Datei importiert werden soll.",
        "Choose replacement textures for mapped draw slots. Hover a row to highlight the affected part in the preview.": "Ersetzungstexturen fuer zugeordnete Draw-Slots waehlen. Eine Zeile anfahren, um den betroffenen Teil in der Vorschau hervorzuheben.",
        "Choose the PNG/DDS source that should replace this texture slot.": "PNG-/DDS-Quelle waehlen, die diesen Textur-Slot ersetzen soll.",
        "Choose the dimensions for generated DDS textures. Source image size preserves imported 4K textures; Original DDS size keeps the old template dimensions.": "Abmessungen fuer generierte DDS-Texturen waehlen. Quellbildgroesse erhaelt importierte 4K-Texturen; Original-DDS-Groesse behaelt alte Vorlagenabmessungen.",
        "Choose the original draw/material target that this selected source should feed.": "Originales Draw-/Materialziel waehlen, das diese ausgewaehlte Quelle speisen soll.",
        "Choose which channels paint, fill, gradient, recolor, and retouch tools are allowed to modify.": "Waehle, welche Kanaele Mal-, Fuell-, Verlaufs-, Umfaerbe- und Retuschewerkzeuge aendern duerfen.",
        "Clear Original": "Original leeren",
        "Clear Preview Cache": "Vorschau-Cache leeren",
        "Clear Replacement": "Ersetzung leeren",
        "Clear Target": "Ziel leeren",
        "Clear all guesses": "Alle Schaetzungen leeren",
        "Clear existing output package before build": "Vor Build vorhandenes Ausgabepaket leeren",
        "Clear only the original reference part selection and preview highlight.": "Nur die Auswahl des originalen Referenzteils und die Vorschauhervorhebung leeren.",
        "Clear only the replacement source selection and preview highlight without changing mappings.": "Nur die Ersetzungsquellenauswahl und Vorschauhervorhebung leeren, ohne Zuordnungen zu aendern.",
        "Clear original, replacement, and target row selections/highlighting without changing mappings.": "Original-, Ersetzungs- und Zielzeilenauswahl/-hervorhebung leeren, ohne Zuordnungen zu aendern.",
        "Clear the temporary Flip Base V and Disable Support Maps preview overrides.": "Temporaere Vorschau-Overrides Flip Base V und Support-Maps deaktivieren leeren.",
        "Clears the in-memory Archive Browser preview cache. Sidecar scan caches on disk are not removed.": "Leert den In-Memory-Vorschaucache des Archiv-Browsers. Sidecar-Scan-Caches auf Datentraeger werden nicht entfernt.",
        "Color palette used for code/text previews and archive Details metadata panes.": "Farbpalette fuer Code-/Textvorschauen und Archiv-Details-Metadatenbereiche.",
        "Color palette used for timestamps, warnings, errors, paths, numbers, backend names, and progress values in log panes.": "Farbpalette fuer Zeitstempel, Warnungen, Fehler, Pfade, Zahlen, Backend-Namen und Fortschrittswerte in Logbereichen.",
        "Columns can be resized or reordered. Use the horizontal scrollbar when the queue is narrower than the full column set.": "Spalten koennen geaendert oder umgeordnet werden. Die horizontale Scrollbar nutzen, wenn die Warteschlange schmaler als alle Spalten ist.",
        "Compatibility": "Kompatibilitaet",
        "Compatibility/manual repair.": "Kompatibilitaet/manuelle Reparatur.",
        "Conflict mode": "Konfliktmodus",
        "Contiguous fill only": "Nur zusammenhaengend fuellen",
        "Controls highlighting intensity for logs, archive details, preview details, and referenced-file detail panes.": "Steuert die Hervorhebungsstaerke fuer Logs, Archivdetails, Vorschaudetails und Detailbereiche referenzierter Dateien.",
        "Controls whether highlighted log/code tokens use bold emphasis.": "Steuert, ob hervorgehobene Log-/Code-Token fett dargestellt werden.",
        "Controls whether the preview prefers mesh-derived base textures or lets sidecar-visible layered textures replace them.": "Steuert, ob die Vorschau Mesh-basierte Basistexturen bevorzugt oder Sidecar-sichtbare Layer-Texturen sie ersetzen duerfen.",
        "Controls which texture types are actually sent to the upscaler. It does not guarantee that the selected model will preserve brightness, contrast, or shading correctly.": "Steuert, welche Texturtypen tatsaechlich an den Upscaler gesendet werden. Es garantiert nicht, dass das ausgewaehlte Modell Helligkeit, Kontrast oder Shading korrekt erhaelt.",
        "Controls which texture types are allowed into the PNG/upscale path and which ones are copied through unchanged.": "Steuert, welche Texturtypen in den PNG-/Upscale-Pfad duerfen und welche unveraendert kopiert werden.",
        "Copied as": "Kopiert als",
        "Copy + Assign To Target": "Kopieren + Ziel zuweisen",
        "Copy Alpha": "Alpha kopieren",
        "Copy Blue": "Blau kopieren",
        "Copy Green": "Gruen kopieren",
        "Copy Original As Source": "Original als Quelle kopieren",
        "Copy Red": "Rot kopieren",
        "Copy To New Layer": "In neue Ebene kopieren",
        "Create ready mod package output": "Ausgabe als mod-fertiges Paket erstellen",
        "Crop To Selection": "Auf Auswahl zuschneiden",
        "Crop, resize, trim, flip, or rotate the current document while keeping layer positions aligned.": "Aktuelles Dokument zuschneiden, skalieren, trimmen, spiegeln oder drehen, waehrend Ebenenpositionen ausgerichtet bleiben.",
        "Current DDS": "Aktuelle DDS",
        "Custom compact paths": "Benutzerdefinierte kompakte Pfade",
        "Cyans": "Cyantoene",
        "DDS Size": "DDS-Groesse",
        "DDS files are converted to PNG first. With no backend selected, Start stops after PNG conversion and does not rebuild DDS.": "DDS-Dateien werden zuerst in PNG umgewandelt. Ohne ausgewaehltes Backend stoppt Start nach der PNG-Konvertierung und baut DDS nicht neu.",
        "DDS files are converted to source PNGs first. PNG-input chaiNNer chains should read the staging PNG root. DDS-direct chains can ignore the staged PNGs if the chain already reads DDS.": "DDS-Dateien werden zuerst in Quell-PNGs umgewandelt. PNG-Eingabe-chaiNNer-Ketten sollten den Staging-PNG-Stamm lesen. DDS-direkte Ketten koennen die gestagten PNGs ignorieren, wenn sie bereits DDS lesen.",
        "DDS files are converted to source PNGs first. Real-ESRGAN NCNN reads the staged PNGs and writes the final upscaled PNGs into PNG root.": "DDS-Dateien werden zuerst in Quell-PNGs umgewandelt. Real-ESRGAN NCNN liest die gestagten PNGs und schreibt die final hochskalierten PNGs in den PNG-Stamm.",
        "DDS rebuild uses the existing PNG root directly.": "Der DDS-Neuaufbau verwendet direkt den vorhandenen PNG-Stamm.",
        "DMM texture folder": "DMM-Texturordner",
        "Dark Preview": "Dunkle Vorschau",
        "Default to generated/retargeted material sidecar in Mesh Replacement Alignment": "Generierten/retargeteten Material-Sidecar in Mesh-Ersetzungsausrichtung standardmaessig verwenden",
        "Definitive Mod Manager": "Definitive Mod Manager",
        "Delete Mask": "Maske loeschen",
        "Delete Note": "Notiz loeschen",
        "Density": "Dichte",
        "Detailed analysis context and warnings will appear here.": "Detaillierter Analysekontext und Warnungen erscheinen hier.",
        "Detailed paths and notes appear here.": "Detaillierte Pfade und Notizen erscheinen hier.",
        "Diagnostic render mode": "Diagnose-Render-Modus",
        "Diffuse light scale": "Diffuse-Licht-Skalierung",
        "Diffuse swizzle": "Diffuse-Swizzle",
        "Disable UV scale": "UV-Skalierung deaktivieren",
        "Disable all support maps": "Alle Support-Maps deaktivieren",
        "Disable base tint": "Basistint deaktivieren",
        "Disable brightness": "Helligkeit deaktivieren",
        "Disable depth test": "Tiefentest deaktivieren",
        "Disable height map": "Height-Map deaktivieren",
        "Disable lighting": "Beleuchtung deaktivieren",
        "Disable material map": "Material-Map deaktivieren",
        "Disable normal map": "Normal-Map deaktivieren",
        "Disable replacement texture assignments for the selected target.": "Ersetzungstexturzuweisungen fuer das ausgewaehlte Ziel deaktivieren.",
        "Docs": "Dokumente",
        "Dodge Highlights": "Highlights abwedeln",
        "Dodge Midtones": "Mitteltone abwedeln",
        "Dodge Shadows": "Schatten abwedeln",
        "Down": "Runter",
        "Drag columns to reorder. Right-click the header to show, hide, or reset columns.": "Spalten zum Umordnen ziehen. Rechtsklick auf die Kopfzeile zeigt, blendet aus oder setzt Spalten zurueck.",
        "Edit Material Values...": "Materialwerte bearbeiten...",
        "Edit mask": "Maske bearbeiten",
        "Edited": "Bearbeitet",
        "Edited Input": "Bearbeitete Eingabe",
        "Edited input": "Bearbeitete Eingabe",
        "Empty Target": "Leeres Ziel",
        "Empty every target slot so you can rebuild the mapping manually.": "Alle Ziel-Slots leeren, damit die Zuordnung manuell neu aufgebaut werden kann.",
        "Empty targets": "Leere Ziele",
        "Use support-map preview shading": "Support-Map-Vorschau-Shading verwenden",
        "Enable mask": "Maske aktivieren",
        "Enable resolved normal, material/mask, and height maps in the preview.": "Aufgeloeste Normal-, Material-/Masken- und Height-Maps in der Vorschau aktivieren.",
        "Existing Files Found": "Vorhandene Dateien gefunden",
        "Export Files": "Dateien exportieren",
        "Export Grid Slices...": "Rastersegmente exportieren...",
        "Export Report CSV": "Bericht als CSV exportieren",
        "Export Report JSON": "Bericht als JSON exportieren",
        "Export Selection...": "Auswahl exportieren...",
        "Export Values": "Exportwerte",
        "Export root": "Exportstamm",
        "External Tools": "Externe Werkzeuge",
        "Extract": "Extrahieren",
        "Extract Alpha": "Alpha extrahieren",
        "Extract Blue": "Blau extrahieren",
        "Extract Green": "Gruen extrahieren",
        "Extract Red": "Rot extrahieren",
        "Extract Related Set": "Verwandtes Set extrahieren",
        "Family Members": "Familienmitglieder",
        "Fatal startup, crash, and hang reports are always written locally. Enable this to also save additional context for recoverable preview/worker errors.": "Fatale Start-, Crash- und Hang-Berichte werden immer lokal geschrieben. Aktivieren, um auch zusaetzlichen Kontext fuer behebbare Vorschau-/Workerfehler zu speichern.",
        "Filter": "Filter",
        "Filter by basename or relative path...": "Nach Basisname oder relativem Pfad filtern...",
        "Filters": "Filter",
        "Final Path": "Finaler Pfad",
        "Find": "Suchen",
        "Find In Archive": "Im Archiv suchen",
        "Fit Size": "Einpassgroesse",
        "Fit the preview to the available space.": "Vorschau an verfuegbaren Platz anpassen.",
        "Flip H": "H spiegeln",
        "Flip V": "V spiegeln",
        "Folder structure": "Ordnerstruktur",
        "Force nearest filtering / no mipmaps": "Nearest-Filter / keine Mipmaps erzwingen",
        "From Channel": "Aus Kanal",
        "Game-relative folders": "Spielrelative Ordner",
        "Generate": "Generieren",
        "Geometry": "Geometrie",
        "Global sidecar indexing is optional. Leave it off for faster startup and browsing; enable it when you need DDS reverse references across the archive. Worker count range: 1-16. Preview cache range: 12-256 entries.": "Globale Sidecar-Indexierung ist optional. Fuer schnelleren Start und Browsing ausgeschaltet lassen; aktivieren, wenn DDS-Rueckverweise im ganzen Archiv gebraucht werden. Worker-Bereich: 1-16. Vorschaucache-Bereich: 12-256 Eintraege.",
        "Good for many swords/weapons when placement is correct but the tip points the wrong way.": "Gut fuer viele Schwerter/Waffen, wenn die Platzierung stimmt, aber die Spitze in die falsche Richtung zeigt.",
        "Green": "Gruen",
        "Grid": "Raster",
        "Grid color": "Rasterfarbe",
        "Grid opacity": "Rasterdeckkraft",
        "Guidance": "Anleitung",
        "Height effect max": "Max. Height-Effekt",
        "Height shininess boost": "Height-Glanz-Boost",
        "Hide companion suffixes": "Begleit-Suffixe ausblenden",
        "High Pass": "Hochpass",
        "Higher anisotropy can improve texture sharpness at grazing angles, but increases GPU work.": "Hoehere Anisotropie kann Texturschaerfe bei flachen Winkeln verbessern, erhoeht aber die GPU-Arbeit.",
        "Highlight the currently selected target slot in the preview.": "Aktuell ausgewaehlten Ziel-Slot in der Vorschau hervorheben.",
        "Horizontal guides": "Horizontale Hilfslinien",
        "Horizontal mirror": "Horizontal spiegeln",
        "Hue / Saturation": "Farbton / Saettigung",
        "Image Size...": "Bildgroesse...",
        "Import Mesh Preview...": "Mesh-Vorschau importieren...",
        "Import Mesh...": "Mesh importieren...",
        "Import Mode": "Importmodus",
        "Import a translated JSON language file. Custom languages are stored beside the app settings and can be selected here.": "Eine uebersetzte JSON-Sprachdatei importieren. Benutzerdefinierte Sprachen werden neben den App-Einstellungen gespeichert und koennen hier ausgewaehlt werden.",
        "Include extra local context in diagnostic reports": "Zusaetzlichen lokalen Kontext in Diagnoseberichte aufnehmen",
        "Index texture sidecars for DDS related-file discovery": "Textur-Sidecars fuer DDS-Dateiverwandtschaft indexieren",
        "Inject missing base/color parameter (risky)": "Fehlenden Basis-/Farbparameter einfuegen (riskant)",
        "Invert Mask": "Maske invertieren",
        "JSON Mod Manager": "JSON Mod Manager",
        "Keep Original": "Original behalten",
    },
}

for _language_code, _translations in _REVIEWED_UI_TRANSLATIONS.items():
    _payload = BUILTIN_LANGUAGES.get(_language_code)
    if isinstance(_payload, dict) and isinstance(_payload.get("translations"), dict):
        _payload["translations"].update(_translations)

_FALLBACK_EXACT_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "A+": "A+",
        "Comp": "Comp",
        "Crimson Desert Mod Workbench": "Crimson Desert Mod Workbench",
        "Crimson Sharp / Crimson Browser": "Crimson Sharp / Crimson Browser",
        "Crosshatch": "Tramado cruzado",
        "Curves": "Curvas",
        "Custom": "Personalizado",
        "Depth": "Profundidad",
        "Detail": "Detalle",
        "Diamond": "Diamante",
        "Example: -dn 0.2": "Ejemplo: -dn 0.2",
        "Expected NCNN model contents": "Contenido esperado del modelo NCNN",
        "Exact": "Exacto",
        "Exact Item Name": "Nombre exacto de item",
        "Existing PNG folder": "Carpeta PNG existente",
        "Expand a category on the left, then select a built-in or detected local NCNN model to review it.": "Expande una categoria a la izquierda y selecciona un modelo NCNN integrado o detectado localmente para revisarlo.",
        "Expand to view additional models found in your configured NCNN model folder.": "Expande para ver modelos adicionales encontrados en tu carpeta de modelos NCNN configurada.",
        "Expert-only override. Forces technical textures such as normals, masks, roughness, height, and vectors onto the generic visible-color PNG/upscale path instead of preserving them. This can produce broken normals, bad masks, or incorrect shading.": "Anulacion solo para expertos. Fuerza texturas tecnicas como normales, mascaras, rugosidad, altura y vectores por la ruta PNG/escalado generica de color visible en vez de preservarlas. Puede producir normales rotas, malas mascaras o sombreado incorrecto.",
        "Flat": "Plano",
        "Gamma x100": "Gamma x100",
        "Gaussian Blur": "Desenfoque gaussiano",
        "GetRect": "Obtener rect",
        "Global font size (8-15 px)": "Tamano de fuente global (8-15 px)",
        "Hard Block": "Bloque duro",
        "Hatch": "Tramado",
        "Heat": "Calor",
        "Highlights": "Altas luces",
        "Impostors": "Impostores",
        "Intersect": "Intersectar",
        "Later": "Mas tarde",
        "Manual": "Manual",
        "Manual / no preset": "Manual / sin preajuste",
        "Mesh Replacement": "Reemplazo de malla",
        "No Changes": "Sin cambios",
        "Name Match": "Coincidencia de nombre",
        "Override wins": "Gana anulacion",
        "Parent export root is required.": "Se requiere la raiz padre de exportacion.",
        "Round-trip Edit": "Edicion de ida y vuelta",
        "Related/inferred": "Relacionado/inferido",
        "Select Files": "Seleccionar archivos",
        "Select Folder": "Seleccionar carpeta",
        "Source image size": "Tamano de imagen de origen",
        "Swap With In-Game Mesh...": "Intercambiar con malla del juego...",
        "Use as Swap Source...": "Usar como origen de intercambio...",
        "Start In-Game Mesh Swap...": "Iniciar intercambio de malla del juego...",
        "Cancel In-Game Mesh Swap Target": "Cancelar destino de intercambio de malla del juego",
        "In-Game Mesh Swap Scope": "Alcance de intercambio de malla del juego",
        "In-Game Mesh Swap": "Intercambio de malla del juego",
        "In-Game Mesh Swap Setup": "Configuracion de intercambio de malla del juego",
        "In-Game Mesh Swap Placement": "Colocacion de intercambio de malla del juego",
        "Lists / columns font size (8-15 px)": "Tamano de fuente de listas / columnas (8-15 px)",
        "Target language": "Idioma de destino",
        "Texture source probe": "Sonda de origen de textura",
        "Window": "Ventana",
        "Detach Current Tab": "Separar pestana actual",
        "Attach Current Tool": "Acoplar herramienta actual",
        "Attach All Tools": "Acoplar todas las herramientas",
        "Show Texture Workflow": "Mostrar flujo de texturas",
        "Show Replace Assistant": "Mostrar asistente de reemplazo",
        "Show Texture Editor": "Mostrar editor de texturas",
        "Show Archive Browser": "Mostrar explorador de archivos",
        "Show Research": "Mostrar investigacion",
        "Show Text Search": "Mostrar busqueda de texto",
        "Show Window": "Mostrar ventana",
        "Attach Back": "Volver a acoplar",
        "Use Show Window to bring it forward, or Attach Back to return it to this tab.": "Usa Mostrar ventana para traerla al frente, o Volver a acoplar para devolverla a esta pestana.",
        "Rebuilt DDS folder": "Carpeta DDS reconstruida",
        "Shortcuts": "Atajos",
        "Texture Editor Shortcuts": "Atajos del editor de texturas",
        "Undo": "Deshacer",
        "Redo": "Rehacer",
        "Paint tool active. Brush presets, image stamps, patterns, and symmetry are available here. Alt+click samples a color into the paint swatch.": "Herramienta de pintura activa. Aqui estan disponibles preajustes de pincel, sellos de imagen, patrones y simetria. Alt+clic toma una muestra de color en la muestra de pintura.",
        "These preferences are stored in the local config beside the EXE and apply across sessions.": "Estas preferencias se guardan en la configuracion local junto al EXE y se aplican en todas las sesiones.",
        "Use Suggested": "Usar sugerido",
    },
    "de": {
        "A+": "A+",
        "Comp": "Komp.",
        "Crimson Desert Mod Workbench": "Crimson Desert Mod Workbench",
        "Crimson Sharp / Crimson Browser": "Crimson Sharp / Crimson Browser",
        "Crosshatch": "Kreuzschraffur",
        "Curves": "Kurven",
        "Custom": "Benutzerdefiniert",
        "Depth": "Tiefe",
        "Detail": "Detail",
        "Diamond": "Diamant",
        "Example: -dn 0.2": "Beispiel: -dn 0.2",
        "Expected NCNN model contents": "Erwarteter NCNN-Modellinhalt",
        "Exact": "Exakt",
        "Exact Item Name": "Exakter Item-Name",
        "Existing PNG folder": "Vorhandener PNG-Ordner",
        "Expand a category on the left, then select a built-in or detected local NCNN model to review it.": "Links eine Kategorie erweitern und dann ein integriertes oder lokal erkanntes NCNN-Modell zur Pruefung auswaehlen.",
        "Expand to view additional models found in your configured NCNN model folder.": "Erweitern, um weitere Modelle aus dem konfigurierten NCNN-Modellordner anzuzeigen.",
        "Expert-only override. Forces technical textures such as normals, masks, roughness, height, and vectors onto the generic visible-color PNG/upscale path instead of preserving them. This can produce broken normals, bad masks, or incorrect shading.": "Override nur fuer Experten. Erzwingt technische Texturen wie Normalen, Masken, Rauheit, Hoehe und Vektoren auf den generischen sichtbaren PNG-/Upscale-Pfad statt sie zu erhalten. Das kann defekte Normalen, falsche Masken oder inkorrektes Shading erzeugen.",
        "Flat": "Flach",
        "Gamma x100": "Gamma x100",
        "Gaussian Blur": "Gausssche Unschaerfe",
        "GetRect": "Rechteck holen",
        "Global font size (8-15 px)": "Globale Schriftgroesse (8-15 px)",
        "Hard Block": "Harter Block",
        "Hatch": "Schraffur",
        "Heat": "Heat",
        "Highlights": "Highlights",
        "Impostors": "Impostors",
        "Intersect": "Schneiden",
        "Later": "Spaeter",
        "Manual": "Manuell",
        "Manual / no preset": "Manuell / kein Preset",
        "Mesh Replacement": "Mesh-Ersetzung",
        "No Changes": "Keine Aenderungen",
        "Name Match": "Namensabgleich",
        "Override wins": "Override gewinnt",
        "Parent export root is required.": "Eltern-Exportstamm ist erforderlich.",
        "Round-trip Edit": "Roundtrip-Bearbeitung",
        "Related/inferred": "Verwandt/abgeleitet",
        "Select Files": "Dateien auswaehlen",
        "Select Folder": "Ordner auswaehlen",
        "Source image size": "Quellbildgroesse",
        "Swap With In-Game Mesh...": "Mit Ingame-Mesh tauschen...",
        "Use as Swap Source...": "Als Swap-Quelle verwenden...",
        "Start In-Game Mesh Swap...": "Ingame-Mesh-Swap starten...",
        "Cancel In-Game Mesh Swap Target": "Ingame-Mesh-Swap-Ziel abbrechen",
        "In-Game Mesh Swap Scope": "Umfang des Ingame-Mesh-Swaps",
        "In-Game Mesh Swap": "Ingame-Mesh-Swap",
        "In-Game Mesh Swap Setup": "Ingame-Mesh-Swap-Einrichtung",
        "In-Game Mesh Swap Placement": "Ingame-Mesh-Swap-Platzierung",
        "Lists / columns font size (8-15 px)": "Schriftgroesse fuer Listen / Spalten (8-15 px)",
        "Target language": "Zielsprache",
        "Texture source probe": "Texturquellen-Probe",
        "Window": "Fenster",
        "Detach Current Tab": "Aktuellen Tab abtrennen",
        "Attach Current Tool": "Aktuelles Werkzeug andocken",
        "Attach All Tools": "Alle Werkzeuge andocken",
        "Show Texture Workflow": "Textur-Workflow anzeigen",
        "Show Replace Assistant": "Ersetzungsassistent anzeigen",
        "Show Texture Editor": "Textur-Editor anzeigen",
        "Show Archive Browser": "Archiv-Browser anzeigen",
        "Show Research": "Recherche anzeigen",
        "Show Text Search": "Textsuche anzeigen",
        "Show Window": "Fenster anzeigen",
        "Attach Back": "Wieder andocken",
        "Use Show Window to bring it forward, or Attach Back to return it to this tab.": "Mit Fenster anzeigen bringst du es nach vorn; mit Wieder andocken kehrt es in diesen Tab zurueck.",
        "Rebuilt DDS folder": "Neu erstellter DDS-Ordner",
        "Shortcuts": "Tastenkurzel",
        "Texture Editor Shortcuts": "Tastenkurzel des Textur-Editors",
        "Undo": "Rueckgaengig",
        "Redo": "Wiederholen",
        "Paint tool active. Brush presets, image stamps, patterns, and symmetry are available here. Alt+click samples a color into the paint swatch.": "Malwerkzeug aktiv. Pinsel-Presets, Bildstempel, Muster und Symmetrie sind hier verfuegbar. Alt+Klick uebernimmt eine Farbe in das Farbfeld.",
        "These preferences are stored in the local config beside the EXE and apply across sessions.": "Diese Einstellungen werden in der lokalen Konfiguration neben der EXE gespeichert und gelten sitzungsuebergreifend.",
        "Use Suggested": "Vorschlag verwenden",
    },
}

_FALLBACK_WORD_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "es": {
        "Above": "Arriba",
        "Add": "Agregar",
        "Advanced": "Avanzado",
        "Alpha": "Alfa",
        "Angle": "Angulo",
        "Apply": "Aplicar",
        "Archive": "Archivo",
        "Author": "Autor",
        "Below": "Abajo",
        "Blue": "Azul",
        "Brightness": "Brillo",
        "Cancel": "Cancelar",
        "Category": "Categoria",
        "Channel": "Canal",
        "Choose": "Elegir",
        "Clear": "Limpiar",
        "Clone": "Clonar",
        "Color": "Color",
        "Columns": "Columnas",
        "Content": "Contenido",
        "Contents": "Contenido",
        "Copy": "Copiar",
        "Current": "Actual",
        "Depth": "Profundidad",
        "Description": "Descripcion",
        "Detail": "Detalle",
        "Details": "Detalles",
        "Diffuse": "Difuso",
        "Disable": "Desactivar",
        "Down": "Abajo",
        "Edit": "Editar",
        "Edited": "Editado",
        "Empty": "Vacio",
        "Enable": "Activar",
        "Export": "Exportar",
        "Exposure": "Exposicion",
        "File": "Archivo",
        "Files": "Archivos",
        "Filter": "Filtro",
        "Folder": "Carpeta",
        "Folders": "Carpetas",
        "Generate": "Generar",
        "Green": "Verde",
        "Grid": "Cuadricula",
        "Group": "Grupo",
        "Groups": "Grupos",
        "Height": "Altura",
        "Import": "Importar",
        "Layer": "Capa",
        "Left": "Izquierda",
        "Light": "Luz",
        "Local": "Local",
        "Manual": "Manual",
        "Mask": "Mascara",
        "Material": "Material",
        "Mesh": "Malla",
        "Mode": "Modo",
        "Model": "Modelo",
        "Move": "Mover",
        "Name": "Nombre",
        "Normal": "Normal",
        "Opacity": "Opacidad",
        "Options": "Opciones",
        "Original": "Original",
        "Output": "Salida",
        "Override": "Anulacion",
        "Package": "Paquete",
        "Path": "Ruta",
        "Paths": "Rutas",
        "Preview": "Vista previa",
        "Quality": "Calidad",
        "Ready": "Listo",
        "Red": "Rojo",
        "Replacement": "Reemplazo",
        "Reset": "Restablecer",
        "Resize": "Redimensionar",
        "Right": "Derecha",
        "Root": "Raiz",
        "Rotate": "Rotar",
        "Scale": "Escala",
        "Search": "Buscar",
        "Selected": "Seleccionado",
        "Selection": "Seleccion",
        "Settings": "Configuracion",
        "Shading": "Sombreado",
        "Size": "Tamano",
        "Slot": "Ranura",
        "Source": "Origen",
        "Status": "Estado",
        "Structure": "Estructura",
        "Target": "Destino",
        "Texture": "Textura",
        "Textures": "Texturas",
        "Tool": "Herramienta",
        "Tools": "Herramientas",
        "Up": "Arriba",
        "Values": "Valores",
        "View": "Vista",
        "Warning": "Advertencia",
        "Warnings": "Advertencias",
        "Zoom": "Zoom",
    },
    "de": {
        "Above": "Oben",
        "Add": "Hinzufuegen",
        "Advanced": "Erweitert",
        "Alpha": "Alpha",
        "Angle": "Winkel",
        "Apply": "Anwenden",
        "Archive": "Archiv",
        "Author": "Autor",
        "Below": "Unten",
        "Blue": "Blau",
        "Brightness": "Helligkeit",
        "Cancel": "Abbrechen",
        "Category": "Kategorie",
        "Channel": "Kanal",
        "Choose": "Waehlen",
        "Clear": "Leeren",
        "Clone": "Klonen",
        "Color": "Farbe",
        "Columns": "Spalten",
        "Content": "Inhalt",
        "Contents": "Inhalt",
        "Copy": "Kopieren",
        "Current": "Aktuell",
        "Depth": "Tiefe",
        "Description": "Beschreibung",
        "Detail": "Detail",
        "Details": "Details",
        "Diffuse": "Diffus",
        "Disable": "Deaktivieren",
        "Down": "Runter",
        "Edit": "Bearbeiten",
        "Edited": "Bearbeitet",
        "Empty": "Leer",
        "Enable": "Aktivieren",
        "Export": "Exportieren",
        "Exposure": "Belichtung",
        "File": "Datei",
        "Files": "Dateien",
        "Filter": "Filter",
        "Folder": "Ordner",
        "Folders": "Ordner",
        "Generate": "Generieren",
        "Green": "Gruen",
        "Grid": "Raster",
        "Group": "Gruppe",
        "Groups": "Gruppen",
        "Height": "Hoehe",
        "Import": "Importieren",
        "Layer": "Ebene",
        "Left": "Links",
        "Light": "Licht",
        "Local": "Lokal",
        "Manual": "Manuell",
        "Mask": "Maske",
        "Material": "Material",
        "Mesh": "Mesh",
        "Mode": "Modus",
        "Model": "Modell",
        "Move": "Verschieben",
        "Name": "Name",
        "Normal": "Normal",
        "Opacity": "Deckkraft",
        "Options": "Optionen",
        "Original": "Original",
        "Output": "Ausgabe",
        "Override": "Override",
        "Package": "Paket",
        "Path": "Pfad",
        "Paths": "Pfade",
        "Preview": "Vorschau",
        "Quality": "Qualitaet",
        "Ready": "Bereit",
        "Red": "Rot",
        "Replacement": "Ersetzung",
        "Reset": "Zuruecksetzen",
        "Resize": "Groesse aendern",
        "Right": "Rechts",
        "Root": "Stamm",
        "Rotate": "Drehen",
        "Scale": "Skalierung",
        "Search": "Suchen",
        "Selected": "Ausgewaehlt",
        "Selection": "Auswahl",
        "Settings": "Einstellungen",
        "Shading": "Shading",
        "Size": "Groesse",
        "Slot": "Slot",
        "Source": "Quelle",
        "Status": "Status",
        "Structure": "Struktur",
        "Target": "Ziel",
        "Texture": "Textur",
        "Textures": "Texturen",
        "Tool": "Werkzeug",
        "Tools": "Werkzeuge",
        "Up": "Hoch",
        "Values": "Werte",
        "View": "Ansicht",
        "Warning": "Warnung",
        "Warnings": "Warnungen",
        "Zoom": "Zoom",
    },
}


def _fallback_builtin_translation(language_code: str, text: str) -> str:
    code = str(language_code or "").strip().lower()
    value = str(text or "")
    if code not in _FALLBACK_WORD_TRANSLATIONS or not _looks_like_translatable_text(value):
        return value
    if re.search(r"[{}\\]", value):
        return value
    exact = _FALLBACK_EXACT_TRANSLATIONS.get(code, {}).get(value)
    if exact:
        return exact
    if len(value) > 120:
        return value
    words = _FALLBACK_WORD_TRANSLATIONS.get(code, {})

    def replace_word(match: re.Match[str]) -> str:
        word = match.group(0)
        return words.get(word, word)

    translated = re.sub(r"\b[A-Za-z][A-Za-z-]*\b", replace_word, value)
    return translated if translated != value else value


def language_name_for_code(code: str) -> str:
    payload = BUILTIN_LANGUAGES.get(str(code or "").strip())
    if isinstance(payload, dict):
        return str(payload.get("language_name", code) or code)
    return str(code or "Custom")


def _canonical_source_text(value: str) -> str:
    """Return the English source key when a widget currently contains a built-in translation."""
    text = str(value or "")
    if not text:
        return text
    for payload in BUILTIN_LANGUAGES.values():
        translations = payload.get("translations") if isinstance(payload, dict) else None
        if not isinstance(translations, dict):
            continue
        for source, translated in translations.items():
            if text == translated:
                return str(source)
    return text


def _coerce_translation_payload(payload: object) -> Tuple[str, str, Dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("Language file must be a JSON object.")
    code = str(payload.get("language_code") or payload.get("code") or "custom").strip() or "custom"
    name = str(payload.get("language_name") or payload.get("name") or code).strip() or code
    translations_raw = payload.get("translations", payload)
    if not isinstance(translations_raw, dict):
        raise ValueError("Language file must contain a translations object.")
    translations = {
        str(key): str(value)
        for key, value in translations_raw.items()
        if isinstance(key, str) and str(value).strip()
    }
    return code, name, translations


def load_language_file(path: Path) -> Tuple[str, str, Dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _coerce_translation_payload(payload)


def write_language_file(
    path: Path,
    *,
    language_code: str,
    language_name: str,
    translations: Mapping[str, str],
) -> None:
    payload = {
        "language_code": language_code,
        "language_name": language_name,
        "warning": LANGUAGE_WARNING,
        "translations": dict(sorted((str(k), str(v)) for k, v in translations.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class UiLocalizer:
    def __init__(self, *, language_dir: Path, language_code: str = "en") -> None:
        self.language_dir = language_dir
        self.language_code = language_code or "en"
        self.language_name = language_name_for_code(self.language_code)
        self.translations: Dict[str, str] = {}
        self.load_language(self.language_code)

    def available_languages(self) -> Tuple[Tuple[str, str], ...]:
        languages = [(code, language_name_for_code(code)) for code in ("en", "es", "de")]
        for language_file in sorted(self.language_dir.glob("*.json")) if self.language_dir.is_dir() else ():
            try:
                code, name, _translations = load_language_file(language_file)
            except Exception:
                continue
            if code not in {item[0] for item in languages}:
                languages.append((code, name))
        return tuple(languages)

    def load_language(self, code: str) -> None:
        normalized_code = str(code or "en").strip() or "en"
        self.language_code = normalized_code
        self.language_name = language_name_for_code(normalized_code)
        self.translations = {}
        builtin = BUILTIN_LANGUAGES.get(normalized_code)
        if isinstance(builtin, dict):
            self.language_name = str(builtin.get("language_name", self.language_name) or self.language_name)
            raw_translations = builtin.get("translations", {})
            if isinstance(raw_translations, dict):
                self.translations.update({str(k): str(v) for k, v in raw_translations.items()})
        language_file = self.language_dir / f"{normalized_code}.json"
        if language_file.is_file():
            _code, name, translations = load_language_file(language_file)
            self.language_name = name
            self.translations.update(translations)

    def import_language_file(self, source_path: Path) -> Tuple[str, str, Path]:
        code, name, translations = load_language_file(source_path)
        safe_code = "".join(ch for ch in code.lower() if ch.isalnum() or ch in {"-", "_"}) or "custom"
        self.language_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.language_dir / f"{safe_code}.json"
        write_language_file(
            target_path,
            language_code=safe_code,
            language_name=name,
            translations=translations,
        )
        self.load_language(safe_code)
        return safe_code, name, target_path

    def translate(self, text: str) -> str:
        value = str(text or "")
        if not value or self.language_code == "en":
            return value
        return self.translations.get(value, _fallback_builtin_translation(self.language_code, value))

    def collect_source_strings(self, root: QWidget) -> Dict[str, str]:
        strings: Dict[str, str] = {}

        def add(value: str) -> None:
            for text in _extract_html_text_segments(str(value or "")):
                if _looks_like_translatable_text(text):
                    strings.setdefault(text, self.translations.get(text, ""))

        def source_or_current(obj: object, property_name: str, current_value: str) -> str:
            key = f"_i18n_source_{property_name}"
            existing = obj.property(key) if hasattr(obj, "property") else None
            if isinstance(existing, str):
                return existing
            return str(current_value or "")

        for widget in [root, *root.findChildren(QWidget)]:
            for attr_name, property_name in (
                ("text", "text"),
                ("title", "title"),
                ("toolTip", "tooltip"),
                ("placeholderText", "placeholder"),
                ("windowTitle", "window_title"),
            ):
                getter = getattr(widget, attr_name, None)
                if callable(getter):
                    try:
                        add(source_or_current(widget, property_name, getter()))
                    except Exception:
                        pass
            if isinstance(widget, QTabWidget):
                for index in range(widget.count()):
                    source = widget.property(f"_i18n_tab_source_{index}")
                    add(source if isinstance(source, str) else widget.tabText(index))
                    add(widget.tabToolTip(index))
            if isinstance(widget, QTreeWidget):
                header = widget.headerItem()
                if header is not None:
                    for column in range(widget.columnCount()):
                        source = widget.property(f"_i18n_tree_header_source_{column}")
                        add(source if isinstance(source, str) else header.text(column))
            if isinstance(widget, QTableWidget):
                for column in range(widget.columnCount()):
                    item = widget.horizontalHeaderItem(column)
                    if item is not None:
                        source = widget.property(f"_i18n_table_horizontal_header_source_{column}")
                        add(source if isinstance(source, str) else item.text())
                for row in range(widget.rowCount()):
                    item = widget.verticalHeaderItem(row)
                    if item is not None:
                        source = widget.property(f"_i18n_table_vertical_header_source_{row}")
                        add(source if isinstance(source, str) else item.text())
            if isinstance(widget, QListWidget) and widget.property("_i18n_translate_items"):
                for row in range(widget.count()):
                    item = widget.item(row)
                    if item is not None:
                        source = item.data(0x0100 + 1000)
                        add(source if isinstance(source, str) else item.text())
            if isinstance(widget, QTextBrowser):
                source = widget.property("_i18n_source_html")
                add(source if isinstance(source, str) else widget.toHtml())
            elif isinstance(widget, QTextEdit) and widget.isReadOnly():
                source = widget.property("_i18n_source_plain_text")
                add(source if isinstance(source, str) else widget.toPlainText())
            if isinstance(widget, QComboBox):
                if not self._should_translate_combo(widget):
                    continue
                for index in range(widget.count()):
                    source = widget.property(f"_i18n_combo_source_{index}")
                    add(source if isinstance(source, str) else widget.itemText(index))

        action_sources = self._iter_window_actions(root) if isinstance(root, QMainWindow) else root.findChildren(QAction)
        menu_sources = self._iter_window_menus(root) if isinstance(root, QMainWindow) else root.findChildren(QMenu)
        for action in action_sources:
            add(source_or_current(action, "text", action.text()))
            add(source_or_current(action, "tooltip", action.toolTip()))
        for menu in menu_sources:
            add(source_or_current(menu, "title", menu.title()))

        return strings

    def apply(self, root: QWidget) -> None:
        self._apply_widget_tree(root)
        action_sources = self._iter_window_actions(root) if isinstance(root, QMainWindow) else root.findChildren(QAction)
        menu_sources = self._iter_window_menus(root) if isinstance(root, QMainWindow) else root.findChildren(QMenu)
        for action in action_sources:
            self._apply_action(action)
        for menu in menu_sources:
            self._apply_menu(menu)

    def _iter_window_actions(self, window: QMainWindow) -> Iterable[QAction]:
        seen: Set[int] = set()

        def emit(action: QAction) -> Iterable[QAction]:
            action_id = id(action)
            if action_id in seen:
                return ()
            seen.add(action_id)
            return (action,)

        for action in window.findChildren(QAction):
            yield from emit(action)

        menu_bar = window.menuBar()
        if menu_bar is None:
            return
        pending = list(menu_bar.actions())
        while pending:
            action = pending.pop(0)
            yield from emit(action)
            menu = action.menu()
            if menu is not None:
                pending.extend(menu.actions())

    def _iter_window_menus(self, window: QMainWindow) -> Iterable[QMenu]:
        seen: Set[int] = set()
        for menu in window.findChildren(QMenu):
            menu_id = id(menu)
            if menu_id in seen:
                continue
            seen.add(menu_id)
            yield menu

        menu_bar = window.menuBar()
        if menu_bar is None:
            return
        pending = [action.menu() for action in menu_bar.actions() if action.menu() is not None]
        while pending:
            menu = pending.pop(0)
            if menu is None:
                continue
            menu_id = id(menu)
            if menu_id in seen:
                continue
            seen.add(menu_id)
            yield menu
            pending.extend(action.menu() for action in menu.actions() if action.menu() is not None)

    def _source_property(self, obj: object, property_name: str, current_value: str) -> str:
        key = f"_i18n_source_{property_name}"
        existing = obj.property(key) if hasattr(obj, "property") else None
        if isinstance(existing, str):
            source = _canonical_source_text(existing)
            if source != existing and hasattr(obj, "setProperty"):
                obj.setProperty(key, source)
            return source
        value = _canonical_source_text(str(current_value or ""))
        if hasattr(obj, "setProperty"):
            obj.setProperty(key, value)
        return value

    def _apply_setter(self, obj: object, property_name: str, getter_name: str, setter_name: str) -> None:
        getter = getattr(obj, getter_name, None)
        setter = getattr(obj, setter_name, None)
        if not callable(getter) or not callable(setter):
            return
        try:
            source = self._source_property(obj, property_name, getter())
            setter(self.translate(source))
        except Exception:
            return

    def _apply_widget_tree(self, root: QWidget) -> None:
        for widget in [root, *root.findChildren(QWidget)]:
            if isinstance(widget, (QLabel, QAbstractButton)):
                self._apply_setter(widget, "text", "text", "setText")
            if isinstance(widget, QGroupBox):
                self._apply_setter(widget, "title", "title", "setTitle")
            if isinstance(widget, QLineEdit):
                self._apply_setter(widget, "placeholder", "placeholderText", "setPlaceholderText")
            self._apply_setter(widget, "tooltip", "toolTip", "setToolTip")
            self._apply_setter(widget, "window_title", "windowTitle", "setWindowTitle")
            if isinstance(widget, QTabWidget):
                self._apply_tab_widget(widget)
            if isinstance(widget, QComboBox):
                self._apply_combo(widget)
            if isinstance(widget, QTreeWidget):
                self._apply_tree_headers(widget)
            if isinstance(widget, QTableWidget):
                self._apply_table_headers(widget)
            if isinstance(widget, QListWidget) and widget.property("_i18n_translate_items"):
                self._apply_list_items(widget)
            if isinstance(widget, QTextBrowser):
                self._apply_text_browser(widget)
            elif isinstance(widget, QTextEdit) and widget.isReadOnly():
                self._apply_readonly_text_edit(widget)

    def _apply_tab_widget(self, widget: QTabWidget) -> None:
        for index in range(widget.count()):
            source_key = f"_i18n_tab_source_{index}"
            source = widget.property(source_key)
            if not isinstance(source, str):
                source = _canonical_source_text(widget.tabText(index))
                widget.setProperty(source_key, source)
            else:
                source = _canonical_source_text(source)
                widget.setProperty(source_key, source)
            widget.setTabText(index, self.translate(source))

    def _apply_combo(self, widget: QComboBox) -> None:
        if not self._should_translate_combo(widget):
            return
        for index in range(widget.count()):
            source_key = f"_i18n_combo_source_{index}"
            source = widget.property(source_key)
            if not isinstance(source, str):
                source = _canonical_source_text(widget.itemText(index))
                widget.setProperty(source_key, source)
            else:
                source = _canonical_source_text(source)
                widget.setProperty(source_key, source)
            widget.setItemText(index, self.translate(source))

    def _should_translate_combo(self, widget: QComboBox) -> bool:
        if widget.property("_i18n_skip_combo_items"):
            return False
        if widget.property("_i18n_translate_combo_items"):
            return True
        if widget.count() <= 0:
            return False
        for index in range(widget.count()):
            if widget.itemData(index) is None:
                return False
        return True

    def _apply_tree_headers(self, widget: QTreeWidget) -> None:
        header = widget.headerItem()
        if header is None:
            return
        for column in range(widget.columnCount()):
            source_key = f"_i18n_tree_header_source_{column}"
            source = widget.property(source_key)
            if not isinstance(source, str):
                source = _canonical_source_text(header.text(column))
                widget.setProperty(source_key, source)
            else:
                source = _canonical_source_text(source)
                widget.setProperty(source_key, source)
            header.setText(column, self.translate(source))

    def _apply_table_headers(self, widget: QTableWidget) -> None:
        for column in range(widget.columnCount()):
            item = widget.horizontalHeaderItem(column)
            if item is None:
                continue
            source_key = f"_i18n_table_horizontal_header_source_{column}"
            source = widget.property(source_key)
            if not isinstance(source, str):
                source = _canonical_source_text(item.text())
                widget.setProperty(source_key, source)
            else:
                source = _canonical_source_text(source)
                widget.setProperty(source_key, source)
            item.setText(self.translate(source))
        for row in range(widget.rowCount()):
            item = widget.verticalHeaderItem(row)
            if item is None:
                continue
            source_key = f"_i18n_table_vertical_header_source_{row}"
            source = widget.property(source_key)
            if not isinstance(source, str):
                source = _canonical_source_text(item.text())
                widget.setProperty(source_key, source)
            else:
                source = _canonical_source_text(source)
                widget.setProperty(source_key, source)
            item.setText(self.translate(source))

    def _apply_list_items(self, widget: QListWidget) -> None:
        source_role = 0x0100 + 1000
        for row in range(widget.count()):
            item = widget.item(row)
            if item is None:
                continue
            source = item.data(source_role)
            if not isinstance(source, str):
                source = _canonical_source_text(item.text())
                item.setData(source_role, source)
            else:
                source = _canonical_source_text(source)
                item.setData(source_role, source)
            item.setText(self.translate(source))

    def _apply_text_browser(self, widget: QTextBrowser) -> None:
        localized_html = widget.property(f"_i18n_html_{self.language_code}")
        if isinstance(localized_html, str) and localized_html.strip():
            widget.setHtml(localized_html)
            return
        source = widget.property("_i18n_source_html")
        if not isinstance(source, str):
            return
        widget.setHtml(_translate_html_text(source, self.translate))

    def _apply_readonly_text_edit(self, widget: QTextEdit) -> None:
        if isinstance(widget, QPlainTextEdit):
            return
        source = widget.property("_i18n_source_plain_text")
        if not isinstance(source, str):
            source = widget.toPlainText()
            widget.setProperty("_i18n_source_plain_text", source)
        translated = self.translate(source)
        if translated != source:
            widget.setPlainText(translated)

    def _apply_action(self, action: QAction) -> None:
        self._apply_setter(action, "text", "text", "setText")
        self._apply_setter(action, "tooltip", "toolTip", "setToolTip")

    def _apply_menu(self, menu: QMenu) -> None:
        self._apply_setter(menu, "title", "title", "setTitle")
