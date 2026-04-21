from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import lz4.block as lz4_block
except Exception:  # pragma: no cover - optional dependency
    lz4_block = None  # type: ignore[assignment]

from crimson_forge_toolkit.constants import APP_NAME
from crimson_forge_toolkit.core.common import raise_if_cancelled
from crimson_forge_toolkit.core.model_preview import _build_lod_summary, _build_model_preview
from crimson_forge_toolkit.models import ArchiveEntry, ModelPreviewData, ModelPreviewMesh, ModPackageInfo
from crimson_forge_toolkit.modding.mesh_exporter import export_fbx, export_fbx_with_skeleton, export_obj
from crimson_forge_toolkit.modding.mesh_importer import build_mesh, import_obj, transfer_pam_edit_to_pamlod_mesh
from crimson_forge_toolkit.modding.mesh_parser import ParsedMesh, SubMesh, parse_mesh
from crimson_forge_toolkit.modding.skeleton_parser import Skeleton, parse_pab


ARCHIVE_MESH_EXTENSIONS = {".pam", ".pamlod", ".pac"}
ARCHIVE_AUDIO_PATCH_EXTENSIONS = {".wem", ".wav"}
ARCHIVE_AUDIO_EXPORT_EXTENSIONS = {".wem", ".wav", ".ogg", ".mp3", ".bnk"}
ARCHIVE_PATCH_BACKUP_ROOT = Path(tempfile.gettempdir()) / APP_NAME / "archive_patch_backups"


@dataclass(slots=True)
class MeshExportResult:
    output_paths: List[Path]
    summary_lines: List[str]


@dataclass(slots=True)
class MeshImportPreviewResult:
    rebuilt_data: bytes
    parsed_mesh: ParsedMesh
    preview_model: ModelPreviewData
    summary_lines: List[str]
    texture_references: Tuple[ArchiveModelTextureReference, ...] = ()
    supplemental_file_specs: Tuple["MeshImportSupplementalFileSpec", ...] = ()
    paired_lod_data: Optional[bytes] = None
    paired_lod_path: str = ""


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


