from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cdmw.models import ArchiveEntry
from cdmw.modding.mesh_parser import ParsedMesh


@dataclass(slots=True)
class MeshImportPreflight:
    summary: str
    detail_lines: tuple[str, ...] = ()
    severity: str = "info"


def _format_count(value: int) -> str:
    return f"{max(0, int(value)):,}"


def _format_size(value: int) -> str:
    size = max(0, int(value))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024.0:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024.0 * 1024.0):.1f} MB"
    return f"{size / (1024.0 * 1024.0 * 1024.0):.2f} GB"


def build_mesh_import_preflight(
    entry: ArchiveEntry,
    scene_path: Path,
    *,
    replacement_mesh: ParsedMesh | None = None,
    original_mesh: ParsedMesh | None = None,
    import_diagnostics: Sequence[str] = (),
) -> MeshImportPreflight:
    source_size = 0
    try:
        source_size = scene_path.expanduser().resolve().stat().st_size
    except OSError:
        source_size = 0
    original_vertices = int(getattr(original_mesh, "total_vertices", 0) or 0)
    replacement_vertices = int(getattr(replacement_mesh, "total_vertices", 0) or 0)
    replacement_faces = int(getattr(replacement_mesh, "total_faces", 0) or 0)
    replacement_parts = len(getattr(replacement_mesh, "submeshes", ()) or ())
    total_work_units = max(original_vertices, 1) + max(replacement_vertices, 1) + max(replacement_faces, 1)
    severity = "info"
    warnings: list[str] = []
    if replacement_vertices >= 250_000 or replacement_faces >= 250_000:
        severity = "warning"
        warnings.append("Large replacement mesh; alignment and automatic mapping can take longer.")
    if source_size >= 256 * 1024 * 1024:
        severity = "warning"
        warnings.append("Large source file; import may be slow while the scene is parsed.")
    if import_diagnostics:
        warnings.extend(str(line) for line in import_diagnostics[:3])
    details = [
        f"Target: {entry.path}",
        f"Source file: {scene_path.name} ({_format_size(source_size)})",
        f"Original mesh: {_format_count(original_vertices)} vertices",
        f"Replacement mesh: {_format_count(replacement_vertices)} vertices, {_format_count(replacement_faces)} faces, {_format_count(replacement_parts)} part(s)",
        f"Estimated mapping work: {_format_count(total_work_units)} units",
    ]
    details.extend(warnings)
    summary = (
        "Preflight complete. Opening alignment with live progress."
        if severity == "info"
        else "Preflight complete with warnings. Opening alignment with live progress."
    )
    return MeshImportPreflight(summary=summary, detail_lines=tuple(details), severity=severity)

