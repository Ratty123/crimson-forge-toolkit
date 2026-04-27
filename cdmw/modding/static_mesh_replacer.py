"""Static OBJ replacement path for PAC/PAM mesh payloads.

This module is intentionally separate from mesh_importer.py.  The importer
remains the strict round-trip edit path; this module maps arbitrary static OBJ
submeshes onto the original game draw sections and asks the binary builders to
serialize new vertex/index buffers.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .logging import get_logger
from .mesh_parser import ParsedMesh, SubMesh, _compute_smooth_normals, inspect_mesh_binary_layout

logger = get_logger("core.static_mesh_replacer")

_STATIC_REPLACEMENT_VERTEX_LIMIT = 65535


@dataclass
class StaticSubmeshMapping:
    target_submesh_index: int
    target_submesh_name: str
    source_submesh_indices: list[int]
    target_material_slot_index: int
    merge_sources: bool = True
    confidence_score: float = 0.0
    confidence_label: str = ""


@dataclass
class StaticReplacementTransform:
    rotate_xyz_degrees: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: float = 1.0
    scale_xyz: tuple[float, float, float] | None = None
    offset_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    fit_to_original_bbox: bool = False
    preserve_aspect_ratio: bool = True
    scale_to_original_length: bool = True
    alignment_mode: str = "auto_anchor"
    source_anchor: tuple[float, float, float] | None = None
    target_anchor: tuple[float, float, float] | None = None
    source_axis: tuple[float, float, float] | None = None
    target_axis: tuple[float, float, float] | None = None
    flip_source_axis: bool = False
    flip_target_axis: bool = False
    manual_adjustment: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class StaticSourcePartAdjustment:
    source_submesh_index: int
    enabled: bool = True
    offset_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotate_xyz_degrees: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale_xyz: tuple[float, float, float] = (1.0, 1.0, 1.0)
    uniform_scale: float = 1.0
    pivot_mode: str = "part_center"


@dataclass
class StaticOriginalPartCopy:
    original_submesh_index: int
    label: str = ""
    keep_original_placement: bool = True


@dataclass
class StaticTextureSlotOverride:
    target_texture_path: str
    source_path: str = ""
    slot_kind: str = ""
    target_material_name: str = ""
    enabled: bool = True


@dataclass
class StaticMeshReplacementOptions:
    transform: StaticReplacementTransform = field(default_factory=StaticReplacementTransform)
    submesh_mappings: list[StaticSubmeshMapping] = field(default_factory=list)
    material_mapping_mode: str = "source_driven_materials"
    allow_merge_source_submeshes: bool = True
    allow_empty_target_submeshes: bool = True
    rebuild_material_sidecar: bool = False
    enable_missing_base_color_parameters: bool = False
    texture_slot_overrides: list[StaticTextureSlotOverride] = field(default_factory=list)
    texture_output_size_mode: str = "source"
    source_part_adjustments: list[StaticSourcePartAdjustment] = field(default_factory=list)
    original_part_copies: list[StaticOriginalPartCopy] = field(default_factory=list)
    replace_lods: bool = False
    strict_static_only: bool = True


@dataclass
class StaticMeshReplacementReport:
    original_submesh_count: int = 0
    replacement_submesh_count: int = 0
    original_vertex_count: int = 0
    replacement_vertex_count: int = 0
    original_face_count: int = 0
    replacement_face_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    mapping_summary: list[str] = field(default_factory=list)
    alignment_summary: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_PART_HINTS: dict[str, tuple[str, ...]] = {
    "acc": ("acc", "accessory", "accent", "ornament", "spike", "trim", "detail", "circular", "circulares"),
    "accessory": ("acc", "accessory", "accent", "ornament", "spike", "trim", "detail", "circular", "circulares"),
    "armor": ("armor", "armour", "plate", "mail", "body", "chest", "torso"),
    "blade": ("blade", "edge", "body", "sword", "spike", "tip", "main", "cuchilla", "hoja"),
    "body": ("body", "main", "base", "core", "shell", "torso", "mesh"),
    "cape": ("cape", "cloth", "fabric", "cloak", "mantle"),
    "cloth": ("cloth", "fabric", "cape", "cloak", "skirt", "sleeve"),
    "edge": ("blade", "edge", "rim", "border", "trim", "borde"),
    "guard": ("guard", "crossguard", "handguard", "protector", "soporte"),
    "handle": ("handle", "hilt", "grip", "pommel", "shaft", "mango", "empunadura"),
    "helmet": ("helmet", "helm", "head", "mask", "face"),
    "hilt": ("handle", "hilt", "grip", "pommel"),
    "metal": ("metal", "steel", "iron", "armor", "plate", "trim"),
    "plate": ("plate", "armor", "armour", "metal", "shell"),
    "trim": ("trim", "edge", "accent", "acc", "border", "ornament"),
}

_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "borde": ("edge", "trim"),
    "bordecuadrado": ("edge", "trim"),
    "circular": ("acc", "detail"),
    "circulares": ("acc", "detail"),
    "cuchilla": ("blade",),
    "dtcirculares": ("acc", "detail"),
    "empunadura": ("handle", "hilt", "grip"),
    "hoja": ("blade",),
    "mango": ("handle", "hilt", "grip"),
    "punta": ("tip", "edge"),
    "soporte": ("guard", "support"),
    "soporteespada": ("guard", "support"),
}

_TOKEN_STOP_WORDS = {
    "cd",
    "phm",
    "pc",
    "sword",
    "weapon",
    "onehandweapon",
    "twohandweapon",
    "low",
    "high",
    "mesh",
    "mat",
    "material",
    "object",
    "cube",
    "default",
}

_GRIP_MARKER_NAMES = ("cdmw_anchor", "cdmw_grip_anchor", "cft_anchor", "cft_grip_anchor")
_TIP_MARKER_NAMES = ("cdmw_tip_anchor", "cft_tip_anchor")
_MARKER_NAMES = {*_GRIP_MARKER_NAMES, *_TIP_MARKER_NAMES}


def analyze_static_replacement(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    options: StaticMeshReplacementOptions | None = None,
) -> StaticMeshReplacementReport:
    """Analyze a replacement OBJ against an original parsed mesh."""
    normalized_options = options or StaticMeshReplacementOptions()
    effective_replacement_mesh, _preserve_source_indices = _replacement_mesh_with_original_part_copies(
        original_mesh,
        replacement_mesh,
        normalized_options.original_part_copies,
    )
    mappings = normalized_options.submesh_mappings or suggest_static_submesh_mappings(
        original_mesh,
        effective_replacement_mesh,
    )
    report = _base_report(original_mesh, effective_replacement_mesh)
    _append_mapping_summary(report, original_mesh, effective_replacement_mesh, mappings)
    _append_static_warnings(report, original_mesh, effective_replacement_mesh, mappings, normalized_options)
    _append_mapping_errors(report, original_mesh, effective_replacement_mesh, mappings, normalized_options)
    _append_alignment_summary(report, original_mesh, effective_replacement_mesh, normalized_options.transform)
    return report


def describe_static_placement_context(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
) -> list[str]:
    """Return user-facing placement values for manual static alignment."""
    original_axis = _dominant_axis(original_mesh) or "unknown"
    replacement_axis = _dominant_axis(replacement_mesh) or "unknown"
    original_anchor = _infer_grip_anchor(original_mesh)
    replacement_anchor = _find_marker_anchor_any(replacement_mesh, _GRIP_MARKER_NAMES) or _infer_grip_anchor(replacement_mesh)
    original_tip = _infer_tip_anchor(original_mesh)
    replacement_tip = _find_marker_anchor_any(replacement_mesh, _TIP_MARKER_NAMES) or _infer_tip_anchor(replacement_mesh)
    original_vertices = [vertex for submesh in original_mesh.submeshes for vertex in submesh.vertices]
    replacement_vertices = [
        vertex
        for submesh in replacement_mesh.submeshes
        if not _is_marker_submesh(submesh)
        for vertex in submesh.vertices
    ]
    original_min, original_max = _bbox(original_vertices)
    replacement_min, replacement_max = _bbox(replacement_vertices)
    original_axis_vec = _axis_vector(original_axis)
    replacement_axis_vec = _axis_vector(replacement_axis)
    original_length = _axis_length(original_mesh, original_axis_vec)
    replacement_length = _axis_length(replacement_mesh, replacement_axis_vec)
    fit_scale = original_length / replacement_length if replacement_length > 1e-8 and original_length > 1e-8 else 1.0
    return [
        f"Original bbox: min {_format_vec(original_min)} max {_format_vec(original_max)} dims {_format_vec(_dims(original_min, original_max))}",
        f"Replacement bbox: min {_format_vec(replacement_min)} max {_format_vec(replacement_max)} dims {_format_vec(_dims(replacement_min, replacement_max))}",
        f"Original axis/length: {original_axis.upper()} / {original_length:.5g}",
        f"Replacement axis/length: {replacement_axis.upper()} / {replacement_length:.5g}",
        f"Original inferred anchor: {_format_vec(original_anchor)}",
        f"Replacement inferred anchor: {_format_vec(replacement_anchor)}",
        f"Original inferred far end: {_format_vec(original_tip)}",
        f"Replacement inferred far end: {_format_vec(replacement_tip)}",
        f"Auto length scale: {fit_scale:.6g}",
    ]


def build_static_mesh_replacement(
    original_data: bytes,
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    options: StaticMeshReplacementOptions | None = None,
) -> tuple[bytes, StaticMeshReplacementReport]:
    """Build a static replacement PAC/PAM payload from an arbitrary OBJ mesh."""
    normalized_options = options or StaticMeshReplacementOptions()
    effective_replacement_mesh, _preserve_source_indices = _replacement_mesh_with_original_part_copies(
        original_mesh,
        replacement_mesh,
        normalized_options.original_part_copies,
    )
    mappings = normalized_options.submesh_mappings or suggest_static_submesh_mappings(
        original_mesh,
        effective_replacement_mesh,
    )
    normalized_options = copy.copy(normalized_options)
    normalized_options.submesh_mappings = mappings

    report = analyze_static_replacement(original_mesh, replacement_mesh, normalized_options)
    layout = inspect_mesh_binary_layout(original_data, original_mesh.path)
    report.warnings.extend(layout.warnings)

    if original_mesh.format.lower() == "pamlod":
        report.errors.append("Static replacement currently supports one selected PAC/PAM mesh payload, not PAMLOD.")
    if normalized_options.replace_lods:
        report.warnings.append("LOD replacement was requested, but this first version only replaces the selected mesh/LOD.")
    if report.errors:
        raise ValueError(_format_static_report_failure(report))

    working_mesh = _build_mapped_replacement_mesh(
        original_mesh,
        replacement_mesh,
        mappings,
        normalized_options,
    )

    fmt = original_mesh.format.lower()
    if fmt == "pac":
        from .mesh_importer import _build_pac_full_rebuild

        rebuilt = _build_pac_full_rebuild(original_mesh, working_mesh, original_data)
    elif fmt == "pam":
        from .mesh_importer import build_pam

        rebuilt = build_pam(working_mesh, original_data)
    else:
        report.errors.append(f"Unsupported static replacement mesh format: {original_mesh.format or 'unknown'}")
        raise ValueError(_format_static_report_failure(report))

    logger.info(
        "Built static mesh replacement for %s: %d -> %d submesh source(s), %d bytes",
        original_mesh.path,
        len(effective_replacement_mesh.submeshes),
        len(working_mesh.submeshes),
        len(rebuilt),
    )
    return rebuilt, report


def build_static_replacement_preview_mesh(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    options: StaticMeshReplacementOptions | None = None,
    *,
    max_source_faces_per_submesh: int | None = None,
) -> ParsedMesh:
    """Build the mapped/transformed preview mesh without serializing a PAC/PAM payload."""
    normalized_options = options or StaticMeshReplacementOptions()
    effective_replacement_mesh, _preserve_source_indices = _replacement_mesh_with_original_part_copies(
        original_mesh,
        replacement_mesh,
        normalized_options.original_part_copies,
    )
    mappings = normalized_options.submesh_mappings or suggest_static_submesh_mappings(
        original_mesh,
        effective_replacement_mesh,
    )
    mappings_by_target = {mapping.target_submesh_index: mapping for mapping in mappings}
    complete_mappings: list[StaticSubmeshMapping] = []
    for target_index, target in enumerate(original_mesh.submeshes):
        mapping = mappings_by_target.get(target_index)
        if mapping is not None:
            complete_mappings.append(mapping)
            continue
        complete_mappings.append(
            StaticSubmeshMapping(
                target_submesh_index=target_index,
                target_submesh_name=target.material or target.name or f"target {target_index}",
                source_submesh_indices=[],
                target_material_slot_index=target_index,
                merge_sources=True,
            )
        )
    normalized_options = copy.copy(normalized_options)
    normalized_options.submesh_mappings = complete_mappings
    return _build_mapped_replacement_mesh(
        original_mesh,
        replacement_mesh,
        complete_mappings,
        normalized_options,
        enforce_vertex_limit=False,
        max_source_faces_per_submesh=max_source_faces_per_submesh,
    )


def suggest_static_submesh_mappings(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
) -> list[StaticSubmeshMapping]:
    """Suggest source-to-target draw-section mappings using metadata and geometry.

    The first pass is intentionally generic: exact names/materials, token overlap,
    broad part aliases, relative position, and size similarity. Weapon-specific
    words are only one hint family among armor, cloth, trim, accessory, body, etc.
    """
    render_source_indices = [
        index
        for index, submesh in enumerate(replacement_mesh.submeshes)
        if not _is_marker_submesh(submesh)
    ]
    if not original_mesh.submeshes or not render_source_indices:
        return []
    if len(original_mesh.submeshes) == 1:
        return [
            StaticSubmeshMapping(
                target_submesh_index=0,
                target_submesh_name=original_mesh.submeshes[0].material or original_mesh.submeshes[0].name,
                source_submesh_indices=render_source_indices,
                target_material_slot_index=0,
                merge_sources=True,
            )
        ]

    spatial_cache = _StaticMappingSpatialCache()
    assignments: dict[int, list[int]] = {index: [] for index in range(len(original_mesh.submeshes))}
    for source_index in render_source_indices:
        source = replacement_mesh.submeshes[source_index]
        best_target, best_score = _best_target_match_for_source(
            source,
            original_mesh.submeshes,
            source_mesh=replacement_mesh,
            target_mesh=original_mesh,
            spatial_cache=spatial_cache,
        )
        assignments.setdefault(best_target, []).append(source_index)
    confidence_by_target_source: dict[tuple[int, int], float] = {}
    for target_index, source_indices in assignments.items():
        if target_index < 0 or target_index >= len(original_mesh.submeshes):
            continue
        target = original_mesh.submeshes[target_index]
        for source_index in source_indices:
            if source_index < 0 or source_index >= len(replacement_mesh.submeshes):
                continue
            confidence_by_target_source[(target_index, source_index)] = _token_score(
                _name_text(replacement_mesh.submeshes[source_index]),
                _name_text(target),
                source_submesh=replacement_mesh.submeshes[source_index],
                target_submesh=target,
                source_mesh=replacement_mesh,
                target_mesh=original_mesh,
                spatial_cache=spatial_cache,
            )

    for target_index, target in enumerate(original_mesh.submeshes):
        if assignments.get(target_index):
            continue
        donor_index = max(assignments, key=lambda index: len(assignments.get(index, ())))
        donor_sources = assignments.get(donor_index, [])
        if len(donor_sources) <= 1:
            continue
        stolen_source = max(
            donor_sources,
            key=lambda source_index: _token_score(
                _name_text(replacement_mesh.submeshes[source_index]),
                _name_text(target),
                source_submesh=replacement_mesh.submeshes[source_index],
                target_submesh=target,
                source_mesh=replacement_mesh,
                target_mesh=original_mesh,
                spatial_cache=spatial_cache,
            ),
        )
        donor_sources.remove(stolen_source)
        assignments[target_index] = [stolen_source]
        confidence_by_target_source[(target_index, stolen_source)] = _token_score(
            _name_text(replacement_mesh.submeshes[stolen_source]),
            _name_text(target),
            source_submesh=replacement_mesh.submeshes[stolen_source],
            target_submesh=target,
            source_mesh=replacement_mesh,
            target_mesh=original_mesh,
            spatial_cache=spatial_cache,
        )

    _rebalance_duplicate_material_assignments(
        assignments,
        confidence_by_target_source,
        original_mesh,
        replacement_mesh,
        spatial_cache=spatial_cache,
    )

    mappings: list[StaticSubmeshMapping] = []
    used_sources: set[int] = set()
    for target_index, target in enumerate(original_mesh.submeshes):
        source_indices = assignments.get(target_index, [])
        used_sources.update(source_indices)
        mappings.append(
            StaticSubmeshMapping(
                target_submesh_index=target_index,
                target_submesh_name=target.material or target.name,
                source_submesh_indices=source_indices,
                target_material_slot_index=target_index,
                merge_sources=True,
                confidence_score=_mapping_confidence_score(target_index, source_indices, confidence_by_target_source),
                confidence_label=_confidence_label(
                    _mapping_confidence_score(target_index, source_indices, confidence_by_target_source)
                ),
            )
        )

    unassigned = [
        index
        for index in render_source_indices
        if index not in used_sources
    ]
    if unassigned:
        largest_target = max(
            range(len(original_mesh.submeshes)),
            key=lambda index: len(original_mesh.submeshes[index].faces),
        )
        mappings[largest_target].source_submesh_indices.extend(unassigned)
        scores = [
            _token_score(
                _name_text(replacement_mesh.submeshes[source_index]),
                _name_text(original_mesh.submeshes[largest_target]),
                source_submesh=replacement_mesh.submeshes[source_index],
                target_submesh=original_mesh.submeshes[largest_target],
                source_mesh=replacement_mesh,
                target_mesh=original_mesh,
                spatial_cache=spatial_cache,
            )
            for source_index in unassigned
        ]
        if scores:
            mappings[largest_target].confidence_score = min(mappings[largest_target].confidence_score or scores[0], *scores)
            mappings[largest_target].confidence_label = _confidence_label(mappings[largest_target].confidence_score)
    return mappings


def _rebalance_duplicate_material_assignments(
    assignments: dict[int, list[int]],
    confidence_by_target_source: dict[tuple[int, int], float],
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    *,
    spatial_cache: "_StaticMappingSpatialCache | None" = None,
) -> None:
    targets_by_material: dict[str, list[int]] = {}
    for target_index, target in enumerate(original_mesh.submeshes):
        key = re.sub(r"[^a-z0-9]+", "", str(target.material or target.name or "").lower())
        if not key:
            continue
        targets_by_material.setdefault(key, []).append(target_index)

    for target_indices in targets_by_material.values():
        if len(target_indices) < 2:
            continue
        source_indices: list[int] = []
        seen_sources: set[int] = set()
        for target_index in target_indices:
            for source_index in assignments.get(target_index, []):
                if source_index not in seen_sources:
                    seen_sources.add(source_index)
                    source_indices.append(source_index)
        if len(source_indices) < 2:
            continue

        representative_target = original_mesh.submeshes[target_indices[0]]
        source_indices.sort(
            key=lambda source_index: _token_score(
                _name_text(replacement_mesh.submeshes[source_index]),
                _name_text(representative_target),
                source_submesh=replacement_mesh.submeshes[source_index],
                target_submesh=representative_target,
                source_mesh=replacement_mesh,
                target_mesh=original_mesh,
                spatial_cache=spatial_cache,
            ),
            reverse=True,
        )

        for target_index in target_indices:
            assignments[target_index] = []
        for ordinal, source_index in enumerate(source_indices):
            target_index = target_indices[min(ordinal, len(target_indices) - 1)]
            assignments.setdefault(target_index, []).append(source_index)
            target = original_mesh.submeshes[target_index]
            confidence_by_target_source[(target_index, source_index)] = _token_score(
                _name_text(replacement_mesh.submeshes[source_index]),
                _name_text(target),
                source_submesh=replacement_mesh.submeshes[source_index],
                target_submesh=target,
                source_mesh=replacement_mesh,
                target_mesh=original_mesh,
                spatial_cache=spatial_cache,
            )


def _base_report(original_mesh: ParsedMesh, replacement_mesh: ParsedMesh) -> StaticMeshReplacementReport:
    return StaticMeshReplacementReport(
        original_submesh_count=len(original_mesh.submeshes),
        replacement_submesh_count=len(replacement_mesh.submeshes),
        original_vertex_count=sum(len(sm.vertices) for sm in original_mesh.submeshes),
        replacement_vertex_count=sum(len(sm.vertices) for sm in replacement_mesh.submeshes),
        original_face_count=sum(len(sm.faces) for sm in original_mesh.submeshes),
        replacement_face_count=sum(len(sm.faces) for sm in replacement_mesh.submeshes),
    )


def _replacement_mesh_with_original_part_copies(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    original_part_copies: list[StaticOriginalPartCopy] | None,
) -> tuple[ParsedMesh, set[int]]:
    copies = list(original_part_copies or [])
    if not copies:
        return replacement_mesh, set()

    effective_mesh = copy.deepcopy(replacement_mesh)
    preserve_source_indices: set[int] = set()
    for copy_request in copies:
        try:
            original_index = int(copy_request.original_submesh_index)
        except (TypeError, ValueError):
            continue
        if original_index < 0 or original_index >= len(original_mesh.submeshes):
            continue
        copied_submesh = copy.deepcopy(original_mesh.submeshes[original_index])
        original_label = copied_submesh.material or copied_submesh.name or f"original {original_index}"
        copy_label = str(copy_request.label or "").strip() or f"{original_label} (original copy)"
        copied_submesh.name = copy_label
        if not copied_submesh.material:
            copied_submesh.material = original_label
        effective_mesh.submeshes.append(copied_submesh)
        copied_source_index = len(effective_mesh.submeshes) - 1
        if copy_request.keep_original_placement:
            preserve_source_indices.add(copied_source_index)

    all_vertices = [vertex for submesh in effective_mesh.submeshes for vertex in submesh.vertices]
    bbox_min, bbox_max = _bbox(all_vertices)
    effective_mesh.bbox_min = bbox_min
    effective_mesh.bbox_max = bbox_max
    effective_mesh.total_vertices = sum(len(submesh.vertices) for submesh in effective_mesh.submeshes)
    effective_mesh.total_faces = sum(len(submesh.faces) for submesh in effective_mesh.submeshes)
    effective_mesh.has_uvs = any(bool(submesh.uvs) for submesh in effective_mesh.submeshes)
    return effective_mesh, preserve_source_indices


def effective_static_replacement_source_mesh(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    options: StaticMeshReplacementOptions | None = None,
) -> ParsedMesh:
    """Return the replacement source mesh after appending copied original parts."""
    normalized_options = options or StaticMeshReplacementOptions()
    effective_mesh, _preserve_source_indices = _replacement_mesh_with_original_part_copies(
        original_mesh,
        replacement_mesh,
        normalized_options.original_part_copies,
    )
    return effective_mesh


def _append_mapping_summary(
    report: StaticMeshReplacementReport,
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    mappings: list[StaticSubmeshMapping],
) -> None:
    for mapping in mappings:
        if mapping.target_submesh_index >= len(original_mesh.submeshes):
            continue
        target = original_mesh.submeshes[mapping.target_submesh_index]
        source_labels = []
        for source_index in mapping.source_submesh_indices:
            if source_index >= len(replacement_mesh.submeshes):
                continue
            source = replacement_mesh.submeshes[source_index]
            source_labels.append(source.material or source.name or f"source {source_index}")
        if not source_labels:
            source_labels.append("(no replacement source)")
        confidence = str(mapping.confidence_label or "").strip()
        suffix = f" [{confidence} confidence]" if confidence and source_labels != ["(no replacement source)"] else ""
        report.mapping_summary.append(
            f"{' + '.join(source_labels)} -> {target.material or target.name or mapping.target_submesh_name}{suffix}"
        )


def _append_static_warnings(
    report: StaticMeshReplacementReport,
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    mappings: list[StaticSubmeshMapping],
    options: StaticMeshReplacementOptions,
) -> None:
    if len(original_mesh.submeshes) != len(replacement_mesh.submeshes):
        report.warnings.append(
            "Replacement submesh count differs from the original; source objects will be mapped/merged into original draw sections."
        )
    original_materials = {sm.material or sm.name for sm in original_mesh.submeshes if sm.material or sm.name}
    replacement_materials = {sm.material or sm.name for sm in replacement_mesh.submeshes if sm.material or sm.name}
    if len(replacement_materials) > len(original_materials):
        report.warnings.append(
            "Replacement uses more material names than the original; static replacement reuses original material slots."
        )
    if any(len(mapping.source_submesh_indices) > 1 for mapping in mappings):
        report.warnings.append("Multiple replacement submeshes will be merged into at least one original draw section.")
    low_confidence_mappings = [
        mapping
        for mapping in mappings
        if mapping.source_submesh_indices and _confidence_label(mapping.confidence_score) == "low"
    ]
    if low_confidence_mappings:
        examples = ", ".join(
            f"target {mapping.target_submesh_index} ({mapping.target_submesh_name})"
            for mapping in low_confidence_mappings[:4]
        )
        report.warnings.append(
            "Low-confidence static submesh mapping detected. Review the source index mapping before building; "
            f"examples: {examples}."
        )
    empty_targets = [mapping.target_submesh_index for mapping in mappings if not mapping.source_submesh_indices]
    if empty_targets:
        if options.allow_empty_target_submeshes:
            report.warnings.append(
                "Original draw section(s) with no replacement source will be emitted empty: "
                f"{empty_targets}."
            )
        else:
            report.warnings.append(
                "Original draw section(s) have no replacement source and empty output is disabled: "
                f"{empty_targets}."
            )
    if original_mesh.has_bones:
        report.warnings.append(
            "Original mesh has bone/weight data. Static replacement will clone compatible original vertex records; new skinning is not authored from OBJ."
        )

    original_axis = _dominant_axis(original_mesh)
    replacement_axis = _dominant_axis(replacement_mesh)
    if original_axis and replacement_axis and original_axis != replacement_axis:
        report.warnings.append(
            f"Replacement appears oriented along {replacement_axis.upper()}, while original appears oriented along {original_axis.upper()}."
        )
    if options.transform.fit_to_original_bbox:
        report.warnings.append("Replacement vertices will be fit to the original bounding box before serialization.")


def _append_mapping_errors(
    report: StaticMeshReplacementReport,
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    mappings: list[StaticSubmeshMapping],
    options: StaticMeshReplacementOptions,
) -> None:
    if not original_mesh.submeshes:
        report.errors.append("Original mesh has no parsed submeshes to replace.")
    if not replacement_mesh.submeshes:
        report.errors.append("Replacement OBJ has no parsed submeshes.")
    seen_targets: set[int] = set()
    seen_sources: set[int] = set()
    disabled_sources = {
        source_index
        for source_index, adjustment in _source_part_adjustments_by_index(options.source_part_adjustments).items()
        if not adjustment.enabled
    }
    for mapping in mappings:
        if mapping.target_submesh_index < 0 or mapping.target_submesh_index >= len(original_mesh.submeshes):
            report.errors.append(f"Mapping references invalid target submesh index {mapping.target_submesh_index}.")
            continue
        if mapping.target_submesh_index in seen_targets:
            report.errors.append(f"Target submesh {mapping.target_submesh_index} is mapped more than once.")
        seen_targets.add(mapping.target_submesh_index)
        if not mapping.source_submesh_indices and not options.allow_empty_target_submeshes:
            report.errors.append(f"Target submesh {mapping.target_submesh_index} has no replacement source submesh.")
        if len(mapping.source_submesh_indices) > 1 and not options.allow_merge_source_submeshes:
            report.errors.append(
                f"Target submesh {mapping.target_submesh_index} requires merging, but merging is disabled."
            )
        for source_index in mapping.source_submesh_indices:
            if source_index < 0 or source_index >= len(replacement_mesh.submeshes):
                report.errors.append(f"Mapping references invalid source submesh index {source_index}.")
            elif _is_marker_submesh(replacement_mesh.submeshes[source_index]):
                report.errors.append(f"Mapping references marker source submesh index {source_index}; marker objects are not render geometry.")
            elif source_index not in disabled_sources:
                seen_sources.add(source_index)
    missing_targets = set(range(len(original_mesh.submeshes))) - seen_targets
    if missing_targets:
        report.errors.append(f"Missing target mapping for original submesh index(es): {sorted(missing_targets)}.")
    render_source_indices = {
        index
        for index, source_submesh in enumerate(replacement_mesh.submeshes)
        if not _is_marker_submesh(source_submesh) and index not in disabled_sources
    }
    missing_sources = render_source_indices - seen_sources
    if missing_sources:
        report.warnings.append(f"Replacement source submesh index(es) not used by mapping: {sorted(missing_sources)}.")


def _build_mapped_replacement_mesh(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    mappings: list[StaticSubmeshMapping],
    options: StaticMeshReplacementOptions,
    *,
    enforce_vertex_limit: bool = True,
    max_source_faces_per_submesh: int | None = None,
) -> ParsedMesh:
    effective_replacement_mesh, preserve_source_indices = _replacement_mesh_with_original_part_copies(
        original_mesh,
        replacement_mesh,
        options.original_part_copies,
    )
    transformed_sources = _transformed_replacement_sources(
        original_mesh,
        effective_replacement_mesh,
        options.transform,
        options.source_part_adjustments,
        global_transform_exempt_indices=preserve_source_indices,
        max_source_faces_per_submesh=max_source_faces_per_submesh,
    )
    adjustments_by_index = _source_part_adjustments_by_index(options.source_part_adjustments)
    mapped_submeshes: list[SubMesh] = []
    mappings_by_target = {mapping.target_submesh_index: mapping for mapping in mappings}
    for target_index, target in enumerate(original_mesh.submeshes):
        mapping = mappings_by_target[target_index]
        source_parts = [
            copy.deepcopy(transformed_sources[source_index])
            for source_index in mapping.source_submesh_indices
            if (
                0 <= source_index < len(transformed_sources)
                and not _is_marker_submesh(transformed_sources[source_index])
                and adjustments_by_index.get(source_index, StaticSourcePartAdjustment(source_index)).enabled
            )
        ]
        merged = _merge_source_submeshes(source_parts, target)
        if enforce_vertex_limit and len(merged.vertices) > _STATIC_REPLACEMENT_VERTEX_LIMIT:
            raise ValueError(
                f"Static replacement target {target_index} has {len(merged.vertices):,} vertices; "
                f"current serializers use 16-bit indices and support at most {_STATIC_REPLACEMENT_VERTEX_LIMIT:,} vertices per target."
            )
        mapped_submeshes.append(merged)

    all_vertices = [vertex for submesh in mapped_submeshes for vertex in submesh.vertices]
    bbox_min, bbox_max = _bbox(all_vertices)
    return ParsedMesh(
        path=original_mesh.path,
        format=original_mesh.format,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        submeshes=mapped_submeshes,
        total_vertices=sum(len(sm.vertices) for sm in mapped_submeshes),
        total_faces=sum(len(sm.faces) for sm in mapped_submeshes),
        has_uvs=any(sm.uvs for sm in mapped_submeshes),
        has_bones=False,
    )


def _transformed_replacement_sources(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    transform: StaticReplacementTransform,
    source_part_adjustments: list[StaticSourcePartAdjustment] | None = None,
    global_transform_exempt_indices: set[int] | None = None,
    *,
    max_source_faces_per_submesh: int | None = None,
) -> list[SubMesh]:
    sources = [copy.deepcopy(submesh) for submesh in replacement_mesh.submeshes]
    if not sources:
        return sources
    max_preview_faces = _normalized_preview_face_limit(max_source_faces_per_submesh)
    if max_preview_faces > 0:
        sources = [_decimate_submesh_for_preview(submesh, max_preview_faces) for submesh in sources]

    adjustments_by_index = _source_part_adjustments_by_index(source_part_adjustments or [])
    for source_index, submesh in enumerate(sources):
        adjustment = adjustments_by_index.get(source_index)
        if adjustment is None or not adjustment.enabled or _is_marker_submesh(submesh):
            continue
        _apply_source_part_adjustment(submesh, adjustment)

    exempt_indices = set(global_transform_exempt_indices or set())
    transform_bound_sources = [
        submesh
        for source_index, submesh in enumerate(sources)
        if source_index not in exempt_indices
    ] or sources
    alignment_replacement_mesh = copy.copy(replacement_mesh)
    alignment_replacement_mesh.submeshes = [
        submesh
        for source_index, submesh in enumerate(sources)
        if source_index not in exempt_indices
    ] or list(sources)

    all_vertices = [vertex for submesh in transform_bound_sources for vertex in submesh.vertices]
    src_min, src_max = _bbox(all_vertices)
    dst_min, dst_max = _bbox([vertex for submesh in original_mesh.submeshes for vertex in submesh.vertices])
    alignment = _compute_anchor_alignment(original_mesh, alignment_replacement_mesh, transform)

    fit_scale_xyz = (1.0, 1.0, 1.0)
    fit_offset = (0.0, 0.0, 0.0)
    if transform.fit_to_original_bbox:
        src_dims = _dims(src_min, src_max)
        dst_dims = _dims(dst_min, dst_max)
        if transform.preserve_aspect_ratio:
            ratios = [
                dst_dims[index] / src_dims[index]
                for index in range(3)
                if src_dims[index] > 1e-8
            ]
            uniform = min(ratios) if ratios else 1.0
            fit_scale_xyz = (uniform, uniform, uniform)
        else:
            fit_scale_xyz = tuple(
                dst_dims[index] / src_dims[index] if src_dims[index] > 1e-8 else 1.0
                for index in range(3)
            )
        src_center = _center(src_min, src_max)
        dst_center = _center(dst_min, dst_max)
        fit_offset = tuple(dst_center[index] - src_center[index] * fit_scale_xyz[index] for index in range(3))

    for source_index, submesh in enumerate(sources):
        if source_index in exempt_indices:
            continue
        submesh.vertices = [
            _apply_transform(vertex, transform, fit_scale_xyz, fit_offset, alignment)
            for vertex in submesh.vertices
        ]
        if submesh.normals and len(submesh.normals) == len(submesh.vertices):
            submesh.normals = [
                _normalize(
                    _rotate_xyz(
                        _apply_alignment_roll(
                            _rotate_between(normal, alignment["source_axis"], alignment["target_axis"]),
                            alignment,
                        ),
                        transform.rotate_xyz_degrees,
                    )
                )
                for normal in submesh.normals
            ]
    return sources


def _source_part_adjustments_by_index(
    adjustments: list[StaticSourcePartAdjustment] | None,
) -> dict[int, StaticSourcePartAdjustment]:
    by_index: dict[int, StaticSourcePartAdjustment] = {}
    for adjustment in adjustments or []:
        try:
            source_index = int(adjustment.source_submesh_index)
        except Exception:
            continue
        if source_index >= 0:
            by_index[source_index] = adjustment
    return by_index


def _apply_source_part_adjustment(submesh: SubMesh, adjustment: StaticSourcePartAdjustment) -> None:
    if not submesh.vertices:
        return
    pivot = _center(*_bbox(submesh.vertices))
    sx, sy, sz = adjustment.scale_xyz or (1.0, 1.0, 1.0)
    uniform = float(adjustment.uniform_scale or 1.0)
    scale_xyz = (float(sx) * uniform, float(sy) * uniform, float(sz) * uniform)
    offset = tuple(float(value) for value in adjustment.offset_xyz)
    rotation = tuple(float(value) for value in adjustment.rotate_xyz_degrees)
    adjusted_vertices: list[tuple[float, float, float]] = []
    for vertex in submesh.vertices:
        local = (
            (float(vertex[0]) - pivot[0]) * scale_xyz[0],
            (float(vertex[1]) - pivot[1]) * scale_xyz[1],
            (float(vertex[2]) - pivot[2]) * scale_xyz[2],
        )
        rotated = _rotate_xyz(local, rotation)
        adjusted_vertices.append(
            (
                rotated[0] + pivot[0] + offset[0],
                rotated[1] + pivot[1] + offset[1],
                rotated[2] + pivot[2] + offset[2],
            )
        )
    submesh.vertices = adjusted_vertices
    if submesh.normals and len(submesh.normals) == len(submesh.vertices):
        submesh.normals = [_normalize(_rotate_xyz(normal, rotation)) for normal in submesh.normals]


def _merge_source_submeshes(submeshes: list[SubMesh], target: SubMesh) -> SubMesh:
    merged = SubMesh(
        name=target.name,
        material=target.material,
        texture=target.texture,
    )
    wants_uvs = any(len(submesh.uvs) == len(submesh.vertices) for submesh in submeshes)
    wants_normals = any(len(submesh.normals) == len(submesh.vertices) for submesh in submeshes)
    for submesh in submeshes:
        base = len(merged.vertices)
        merged.vertices.extend(copy.deepcopy(submesh.vertices))
        if wants_uvs:
            merged.uvs.extend(
                copy.deepcopy(submesh.uvs)
                if len(submesh.uvs) == len(submesh.vertices)
                else [(0.0, 0.0)] * len(submesh.vertices)
            )
        if wants_normals:
            merged.normals.extend(
                copy.deepcopy(submesh.normals)
                if len(submesh.normals) == len(submesh.vertices)
                else [(0.0, 1.0, 0.0)] * len(submesh.vertices)
            )
        for face in submesh.faces:
            if len(face) == 3:
                merged.faces.append((face[0] + base, face[1] + base, face[2] + base))
    if not merged.normals or len(merged.normals) != len(merged.vertices):
        merged.normals = _compute_smooth_normals(merged.vertices, merged.faces)
    merged.vertex_count = len(merged.vertices)
    merged.face_count = len(merged.faces)
    return merged


def _normalized_preview_face_limit(value: int | None) -> int:
    try:
        limit = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, limit)


def _decimate_submesh_for_preview(submesh: SubMesh, max_faces: int) -> SubMesh:
    faces = list(submesh.faces or [])
    if max_faces <= 0 or len(faces) <= max_faces:
        return submesh
    if not submesh.vertices:
        return submesh

    step = max(1, math.ceil(len(faces) / float(max_faces)))
    sampled_faces = faces[::step][:max_faces]
    source_to_preview: dict[int, int] = {}
    preview_vertices: list[tuple[float, float, float]] = []
    preview_faces: list[tuple[int, int, int]] = []

    for face in sampled_faces:
        remapped_face: list[int] = []
        for raw_index in face[:3]:
            try:
                source_index = int(raw_index)
            except (TypeError, ValueError):
                remapped_face = []
                break
            if source_index < 0 or source_index >= len(submesh.vertices):
                remapped_face = []
                break
            preview_index = source_to_preview.get(source_index)
            if preview_index is None:
                preview_index = len(preview_vertices)
                source_to_preview[source_index] = preview_index
                preview_vertices.append(submesh.vertices[source_index])
            remapped_face.append(preview_index)
        if len(remapped_face) == 3:
            preview_faces.append((remapped_face[0], remapped_face[1], remapped_face[2]))

    if not preview_faces:
        return submesh

    ordered_source_indices = [
        source_index
        for source_index, _preview_index in sorted(source_to_preview.items(), key=lambda item: item[1])
    ]
    preview = copy.deepcopy(submesh)
    preview.vertices = preview_vertices
    preview.faces = preview_faces
    preview.uvs = (
        [submesh.uvs[source_index] for source_index in ordered_source_indices]
        if len(submesh.uvs) == len(submesh.vertices)
        else []
    )
    preview.normals = (
        [submesh.normals[source_index] for source_index in ordered_source_indices]
        if len(submesh.normals) == len(submesh.vertices)
        else []
    )
    preview.bone_indices = (
        [submesh.bone_indices[source_index] for source_index in ordered_source_indices]
        if len(submesh.bone_indices) == len(submesh.vertices)
        else []
    )
    preview.bone_weights = (
        [submesh.bone_weights[source_index] for source_index in ordered_source_indices]
        if len(submesh.bone_weights) == len(submesh.vertices)
        else []
    )
    preview.source_vertex_map = (
        [submesh.source_vertex_map[source_index] for source_index in ordered_source_indices]
        if len(submesh.source_vertex_map) == len(submesh.vertices)
        else []
    )
    preview.vertex_count = len(preview.vertices)
    preview.face_count = len(preview.faces)
    preview.source_vertex_offsets = []
    preview.source_index_offset = -1
    preview.source_index_count = len(preview.faces) * 3
    return preview


def _best_target_index_for_source(
    source: SubMesh,
    targets: list[SubMesh],
    *,
    source_mesh: ParsedMesh | None = None,
    target_mesh: ParsedMesh | None = None,
    spatial_cache: "_StaticMappingSpatialCache | None" = None,
) -> int:
    best_index, _best_score = _best_target_match_for_source(
        source,
        targets,
        source_mesh=source_mesh,
        target_mesh=target_mesh,
        spatial_cache=spatial_cache,
    )
    return best_index


@dataclass
class _StaticMappingSpatialCache:
    mesh_bounds_by_id: dict[int, tuple[tuple[float, float, float], tuple[float, float, float]]] = field(default_factory=dict)
    submesh_center_by_id: dict[tuple[int, int], tuple[float, float, float] | None] = field(default_factory=dict)


def _best_target_match_for_source(
    source: SubMesh,
    targets: list[SubMesh],
    *,
    source_mesh: ParsedMesh | None = None,
    target_mesh: ParsedMesh | None = None,
    spatial_cache: _StaticMappingSpatialCache | None = None,
) -> tuple[int, float]:
    source_text = _name_text(source)
    best_index = 0
    best_score = float("-inf")
    for target_index, target in enumerate(targets):
        target_text = _name_text(target)
        score = _token_score(
            source_text,
            target_text,
            source_submesh=source,
            target_submesh=target,
            source_mesh=source_mesh,
            target_mesh=target_mesh,
            spatial_cache=spatial_cache,
        )
        if score > best_score:
            best_score = score
            best_index = target_index
    return best_index, best_score


def _mapping_confidence_score(
    target_index: int,
    source_indices: list[int],
    confidence_by_target_source: dict[tuple[int, int], float],
) -> float:
    scores = [
        confidence_by_target_source.get((target_index, source_index), 0.0)
        for source_index in source_indices
    ]
    return min(scores) if scores else 0.0


def _confidence_label(score: float) -> str:
    if score >= 18.0:
        return "high"
    if score >= 10.0:
        return "medium"
    return "low"


def _name_text(submesh: SubMesh) -> str:
    return f"{submesh.name} {submesh.material} {submesh.texture}".replace("_", " ").replace(".", " ").lower()


def _token_score(
    source_text: str,
    target_text: str,
    *,
    source_submesh: SubMesh | None = None,
    target_submesh: SubMesh | None = None,
    source_mesh: ParsedMesh | None = None,
    target_mesh: ParsedMesh | None = None,
    spatial_cache: _StaticMappingSpatialCache | None = None,
) -> float:
    source_tokens = _semantic_tokens(source_text)
    target_tokens = _semantic_tokens(target_text)
    score = 0.0
    if source_text.strip() and target_text.strip() and source_text.strip() == target_text.strip():
        score += 80.0
    if source_submesh is not None and target_submesh is not None:
        if _normalized_label(source_submesh.name) and _normalized_label(source_submesh.name) == _normalized_label(target_submesh.name):
            score += 60.0
        if _normalized_label(source_submesh.material) and _normalized_label(source_submesh.material) == _normalized_label(target_submesh.material):
            score += 70.0
    overlap = source_tokens & target_tokens
    score += float(len(overlap) * 8)
    if overlap:
        score += min(10.0, sum(len(token) for token in overlap) * 0.5)
    for target_token in target_tokens:
        hints = _PART_HINTS.get(target_token, ())
        if hints and any(hint in source_tokens or hint in source_text for hint in hints):
            score += 9.0
    for source_token in source_tokens:
        hints = _PART_HINTS.get(source_token, ())
        if hints and any(hint in target_tokens or hint in target_text for hint in hints):
            score += 5.0
    if source_submesh is not None and target_submesh is not None:
        score += _submesh_size_similarity_score(source_submesh, target_submesh)
    if source_submesh is not None and target_submesh is not None and source_mesh is not None and target_mesh is not None:
        score += _submesh_spatial_similarity_score(
            source_submesh,
            source_mesh,
            target_submesh,
            target_mesh,
            spatial_cache=spatial_cache,
        )
    return score


def _normalized_label(value: str) -> str:
    return " ".join(_semantic_tokens(value))


def _semantic_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    tokens: set[str] = set()
    for raw_token in normalized.split():
        token = raw_token.strip()
        if not token or token in _TOKEN_STOP_WORDS or token.isdigit():
            continue
        token = re.sub(r"\d+$", "", token)
        if len(token) <= 1 or token in _TOKEN_STOP_WORDS:
            continue
        tokens.add(token)
        for alias, expanded_tokens in _TOKEN_ALIASES.items():
            if alias in token:
                tokens.update(expanded_tokens)
    return tokens


def _submesh_size_similarity_score(source: SubMesh, target: SubMesh) -> float:
    source_faces = max(1, len(source.faces) or source.face_count)
    target_faces = max(1, len(target.faces) or target.face_count)
    face_ratio = min(source_faces, target_faces) / max(source_faces, target_faces)
    source_vertices = max(1, len(source.vertices) or source.vertex_count)
    target_vertices = max(1, len(target.vertices) or target.vertex_count)
    vertex_ratio = min(source_vertices, target_vertices) / max(source_vertices, target_vertices)
    return (face_ratio * 3.0) + (vertex_ratio * 2.0)


def _submesh_spatial_similarity_score(
    source: SubMesh,
    source_mesh: ParsedMesh,
    target: SubMesh,
    target_mesh: ParsedMesh,
    *,
    spatial_cache: _StaticMappingSpatialCache | None = None,
) -> float:
    source_center = _normalized_submesh_center(source, source_mesh, spatial_cache=spatial_cache)
    target_center = _normalized_submesh_center(target, target_mesh, spatial_cache=spatial_cache)
    if source_center is None or target_center is None:
        return 0.0
    distance = math.sqrt(sum((source_center[index] - target_center[index]) ** 2 for index in range(3)))
    return max(0.0, 8.0 - distance * 10.0)


def _normalized_submesh_center(
    submesh: SubMesh,
    mesh: ParsedMesh,
    *,
    spatial_cache: _StaticMappingSpatialCache | None = None,
) -> tuple[float, float, float] | None:
    if not submesh.vertices:
        return None
    cache_key = (id(mesh), id(submesh))
    if spatial_cache is not None and cache_key in spatial_cache.submesh_center_by_id:
        return spatial_cache.submesh_center_by_id[cache_key]
    mesh_bounds_key = id(mesh)
    if spatial_cache is not None and mesh_bounds_key in spatial_cache.mesh_bounds_by_id:
        mesh_min, mesh_max = spatial_cache.mesh_bounds_by_id[mesh_bounds_key]
    else:
        mesh_vertices = [
            vertex
            for candidate in mesh.submeshes
            if not _is_marker_submesh(candidate)
            for vertex in candidate.vertices
        ]
        if not mesh_vertices:
            if spatial_cache is not None:
                spatial_cache.submesh_center_by_id[cache_key] = None
            return None
        mesh_min, mesh_max = _bbox(mesh_vertices)
        if spatial_cache is not None:
            spatial_cache.mesh_bounds_by_id[mesh_bounds_key] = (mesh_min, mesh_max)
    mesh_dims = _dims(mesh_min, mesh_max)
    submesh_min, submesh_max = _bbox(submesh.vertices)
    center = _center(submesh_min, submesh_max)
    normalized_center = tuple(
        0.5 if mesh_dims[index] <= 1e-8 else (center[index] - mesh_min[index]) / mesh_dims[index]
        for index in range(3)
    )
    if spatial_cache is not None:
        spatial_cache.submesh_center_by_id[cache_key] = normalized_center
    return normalized_center


def _dominant_axis(mesh: ParsedMesh) -> str:
    vertices = [
        vertex
        for submesh in mesh.submeshes
        if not _is_marker_submesh(submesh)
        for vertex in submesh.vertices
    ]
    if not vertices:
        return ""
    bmin, bmax = _bbox(vertices)
    dims = _dims(bmin, bmax)
    axis_index = max(range(3), key=lambda index: dims[index])
    if dims[axis_index] <= 1e-8:
        return ""
    return ("x", "y", "z")[axis_index]


def _bbox(
    vertices: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not vertices:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xs, ys, zs = zip(*vertices)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _dims(
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(max(0.0, bmax[index] - bmin[index]) for index in range(3))


def _center(
    bmin: tuple[float, float, float],
    bmax: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple((bmin[index] + bmax[index]) * 0.5 for index in range(3))


def _apply_transform(
    vertex: tuple[float, float, float],
    transform: StaticReplacementTransform,
    fit_scale_xyz: tuple[float, float, float],
    fit_offset: tuple[float, float, float],
    alignment: dict[str, tuple[float, float, float] | float],
) -> tuple[float, float, float]:
    source_anchor = alignment["source_anchor"]
    target_anchor = alignment["target_anchor"]
    source_axis = alignment["source_axis"]
    target_axis = alignment["target_axis"]
    align_scale = float(alignment["scale"])
    centered = (
        vertex[0] - source_anchor[0],
        vertex[1] - source_anchor[1],
        vertex[2] - source_anchor[2],
    )
    x, y, z = _apply_alignment_roll(_rotate_between(centered, source_axis, target_axis), alignment)
    manual_scale = transform.scale_xyz or (transform.scale, transform.scale, transform.scale)
    x *= manual_scale[0] * align_scale * fit_scale_xyz[0]
    y *= manual_scale[1] * align_scale * fit_scale_xyz[1]
    z *= manual_scale[2] * align_scale * fit_scale_xyz[2]
    x, y, z = _rotate_xyz((x, y, z), transform.rotate_xyz_degrees)
    return (
        x + target_anchor[0] + fit_offset[0] + transform.offset_xyz[0] + transform.manual_adjustment[0],
        y + target_anchor[1] + fit_offset[1] + transform.offset_xyz[1] + transform.manual_adjustment[1],
        z + target_anchor[2] + fit_offset[2] + transform.offset_xyz[2] + transform.manual_adjustment[2],
    )


def _apply_alignment_roll(
    value: tuple[float, float, float],
    alignment: dict[str, tuple[float, float, float] | float],
) -> tuple[float, float, float]:
    roll_angle = float(alignment.get("roll_angle", 0.0) or 0.0)
    if abs(roll_angle) <= 1e-8:
        return value
    return _rotate_around_axis(value, alignment["target_axis"], roll_angle)


def _rotate_xyz(
    value: tuple[float, float, float],
    degrees: tuple[float, float, float],
) -> tuple[float, float, float]:
    x, y, z = value
    rx, ry, rz = (math.radians(deg) for deg in degrees)
    if abs(rx) > 1e-8:
        cy, sy = math.cos(rx), math.sin(rx)
        y, z = y * cy - z * sy, y * sy + z * cy
    if abs(ry) > 1e-8:
        cx, sx = math.cos(ry), math.sin(ry)
        x, z = x * cx + z * sx, -x * sx + z * cx
    if abs(rz) > 1e-8:
        cz, sz = math.cos(rz), math.sin(rz)
        x, y = x * cz - y * sz, x * sz + y * cz
    return x, y, z


def _normalize(value: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(value[0] * value[0] + value[1] * value[1] + value[2] * value[2])
    if length <= 1e-8:
        return (0.0, 1.0, 0.0)
    return (value[0] / length, value[1] / length, value[2] / length)


def _is_marker_submesh(submesh: SubMesh) -> bool:
    text = _name_text(submesh).replace(" ", "_")
    return any(marker in text for marker in _MARKER_NAMES)


def _find_marker_anchor(mesh: ParsedMesh, marker_name: str) -> tuple[float, float, float] | None:
    normalized_marker = marker_name.lower()
    for submesh in mesh.submeshes:
        text = _name_text(submesh).replace(" ", "_")
        if normalized_marker not in text or not submesh.vertices:
            continue
        return _centroid(submesh.vertices)
    return None


def _find_marker_anchor_any(mesh: ParsedMesh, marker_names: Iterable[str]) -> tuple[float, float, float] | None:
    for marker_name in marker_names:
        anchor = _find_marker_anchor(mesh, marker_name)
        if anchor is not None:
            return anchor
    return None


def _append_alignment_summary(
    report: StaticMeshReplacementReport,
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    transform: StaticReplacementTransform,
) -> None:
    alignment = _compute_anchor_alignment(original_mesh, replacement_mesh, transform)
    report.alignment_summary.extend(
        [
            f"mode={transform.alignment_mode or 'manual'}",
            f"source_anchor={_format_vec(alignment['source_anchor'])}",
            f"target_anchor={_format_vec(alignment['target_anchor'])}",
            f"source_axis={_format_vec(alignment['source_axis'])}",
            f"target_axis={_format_vec(alignment['target_axis'])}",
            f"scale={float(alignment['scale']):.6g}",
            f"scale_to_original_length={transform.scale_to_original_length}",
            f"auto_roll_degrees={math.degrees(float(alignment.get('roll_angle', 0.0) or 0.0)):.5g}",
        ]
    )
    if transform.flip_source_axis or transform.flip_target_axis:
        report.alignment_summary.append(
            "axis_flip="
            + ", ".join(
                label
                for enabled, label in (
                    (transform.flip_source_axis, "source"),
                    (transform.flip_target_axis, "target"),
                )
                if enabled
            )
        )


def _compute_anchor_alignment(
    original_mesh: ParsedMesh,
    replacement_mesh: ParsedMesh,
    transform: StaticReplacementTransform,
) -> dict[str, tuple[float, float, float] | float]:
    alignment_mode = str(transform.alignment_mode or "").strip().lower()
    if alignment_mode in {"manual", "none", "off"}:
        return {
            "source_anchor": transform.source_anchor or (0.0, 0.0, 0.0),
            "target_anchor": transform.target_anchor or (0.0, 0.0, 0.0),
            "source_axis": _normalize(transform.source_axis or _axis_vector(_dominant_axis(replacement_mesh))),
            "target_axis": _normalize(transform.target_axis or _axis_vector(_dominant_axis(original_mesh))),
            "scale": 1.0,
            "roll_angle": 0.0,
        }
    if alignment_mode in {"auto_fit", "auto_fit_original", "preserve_original", "bbox_center", "center"}:
        source_anchor = transform.source_anchor or _mesh_center_anchor(replacement_mesh)
        target_anchor = transform.target_anchor or _mesh_center_anchor(original_mesh)
        source_axis = transform.source_axis or _axis_vector(_dominant_axis(replacement_mesh))
        target_axis = transform.target_axis or _axis_vector(_dominant_axis(original_mesh))
        if transform.flip_source_axis:
            source_axis = (-source_axis[0], -source_axis[1], -source_axis[2])
        if transform.flip_target_axis:
            target_axis = (-target_axis[0], -target_axis[1], -target_axis[2])
        source_length = _axis_length(replacement_mesh, source_axis)
        target_length = _axis_length(original_mesh, target_axis)
        scale = (
            target_length / source_length
            if transform.scale_to_original_length and source_length > 1e-8 and target_length > 1e-8
            else 1.0
        )
        source_axis_normalized = _normalize(source_axis)
        target_axis_normalized = _normalize(target_axis)
        return {
            "source_anchor": source_anchor,
            "target_anchor": target_anchor,
            "source_axis": source_axis_normalized,
            "target_axis": target_axis_normalized,
            "scale": scale,
            "roll_angle": _auto_roll_angle(replacement_mesh, original_mesh, source_axis_normalized, target_axis_normalized),
        }

    source_anchor = transform.source_anchor or _find_marker_anchor_any(replacement_mesh, _GRIP_MARKER_NAMES) or _infer_grip_anchor(replacement_mesh)
    source_tip = _find_marker_anchor_any(replacement_mesh, _TIP_MARKER_NAMES)
    target_anchor = transform.target_anchor or _infer_grip_anchor(original_mesh)
    target_tip = _infer_tip_anchor(original_mesh)

    source_axis = transform.source_axis or (
        _normalize(_sub(source_tip, source_anchor)) if source_tip is not None else _axis_vector(_dominant_axis(replacement_mesh))
    )
    target_axis = transform.target_axis or (
        _normalize(_sub(target_tip, target_anchor)) if target_tip is not None else _axis_vector(_dominant_axis(original_mesh))
    )
    source_length = _axis_length(replacement_mesh, source_axis)
    target_length = _axis_length(original_mesh, target_axis)
    scale = (
        target_length / source_length
        if transform.scale_to_original_length and source_length > 1e-8 and target_length > 1e-8
        else 1.0
    )
    if transform.flip_source_axis:
        source_axis = (-source_axis[0], -source_axis[1], -source_axis[2])
    if transform.flip_target_axis:
        target_axis = (-target_axis[0], -target_axis[1], -target_axis[2])
    source_axis_normalized = _normalize(source_axis)
    target_axis_normalized = _normalize(target_axis)
    return {
        "source_anchor": source_anchor,
        "target_anchor": target_anchor,
        "source_axis": source_axis_normalized,
        "target_axis": target_axis_normalized,
        "scale": scale,
        "roll_angle": _auto_roll_angle(replacement_mesh, original_mesh, source_axis_normalized, target_axis_normalized),
    }


def _auto_roll_angle(
    replacement_mesh: ParsedMesh,
    original_mesh: ParsedMesh,
    source_axis: tuple[float, float, float],
    target_axis: tuple[float, float, float],
) -> float:
    source_secondary = _secondary_axis_vector(replacement_mesh, source_axis)
    target_secondary = _secondary_axis_vector(original_mesh, target_axis)
    rotated_source_secondary = _rotate_between(source_secondary, source_axis, target_axis)
    return _signed_angle_around_axis(rotated_source_secondary, target_secondary, target_axis)


def _secondary_axis_vector(mesh: ParsedMesh, primary_axis: tuple[float, float, float]) -> tuple[float, float, float]:
    vertices = [
        vertex
        for submesh in mesh.submeshes
        if not _is_marker_submesh(submesh)
        for vertex in submesh.vertices
    ]
    if not vertices:
        return (0.0, 1.0, 0.0)
    bmin, bmax = _bbox(vertices)
    dims = _dims(bmin, bmax)
    primary_index = max(range(3), key=lambda index: abs(primary_axis[index]))
    candidates = [index for index in range(3) if index != primary_index]
    secondary_index = max(candidates, key=lambda index: dims[index]) if candidates else 1
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))[secondary_index]


def _signed_angle_around_axis(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
    axis: tuple[float, float, float],
) -> float:
    normalized_axis = _normalize(axis)
    source_projected = _normalize(_sub(source, _mul(normalized_axis, _dot(source, normalized_axis))))
    target_projected = _normalize(_sub(target, _mul(normalized_axis, _dot(target, normalized_axis))))
    cross = _cross(source_projected, target_projected)
    sin_theta = _dot(normalized_axis, cross)
    cos_theta = max(-1.0, min(1.0, _dot(source_projected, target_projected)))
    return math.atan2(sin_theta, cos_theta)


def _mesh_center_anchor(mesh: ParsedMesh) -> tuple[float, float, float]:
    vertices = [
        vertex
        for submesh in mesh.submeshes
        if not _is_marker_submesh(submesh)
        for vertex in submesh.vertices
    ]
    if not vertices:
        return (0.0, 0.0, 0.0)
    return _center(*_bbox(vertices))


def _infer_grip_anchor(mesh: ParsedMesh) -> tuple[float, float, float]:
    handle = _find_named_part(mesh, ("handle", "hilt", "grip"))
    submeshes = [handle] if handle is not None else [sm for sm in mesh.submeshes if not _is_marker_submesh(sm)]
    vertices = [vertex for submesh in submeshes for vertex in submesh.vertices]
    if not vertices:
        return (0.0, 0.0, 0.0)
    axis = _axis_vector(_dominant_axis(_mesh_from_submeshes(mesh, submeshes)))
    return _axis_extreme_point(vertices, axis, minimum=True)


def _infer_tip_anchor(mesh: ParsedMesh) -> tuple[float, float, float]:
    vertices = [vertex for submesh in mesh.submeshes if not _is_marker_submesh(submesh) for vertex in submesh.vertices]
    if not vertices:
        return (0.0, 0.0, 1.0)
    axis = _axis_vector(_dominant_axis(mesh))
    return _axis_extreme_point(vertices, axis, minimum=False)


def _find_named_part(mesh: ParsedMesh, tokens: tuple[str, ...]) -> SubMesh | None:
    for submesh in mesh.submeshes:
        text = _name_text(submesh)
        if any(token in text for token in tokens):
            return submesh
    return None


def _mesh_from_submeshes(source: ParsedMesh, submeshes: list[SubMesh]) -> ParsedMesh:
    clone = ParsedMesh(path=source.path, format=source.format, submeshes=submeshes)
    return clone


def _axis_vector(axis_name: str) -> tuple[float, float, float]:
    return {
        "x": (1.0, 0.0, 0.0),
        "y": (0.0, 1.0, 0.0),
        "z": (0.0, 0.0, 1.0),
    }.get(str(axis_name or "").lower(), (0.0, 0.0, 1.0))


def _axis_extreme_point(
    vertices: list[tuple[float, float, float]],
    axis: tuple[float, float, float],
    *,
    minimum: bool,
) -> tuple[float, float, float]:
    normalized_axis = _normalize(axis)
    return min(vertices, key=lambda vertex: _dot(vertex, normalized_axis)) if minimum else max(vertices, key=lambda vertex: _dot(vertex, normalized_axis))


def _axis_length(mesh: ParsedMesh, axis: tuple[float, float, float]) -> float:
    vertices = [vertex for submesh in mesh.submeshes if not _is_marker_submesh(submesh) for vertex in submesh.vertices]
    if not vertices:
        return 1.0
    normalized_axis = _normalize(axis)
    values = [_dot(vertex, normalized_axis) for vertex in vertices]
    return max(values) - min(values)


def _rotate_between(
    value: tuple[float, float, float],
    source_axis: tuple[float, float, float],
    target_axis: tuple[float, float, float],
) -> tuple[float, float, float]:
    a = _normalize(source_axis)
    b = _normalize(target_axis)
    cos_theta = max(-1.0, min(1.0, _dot(a, b)))
    if cos_theta > 0.999999:
        return value
    if cos_theta < -0.999999:
        fallback = _normalize((1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0))
        axis = _normalize(_cross(a, fallback))
    else:
        axis = _normalize(_cross(a, b))
    angle = math.acos(cos_theta)
    return _rotate_around_axis(value, axis, angle)


def _rotate_around_axis(
    value: tuple[float, float, float],
    axis: tuple[float, float, float],
    angle: float,
) -> tuple[float, float, float]:
    ux, uy, uz = _normalize(axis)
    x, y, z = value
    c = math.cos(angle)
    s = math.sin(angle)
    dot = ux * x + uy * y + uz * z
    return (
        x * c + (uy * z - uz * y) * s + ux * dot * (1.0 - c),
        y * c + (uz * x - ux * z) * s + uy * dot * (1.0 - c),
        z * c + (ux * y - uy * x) * s + uz * dot * (1.0 - c),
    )


def _centroid(vertices: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if not vertices:
        return (0.0, 0.0, 0.0)
    return (
        sum(vertex[0] for vertex in vertices) / len(vertices),
        sum(vertex[1] for vertex in vertices) / len(vertices),
        sum(vertex[2] for vertex in vertices) / len(vertices),
    )


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: tuple[float, float, float], scalar: float) -> tuple[float, float, float]:
    return (a[0] * scalar, a[1] * scalar, a[2] * scalar)


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _format_vec(value: tuple[float, float, float]) -> str:
    return f"({value[0]:.5g}, {value[1]:.5g}, {value[2]:.5g})"


def _format_static_report_failure(report: StaticMeshReplacementReport) -> str:
    lines = [
        "Static mesh replacement failed.",
        "",
        "Original:",
        f"  submeshes: {report.original_submesh_count}",
        f"  vertices: {report.original_vertex_count}",
        f"  faces: {report.original_face_count}",
        "",
        "Replacement:",
        f"  submeshes: {report.replacement_submesh_count}",
        f"  vertices: {report.replacement_vertex_count}",
        f"  faces: {report.replacement_face_count}",
    ]
    if report.mapping_summary:
        lines.extend(["", "Mapping:"])
        lines.extend(f"  {line}" for line in report.mapping_summary)
    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  {line}" for line in report.warnings)
    if report.errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"  {line}" for line in report.errors)
    return "\n".join(lines)
