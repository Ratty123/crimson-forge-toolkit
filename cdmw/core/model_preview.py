from __future__ import annotations

import math
import struct
from dataclasses import dataclass
import threading
import time
from typing import List, Optional, Sequence, Tuple

from cdmw.core.common import raise_if_cancelled
from cdmw.models import ArchiveEntry, ModelPreviewData, ModelPreviewMesh, RunCancelled

_PAM_SUBMESH_TABLE_OFFSET = 1040
_PAM_SUBMESH_STRIDE = 536
_PAM_HEADER_MESH_COUNT_OFFSET = 16
_PAM_HEADER_BBOX_MIN_OFFSET = 20
_PAM_HEADER_BBOX_MAX_OFFSET = 32
_PAM_HEADER_GEOM_OFFSET = 60
_PAM_GLOBAL_VERTEX_BASE = 3068
_PAM_GLOBAL_INDEX_OFFSET = 104512
_PAM_TEXTURE_NAME_OFFSET = 16
_PAM_MATERIAL_NAME_OFFSET = 272
_PAM_NAME_MAX_LENGTH = 256
_PAMLOD_HEADER_LOD_COUNT_OFFSET = 0
_PAMLOD_HEADER_GEOM_OFFSET = 4
_PAMLOD_HEADER_BBOX_MIN_OFFSET = 16
_PAMLOD_HEADER_BBOX_MAX_OFFSET = 28
_PAMLOD_ENTRY_TABLE_OFFSET = 80
_PAMLOD_PRIMARY_SEARCH_PADDING = 64
_PAMLOD_EXTENDED_SEARCH_PADDING = 4096
_PAM_GLOBAL_INDEX_SEARCH_SAMPLE_COUNT = 180
_PAM_GLOBAL_INDEX_SEARCH_MIN_UNIQUE = 24
_PAM_GLOBAL_INDEX_SEARCH_MAX_CANDIDATES = 12
_PAM_GLOBAL_INDEX_SEARCH_MAX_BYTES = 8 * 1024 * 1024
_PAM_GLOBAL_INDEX_SEARCH_MAX_SECONDS = 0.35
_PAM_CANDIDATE_STRIDES: Tuple[int, ...] = (
    6,
    8,
    10,
    12,
    14,
    16,
    18,
    20,
    22,
    24,
    26,
    28,
    30,
    32,
    36,
    40,
)
_PAM_GLOBAL_VERTEX_BASE_CANDIDATES: Tuple[int, ...] = (
    _PAM_GLOBAL_VERTEX_BASE,
    0,
    256,
    512,
    1024,
    1536,
    2048,
    2560,
    2816,
    3328,
    3584,
    4096,
    4608,
    5120,
    6144,
    7168,
    8192,
)
_PAMLOD_CANDIDATE_STRIDES: Tuple[int, ...] = tuple(
    sorted(_PAM_CANDIDATE_STRIDES, key=lambda candidate: (abs(candidate - 20), candidate))
)


@dataclass(slots=True)
class _RawPamEntry:
    index: int
    vertex_count: int
    index_count: int
    vertex_element_offset: int
    index_element_offset: int
    texture_name: str
    material_name: str