def _export_related_archive_entries(
    entries: Sequence[ArchiveEntry],
    output_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    from crimson_forge_toolkit.core.archive import ensure_archive_preview_source

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
        source_path, _note = ensure_archive_preview_source(entry)
        target_path = output_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(on_log, f"Copying related file: {target_path.relative_to(output_root).as_posix()}")
        shutil.copy2(source_path, target_path)
        written_paths.append(target_path)
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
    from crimson_forge_toolkit.core.mod_package import resolve_mod_package_root, write_mod_package_info

    if not requests:
        raise ValueError("No archive payloads were provided for mod-ready loose export.")

    resolved_parent_root = parent_root.expanduser().resolve()
    package_root = resolve_mod_package_root(resolved_parent_root, package_info)
    package_root.mkdir(parents=True, exist_ok=True)
    write_mod_package_info(package_root, package_info, create_no_encrypt_file=create_no_encrypt_file)

    written_files: List[Path] = []
    for request in requests:
        relative_parts = PurePosixPath(request.entry.path.replace("\\", "/")).parts
        if not relative_parts:
            raise ValueError(f"Archive path is invalid: {request.entry.path}")
        target_path = package_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(on_log, f"Writing loose mod payload: {target_path.relative_to(package_root)}")
        target_path.write_bytes(request.payload_data)
        written_files.append(target_path)

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
        resolve_mod_package_root,
        write_mesh_loose_mod_package_metadata,
    )
    from crimson_forge_toolkit.core.archive import ensure_archive_preview_source

    if not requests:
        raise ValueError("No archive payloads were provided for mesh mod-ready loose export.")

    resolved_parent_root = parent_root.expanduser().resolve()
    package_root = resolve_mod_package_root(resolved_parent_root, package_info)
    files_root = package_root / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    written_files: List[Path] = []
    file_rows: List[MeshLooseModFile] = []
    source_obj_display = source_obj_path.expanduser().resolve().as_posix()
    paired_lod_path = (preview_result.paired_lod_path or "").strip().replace("\\", "/")
    primary_path = primary_entry.path.replace("\\", "/")
    written_virtual_paths: set[str] = set()
    for request in requests:
        relative_parts = PurePosixPath(request.entry.path.replace("\\", "/")).parts
        if not relative_parts:
            raise ValueError(f"Archive path is invalid: {request.entry.path}")
        target_path = files_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(on_log, f"Writing loose mesh payload: files/{target_path.relative_to(files_root).as_posix()}")
        target_path.write_bytes(request.payload_data)
        written_files.append(target_path)
        note = ""
        normalized_request_path = request.entry.path.replace("\\", "/")
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
        if isinstance(spec, MeshImportSupplementalFileSpec) and spec.source_path.expanduser().resolve().is_file()
    ]
    for spec in supplemental_specs:
        normalized_target_path = str(spec.target_path or "").strip().replace("\\", "/")
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
        target_path = files_root.joinpath(*relative_parts)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_log(
            on_log,
            f"Copying selected supplemental file: files/{target_path.relative_to(files_root).as_posix()}",
        )
        shutil.copy2(spec.source_path, target_path)
        if target_path not in written_files:
            written_files.append(target_path)
        written_virtual_paths.add(normalized_target_path.lower())
        if spec.kind == "sidecar":
            note = f"Selected local sidecar included for {primary_entry.path}"
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
                format=spec.source_path.suffix.lstrip(".").lower(),
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
            normalized_related_path = related_entry.path.replace("\\", "/")
            if normalized_related_path.lower() in written_virtual_paths:
                continue
            relative_parts = PurePosixPath(normalized_related_path).parts
            if not relative_parts:
                continue
            source_path, _note = ensure_archive_preview_source(related_entry)
            target_path = files_root.joinpath(*relative_parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _safe_log(on_log, f"Copying related file: files/{target_path.relative_to(files_root).as_posix()}")
            shutil.copy2(source_path, target_path)
            written_files.append(target_path)
            written_virtual_paths.add(normalized_related_path.lower())
            if related_entry.extension in {".xml", ".pami", ".json"}:
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
    metadata_files = write_mesh_loose_mod_package_metadata(
        package_root,
        package_info,
        assets=asset_rows,
        files=file_rows,
        include_paired_lod=bool(paired_lod_path),
        create_no_encrypt_file=create_no_encrypt_file,
    )

    return ArchiveLooseExportResult(
        package_root=package_root,
        written_files=[*written_files, *metadata_files],
    )


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
) -> Optional[ArchiveEntry]:
    skeleton_path = f"{Path(entry.path).with_suffix('.pab').as_posix()}"
    normalized = _normalize_virtual_path(skeleton_path)
    if archive_entries_by_normalized_path:
        candidates = archive_entries_by_normalized_path.get(normalized, ())
        return candidates[0] if candidates else None
    return None


