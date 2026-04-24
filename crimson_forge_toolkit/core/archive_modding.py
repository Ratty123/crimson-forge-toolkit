from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import dataclasses
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import lz4.block as lz4_block
except Exception:  # pragma: no cover - optional dependency
    lz4_block = None  # type: ignore[assignment]

from crimson_forge_toolkit.constants import APP_NAME
from crimson_forge_toolkit.core.common import raise_if_cancelled, run_process_with_cancellation
from crimson_forge_toolkit.core.model_preview import _build_lod_summary, _build_model_preview
from crimson_forge_toolkit.models import (
    ArchiveEntry,
    ArchiveModelTextureReference,
    ImportAutoFixResult,
    ImportIssue,
    ImportIssueStatus,
    MeshImportDiff,
    ModelPreviewData,
    ModelPreviewMesh,
    ModPackageInfo,
)
from crimson_forge_toolkit.modding.mesh_exporter import export_fbx, export_fbx_with_skeleton, export_obj, write_roundtrip_manifest
from crimson_forge_toolkit.modding.mesh_importer import (
    _load_obj_roundtrip_sidecar,
    build_mesh,
    transfer_pam_edit_to_pamlod_mesh,
)
from crimson_forge_toolkit.modding.mesh_parser import ParsedMesh, SubMesh, parse_mesh
from crimson_forge_toolkit.modding.scene_importer import (
    SCENE_TEXTURE_SOURCE_EXTENSIONS,
    discover_scene_texture_files,
    import_scene_mesh,
)
from crimson_forge_toolkit.modding.material_replacer import (
    TextureReplacementPayload,
    build_texture_replacement_payloads,
)
from crimson_forge_toolkit.modding.static_mesh_replacer import (
    StaticMeshReplacementOptions,
    build_static_mesh_replacement,
    suggest_static_submesh_mappings,
)
from crimson_forge_toolkit.modding.skeleton_parser import Skeleton, iter_pab_candidate_basenames, parse_pab


ARCHIVE_MESH_EXTENSIONS = {".pam", ".pamlod", ".pac"}
ARCHIVE_AUDIO_PATCH_EXTENSIONS = {".wem", ".wav"}
ARCHIVE_AUDIO_EXPORT_EXTENSIONS = {".wem", ".wav", ".ogg", ".mp3", ".bnk"}
ARCHIVE_PATCH_BACKUP_ROOT = Path(tempfile.gettempdir()) / APP_NAME / "archive_patch_backups"
MESH_IMPORT_SIDECAR_EXTENSIONS = {".xml", ".pami", ".pac_xml", ".pam_xml", ".pamlod_xml"}


@dataclass(slots=True)
class MeshExportResult:
    output_paths: List[Path]
    summary_lines: List[str]
    requires_confirmation: bool = False
    confirmation_title: str = ""
    confirmation_message: str = ""


@dataclass(slots=True)
class MeshImportPreviewResult:
    rebuilt_data: bytes
    parsed_mesh: ParsedMesh
    preview_model: ModelPreviewData
    summary_lines: List[str]
    import_mode: str = "roundtrip"
    texture_references: Tuple[ArchiveModelTextureReference, ...] = ()
    supplemental_file_specs: Tuple["MeshImportSupplementalFileSpec", ...] = ()
    paired_lod_data: Optional[bytes] = None
    paired_lod_path: str = ""
    import_diffs: Tuple[MeshImportDiff, ...] = ()
    import_issues: Tuple[ImportIssue, ...] = ()
    auto_fix_result: ImportAutoFixResult = field(default_factory=ImportAutoFixResult)
    roundtrip_manifest: Optional[dict] = None


@dataclass(slots=True)
class ArchivePatchRequest:
    entry: ArchiveEntry
    payload_data: bytes


@dataclass(slots=True)
class ArchivePatchResult:
    backup_dir: Path
    changed_entries: Dict[str, ArchiveEntry]
    changed_paths: List[str]
    warnings: List[str]


@dataclass(slots=True)
class ArchiveLooseExportResult:
    package_root: Path
    written_files: List[Path]


@dataclass(slots=True)
class MeshImportSupplementalFileSpec:
    source_path: Path
    target_path: str = ""
    kind: str = ""
    target_entry: Optional[ArchiveEntry] = None
    used_for_preview: bool = False
    payload_data: bytes = b""
    note: str = ""


def _normalize_import_lookup_path(raw_path: str) -> str:
    return str(raw_path or "").replace("\\", "/").strip().lower()


def _normalize_import_binding_token(raw_value: str) -> str:
    return str(raw_value or "").strip().lower()


def _summarize_import_values(values: Sequence[str], *, limit: int = 3) -> str:
    compact_values = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not compact_values:
        return "None"
    if len(compact_values) <= limit:
        return ", ".join(compact_values)
    return ", ".join(compact_values[:limit]) + f" (+{len(compact_values) - limit} more)"


def _describe_sidecar_binding_locator(record: Mapping[str, str]) -> str:
    sidecar_path = str(record.get("sidecar_display") or "").strip()
    parameter_name = str(record.get("parameter_name") or "").strip() or "<unnamed parameter>"
    submesh_name = str(record.get("submesh_name") or "").strip()
    locator = f"{Path(sidecar_path).name or sidecar_path} :: {parameter_name}"
    if submesh_name:
        locator += f" [{submesh_name}]"
    return locator


def _build_selected_sidecar_target_overrides(
    supplemental_file_specs: Sequence[MeshImportSupplementalFileSpec],
) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for spec in supplemental_file_specs:
        if str(getattr(spec, "kind", "") or "").strip().lower() != "sidecar":
            continue
        target_path = _normalize_import_lookup_path(getattr(spec, "target_path", ""))
        if not target_path:
            continue
        source_path = getattr(spec, "source_path", None)
        if not isinstance(source_path, Path):
            continue
        candidate_keys = {
            _normalize_import_lookup_path(str(source_path)),
            _normalize_import_lookup_path(source_path.as_posix()),
            source_path.name.strip().lower(),
        }
        for candidate_key in candidate_keys:
            if candidate_key:
                overrides[candidate_key] = target_path
    return overrides


def _iter_normalized_sidecar_binding_records(
    bindings: Sequence[object],
    *,
    sidecar_target_overrides: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, str]]:
    from crimson_forge_toolkit.core.upscale_profiles import normalize_texture_reference_for_sidecar_lookup

    records: List[Dict[str, str]] = []
    for binding in bindings:
        texture_path = str(getattr(binding, "texture_path", "") or "").strip()
        normalized_texture = normalize_texture_reference_for_sidecar_lookup(texture_path)
        if not normalized_texture:
            continue
        parameter_name = str(getattr(binding, "parameter_name", "") or "").strip()
        submesh_name = str(getattr(binding, "submesh_name", "") or "").strip()
        raw_sidecar_path = str(getattr(binding, "sidecar_path", "") or "").replace("\\", "/").strip()
        raw_sidecar_key = _normalize_import_lookup_path(raw_sidecar_path)
        sidecar_basename_key = PurePosixPath(raw_sidecar_path).name.lower() if raw_sidecar_path else ""
        target_sidecar_key = ""
        if sidecar_target_overrides:
            for candidate_key in (raw_sidecar_key, sidecar_basename_key):
                if candidate_key and candidate_key in sidecar_target_overrides:
                    target_sidecar_key = str(sidecar_target_overrides[candidate_key] or "").strip()
                    break
        compare_key = _normalize_import_lookup_path(target_sidecar_key or raw_sidecar_key or sidecar_basename_key)
        records.append(
            {
                "sidecar_compare_key": compare_key,
                "sidecar_display": str(target_sidecar_key or raw_sidecar_path or sidecar_basename_key),
                "parameter_name": parameter_name,
                "parameter_key": _normalize_import_binding_token(parameter_name),
                "submesh_name": submesh_name,
                "submesh_key": _normalize_import_binding_token(submesh_name),
                "texture_path": texture_path,
                "texture_key": normalized_texture,
            }
        )
    return records


def _build_sidecar_binding_validation(
    *,
    original_sidecar_bindings: Sequence[object],
    selected_sidecar_bindings: Sequence[object],
    supplemental_file_specs: Sequence[MeshImportSupplementalFileSpec],
) -> Tuple[Tuple[MeshImportDiff, ...], Tuple[ImportIssue, ...], List[str], Tuple[str, ...], Tuple[str, ...]]:
    selected_sidecar_specs = [
        spec for spec in supplemental_file_specs if str(getattr(spec, "kind", "") or "").strip().lower() == "sidecar"
    ]
    if not selected_sidecar_specs:
        return (), (), [], (), ()

    diffs: List[MeshImportDiff] = []
    issues: List[ImportIssue] = []
    summary_lines: List[str] = []
    warning_fields: List[str] = []
    manual_review_fields: List[str] = []

    unmapped_sidecars = [spec for spec in selected_sidecar_specs if not str(getattr(spec, "target_path", "") or "").strip()]
    if unmapped_sidecars:
        sidecar_names = [spec.source_path.name for spec in unmapped_sidecars if isinstance(spec.source_path, Path)]
        diff = MeshImportDiff(
            field_name="selected_sidecar_targets",
            original_value="mapped archive targets",
            imported_value=f"{len(unmapped_sidecars):,} selected sidecar(s) unmapped",
            severity="warning",
            safe_to_auto_fix=False,
            detail=(
                "One or more selected local sidecar files could not be mapped to their original archive targets. "
                "Those files cannot be validated or patched safely."
            ),
        )
        diffs.append(diff)
        issues.append(
            ImportIssue(
                code="unmapped-sidecar-targets",
                title="Unmapped selected sidecars",
                status=ImportIssueStatus.WARNING.value,
                detail=(
                    f"{len(unmapped_sidecars):,} selected sidecar file(s) could not be mapped back to archive targets. "
                    f"Examples: {_summarize_import_values(sidecar_names)}."
                ),
                diffs=(diff,),
            )
        )
        warning_fields.append("selected_sidecar_targets")

    if not selected_sidecar_bindings:
        diff = MeshImportDiff(
            field_name="selected_sidecar_bindings",
            original_value="recognized material/texture bindings",
            imported_value="no recognized bindings parsed",
            severity="warning",
            safe_to_auto_fix=False,
            detail="Selected local sidecar files did not produce any recognized texture/material bindings.",
        )
        diffs.append(diff)
        issues.append(
            ImportIssue(
                code="selected-sidecar-no-bindings",
                title="Selected sidecars exposed no recognized bindings",
                status=ImportIssueStatus.WARNING.value,
                detail=(
                    "The selected material sidecar file(s) were loaded, but no supported material/texture bindings were detected. "
                    "Import can continue, but compatibility checks are limited."
                ),
                diffs=(diff,),
            )
        )
        warning_fields.append("selected_sidecar_bindings")
        return tuple(diffs), tuple(issues), summary_lines, tuple(dict.fromkeys(warning_fields)), ()

    sidecar_target_overrides = _build_selected_sidecar_target_overrides(selected_sidecar_specs)
    original_records = _iter_normalized_sidecar_binding_records(original_sidecar_bindings)
    selected_records = _iter_normalized_sidecar_binding_records(
        selected_sidecar_bindings,
        sidecar_target_overrides=sidecar_target_overrides,
    )
    selected_target_keys = {
        _normalize_import_lookup_path(getattr(spec, "target_path", ""))
        for spec in selected_sidecar_specs
        if str(getattr(spec, "target_path", "") or "").strip()
    }
    if selected_target_keys:
        original_records = [record for record in original_records if record["sidecar_compare_key"] in selected_target_keys]
        selected_records = [record for record in selected_records if record["sidecar_compare_key"] in selected_target_keys]

    if not original_records:
        diff = MeshImportDiff(
            field_name="original_sidecar_bindings",
            original_value="archive sidecar bindings available",
            imported_value="not available for selected targets",
            severity="warning",
            safe_to_auto_fix=False,
            detail="The original archive sidecar bindings could not be recovered for the selected sidecar target(s).",
        )
        diffs.append(diff)
        issues.append(
            ImportIssue(
                code="original-sidecar-baseline-missing",
                title="Original sidecar baseline unavailable",
                status=ImportIssueStatus.WARNING.value,
                detail=(
                    "The tool could not recover the original archive sidecar bindings for one or more selected targets, "
                    "so binding-level compatibility checks are incomplete."
                ),
                diffs=(diff,),
            )
        )
        warning_fields.append("original_sidecar_bindings")
        return tuple(diffs), tuple(issues), summary_lines, tuple(dict.fromkeys(warning_fields)), ()

    from collections import defaultdict

    original_by_locator: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    selected_by_locator: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    for record in original_records:
        locator = (record["sidecar_compare_key"], record["submesh_key"], record["parameter_key"])
        original_by_locator[locator].append(record)
    for record in selected_records:
        locator = (record["sidecar_compare_key"], record["submesh_key"], record["parameter_key"])
        selected_by_locator[locator].append(record)

    changed_diffs: List[MeshImportDiff] = []
    missing_diffs: List[MeshImportDiff] = []
    added_diffs: List[MeshImportDiff] = []
    matched_locator_count = 0

    for locator in sorted(set(original_by_locator) | set(selected_by_locator)):
        original_bucket = original_by_locator.get(locator, [])
        selected_bucket = selected_by_locator.get(locator, [])
        display_record = selected_bucket[0] if selected_bucket else original_bucket[0]
        original_textures = tuple(sorted({record["texture_path"] for record in original_bucket}))
        selected_textures = tuple(sorted({record["texture_path"] for record in selected_bucket}))
        original_texture_keys = {record["texture_key"] for record in original_bucket}
        selected_texture_keys = {record["texture_key"] for record in selected_bucket}
        locator_label = _describe_sidecar_binding_locator(display_record)
        if original_texture_keys and selected_texture_keys and original_texture_keys == selected_texture_keys:
            matched_locator_count += 1
            continue
        if original_bucket and selected_bucket:
            changed_diffs.append(
                MeshImportDiff(
                    field_name="sidecar_binding_texture",
                    original_value=_summarize_import_values(original_textures),
                    imported_value=_summarize_import_values(selected_textures),
                    severity="warning",
                    safe_to_auto_fix=False,
                    detail=f"Binding target changed for {locator_label}.",
                )
            )
            continue
        if original_bucket:
            missing_diffs.append(
                MeshImportDiff(
                    field_name="sidecar_binding_missing",
                    original_value=_summarize_import_values(original_textures),
                    imported_value="missing",
                    severity="warning",
                    safe_to_auto_fix=False,
                    detail=f"Original binding is missing from the selected sidecar for {locator_label}.",
                )
            )
            continue
        added_diffs.append(
            MeshImportDiff(
                field_name="sidecar_binding_added",
                original_value="not present",
                imported_value=_summarize_import_values(selected_textures),
                severity="warning",
                safe_to_auto_fix=False,
                detail=f"Selected sidecar introduced an extra binding for {locator_label}.",
            )
        )

    if changed_diffs:
        diffs.extend(changed_diffs)
        issues.append(
            ImportIssue(
                code="sidecar-binding-targets-changed",
                title="Material sidecar binding targets changed",
                status=ImportIssueStatus.REQUIRES_MANUAL_REVIEW.value,
                detail=(
                    f"{len(changed_diffs):,} sidecar binding locator(s) now point to different texture target(s). "
                    "This can change how the model shades or make parts render incorrectly."
                ),
                diffs=tuple(changed_diffs[:8]),
            )
        )
        manual_review_fields.append("sidecar_binding_texture")
        summary_lines.append(
            f"Import validation: detected {len(changed_diffs):,} sidecar binding target change(s) compared with the original archive sidecar."
        )

    if missing_diffs:
        diffs.extend(missing_diffs)
        issues.append(
            ImportIssue(
                code="sidecar-bindings-missing",
                title="Original material sidecar bindings are missing",
                status=ImportIssueStatus.REQUIRES_MANUAL_REVIEW.value,
                detail=(
                    f"{len(missing_diffs):,} original sidecar binding locator(s) are missing from the selected local sidecar set. "
                    "Missing bindings can leave textures, masks, or support maps unassigned."
                ),
                diffs=tuple(missing_diffs[:8]),
            )
        )
        manual_review_fields.append("sidecar_binding_missing")
        summary_lines.append(
            f"Import validation: {len(missing_diffs):,} original sidecar binding(s) are missing from the selected sidecar file(s)."
        )

    if added_diffs:
        diffs.extend(added_diffs)
        issues.append(
            ImportIssue(
                code="sidecar-bindings-added",
                title="Selected sidecars added extra bindings",
                status=ImportIssueStatus.WARNING.value,
                detail=(
                    f"{len(added_diffs):,} extra sidecar binding locator(s) were added compared with the original archive sidecar. "
                    "This is allowed, but it should be reviewed if the model is expected to remain game-compatible."
                ),
                diffs=tuple(added_diffs[:8]),
            )
        )
        warning_fields.append("sidecar_binding_added")
        summary_lines.append(
            f"Import validation: {len(added_diffs):,} extra sidecar binding(s) were added by the selected sidecar file(s)."
        )

    if matched_locator_count > 0 and not changed_diffs and not missing_diffs:
        summary_lines.append(
            f"Validated {matched_locator_count:,} selected sidecar binding locator(s) against the original archive sidecar with no texture-target drift."
        )

    return (
        tuple(diffs),
        tuple(issues),
        summary_lines,
        tuple(dict.fromkeys(warning_fields)),
        tuple(dict.fromkeys(manual_review_fields)),
    )