def build_pam_model_preview(
    entry: ArchiveEntry,
    data: bytes,
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewData:
    if len(data) < 64 or data[:4] != b"PAR ":
        raise ValueError("Invalid PAM file header.")

    bbox_min = _read_vector3(data, _PAM_HEADER_BBOX_MIN_OFFSET)
    bbox_max = _read_vector3(data, _PAM_HEADER_BBOX_MAX_OFFSET)
    geom_offset = struct.unpack_from("<i", data, _PAM_HEADER_GEOM_OFFSET)[0]
    mesh_count = struct.unpack_from("<i", data, _PAM_HEADER_MESH_COUNT_OFFSET)[0]
    if geom_offset <= 0 or geom_offset >= len(data) or mesh_count <= 0:
        raise ValueError("PAM geometry header is invalid.")

    raw_meshes = _read_pam_submeshes(data, mesh_count, stop_event=stop_event)
    if not raw_meshes:
        raise ValueError("PAM submesh table is empty.")

    candidate_mesh_sets: List[List[ModelPreviewMesh]] = []
    if _uses_combined_layout(raw_meshes):
        combined_meshes = _parse_combined_pam_meshes(
            data,
            raw_meshes,
            geom_offset,
            bbox_min,
            bbox_max,
            stop_event=stop_event,
        )
        if combined_meshes:
            try:
                combined_preview = _build_model_preview(
                    entry.path,
                    "pam",
                    combined_meshes,
                    "submesh",
                    stop_event=stop_event,
                )
                ensure_model_preview_is_reasonable(combined_preview, stop_event=stop_event)
                if _preview_matches_declared_pam_geometry(raw_meshes, combined_preview):
                    return combined_preview
            except RunCancelled:
                raise
            except Exception:
                pass
            candidate_mesh_sets.append(combined_meshes)

    candidate_mesh_sets.append(
        _parse_local_pam_meshes(data, raw_meshes, geom_offset, bbox_min, bbox_max, stop_event=stop_event)
    )
    preview = _select_best_model_preview_candidate(
        entry.path,
        "pam",
        candidate_mesh_sets,
        "submesh",
        stop_event=stop_event,
    )
    if preview is None:
        raise ValueError("Renderable model geometry could not be recovered.")
    return preview


def _preview_matches_declared_pam_geometry(
    raw_meshes: Sequence[_RawPamEntry],
    model_preview: ModelPreviewData,
) -> bool:
    expected_mesh_count = sum(1 for mesh in raw_meshes if mesh.vertex_count > 0 and mesh.index_count >= 3)
    expected_face_count = sum((mesh.index_count // 3) for mesh in raw_meshes if mesh.vertex_count > 0 and mesh.index_count >= 3)
    if expected_mesh_count > 0 and model_preview.mesh_count < expected_mesh_count:
        return False
    if expected_face_count > 0 and model_preview.face_count < max(1, int(expected_face_count * 0.95)):
        return False
    return True


def build_pamlod_model_preview(
    entry: ArchiveEntry,
    data: bytes,
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewData:
    if len(data) < _PAMLOD_ENTRY_TABLE_OFFSET:
        raise ValueError("Invalid PAMLOD file header.")

    lod_count = struct.unpack_from("<i", data, _PAMLOD_HEADER_LOD_COUNT_OFFSET)[0]
    geom_offset = struct.unpack_from("<i", data, _PAMLOD_HEADER_GEOM_OFFSET)[0]
    if lod_count <= 0 or geom_offset <= 0 or geom_offset >= len(data):
        raise ValueError("Invalid PAMLOD file header.")

    bbox_min = _read_vector3(data, _PAMLOD_HEADER_BBOX_MIN_OFFSET)
    bbox_max = _read_vector3(data, _PAMLOD_HEADER_BBOX_MAX_OFFSET)
    raw_meshes = _read_pamlod_entries(data, geom_offset, stop_event=stop_event)
    if not raw_meshes:
        raise ValueError("PAMLOD mesh table is empty.")

    groups = _group_pamlod_entries(raw_meshes, lod_count)
    lod_mesh_sets = _parse_pamlod_groups(data, groups, geom_offset, bbox_min, bbox_max, stop_event=stop_event)
    if not lod_mesh_sets:
        raise ValueError("Renderable model geometry could not be recovered.")

    preview = _build_model_preview(entry.path, "pamlod", lod_mesh_sets[0], "lod mesh", stop_event=stop_event)
    preview.lod_index = 0
    preview.lod_count = len(lod_mesh_sets)
    preview.summary = _build_lod_summary(
        entry.path,
        displayed_lod_index=preview.lod_index,
        recovered_lod_count=preview.lod_count,
        vertex_count=preview.vertex_count,
        face_count=preview.face_count,
    )
    return preview


def _read_pam_submeshes(
    data: bytes,
    mesh_count: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[_RawPamEntry]:
    meshes: List[_RawPamEntry] = []
    for index in range(mesh_count):
        if (index % 32) == 0:
            raise_if_cancelled(stop_event)
        entry_offset = _PAM_SUBMESH_TABLE_OFFSET + index * _PAM_SUBMESH_STRIDE
        if entry_offset + _PAM_SUBMESH_STRIDE > len(data):
            break
        meshes.append(
            _RawPamEntry(
                index=index,
                vertex_count=struct.unpack_from("<I", data, entry_offset)[0],
                index_count=struct.unpack_from("<I", data, entry_offset + 4)[0],
                vertex_element_offset=struct.unpack_from("<I", data, entry_offset + 8)[0],
                index_element_offset=struct.unpack_from("<I", data, entry_offset + 12)[0],
                texture_name=_read_c_string(data, entry_offset + _PAM_TEXTURE_NAME_OFFSET, _PAM_NAME_MAX_LENGTH),
                material_name=_read_c_string(data, entry_offset + _PAM_MATERIAL_NAME_OFFSET, _PAM_NAME_MAX_LENGTH),
            )
        )
    return meshes


def _uses_combined_layout(meshes: Sequence[_RawPamEntry]) -> bool:
    if len(meshes) <= 1:
        return False
    expected_vertex_offset = 0
    expected_index_offset = 0
    for mesh in meshes:
        if mesh.vertex_element_offset != expected_vertex_offset or mesh.index_element_offset != expected_index_offset:
            return False
        expected_vertex_offset += mesh.vertex_count
        expected_index_offset += mesh.index_count
    return True


def _parse_local_pam_meshes(
    data: bytes,
    raw_meshes: Sequence[_RawPamEntry],
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[ModelPreviewMesh]:
    meshes: List[ModelPreviewMesh] = []
    max_global_index_count = max(0, (len(data) - _PAM_GLOBAL_INDEX_OFFSET) // 2)
    for raw_mesh in raw_meshes:
        raise_if_cancelled(stop_event)
        if raw_mesh.vertex_count <= 0 or raw_mesh.index_count < 3:
            continue
        local_layout = _try_find_local_layout(
            data,
            geom_offset,
            raw_mesh.vertex_element_offset,
            raw_mesh.vertex_count,
            raw_mesh.index_count,
            stop_event=stop_event,
        )
        if local_layout is not None:
            stride, index_offset = local_layout
            mesh = _parse_quantized_mesh(
                data,
                raw_mesh,
                geom_offset + raw_mesh.vertex_element_offset,
                index_offset,
                stride,
                bbox_min,
                bbox_max,
                stop_event=stop_event,
            )
        elif raw_mesh.index_element_offset + raw_mesh.index_count <= max_global_index_count:
            mesh = _parse_best_global_mesh(data, raw_mesh, geom_offset, bbox_min, bbox_max, stop_event=stop_event)
        else:
            continue
        if mesh.positions and mesh.indices:
            meshes.append(mesh)
    return meshes


def _parse_combined_pam_meshes(
    data: bytes,
    raw_meshes: Sequence[_RawPamEntry],
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[ModelPreviewMesh]:
    total_vertices = sum(mesh.vertex_count for mesh in raw_meshes)
    total_indices = sum(mesh.index_count for mesh in raw_meshes)
    if total_vertices <= 0 or total_indices <= 0:
        return []

    layout = _find_combined_pam_layout(data, raw_meshes, geom_offset, stop_event=stop_event)
    if layout is None:
        return []
    stride, index_block_offset = layout
    meshes: List[ModelPreviewMesh] = []
    for raw_mesh in raw_meshes:
        raise_if_cancelled(stop_event)
        vertex_base = geom_offset + raw_mesh.vertex_element_offset * stride
        index_offset = index_block_offset + raw_mesh.index_element_offset * 2
        mesh = _parse_quantized_mesh(
            data,
            raw_mesh,
            vertex_base,
            index_offset,
            stride,
            bbox_min,
            bbox_max,
            stop_event=stop_event,
        )
        if mesh.positions and mesh.indices:
            meshes.append(mesh)
    return meshes


def _find_combined_pam_layout(
    data: bytes,
    raw_meshes: Sequence[_RawPamEntry],
    geom_offset: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[Tuple[int, int]]:
    total_vertices = sum(mesh.vertex_count for mesh in raw_meshes)
    total_indices = sum(mesh.index_count for mesh in raw_meshes)
    remaining_bytes = len(data) - geom_offset
    target_stride = ((remaining_bytes - (total_indices * 2)) / total_vertices) if total_vertices else 0.0
    candidates = sorted(_PAM_CANDIDATE_STRIDES, key=lambda candidate: abs(candidate - target_stride))
    for stride in candidates:
        raise_if_cancelled(stop_event)
        index_block_offset = geom_offset + total_vertices * stride
        if index_block_offset + total_indices * 2 > len(data):
            continue
        if all(
            _indices_fit_vertex_count(
                data,
                index_block_offset + raw_mesh.index_element_offset * 2,
                raw_mesh.index_count,
                raw_mesh.vertex_count,
                stop_event=stop_event,
            )
            for raw_mesh in raw_meshes
        ):
            return stride, index_block_offset
    return None


def _read_pamlod_entries(
    data: bytes,
    geom_offset: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[_RawPamEntry]:
    meshes: List[_RawPamEntry] = []
    search_limit = max(_PAMLOD_ENTRY_TABLE_OFFSET, geom_offset - 5)
    for offset in range(_PAMLOD_ENTRY_TABLE_OFFSET, search_limit):
        if (offset % 256) == 0:
            raise_if_cancelled(stop_event)
        if not _looks_like_dds_string(data, offset):
            continue
        entry_offset = offset - 16
        if entry_offset < _PAMLOD_ENTRY_TABLE_OFFSET:
            continue
        vertex_count = struct.unpack_from("<I", data, entry_offset)[0]
        index_count = struct.unpack_from("<I", data, entry_offset + 4)[0]
        if vertex_count == 0 or vertex_count > 131072 or index_count == 0 or index_count % 3 != 0:
            continue
        meshes.append(
            _RawPamEntry(
                index=len(meshes),
                vertex_count=vertex_count,
                index_count=index_count,
                vertex_element_offset=struct.unpack_from("<I", data, offset - 8)[0],
                index_element_offset=struct.unpack_from("<I", data, offset - 4)[0],
                texture_name=_read_c_string(data, offset, _PAM_NAME_MAX_LENGTH),
                material_name=_read_c_string(data, offset + _PAM_NAME_MAX_LENGTH, _PAM_NAME_MAX_LENGTH),
            )
        )
    return meshes


def _group_pamlod_entries(entries: Sequence[_RawPamEntry], lod_count: int) -> List[List[_RawPamEntry]]:
    groups: List[List[_RawPamEntry]] = []
    current_group: List[_RawPamEntry] = []
    expected_vertex_offset = 0
    expected_index_offset = 0
    for entry in entries:
        if (
            current_group
            and (
                entry.vertex_element_offset != expected_vertex_offset
                or entry.index_element_offset != expected_index_offset
            )
        ):
            groups.append(current_group)
            current_group = []
        current_group.append(entry)
        expected_vertex_offset = entry.vertex_element_offset + entry.vertex_count
        expected_index_offset = entry.index_element_offset + entry.index_count
    if current_group:
        groups.append(current_group)
    return groups[:lod_count]


def _parse_pamlod_groups(
    data: bytes,
    groups: Sequence[Sequence[_RawPamEntry]],
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[List[ModelPreviewMesh]]:
    lod_mesh_sets: List[List[ModelPreviewMesh]] = []
    cursor = geom_offset
    for group in groups:
        raise_if_cancelled(stop_event)
        total_vertices = sum(mesh.vertex_count for mesh in group)
        total_indices = sum(mesh.index_count for mesh in group)
        if total_vertices <= 0 or total_indices <= 0:
            continue

        layout = _find_pamlod_group_layout(data, cursor, group, stop_event=stop_event)
        if layout is None:
            continue
        vertex_base, stride, index_offset = layout

        lod_meshes: List[ModelPreviewMesh] = []
        for raw_mesh in group:
            parsed_mesh = _parse_quantized_mesh(
                data,
                raw_mesh,
                vertex_base + raw_mesh.vertex_element_offset * stride,
                index_offset + raw_mesh.index_element_offset * 2,
                stride,
                bbox_min,
                bbox_max,
                stop_event=stop_event,
            )
            if parsed_mesh.positions and parsed_mesh.indices:
                lod_meshes.append(parsed_mesh)
        if lod_meshes:
            lod_mesh_sets.append(lod_meshes)
        cursor = index_offset + total_indices * 2
    return lod_mesh_sets


def _find_pamlod_group_layout(
    data: bytes,
    cursor: int,
    group: Sequence[_RawPamEntry],
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[Tuple[int, int, int]]:
    total_vertices = sum(mesh.vertex_count for mesh in group)
    total_indices = sum(mesh.index_count for mesh in group)
    for padding in _iter_pamlod_padding_candidates():
        if padding >= _PAMLOD_EXTENDED_SEARCH_PADDING:
            break
        if (padding % 256) == 0:
            raise_if_cancelled(stop_event)
        candidate_vertex_base = cursor + padding
        for candidate_stride in _PAMLOD_CANDIDATE_STRIDES:
            candidate_index_offset = candidate_vertex_base + total_vertices * candidate_stride
            if candidate_index_offset + total_indices * 2 > len(data):
                continue
            if _pamlod_group_indices_fit_entries(
                data,
                group,
                candidate_index_offset,
                stop_event=stop_event,
            ):
                return candidate_vertex_base, candidate_stride, candidate_index_offset
    return None


def _iter_pamlod_padding_candidates() -> Sequence[int]:
    candidates: List[int] = []
    candidates.extend(range(0, _PAMLOD_PRIMARY_SEARCH_PADDING, 2))
    candidates.extend(range(_PAMLOD_PRIMARY_SEARCH_PADDING, min(_PAMLOD_EXTENDED_SEARCH_PADDING, 512), 4))
    candidates.extend(range(512, _PAMLOD_EXTENDED_SEARCH_PADDING, 8))
    return tuple(dict.fromkeys(candidates))


def _pamlod_group_indices_fit_entries(
    data: bytes,
    group: Sequence[_RawPamEntry],
    index_block_offset: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    for raw_mesh in group:
        if not _indices_fit_vertex_count(
            data,
            index_block_offset + raw_mesh.index_element_offset * 2,
            raw_mesh.index_count,
            raw_mesh.vertex_count,
            stop_event=stop_event,
        ):
            return False
    return True


def _try_find_local_layout(
    data: bytes,
    geom_offset: int,
    vertex_offset_bytes: int,
    vertex_count: int,
    index_count: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[Tuple[int, int]]:
    vertex_base = geom_offset + vertex_offset_bytes
    if vertex_base < 0 or vertex_base >= len(data):
        return None
    for stride in _PAM_CANDIDATE_STRIDES:
        raise_if_cancelled(stop_event)
        index_offset = vertex_base + vertex_count * stride
        if index_offset + index_count * 2 > len(data):
            continue
        if _indices_fit_vertex_count(data, index_offset, index_count, vertex_count, stop_event=stop_event):
            return stride, index_offset
    return None


def _indices_fit_vertex_count(
    data: bytes,
    index_offset: int,
    index_count: int,
    vertex_count: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    for index in range(index_count):
        if (index % 256) == 0:
            raise_if_cancelled(stop_event)
        if struct.unpack_from("<H", data, index_offset + index * 2)[0] >= vertex_count:
            return False
    return True


def _parse_global_mesh(
    data: bytes,
    raw_mesh: _RawPamEntry,
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewMesh:
    index_offset = _PAM_GLOBAL_INDEX_OFFSET + raw_mesh.index_element_offset * 2
    return _parse_global_mesh_at(
        data,
        raw_mesh,
        geom_offset,
        bbox_min,
        bbox_max,
        index_offset,
        _PAM_GLOBAL_VERTEX_BASE,
        stop_event=stop_event,
    )


def _parse_best_global_mesh(
    data: bytes,
    raw_mesh: _RawPamEntry,
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewMesh:
    best_mesh = _parse_global_mesh(data, raw_mesh, geom_offset, bbox_min, bbox_max, stop_event=stop_event)
    best_score = _score_global_mesh_candidate(best_mesh, raw_mesh, stop_event=stop_event)
    if not _global_mesh_search_needed(best_mesh, raw_mesh):
        return best_mesh
    if _global_mesh_search_looks_hopeless(best_mesh, raw_mesh, stop_event=stop_event):
        return best_mesh

    search_started_at = time.perf_counter()
    for candidate_index_offset in _find_global_index_offset_candidates(
        data,
        geom_offset,
        raw_mesh,
        stop_event=stop_event,
        started_at=search_started_at,
        max_seconds=_PAM_GLOBAL_INDEX_SEARCH_MAX_SECONDS,
    ):
        if time.perf_counter() - search_started_at > _PAM_GLOBAL_INDEX_SEARCH_MAX_SECONDS:
            break
        for global_vertex_base in _PAM_GLOBAL_VERTEX_BASE_CANDIDATES:
            raise_if_cancelled(stop_event)
            if time.perf_counter() - search_started_at > _PAM_GLOBAL_INDEX_SEARCH_MAX_SECONDS:
                break
            candidate_mesh = _parse_global_mesh_at(
                data,
                raw_mesh,
                geom_offset,
                bbox_min,
                bbox_max,
                candidate_index_offset,
                global_vertex_base,
                stop_event=stop_event,
            )
            candidate_score = _score_global_mesh_candidate(candidate_mesh, raw_mesh, stop_event=stop_event)
            if candidate_score > best_score:
                best_mesh = candidate_mesh
                best_score = candidate_score
    return best_mesh


def _parse_global_mesh_at(
    data: bytes,
    raw_mesh: _RawPamEntry,
    geom_offset: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    index_offset: int,
    global_vertex_base: int,
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewMesh:
    mesh = ModelPreviewMesh(material_name=raw_mesh.material_name, texture_name=raw_mesh.texture_name)
    if index_offset < 0 or index_offset + raw_mesh.index_count * 2 > len(data):
        return mesh

    source_indices = [
        struct.unpack_from("<H", data, index_offset + index * 2)[0] for index in range(raw_mesh.index_count)
    ]
    source_to_local: dict[int, int] = {}
    for source_index_index, source_index in enumerate(sorted(set(source_indices))):
        if (source_index_index % 256) == 0:
            raise_if_cancelled(stop_event)
        vertex_index = source_index - global_vertex_base
        if vertex_index < 0:
            continue
        vertex_offset = geom_offset + vertex_index * 6
        if vertex_offset + 6 > len(data):
            continue
        x = _dequantize_int16(struct.unpack_from("<h", data, vertex_offset)[0], bbox_min[0], bbox_max[0])
        y = _dequantize_int16(struct.unpack_from("<h", data, vertex_offset + 2)[0], bbox_min[1], bbox_max[1])
        z = _dequantize_int16(struct.unpack_from("<h", data, vertex_offset + 4)[0], bbox_min[2], bbox_max[2])
        source_to_local[source_index] = len(mesh.positions)
        mesh.positions.append((x, y, z))

    for index in range(0, len(source_indices) - 2, 3):
        if (index % 768) == 0:
            raise_if_cancelled(stop_event)
        a = source_to_local.get(source_indices[index])
        b = source_to_local.get(source_indices[index + 1])
        c = source_to_local.get(source_indices[index + 2])
        if a is None or b is None or c is None:
            continue
        mesh.indices.extend((a, b, c))
    return mesh


def _global_mesh_search_needed(mesh: ModelPreviewMesh, raw_mesh: _RawPamEntry) -> bool:
    if len(mesh.positions) < 3 or len(mesh.indices) < 3:
        return True
    face_count = len(mesh.indices) // 3
    declared_face_count = max(1, raw_mesh.index_count // 3)
    referenced_vertices = len({index for index in mesh.indices if 0 <= index < len(mesh.positions)})
    reference_ratio = referenced_vertices / max(1, len(mesh.positions))
    recovered_face_ratio = face_count / declared_face_count
    return recovered_face_ratio < 0.4 or reference_ratio < 0.6


def _global_mesh_search_looks_hopeless(
    mesh: ModelPreviewMesh,
    raw_mesh: _RawPamEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    if len(mesh.positions) < 3 or len(mesh.indices) < 3:
        return True
    face_count = len(mesh.indices) // 3
    declared_face_count = max(1, raw_mesh.index_count // 3)
    referenced_vertices = len({index for index in mesh.indices if 0 <= index < len(mesh.positions)})
    reference_ratio = referenced_vertices / max(1, len(mesh.positions))
    recovered_face_ratio = face_count / declared_face_count
    median_edge_length, edge_length_p90 = _mesh_edge_length_percentiles(
        mesh.positions,
        mesh.indices,
        stop_event=stop_event,
    )
    return (
        recovered_face_ratio < 0.2
        and reference_ratio < 0.4
        and median_edge_length > 0.6
        and edge_length_p90 > 1.5
    )


def _find_global_index_offset_candidates(
    data: bytes,
    geom_offset: int,
    raw_mesh: _RawPamEntry,
    *,
    stop_event: Optional[threading.Event] = None,
    started_at: Optional[float] = None,
    max_seconds: float = _PAM_GLOBAL_INDEX_SEARCH_MAX_SECONDS,
) -> List[int]:
    if raw_mesh.index_count < 120 or raw_mesh.vertex_count < 256:
        return []
    sample_index_count = min(raw_mesh.index_count, _PAM_GLOBAL_INDEX_SEARCH_SAMPLE_COUNT)
    if sample_index_count < 24:
        return []
    min_unique = min(
        raw_mesh.vertex_count,
        max(12, sample_index_count // 6),
        _PAM_GLOBAL_INDEX_SEARCH_MIN_UNIQUE,
    )
    search_start = max(_PAM_GLOBAL_INDEX_OFFSET, geom_offset)
    search_stop = len(data) - sample_index_count * 2
    if search_stop <= search_start:
        return []
    if search_stop - search_start > _PAM_GLOBAL_INDEX_SEARCH_MAX_BYTES:
        search_stop = search_start + _PAM_GLOBAL_INDEX_SEARCH_MAX_BYTES
    max_index_value = raw_mesh.vertex_count + max(_PAM_GLOBAL_VERTEX_BASE_CANDIDATES)
    candidates: List[int] = []
    started = float(started_at if started_at is not None else time.perf_counter())
    for index_offset in range(search_start, search_stop + 1, 2):
        if (index_offset % 512) == 0:
            raise_if_cancelled(stop_event)
            if time.perf_counter() - started > max_seconds:
                break
        sampled_indices: List[int] = []
        valid = True
        for sample_index in range(0, sample_index_count, 3):
            value = struct.unpack_from("<H", data, index_offset + sample_index * 2)[0]
            if value > max_index_value:
                valid = False
                break
            sampled_indices.append(value)
        if not valid or len(set(sampled_indices)) < min_unique:
            continue
        candidates.append(index_offset)
        if len(candidates) >= _PAM_GLOBAL_INDEX_SEARCH_MAX_CANDIDATES:
            break
    return candidates


def _score_global_mesh_candidate(
    mesh: ModelPreviewMesh,
    raw_mesh: _RawPamEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> float:
    if len(mesh.positions) < 3 or len(mesh.indices) < 3:
        return float("-inf")
    face_count = len(mesh.indices) // 3
    if face_count <= 0:
        return float("-inf")
    non_degenerate_faces = _count_non_degenerate_triangles(mesh.positions, mesh.indices, stop_event=stop_event)
    if non_degenerate_faces <= 0:
        return float("-inf")
    referenced_vertices = len({index for index in mesh.indices if 0 <= index < len(mesh.positions)})
    dimension = _mesh_bounds_dimension(mesh.positions)
    if dimension <= 1e-9:
        return float("-inf")
    median_edge_length, edge_length_p90 = _mesh_edge_length_percentiles(
        mesh.positions,
        mesh.indices,
        stop_event=stop_event,
    )
    normalization_scale = 2.0 / dimension
    median_edge_length *= normalization_scale
    edge_length_p90 *= normalization_scale
    recovered_face_ratio = face_count / max(1, raw_mesh.index_count // 3)
    reference_ratio = referenced_vertices / max(1, len(mesh.positions))
    non_degenerate_ratio = non_degenerate_faces / face_count
    return (
        recovered_face_ratio * 4.0
        + reference_ratio * 3.0
        + non_degenerate_ratio * 2.0
        - edge_length_p90 * 1.25
        - median_edge_length * 0.5
    )


def _parse_quantized_mesh(
    data: bytes,
    raw_mesh: _RawPamEntry,
    vertex_base: int,
    index_offset: int,
    stride: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewMesh:
    mesh = ModelPreviewMesh(material_name=raw_mesh.material_name, texture_name=raw_mesh.texture_name)
    if vertex_base < 0 or index_offset < 0:
        return mesh
    if index_offset + raw_mesh.index_count * 2 > len(data):
        return mesh

    source_indices = [
        struct.unpack_from("<H", data, index_offset + index * 2)[0] for index in range(raw_mesh.index_count)
    ]
    source_to_local: dict[int, int] = {}
    for source_index_index, source_index in enumerate(sorted(set(source_indices))):
        if (source_index_index % 256) == 0:
            raise_if_cancelled(stop_event)
        vertex_offset = vertex_base + source_index * stride
        if vertex_offset + 6 > len(data):
            continue
        x = _dequantize_uint16(struct.unpack_from("<H", data, vertex_offset)[0], bbox_min[0], bbox_max[0])
        y = _dequantize_uint16(struct.unpack_from("<H", data, vertex_offset + 2)[0], bbox_min[1], bbox_max[1])
        z = _dequantize_uint16(struct.unpack_from("<H", data, vertex_offset + 4)[0], bbox_min[2], bbox_max[2])
        source_to_local[source_index] = len(mesh.positions)
        mesh.positions.append((x, y, z))
        if stride >= 12 and vertex_offset + 12 <= len(data):
            mesh.texture_coordinates.append(
                (
                    _unpack_half(data, vertex_offset + 8),
                    _unpack_half(data, vertex_offset + 10),
                )
            )

    for index in range(0, len(source_indices) - 2, 3):
        if (index % 768) == 0:
            raise_if_cancelled(stop_event)
        a = source_to_local.get(source_indices[index])
        b = source_to_local.get(source_indices[index + 1])
        c = source_to_local.get(source_indices[index + 2])
        if a is None or b is None or c is None:
            continue
        mesh.indices.extend((a, b, c))
    return mesh


def _model_preview_mesh_overlap_metrics(
    model_preview: ModelPreviewData,
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    if model_preview.mesh_count <= 1:
        return (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)

    mesh_mins: List[Tuple[float, float, float]] = []
    mesh_maxs: List[Tuple[float, float, float]] = []
    for mesh in model_preview.meshes:
        if not mesh.positions:
            continue
        xs = [position[0] for position in mesh.positions]
        ys = [position[1] for position in mesh.positions]
        zs = [position[2] for position in mesh.positions]
        mesh_mins.append((min(xs), min(ys), min(zs)))
        mesh_maxs.append((max(xs), max(ys), max(zs)))

    if len(mesh_mins) <= 1:
        return (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)

    global_min = tuple(min(bounds[axis] for bounds in mesh_mins) for axis in range(3))
    global_max = tuple(max(bounds[axis] for bounds in mesh_maxs) for axis in range(3))
    global_size = tuple(max(global_max[axis] - global_min[axis], 1e-9) for axis in range(3))

    span_ratios_by_axis: List[List[float]] = [[], [], []]
    center_ratios_by_axis: List[List[float]] = [[], [], []]
    for mesh_min, mesh_max in zip(mesh_mins, mesh_maxs):
        for axis in range(3):
            span_ratios_by_axis[axis].append((mesh_max[axis] - mesh_min[axis]) / global_size[axis])
            center_ratios_by_axis[axis].append(
                (((mesh_min[axis] + mesh_max[axis]) * 0.5) - global_min[axis]) / global_size[axis]
            )

    median_spans = []
    center_ranges = []
    for axis in range(3):
        span_values = sorted(span_ratios_by_axis[axis])
        center_values = center_ratios_by_axis[axis]
        median_spans.append(span_values[len(span_values) // 2])
        center_ranges.append(max(center_values) - min(center_values))
    return tuple(center_ranges), tuple(median_spans)


def ensure_model_preview_is_reasonable(
    model_preview: ModelPreviewData,
    *,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if model_preview.mesh_count <= 0 or model_preview.vertex_count < 3 or model_preview.face_count <= 0:
        raise ValueError("Recovered geometry is empty.")
    if model_preview.vertex_count < model_preview.mesh_count * 3:
        raise ValueError("Recovered geometry is too sparse to render reliably.")
    if model_preview.face_count > model_preview.vertex_count * 64:
        raise ValueError("Recovered geometry is implausibly dense for the recovered vertex count.")
    non_degenerate_faces = sum(
        _count_non_degenerate_triangles(mesh.positions, mesh.indices, stop_event=stop_event) for mesh in model_preview.meshes
    )
    if non_degenerate_faces <= 0:
        raise ValueError("Recovered geometry only contained degenerate triangles.")
    non_degenerate_ratio = non_degenerate_faces / max(1, model_preview.face_count)
    median_edge_length, edge_length_p90 = _model_preview_edge_length_percentiles(model_preview, stop_event=stop_event)
    reference_ratio = _model_preview_reference_ratio(model_preview)
    mesh_center_ranges, mesh_median_spans = _model_preview_mesh_overlap_metrics(model_preview)
    if (
        model_preview.mesh_count == 1
        and
        model_preview.face_count >= 1000
        and reference_ratio < 0.5
        and median_edge_length > 0.25
        and edge_length_p90 > 0.7
    ):
        raise ValueError("Recovered geometry leaves too many vertices unreferenced and was suppressed.")
    if (
        model_preview.format == "pam"
        and median_edge_length > 0.35
        and edge_length_p90 > 0.95
        and (reference_ratio < 0.6 or non_degenerate_ratio < 0.95)
    ):
        raise ValueError("Recovered geometry appears scrambled and was suppressed.")
    if (
        model_preview.format == "pam"
        and model_preview.mesh_count == 1
        and model_preview.face_count >= 12000
        and median_edge_length > 0.45
        and edge_length_p90 > 0.95
    ):
        raise ValueError("Recovered geometry appears scrambled and was suppressed.")
    if (
        model_preview.format == "pam"
        and model_preview.mesh_count >= 8
        and model_preview.face_count >= 20000
        and min(mesh_median_spans) > 0.95
        and max(mesh_center_ranges) < 0.02
        and edge_length_p90 > 1.0
    ):
        raise ValueError("Recovered geometry collapses many PAM submeshes onto the same bounds and was suppressed.")
    if model_preview.face_count >= 500 and median_edge_length > 0.5 and edge_length_p90 > 1.0:
        raise ValueError("Recovered geometry appears scrambled and was suppressed.")


def _build_model_preview(
    path: str,
    file_format: str,
    meshes: Sequence[ModelPreviewMesh],
    label: str,
    *,
    stop_event: Optional[threading.Event] = None,
) -> ModelPreviewData:
    filtered_meshes = [mesh for mesh in meshes if _mesh_has_renderable_geometry(mesh)]
    if not filtered_meshes:
        raise ValueError("Renderable model geometry could not be recovered.")

    raise_if_cancelled(stop_event)
    normalization_center, normalization_scale = _normalize_model_meshes(filtered_meshes)
    for mesh in filtered_meshes:
        raise_if_cancelled(stop_event)
        mesh.normals = _build_vertex_normals(mesh.positions, mesh.indices, stop_event=stop_event)

    vertex_count = sum(len(mesh.positions) for mesh in filtered_meshes)
    face_count = sum(len(mesh.indices) // 3 for mesh in filtered_meshes)
    return ModelPreviewData(
        path=path,
        format=file_format,
        summary=_build_submesh_summary(path, filtered_meshes, label),
        mesh_count=len(filtered_meshes),
        vertex_count=vertex_count,
        face_count=face_count,
        normalization_center=normalization_center,
        normalization_scale=normalization_scale,
        meshes=filtered_meshes,
    )


def _build_lod_summary(
    path: str,
    *,
    displayed_lod_index: int,
    recovered_lod_count: int,
    vertex_count: int,
    face_count: int,
) -> str:
    lod_label = f"LOD {displayed_lod_index + 1}" if displayed_lod_index >= 0 else "LOD"
    if recovered_lod_count > 0 and displayed_lod_index >= 0:
        lod_label = f"{lod_label} of {recovered_lod_count}"
    return f"{path}\n{lod_label}\n{vertex_count:,} vertices\n{face_count:,} faces"


def _select_best_model_preview_candidate(
    path: str,
    file_format: str,
    mesh_sets: Sequence[Sequence[ModelPreviewMesh]],
    label: str,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[ModelPreviewData]:
    candidates: List[Tuple[bool, float, int, int, ModelPreviewData]] = []
    for meshes in mesh_sets:
        raise_if_cancelled(stop_event)
        if not meshes:
            continue
        try:
            preview = _build_model_preview(path, file_format, meshes, label, stop_event=stop_event)
        except Exception:
            continue
        try:
            ensure_model_preview_is_reasonable(preview, stop_event=stop_event)
            reasonable = True
        except Exception:
            reasonable = False
        candidates.append(
            (
                reasonable,
                _score_model_preview_selection(preview, stop_event=stop_event),
                preview.face_count,
                preview.vertex_count,
                preview,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    return candidates[0][4]


def _score_model_preview_selection(
    model_preview: ModelPreviewData,
    *,
    stop_event: Optional[threading.Event] = None,
) -> float:
    reference_ratio = _model_preview_reference_ratio(model_preview)
    non_degenerate_faces = sum(
        _count_non_degenerate_triangles(mesh.positions, mesh.indices, stop_event=stop_event)
        for mesh in model_preview.meshes
    )
    non_degenerate_ratio = non_degenerate_faces / max(1, model_preview.face_count)
    median_edge_length, edge_length_p90 = _model_preview_edge_length_percentiles(model_preview, stop_event=stop_event)
    face_density = model_preview.face_count / max(1, model_preview.vertex_count)
    return (
        reference_ratio * 3.0
        + non_degenerate_ratio * 2.0
        + min(face_density, 2.0)
        - edge_length_p90 * 1.25
        - median_edge_length * 0.5
    )


def _model_preview_reference_ratio(model_preview: ModelPreviewData) -> float:
    referenced_vertices = sum(
        len({index for index in mesh.indices if 0 <= index < len(mesh.positions)})
        for mesh in model_preview.meshes
    )
    return referenced_vertices / max(1, model_preview.vertex_count)


def _mesh_has_renderable_geometry(mesh: ModelPreviewMesh) -> bool:
    if len(mesh.positions) < 3 or len(mesh.indices) < 3:
        return False
    referenced_vertices = {index for index in mesh.indices if 0 <= index < len(mesh.positions)}
    if len(referenced_vertices) < 3:
        return False
    if _mesh_bounds_dimension(mesh.positions) <= 1e-9:
        return False
    return _count_non_degenerate_triangles(mesh.positions, mesh.indices) > 0


def _mesh_bounds_dimension(positions: Sequence[Tuple[float, float, float]]) -> float:
    min_x = min(position[0] for position in positions)
    min_y = min(position[1] for position in positions)
    min_z = min(position[2] for position in positions)
    max_x = max(position[0] for position in positions)
    max_y = max(position[1] for position in positions)
    max_z = max(position[2] for position in positions)
    return max(max_x - min_x, max_y - min_y, max_z - min_z)


def _count_non_degenerate_triangles(
    positions: Sequence[Tuple[float, float, float]],
    indices: Sequence[int],
    *,
    stop_event: Optional[threading.Event] = None,
) -> int:
    count = 0
    for index in range(0, len(indices) - 2, 3):
        if (index % 768) == 0:
            raise_if_cancelled(stop_event)
        a_index = indices[index]
        b_index = indices[index + 1]
        c_index = indices[index + 2]
        if (
            a_index < 0
            or b_index < 0
            or c_index < 0
            or a_index >= len(positions)
            or b_index >= len(positions)
            or c_index >= len(positions)
        ):
            continue
        ax, ay, az = positions[a_index]
        bx, by, bz = positions[b_index]
        cx, cy, cz = positions[c_index]
        ab = (bx - ax, by - ay, bz - az)
        ac = (cx - ax, cy - ay, cz - az)
        nx = ab[1] * ac[2] - ab[2] * ac[1]
        ny = ab[2] * ac[0] - ab[0] * ac[2]
        nz = ab[0] * ac[1] - ab[1] * ac[0]
        if (nx * nx + ny * ny + nz * nz) > 1e-18:
            count += 1
    return count


def _normalize_model_meshes(meshes: Sequence[ModelPreviewMesh]) -> Tuple[Tuple[float, float, float], float]:
    all_positions = [position for mesh in meshes for position in mesh.positions]
    if not all_positions:
        return (0.0, 0.0, 0.0), 1.0
    min_x = min(position[0] for position in all_positions)
    min_y = min(position[1] for position in all_positions)
    min_z = min(position[2] for position in all_positions)
    max_x = max(position[0] for position in all_positions)
    max_y = max(position[1] for position in all_positions)
    max_z = max(position[2] for position in all_positions)
    center_x = (min_x + max_x) * 0.5
    center_y = (min_y + max_y) * 0.5
    center_z = (min_z + max_z) * 0.5
    max_dimension = max(max_x - min_x, max_y - min_y, max_z - min_z)
    if max_dimension <= 1e-6:
        return (center_x, center_y, center_z), 1.0
    scale = 2.0 / max_dimension
    for mesh in meshes:
        mesh.positions = [
            (
                (position[0] - center_x) * scale,
                (position[1] - center_y) * scale,
                (position[2] - center_z) * scale,
            )
            for position in mesh.positions
        ]
    return (center_x, center_y, center_z), scale


def _build_vertex_normals(
    positions: Sequence[Tuple[float, float, float]],
    indices: Sequence[int],
    *,
    stop_event: Optional[threading.Event] = None,
) -> List[Tuple[float, float, float]]:
    normals = [[0.0, 0.0, 0.0] for _ in range(len(positions))]
    for index in range(0, len(indices) - 2, 3):
        if (index % 768) == 0:
            raise_if_cancelled(stop_event)
        a_index = indices[index]
        b_index = indices[index + 1]
        c_index = indices[index + 2]
        if (
            a_index < 0
            or b_index < 0
            or c_index < 0
            or a_index >= len(positions)
            or b_index >= len(positions)
            or c_index >= len(positions)
        ):
            continue
        ax, ay, az = positions[a_index]
        bx, by, bz = positions[b_index]
        cx, cy, cz = positions[c_index]
        ab = (bx - ax, by - ay, bz - az)
        ac = (cx - ax, cy - ay, cz - az)
        nx = ab[1] * ac[2] - ab[2] * ac[1]
        ny = ab[2] * ac[0] - ab[0] * ac[2]
        nz = ab[0] * ac[1] - ab[1] * ac[0]
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue
        nx /= length
        ny /= length
        nz /= length
        normals[a_index][0] += nx
        normals[a_index][1] += ny
        normals[a_index][2] += nz
        normals[b_index][0] += nx
        normals[b_index][1] += ny
        normals[b_index][2] += nz
        normals[c_index][0] += nx
        normals[c_index][1] += ny
        normals[c_index][2] += nz
    return [_normalize_vector(tuple(normal)) for normal in normals]


def _normalize_vector(vector: Tuple[float, float, float]) -> Tuple[float, float, float]:
    length = math.sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])
    if length <= 1e-12:
        return (0.0, 0.0, 1.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _build_submesh_summary(path: str, meshes: Sequence[ModelPreviewMesh], label: str) -> str:
    vertex_count = sum(len(mesh.positions) for mesh in meshes)
    face_count = sum(len(mesh.indices) // 3 for mesh in meshes)
    return f"{path}\n{len(meshes):,} {label}(es)\n{vertex_count:,} vertices\n{face_count:,} faces"


def _mesh_edge_length_percentiles(
    positions: Sequence[Tuple[float, float, float]],
    indices: Sequence[int],
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[float, float]:
    edge_lengths: List[float] = []
    for index in range(0, len(indices) - 2, 3):
        if (index % 768) == 0:
            raise_if_cancelled(stop_event)
        a_index = indices[index]
        b_index = indices[index + 1]
        c_index = indices[index + 2]
        if (
            a_index < 0
            or b_index < 0
            or c_index < 0
            or a_index >= len(positions)
            or b_index >= len(positions)
            or c_index >= len(positions)
        ):
            continue
        a = positions[a_index]
        b = positions[b_index]
        c = positions[c_index]
        edge_lengths.append(math.dist(a, b))
        edge_lengths.append(math.dist(b, c))
        edge_lengths.append(math.dist(c, a))
    if not edge_lengths:
        return 0.0, 0.0
    edge_lengths.sort()
    median_index = len(edge_lengths) // 2
    p90_index = min(len(edge_lengths) - 1, int(len(edge_lengths) * 0.9))
    return edge_lengths[median_index], edge_lengths[p90_index]


def _model_preview_edge_length_percentiles(
    model_preview: ModelPreviewData,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[float, float]:
    edge_lengths: List[float] = []
    for mesh in model_preview.meshes:
        positions = mesh.positions
        indices = mesh.indices
        for index in range(0, len(indices) - 2, 3):
            if (index % 768) == 0:
                raise_if_cancelled(stop_event)
            a_index = indices[index]
            b_index = indices[index + 1]
            c_index = indices[index + 2]
            if (
                a_index < 0
                or b_index < 0
                or c_index < 0
                or a_index >= len(positions)
                or b_index >= len(positions)
                or c_index >= len(positions)
            ):
                continue
            a = positions[a_index]
            b = positions[b_index]
            c = positions[c_index]
            edge_lengths.append(math.dist(a, b))
            edge_lengths.append(math.dist(b, c))
            edge_lengths.append(math.dist(c, a))
    if not edge_lengths:
        return 0.0, 0.0
    edge_lengths.sort()
    median_index = len(edge_lengths) // 2
    p90_index = min(len(edge_lengths) - 1, int(len(edge_lengths) * 0.9))
    return edge_lengths[median_index], edge_lengths[p90_index]


def _read_vector3(data: bytes, offset: int) -> Tuple[float, float, float]:
    return struct.unpack_from("<fff", data, offset)


def _read_c_string(data: bytes, offset: int, max_length: int) -> str:
    if offset >= len(data):
        return ""
    end = offset
    limit = min(len(data), offset + max_length)
    while end < limit and data[end] != 0:
        end += 1
    if end <= offset:
        return ""
    return data[offset:end].decode("ascii", errors="ignore")


def _looks_like_dds_string(data: bytes, offset: int) -> bool:
    if offset >= len(data):
        return False
    if offset > 0 and 32 <= data[offset - 1] <= 126:
        return False
    limit = min(len(data), offset + _PAM_NAME_MAX_LENGTH)
    end = offset
    while end < limit and data[end] != 0:
        end += 1
    length = end - offset
    if length <= 4 or length > 255:
        return False
    return data[offset:end].decode("ascii", errors="ignore").lower().endswith(".dds")


def _dequantize_uint16(value: int, minimum: float, maximum: float) -> float:
    return minimum + (value / 65535.0) * (maximum - minimum)


def _dequantize_int16(value: int, minimum: float, maximum: float) -> float:
    return minimum + ((value + 32768.0) / 65536.0) * (maximum - minimum)


def _unpack_half(data: bytes, offset: int) -> float:
    return struct.unpack_from("<e", data, offset)[0]