def export_archive_mesh(
    entry: ArchiveEntry,
    output_dir: Path,
    export_format: str,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    related_entries: Sequence[ArchiveEntry] = (),
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
    copied_related_count = 0
    if export_kind == "obj":
        output_paths.extend(Path(path) for path in export_obj(parsed_mesh, str(output_dir), basename))
    else:
        if entry.extension == ".pac":
            skeleton_entry = _find_matching_skeleton_entry(
                entry,
                archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            )
            if skeleton_entry is not None:
                from crimson_forge_toolkit.core.archive import read_archive_entry_data

                skeleton_data, _decompressed, _note = read_archive_entry_data(skeleton_entry)
                skeleton = parse_pab(skeleton_data, skeleton_entry.path)
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

    summary_lines = [
        f"Path: {entry.path}",
        f"Format: {parsed_mesh.format.upper()}",
        f"Submeshes: {len(parsed_mesh.submeshes):,}",
        f"Vertices: {parsed_mesh.total_vertices:,}",
        f"Faces: {parsed_mesh.total_faces:,}",
    ]
    if copied_related_count:
        summary_lines.append(f"Referenced files copied: {copied_related_count:,}")
    if skeleton is not None and skeleton.bones:
        summary_lines.append(f"Skeleton bones: {len(skeleton.bones):,}")
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
        if supplemental_path.suffix.lower() not in {".xml", ".pami"}:
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
        preferred_paths: List[str] = []
        if extension == ".dds":
            preferred_paths.extend(reference_candidates_by_basename.get(resolved_source.name.lower(), ()))
        elif extension in {".xml", ".pami"}:
            related_by_extension = related_entries_by_extension.get(extension, [])
            if len(related_by_extension) == 1:
                preferred_paths.append(related_by_extension[0].path)
        target_entry, target_path = _resolve_supplemental_target_entry(
            resolved_source,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
            preferred_paths=preferred_paths,
        )
        kind = "texture" if extension == ".dds" else "sidecar" if extension in {".xml", ".pami"} else "file"
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


def build_mesh_import_preview(
    entry: ArchiveEntry,
    obj_path: Path,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texconv_path: Optional[Path] = None,
    texture_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    supplemental_files: Sequence[Path] = (),
) -> MeshImportPreviewResult:
    from crimson_forge_toolkit.core.archive import (
        _attach_model_sidecar_texture_preview_paths,
        _attach_model_texture_preview_paths,
        _extract_archive_model_sidecar_texture_references,
        build_archive_model_texture_references,
        read_archive_entry_data,
    )

    imported_mesh = import_obj(str(obj_path))
    imported_mesh.path = entry.path
    imported_mesh.format = entry.extension.lstrip(".").lower()
    original_data, _decompressed, _note = read_archive_entry_data(entry)
    rebuilt_data = build_mesh(imported_mesh, original_data)
    preview_model, parsed_mesh = build_mesh_preview_from_bytes(rebuilt_data, entry.path)

    summary_lines = [
        f"Preview rebuilt mesh for {entry.path}",
        f"Vertices: {parsed_mesh.total_vertices:,}",
        f"Faces: {parsed_mesh.total_faces:,}",
        f"Submeshes: {len(parsed_mesh.submeshes):,}",
        f"Rebuilt size: {len(rebuilt_data):,} bytes",
    ]
    resolved_supplemental_files = tuple(
        path.expanduser().resolve()
        for path in supplemental_files
        if isinstance(path, Path) and path.expanduser().resolve().is_file()
    )
    if resolved_supplemental_files:
        summary_lines.append(f"Selected supplemental files: {len(resolved_supplemental_files):,}")
    sidecar_texture_references: Tuple[object, ...] = ()
    sidecar_reference_paths: Tuple[str, ...] = ()
    sidecar_texts_by_normalized_path: Dict[str, Tuple[str, ...]] = {}
    sidecar_texts_by_basename: Dict[str, Tuple[str, ...]] = {}
    selected_sidecar_texture_references: Tuple[object, ...] = ()
    selected_sidecar_reference_paths: Tuple[str, ...] = ()
    selected_sidecar_texts_by_normalized_path: Dict[str, Tuple[str, ...]] = {}
    selected_sidecar_texts_by_basename: Dict[str, Tuple[str, ...]] = {}
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
    if texture_entries_by_basename is not None and not selected_sidecar_texture_references:
        (
            sidecar_texture_references,
            sidecar_reference_paths,
            sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename,
        ) = _extract_archive_model_sidecar_texture_references(
            entry,
            archive_entries_by_basename=(
                dict(texture_entries_by_basename) if texture_entries_by_basename is not None else None
            ),
        )
        if sidecar_texture_references:
            sidecar_suffix = f" from {', '.join(sidecar_reference_paths[:2])}" if sidecar_reference_paths else ""
            if len(sidecar_reference_paths) > 2:
                sidecar_suffix += " ..."
            summary_lines.append(
                f"Companion material sidecar data contributed {len(sidecar_texture_references):,} texture binding(s){sidecar_suffix}."
            )
            summary_lines.append(
                "Loose mesh mods may still need the matching companion .xml sidecar when custom material or texture remaps are involved."
            )
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
    if entry.extension == ".pam" and archive_entries_by_normalized_path is not None:
        paired_path = f"{Path(entry.path).with_suffix('.pamlod').as_posix()}"
        paired_candidates = archive_entries_by_normalized_path.get(_normalize_virtual_path(paired_path), ())
        if paired_candidates:
            paired_entry = paired_candidates[0]
            paired_original, _paired_decompressed, _paired_note = read_archive_entry_data(paired_entry)
            paired_mesh = transfer_pam_edit_to_pamlod_mesh(imported_mesh, original_data, paired_original, paired_entry.path)
            paired_lod_data = build_mesh(paired_mesh, paired_original)
            paired_lod_path = paired_entry.path
            summary_lines.append(f"Paired PAMLOD rebuild prepared: {paired_entry.path}")

    return MeshImportPreviewResult(
        rebuilt_data=rebuilt_data,
        parsed_mesh=parsed_mesh,
        preview_model=preview_model,
        summary_lines=summary_lines,
        texture_references=texture_references,
        supplemental_file_specs=supplemental_file_specs,
        paired_lod_data=paired_lod_data,
        paired_lod_path=paired_lod_path,
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


def _normalize_audio_input(audio_path: Path, *, sample_rate: int, channels: int) -> Path:
    if audio_path.suffix.lower() == ".wav":
        return audio_path
    temp_root = Path(tempfile.gettempdir()) / APP_NAME / "audio_patch"
    temp_root.mkdir(parents=True, exist_ok=True)
    output_path = temp_root / f"{audio_path.stem}_{sample_rate}hz_{channels}ch.wav"
    ffmpeg_path = next(iter(_ffmpeg_candidates()), None)
    if ffmpeg_path is None:
        raise ValueError("ffmpeg.exe is required to import non-WAV audio for patching.")
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
    if len(original_data) >= 28 and original_data[:4] == b"RIFF":
        try:
            channels = struct.unpack_from("<H", original_data, 22)[0]
            sample_rate = struct.unpack_from("<I", original_data, 24)[0]
        except struct.error:
            channels = 1
            sample_rate = 48000
    normalized_wav = _normalize_audio_input(normalized_path, sample_rate=sample_rate, channels=channels)
    return _build_pcm_wem(normalized_wav.read_bytes(), sample_rate, channels)


def build_pab_preview(data: bytes, virtual_path: str) -> SkeletonPreviewResult:
    skeleton = parse_pab(data, virtual_path)
    lines = [f"PAB skeleton preview for {virtual_path}"]
    detail_lines = [f"Detected {len(skeleton.bones):,} bone(s)."]
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
            f"- Bones: {len(skeleton.bones):,}",
            f"- Root bones: {len(root_bones):,}",
            f"- Named bones: {len(named_bones):,}",
            f"- Max hierarchy depth: {max_depth}",
        ]
    )
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
    lines = [f"HKX tagfile preview for {virtual_path}", ""]
    detail_lines = ["Structured Havok tagfile or binary animation metadata detected."]
    if class_names:
        detail_lines.append(f"Detected {len(class_names):,} Havok class/type marker(s).")
        lines.append("Detected classes/types:")
        lines.extend(class_names[:64])
    elif printable:
        lines.append("Readable strings:")
        lines.extend(printable[:64])
    else:
        lines.append("No readable Havok strings were recovered from the preview sample.")
    if len(printable) >= 96:
        lines.append("")
        lines.append("String scan truncated to keep the preview responsive.")
    return HkxPreviewResult(preview_text="\n".join(lines), detail_lines=detail_lines)