def _build_mesh_import_validation(
    entry: ArchiveEntry,
    original_mesh: ParsedMesh,
    rebuilt_mesh: ParsedMesh,
    *,
    import_mode: str = "roundtrip",
    texture_references: Sequence[ArchiveModelTextureReference] = (),
    supplemental_file_specs: Sequence[MeshImportSupplementalFileSpec] = (),
    original_sidecar_bindings: Sequence[object] = (),
    selected_sidecar_bindings: Sequence[object] = (),
    paired_lod_path: str = "",
    manifest_payload: Optional[dict] = None,
) -> Tuple[Tuple[MeshImportDiff, ...], Tuple[ImportIssue, ...], ImportAutoFixResult, List[str]]:
    diffs: List[MeshImportDiff] = []
    issues: List[ImportIssue] = []
    applied_fields: List[str] = []
    warning_fields: List[str] = []
    manual_review_fields: List[str] = []
    summary_lines: List[str] = []

    submesh_count_changed = len(original_mesh.submeshes) != len(rebuilt_mesh.submeshes)
    if submesh_count_changed:
        is_static_replacement = str(import_mode or "").strip().lower() in {
            "static",
            "static_replacement",
            "static-mesh-replacement",
        }
        status = (
            ImportIssueStatus.WARNING.value
            if is_static_replacement
            else ImportIssueStatus.REQUIRES_MANUAL_REVIEW.value
        )
        detail = (
            "Static replacement changed the parsed output submesh count after mapped or empty draw sections were rebuilt. "
            "This is expected when replacing a mesh with a different part layout; review the static mapping summary if parts are missing."
            if is_static_replacement
            else "Submesh count changed compared with the original mesh. This can break bindings or make parts invisible."
        )
        diffs.append(
            MeshImportDiff(
                field_name="submesh_count",
                original_value=str(len(original_mesh.submeshes)),
                imported_value=str(len(rebuilt_mesh.submeshes)),
                severity="warning",
                safe_to_auto_fix=is_static_replacement,
                detail="Submesh count changed during import preview.",
            )
        )
        issues.append(
            ImportIssue(
                code="submesh-count-drift",
                title=(
                    "Static replacement submesh remap"
                    if is_static_replacement
                    else "Submesh count/order drift"
                ),
                status=status,
                detail=detail,
                diffs=(diffs[-1],),
            )
        )
        if is_static_replacement:
            warning_fields.append("submesh_count")
        else:
            manual_review_fields.append("submesh_count")

    missing_uvs = any(not getattr(submesh, "uvs", None) for submesh in rebuilt_mesh.submeshes)
    if missing_uvs:
        diff = MeshImportDiff(
            field_name="uv_sets",
            original_value="present",
            imported_value="missing on one or more submeshes",
            severity="warning",
            safe_to_auto_fix=False,
            detail="One or more imported submeshes no longer contain UVs.",
        )
        diffs.append(diff)
        issues.append(
            ImportIssue(
                code="missing-uvs",
                title="Missing UVs",
                status=ImportIssueStatus.REQUIRES_MANUAL_REVIEW.value,
                detail="Missing UVs can make textures invisible or incorrect in-game.",
                diffs=(diff,),
            )
        )
        manual_review_fields.append("uv_sets")

    resolved_sidecars = [reference for reference in texture_references if reference.relation_group == "Material Sidecars"]
    if not resolved_sidecars:
        diff = MeshImportDiff(
            field_name="material_sidecars",
            original_value="expected",
            imported_value="not resolved",
            severity="warning",
            safe_to_auto_fix=False,
            detail="No material sidecar could be resolved for the imported mesh.",
        )
        diffs.append(diff)
        issues.append(
            ImportIssue(
                code="missing-sidecars",
                title="Missing sidecars",
                status=ImportIssueStatus.WARNING.value,
                detail="Material sidecars were not resolved. Import can continue, but texture bindings may be incomplete.",
                diffs=(diff,),
            )
        )
        warning_fields.append("material_sidecars")

    sidecar_diffs, sidecar_issues, sidecar_summary_lines, sidecar_warning_fields, sidecar_manual_review_fields = (
        _build_sidecar_binding_validation(
            original_sidecar_bindings=original_sidecar_bindings,
            selected_sidecar_bindings=selected_sidecar_bindings,
            supplemental_file_specs=supplemental_file_specs,
        )
    )
    if sidecar_diffs:
        diffs.extend(sidecar_diffs)
    if sidecar_issues:
        issues.extend(sidecar_issues)
    if sidecar_summary_lines:
        summary_lines.extend(sidecar_summary_lines)
    if sidecar_warning_fields:
        warning_fields.extend(sidecar_warning_fields)
    if sidecar_manual_review_fields:
        manual_review_fields.extend(sidecar_manual_review_fields)

    if paired_lod_path:
        applied_fields.append("paired_pamlod_path")
        issues.append(
            ImportIssue(
                code="paired-pamlod-restored",
                title="Paired PAMLOD restored",
                status=ImportIssueStatus.AUTO_FIXED.value,
                detail=f"Paired PAMLOD rebuild is prepared for {paired_lod_path}.",
            )
        )
        summary_lines.append(f"Auto-fixed: paired PAMLOD linkage restored ({paired_lod_path}).")

    mapped_specs = [spec for spec in supplemental_file_specs if spec.target_path]
    if mapped_specs:
        applied_fields.append("selected_sidecar_association")
        issues.append(
            ImportIssue(
                code="supplemental-targets-restored",
                title="Selected companion targets restored",
                status=ImportIssueStatus.AUTO_FIXED.value,
                detail=f"Recovered {len(mapped_specs):,} selected supplemental target path(s).",
            )
        )
        summary_lines.append(f"Auto-fixed: restored {len(mapped_specs):,} selected companion target path(s).")

    if isinstance(manifest_payload, dict):
        applied_fields.extend(
            field_name
            for field_name in (
                "source_path",
                "source_format",
                "family_graph",
                "skeleton_identity",
            )
            if field_name in manifest_payload
        )
        if manifest_payload.get("skeleton_identity"):
            summary_lines.append("Auto-fixed: restored original skeleton identity metadata from the round-trip manifest.")

    if issues:
        status_counts = Counter(issue.status for issue in issues)
        summary_lines.append(
            "Import validation: "
            + ", ".join(
                f"{status_counts.get(status, 0):,} {status}"
                for status in (
                    ImportIssueStatus.AUTO_FIXED.value,
                    ImportIssueStatus.WARNING.value,
                    ImportIssueStatus.REQUIRES_MANUAL_REVIEW.value,
                )
                if status_counts.get(status, 0) > 0
            )
        )

    return (
        tuple(diffs),
        tuple(issues),
        ImportAutoFixResult(
            applied_fields=tuple(dict.fromkeys(applied_fields)),
            warning_fields=tuple(dict.fromkeys(warning_fields)),
            manual_review_fields=tuple(dict.fromkeys(manual_review_fields)),
            issues=tuple(issues),
        ),
        summary_lines,
    )


@dataclass(slots=True)
class SkeletonPreviewResult:
    preview_text: str
    detail_lines: List[str]


@dataclass(slots=True)
class HkxPreviewResult:
    preview_text: str
    detail_lines: List[str]


class _VfsPathResolver:
    def __init__(self, name_block: bytes) -> None:
        self._name_block = name_block
        self._path_cache: Dict[int, str] = {0xFFFFFFFF: ""}

    def get_full_path(self, offset: int) -> str:
        if offset == 0xFFFFFFFF or offset >= len(self._name_block):
            return ""
        cached = self._path_cache.get(offset)
        if cached is not None:
            return cached
        parts: List[Tuple[int, str]] = []
        current_offset = offset
        base = ""
        while current_offset != 0xFFFFFFFF:
            cached = self._path_cache.get(current_offset)
            if cached is not None:
                base = cached
                break
            pos = current_offset
            if pos + 5 > len(self._name_block):
                break
            parent_offset = struct.unpack_from("<I", self._name_block, pos)[0]
            part_len = self._name_block[pos + 4]
            if pos + 5 + part_len > len(self._name_block):
                break
            part = self._name_block[pos + 5 : pos + 5 + part_len].decode("utf-8", errors="replace")
            parts.append((current_offset, part))
            current_offset = parent_offset
            if len(parts) > 255:
                break
        built = base
        for part_offset, part in reversed(parts):
            built = f"{built}{part}"
            self._path_cache[part_offset] = built
        return self._path_cache.get(offset, built)


@dataclass(slots=True)
class _MutablePazRecord:
    index: int
    entry_offset: int
    checksum: int
    size: int


@dataclass(slots=True)
class _MutableFileRecord:
    path: str
    paz_index: int
    flags: int
    record_offset: int
    offset: int
    comp_size: int
    orig_size: int


@dataclass(slots=True)
class _MutablePamt:
    path: Path
    raw: bytearray
    paz_records: Dict[int, _MutablePazRecord]
    file_records: Dict[str, _MutableFileRecord]


def _normalize_virtual_path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().lower()


