from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    import lz4.block as lz4_block
except ImportError:
    lz4_block = None

from crimson_texture_forge.models import ArchiveEntry
from crimson_texture_forge.core.archive import extract_archive_entry, read_archive_entry_raw_data


@dataclass
class RuntimeVertex:
    pos: Tuple[float, float, float]
    uv: Tuple[float, float]
    normal: Tuple[float, float, float]


@dataclass
class RuntimeMesh:
    name: str
    material: str
    vertices: List[RuntimeVertex] = field(default_factory=list)
    indices: List[int] = field(default_factory=list)


@dataclass
class MeshDescriptor:
    display_name: str
    material_name: str
    center: Tuple[float, float, float]
    half_extent: Tuple[float, float, float]
    vertex_counts: List[int]
    index_counts: List[int]
    bbox_unknowns: Tuple[float, float] = ()


@dataclass
class PamSubmesh:
    nv: int
    ni: int
    voff: int
    ioff: int
    texture_name: str
    material_name: str


def _safe_ascii_name(name: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip()
    return sanitized or "model"


def decompress_type1_pac(raw_data: bytes) -> bytes:
    if lz4_block is None:
        raise RuntimeError("lz4 is required for PAC type 1 decompression.")

    output = bytearray(raw_data[:0x50])
    file_offset = 0x50

    for slot in range(8):
        off = 0x10 + slot * 8
        comp = struct.unpack_from("<I", raw_data, off)[0]
        decomp = struct.unpack_from("<I", raw_data, off + 4)[0]
        if decomp == 0:
            continue
        if comp > 0:
            blob = raw_data[file_offset : file_offset + comp]
            output.extend(lz4_block.decompress(blob, uncompressed_size=decomp))
            file_offset += comp
        else:
            output.extend(raw_data[file_offset : file_offset + decomp])
            file_offset += decomp

    for slot in range(8):
        struct.pack_into("<I", output, 0x10 + slot * 8, 0)

    return bytes(output)


def parse_pac_header(data: bytes) -> Dict[str, object]:
    if data[:4] != b"PAR ":
        raise ValueError(f"Not a PAC/PAM PAR file (magic={data[:4]!r})")
    version = struct.unpack_from("<I", data, 4)[0]
    sections = []
    offset = 0x50
    for i in range(8):
        slot_off = 0x10 + i * 8
        comp_size = struct.unpack_from("<I", data, slot_off)[0]
        decomp_size = struct.unpack_from("<I", data, slot_off + 4)[0]
        stored_size = comp_size if comp_size > 0 else decomp_size
        if decomp_size > 0:
            sections.append({"index": i, "offset": offset, "size": decomp_size})
            offset += stored_size
    return {"version": version, "sections": sections}


ATTR4_PATTERN = bytes([0x04, 0x00, 0x01, 0x02, 0x03])
ATTR3_PATTERN = bytes([0x03, 0x00, 0x01, 0x02])
ATTR3_VARIANT = bytes([0x03, 0x00, 0x01, 0x01])
ATTR2_PATTERN = bytes([0x02, 0x00, 0x01])


def _find_name_strings(region: bytes, desc_start: int) -> Tuple[str, str]:
    names: List[str] = []
    cursor = desc_start
    for _ in range(2):
        found = False
        for back in range(1, 200):
            candidate_len = region[cursor - back]
            if candidate_len == 0:
                continue
            if candidate_len == back - 1:
                name_bytes = region[cursor - back + 1 : cursor]
                try:
                    name = name_bytes.decode("ascii")
                except UnicodeDecodeError:
                    continue
                if all(32 <= c < 127 for c in name_bytes):
                    names.append(name)
                    cursor = cursor - back
                    found = True
                    break
        if not found:
            names.append(f"unknown_{desc_start:x}")
    names.reverse()
    return names[0], names[1]


def find_pac_mesh_descriptors(data: bytes, sec0_offset: int, sec0_size: int) -> List[MeshDescriptor]:
    region = data[sec0_offset : sec0_offset + sec0_size]
    found: List[Tuple[int, MeshDescriptor]] = []

    pos = 0
    while True:
        idx = region.find(ATTR4_PATTERN, pos)
        if idx == -1:
            break
        desc_start = idx - 35
        if desc_start >= 0 and region[desc_start] == 0x01:
            floats = struct.unpack_from("<8f", region, desc_start + 3)
            vc = [struct.unpack_from("<H", region, desc_start + 40 + i * 2)[0] for i in range(4)]
            ic = [struct.unpack_from("<I", region, desc_start + 48 + i * 4)[0] for i in range(4)]
            names = _find_name_strings(region, desc_start)
            found.append(
                (
                    desc_start,
                    MeshDescriptor(
                        display_name=names[0],
                        material_name=names[1],
                        center=(floats[2], floats[3], floats[4]),
                        half_extent=(floats[5], floats[6], floats[7]),
                        vertex_counts=vc,
                        index_counts=ic,
                        bbox_unknowns=(floats[0], floats[1]),
                    ),
                )
            )
        pos = idx + 5

    for attr3_pattern in (ATTR3_PATTERN, ATTR3_VARIANT):
        pos = 0
        while True:
            idx = region.find(attr3_pattern, pos)
            if idx == -1:
                break
            desc_start = idx - 35
            if desc_start >= 0 and region[desc_start] == 0x01:
                if idx >= 1 and region[idx - 1] == 0x04:
                    pos = idx + 4
                    continue
                floats = struct.unpack_from("<8f", region, desc_start + 3)
                vc3 = [struct.unpack_from("<H", region, desc_start + 40 + i * 2)[0] for i in range(3)]
                ic3 = [struct.unpack_from("<I", region, desc_start + 46 + i * 4)[0] for i in range(3)]
                names = _find_name_strings(region, desc_start)
                found.append(
                    (
                        desc_start,
                        MeshDescriptor(
                            display_name=names[0],
                            material_name=names[1],
                            center=(floats[2], floats[3], floats[4]),
                            half_extent=(floats[5], floats[6], floats[7]),
                            vertex_counts=vc3 + [0],
                            index_counts=ic3 + [0],
                            bbox_unknowns=(floats[0], floats[1]),
                        ),
                    )
                )
            pos = idx + 4

    pos = 0
    while True:
        idx = region.find(ATTR2_PATTERN, pos)
        if idx == -1:
            break
        desc_start = idx - 35
        if desc_start >= 0 and region[desc_start] == 0x01:
            if idx >= 1 and region[idx - 1] in (0x03, 0x04):
                pos = idx + 3
                continue
            floats = struct.unpack_from("<8f", region, desc_start + 3)
            vc2 = [struct.unpack_from("<H", region, desc_start + 40 + i * 2)[0] for i in range(2)]
            ic2 = [struct.unpack_from("<I", region, desc_start + 44 + i * 4)[0] for i in range(2)]
            if vc2[0] > 50000 or ic2[0] > 500000:
                pos = idx + 3
                continue
            names = _find_name_strings(region, desc_start)
            found.append(
                (
                    desc_start,
                    MeshDescriptor(
                        display_name=names[0],
                        material_name=names[1],
                        center=(floats[2], floats[3], floats[4]),
                        half_extent=(floats[5], floats[6], floats[7]),
                        vertex_counts=vc2 + [0, 0],
                        index_counts=ic2 + [0, 0],
                        bbox_unknowns=(floats[0], floats[1]),
                    ),
                )
            )
        pos = idx + 3

    found.sort(key=lambda item: item[0])
    return [descriptor for _offset, descriptor in found]


def decode_pac_vertices(
    data: bytes,
    section_offset: int,
    vertex_count: int,
    descriptor: MeshDescriptor,
    *,
    vertex_start: int = 0,
) -> List[RuntimeVertex]:
    stride = 40
    cx, cy, cz = descriptor.center
    hx, hy, hz = descriptor.half_extent
    base = section_offset + vertex_start
    vertices: List[RuntimeVertex] = []

    for i in range(vertex_count):
        offset = base + i * stride
        px, py, pz = struct.unpack_from("<HHH", data, offset)
        x = cx + (px / 32767.0) * hx
        y = cy + (py / 32767.0) * hy
        z = cz + (pz / 32767.0) * hz
        u, v = struct.unpack_from("<ee", data, offset + 8)
        packed = struct.unpack_from("<I", data, offset + 16)[0]
        nx_raw = (packed >> 0) & 0x3FF
        ny_raw = (packed >> 10) & 0x3FF
        nz_raw = (packed >> 20) & 0x3FF
        nx = ny_raw / 511.5 - 1.0
        ny = nz_raw / 511.5 - 1.0
        nz = nx_raw / 511.5 - 1.0
        vertices.append(RuntimeVertex(pos=(x, y, z), uv=(float(u), float(v)), normal=(nx, ny, nz)))

    return vertices


def decode_indices(data: bytes, section_offset: int, index_count: int, *, index_start: int = 0) -> List[int]:
    base = section_offset + index_start
    return [struct.unpack_from("<H", data, base + i * 2)[0] for i in range(index_count)]


def _vector_len(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _find_pac_section_layout(
    data: bytes,
    geom_sec: Dict[str, int],
    descriptors: Sequence[MeshDescriptor],
    lod: int,
    total_indices: int,
) -> Tuple[int, int]:
    sec_off = geom_sec["offset"]
    sec_size = geom_sec["size"]
    total_verts = sum(descriptor.vertex_counts[lod] for descriptor in descriptors)
    primary_bytes = total_verts * 40
    index_bytes = total_indices * 2
    if primary_bytes + index_bytes >= sec_size:
        return 0, primary_bytes

    gap = sec_size - primary_bytes - index_bytes
    if gap <= 0:
        return 0, primary_bytes

    first_vc = next((descriptor.vertex_counts[lod] for descriptor in descriptors if descriptor.vertex_counts[lod] > 0), 0)
    if first_vc == 0:
        return 0, primary_bytes

    secondary_bytes = (gap // 40) * 40

    def scan_idx_start(after_verts: int) -> Optional[int]:
        for adj in range(0, sec_size - after_verts, 2):
            trial = after_verts + adj
            if trial + 6 > sec_size:
                break
            if struct.unpack_from("<H", data, sec_off + trial)[0] == 0:
                v1 = struct.unpack_from("<H", data, sec_off + trial + 2)[0]
                v2 = struct.unpack_from("<H", data, sec_off + trial + 4)[0]
                if v1 < first_vc and v2 < first_vc:
                    return trial
        return None

    def measure_quality(vertex_start: int, index_start: Optional[int]) -> float:
        if index_start is None or index_start + total_indices * 2 > sec_size:
            return 999.0
        vertices = decode_pac_vertices(data, sec_off, first_vc, descriptors[0], vertex_start=vertex_start)
        positions = [vertex.pos for vertex in vertices]
        first_ic = next((descriptor.index_counts[lod] for descriptor in descriptors if descriptor.index_counts[lod] > 0), 0)
        triangle_count = first_ic // 3
        if not positions or triangle_count <= 0:
            return 999.0
        sample_stride = max(1, triangle_count // 30)
        total_edge = 0.0
        for triangle_index in range(0, triangle_count, sample_stride):
            if triangle_index >= triangle_count:
                break
            base = sec_off + index_start + triangle_index * 6
            i0 = struct.unpack_from("<H", data, base)[0]
            i1 = struct.unpack_from("<H", data, base + 2)[0]
            i2 = struct.unpack_from("<H", data, base + 4)[0]
            if max(i0, i1, i2) >= len(positions):
                return 999.0
            p0, p1, p2 = positions[i0], positions[i1], positions[i2]
            total_edge += max(_vector_len(p1, p0), _vector_len(p2, p1), _vector_len(p0, p2))
            if triangle_index >= sample_stride * 30:
                break
        return total_edge

    best_vertex_start = 0
    best_index_start = primary_bytes + secondary_bytes
    best_quality = measure_quality(0, best_index_start) if best_index_start + total_indices * 2 <= sec_size else 999.0

    for secondary_vertex_count in range(0, gap // 40 + 1):
        vertex_start = secondary_vertex_count * 40
        all_vertices_end = vertex_start + primary_bytes
        if all_vertices_end >= sec_size:
            break
        index_start = scan_idx_start(all_vertices_end)
        if index_start is None or index_start + total_indices * 2 > sec_size:
            continue
        quality = measure_quality(vertex_start, index_start)
        if quality < best_quality:
            best_quality = quality
            best_vertex_start = vertex_start
            best_index_start = index_start

    return best_vertex_start, best_index_start


PAM_VERSIONS = {0x00001802, 0x00001803, 0x01001806}
PAC_VERSION = 0x01000903
SUBMESH_TABLE_OFF = 0x410
SUBMESH_STRIDE = 0x218


def parse_pam_header(data: bytes) -> Dict[str, object]:
    if data[:4] != b"PAR ":
        raise ValueError(f"Not a PAR file (magic={data[:4]!r})")
    version = struct.unpack_from("<I", data, 4)[0]
    if version == PAC_VERSION:
        raise ValueError("This is a PAC file, not PAM.")
    if version not in PAM_VERSIONS:
        raise ValueError(f"Unknown PAM version: 0x{version:08X}")

    return {
        "version": version,
        "mesh_count": struct.unpack_from("<I", data, 0x10)[0],
        "bbox_min": struct.unpack_from("<3f", data, 0x14),
        "bbox_max": struct.unpack_from("<3f", data, 0x20),
        "geom_off": struct.unpack_from("<I", data, 0x3C)[0],
        "geom_size": struct.unpack_from("<I", data, 0x40)[0],
        "comp_geom_size": struct.unpack_from("<I", data, 0x44)[0],
    }


def parse_pam_submeshes(data: bytes, count: int) -> List[PamSubmesh]:
    submeshes: List[PamSubmesh] = []
    for index in range(count):
        offset = SUBMESH_TABLE_OFF + index * SUBMESH_STRIDE
        if offset + SUBMESH_STRIDE > len(data):
            break
        nv, ni, voff, ioff = struct.unpack_from("<4I", data, offset)
        texture_name = data[offset + 16 : offset + 16 + 256].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        material_name = data[offset + 272 : offset + 272 + 256].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        submeshes.append(
            PamSubmesh(
                nv=nv,
                ni=ni,
                voff=voff,
                ioff=ioff,
                texture_name=texture_name,
                material_name=material_name,
            )
        )
    return submeshes


def decompress_pam_geometry(data: bytes) -> bytes:
    comp_size = struct.unpack_from("<I", data, 0x44)[0]
    if comp_size == 0:
        return data
    if lz4_block is None:
        raise RuntimeError("lz4 is required for PAM geometry decompression.")
    geom_off = struct.unpack_from("<I", data, 0x3C)[0]
    decomp_size = struct.unpack_from("<I", data, 0x40)[0]
    decompressed = lz4_block.decompress(data[geom_off : geom_off + comp_size], uncompressed_size=decomp_size)
    output = bytearray(data[:geom_off])
    output.extend(decompressed)
    footer_start = geom_off + comp_size
    if footer_start < len(data):
        output.extend(data[footer_start:])
    struct.pack_into("<I", output, 0x44, 0)
    return bytes(output)


def detect_vertex_stride(header: Dict[str, object], submeshes: Sequence[PamSubmesh]) -> int:
    total_nv = sum(submesh.nv for submesh in submeshes)
    total_ni = sum(submesh.ni for submesh in submeshes)
    if total_nv == 0:
        return 20
    geom_size = int(header["geom_size"])
    remaining = geom_size - total_ni * 2
    if remaining > 0 and remaining % total_nv == 0:
        return remaining // total_nv
    for stride in (20, 24, 28, 32, 36, 40, 16, 12, 8):
        if total_nv * stride + total_ni * 2 <= geom_size:
            return stride
    return 20


def decode_pam_vertices(
    data: bytes,
    geom_off: int,
    byte_offset: int,
    count: int,
    bbox_min: Tuple[float, float, float],
    bbox_max: Tuple[float, float, float],
    *,
    stride: int = 20,
) -> List[RuntimeVertex]:
    extent = (
        bbox_max[0] - bbox_min[0],
        bbox_max[1] - bbox_min[1],
        bbox_max[2] - bbox_min[2],
    )
    base = geom_off + byte_offset
    vertices: List[RuntimeVertex] = []

    for index in range(count):
        offset = base + index * stride
        px, py, pz = struct.unpack_from("<HHH", data, offset)
        x = bbox_min[0] + (px / 65535.0) * extent[0]
        y = bbox_min[1] + (py / 65535.0) * extent[1]
        z = bbox_min[2] + (pz / 65535.0) * extent[2]

        if stride >= 12:
            u, v = struct.unpack_from("<ee", data, offset + 8)
            uv = (float(u), float(v))
        else:
            uv = (0.0, 0.0)

        if stride >= 16:
            packed = struct.unpack_from("<I", data, offset + 12)[0]
            nx_raw = (packed >> 0) & 0x3FF
            ny_raw = (packed >> 10) & 0x3FF
            nz_raw = (packed >> 20) & 0x3FF
            normal = (
                ny_raw / 511.5 - 1.0,
                nz_raw / 511.5 - 1.0,
                nx_raw / 511.5 - 1.0,
            )
        else:
            normal = (0.0, 1.0, 0.0)

        vertices.append(RuntimeVertex(pos=(x, y, z), uv=uv, normal=normal))

    return vertices


def decode_pam_indices(data: bytes, byte_offset: int, count: int) -> List[int]:
    return [struct.unpack_from("<H", data, byte_offset + index * 2)[0] for index in range(count)]


def read_model_entry_bytes(entry: ArchiveEntry) -> bytes:
    raw = read_archive_entry_raw_data(entry)
    extension = entry.extension.lower()
    if extension == ".pac" and entry.compressed and entry.compression_type == 1:
        return decompress_type1_pac(raw)
    if extension == ".pam":
        return decompress_pam_geometry(raw)
    return raw


def load_pac_meshes(raw: bytes) -> List[RuntimeMesh]:
    header = parse_pac_header(raw)
    sections = {section["index"]: section for section in header["sections"]}  # type: ignore[index]
    if 0 not in sections:
        raise ValueError("No PAC metadata section was found.")

    geom_sec = None
    lod = 0
    for lod_section in (4, 3, 2, 1):
        if lod_section in sections:
            geom_sec = sections[lod_section]
            lod = 4 - lod_section
            break
    if geom_sec is None:
        raise ValueError("No PAC geometry section was found.")

    sec0 = sections[0]
    descriptors = find_pac_mesh_descriptors(raw, sec0["offset"], sec0["size"])  # type: ignore[index]
    if not descriptors:
        raise ValueError("No PAC mesh descriptors were found.")

    total_indices = sum(descriptor.index_counts[lod] for descriptor in descriptors)
    vert_base, index_byte_offset = _find_pac_section_layout(raw, geom_sec, descriptors, lod, total_indices)  # type: ignore[arg-type]

    descriptor_vertex_offsets: List[int] = []
    offset = vert_base
    for descriptor in descriptors:
        descriptor_vertex_offsets.append(offset)
        offset += descriptor.vertex_counts[lod] * 40

    partner_map: Dict[int, int] = {}
    index_offset_check = index_byte_offset
    for descriptor_index, descriptor in enumerate(descriptors):
        index_count = descriptor.index_counts[lod]
        vertex_count = descriptor.vertex_counts[lod]
        if vertex_count == 0:
            index_offset_check += index_count * 2
            continue
        raw_indices = decode_indices(raw, geom_sec["offset"], index_count, index_start=index_offset_check)  # type: ignore[index]
        max_index = max(raw_indices) if raw_indices else 0
        if max_index >= vertex_count:
            for partner_index, partner in enumerate(descriptors):
                if partner.vertex_counts[lod] > max_index and partner_index != descriptor_index:
                    partner_map[descriptor_index] = partner_index
                    break
        index_offset_check += index_count * 2

    meshes: List[RuntimeMesh] = []
    for descriptor_index, descriptor in enumerate(descriptors):
        vertex_count = descriptor.vertex_counts[lod]
        index_count = descriptor.index_counts[lod]
        if vertex_count == 0:
            continue

        indices = decode_indices(raw, geom_sec["offset"], index_count, index_start=index_byte_offset)  # type: ignore[index]
        if descriptor_index in partner_map:
            partner_index = partner_map[descriptor_index]
            partner_descriptor = descriptors[partner_index]
            vertices = decode_pac_vertices(
                raw,
                geom_sec["offset"],  # type: ignore[index]
                partner_descriptor.vertex_counts[lod],
                descriptor,
                vertex_start=descriptor_vertex_offsets[partner_index],
            )
        else:
            vertices = decode_pac_vertices(
                raw,
                geom_sec["offset"],  # type: ignore[index]
                vertex_count,
                descriptor,
                vertex_start=descriptor_vertex_offsets[descriptor_index],
            )

        meshes.append(
            RuntimeMesh(
                name=descriptor.display_name,
                material=descriptor.material_name,
                vertices=vertices,
                indices=indices,
            )
        )
        index_byte_offset += index_count * 2

    if not meshes:
        raise ValueError("No PAC meshes with geometry were found.")
    return meshes


def load_pam_meshes(raw: bytes) -> List[RuntimeMesh]:
    header = parse_pam_header(raw)
    submeshes = parse_pam_submeshes(raw, int(header["mesh_count"]))
    if not submeshes:
        raise ValueError("No PAM submeshes were found.")

    stride = detect_vertex_stride(header, submeshes)
    total_nv = sum(submesh.nv for submesh in submeshes)
    geom_off = int(header["geom_off"])
    index_byte_start = geom_off + total_nv * stride

    meshes: List[RuntimeMesh] = []
    for submesh in submeshes:
        if submesh.nv == 0:
            continue
        vertices = decode_pam_vertices(
            raw,
            geom_off,
            submesh.voff * stride,
            submesh.nv,
            header["bbox_min"],  # type: ignore[arg-type]
            header["bbox_max"],  # type: ignore[arg-type]
            stride=stride,
        )
        indices = decode_pam_indices(raw, index_byte_start + submesh.ioff * 2, submesh.ni)
        material_name = submesh.texture_name[:-4] if submesh.texture_name.lower().endswith(".dds") else submesh.texture_name
        meshes.append(
            RuntimeMesh(
                name=submesh.material_name or material_name or f"submesh_{len(meshes)}",
                material=material_name or submesh.material_name or "(null)",
                vertices=vertices,
                indices=indices,
            )
        )

    if not meshes:
        raise ValueError("No PAM meshes with geometry were found.")
    return meshes


def material_to_dds_basename(material_name: str) -> str:
    lower = material_name.lower()
    if lower.startswith("cd_phw_00_nude_") or lower.startswith("cd_phw_00_head_"):
        prefix_end = len("cd_phw_00_")
        rest = lower[prefix_end:]
        parts = rest.split("_")
        for index, part in enumerate(parts):
            if part.isdigit() and len(part) == 4:
                parts.insert(index, "00")
                break
        return "cd_phw_00_" + "_".join(parts)
    return lower


def write_mtl(meshes: Sequence[RuntimeMesh], mtl_path: Path, *, texture_rel_dir: str = "", available_textures: Optional[set[str]] = None) -> None:
    with mtl_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Materials for {mtl_path.stem}\n\n")
        seen: set[str] = set()
        for mesh in meshes:
            if mesh.material in seen or mesh.material == "(null)":
                continue
            seen.add(mesh.material)

            dds_base = material_to_dds_basename(mesh.material)
            tex_prefix = f".\\{texture_rel_dir}\\{dds_base}" if texture_rel_dir else dds_base

            def tex_exists(suffix: str) -> bool:
                name = f"{dds_base}{suffix}.dds"
                return available_textures is None or name in available_textures

            handle.write(f"newmtl {mesh.material}\n")
            handle.write("Ka 0.2 0.2 0.2\n")
            handle.write("Kd 0.8 0.8 0.8\n")
            handle.write("Ks 0.5 0.5 0.5\n")
            handle.write("Ns 100.0\n")
            if tex_exists(""):
                handle.write(f"map_Kd {tex_prefix}.dds\n")
            elif tex_exists("_ma"):
                handle.write(f"map_Kd {tex_prefix}_ma.dds\n")
            if tex_exists("_n"):
                handle.write(f"bump {tex_prefix}_n.dds\n")
            if tex_exists("_sp"):
                handle.write(f"map_Ks {tex_prefix}_sp.dds\n")
            elif tex_exists("_mg"):
                handle.write(f"map_Ks {tex_prefix}_mg.dds\n")
            if tex_exists("_disp"):
                handle.write(f"disp {tex_prefix}_disp.dds\n")
            handle.write("\n")


def write_obj(meshes: Sequence[RuntimeMesh], obj_path: Path, mtl_filename: str) -> None:
    with obj_path.open("w", encoding="utf-8") as handle:
        handle.write("# Crimson Texture Forge model export\n")
        handle.write(f"mtllib {mtl_filename}\n\n")

        vertex_offset = 0
        for mesh in meshes:
            handle.write(f"o {_safe_ascii_name(mesh.name)}\n")
            handle.write(f"usemtl {_safe_ascii_name(mesh.material)}\n")
            for vertex in mesh.vertices:
                x, y, z = vertex.pos
                handle.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")
            for vertex in mesh.vertices:
                nx, ny, nz = vertex.normal
                handle.write(f"vn {nx:.6f} {ny:.6f} {nz:.6f}\n")
            for vertex in mesh.vertices:
                u, v = vertex.uv
                handle.write(f"vt {u:.6f} {1.0 - v:.6f}\n")
            for index in range(0, len(mesh.indices), 3):
                if index + 2 >= len(mesh.indices):
                    break
                i0 = mesh.indices[index] + vertex_offset + 1
                i1 = mesh.indices[index + 1] + vertex_offset + 1
                i2 = mesh.indices[index + 2] + vertex_offset + 1
                handle.write(f"f {i0}/{i0}/{i0} {i1}/{i1}/{i1} {i2}/{i2}/{i2}\n")
            vertex_offset += len(mesh.vertices)
            handle.write("\n")


def load_runtime_meshes_for_entry(entry: ArchiveEntry) -> List[RuntimeMesh]:
    extension = entry.extension.lower()
    raw = read_model_entry_bytes(entry)
    if extension == ".pac":
        return load_pac_meshes(raw)
    if extension == ".pam":
        return load_pam_meshes(raw)
    raise ValueError(f"Unsupported runtime model preview format: {entry.extension}")


def build_wireframe_preview(
    meshes: Sequence[RuntimeMesh],
    *,
    max_preview_triangles: int = 12_000,
) -> Tuple[List[Tuple[float, float, float]], List[Tuple[int, int]], Dict[str, int]]:
    total_triangles = sum(len(mesh.indices) // 3 for mesh in meshes)
    total_vertices = sum(len(mesh.vertices) for mesh in meshes)
    if total_triangles <= 0 or total_vertices <= 0:
        return [], [], {"meshes": len(meshes), "vertices": total_vertices, "triangles": total_triangles, "sampled_triangles": 0}

    triangle_stride = max(1, math.ceil(total_triangles / max_preview_triangles))
    compact_vertices: List[Tuple[float, float, float]] = []
    edges: List[Tuple[int, int]] = []
    vertex_map: Dict[Tuple[int, int], int] = {}
    edge_set: set[Tuple[int, int]] = set()
    sampled_triangles = 0
    global_triangle_index = 0

    def map_vertex(mesh_index: int, vertex_index: int, vertex: RuntimeVertex) -> int:
        key = (mesh_index, vertex_index)
        existing = vertex_map.get(key)
        if existing is not None:
            return existing
        mapped = len(compact_vertices)
        compact_vertices.append(vertex.pos)
        vertex_map[key] = mapped
        return mapped

    for mesh_index, mesh in enumerate(meshes):
        indices = mesh.indices
        vertices = mesh.vertices
        for tri_index in range(0, len(indices), 3):
            if tri_index + 2 >= len(indices):
                break
            if global_triangle_index % triangle_stride != 0:
                global_triangle_index += 1
                continue
            i0, i1, i2 = indices[tri_index], indices[tri_index + 1], indices[tri_index + 2]
            if max(i0, i1, i2) >= len(vertices):
                global_triangle_index += 1
                continue
            v0 = map_vertex(mesh_index, i0, vertices[i0])
            v1 = map_vertex(mesh_index, i1, vertices[i1])
            v2 = map_vertex(mesh_index, i2, vertices[i2])
            for a, b in ((v0, v1), (v1, v2), (v2, v0)):
                edge = (a, b) if a < b else (b, a)
                if edge not in edge_set:
                    edge_set.add(edge)
                    edges.append(edge)
            sampled_triangles += 1
            global_triangle_index += 1

    return compact_vertices, edges, {
        "meshes": len(meshes),
        "vertices": total_vertices,
        "triangles": total_triangles,
        "sampled_triangles": sampled_triangles,
    }


def _safe_export_dir_for_entry(entry: ArchiveEntry, output_root: Path) -> Path:
    pure_path = PurePosixPath(entry.path.replace("\\", "/"))
    safe_parts = [part for part in pure_path.parts if part not in {"", ".", ".."}]
    package_root = entry.pamt_path.parent.name.strip() or "package"
    folder_parts = [_safe_ascii_name(part) for part in safe_parts[:-1]]
    stem = _safe_ascii_name(Path(safe_parts[-1]).stem if safe_parts else "model")
    return output_root.joinpath(package_root, *folder_parts, stem)


def export_entry_for_blender(
    entry: ArchiveEntry,
    all_entries: Sequence[ArchiveEntry],
    output_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    extension = entry.extension.lower()
    if extension not in {".pac", ".pam"}:
        raise ValueError("Blender export is currently implemented for PAC and PAM files.")

    meshes = load_runtime_meshes_for_entry(entry)
    export_dir = _safe_export_dir_for_entry(entry, output_root)
    export_dir.mkdir(parents=True, exist_ok=True)
    base_name = _safe_ascii_name(Path(entry.path).stem)
    obj_path = export_dir / f"{base_name}.obj"
    mtl_path = export_dir / f"{base_name}.mtl"
    textures_dir = export_dir / "textures"

    texture_candidates: Dict[str, ArchiveEntry] = {}
    lookup: Dict[str, ArchiveEntry] = {}
    for candidate in all_entries:
        if candidate.extension.lower() != ".dds":
            continue
        lookup.setdefault(candidate.basename.lower(), candidate)

    for mesh in meshes:
        base = material_to_dds_basename(mesh.material)
        for suffix in ("", "_ma", "_n", "_sp", "_mg", "_disp"):
            filename = f"{base}{suffix}.dds"
            candidate = lookup.get(filename.lower())
            if candidate is not None:
                texture_candidates[filename.lower()] = candidate

    available_textures: set[str] = set()
    if texture_candidates:
        textures_dir.mkdir(parents=True, exist_ok=True)
        for filename, texture_entry in sorted(texture_candidates.items()):
            out_path = textures_dir / filename
            extract_archive_entry(texture_entry, out_path)
            available_textures.add(filename)
            if on_log is not None:
                on_log(f"Exported texture sidecar: {texture_entry.path} -> {out_path}")

    write_obj(meshes, obj_path, mtl_path.name)
    write_mtl(meshes, mtl_path, texture_rel_dir="textures" if available_textures else "", available_textures=available_textures or None)
    if on_log is not None:
        on_log(f"Exported Blender-ready model: {entry.path} -> {obj_path}")

    return {
        "entry_path": entry.path,
        "output_dir": str(export_dir),
        "obj_path": str(obj_path),
        "mtl_path": str(mtl_path),
        "texture_count": len(available_textures),
        "mesh_count": len(meshes),
        "vertex_count": sum(len(mesh.vertices) for mesh in meshes),
        "triangle_count": sum(len(mesh.indices) // 3 for mesh in meshes),
    }

