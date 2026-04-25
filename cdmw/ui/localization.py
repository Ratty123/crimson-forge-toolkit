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
            "Enable high-quality shading by default": "Activar sombreado de alta calidad por defecto",
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
            "Enable high-quality shading by default": "Hochwertige Schattierung standardmaessig aktivieren",
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
        return self.translations.get(value, value)

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