def _mesh_import_candidate_virtual_paths(source_path: Path) -> Tuple[str, ...]:
    normalized_parts = [part for part in source_path.expanduser().parts if part]
    if not normalized_parts:
        return ()
    lowered_parts = [str(part).strip() for part in normalized_parts]
    ordered: List[str] = []
    seen: set[str] = set()

    def _append(parts: Sequence[str]) -> None:
        candidate = PurePosixPath(*parts).as_posix().strip()
        normalized_candidate = _normalize_virtual_path(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            return
        seen.add(normalized_candidate)
        ordered.append(candidate)

    for index, part in enumerate(lowered_parts):
        if str(part).strip().lower() == "files" and index + 1 < len(lowered_parts):
            _append(lowered_parts[index + 1 :])
            break

    asset_root_markers = {
        "animation",
        "character",
        "effect",
        "gamedata",
        "leveldata",
        "movie",
        "object",
        "sound",
        "ui",
    }
    for index, part in enumerate(lowered_parts):
        if str(part).strip().lower() in asset_root_markers:
            _append(lowered_parts[index:])
            break

    _append([source_path.name])
    return tuple(ordered)


def _resolve_supplemental_target_entry(
    source_path: Path,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    preferred_paths: Sequence[str] = (),
) -> Tuple[Optional[ArchiveEntry], str]:
    candidate_virtual_paths: List[str] = []
    seen_virtual_paths: set[str] = set()
    for raw_path in list(preferred_paths) + list(_mesh_import_candidate_virtual_paths(source_path)):
        normalized = _normalize_virtual_path(raw_path)
        if not normalized or normalized in seen_virtual_paths:
            continue
        seen_virtual_paths.add(normalized)
        candidate_virtual_paths.append(raw_path)

    if archive_entries_by_normalized_path is not None:
        for candidate_virtual_path in candidate_virtual_paths:
            normalized = _normalize_virtual_path(candidate_virtual_path)
            entries = archive_entries_by_normalized_path.get(normalized, ())
            if entries:
                return entries[0], candidate_virtual_path.replace("\\", "/")

    basename = source_path.name.lower()
    if archive_entries_by_basename is not None and basename:
        entries = archive_entries_by_basename.get(basename, ())
        if len(entries) == 1:
            return entries[0], entries[0].path

    if candidate_virtual_paths:
        return None, candidate_virtual_paths[0].replace("\\", "/")
    return None, ""


def _build_mesh_import_local_dds_lookup(
    supplemental_files: Sequence[Path],
) -> Tuple[Dict[str, Path], Dict[str, Path]]:
    by_normalized_path: Dict[str, Path] = {}
    by_basename: Dict[str, Path] = {}
    for supplemental_path in supplemental_files:
        if supplemental_path.suffix.lower() != ".dds":
            continue
        resolved_path = supplemental_path.expanduser().resolve()
        for candidate_virtual_path in _mesh_import_candidate_virtual_paths(resolved_path):
            normalized = _normalize_virtual_path(candidate_virtual_path)
            if normalized and normalized not in by_normalized_path:
                by_normalized_path[normalized] = resolved_path
        basename = resolved_path.name.lower()
        if basename and basename not in by_basename:
            by_basename[basename] = resolved_path
    return by_normalized_path, by_basename


def _apply_mesh_import_local_sidecar_texture_overrides(
    preview_model: ModelPreviewData,
    parsed_mesh: Optional[ParsedMesh],
    sidecar_texture_bindings: Sequence[object],
    supplemental_dds_by_normalized_path: Mapping[str, Path],
    supplemental_dds_by_basename: Mapping[str, Path],
    *,
    texconv_path: Optional[Path],
) -> List[str]:
    if texconv_path is None or not getattr(preview_model, "meshes", None) or not sidecar_texture_bindings:
        return []

    from crimson_forge_toolkit.core.archive import (
        _is_visible_model_texture_type,
        _iter_model_submesh_reference_candidates,
        _iter_parsed_model_submeshes,
        _model_texture_hint_priority,
        _model_texture_semantic_priority,
        _normalize_model_submesh_reference,
        _resolve_model_texture_semantics,
    )
    from crimson_forge_toolkit.core.pipeline import ensure_dds_display_preview_png, parse_dds
    from crimson_forge_toolkit.core.upscale_profiles import normalize_texture_reference_for_sidecar_lookup

    resolved_texconv_path = texconv_path.expanduser().resolve()
    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    preview_cache: Dict[str, str] = {}
    resolved_by_submesh: Dict[str, Tuple[Tuple[int, int, int, int], Path, str, str, str]] = {}
    global_visible_bindings: List[Tuple[Path, str, str, str]] = []
    fallback_visible_bindings: List[Tuple[Tuple[int, int, int, int], Path, str, str, str]] = []
    seen_fallback_binding_keys: set[Tuple[str, str, str]] = set()
    seen_global_binding_keys: set[Tuple[str, str]] = set()
    promoted_anonymous_fallback = False

    for binding in sidecar_texture_bindings:
        texture_path = str(getattr(binding, "texture_path", "") or "").strip()
        if not texture_path:
            continue
        normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(texture_path)
        basename = PurePosixPath(normalized_texture_path or texture_path.replace("\\", "/")).name.lower()
        override_path = supplemental_dds_by_normalized_path.get(normalized_texture_path)
        if override_path is None and basename:
            override_path = supplemental_dds_by_basename.get(basename)
        if override_path is None:
            continue

        parameter_name = str(getattr(binding, "parameter_name", "") or "").strip()
        texture_type, semantic_subtype, confidence = _resolve_model_texture_semantics(texture_path)
        priority = _model_texture_hint_priority(parameter_name)
        if priority is None:
            priority = _model_texture_semantic_priority(texture_type, semantic_subtype)
        if priority[0] <= 0 and not _is_visible_model_texture_type(texture_type):
            continue

        candidate_key = (priority[0], priority[1], confidence, -len(texture_path or override_path.name))
        submesh_name = str(getattr(binding, "submesh_name", "") or "").strip()
        submesh_keys = _iter_model_submesh_reference_candidates(submesh_name)
        fallback_binding_key = (
            _normalize_model_submesh_reference(submesh_name),
            basename,
            parameter_name.lower(),
        )
        if fallback_binding_key not in seen_fallback_binding_keys:
            seen_fallback_binding_keys.add(fallback_binding_key)
            fallback_visible_bindings.append((candidate_key, override_path, parameter_name, submesh_name, texture_path))
        if submesh_keys:
            for submesh_key in submesh_keys:
                existing = resolved_by_submesh.get(submesh_key)
                if existing is None or candidate_key > existing[0]:
                    resolved_by_submesh[submesh_key] = (
                        candidate_key,
                        override_path,
                        parameter_name,
                        submesh_name,
                        texture_path,
                    )
        else:
            global_key = (basename, parameter_name.lower())
            if global_key not in seen_global_binding_keys:
                seen_global_binding_keys.add(global_key)
                global_visible_bindings.append((override_path, parameter_name, submesh_name, texture_path))

    def _preview_path_for_dds(dds_path: Path) -> str:
        cache_key = str(dds_path).lower()
        preview_path = preview_cache.get(cache_key, "")
        if preview_path:
            return preview_path
        dds_info = None
        try:
            dds_info = parse_dds(dds_path)
        except Exception:
            dds_info = None
        preview_path = ensure_dds_display_preview_png(
            resolved_texconv_path,
            dds_path,
            dds_info=dds_info,
        )
        preview_cache[cache_key] = preview_path
        return preview_path

    assigned_count = 0
    unresolved_meshes: List[ModelPreviewMesh] = []
    for mesh_index, mesh in enumerate(preview_model.meshes):
        if str(getattr(mesh, "preview_texture_path", "") or "").strip():
            continue
        parsed_submesh = parsed_submeshes[mesh_index] if mesh_index < len(parsed_submeshes) else None
        candidate_keys = _iter_model_submesh_reference_candidates(
            str(getattr(parsed_submesh, "name", "") or ""),
            str(getattr(parsed_submesh, "material", "") or ""),
            str(getattr(parsed_submesh, "texture", "") or ""),
            str(getattr(mesh, "material_name", "") or ""),
            str(getattr(mesh, "texture_name", "") or ""),
        )
        best_match: Optional[Tuple[Tuple[int, int, int, int], Path, str, str, str]] = None
        for candidate_key_text in candidate_keys:
            resolved = resolved_by_submesh.get(candidate_key_text)
            if resolved is None:
                continue
            if best_match is None or resolved[0] > best_match[0]:
                best_match = resolved
        if best_match is None:
            unresolved_meshes.append(mesh)
            continue
        _candidate_key, override_path, _parameter_name, submesh_name, texture_path = best_match
        try:
            mesh.preview_texture_path = _preview_path_for_dds(override_path)
            mesh.texture_name = texture_path or override_path.name
            mesh.preview_texture_flip_vertical = False
            current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
            if submesh_name and not current_material_name:
                mesh.material_name = submesh_name
            assigned_count += 1
        except Exception:
            continue

    if not global_visible_bindings and unresolved_meshes and fallback_visible_bindings:
        unique_named_sidecar_submeshes = {
            _normalize_model_submesh_reference(submesh_name)
            for _candidate_key, _override_path, _parameter_name, submesh_name, _texture_path in fallback_visible_bindings
            if _normalize_model_submesh_reference(submesh_name)
        }
        should_promote_fallback = (
            len(unresolved_meshes) == 1
            or len(preview_model.meshes) == 1
            or len(parsed_submeshes) <= 1
            or len(unique_named_sidecar_submeshes) == 1
        )
        if should_promote_fallback:
            fallback_visible_bindings.sort(key=lambda item: item[0], reverse=True)
            _candidate_key, override_path, parameter_name, submesh_name, texture_path = fallback_visible_bindings[0]
            global_visible_bindings.append((override_path, parameter_name, submesh_name, texture_path))
            promoted_anonymous_fallback = True

    if global_visible_bindings and unresolved_meshes:
        if len(global_visible_bindings) == 1:
            override_path, _parameter_name, submesh_name, texture_path = global_visible_bindings[0]
            for mesh in unresolved_meshes:
                if str(getattr(mesh, "preview_texture_path", "") or "").strip():
                    continue
                try:
                    mesh.preview_texture_path = _preview_path_for_dds(override_path)
                    mesh.texture_name = texture_path or override_path.name
                    mesh.preview_texture_flip_vertical = False
                    current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
                    if submesh_name and not current_material_name:
                        mesh.material_name = submesh_name
                    assigned_count += 1
                except Exception:
                    continue
        else:
            binding_index = 0
            for mesh in unresolved_meshes:
                if str(getattr(mesh, "preview_texture_path", "") or "").strip():
                    continue
                if binding_index >= len(global_visible_bindings):
                    break
                override_path, _parameter_name, submesh_name, texture_path = global_visible_bindings[binding_index]
                binding_index += 1
                try:
                    mesh.preview_texture_path = _preview_path_for_dds(override_path)
                    mesh.texture_name = texture_path or override_path.name
                    mesh.preview_texture_flip_vertical = False
                    current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
                    if submesh_name and not current_material_name:
                        mesh.material_name = submesh_name
                    assigned_count += 1
                except Exception:
                    continue

    if assigned_count <= 0:
        return []
    info_lines = [
        f"Applied {assigned_count:,} local sidecar-driven texture preview binding(s) from the selected supplemental files."
    ]
    if promoted_anonymous_fallback:
        info_lines.append(
            "Used a local sidecar texture fallback because the rebuilt preview did not preserve a reliable submesh/material name match."
        )
    return info_lines


def _apply_mesh_import_local_support_texture_overrides(
    preview_model: ModelPreviewData,
    parsed_mesh: Optional[ParsedMesh],
    sidecar_texture_bindings: Sequence[object],
    supplemental_dds_by_normalized_path: Mapping[str, Path],
    supplemental_dds_by_basename: Mapping[str, Path],
    *,
    texconv_path: Optional[Path],
) -> List[str]:
    if texconv_path is None or not getattr(preview_model, "meshes", None) or not sidecar_texture_bindings:
        return []

    from collections import defaultdict

    from crimson_forge_toolkit.core.archive import (
        _infer_model_preview_normal_strength,
        _infer_model_preview_texture_slot,
        _iter_model_submesh_reference_candidates,
        _iter_parsed_model_submeshes,
        _model_texture_candidate_slot_priority,
        _model_texture_slot_hint_priority,
        _normalize_model_submesh_reference,
        _refine_model_texture_semantic_from_hint,
        _resolve_model_texture_semantic_details,
    )
    from crimson_forge_toolkit.core.pipeline import ensure_dds_display_preview_png, parse_dds
    from crimson_forge_toolkit.core.upscale_profiles import normalize_texture_reference_for_sidecar_lookup

    resolved_texconv_path = texconv_path.expanduser().resolve()
    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    preview_cache: Dict[str, str] = {}
    support_slots = ("normal", "material", "height")
    slot_labels = {
        "normal": "local normal-map override(s)",
        "material": "local material-mask override(s)",
        "height": "local height/displacement override(s)",
    }
    resolved_by_submesh: Dict[Tuple[str, str], Tuple[Tuple[int, int, int, int], Path, str, str, str]] = {}
    global_bindings: Dict[str, List[Tuple[Tuple[int, int, int, int], Path, str, str, str]]] = defaultdict(list)
    seen_global_keys: set[Tuple[str, str, str]] = set()
    assigned_by_slot: Dict[str, int] = {slot: 0 for slot in support_slots}

    for binding in sidecar_texture_bindings:
        texture_path = str(getattr(binding, "texture_path", "") or "").strip()
        if not texture_path:
            continue
        parameter_name = str(getattr(binding, "parameter_name", "") or "").strip()
        slot_name = _infer_model_preview_texture_slot(texture_path, semantic_hint=parameter_name)
        if slot_name not in support_slots:
            continue
        normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(texture_path)
        basename = PurePosixPath(normalized_texture_path or texture_path.replace("\\", "/")).name.lower()
        override_path = supplemental_dds_by_normalized_path.get(normalized_texture_path)
        if override_path is None and basename:
            override_path = supplemental_dds_by_basename.get(basename)
        if override_path is None:
            continue

        slot_priority = (
            _model_texture_slot_hint_priority(slot_name, parameter_name)
            or _model_texture_candidate_slot_priority(slot_name, texture_path)
            or (0, 0)
        )
        candidate_key = (
            slot_priority[0],
            slot_priority[1],
            len(parameter_name),
            -len(texture_path or override_path.name),
        )
        submesh_name = str(getattr(binding, "submesh_name", "") or "").strip()
        submesh_keys = _iter_model_submesh_reference_candidates(submesh_name)
        if submesh_keys:
            for submesh_key in submesh_keys:
                resolved_key = (slot_name, submesh_key)
                existing = resolved_by_submesh.get(resolved_key)
                if existing is None or candidate_key > existing[0]:
                    resolved_by_submesh[resolved_key] = (
                        candidate_key,
                        override_path,
                        parameter_name,
                        submesh_name,
                        texture_path,
                    )
        else:
            global_key = (slot_name, basename, parameter_name.lower())
            if global_key not in seen_global_keys:
                seen_global_keys.add(global_key)
                global_bindings[slot_name].append(
                    (
                        candidate_key,
                        override_path,
                        parameter_name,
                        submesh_name,
                        texture_path,
                    )
                )

    def _preview_path_for_dds(dds_path: Path) -> str:
        cache_key = str(dds_path).lower()
        preview_path = preview_cache.get(cache_key, "")
        if preview_path:
            return preview_path
        dds_info = None
        try:
            dds_info = parse_dds(dds_path)
        except Exception:
            dds_info = None
        preview_path = ensure_dds_display_preview_png(
            resolved_texconv_path,
            dds_path,
            dds_info=dds_info,
        )
        preview_cache[cache_key] = preview_path
        return preview_path

    def _assign_slot(
        mesh: ModelPreviewMesh,
        slot_name: str,
        override_path: Path,
        parameter_name: str,
        texture_path: str,
    ) -> bool:
        try:
            preview_path = _preview_path_for_dds(override_path)
        except Exception:
            return False
        if slot_name == "normal":
            mesh.preview_normal_texture_path = preview_path
            mesh.preview_normal_texture_name = texture_path or override_path.name
            mesh.preview_normal_texture_strength = _infer_model_preview_normal_strength(
                base_texture_path=str(getattr(mesh, "texture_name", "") or "").strip(),
                normal_texture_path=texture_path or override_path.name,
                material_name=str(getattr(mesh, "material_name", "") or "").strip(),
                semantic_hint=parameter_name,
                prefer_stronger=False,
            )
            return True
        if slot_name == "material":
            semantic_type, semantic_subtype, _confidence, packed_channels = _resolve_model_texture_semantic_details(
                texture_path or override_path.name
            )
            semantic_type, semantic_subtype = _refine_model_texture_semantic_from_hint(
                semantic_type,
                semantic_subtype,
                parameter_name,
            )
            mesh.preview_material_texture_path = preview_path
            mesh.preview_material_texture_name = texture_path or override_path.name
            mesh.preview_material_texture_type = semantic_type
            mesh.preview_material_texture_subtype = semantic_subtype
            mesh.preview_material_texture_packed_channels = tuple(packed_channels)
            return True
        if slot_name == "height":
            mesh.preview_height_texture_path = preview_path
            mesh.preview_height_texture_name = texture_path or override_path.name
            return True
        return False

    for mesh_index, mesh in enumerate(preview_model.meshes):
        parsed_submesh = parsed_submeshes[mesh_index] if mesh_index < len(parsed_submeshes) else None
        candidate_keys = _iter_model_submesh_reference_candidates(
            str(getattr(parsed_submesh, "name", "") or ""),
            str(getattr(parsed_submesh, "material", "") or ""),
            str(getattr(parsed_submesh, "texture", "") or ""),
            str(getattr(mesh, "material_name", "") or ""),
            str(getattr(mesh, "texture_name", "") or ""),
        )
        for slot_name in support_slots:
            best_match: Optional[Tuple[Tuple[int, int, int, int], Path, str, str, str]] = None
            for candidate_key_text in candidate_keys:
                resolved = resolved_by_submesh.get((slot_name, candidate_key_text))
                if resolved is None:
                    continue
                if best_match is None or resolved[0] > best_match[0]:
                    best_match = resolved
            if best_match is None:
                continue
            _candidate_key, override_path, parameter_name, _submesh_name, texture_path = best_match
            if _assign_slot(mesh, slot_name, override_path, parameter_name, texture_path):
                assigned_by_slot[slot_name] += 1

    for slot_name in support_slots:
        bindings = global_bindings.get(slot_name, [])
        if not bindings:
            continue
        bindings.sort(key=lambda item: item[0], reverse=True)
        unresolved_meshes = [
            mesh
            for mesh in preview_model.meshes
            if not str(getattr(mesh, f"preview_{slot_name}_texture_path", "") or "").strip()
        ]
        if not unresolved_meshes:
            continue
        if len(bindings) == 1:
            _candidate_key, override_path, parameter_name, _submesh_name, texture_path = bindings[0]
            for mesh in unresolved_meshes:
                if _assign_slot(mesh, slot_name, override_path, parameter_name, texture_path):
                    assigned_by_slot[slot_name] += 1
            continue
        binding_index = 0
        for mesh in unresolved_meshes:
            if binding_index >= len(bindings):
                break
            _candidate_key, override_path, parameter_name, _submesh_name, texture_path = bindings[binding_index]
            binding_index += 1
            if _assign_slot(mesh, slot_name, override_path, parameter_name, texture_path):
                assigned_by_slot[slot_name] += 1

    total_assigned = sum(assigned_by_slot.values())
    if total_assigned <= 0:
        return []
    info_lines = [f"Applied {total_assigned:,} local DDS support-map override(s) from the selected supplemental files."]
    for slot_name in support_slots:
        count = assigned_by_slot[slot_name]
        if count > 0:
            info_lines.append(f"{slot_labels[slot_name].capitalize()}: {count:,}.")
    return info_lines


def _apply_mesh_import_local_texture_overrides(
    preview_model: ModelPreviewData,
    supplemental_dds_by_normalized_path: Mapping[str, Path],
    supplemental_dds_by_basename: Mapping[str, Path],
    *,
    texconv_path: Optional[Path],
) -> List[str]:
    if texconv_path is None or not getattr(preview_model, "meshes", None):
        return []

    from crimson_forge_toolkit.core.pipeline import ensure_dds_display_preview_png, parse_dds

    resolved_texconv_path = texconv_path.expanduser().resolve()
    preview_cache: Dict[str, str] = {}
    override_count = 0
    unresolved_names: List[str] = []
    for mesh in preview_model.meshes:
        texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        if not texture_name:
            continue
        normalized_texture_name = _normalize_virtual_path(texture_name)
        basename = PurePosixPath(texture_name.replace("\\", "/")).name.lower()
        override_path = supplemental_dds_by_normalized_path.get(normalized_texture_name)
        if override_path is None and basename:
            override_path = supplemental_dds_by_basename.get(basename)
        if override_path is None:
            if texture_name not in unresolved_names and len(unresolved_names) < 5:
                unresolved_names.append(texture_name)
            continue
        cache_key = str(override_path).lower()
        preview_path = preview_cache.get(cache_key, "")
        if not preview_path:
            dds_info = None
            try:
                dds_info = parse_dds(override_path)
            except Exception:
                dds_info = None
            preview_path = ensure_dds_display_preview_png(
                resolved_texconv_path,
                override_path,
                dds_info=dds_info,
            )
            preview_cache[cache_key] = preview_path
        mesh.preview_texture_path = preview_path
        mesh.preview_texture_flip_vertical = False
        override_count += 1

    info_lines: List[str] = []
    if override_count > 0:
        info_lines.append(f"Applied {override_count:,} local DDS override texture(s) from the selected supplemental files.")
    return info_lines


def _merge_sidecar_text_maps(
    base_map: Mapping[str, Tuple[str, ...]],
    extra_map: Mapping[str, Tuple[str, ...]],
) -> Dict[str, Tuple[str, ...]]:
    merged: Dict[str, List[str]] = {key: list(values) for key, values in base_map.items()}
    for key, values in extra_map.items():
        bucket = merged.setdefault(key, [])
        for value in values:
            if value not in bucket:
                bucket.append(value)
    return {key: tuple(values) for key, values in merged.items()}


def _safe_log(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        log(message)


def _merge_note_text(existing_note: str, new_note: str) -> str:
    existing_parts = [part.strip() for part in str(existing_note or "").split(";") if part.strip()]
    for part in [part.strip() for part in str(new_note or "").split(";") if part.strip()]:
        if part not in existing_parts:
            existing_parts.append(part)
    return "; ".join(existing_parts)


def _dedupe_mesh_loose_file_rows(file_rows: Sequence["MeshLooseModFile"]) -> List["MeshLooseModFile"]:
    deduped: List["MeshLooseModFile"] = []
    by_path: Dict[str, "MeshLooseModFile"] = {}
    for row in file_rows:
        path_key = str(getattr(row, "path", "") or "").replace("\\", "/").strip().lower()
        if not path_key:
            continue
        existing = by_path.get(path_key)
        if existing is None:
            by_path[path_key] = row
            deduped.append(row)
            continue
        if not getattr(existing, "package_group", "") and getattr(row, "package_group", ""):
            existing.package_group = row.package_group
        if not getattr(existing, "format", "") and getattr(row, "format", ""):
            existing.format = row.format
        if not getattr(existing, "generated_from", "") and getattr(row, "generated_from", ""):
            existing.generated_from = row.generated_from
        existing.note = _merge_note_text(getattr(existing, "note", ""), getattr(row, "note", ""))
    return deduped


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _export_related_archive_entries(
    entries: Sequence[ArchiveEntry],
    output_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    from crimson_forge_toolkit.core.archive import extract_archive_entry

    written_paths: List[Path] = []
    seen_paths: set[str] = set()
    for entry in entries:
        normalized_path = _normalize_virtual_path(entry.path)
        if not normalized_path or normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        relative_parts = PurePosixPath(entry.path.replace("\\", "/")).parts
        if not relative_parts:
            continue
        target_path = output_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _safe_log(on_log, f"Copying related file: {target_path.relative_to(output_root).as_posix()}")
            extract_archive_entry(entry, target_path)
            written_paths.append(target_path)
        except Exception as exc:
            _safe_log(
                on_log,
                f"Warning: could not export related file {entry.path}: {exc}",
            )
    return written_paths


def _parse_mutable_pamt(pamt_path: Path) -> _MutablePamt:
    data = bytearray(pamt_path.read_bytes())
    if len(data) < 12:
        raise ValueError(f"{pamt_path} is too small to be a valid PAMT file.")

    off = 0
    _header_crc, paz_count, _unknown = struct.unpack_from("<III", data, off)
    off += 12

    paz_records: Dict[int, _MutablePazRecord] = {}
    for paz_index in range(paz_count):
        record_offset = off + (paz_index * 12)
        checksum, size, _reserved = struct.unpack_from("<III", data, record_offset)
        paz_records[paz_index] = _MutablePazRecord(
            index=paz_index,
            entry_offset=record_offset,
            checksum=checksum,
            size=size,
        )
    off += paz_count * 12

    if off + 4 > len(data):
        raise ValueError(f"{pamt_path.name} directory block length is truncated.")
    dir_block_size = struct.unpack_from("<I", data, off)[0]
    off += 4
    directory_data = bytes(data[off : off + dir_block_size])
    off += dir_block_size

    if off + 4 > len(data):
        raise ValueError(f"{pamt_path.name} file-name block length is truncated.")
    file_name_block_size = struct.unpack_from("<I", data, off)[0]
    off += 4
    file_names = bytes(data[off : off + file_name_block_size])
    off += file_name_block_size

    if off + 4 > len(data):
        raise ValueError(f"{pamt_path.name} folder table length is truncated.")
    folder_count = struct.unpack_from("<I", data, off)[0]
    off += 4
    folder_table_size = folder_count * 16
    folders = list(struct.iter_unpack("<IIII", data[off : off + folder_table_size]))
    off += folder_table_size

    if off + 4 > len(data):
        raise ValueError(f"{pamt_path.name} file table length is truncated.")
    file_count = struct.unpack_from("<I", data, off)[0]
    off += 4
    file_table_offset = off
    file_table_size = file_count * struct.calcsize("<IIIIHH")
    raw_file_records = list(struct.iter_unpack("<IIIIHH", data[file_table_offset : file_table_offset + file_table_size]))

    resolver = _VfsPathResolver(file_names)
    dir_resolver = _VfsPathResolver(directory_data)
    folder_ranges = sorted(
        (
            file_start_index,
            file_start_index + folder_file_count,
            dir_resolver.get_full_path(name_offset).replace("\\", "/").strip("/"),
        )
        for _folder_hash, name_offset, file_start_index, folder_file_count in folders
        if folder_file_count > 0
    )

    file_records: Dict[str, _MutableFileRecord] = {}
    folder_cursor = 0
    record_stride = struct.calcsize("<IIIIHH")
    for entry_index, (name_offset, paz_offset, comp_size, orig_size, paz_index, flags) in enumerate(raw_file_records):
        relative_path = resolver.get_full_path(name_offset).replace("\\", "/").strip("/")
        guessed_dir = ""
        while folder_cursor < len(folder_ranges) and entry_index >= folder_ranges[folder_cursor][1]:
            folder_cursor += 1
        if folder_cursor < len(folder_ranges):
            start, end, candidate_dir = folder_ranges[folder_cursor]
            if start <= entry_index < end:
                guessed_dir = candidate_dir
        full_path = f"{guessed_dir}/{relative_path}".strip("/") if guessed_dir else relative_path
        normalized_path = _normalize_virtual_path(full_path)
        file_records[normalized_path] = _MutableFileRecord(
            path=full_path,
            paz_index=int(paz_index),
            flags=int(flags),
            record_offset=file_table_offset + (entry_index * record_stride),
            offset=int(paz_offset),
            comp_size=int(comp_size),
            orig_size=int(orig_size),
        )

    return _MutablePamt(path=pamt_path, raw=data, paz_records=paz_records, file_records=file_records)


def _calculate_pa_checksum(value: bytes) -> int:
    from crimson_forge_toolkit.core.archive import calculate_pa_checksum

    return int(calculate_pa_checksum(value))


def _crypt_archive_payload(data: bytes, basename: str) -> bytes:
    from crimson_forge_toolkit.core.archive import crypt_chacha20_filename

    return crypt_chacha20_filename(data, basename)


def _compress_archive_payload(data: bytes, compression_type: int) -> bytes:
    if compression_type in {0, 1}:
        return data
    if compression_type == 2:
        if lz4_block is None:
            raise ValueError("LZ4 support is not available in this build.")
        return lz4_block.compress(data, store_size=False)
    raise ValueError(f"Archive patching does not support compression type {compression_type} yet.")


def _write_bytes_preserve_timestamps(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp_path.open("wb") as handle:
        handle.write(data)
    os.replace(temp_path, path)


def _pad_to_16(data: bytes) -> bytes:
    padding = (-len(data)) % 16
    if padding <= 0:
        return data
    return data + (b"\x00" * padding)


def _format_progress_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024.0:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024.0 * 1024.0):.1f} MB"
    return f"{size_bytes / (1024.0 * 1024.0 * 1024.0):.2f} GB"


def _copy_file_with_progress(
    source: Path,
    target: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    label: str = "",
) -> None:
    total_size = max(0, int(source.stat().st_size))
    copied = 0
    last_logged_percent = -10
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src_handle, target.open("wb") as dst_handle:
        while True:
            chunk = src_handle.read(16 * 1024 * 1024)
            if not chunk:
                break
            dst_handle.write(chunk)
            copied += len(chunk)
            if on_log is None or total_size <= 0:
                continue
            percent = min(100, int((copied * 100) / total_size))
            if percent >= 100 or percent - last_logged_percent >= 10:
                last_logged_percent = percent
                prefix = f"{label}: " if label else ""
                _safe_log(
                    on_log,
                    f"{prefix}{percent}% ({_format_progress_size(copied)} / {_format_progress_size(total_size)})",
                )
    shutil.copystat(source, target)


def _write_paz_payload(entry: ArchiveEntry, payload: bytes) -> int:
    padded_payload = _pad_to_16(payload)
    paz_path = entry.paz_file

    # Always append a new payload instead of overwriting or clearing the old slot.
    # This keeps the previous archive bytes intact until the updated PAMT has been
    # written successfully, which makes forced-close failures recoverable.
    with paz_path.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        paz_size = handle.tell()
        new_offset = (paz_size + 15) & ~15
        if new_offset > paz_size:
            handle.write(b"\x00" * (new_offset - paz_size))
        handle.write(padded_payload)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    return new_offset


def _package_root_from_entry(entry: ArchiveEntry) -> Path:
    return entry.pamt_path.parent.parent


def _resolve_papgt_path(entry: ArchiveEntry) -> Path:
    root = _package_root_from_entry(entry)
    papgt_path = root / "meta" / "0.papgt"
    if not papgt_path.is_file():
        raise FileNotFoundError(f"Could not find PAPGT root index at {papgt_path}.")
    return papgt_path


def _package_group_sort_order(package_root: Path) -> List[str]:
    groups: List[str] = []
    for child in sorted(package_root.iterdir()):
        if child.is_dir() and (child / "0.pamt").is_file():
            groups.append(child.name)
    return groups


def _papgt_crc_offset(papgt_path: Path, package_group: str) -> int:
    package_root = papgt_path.parent.parent
    groups = _package_group_sort_order(package_root)
    if package_group not in groups:
        raise ValueError(f"Package group {package_group} is not present under {package_root}.")
    index = groups.index(package_group)
    return 12 + (index * 12) + 8


def _verify_crc_chain(papgt_path: Path, touched_pamt_paths: Iterable[Path]) -> None:
    papgt_data = papgt_path.read_bytes()
    stored_papgt_crc = struct.unpack_from("<I", papgt_data, 4)[0]
    computed_papgt_crc = _calculate_pa_checksum(papgt_data[12:])
    if stored_papgt_crc != computed_papgt_crc:
        raise ValueError(
            f"PAPGT checksum verification failed: stored=0x{stored_papgt_crc:08X} computed=0x{computed_papgt_crc:08X}"
        )

    for pamt_path in touched_pamt_paths:
        pamt_data = pamt_path.read_bytes()
        stored_pamt_crc = struct.unpack_from("<I", pamt_data, 0)[0]
        computed_pamt_crc = _calculate_pa_checksum(pamt_data[12:])
        if stored_pamt_crc != computed_pamt_crc:
            raise ValueError(
                f"PAMT checksum verification failed for {pamt_path.name}: "
                f"stored=0x{stored_pamt_crc:08X} computed=0x{computed_pamt_crc:08X}"
            )


def _create_backup(
    files: Sequence[Path],
    *,
    description: str,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    backup_root = ARCHIVE_PATCH_BACKUP_ROOT
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / timestamp
    counter = 1
    while backup_dir.exists():
        backup_dir = backup_root / f"{timestamp}_{counter:02d}"
        counter += 1
    backup_dir.mkdir(parents=True, exist_ok=True)

    manifest: List[Dict[str, str]] = []
    for path in files:
        if not path.exists():
            continue
        target_name = f"{path.parent.name}_{path.name}"
        target_path = backup_dir / target_name
        _safe_log(
            on_log,
            f"Backing up {path.name} to {backup_dir.name} ({_format_progress_size(path.stat().st_size)})...",
        )
        _copy_file_with_progress(path, target_path, on_log=on_log, label=f"Backup {path.name}")
        manifest.append(
            {
                "original_path": str(path),
                "backup_path": str(target_path),
            }
        )
    manifest_path = backup_dir / "backup_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "description": description,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": manifest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup_dir


def _restore_backup(backup_dir: Path, *, on_log: Optional[Callable[[str], None]] = None) -> None:
    manifest_path = backup_dir / "backup_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Backup manifest was not found under {backup_dir}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for file_info in manifest.get("files", []):
        source = Path(str(file_info.get("backup_path", "")))
        target = Path(str(file_info.get("original_path", "")))
        if source.is_file():
            _safe_log(
                on_log,
                f"Restoring {target.name} from backup ({_format_progress_size(source.stat().st_size)})...",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_target = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
            try:
                _copy_file_with_progress(source, temp_target, on_log=on_log, label=f"Restore {target.name}")
                os.replace(temp_target, target)
            finally:
                if temp_target.exists():
                    try:
                        temp_target.unlink()
                    except OSError:
                        pass


def list_archive_patch_backups(*, limit: Optional[int] = None) -> List[Path]:
    if not ARCHIVE_PATCH_BACKUP_ROOT.is_dir():
        return []
    backups = [
        path
        for path in ARCHIVE_PATCH_BACKUP_ROOT.iterdir()
        if path.is_dir() and (path / "backup_manifest.json").is_file()
    ]
    backups.sort(key=lambda path: path.name, reverse=True)
    if limit is not None:
        return backups[: max(0, int(limit))]
    return backups


def restore_archive_patch_backup(
    backup_dir: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> Path:
    resolved = backup_dir.expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"Archive patch backup directory was not found: {resolved}")
    _restore_backup(resolved, on_log=on_log)
    return resolved


def get_archive_texture_patch_blocker(entry: ArchiveEntry) -> str:
    if entry.extension != ".dds":
        return f"{entry.path} is not a DDS archive entry."
    if entry.compression_type == 1:
        return (
            "Direct archive patching for Partial DDS entries is not supported yet. "
            "Write a mod-ready loose replacement instead."
        )
    return ""


def build_archive_texture_payload_from_dds(
    entry: ArchiveEntry,
    replacement_dds_path: Path,
) -> bytes:
    from crimson_forge_toolkit.core.pipeline import parse_dds

    if entry.extension != ".dds":
        raise ValueError(f"{entry.path} is not a DDS archive entry.")

    resolved_path = replacement_dds_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Replacement DDS was not found: {resolved_path}")

    parse_dds(resolved_path)
    return resolved_path.read_bytes()


def build_archive_texture_payload_from_png(
    entry: ArchiveEntry,
    replacement_png_path: Path,
    *,
    texconv_path: Path,
    on_log: Optional[Callable[[str], None]] = None,
) -> bytes:
    from crimson_forge_toolkit.core.archive import ensure_archive_preview_source
    from crimson_forge_toolkit.core.pipeline import (
        build_texconv_command,
        max_mips_for_size,
        parse_dds,
    )

    if entry.extension != ".dds":
        raise ValueError(f"{entry.path} is not a DDS archive entry.")

    resolved_png = replacement_png_path.expanduser().resolve()
    if not resolved_png.is_file():
        raise FileNotFoundError(f"Replacement PNG was not found: {resolved_png}")

    resolved_texconv = texconv_path.expanduser().resolve()
    if not resolved_texconv.is_file():
        raise FileNotFoundError(f"texconv.exe was not found: {resolved_texconv}")

    original_dds_path, _note = ensure_archive_preview_source(entry)
    original_info = parse_dds(original_dds_path)

    target_stem = PurePosixPath(entry.path.replace("\\", "/")).stem or "replacement"
    with tempfile.TemporaryDirectory(prefix="ctf_archive_texture_rebuild_") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        normalized_png_path = temp_dir / f"{target_stem}.png"
        shutil.copy2(resolved_png, normalized_png_path)
        output_dir = temp_dir / "rebuilt"
        output_dir.mkdir(parents=True, exist_ok=True)
        mip_count = max(
            1,
            min(
                max_mips_for_size(original_info.width, original_info.height),
                int(original_info.mip_count or 1),
            ),
        )
        texconv_cmd = build_texconv_command(
            resolved_texconv,
            normalized_png_path,
            output_dir,
            original_info.texconv_format,
            mip_count,
            original_info.width,
            original_info.height,
            overwrite_existing_dds=True,
        )
        _safe_log(
            on_log,
            (
                f"Rebuilding DDS for {entry.path} from {resolved_png.name} "
                f"using {original_info.texconv_format} at {original_info.width}x{original_info.height}."
            ),
        )
        return_code, stdout, stderr = run_process_with_cancellation(texconv_cmd)
        if return_code != 0:
            failure_text = stderr.strip() or stdout.strip() or f"texconv exited with code {return_code}"
            raise RuntimeError(f"texconv failed while rebuilding {entry.basename}: {failure_text}")
        rebuilt_dds_path = output_dir / f"{target_stem}.dds"
        if not rebuilt_dds_path.is_file():
            raise FileNotFoundError(f"texconv did not produce the expected DDS output: {rebuilt_dds_path}")
        return rebuilt_dds_path.read_bytes()


def export_archive_payloads_to_mod_ready_loose(
    requests: Sequence[ArchivePatchRequest],
    *,
    parent_root: Path,
    package_info: ModPackageInfo,
    create_no_encrypt_file: bool = True,
    on_log: Optional[Callable[[str], None]] = None,
) -> ArchiveLooseExportResult:
    from crimson_forge_toolkit.core.mod_package import (
        normalize_mod_package_payload_path,
        resolve_mod_package_root,
        write_mod_package_manifest,
    )

    if not requests:
        raise ValueError("No archive payloads were provided for mod-ready loose export.")

    resolved_parent_root = parent_root.expanduser().resolve()
    package_root = resolve_mod_package_root(resolved_parent_root, package_info)
    package_root.mkdir(parents=True, exist_ok=True)

    written_files: List[Path] = []
    for request in requests:
        relative_parts = normalize_mod_package_payload_path(request.entry.path).parts
        if not relative_parts:
            raise ValueError(f"Archive path is invalid: {request.entry.path}")
        target_path = package_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(on_log, f"Writing loose mod payload: {target_path.relative_to(package_root)}")
        target_path.write_bytes(request.payload_data)
        written_files.append(target_path)

    manifest_path = write_mod_package_manifest(
        package_root,
        package_info,
        kind="archive_loose_mod",
        extra_fields={"file_count": len(written_files)},
        create_no_encrypt_file=create_no_encrypt_file,
    )
    written_files.append(manifest_path)

    return ArchiveLooseExportResult(package_root=package_root, written_files=written_files)


def export_archive_mesh_payloads_to_mod_ready_loose(
    requests: Sequence[ArchivePatchRequest],
    *,
    primary_entry: ArchiveEntry,
    preview_result: MeshImportPreviewResult,
    source_obj_path: Path,
    parent_root: Path,
    package_info: ModPackageInfo,
    create_no_encrypt_file: bool = True,
    include_related_files: bool = False,
    related_entries_to_include: Optional[Sequence[ArchiveEntry]] = None,
    supplemental_files_to_include: Sequence[MeshImportSupplementalFileSpec] = (),
    on_log: Optional[Callable[[str], None]] = None,
) -> ArchiveLooseExportResult:
    from crimson_forge_toolkit.core.mod_package import (
        MeshLooseModAsset,
        MeshLooseModFile,
        normalize_mod_package_payload_path,
        resolve_mod_package_root,
        write_mesh_loose_mod_package_metadata,
    )
    from crimson_forge_toolkit.core.archive import extract_archive_entry

    if not requests:
        raise ValueError("No archive payloads were provided for mesh mod-ready loose export.")

    resolved_parent_root = parent_root.expanduser().resolve()
    package_root = resolve_mod_package_root(resolved_parent_root, package_info)
    _safe_log(on_log, f"Mod-ready mesh package root: {package_root}")
    _clear_existing_mesh_loose_package_root(package_root, resolved_parent_root, on_log=on_log)
    package_root.mkdir(parents=True, exist_ok=True)

    written_files: List[Path] = []
    file_rows: List[MeshLooseModFile] = []
    source_obj_display = source_obj_path.expanduser().resolve().as_posix()
    paired_lod_path = (preview_result.paired_lod_path or "").strip().replace("\\", "/")
    primary_path = primary_entry.path.replace("\\", "/")
    written_virtual_paths: set[str] = set()
    for request in requests:
        normalized_request_path = normalize_mod_package_payload_path(request.entry.path).as_posix()
        relative_parts = PurePosixPath(normalized_request_path).parts
        if not relative_parts:
            raise ValueError(f"Archive path is invalid: {request.entry.path}")
        target_path = package_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(on_log, f"Writing loose mesh payload: {target_path.relative_to(package_root).as_posix()}")
        target_path.write_bytes(request.payload_data)
        written_files.append(target_path)
        note = ""
        written_virtual_paths.add(normalized_request_path.lower())
        if paired_lod_path and normalized_request_path == paired_lod_path and normalized_request_path != primary_path:
            note = f"Auto-generated paired LOD for {primary_entry.path}"
        file_rows.append(
            MeshLooseModFile(
                path=normalized_request_path,
                package_group=request.entry.pamt_path.parent.name,
                format=request.entry.extension.lstrip(".").lower(),
                generated_from=source_obj_display,
                note=note,
            )
        )

    supplemental_specs = [
        spec
        for spec in supplemental_files_to_include
        if isinstance(spec, MeshImportSupplementalFileSpec)
        and (bool(spec.payload_data) or spec.source_path.expanduser().resolve().is_file())
    ]
    for spec in supplemental_specs:
        normalized_target_path = normalize_mod_package_payload_path(spec.target_path or "").as_posix()
        if not normalized_target_path:
            _safe_log(
                on_log,
                f"Skipping selected supplemental file without a mapped loose target: {spec.source_path.name}",
            )
            continue
        relative_parts = PurePosixPath(normalized_target_path).parts
        if not relative_parts:
            _safe_log(
                on_log,
                f"Skipping selected supplemental file with an invalid target path: {spec.source_path.name}",
            )
            continue
        normalized_target_key = normalized_target_path.lower()
        if normalized_target_key in written_virtual_paths:
            _safe_log(
                on_log,
                f"Skipping selected supplemental file already written in this package: {normalized_target_path}",
            )
            continue
        target_path = package_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if spec.payload_data:
            _safe_log(
                on_log,
                f"Writing generated supplemental payload: {target_path.relative_to(package_root).as_posix()}",
            )
            target_path.write_bytes(spec.payload_data)
        else:
            _safe_log(
                on_log,
                f"Copying selected supplemental file: {target_path.relative_to(package_root).as_posix()}",
            )
            shutil.copy2(spec.source_path, target_path)
        if target_path not in written_files:
            written_files.append(target_path)
        written_virtual_paths.add(normalized_target_key)
        if spec.note:
            note = spec.note
        elif spec.kind == "sidecar":
            note = f"Selected local sidecar included for {primary_entry.path}"
        elif spec.kind == "sidecar_generated":
            note = f"Generated patched sidecar for {primary_entry.path}"
        elif spec.kind == "texture_generated":
            note = f"Generated replacement texture for {primary_entry.path}"
        elif spec.kind == "texture":
            note = f"Selected local texture override included for {primary_entry.path}"
        else:
            note = f"Selected local file included for {primary_entry.path}"
        package_group = ""
        if isinstance(spec.target_entry, ArchiveEntry):
            package_group = spec.target_entry.pamt_path.parent.name
        file_rows.append(
            MeshLooseModFile(
                path=normalized_target_path,
                package_group=package_group,
                format=PurePosixPath(normalized_target_path).suffix.lstrip(".").lower()
                or spec.source_path.suffix.lstrip(".").lower(),
                generated_from=spec.source_path.as_posix(),
                note=note,
            )
        )

    related_entries: List[ArchiveEntry] = []
    if related_entries_to_include is not None:
        related_entries.extend(entry for entry in related_entries_to_include if isinstance(entry, ArchiveEntry))
    elif include_related_files:
        for reference in preview_result.texture_references:
            related_entry = getattr(reference, "resolved_entry", None)
            if isinstance(related_entry, ArchiveEntry):
                related_entries.append(related_entry)

    if related_entries:
        for related_entry in related_entries:
            normalized_related_path = normalize_mod_package_payload_path(related_entry.path).as_posix()
            if normalized_related_path.lower() in written_virtual_paths:
                continue
            relative_parts = PurePosixPath(normalized_related_path).parts
            if not relative_parts:
                continue
            target_path = package_root.joinpath(*relative_parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _safe_log(on_log, f"Copying related file: {target_path.relative_to(package_root).as_posix()}")
                extract_archive_entry(related_entry, target_path)
                written_files.append(target_path)
                written_virtual_paths.add(normalized_related_path.lower())
                if related_entry.extension in MESH_IMPORT_SIDECAR_EXTENSIONS or related_entry.extension == ".json":
                    note = f"Related companion file copied from archive for {primary_entry.path}"
                else:
                    note = f"Referenced texture copied from archive for {primary_entry.path}"
                file_rows.append(
                    MeshLooseModFile(
                        path=normalized_related_path,
                        package_group=related_entry.pamt_path.parent.name,
                        format=related_entry.extension.lstrip(".").lower(),
                        generated_from=source_obj_display,
                        note=note,
                    )
                )
            except Exception as exc:
                _safe_log(
                    on_log,
                    f"Warning: could not include related file {related_entry.path}: {exc}",
                )

    asset_rows = [
        MeshLooseModAsset(
            entry_path=primary_path,
            package_group=primary_entry.pamt_path.parent.name,
            format=primary_entry.extension.lstrip(".").lower(),
            obj_path=source_obj_display,
            vertices=int(preview_result.parsed_mesh.total_vertices or 0),
            faces=int(preview_result.parsed_mesh.total_faces or 0),
            submeshes=len(preview_result.parsed_mesh.submeshes),
        )
    ]
    deduped_file_rows = _dedupe_mesh_loose_file_rows(file_rows)
    duplicate_row_count = len(file_rows) - len(deduped_file_rows)
    if duplicate_row_count > 0:
        _safe_log(on_log, f"Removed {duplicate_row_count:,} duplicate file metadata row(s) before writing manifest.json.")

    metadata_files = write_mesh_loose_mod_package_metadata(
        package_root,
        package_info,
        assets=asset_rows,
        files=deduped_file_rows,
        include_paired_lod=bool(paired_lod_path),
        create_no_encrypt_file=create_no_encrypt_file,
    )
    _safe_log(
        on_log,
        f"Finished mod-ready mesh package with {len(written_files):,} payload file(s) and {len(metadata_files):,} metadata file(s).",
    )

    return ArchiveLooseExportResult(
        package_root=package_root,
        written_files=[*written_files, *metadata_files],
    )


def _clear_existing_mesh_loose_package_root(
    package_root: Path,
    resolved_parent_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> None:
    resolved_package_root = package_root.expanduser().resolve()
    if not resolved_package_root.exists():
        return
    if resolved_package_root == resolved_parent_root:
        raise ValueError(f"Refusing to clear loose export parent directory: {resolved_package_root}")
    try:
        resolved_package_root.relative_to(resolved_parent_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to clear loose export folder outside the selected export root: {resolved_package_root}"
        ) from exc
    _safe_log(on_log, f"Clearing existing loose mesh package folder: {resolved_package_root}")
    for child in resolved_package_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _refresh_changed_entries(pamt_paths: Iterable[Path], changed_paths: Iterable[str]) -> Dict[str, ArchiveEntry]:
    from crimson_forge_toolkit.core.archive import parse_archive_pamt

    changed_lookup = {_normalize_virtual_path(path) for path in changed_paths}
    refreshed: Dict[str, ArchiveEntry] = {}
    for pamt_path in pamt_paths:
        for entry in parse_archive_pamt(pamt_path, paz_dir=pamt_path.parent):
            normalized = _normalize_virtual_path(entry.path)
            if normalized in changed_lookup:
                refreshed[normalized] = entry
    return refreshed


def patch_archive_entries(
    requests: Sequence[ArchivePatchRequest],
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> ArchivePatchResult:
    if not requests:
        raise ValueError("No archive modifications were provided.")

    package_roots = {_package_root_from_entry(request.entry).resolve() for request in requests}
    if len(package_roots) != 1:
        raise ValueError("Archive patching currently requires all modified entries to come from the same package root.")
    package_root = next(iter(package_roots))
    papgt_path = _resolve_papgt_path(requests[0].entry)

    grouped_requests: Dict[Path, List[ArchivePatchRequest]] = {}
    for request in requests:
        grouped_requests.setdefault(request.entry.pamt_path.resolve(), []).append(request)

    backup_targets: List[Path] = [papgt_path]
    for pamt_path, group_requests in grouped_requests.items():
        backup_targets.append(pamt_path)
        backup_targets.extend({request.entry.paz_file.resolve() for request in group_requests})
    _safe_log(on_log, f"Creating archive patch backup for {len(requests)} entrie(s)...")
    backup_dir = _create_backup(
        sorted(set(backup_targets)),
        description=f"Patch {len(requests)} archive entrie(s)",
        on_log=on_log,
    )
    _safe_log(on_log, f"Backup created: {backup_dir}")
    warnings: List[str] = []
    touched_pamt_paths: List[Path] = []
    changed_paths = [request.entry.path for request in requests]

    try:
        papgt_raw = bytearray(papgt_path.read_bytes())

        for pamt_path, group_requests in grouped_requests.items():
            mutable = _parse_mutable_pamt(pamt_path)
            touched_paz_indices: set[int] = set()
            group_name = pamt_path.parent.name
            _safe_log(on_log, f"Updating {group_name}/{pamt_path.name} ({len(group_requests)} entrie(s))...")

            for request in group_requests:
                normalized_path = _normalize_virtual_path(request.entry.path)
                mutable_record = mutable.file_records.get(normalized_path)
                if mutable_record is None:
                    raise ValueError(f"Could not locate {request.entry.path} inside {pamt_path.name}.")

                _safe_log(on_log, f"Preparing payload for {request.entry.path}...")
                processed_payload = _compress_archive_payload(request.payload_data, request.entry.compression_type)
                if request.entry.encrypted:
                    processed_payload = _crypt_archive_payload(processed_payload, request.entry.basename)

                _safe_log(
                    on_log,
                    f"Writing {request.entry.basename} to {request.entry.paz_file.name} at a safe append-only offset...",
                )
                new_offset = _write_paz_payload(request.entry, processed_payload)
                struct.pack_into("<I", mutable.raw, mutable_record.record_offset + 4, new_offset)
                struct.pack_into("<I", mutable.raw, mutable_record.record_offset + 8, len(processed_payload))
                struct.pack_into("<I", mutable.raw, mutable_record.record_offset + 12, len(request.payload_data))
                touched_paz_indices.add(int(request.entry.paz_index))

            for paz_index in sorted(touched_paz_indices):
                paz_record = mutable.paz_records.get(paz_index)
                if paz_record is None:
                    raise ValueError(f"PAMT {pamt_path.name} is missing PAZ table entry {paz_index}.")
                paz_path = pamt_path.parent / f"{paz_index}.paz"
                if not paz_path.is_file():
                    raise FileNotFoundError(f"Could not find archive payload file {paz_path}.")
                _safe_log(on_log, f"Recalculating checksum for {paz_path.name}...")
                paz_data = paz_path.read_bytes()
                struct.pack_into("<I", mutable.raw, paz_record.entry_offset, _calculate_pa_checksum(paz_data))
                struct.pack_into("<I", mutable.raw, paz_record.entry_offset + 4, len(paz_data))

            _safe_log(on_log, f"Writing updated {pamt_path.name}...")
            pamt_crc = _calculate_pa_checksum(bytes(mutable.raw[12:]))
            struct.pack_into("<I", mutable.raw, 0, pamt_crc)
            _write_bytes_preserve_timestamps(pamt_path, bytes(mutable.raw))
            touched_pamt_paths.append(pamt_path)

            crc_offset = _papgt_crc_offset(papgt_path, group_name)
            struct.pack_into("<I", papgt_raw, crc_offset, pamt_crc)

        _safe_log(on_log, f"Writing updated {papgt_path.name}...")
        papgt_crc = _calculate_pa_checksum(bytes(papgt_raw[12:]))
        struct.pack_into("<I", papgt_raw, 4, papgt_crc)
        _write_bytes_preserve_timestamps(papgt_path, bytes(papgt_raw))

        _safe_log(on_log, "Verifying archive checksum chain...")
        _verify_crc_chain(papgt_path, touched_pamt_paths)
    except Exception:
        _safe_log(on_log, f"Patch failed. Restoring files from backup: {backup_dir}")
        _restore_backup(backup_dir, on_log=on_log)
        raise

    _safe_log(on_log, "Refreshing changed archive entries...")
    refreshed_entries = _refresh_changed_entries(touched_pamt_paths, changed_paths)
    _safe_log(on_log, f"Patch complete. Backup available at {backup_dir}")
    return ArchivePatchResult(
        backup_dir=backup_dir,
        changed_entries=refreshed_entries,
        changed_paths=changed_paths,
        warnings=warnings,
    )


def _mesh_export_basename(entry: ArchiveEntry) -> str:
    return Path(entry.path.replace("\\", "/")).with_suffix("").as_posix().replace("/", "_")


def _parse_archive_mesh(entry: ArchiveEntry) -> ParsedMesh:
    from crimson_forge_toolkit.core.archive import read_archive_entry_data

    data, _decompressed, _note = read_archive_entry_data(entry)
    return parse_mesh(data, entry.path)


def _find_matching_skeleton_entry(
    entry: ArchiveEntry,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> tuple[Optional[ArchiveEntry], str, Tuple[str, ...]]:
    normalized_entry_path = str(entry.path or "").replace("\\", "/").strip()
    expected_path = PurePosixPath(normalized_entry_path).with_suffix(".pab").as_posix()
    expected_normalized = _normalize_virtual_path(expected_path)
    candidate_basenames = iter_pab_candidate_basenames(normalized_entry_path)
    resolution_tokens: Tuple[str, ...] = tuple(
        token
        for token in (
            PurePosixPath(normalized_entry_path).stem.lower(),
            *(PurePosixPath(candidate).stem.lower() for candidate in candidate_basenames),
            *(
                str(part or "").strip().lower()
                for part in PurePosixPath(normalized_entry_path.lower()).parts
                if str(part or "").strip()
            ),
        )
        if token
    )

    attempted_paths: List[str] = []
    seen_attempts: set[str] = set()

    def _remember_attempt(raw_value: str) -> None:
        normalized_value = _normalize_virtual_path(raw_value)
        if not normalized_value or normalized_value in seen_attempts:
            return
        seen_attempts.add(normalized_value)
        attempted_paths.append(raw_value.replace("\\", "/"))

    def _score_candidate(
        candidate: ArchiveEntry,
        *,
        exact_path: bool = False,
        matched_basename: str = "",
    ) -> int:
        candidate_path = _normalize_virtual_path(candidate.path)
        if not candidate_path.endswith(".pab"):
            return -1
        score = 0
        if exact_path or candidate_path == expected_normalized:
            score += 100
        if matched_basename:
            score += 30
        if candidate.pamt_path.parent == entry.pamt_path.parent:
            score += 10
        if PurePosixPath(candidate_path).parent == PurePosixPath(expected_normalized).parent:
            score += 12
        if "skeleton" in PurePosixPath(candidate_path).parts:
            score += 15
        for token in resolution_tokens:
            if token and token in candidate_path:
                score += 3
        return score

    best_entry: Optional[ArchiveEntry] = None
    best_score = -1
    seen_candidate_paths: set[str] = set()

    def _consider_candidates(
        candidates: Sequence[ArchiveEntry],
        *,
        exact_path: bool = False,
        matched_basename: str = "",
    ) -> None:
        nonlocal best_entry, best_score
        for candidate in candidates:
            candidate_path = _normalize_virtual_path(candidate.path)
            if not candidate_path or candidate_path in seen_candidate_paths:
                continue
            seen_candidate_paths.add(candidate_path)
            score = _score_candidate(
                candidate,
                exact_path=exact_path,
                matched_basename=matched_basename,
            )
            if score > best_score:
                best_score = score
                best_entry = candidate

    _remember_attempt(expected_path)
    if archive_entries_by_normalized_path is not None:
        _consider_candidates(
            archive_entries_by_normalized_path.get(expected_normalized, ()),
            exact_path=True,
            matched_basename=PurePosixPath(expected_path).name.lower(),
        )

    if archive_entries_by_basename is not None:
        for candidate_basename in candidate_basenames:
            _remember_attempt(candidate_basename)
            _consider_candidates(
                archive_entries_by_basename.get(candidate_basename.lower(), ()),
                matched_basename=candidate_basename.lower(),
            )

    if best_entry is not None:
        return best_entry, "", tuple(attempted_paths)

    detail = (
        f"Could not resolve a matching PAB skeleton for {entry.path}. "
        f"Tried {len(attempted_paths):,} candidate path(s)/basename(s)."
    )
    if attempted_paths:
        preview = ", ".join(attempted_paths[:5])
        if len(attempted_paths) > 5:
            preview += " ..."
        detail += f"\nTried: {preview}"
    return None, detail, tuple(attempted_paths)


def export_archive_mesh(
    entry: ArchiveEntry,
    output_dir: Path,
    export_format: str,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    related_entries: Sequence[ArchiveEntry] = (),
    allow_missing_skeleton: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
) -> MeshExportResult:
    export_kind = export_format.strip().lower()
    if export_kind not in {"obj", "fbx"}:
        raise ValueError(f"Unsupported mesh export format: {export_format}")
    if entry.extension not in ARCHIVE_MESH_EXTENSIONS:
        raise ValueError(f"{entry.path} is not a supported mesh entry.")

    parsed_mesh = _parse_archive_mesh(entry)
    if not parsed_mesh.submeshes and not parsed_mesh.lod_levels:
        raise ValueError("No geometry could be recovered from the selected mesh.")

    output_dir.mkdir(parents=True, exist_ok=True)
    basename = _mesh_export_basename(entry)
    _safe_log(on_log, f"Exporting {entry.path} as {export_kind.upper()}...")

    output_paths: List[Path] = []
    skeleton: Optional[Skeleton] = None
    skeleton_entry: Optional[ArchiveEntry] = None
    skeleton_resolution_warning = ""
    copied_related_count = 0
    if entry.extension == ".pac":
        skeleton_entry, skeleton_resolution_warning, _attempted_paths = _find_matching_skeleton_entry(
            entry,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
        )
    if export_kind == "obj":
        output_paths.extend(Path(path) for path in export_obj(parsed_mesh, str(output_dir), basename))
    else:
        if entry.extension == ".pac":
            if skeleton_entry is not None:
                from crimson_forge_toolkit.core.archive import read_archive_entry_data

                try:
                    skeleton_data, _decompressed, _note = read_archive_entry_data(skeleton_entry)
                    skeleton = parse_pab(skeleton_data, skeleton_entry.path)
                    if not skeleton.bones:
                        skeleton_resolution_warning = (
                            f"Matched skeleton {skeleton_entry.path} did not contain any bones."
                        )
                        skeleton = None
                except Exception as exc:
                    skeleton_resolution_warning = (
                        f"Matched skeleton {skeleton_entry.path} could not be parsed: {exc}"
                    )
                    skeleton = None
            if skeleton is None and not allow_missing_skeleton:
                confirmation_message = (
                    f"Export {entry.path} as FBX without an armature?\n\n"
                    f"{skeleton_resolution_warning or 'No matching PAB skeleton could be resolved.'}\n\n"
                    "Choose Yes to continue with a mesh-only FBX export, or No to cancel."
                )
                return MeshExportResult(
                    output_paths=[],
                    summary_lines=[
                        f"Path: {entry.path}",
                        f"Format: {parsed_mesh.format.upper()}",
                        "FBX export is waiting for confirmation because no usable skeleton could be attached.",
                    ],
                    requires_confirmation=True,
                    confirmation_title="Export FBX Without Skeleton?",
                    confirmation_message=confirmation_message,
                )
        if skeleton is not None and skeleton.bones:
            output_paths.append(Path(export_fbx_with_skeleton(parsed_mesh, skeleton, str(output_dir), basename)))
        else:
            output_paths.append(Path(export_fbx(parsed_mesh, str(output_dir), basename)))

    if related_entries:
        related_output_root = output_dir / "referenced_files"
        copied_paths = _export_related_archive_entries(
            related_entries,
            related_output_root,
            on_log=on_log,
        )
        output_paths.extend(copied_paths)
        copied_related_count = len(copied_paths)

    manifest_target_path = next(
        (
            path for path in output_paths
            if path.suffix.lower() in {".obj", ".fbx"}
        ),
        None,
    )
    if manifest_target_path is not None:
        try:
            from crimson_forge_toolkit.core.archive import build_archive_preview_result

            preview_result = build_archive_preview_result(
                None,
                entry,
                (),
                texture_entries_by_normalized_path=(
                    dict(archive_entries_by_normalized_path) if archive_entries_by_normalized_path is not None else None
                ),
                texture_entries_by_basename=(
                    dict(archive_entries_by_basename) if archive_entries_by_basename is not None else None
                ),
            )
            paired_lod_target = ""
            if entry.extension == ".pam" and archive_entries_by_normalized_path is not None:
                paired_candidates = archive_entries_by_normalized_path.get(
                    str(PurePosixPath(entry.path).with_suffix(".pamlod")).replace("\\", "/").strip().lower(),
                    (),
                )
                if paired_candidates:
                    paired_lod_target = paired_candidates[0].path
            companion_path = ""
            if manifest_target_path.suffix.lower() == ".obj":
                companion_candidate = manifest_target_path.with_suffix(".mtl")
                if companion_candidate.is_file():
                    companion_path = str(companion_candidate)
            selected_companion_files: List[str] = []
            seen_selected_companion_files: set[str] = set()
            sidecar_hashes: Dict[str, str] = {}
            for related_entry in related_entries:
                if not isinstance(related_entry, ArchiveEntry):
                    continue
                normalized_related_path = related_entry.path.replace("\\", "/").strip()
                normalized_related_key = normalized_related_path.lower()
                if normalized_related_path and normalized_related_key not in seen_selected_companion_files:
                    seen_selected_companion_files.add(normalized_related_key)
                    selected_companion_files.append(normalized_related_path)
                related_extension = related_entry.extension.lower()
                related_basename = PurePosixPath(normalized_related_path).name.lower()
                if related_extension in {".xml", ".pami", ".json"} or related_basename.endswith("_xml"):
                    copied_sidecar_path = (output_dir / "referenced_files").joinpath(
                        *PurePosixPath(normalized_related_path).parts
                    )
                    if copied_sidecar_path.is_file():
                        sidecar_hashes[normalized_related_path] = _sha256_file(copied_sidecar_path)
            family_graph_payload = {}
            if getattr(preview_result, "asset_family_graph", None) is not None:
                family_graph = preview_result.asset_family_graph
                family_graph_payload = {
                    "root_path": family_graph.root_path,
                    "family_key": family_graph.family_key,
                    "members": list(family_graph.members),
                    "grouped_paths": {
                        key: list(value)
                        for key, value in getattr(family_graph, "grouped_paths", {}).items()
                    },
                    "relations": [
                        {
                            "source_path": relation.source_path,
                            "target_path": relation.target_path,
                            "relation_kind": relation.relation_kind,
                            "confidence": relation.confidence,
                            "role_label": relation.role_label,
                            "reason": relation.reason,
                            "semantic_label": relation.semantic_label,
                            "semantic_hint": relation.semantic_hint,
                            "sidecar_parameter_name": relation.sidecar_parameter_name,
                            "material_name": relation.material_name,
                            "package_label": relation.package_label,
                        }
                        for relation in getattr(family_graph, "relations", ())
                    ],
                }
            texture_binding_rows = [
                {
                    "reference_name": reference.reference_name,
                    "resolved_archive_path": reference.resolved_archive_path,
                    "semantic_label": reference.semantic_label,
                    "semantic_hint": reference.semantic_hint,
                    "sidecar_parameter_name": reference.sidecar_parameter_name,
                    "material_name": reference.material_name,
                    "relation_group": reference.relation_group,
                }
                for reference in getattr(preview_result, "model_texture_references", ())
                if str(getattr(reference, "relation_group", "") or "").strip() == "Textures"
            ]
            manifest_path = write_roundtrip_manifest(
                parsed_mesh,
                manifest_target_path,
                companion_path=companion_path,
                extra_payload={
                    "source_archive_path": entry.path,
                    "source_archive_format": entry.extension.lstrip(".").lower(),
                    "export_format": manifest_target_path.suffix.lstrip(".").lower(),
                    "selected_companion_files": selected_companion_files,
                    "family_graph": family_graph_payload,
                    "texture_bindings": texture_binding_rows,
                    "texture_semantics": texture_binding_rows,
                    "sidecar_hashes": sidecar_hashes,
                    "paired_pamlod_target": paired_lod_target,
                    "skeleton_identity": skeleton_entry.path if skeleton_entry is not None else "",
                },
            )
            if manifest_path not in output_paths:
                output_paths.append(manifest_path)
        except Exception as exc:
            _safe_log(on_log, f"Warning: could not write round-trip manifest for {entry.path}: {exc}")

    summary_lines = [
        f"Path: {entry.path}",
        f"Format: {parsed_mesh.format.upper()}",
        f"Submeshes: {len(parsed_mesh.submeshes):,}",
        f"Vertices: {parsed_mesh.total_vertices:,}",
        f"Faces: {parsed_mesh.total_faces:,}",
    ]
    if copied_related_count:
        summary_lines.append(f"Referenced files copied: {copied_related_count:,}")
    if skeleton_entry is not None and skeleton is not None and skeleton.bones:
        summary_lines.append(f"Skeleton: {skeleton_entry.path}")
        summary_lines.append(f"Skeleton bones: {len(skeleton.bones):,}")
    elif export_kind == "fbx" and entry.extension == ".pac":
        summary_lines.append("Skeleton: mesh-only export")
        if skeleton_resolution_warning:
            summary_lines.append(f"Skeleton fallback reason: {skeleton_resolution_warning}")
    return MeshExportResult(output_paths=output_paths, summary_lines=summary_lines)


def _preview_meshes_from_submeshes(submeshes: Sequence[SubMesh]) -> List[ModelPreviewMesh]:
    preview_meshes: List[ModelPreviewMesh] = []
    for submesh in submeshes:
        if not submesh.vertices or not submesh.faces:
            continue
        indices: List[int] = []
        for face in submesh.faces:
            indices.extend(int(index) for index in face[:3])
        preview_meshes.append(
            ModelPreviewMesh(
                material_name=str(submesh.material or submesh.name or ""),
                texture_name=str(submesh.texture or ""),
                positions=[tuple(vertex) for vertex in submesh.vertices],
                texture_coordinates=[tuple(uv) for uv in submesh.uvs[: len(submesh.vertices)]],
                normals=[tuple(normal) for normal in submesh.normals[: len(submesh.vertices)]],
                indices=indices,
            )
        )
    return preview_meshes


def parsed_mesh_to_preview_model(parsed_mesh: ParsedMesh) -> ModelPreviewData:
    if parsed_mesh.format == "pamlod" and parsed_mesh.lod_levels:
        source_submeshes = parsed_mesh.lod_levels[0]
        preview_model = _build_model_preview(parsed_mesh.path, "pamlod", _preview_meshes_from_submeshes(source_submeshes), "lod mesh")
        preview_model.lod_index = 0
        preview_model.lod_count = len(parsed_mesh.lod_levels)
        preview_model.summary = _build_lod_summary(
            parsed_mesh.path,
            displayed_lod_index=0,
            recovered_lod_count=len(parsed_mesh.lod_levels),
            vertex_count=preview_model.vertex_count,
            face_count=preview_model.face_count,
        )
        return preview_model

    source_submeshes = parsed_mesh.submeshes
    label = "submesh" if parsed_mesh.format != "pac" else "mesh"
    return _build_model_preview(parsed_mesh.path, parsed_mesh.format, _preview_meshes_from_submeshes(source_submeshes), label)


def _restore_rebuilt_mesh_texture_identity(
    source_mesh: ParsedMesh,
    rebuilt_mesh: ParsedMesh,
) -> int:
    if not source_mesh.submeshes or not rebuilt_mesh.submeshes:
        return 0

    def _normalize_identity(value: str) -> str:
        return str(value or "").strip().lower()

    source_by_name: Dict[str, SubMesh] = {}
    duplicate_names: set[str] = set()
    for submesh in source_mesh.submeshes:
        normalized_name = _normalize_identity(submesh.name)
        if not normalized_name:
            continue
        if normalized_name in source_by_name:
            duplicate_names.add(normalized_name)
            continue
        source_by_name[normalized_name] = submesh
    for duplicate_name in duplicate_names:
        source_by_name.pop(duplicate_name, None)

    restored_count = 0
    for index, rebuilt_submesh in enumerate(rebuilt_mesh.submeshes):
        source_submesh: Optional[SubMesh] = None
        normalized_name = _normalize_identity(rebuilt_submesh.name)
        if normalized_name:
            source_submesh = source_by_name.get(normalized_name)
        if source_submesh is None and index < len(source_mesh.submeshes):
            source_submesh = source_mesh.submeshes[index]
        if source_submesh is None:
            continue

        source_texture = str(getattr(source_submesh, "texture", "") or "").strip()
        if source_texture and str(getattr(rebuilt_submesh, "texture", "") or "").strip() != source_texture:
            rebuilt_submesh.texture = source_texture
            restored_count += 1
        if not str(getattr(rebuilt_submesh, "material", "") or "").strip():
            rebuilt_submesh.material = str(getattr(source_submesh, "material", "") or "").strip()
        if not str(getattr(rebuilt_submesh, "name", "") or "").strip():
            rebuilt_submesh.name = str(getattr(source_submesh, "name", "") or "").strip()
    return restored_count


def build_mesh_preview_from_bytes(data: bytes, virtual_path: str) -> Tuple[ModelPreviewData, ParsedMesh]:
    parsed_mesh = parse_mesh(data, virtual_path)
    preview_model = parsed_mesh_to_preview_model(parsed_mesh)
    return preview_model, parsed_mesh


def _build_selected_sidecar_texture_bindings(
    supplemental_files: Sequence[Path],
) -> Tuple[
    Tuple[object, ...],
    Tuple[str, ...],
    Dict[str, Tuple[str, ...]],
    Dict[str, Tuple[str, ...]],
]:
    from collections import defaultdict

    from crimson_forge_toolkit.core.archive import _ArchiveModelSidecarTextureBinding
    from crimson_forge_toolkit.core.upscale_profiles import normalize_texture_reference_for_sidecar_lookup, parse_texture_sidecar_bindings

    bindings: List[object] = []
    sidecar_paths: List[str] = []
    seen_binding_keys: set[Tuple[str, str, str]] = set()
    sidecar_texts_by_normalized_path: Dict[str, List[str]] = defaultdict(list)
    sidecar_texts_by_basename: Dict[str, List[str]] = defaultdict(list)
    for supplemental_path in supplemental_files:
        if supplemental_path.suffix.lower() not in MESH_IMPORT_SIDECAR_EXTENSIONS:
            continue
        resolved_path = supplemental_path.expanduser().resolve()
        if not resolved_path.is_file():
            continue
        try:
            text = resolved_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        parsed_bindings = parse_texture_sidecar_bindings(text, sidecar_path=resolved_path.name)
        if not parsed_bindings:
            continue
        sidecar_paths.append(resolved_path.name)
        for binding in parsed_bindings:
            normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
            key = (
                normalized_texture_path,
                str(binding.submesh_name or "").strip().lower(),
                str(binding.parameter_name or "").strip().lower(),
            )
            if normalized_texture_path:
                sidecar_texts_by_normalized_path[normalized_texture_path].append(text)
                basename = PurePosixPath(normalized_texture_path).name
                if basename:
                    sidecar_texts_by_basename[basename].append(text)
            if key in seen_binding_keys:
                continue
            seen_binding_keys.add(key)
            bindings.append(
                _ArchiveModelSidecarTextureBinding(
                    texture_path=binding.texture_path,
                    parameter_name=binding.parameter_name,
                    submesh_name=binding.submesh_name,
                    sidecar_path=resolved_path.name,
                )
            )
    return (
        tuple(bindings),
        tuple(sidecar_paths),
        {key: tuple(values) for key, values in sidecar_texts_by_normalized_path.items()},
        {key: tuple(values) for key, values in sidecar_texts_by_basename.items()},
    )


def _build_mesh_import_supplemental_file_specs(
    entry: ArchiveEntry,
    supplemental_files: Sequence[Path],
    texture_references: Sequence[ArchiveModelTextureReference],
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[MeshImportSupplementalFileSpec, ...]:
    if not supplemental_files:
        return ()

    reference_candidates_by_basename: Dict[str, List[str]] = {}
    for reference in texture_references:
        resolved_archive_path = str(getattr(reference, "resolved_archive_path", "") or "").strip()
        reference_name = str(getattr(reference, "reference_name", "") or "").strip()
        target_path = resolved_archive_path or reference_name
        if not target_path:
            continue
        basename = PurePosixPath(target_path.replace("\\", "/")).name.lower()
        if not basename:
            continue
        bucket = reference_candidates_by_basename.setdefault(basename, [])
        if target_path not in bucket:
            bucket.append(target_path)

    related_entries: Sequence[ArchiveEntry] = ()
    if archive_entries_by_basename is not None:
        from crimson_forge_toolkit.core.archive import _find_archive_model_related_entries

        related_entries = _find_archive_model_related_entries(entry, dict(archive_entries_by_basename))
    related_entries_by_extension: Dict[str, List[ArchiveEntry]] = {}
    for related_entry in related_entries:
        related_entries_by_extension.setdefault(related_entry.extension.lower(), []).append(related_entry)

    specs: List[MeshImportSupplementalFileSpec] = []
    for supplemental_path in supplemental_files:
        resolved_source = supplemental_path.expanduser().resolve()
        if not resolved_source.is_file():
            continue
        extension = resolved_source.suffix.lower()
        if extension in SCENE_TEXTURE_SOURCE_EXTENSIONS - {".dds"}:
            continue
        preferred_paths: List[str] = []
        if extension == ".dds":
            preferred_paths.extend(reference_candidates_by_basename.get(resolved_source.name.lower(), ()))
        elif extension in MESH_IMPORT_SIDECAR_EXTENSIONS:
            related_by_extension = related_entries_by_extension.get(extension, [])
            if len(related_by_extension) == 1:
                preferred_paths.append(related_by_extension[0].path)
        target_entry, target_path = _resolve_supplemental_target_entry(
            resolved_source,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
            preferred_paths=preferred_paths,
        )
        if extension == ".dds" and target_entry is None and not preferred_paths:
            continue
        kind = "texture" if extension == ".dds" else "sidecar" if extension in MESH_IMPORT_SIDECAR_EXTENSIONS else "file"
        specs.append(
            MeshImportSupplementalFileSpec(
                source_path=resolved_source,
                target_path=target_path or (target_entry.path if isinstance(target_entry, ArchiveEntry) else ""),
                kind=kind,
                target_entry=target_entry if isinstance(target_entry, ArchiveEntry) else None,
                used_for_preview=kind in {"texture", "sidecar"},
            )
        )
    return tuple(specs)


def _find_first_archive_entry_by_virtual_path(
    virtual_path: str,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]],
) -> Optional[ArchiveEntry]:
    if archive_entries_by_normalized_path is None:
        return None
    candidates = archive_entries_by_normalized_path.get(_normalize_virtual_path(virtual_path), ())
    return candidates[0] if candidates else None


def _collect_original_mesh_sidecar_texts(
    entry: ArchiveEntry,
    archive_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]],
) -> Tuple[Tuple[ArchiveEntry, str], ...]:
    if archive_entries_by_basename is None:
        return ()
    from crimson_forge_toolkit.core.archive import (
        _find_archive_model_sidecar_entries,
        read_archive_entry_data,
        try_decode_text_like_archive_data,
    )

    sidecars: List[Tuple[ArchiveEntry, str]] = []
    for sidecar_entry in _find_archive_model_sidecar_entries(entry, dict(archive_entries_by_basename)):
        try:
            sidecar_data, _decompressed, _note = read_archive_entry_data(sidecar_entry)
        except Exception:
            continue
        sidecar_text = try_decode_text_like_archive_data(sidecar_data)
        if sidecar_text:
            sidecars.append((sidecar_entry, sidecar_text))
    return tuple(sidecars)


def _mesh_texture_original_source_path(texture_entry: object) -> Path:
    from crimson_forge_toolkit.core.archive import ensure_archive_preview_source

    if not isinstance(texture_entry, ArchiveEntry):
        raise ValueError("Original texture archive entry is unavailable.")
    source_path, _note = ensure_archive_preview_source(texture_entry)
    return source_path


def _mesh_texture_original_bytes(texture_entry: object) -> bytes:
    from crimson_forge_toolkit.core.archive import read_archive_entry_data

    if not isinstance(texture_entry, ArchiveEntry):
        raise ValueError("Original texture archive entry is unavailable.")
    data, _decompressed, _note = read_archive_entry_data(texture_entry)
    return data


def _texture_replacement_payloads_to_specs(
    payloads: Sequence[TextureReplacementPayload],
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]],
) -> Tuple[MeshImportSupplementalFileSpec, ...]:
    specs: List[MeshImportSupplementalFileSpec] = []
    for payload in payloads:
        target_entry = _find_first_archive_entry_by_virtual_path(
            payload.target_path,
            archive_entries_by_normalized_path,
        )
        specs.append(
            MeshImportSupplementalFileSpec(
                source_path=payload.source_path,
                target_path=payload.target_path,
                kind=payload.kind,
                target_entry=target_entry,
                used_for_preview=True,
                payload_data=payload.payload_data,
                note=payload.note,
            )
        )
    return tuple(specs)


def _generated_texture_preview_file(payload: TextureReplacementPayload) -> Path:
    digest = hashlib.sha1(payload.payload_data).hexdigest()[:16]
    target_name = PurePosixPath(str(payload.target_path or "").replace("\\", "/")).name
    if not target_name:
        target_name = payload.source_path.with_suffix(".dds").name
    if not target_name.lower().endswith(".dds"):
        target_name = f"{Path(target_name).stem}.dds"
    output_dir = Path(tempfile.gettempdir()) / APP_NAME / "static_mesh_texture_previews"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{Path(target_name).stem}_{digest}.dds"
    if not output_path.is_file():
        output_path.write_bytes(payload.payload_data)
    return output_path


def _apply_generated_static_texture_previews(
    preview_model: ModelPreviewData,
    *,
    generated_payloads: Sequence[TextureReplacementPayload],
    texture_replacement_report: object,
    texconv_path: Optional[Path],
) -> int:
    if texconv_path is None or not getattr(preview_model, "meshes", None):
        return 0
    texture_payloads_by_target = {
        str(payload.target_path or "").replace("\\", "/").strip().lower(): payload
        for payload in generated_payloads
        if payload.kind == "texture_generated" and payload.payload_data
    }
    if not texture_payloads_by_target:
        return 0

    from crimson_forge_toolkit.core.archive import _resolve_model_texture_semantic_details
    from crimson_forge_toolkit.core.pipeline import ensure_dds_display_preview_png, parse_dds

    resolved_texconv_path = texconv_path.expanduser().resolve()
    preview_cache: Dict[str, str] = {}

    def _preview_path_for_payload(payload: TextureReplacementPayload) -> str:
        dds_path = _generated_texture_preview_file(payload)
        cache_key = dds_path.as_posix().lower()
        cached = preview_cache.get(cache_key, "")
        if cached:
            return cached
        dds_info = None
        try:
            dds_info = parse_dds(dds_path)
        except Exception:
            dds_info = None
        preview_path = ensure_dds_display_preview_png(resolved_texconv_path, dds_path, dds_info=dds_info)
        preview_cache[cache_key] = preview_path
        return preview_path

    def _tokens(value: str) -> set[str]:
        stop_words = {"cd", "phm", "pc", "texture", "textures", "dds", "png", "normal", "base", "color", "roughness", "metallic"}
        tokens: set[str] = set()
        for raw_token in re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split():
            token = re.sub(r"\d+$", "", raw_token.strip())
            if len(token) > 1 and token not in stop_words and not token.isdigit():
                tokens.add(token)
        return tokens

    def _mesh_match_score(mesh: ModelPreviewMesh, material_name: str, texture_path: str) -> float:
        mesh_material = str(getattr(mesh, "material_name", "") or "")
        mesh_name = str(getattr(mesh, "name", "") or "")
        material_key = str(material_name or "").strip().lower()
        mesh_material_key = mesh_material.strip().lower()
        if material_key and mesh_material_key == material_key:
            return 100.0
        query_tokens = _tokens(f"{material_name} {texture_path}")
        mesh_tokens = _tokens(f"{mesh_material} {mesh_name}")
        if not query_tokens or not mesh_tokens:
            return 0.0
        overlap = query_tokens & mesh_tokens
        score = float(len(overlap) * 12)
        for token in overlap:
            score += min(6.0, len(token) * 0.75)
        for query_token in query_tokens:
            for mesh_token in mesh_tokens:
                if len(query_token) >= 4 and len(mesh_token) >= 4 and (query_token in mesh_token or mesh_token in query_token):
                    score += 3.0
        return score

    def _candidate_meshes(material_name: str, texture_path: str) -> List[ModelPreviewMesh]:
        scored = [
            (_mesh_match_score(mesh, material_name, texture_path), mesh)
            for mesh in preview_model.meshes
        ]
        best_score = max((score for score, _mesh in scored), default=0.0)
        if best_score > 0.0:
            return [mesh for score, mesh in scored if score == best_score]
        return list(preview_model.meshes) if len(preview_model.meshes) == 1 else []

    assigned_count = 0
    slot_mappings = list(getattr(texture_replacement_report, "slot_mappings", ()) or ())
    source_material_by_target: Dict[str, str] = {}
    base_targets: set[str] = set()
    for mapping in slot_mappings:
        target_path = str(getattr(mapping, "output_texture_path", "") or "").replace("\\", "/").strip().lower()
        payload = texture_payloads_by_target.get(target_path)
        if payload is None:
            continue
        target_material_name = str(getattr(mapping, "target_material_name", "") or "")
        source_material_name = str(getattr(mapping, "source_material_name", "") or "")
        if target_material_name and source_material_name:
            source_material_by_target.setdefault(target_material_name.strip().lower(), source_material_name)
        try:
            preview_path = _preview_path_for_payload(payload)
        except Exception:
            continue
        slot_kind = str(getattr(mapping, "slot_kind", "") or "").strip().lower()
        if slot_kind == "base" and target_material_name:
            base_targets.add(target_material_name.strip().lower())
        source_name = getattr(getattr(mapping, "source_path", None), "name", "") or PurePosixPath(payload.target_path).name
        for mesh in _candidate_meshes(
            target_material_name,
            str(getattr(mapping, "target_texture_path", "") or ""),
        ):
            if slot_kind == "base":
                mesh.preview_texture_path = preview_path
                mesh.texture_name = source_name
                mesh.preview_texture_flip_vertical = False
                assigned_count += 1
            elif slot_kind == "normal":
                mesh.preview_normal_texture_path = preview_path
                mesh.preview_normal_texture_name = source_name
                mesh.preview_normal_texture_strength = 0.75
                assigned_count += 1
            elif slot_kind == "height":
                mesh.preview_height_texture_path = preview_path
                mesh.preview_height_texture_name = source_name
                assigned_count += 1
            elif slot_kind == "material":
                semantic_type, semantic_subtype, _confidence, packed_channels = _resolve_model_texture_semantic_details(source_name)
                mesh.preview_material_texture_path = preview_path
                mesh.preview_material_texture_name = source_name
                mesh.preview_material_texture_type = semantic_type
                mesh.preview_material_texture_subtype = semantic_subtype
                mesh.preview_material_texture_packed_channels = tuple(packed_channels)
                assigned_count += 1

    # PAC-driven sidecar generation binds by the rebuilt draw-section names.
    # If the first pass did not find a fuzzy match for a renamed/merged section,
    # assign by token overlap without requiring a texture-path match so the
    # preview follows the same material routing that will be packaged.
    for mapping in slot_mappings:
        target_material_name = str(getattr(mapping, "target_material_name", "") or "")
        target_path = str(getattr(mapping, "output_texture_path", "") or "").replace("\\", "/").strip().lower()
        payload = texture_payloads_by_target.get(target_path)
        if payload is None:
            continue
        try:
            preview_path = _preview_path_for_payload(payload)
        except Exception:
            continue
        slot_kind = str(getattr(mapping, "slot_kind", "") or "").strip().lower()
        source_name = getattr(getattr(mapping, "source_path", None), "name", "") or PurePosixPath(payload.target_path).name
        target_tokens = _tokens(target_material_name)
        for mesh in preview_model.meshes:
            mesh_tokens = _tokens(f"{getattr(mesh, 'material_name', '')} {getattr(mesh, 'name', '')}")
            if target_tokens and mesh_tokens and not (target_tokens & mesh_tokens):
                continue
            if slot_kind == "base" and not str(getattr(mesh, "preview_texture_path", "") or "").strip():
                mesh.preview_texture_path = preview_path
                mesh.texture_name = source_name
                mesh.preview_texture_flip_vertical = False
                assigned_count += 1
            elif slot_kind == "normal" and not str(getattr(mesh, "preview_normal_texture_path", "") or "").strip():
                mesh.preview_normal_texture_path = preview_path
                mesh.preview_normal_texture_name = source_name
                mesh.preview_normal_texture_strength = 0.75
                assigned_count += 1
            elif slot_kind == "height" and not str(getattr(mesh, "preview_height_texture_path", "") or "").strip():
                mesh.preview_height_texture_path = preview_path
                mesh.preview_height_texture_name = source_name
                assigned_count += 1
            elif slot_kind == "material" and not str(getattr(mesh, "preview_material_texture_path", "") or "").strip():
                semantic_type, semantic_subtype, _confidence, packed_channels = _resolve_model_texture_semantic_details(source_name)
                mesh.preview_material_texture_path = preview_path
                mesh.preview_material_texture_name = source_name
                mesh.preview_material_texture_type = semantic_type
                mesh.preview_material_texture_subtype = semantic_subtype
                mesh.preview_material_texture_packed_channels = tuple(packed_channels)
                assigned_count += 1
    texture_sets_by_source = {
        str(getattr(texture_set, "material_name", "") or "").strip().lower(): texture_set
        for texture_set in (getattr(texture_replacement_report, "texture_sets", ()) or ())
    }
    for target_material_key, source_material_name in source_material_by_target.items():
        if target_material_key in base_targets:
            continue
        texture_set = texture_sets_by_source.get(str(source_material_name or "").strip().lower())
        base_slot = getattr(texture_set, "slots", {}).get("base") if texture_set is not None else None
        source_path = getattr(base_slot, "source_path", None)
        if not isinstance(source_path, Path) or not source_path.is_file():
            continue
        for mesh in _candidate_meshes(target_material_key, ""):
            mesh.preview_texture_path = source_path.as_posix()
            mesh.texture_name = source_path.name
            mesh.preview_texture_flip_vertical = False
            assigned_count += 1
    return assigned_count


def build_mesh_import_preview(
    entry: ArchiveEntry,
    obj_path: Path,
    *,
    import_mode: str = "roundtrip",
    static_replacement_options: Optional[StaticMeshReplacementOptions] = None,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texconv_path: Optional[Path] = None,
    texture_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    visible_texture_mode: str = "mesh_base_first",
    supplemental_files: Sequence[Path] = (),
) -> MeshImportPreviewResult:
    from crimson_forge_toolkit.core.archive import (
        _attach_model_sidecar_texture_preview_paths,
        _attach_model_support_texture_preview_paths,
        _attach_model_texture_preview_paths,
        _extract_archive_model_sidecar_texture_references,
        _normalize_model_visible_texture_mode,
        build_archive_model_texture_references,
        read_archive_entry_data,
    )

    imported_mesh = import_scene_mesh(obj_path)
    imported_mesh.path = entry.path
    imported_mesh.format = entry.extension.lstrip(".").lower()
    manifest_payload = _load_obj_roundtrip_sidecar(str(obj_path)) if obj_path.suffix.lower() == ".obj" else None
    original_data, _decompressed, _note = read_archive_entry_data(entry)
    original_mesh = parse_mesh(original_data, entry.path)
    normalized_import_mode = str(import_mode or "roundtrip").strip().lower()
    static_mappings = []
    enable_missing_base_color_parameters = False
    if normalized_import_mode in {"static", "static_replacement", "static-mesh-replacement"}:
        base_static_options = static_replacement_options or StaticMeshReplacementOptions()
        enable_missing_base_color_parameters = bool(
            getattr(base_static_options, "enable_missing_base_color_parameters", False)
        )
        static_mappings = base_static_options.submesh_mappings or suggest_static_submesh_mappings(
            original_mesh,
            imported_mesh,
        )
        if not base_static_options.submesh_mappings:
            base_static_options = dataclasses.replace(base_static_options, submesh_mappings=static_mappings)
        rebuilt_data, static_report = build_static_mesh_replacement(
            original_data,
            original_mesh,
            imported_mesh,
            base_static_options,
        )
        normalized_import_mode = "static_replacement"
    else:
        if obj_path.suffix.lower() != ".obj":
            raise ValueError("Round-trip edit import only supports OBJ. Use Static Mesh Replacement for DAE/FBX imports.")
        static_report = None
        normalized_import_mode = "roundtrip"
        rebuilt_data = build_mesh(imported_mesh, original_data)
    parsed_mesh = parse_mesh(rebuilt_data, entry.path)
    restored_texture_identity_count = _restore_rebuilt_mesh_texture_identity(imported_mesh, parsed_mesh)
    preview_model = parsed_mesh_to_preview_model(parsed_mesh)

    summary_lines = [
        f"Preview rebuilt mesh for {entry.path}",
        f"Import mode: {'Static mesh replacement' if normalized_import_mode == 'static_replacement' else 'Round-trip edit'}",
        f"Vertices: {parsed_mesh.total_vertices:,}",
        f"Faces: {parsed_mesh.total_faces:,}",
        f"Submeshes: {len(parsed_mesh.submeshes):,}",
        f"Rebuilt size: {len(rebuilt_data):,} bytes",
    ]
    if static_report is not None:
        summary_lines.append(
            "Static replacement analysis: "
            f"original {static_report.original_submesh_count} submesh(es), "
            f"replacement {static_report.replacement_submesh_count} source submesh(es)."
        )
        if static_report.mapping_summary:
            summary_lines.append("Static replacement mapping:")
            summary_lines.extend(f"  {line}" for line in static_report.mapping_summary)
        if static_report.warnings:
            summary_lines.append("Static replacement warnings:")
            summary_lines.extend(f"  {line}" for line in static_report.warnings)
        if static_report.alignment_summary:
            summary_lines.append("Static replacement alignment:")
            summary_lines.extend(f"  {line}" for line in static_report.alignment_summary)
    if restored_texture_identity_count > 0:
        summary_lines.append(
            f"Restored {restored_texture_identity_count:,} imported submesh texture identifier(s) onto rebuilt preview metadata."
        )
    resolved_supplemental_files = tuple(
        path.expanduser().resolve()
        for path in supplemental_files
        if isinstance(path, Path) and path.expanduser().resolve().is_file()
    )
    if normalized_import_mode == "static_replacement":
        auto_scene_texture_files = tuple(
            path for path in discover_scene_texture_files(obj_path, imported_mesh) if path.is_file()
        )
        if auto_scene_texture_files:
            seen_supplemental = {str(path).lower() for path in resolved_supplemental_files}
            appended = [path for path in auto_scene_texture_files if str(path).lower() not in seen_supplemental]
            if appended:
                resolved_supplemental_files = tuple(resolved_supplemental_files) + tuple(appended)
                summary_lines.append(
                    f"Auto-discovered {len(appended):,} texture source file(s) next to the imported scene."
                )
    if resolved_supplemental_files:
        summary_lines.append(f"Selected supplemental files: {len(resolved_supplemental_files):,}")
    sidecar_texture_references: Tuple[object, ...] = ()
    sidecar_reference_paths: Tuple[str, ...] = ()
    sidecar_texts_by_normalized_path: Dict[str, Tuple[str, ...]] = {}
    sidecar_texts_by_basename: Dict[str, Tuple[str, ...]] = {}
    original_archive_sidecar_texture_references: Tuple[object, ...] = ()
    original_archive_sidecar_reference_paths: Tuple[str, ...] = ()
    selected_sidecar_texture_references: Tuple[object, ...] = ()
    selected_sidecar_reference_paths: Tuple[str, ...] = ()
    selected_sidecar_texts_by_normalized_path: Dict[str, Tuple[str, ...]] = {}
    selected_sidecar_texts_by_basename: Dict[str, Tuple[str, ...]] = {}
    normalized_visible_texture_mode = _normalize_model_visible_texture_mode(visible_texture_mode)
    if resolved_supplemental_files:
        (
            selected_sidecar_texture_references,
            selected_sidecar_reference_paths,
            selected_sidecar_texts_by_normalized_path,
            selected_sidecar_texts_by_basename,
        ) = _build_selected_sidecar_texture_bindings(resolved_supplemental_files)
        if selected_sidecar_texture_references:
            summary_lines.append(
                f"Using {len(selected_sidecar_texture_references):,} texture binding(s) from selected local sidecar file(s): {', '.join(selected_sidecar_reference_paths[:3])}"
                + (" ..." if len(selected_sidecar_reference_paths) > 3 else "")
            )
    if texture_entries_by_basename is not None:
        (
            original_archive_sidecar_texture_references,
            original_archive_sidecar_reference_paths,
            sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename,
        ) = _extract_archive_model_sidecar_texture_references(
            entry,
            archive_entries_by_basename=(
                dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
            ),
        )
        if original_archive_sidecar_texture_references and not selected_sidecar_texture_references:
            sidecar_suffix = (
                f" from {', '.join(original_archive_sidecar_reference_paths[:2])}"
                if original_archive_sidecar_reference_paths
                else ""
            )
            if len(original_archive_sidecar_reference_paths) > 2:
                sidecar_suffix += " ..."
            summary_lines.append(
                f"Companion material sidecar data contributed {len(original_archive_sidecar_texture_references):,} texture binding(s){sidecar_suffix}."
            )
            summary_lines.append(
                "Loose mesh mods may still need the matching companion .xml sidecar when custom material or texture remaps are involved."
            )
    if original_archive_sidecar_texture_references:
        sidecar_texture_references = original_archive_sidecar_texture_references
        sidecar_reference_paths = original_archive_sidecar_reference_paths
    if selected_sidecar_texture_references:
        sidecar_texture_references = selected_sidecar_texture_references
        sidecar_reference_paths = selected_sidecar_reference_paths
        sidecar_texts_by_normalized_path = selected_sidecar_texts_by_normalized_path
        sidecar_texts_by_basename = selected_sidecar_texts_by_basename
    texture_references: Tuple[ArchiveModelTextureReference, ...] = ()
    if texconv_path is not None:
        if sidecar_texture_references:
            summary_lines.extend(
                _attach_model_sidecar_texture_preview_paths(
                    texconv_path,
                    entry,
                    preview_model,
                    parsed_mesh=parsed_mesh,
                    sidecar_texture_bindings=sidecar_texture_references,
                    visible_texture_mode=normalized_visible_texture_mode,
                    texture_entries_by_normalized_path=(
                        dict(texture_entries_by_normalized_path) if texture_entries_by_normalized_path is not None else None
                    ),
                    texture_entries_by_basename=(
                        dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
                    ),
                    sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                    sidecar_texts_by_basename=sidecar_texts_by_basename,
                )
            )
        summary_lines.extend(
            _attach_model_texture_preview_paths(
                texconv_path,
                entry,
                preview_model,
                texture_entries_by_normalized_path=(
                    dict(texture_entries_by_normalized_path) if texture_entries_by_normalized_path is not None else None
                ),
                texture_entries_by_basename=(
                    dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
                ),
                sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                sidecar_texts_by_basename=sidecar_texts_by_basename,
            )
        )
        if sidecar_texture_references and normalized_visible_texture_mode == "mesh_base_first":
            summary_lines.extend(
                _attach_model_sidecar_texture_preview_paths(
                    texconv_path,
                    entry,
                    preview_model,
                    parsed_mesh=parsed_mesh,
                    sidecar_texture_bindings=sidecar_texture_references,
                    visible_texture_mode="layer_aware_visible",
                    texture_entries_by_normalized_path=(
                        dict(texture_entries_by_normalized_path) if texture_entries_by_normalized_path is not None else None
                    ),
                    texture_entries_by_basename=(
                        dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
                    ),
                    sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                    sidecar_texts_by_basename=sidecar_texts_by_basename,
                    fallback_only=True,
                )
            )
        summary_lines.extend(
            _attach_model_support_texture_preview_paths(
                texconv_path,
                entry,
                preview_model,
                parsed_mesh=parsed_mesh,
                sidecar_texture_bindings=sidecar_texture_references,
                texture_entries_by_normalized_path=(
                    dict(texture_entries_by_normalized_path) if texture_entries_by_normalized_path is not None else None
                ),
                texture_entries_by_basename=(
                    dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
                ),
                sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                sidecar_texts_by_basename=sidecar_texts_by_basename,
            )
        )
        if resolved_supplemental_files:
            supplemental_dds_by_normalized_path, supplemental_dds_by_basename = _build_mesh_import_local_dds_lookup(
                resolved_supplemental_files
            )
            if selected_sidecar_texture_references:
                summary_lines.extend(
                    _apply_mesh_import_local_sidecar_texture_overrides(
                        preview_model,
                        parsed_mesh,
                        selected_sidecar_texture_references,
                        supplemental_dds_by_normalized_path,
                        supplemental_dds_by_basename,
                        texconv_path=texconv_path,
                    )
                )
                summary_lines.extend(
                    _apply_mesh_import_local_support_texture_overrides(
                        preview_model,
                        parsed_mesh,
                        selected_sidecar_texture_references,
                        supplemental_dds_by_normalized_path,
                        supplemental_dds_by_basename,
                        texconv_path=texconv_path,
                    )
                )
            summary_lines.extend(
                _apply_mesh_import_local_texture_overrides(
                    preview_model,
                    supplemental_dds_by_normalized_path,
                    supplemental_dds_by_basename,
                    texconv_path=texconv_path,
                )
            )
    texture_references = tuple(
        build_archive_model_texture_references(
            entry,
            preview_model,
            parsed_mesh=parsed_mesh,
            sidecar_texture_references=sidecar_texture_references,
            texture_entries_by_normalized_path=(
                dict(texture_entries_by_normalized_path) if texture_entries_by_normalized_path is not None else None
            ),
            texture_entries_by_basename=(
                dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
            ),
            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename=sidecar_texts_by_basename,
        )
    )
    supplemental_file_specs = _build_mesh_import_supplemental_file_specs(
        entry,
        resolved_supplemental_files,
        texture_references,
        archive_entries_by_normalized_path=archive_entries_by_normalized_path,
        archive_entries_by_basename=texture_entries_by_basename,
    )
    if normalized_import_mode == "static_replacement" and resolved_supplemental_files:
        original_sidecars = _collect_original_mesh_sidecar_texts(entry, texture_entries_by_basename)
        texture_source_files = tuple(
            path for path in resolved_supplemental_files if path.suffix.lower() in SCENE_TEXTURE_SOURCE_EXTENSIONS
        )
        if texture_source_files:
            try:
                generated_payloads, texture_replacement_report = build_texture_replacement_payloads(
                    obj_mesh=imported_mesh,
                    rebuilt_mesh=parsed_mesh,
                    texture_files=texture_source_files,
                    original_texture_refs=texture_references,
                    original_sidecars=original_sidecars,
                    submesh_mappings=static_mappings,
                    texconv_path=texconv_path,
                    read_original_texture_bytes=_mesh_texture_original_bytes,
                    original_texture_source_path=_mesh_texture_original_source_path,
                    enable_missing_base_color_parameters=enable_missing_base_color_parameters,
                    texture_slot_overrides=tuple(
                        getattr(static_replacement_options, "texture_slot_overrides", ()) or ()
                    ),
                    pac_driven_sidecar=bool(
                        getattr(static_replacement_options, "rebuild_material_sidecar", True)
                    ),
                )
            except Exception as exc:
                generated_payloads = []
                texture_replacement_report = None
                summary_lines.append(f"Static texture replacement failed: {exc}")
            if texture_replacement_report is not None:
                if texture_replacement_report.slot_mappings:
                    summary_lines.append("Static texture replacement mapping:")
                    summary_lines.extend(
                        "  "
                        f"{mapping.source_material_name} {mapping.slot_kind} "
                        f"({mapping.source_path.name}) -> {mapping.output_texture_path}"
                        for mapping in texture_replacement_report.slot_mappings[:16]
                    )
                    if len(texture_replacement_report.slot_mappings) > 16:
                        summary_lines.append(
                            f"  ... {len(texture_replacement_report.slot_mappings) - 16:,} more texture mapping(s)"
                        )
                if texture_replacement_report.warnings:
                    summary_lines.append("Static texture replacement warnings:")
                    summary_lines.extend(f"  {warning}" for warning in texture_replacement_report.warnings)
                if texture_replacement_report.errors:
                    summary_lines.append("Static texture replacement errors:")
                    summary_lines.extend(f"  {error}" for error in texture_replacement_report.errors)
                if (
                    not texture_replacement_report.slot_mappings
                    and not texture_replacement_report.warnings
                    and not texture_replacement_report.errors
                ):
                    summary_lines.append(
                        "Static texture replacement found no matching original texture bindings for the selected PNG/DDS files."
                    )
            if generated_payloads:
                preview_assignment_count = _apply_generated_static_texture_previews(
                    preview_model,
                    generated_payloads=generated_payloads,
                    texture_replacement_report=texture_replacement_report,
                    texconv_path=texconv_path,
                )
                if preview_assignment_count > 0:
                    summary_lines.append(
                        f"Applied {preview_assignment_count:,} generated static texture preview slot(s) from PNG/DDS replacements."
                    )
                elif texconv_path is None:
                    summary_lines.append(
                        "Generated static texture payloads were not shown in preview because texconv.exe is not configured."
                    )
                generated_specs = _texture_replacement_payloads_to_specs(
                    generated_payloads,
                    archive_entries_by_normalized_path=archive_entries_by_normalized_path,
                )
                supplemental_file_specs = tuple(supplemental_file_specs) + generated_specs
                generated_texture_count = sum(1 for payload in generated_payloads if payload.kind == "texture_generated")
                generated_sidecar_count = sum(1 for payload in generated_payloads if payload.kind == "sidecar_generated")
                summary_lines.append(
                    f"Generated static replacement payloads: {generated_texture_count:,} texture(s), {generated_sidecar_count:,} sidecar(s)."
                )
            if generated_payloads and not original_sidecars:
                summary_lines.append(
                    "Generated replacement texture payloads without a patched material sidecar because no original sidecar text was available."
                )
    if supplemental_file_specs:
        mapped_count = sum(1 for spec in supplemental_file_specs if spec.target_path)
        unmapped_count = len(supplemental_file_specs) - mapped_count
        summary_lines.append(f"Supplemental files mapped to package/archive targets: {mapped_count:,}")
        if unmapped_count > 0:
            summary_lines.append(
                f"{unmapped_count:,} supplemental file(s) could not be mapped to a known game-relative target automatically."
            )

    paired_lod_data: Optional[bytes] = None
    paired_lod_path = ""
    if (
        normalized_import_mode == "roundtrip"
        and entry.extension == ".pam"
        and archive_entries_by_normalized_path is not None
    ):
        paired_path = f"{Path(entry.path).with_suffix('.pamlod').as_posix()}"
        paired_candidates = archive_entries_by_normalized_path.get(_normalize_virtual_path(paired_path), ())
        if paired_candidates:
            paired_entry = paired_candidates[0]
            paired_original, _paired_decompressed, _paired_note = read_archive_entry_data(paired_entry)
            paired_mesh = transfer_pam_edit_to_pamlod_mesh(imported_mesh, original_data, paired_original, paired_entry.path)
            paired_lod_data = build_mesh(paired_mesh, paired_original)
            paired_lod_path = paired_entry.path
            summary_lines.append(f"Paired PAMLOD rebuild prepared: {paired_entry.path}")

    import_diffs, import_issues, auto_fix_result, validation_summary_lines = _build_mesh_import_validation(
        entry,
        original_mesh,
        parsed_mesh,
        import_mode=normalized_import_mode,
        texture_references=texture_references,
        supplemental_file_specs=supplemental_file_specs,
        original_sidecar_bindings=original_archive_sidecar_texture_references,
        selected_sidecar_bindings=selected_sidecar_texture_references,
        paired_lod_path=paired_lod_path,
        manifest_payload=manifest_payload,
    )
    summary_lines.extend(validation_summary_lines)

    return MeshImportPreviewResult(
        rebuilt_data=rebuilt_data,
        parsed_mesh=parsed_mesh,
        preview_model=preview_model,
        summary_lines=summary_lines,
        import_mode=normalized_import_mode,
        texture_references=texture_references,
        supplemental_file_specs=supplemental_file_specs,
        paired_lod_data=paired_lod_data,
        paired_lod_path=paired_lod_path,
        import_diffs=import_diffs,
        import_issues=import_issues,
        auto_fix_result=auto_fix_result,
        roundtrip_manifest=manifest_payload if isinstance(manifest_payload, dict) else None,
    )


def _ffmpeg_candidates() -> List[Path]:
    candidates: List[Path] = []
    env_path = shutil.which("ffmpeg")
    if env_path:
        candidates.append(Path(env_path))
    runtime_roots = [Path.cwd()]
    meipass = getattr(__import__("sys"), "_MEIPASS", "")
    if meipass:
        runtime_roots.append(Path(meipass))
    runtime_roots.append(Path(__file__).resolve().parents[2])
    for root in runtime_roots:
        for relative in ("ffmpeg/ffmpeg.exe", ".tools/ffmpeg/ffmpeg.exe", "ffmpeg.exe"):
            path = root / relative
            if path.is_file():
                candidates.append(path)
    seen: set[str] = set()
    unique: List[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _run_hidden_process(command: Sequence[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    popen_kwargs: Dict[str, object] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if os.name == "nt":
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if creation_flags:
            popen_kwargs["creationflags"] = creation_flags
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        popen_kwargs["startupinfo"] = startup_info
    return subprocess.run(list(command), **popen_kwargs)


def _convert_audio_to_wav(source_path: Path, output_path: Path) -> Path:
    if source_path.suffix.lower() == ".wav":
        shutil.copy2(source_path, output_path)
        return output_path

    ffmpeg_path = next(iter(_ffmpeg_candidates()), None)
    if ffmpeg_path is None:
        raise ValueError("ffmpeg.exe is required to convert this audio stream to WAV.")
    result = _run_hidden_process(
        [str(ffmpeg_path), "-y", "-i", str(source_path), "-ar", "48000", "-ac", "1", "-sample_fmt", "s16", str(output_path)],
        timeout=180,
    )
    if result.returncode != 0 or not output_path.is_file():
        raise ValueError(result.stderr.strip() or "ffmpeg could not convert the selected audio stream.")
    return output_path


def _decode_with_vgmstream(source_path: Path, output_path: Path) -> Path:
    from crimson_forge_toolkit.core.archive import _resolve_vgmstream_cli_path

    cli_path = _resolve_vgmstream_cli_path()
    if cli_path is None:
        raise ValueError("Bundled vgmstream decoder is not available in this build.")
    result = _run_hidden_process([str(cli_path), "-o", str(output_path), str(source_path)], timeout=180)
    if result.returncode != 0 or not output_path.is_file():
        raise ValueError(result.stderr.strip() or "vgmstream-cli could not decode this Wwise stream.")
    return output_path


def export_archive_audio_as_wav(entry: ArchiveEntry, output_path: Path) -> Path:
    from crimson_forge_toolkit.core.archive import ensure_archive_preview_source

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_path, _note = ensure_archive_preview_source(entry)
    extension = entry.extension.lower()
    if extension in {".wem", ".bnk"}:
        return _decode_with_vgmstream(source_path, output_path)
    return _convert_audio_to_wav(source_path, output_path)


@dataclass(slots=True, frozen=True)
class _WavFormatInfo:
    audio_format: int
    channels: int
    sample_rate: int
    bits_per_sample: int


def _iter_riff_chunks(data: bytes, *, max_chunks: int = 64) -> List[Tuple[bytes, int, int]]:
    chunks: List[Tuple[bytes, int, int]] = []
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return chunks
    offset = 12
    while offset + 8 <= len(data) and len(chunks) < max_chunks:
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        data_offset = offset + 8
        if data_offset > len(data):
            break
        chunks.append((chunk_id, chunk_size, data_offset))
        next_offset = data_offset + chunk_size
        if next_offset <= offset:
            break
        offset = next_offset + (chunk_size % 2)
    return chunks


def _read_wav_format_info_from_bytes(data: bytes) -> Optional[_WavFormatInfo]:
    for chunk_id, chunk_size, chunk_offset in _iter_riff_chunks(data):
        if chunk_id != b"fmt " or chunk_size < 16 or chunk_offset + 16 > len(data):
            continue
        try:
            audio_format, channels, sample_rate, _byte_rate, _block_align, bits_per_sample = struct.unpack_from(
                "<HHIIHH",
                data,
                chunk_offset,
            )
        except struct.error:
            return None
        return _WavFormatInfo(
            audio_format=int(audio_format),
            channels=int(channels),
            sample_rate=int(sample_rate),
            bits_per_sample=int(bits_per_sample),
        )
    return None


def _read_wav_format_info(audio_path: Path, *, header_limit: int = 262_144) -> Optional[_WavFormatInfo]:
    try:
        with audio_path.open("rb") as handle:
            header = handle.read(max(64, int(header_limit)))
    except OSError:
        return None
    return _read_wav_format_info_from_bytes(header)


def _normalize_audio_input(audio_path: Path, *, sample_rate: int, channels: int) -> Path:
    if audio_path.suffix.lower() == ".wav":
        wav_info = _read_wav_format_info(audio_path)
        if (
            wav_info is not None
            and wav_info.audio_format == 1
            and wav_info.sample_rate == int(sample_rate)
            and wav_info.channels == int(channels)
            and wav_info.bits_per_sample == 16
        ):
            return audio_path
    temp_root = Path(tempfile.gettempdir()) / APP_NAME / "audio_patch"
    temp_root.mkdir(parents=True, exist_ok=True)
    output_path = temp_root / f"{audio_path.stem}_{sample_rate}hz_{channels}ch.wav"
    ffmpeg_path = next(iter(_ffmpeg_candidates()), None)
    if ffmpeg_path is None:
        raise ValueError("ffmpeg.exe is required to normalize replacement audio for patching.")
    result = _run_hidden_process(
        [str(ffmpeg_path), "-y", "-i", str(audio_path), "-ar", str(sample_rate), "-ac", str(channels), "-sample_fmt", "s16", str(output_path)],
        timeout=180,
    )
    if result.returncode != 0 or not output_path.is_file():
        raise ValueError(result.stderr.strip() or "ffmpeg could not normalize the selected replacement audio.")
    return output_path


def _build_pcm_wem(wav_data: bytes, sample_rate: int, channels: int) -> bytes:
    pcm_data = b""
    if wav_data[:4] == b"RIFF" and wav_data[8:12] == b"WAVE":
        cursor = 12
        while cursor + 8 <= len(wav_data):
            chunk_id = wav_data[cursor : cursor + 4]
            chunk_size = struct.unpack_from("<I", wav_data, cursor + 4)[0]
            if chunk_id == b"data":
                pcm_data = wav_data[cursor + 8 : cursor + 8 + chunk_size]
                break
            cursor += 8 + chunk_size
    else:
        pcm_data = wav_data

    if not pcm_data:
        return wav_data

    bits_per_sample = 16
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    data_chunk = b"data" + struct.pack("<I", len(pcm_data)) + pcm_data
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + data_chunk


def build_archive_audio_patch_payload(entry: ArchiveEntry, replacement_audio_path: Path) -> bytes:
    from crimson_forge_toolkit.core.archive import read_archive_entry_data

    normalized_path = replacement_audio_path.expanduser().resolve()
    if not normalized_path.is_file():
        raise FileNotFoundError(f"Audio replacement file was not found: {normalized_path}")

    if entry.extension == ".wav":
        return normalized_path.read_bytes()

    if entry.extension != ".wem":
        raise ValueError(f"Archive audio patching currently supports .wav and .wem targets only, not {entry.extension}.")

    original_data, _decompressed, _note = read_archive_entry_data(entry)
    sample_rate = 48000
    channels = 1
    original_wav_info = _read_wav_format_info_from_bytes(original_data)
    if original_wav_info is not None:
        channels = max(1, int(original_wav_info.channels))
        sample_rate = max(1, int(original_wav_info.sample_rate))
    normalized_wav = _normalize_audio_input(normalized_path, sample_rate=sample_rate, channels=channels)
    return _build_pcm_wem(normalized_wav.read_bytes(), sample_rate, channels)


def build_pab_preview(data: bytes, virtual_path: str) -> SkeletonPreviewResult:
    skeleton = parse_pab(data, virtual_path)
    lines = [f"PAB skeleton preview for {virtual_path}"]
    parser_mode = str(getattr(skeleton, "parser_mode", "") or "fixed")
    tail_data = bytes(getattr(skeleton, "tail_data", b"") or b"")
    parse_warning = str(getattr(skeleton, "parse_warning", "") or "").strip()
    detail_lines = [
        f"Declared bones: {int(getattr(skeleton, 'bone_count', len(skeleton.bones)) or 0):,}",
        f"Parsed bones: {len(skeleton.bones):,}",
        f"Parser mode: {parser_mode}",
        f"Tail data: {len(tail_data):,} bytes",
    ]
    if parse_warning:
        detail_lines.append(parse_warning)
    if not skeleton.bones:
        lines.append("No bones were recovered.")
        return SkeletonPreviewResult(preview_text="\n".join(lines), detail_lines=detail_lines)

    root_bones = [bone for bone in skeleton.bones if bone.parent_index < 0]
    named_bones = [bone for bone in skeleton.bones if str(bone.name or "").strip()]
    child_map: Dict[int, List[int]] = {}
    for bone in skeleton.bones:
        child_map.setdefault(int(bone.parent_index), []).append(int(bone.index))

    def _depth(index: int) -> int:
        children = child_map.get(index, [])
        if not children:
            return 1
        return 1 + max(_depth(child_index) for child_index in children)

    max_depth = max((_depth(root.index) for root in root_bones), default=0)
    positions = [
        tuple(float(component) for component in bone.position)
        for bone in skeleton.bones
        if len(tuple(bone.position)) >= 3
    ]
    detail_lines.append(f"Root bones: {len(root_bones):,}")
    detail_lines.append(f"Named bones: {len(named_bones):,}")
    detail_lines.append(f"Max hierarchy depth: {max_depth}")
    if positions:
        min_x = min(position[0] for position in positions)
        min_y = min(position[1] for position in positions)
        min_z = min(position[2] for position in positions)
        max_x = max(position[0] for position in positions)
        max_y = max(position[1] for position in positions)
        max_z = max(position[2] for position in positions)
        detail_lines.append(
            "Bone position bounds: "
            f"min=({min_x:.3f}, {min_y:.3f}, {min_z:.3f}) "
            f"max=({max_x:.3f}, {max_y:.3f}, {max_z:.3f})"
        )
    lines.extend(
        [
            "",
            "Summary:",
            f"- Declared bones: {int(getattr(skeleton, 'bone_count', len(skeleton.bones)) or 0):,}",
            f"- Bones: {len(skeleton.bones):,}",
            f"- Root bones: {len(root_bones):,}",
            f"- Named bones: {len(named_bones):,}",
            f"- Max hierarchy depth: {max_depth}",
            f"- Parser mode: {parser_mode}",
            f"- Tail data: {len(tail_data):,} bytes",
        ]
    )
    if parse_warning:
        lines.append(f"- Warning: {parse_warning}")
    if root_bones:
        lines.append("- Root names: " + ", ".join((bone.name or "<unnamed>") for bone in root_bones[:8]))
        if len(root_bones) > 8:
            lines[-1] += " ..."
    lines.append("")
    lines.append("Bone hierarchy:")
    for bone in skeleton.bones[:128]:
        parent_text = "root" if bone.parent_index < 0 else f"parent {bone.parent_index}"
        position_text = ""
        if len(tuple(bone.position)) >= 3:
            position_text = f" pos=({bone.position[0]:.3f}, {bone.position[1]:.3f}, {bone.position[2]:.3f})"
        lines.append(f"[{bone.index:03d}] {bone.name or '<unnamed>'} ({parent_text}){position_text}")
    if len(skeleton.bones) > 128:
        lines.append("")
        lines.append("Preview truncated to the first 128 bones.")
    return SkeletonPreviewResult(preview_text="\n".join(lines), detail_lines=detail_lines)


def build_hkx_preview(data: bytes, virtual_path: str) -> HkxPreviewResult:
    printable = []
    current = bytearray()
    for value in data[:262144]:
        if 32 <= value <= 126:
            current.append(value)
            continue
        if len(current) >= 4:
            printable.append(current.decode("ascii", errors="ignore"))
            if len(printable) >= 96:
                break
        current.clear()
    class_names = sorted({item for item in printable if item.startswith("hk") or item.startswith("hka") or item.startswith("hkp")})
    asset_references = []
    seen_asset_references: set[str] = set()
    for text in printable:
        normalized = str(text or "").strip()
        lowered = normalized.lower()
        if not normalized or lowered in seen_asset_references:
            continue
        if any(
            lowered.endswith(extension)
            for extension in (
                ".hkx",
                ".motionblending",
                ".pab",
                ".pac",
                ".pam",
                ".pamlod",
                ".xml",
                ".pami",
                ".dds",
            )
        ):
            seen_asset_references.add(lowered)
            asset_references.append(normalized)
    markers = [
        marker
        for marker in ("hkRootLevelContainer", "hkaAnimation", "hkaSkeleton", "hkaAnimationBinding", "hkpPhysicsData")
        if any(marker in item for item in printable)
    ]
    lines = [f"HKX tagfile preview for {virtual_path}", ""]
    detail_lines = ["Structured Havok tagfile or binary animation metadata detected."]
    if class_names:
        detail_lines.append(f"Detected {len(class_names):,} Havok class/type marker(s).")
        lines.append("Detected classes/types:")
        lines.extend(class_names[:64])
    if markers:
        detail_lines.append(f"Detected structured marker(s): {', '.join(markers[:6])}.")
        lines.extend(["", "Detected markers:"])
        lines.extend(markers[:12])
    if asset_references:
        detail_lines.append(f"Detected {len(asset_references):,} related asset reference(s).")
        lines.extend(["", "Detected asset references:"])
        lines.extend(asset_references[:24])
        if len(asset_references) > 24:
            lines.append(f"... {len(asset_references) - 24} more")
    elif printable:
        lines.append("Readable strings:")
        lines.extend(printable[:64])
    else:
        lines.append("No readable Havok strings were recovered from the preview sample.")
    if len(printable) >= 96:
        lines.append("")
        lines.append("String scan truncated to keep the preview responsive.")
    return HkxPreviewResult(preview_text="\n".join(lines), detail_lines=detail_lines)
