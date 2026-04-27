"""Scene-file import helpers for static mesh replacement.

OBJ remains the strict round-trip format.  This module accepts broader scene
formats only for static replacement and normalizes them into ParsedMesh.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import struct
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .logging import get_logger
from .mesh_importer import import_obj
from .mesh_parser import ParsedMesh, SubMesh, _compute_smooth_normals

logger = get_logger("core.scene_importer")

SCENE_IMPORT_EXTENSIONS = {".obj", ".dae", ".gltf", ".glb"}
SCENE_TEXTURE_SOURCE_EXTENSIONS = {".png", ".dds", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}
_GLTF_COMPONENT_FORMATS = {
    5120: ("b", 1, True),
    5121: ("B", 1, False),
    5122: ("h", 2, True),
    5123: ("H", 2, False),
    5125: ("I", 4, False),
    5126: ("f", 4, True),
}
_GLTF_TYPE_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_GLTF_IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/vnd-ms.dds": ".dds",
    "image/x-dds": ".dds",
    "image/tga": ".tga",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
}
_SCENE_TEXTURE_DISCOVERY_MAX_FILES = 5000


@dataclass(slots=True)
class SceneImportResult:
    mesh: ParsedMesh
    diagnostics: tuple[str, ...] = ()
    discovered_texture_files: tuple[Path, ...] = ()
    extracted_embedded_files: tuple[Path, ...] = ()


@dataclass(slots=True)
class _ColladaGeometry:
    geometry_id: str
    name: str
    primitives: list[SubMesh]


@dataclass(slots=True)
class _GltfPayload:
    document: dict[str, Any]
    buffers: list[bytes]
    source_path: Path
    format_name: str
    diagnostics: list[str]
    extracted_embedded_files: list[Path]
    discovered_texture_files: list[Path]


def import_scene_mesh(path: str | Path) -> ParsedMesh:
    return import_scene_mesh_with_report(path).mesh


def import_scene_mesh_with_report(path: str | Path) -> SceneImportResult:
    source_path = Path(path).expanduser().resolve()
    suffix = source_path.suffix.lower()
    if suffix == ".obj":
        mesh = import_obj(str(source_path))
        return SceneImportResult(mesh=mesh, discovered_texture_files=discover_scene_texture_files(source_path, mesh))
    if suffix == ".dae":
        mesh = import_dae(source_path)
        return SceneImportResult(mesh=mesh, discovered_texture_files=discover_scene_texture_files(source_path, mesh))
    if suffix in {".gltf", ".glb"}:
        return import_gltf(source_path)
    raise ValueError(f"Unsupported mesh import format: {source_path.suffix or source_path.name}")


def import_gltf(path: str | Path) -> SceneImportResult:
    source_path = Path(path).expanduser().resolve()
    payload = _load_gltf_payload(source_path)
    _validate_gltf_static_payload(payload)
    material_names, material_textures = _gltf_material_info(payload)
    submeshes: list[SubMesh] = []
    mesh_instances = _iter_gltf_mesh_instances(payload.document)
    if not mesh_instances:
        mesh_instances = [(index, _identity_matrix(), "") for index, _mesh in enumerate(payload.document.get("meshes", []) or [])]
    for mesh_index, transform, node_name in mesh_instances:
        gltf_meshes = payload.document.get("meshes", []) or []
        if mesh_index < 0 or mesh_index >= len(gltf_meshes):
            continue
        mesh_entry = gltf_meshes[mesh_index]
        mesh_name = str(mesh_entry.get("name", "") or "")
        for primitive_index, primitive in enumerate(mesh_entry.get("primitives", []) or []):
            if not isinstance(primitive, dict):
                continue
            mode = int(primitive.get("mode", 4) or 4)
            if mode != 4:
                payload.diagnostics.append(
                    f"Skipped glTF primitive {mesh_name or mesh_index}:{primitive_index} because only TRIANGLES mode is supported."
                )
                continue
            attributes = primitive.get("attributes", {})
            if not isinstance(attributes, dict) or "POSITION" not in attributes:
                payload.diagnostics.append(
                    f"Skipped glTF primitive {mesh_name or mesh_index}:{primitive_index} because it has no POSITION attribute."
                )
                continue
            material_index = _safe_int(primitive.get("material"), -1)
            material_name = (material_names.get(material_index, "") or f"material_{material_index}") if material_index >= 0 else ""
            texture_path = material_textures.get(material_index, "")
            submesh = _parse_gltf_primitive(
                payload,
                primitive,
                name=node_name or mesh_name or f"mesh_{mesh_index}_{primitive_index}",
                material=material_name or node_name or mesh_name or f"mesh_{mesh_index}_{primitive_index}",
                texture=texture_path,
            )
            if not submesh.faces:
                payload.diagnostics.append(
                    f"Skipped glTF primitive {mesh_name or mesh_index}:{primitive_index} because it produced no triangle faces."
                )
                continue
            copied = _copy_submesh_with_transform(submesh, transform)
            copied.name = submesh.name
            copied.material = submesh.material
            copied.texture = submesh.texture
            submeshes.append(copied)
    if not submeshes:
        raise ValueError(f"glTF import did not contain supported uncompressed triangle geometry: {source_path}")
    vertices = [vertex for submesh in submeshes for vertex in submesh.vertices]
    bbox_min, bbox_max = _bbox(vertices)
    mesh = ParsedMesh(
        path=str(source_path),
        format=payload.format_name,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        submeshes=submeshes,
        total_vertices=sum(len(submesh.vertices) for submesh in submeshes),
        total_faces=sum(len(submesh.faces) for submesh in submeshes),
        has_uvs=any(submesh.uvs for submesh in submeshes),
        has_bones=False,
    )
    if payload.extracted_embedded_files:
        payload.diagnostics.append(
            f"Extracted {len(payload.extracted_embedded_files):,} embedded glTF texture file(s) for supplemental import."
        )
    if payload.discovered_texture_files:
        payload.diagnostics.append(
            f"Discovered {len(payload.discovered_texture_files):,} glTF texture reference(s)."
        )
    return SceneImportResult(
        mesh=mesh,
        diagnostics=tuple(_dedupe_text(payload.diagnostics)),
        discovered_texture_files=tuple(_dedupe_paths(payload.discovered_texture_files)),
        extracted_embedded_files=tuple(_dedupe_paths(payload.extracted_embedded_files)),
    )


def import_dae(path: str | Path) -> ParsedMesh:
    dae_path = Path(path).expanduser().resolve()
    tree = ET.parse(dae_path)
    root = tree.getroot()
    ns_uri = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    ns = {"c": ns_uri} if ns_uri else {}
    prefix = "c:" if ns_uri else ""

    material_names = _collada_material_names(root, prefix, ns)
    geometries: dict[str, _ColladaGeometry] = {}
    for geometry in root.findall(f".//{prefix}library_geometries/{prefix}geometry", ns):
        parsed = _parse_collada_geometry(geometry, material_names, prefix, ns)
        geometries[parsed.geometry_id] = parsed

    submeshes: list[SubMesh] = []
    for instance in _iter_collada_geometry_instances(root, prefix, ns):
        geometry = geometries.get(instance["geometry_id"])
        if geometry is None:
            continue
        transform = instance["matrix"]
        node_name = instance["node_name"]
        for primitive in geometry.primitives:
            material = instance["materials"].get(primitive.material, primitive.material)
            material = material_names.get(str(material), str(material))
            name = node_name or geometry.name or primitive.name or geometry.geometry_id
            copied = _copy_submesh_with_transform(primitive, transform)
            copied.name = name
            copied.material = material or primitive.material or name
            copied.texture = _guess_scene_material_texture(dae_path, copied.material)
            submeshes.append(copied)

    if not submeshes:
        for geometry in geometries.values():
            for primitive in geometry.primitives:
                copied = _copy_submesh_with_transform(primitive, _identity_matrix())
                copied.name = geometry.name or primitive.name or geometry.geometry_id
                copied.material = material_names.get(primitive.material, primitive.material) or copied.name
                copied.texture = _guess_scene_material_texture(dae_path, copied.material)
                submeshes.append(copied)

    if not submeshes:
        raise ValueError(f"DAE import did not contain supported triangle/polylist geometry: {dae_path}")
    vertices = [vertex for submesh in submeshes for vertex in submesh.vertices]
    bbox_min, bbox_max = _bbox(vertices)
    return ParsedMesh(
        path=str(dae_path),
        format="dae",
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        submeshes=submeshes,
        total_vertices=sum(len(submesh.vertices) for submesh in submeshes),
        total_faces=sum(len(submesh.faces) for submesh in submeshes),
        has_uvs=any(submesh.uvs for submesh in submeshes),
        has_bones=False,
    )


def import_fbx(path: str | Path) -> ParsedMesh:
    fbx_path = Path(path).expanduser().resolve()
    raise ValueError(
        f"FBX import is disabled in this build because it required launching Blender: {fbx_path}. "
        "Export the model as OBJ or DAE first."
    )


def discover_scene_texture_files(path: str | Path, mesh: Optional[ParsedMesh] = None) -> tuple[Path, ...]:
    scene_path = Path(path).expanduser().resolve()
    discovered: list[Path] = []
    if scene_path.suffix.lower() == ".dae":
        discovered.extend(_collada_image_paths(scene_path))
    elif scene_path.suffix.lower() in {".gltf", ".glb"}:
        try:
            payload = _load_gltf_payload(scene_path)
            _gltf_material_info(payload)
            discovered.extend(payload.discovered_texture_files)
            discovered.extend(payload.extracted_embedded_files)
        except Exception as exc:
            logger.warning("Failed to discover glTF texture files for %s: %s", scene_path, exc)
    material_names = {
        str(submesh.material or submesh.name or "").strip().lower()
        for submesh in (mesh.submeshes if mesh is not None else [])
        if str(submesh.material or submesh.name or "").strip()
    }
    search_roots = [scene_path.parent, scene_path.parent / "textures", scene_path.parent.parent / "textures"]
    scanned_files = 0
    search_limited = False
    for root in search_roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            scanned_files += 1
            if scanned_files > _SCENE_TEXTURE_DISCOVERY_MAX_FILES:
                search_limited = True
                break
            if not candidate.is_file() or candidate.suffix.lower() not in SCENE_TEXTURE_SOURCE_EXTENSIONS:
                continue
            stem = candidate.stem.lower()
            if any(stem.startswith(material_name) for material_name in material_names):
                discovered.append(candidate)
        if search_limited:
            break
    if search_limited:
        logger.info(
            "Stopped scene texture discovery for %s after scanning %d filesystem entries. "
            "Add additional textures through Supplemental Files if needed.",
            scene_path,
            _SCENE_TEXTURE_DISCOVERY_MAX_FILES,
        )
    unique: dict[str, Path] = {}
    for candidate in discovered:
        if candidate.is_file():
            unique.setdefault(str(candidate.resolve()).lower(), candidate.resolve())
    return tuple(unique.values())


def _load_gltf_payload(source_path: Path) -> _GltfPayload:
    diagnostics: list[str] = []
    extracted_embedded_files: list[Path] = []
    discovered_texture_files: list[Path] = []
    suffix = source_path.suffix.lower()
    if suffix == ".glb":
        document, bin_chunk = _read_glb(source_path)
        format_name = "glb"
    else:
        try:
            document = json.loads(source_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            document = json.loads(source_path.read_text(encoding="utf-8-sig"))
        bin_chunk = b""
        format_name = "gltf"
    if not isinstance(document, dict):
        raise ValueError(f"glTF document is not a JSON object: {source_path}")
    asset = document.get("asset", {})
    version = str(asset.get("version", "") if isinstance(asset, dict) else "")
    if version and not version.startswith("2."):
        diagnostics.append(f"glTF asset version is {version}; importer is written for glTF 2.0.")
    buffers: list[bytes] = []
    for index, buffer_entry in enumerate(document.get("buffers", []) or []):
        if not isinstance(buffer_entry, dict):
            buffers.append(b"")
            continue
        uri = str(buffer_entry.get("uri", "") or "")
        if suffix == ".glb" and index == 0 and not uri:
            buffers.append(bin_chunk)
        elif uri.startswith("data:"):
            buffers.append(_decode_data_uri(uri))
        elif uri:
            buffer_path = _resolve_scene_uri(source_path.parent, uri)
            buffers.append(buffer_path.read_bytes())
        else:
            buffers.append(b"")
    return _GltfPayload(
        document=document,
        buffers=buffers,
        source_path=source_path,
        format_name=format_name,
        diagnostics=diagnostics,
        extracted_embedded_files=extracted_embedded_files,
        discovered_texture_files=discovered_texture_files,
    )


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError(f"GLB file is too small: {path}")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError(f"Invalid GLB header: {path}")
    if version != 2:
        raise ValueError(f"Unsupported GLB version {version}; export as GLB 2.0.")
    cursor = 12
    document: dict[str, Any] | None = None
    bin_chunk = b""
    while cursor + 8 <= min(length, len(data)):
        chunk_length, chunk_type = struct.unpack_from("<II", data, cursor)
        cursor += 8
        chunk_data = data[cursor : cursor + chunk_length]
        cursor += chunk_length
        if chunk_type == 0x4E4F534A:
            document = json.loads(chunk_data.rstrip(b"\x00 ").decode("utf-8"))
        elif chunk_type == 0x004E4942:
            bin_chunk = bytes(chunk_data)
    if document is None:
        raise ValueError(f"GLB file does not contain a JSON chunk: {path}")
    return document, bin_chunk


def _validate_gltf_static_payload(payload: _GltfPayload) -> None:
    doc = payload.document
    used_extensions = set(doc.get("extensionsUsed", []) or []) | set(doc.get("extensionsRequired", []) or [])
    compressed = sorted(ext for ext in used_extensions if ext in {"KHR_draco_mesh_compression", "EXT_meshopt_compression"})
    if compressed:
        raise ValueError(
            "This glTF/GLB uses compressed mesh data "
            f"({', '.join(compressed)}). Export an uncompressed GLB/glTF before importing."
        )
    if doc.get("skins"):
        payload.diagnostics.append("glTF skins/bones are ignored; import will use static Mesh Replacement only.")
    if doc.get("animations"):
        payload.diagnostics.append("glTF animations are ignored; import will use static Mesh Replacement only.")
    warned_morphs = False
    for mesh in doc.get("meshes", []) or []:
        if not isinstance(mesh, dict):
            continue
        for primitive in mesh.get("primitives", []) or []:
            if isinstance(primitive, dict) and primitive.get("targets") and not warned_morphs:
                payload.diagnostics.append("glTF morph targets are ignored for static Mesh Replacement.")
                warned_morphs = True


def _gltf_material_info(payload: _GltfPayload) -> tuple[dict[int, str], dict[int, str]]:
    material_names: dict[int, str] = {}
    material_textures: dict[int, str] = {}
    textures = payload.document.get("textures", []) or []
    images = payload.document.get("images", []) or []
    for material_index, material in enumerate(payload.document.get("materials", []) or []):
        if not isinstance(material, dict):
            continue
        material_names[material_index] = str(material.get("name", "") or f"material_{material_index}")
        pbr = material.get("pbrMetallicRoughness", {})
        texture_info = pbr.get("baseColorTexture") if isinstance(pbr, dict) else None
        if not isinstance(texture_info, dict):
            continue
        texture_index = _safe_int(texture_info.get("index"), -1)
        if texture_index < 0 or texture_index >= len(textures) or not isinstance(textures[texture_index], dict):
            continue
        image_index = _safe_int(textures[texture_index].get("source"), -1)
        if image_index < 0 or image_index >= len(images) or not isinstance(images[image_index], dict):
            continue
        image_path = _resolve_gltf_image(payload, images[image_index], image_index)
        if image_path is not None:
            material_textures[material_index] = image_path.as_posix()
            if image_path.suffix.lower() in SCENE_TEXTURE_SOURCE_EXTENSIONS:
                payload.discovered_texture_files.append(image_path)
    return material_names, material_textures


def _resolve_gltf_image(payload: _GltfPayload, image: dict[str, Any], image_index: int) -> Optional[Path]:
    uri = str(image.get("uri", "") or "")
    if uri:
        if uri.startswith("data:"):
            mime_type, data = _decode_data_uri_with_mime(uri)
            return _write_embedded_gltf_image(payload, image_index, data, mime_type)
        image_path = _resolve_scene_uri(payload.source_path.parent, uri)
        return image_path.resolve() if image_path.is_file() else image_path
    buffer_view_index = _safe_int(image.get("bufferView"), -1)
    if buffer_view_index >= 0:
        image_bytes = _read_gltf_buffer_view_bytes(payload, buffer_view_index)
        mime_type = str(image.get("mimeType", "") or "")
        return _write_embedded_gltf_image(payload, image_index, image_bytes, mime_type)
    return None


def _write_embedded_gltf_image(payload: _GltfPayload, image_index: int, data: bytes, mime_type: str) -> Path:
    ext = _GLTF_IMAGE_MIME_EXTENSIONS.get(str(mime_type or "").lower(), "")
    if not ext:
        guessed = mimetypes.guess_extension(str(mime_type or "")) or ""
        ext = guessed if guessed.lower() in SCENE_TEXTURE_SOURCE_EXTENSIONS else ".bin"
    export_dir = _embedded_gltf_extract_dir(payload.source_path)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"image_{image_index}{ext}"
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)
    payload.extracted_embedded_files.append(path.resolve())
    return path.resolve()


def _embedded_gltf_extract_dir(source_path: Path) -> Path:
    try:
        stat = source_path.stat()
        key = f"{source_path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        key = str(source_path)
    digest = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "cdmw_gltf_imports" / digest


def _iter_gltf_mesh_instances(document: dict[str, Any]) -> list[tuple[int, tuple[float, ...], str]]:
    scenes = document.get("scenes", []) or []
    scene_index = _safe_int(document.get("scene"), 0)
    root_nodes: list[int] = []
    if 0 <= scene_index < len(scenes) and isinstance(scenes[scene_index], dict):
        root_nodes = [_safe_int(value, -1) for value in scenes[scene_index].get("nodes", []) or []]
    if not root_nodes:
        root_nodes = list(range(len(document.get("nodes", []) or [])))
    instances: list[tuple[int, tuple[float, ...], str]] = []
    for node_index in root_nodes:
        _walk_gltf_node(document, node_index, _identity_matrix(), instances)
    return instances


def _walk_gltf_node(
    document: dict[str, Any],
    node_index: int,
    parent_matrix: tuple[float, ...],
    instances: list[tuple[int, tuple[float, ...], str]],
) -> None:
    nodes = document.get("nodes", []) or []
    if node_index < 0 or node_index >= len(nodes) or not isinstance(nodes[node_index], dict):
        return
    node = nodes[node_index]
    matrix = _multiply_matrix(parent_matrix, _gltf_node_matrix(node))
    mesh_index = _safe_int(node.get("mesh"), -1)
    node_name = str(node.get("name", "") or "")
    if mesh_index >= 0:
        instances.append((mesh_index, matrix, node_name))
    for child_index in node.get("children", []) or []:
        _walk_gltf_node(document, _safe_int(child_index, -1), matrix, instances)


def _gltf_node_matrix(node: dict[str, Any]) -> tuple[float, ...]:
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) >= 16:
        values = [float(value) for value in matrix[:16]]
        return (
            values[0], values[4], values[8], values[12],
            values[1], values[5], values[9], values[13],
            values[2], values[6], values[10], values[14],
            values[3], values[7], values[11], values[15],
        )
    translation = _float_list(node.get("translation"), 3, (0.0, 0.0, 0.0))
    rotation = _float_list(node.get("rotation"), 4, (0.0, 0.0, 0.0, 1.0))
    scale = _float_list(node.get("scale"), 3, (1.0, 1.0, 1.0))
    return _compose_trs_matrix(translation, rotation, scale)


def _compose_trs_matrix(
    translation: tuple[float, ...],
    rotation: tuple[float, ...],
    scale: tuple[float, ...],
) -> tuple[float, ...]:
    x, y, z, w = rotation
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    sx, sy, sz = scale
    return (
        (1.0 - 2.0 * (yy + zz)) * sx,
        (2.0 * (xy - wz)) * sy,
        (2.0 * (xz + wy)) * sz,
        translation[0],
        (2.0 * (xy + wz)) * sx,
        (1.0 - 2.0 * (xx + zz)) * sy,
        (2.0 * (yz - wx)) * sz,
        translation[1],
        (2.0 * (xz - wy)) * sx,
        (2.0 * (yz + wx)) * sy,
        (1.0 - 2.0 * (xx + yy)) * sz,
        translation[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def _parse_gltf_primitive(
    payload: _GltfPayload,
    primitive: dict[str, Any],
    *,
    name: str,
    material: str,
    texture: str,
) -> SubMesh:
    attributes = primitive.get("attributes", {})
    positions = _read_gltf_accessor(payload, _safe_int(attributes.get("POSITION"), -1), expected_components=3)
    normals = _read_gltf_accessor(payload, _safe_int(attributes.get("NORMAL"), -1), expected_components=3)
    uvs = _read_gltf_accessor(payload, _safe_int(attributes.get("TEXCOORD_0"), -1), expected_components=2)
    index_accessor = _safe_int(primitive.get("indices"), -1)
    if index_accessor >= 0:
        raw_indices = [int(values[0]) for values in _read_gltf_accessor(payload, index_accessor, expected_components=1)]
    else:
        raw_indices = list(range(len(positions)))
    faces = [
        (raw_indices[index], raw_indices[index + 1], raw_indices[index + 2])
        for index in range(0, len(raw_indices) - 2, 3)
        if max(raw_indices[index], raw_indices[index + 1], raw_indices[index + 2]) < len(positions)
    ]
    normalized_uvs = [(float(uv[0]), 1.0 - float(uv[1])) for uv in uvs]
    if len(normalized_uvs) != len(positions):
        normalized_uvs = [(0.0, 0.0)] * len(positions)
    if len(normals) != len(positions):
        normals = _compute_smooth_normals(positions, faces)
    return SubMesh(
        name=name,
        material=material,
        texture=texture,
        vertices=[(float(v[0]), float(v[1]), float(v[2])) for v in positions],
        uvs=normalized_uvs,
        normals=[(float(n[0]), float(n[1]), float(n[2])) for n in normals],
        faces=faces,
        vertex_count=len(positions),
        face_count=len(faces),
    )


def _read_gltf_accessor(payload: _GltfPayload, accessor_index: int, *, expected_components: int) -> list[tuple[float, ...]]:
    accessors = payload.document.get("accessors", []) or []
    if accessor_index < 0:
        return []
    if accessor_index >= len(accessors) or not isinstance(accessors[accessor_index], dict):
        raise ValueError(f"glTF accessor index is invalid: {accessor_index}")
    accessor = accessors[accessor_index]
    if accessor.get("sparse"):
        payload.diagnostics.append("glTF sparse accessors are not expanded; affected attributes may import incompletely.")
    component_type = int(accessor.get("componentType", 0) or 0)
    type_name = str(accessor.get("type", "SCALAR") or "SCALAR")
    component_count = _GLTF_TYPE_COUNTS.get(type_name, 1)
    if expected_components > component_count:
        return []
    count = int(accessor.get("count", 0) or 0)
    buffer_view_index = _safe_int(accessor.get("bufferView"), -1)
    if buffer_view_index < 0:
        return [(0.0,) * expected_components for _index in range(count)]
    view = _gltf_buffer_view(payload, buffer_view_index)
    fmt, component_size, _signed = _GLTF_COMPONENT_FORMATS.get(component_type, ("", 0, False))
    if not fmt or component_size <= 0:
        raise ValueError(f"Unsupported glTF accessor component type: {component_type}")
    buffer_index = _safe_int(view.get("buffer"), -1)
    if buffer_index < 0 or buffer_index >= len(payload.buffers):
        raise ValueError(f"glTF accessor references missing buffer {buffer_index}.")
    buffer_data = payload.buffers[buffer_index]
    view_offset = int(view.get("byteOffset", 0) or 0)
    accessor_offset = int(accessor.get("byteOffset", 0) or 0)
    byte_stride = int(view.get("byteStride", 0) or 0) or component_size * component_count
    start = view_offset + accessor_offset
    normalized = bool(accessor.get("normalized", False))
    rows: list[tuple[float, ...]] = []
    unpack = struct.Struct("<" + fmt)
    for row_index in range(count):
        row_start = start + row_index * byte_stride
        values: list[float] = []
        for component_index in range(component_count):
            offset = row_start + component_index * component_size
            if offset + component_size > len(buffer_data):
                values.append(0.0)
                continue
            value = unpack.unpack_from(buffer_data, offset)[0]
            values.append(float(_normalize_gltf_component(value, component_type)) if normalized else float(value))
        rows.append(tuple(values[:expected_components]))
    return rows


def _gltf_buffer_view(payload: _GltfPayload, view_index: int) -> dict[str, Any]:
    views = payload.document.get("bufferViews", []) or []
    if view_index < 0 or view_index >= len(views) or not isinstance(views[view_index], dict):
        raise ValueError(f"glTF bufferView index is invalid: {view_index}")
    return views[view_index]


def _read_gltf_buffer_view_bytes(payload: _GltfPayload, view_index: int) -> bytes:
    view = _gltf_buffer_view(payload, view_index)
    buffer_index = _safe_int(view.get("buffer"), -1)
    if buffer_index < 0 or buffer_index >= len(payload.buffers):
        raise ValueError(f"glTF image references missing buffer {buffer_index}.")
    offset = int(view.get("byteOffset", 0) or 0)
    length = int(view.get("byteLength", 0) or 0)
    return payload.buffers[buffer_index][offset : offset + length]


def _normalize_gltf_component(value: object, component_type: int) -> float:
    number = float(value)
    if component_type == 5120:
        return max(number / 127.0, -1.0)
    if component_type == 5121:
        return number / 255.0
    if component_type == 5122:
        return max(number / 32767.0, -1.0)
    if component_type == 5123:
        return number / 65535.0
    if component_type == 5125:
        return number / 4294967295.0
    return number


def _decode_data_uri(uri: str) -> bytes:
    _mime_type, data = _decode_data_uri_with_mime(uri)
    return data


def _decode_data_uri_with_mime(uri: str) -> tuple[str, bytes]:
    header, _sep, payload = uri.partition(",")
    mime_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    if ";base64" in header.lower():
        return mime_type, base64.b64decode(payload)
    return mime_type, unquote(payload).encode("utf-8")


def _resolve_scene_uri(base_dir: Path, uri: str) -> Path:
    parsed = urlparse(uri)
    raw_path = unquote(parsed.path if parsed.scheme == "file" else uri)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base_dir / raw_path
    return candidate.expanduser().resolve()


def _parse_collada_geometry(
    geometry: ET.Element,
    material_names: dict[str, str],
    prefix: str,
    ns: dict[str, str],
) -> _ColladaGeometry:
    geometry_id = geometry.attrib.get("id", "")
    geometry_name = geometry.attrib.get("name", "") or geometry_id
    mesh = geometry.find(f"{prefix}mesh", ns)
    if mesh is None:
        return _ColladaGeometry(geometry_id=geometry_id, name=geometry_name, primitives=[])

    sources = _collada_sources(mesh, prefix, ns)
    vertices_sources = _collada_vertices_sources(mesh, prefix, ns)
    primitives: list[SubMesh] = []
    for primitive in list(mesh.findall(f"{prefix}triangles", ns)) + list(mesh.findall(f"{prefix}polylist", ns)):
        material_symbol = primitive.attrib.get("material", "")
        material_name = material_names.get(material_symbol, material_symbol)
        submesh = _parse_collada_primitive(
            primitive,
            sources,
            vertices_sources,
            name=geometry_name,
            material=material_name,
            prefix=prefix,
            ns=ns,
        )
        if submesh.faces:
            primitives.append(submesh)
    return _ColladaGeometry(geometry_id=geometry_id, name=geometry_name, primitives=primitives)


def _collada_sources(mesh: ET.Element, prefix: str, ns: dict[str, str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for source in mesh.findall(f"{prefix}source", ns):
        source_id = source.attrib.get("id", "")
        array = source.find(f"{prefix}float_array", ns)
        accessor = source.find(f"{prefix}technique_common/{prefix}accessor", ns)
        if not source_id or array is None:
            continue
        values = _parse_float_list(array.text or "")
        stride = 3
        if accessor is not None:
            try:
                stride = max(1, int(accessor.attrib.get("stride", "3")))
            except ValueError:
                stride = 3
        result[source_id] = {"values": values, "stride": stride}
    return result


def _collada_vertices_sources(mesh: ET.Element, prefix: str, ns: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for vertices in mesh.findall(f"{prefix}vertices", ns):
        vertices_id = vertices.attrib.get("id", "")
        for input_element in vertices.findall(f"{prefix}input", ns):
            if input_element.attrib.get("semantic", "").upper() == "POSITION":
                result[vertices_id] = input_element.attrib.get("source", "").lstrip("#")
    return result


def _parse_collada_primitive(
    primitive: ET.Element,
    sources: dict[str, dict[str, object]],
    vertices_sources: dict[str, str],
    *,
    name: str,
    material: str,
    prefix: str,
    ns: dict[str, str],
) -> SubMesh:
    inputs = []
    for input_element in primitive.findall(f"{prefix}input", ns):
        try:
            offset = int(input_element.attrib.get("offset", "0"))
        except ValueError:
            offset = 0
        semantic = input_element.attrib.get("semantic", "").upper()
        source_id = input_element.attrib.get("source", "").lstrip("#")
        if semantic == "VERTEX":
            source_id = vertices_sources.get(source_id, source_id)
            semantic = "POSITION"
        inputs.append((offset, semantic, source_id))
    if not inputs:
        return SubMesh(name=name, material=material)
    index_stride = max(offset for offset, _semantic, _source in inputs) + 1
    p_element = primitive.find(f"{prefix}p", ns)
    if p_element is None or not (p_element.text or "").strip():
        return SubMesh(name=name, material=material)
    raw_indices = [int(value) for value in (p_element.text or "").split()]
    polygon_counts: list[int]
    vcount_element = primitive.find(f"{prefix}vcount", ns)
    if vcount_element is not None and (vcount_element.text or "").strip():
        polygon_counts = [int(value) for value in (vcount_element.text or "").split()]
    else:
        polygon_counts = [3] * (len(raw_indices) // (index_stride * 3))

    vertices: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    normals: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    corner_to_index: dict[tuple[str, int, str, int, str, int], int] = {}
    cursor = 0
    for polygon_size in polygon_counts:
        corners = []
        for _corner_index in range(polygon_size):
            chunk = raw_indices[cursor : cursor + index_stride]
            cursor += index_stride
            corners.append(_collada_corner_index(chunk, inputs))
        if len(corners) < 3:
            continue
        for tri_index in range(1, len(corners) - 1):
            face_indices = []
            for corner in (corners[0], corners[tri_index], corners[tri_index + 1]):
                local_index = corner_to_index.get(corner)
                if local_index is None:
                    position = _source_tuple(sources, corner[0], corner[1], 3)
                    uv = _source_tuple(sources, corner[2], corner[3], 2) if corner[3] >= 0 else (0.0, 0.0)
                    normal = _source_tuple(sources, corner[4], corner[5], 3) if corner[5] >= 0 else (0.0, 1.0, 0.0)
                    local_index = len(vertices)
                    corner_to_index[corner] = local_index
                    vertices.append(position)  # type: ignore[arg-type]
                    uvs.append((float(uv[0]), 1.0 - float(uv[1])))
                    normals.append(normal)  # type: ignore[arg-type]
                face_indices.append(local_index)
            if len(face_indices) == 3:
                faces.append((face_indices[0], face_indices[1], face_indices[2]))
    if not normals or len(normals) != len(vertices):
        normals = _compute_smooth_normals(vertices, faces)
    return SubMesh(
        name=name,
        material=material,
        texture="",
        vertices=vertices,
        uvs=uvs,
        normals=normals,
        faces=faces,
        vertex_count=len(vertices),
        face_count=len(faces),
    )


def _collada_corner_index(chunk: list[int], inputs: list[tuple[int, str, str]]) -> tuple[str, int, str, int, str, int]:
    position_index = -1
    uv_index = -1
    normal_index = -1
    position_source = ""
    uv_source = ""
    normal_source = ""
    for offset, semantic, _source_id in inputs:
        if offset >= len(chunk):
            continue
        if semantic == "POSITION":
            position_index = chunk[offset]
            position_source = _source_id
        elif semantic == "TEXCOORD" and uv_index < 0:
            uv_index = chunk[offset]
            uv_source = _source_id
        elif semantic == "NORMAL":
            normal_index = chunk[offset]
            normal_source = _source_id
    return position_source, position_index, uv_source, uv_index, normal_source, normal_index


def _source_tuple(
    sources: dict[str, dict[str, object]],
    source_id: str,
    index: int,
    expected: int,
) -> tuple[float, ...]:
    source = sources.get(source_id)
    if source is not None:
        stride = int(source.get("stride", expected) or expected)
        values = source.get("values", [])
        if isinstance(values, list):
            start = index * stride
            if 0 <= start and start + expected <= len(values):
                return tuple(float(values[start + item]) for item in range(expected))
    return (0.0, 0.0) if expected == 2 else (0.0, 0.0, 0.0)


def _collada_material_names(root: ET.Element, prefix: str, ns: dict[str, str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for material in root.findall(f".//{prefix}library_materials/{prefix}material", ns):
        material_id = material.attrib.get("id", "")
        material_name = material.attrib.get("name", "") or material_id
        if material_id:
            names[material_id] = material_name
        if material_name:
            names[material_name] = material_name
    return names


def _iter_collada_geometry_instances(
    root: ET.Element,
    prefix: str,
    ns: dict[str, str],
) -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    for node in root.findall(f".//{prefix}library_visual_scenes/{prefix}visual_scene//{prefix}node", ns):
        node_name = node.attrib.get("name", "") or node.attrib.get("id", "")
        matrix = _collada_node_matrix(node, prefix, ns)
        for instance_geometry in node.findall(f"{prefix}instance_geometry", ns):
            geometry_id = instance_geometry.attrib.get("url", "").lstrip("#")
            if not geometry_id:
                continue
            materials: dict[str, str] = {}
            for instance_material in instance_geometry.findall(f".//{prefix}instance_material", ns):
                symbol = instance_material.attrib.get("symbol", "")
                target = instance_material.attrib.get("target", "").lstrip("#")
                if symbol:
                    materials[symbol] = target or symbol
            instances.append(
                {
                    "geometry_id": geometry_id,
                    "node_name": node_name,
                    "matrix": matrix,
                    "materials": materials,
                }
            )
    return instances


def _collada_node_matrix(
    node: ET.Element,
    prefix: str,
    ns: dict[str, str],
) -> tuple[float, ...]:
    matrix_element = node.find(f"{prefix}matrix", ns)
    if matrix_element is not None:
        values = _parse_float_list(matrix_element.text or "")
        if len(values) >= 16:
            return tuple(values[:16])
    matrix = list(_identity_matrix())
    for translate in node.findall(f"{prefix}translate", ns):
        values = _parse_float_list(translate.text or "")
        if len(values) >= 3:
            matrix[3] += values[0]
            matrix[7] += values[1]
            matrix[11] += values[2]
    for scale in node.findall(f"{prefix}scale", ns):
        values = _parse_float_list(scale.text or "")
        if len(values) >= 3:
            matrix[0] *= values[0]
            matrix[5] *= values[1]
            matrix[10] *= values[2]
    return tuple(matrix)


def _copy_submesh_with_transform(
    submesh: SubMesh,
    matrix: tuple[float, ...],
) -> SubMesh:
    vertices = [_transform_point(vertex, matrix) for vertex in submesh.vertices]
    normals = [_normalize_vec(_transform_vector(normal, matrix)) for normal in submesh.normals]
    copied = SubMesh(
        name=submesh.name,
        material=submesh.material,
        texture=submesh.texture,
        vertices=vertices,
        uvs=list(submesh.uvs),
        normals=normals if len(normals) == len(vertices) else _compute_smooth_normals(vertices, submesh.faces),
        faces=list(submesh.faces),
        vertex_count=len(vertices),
        face_count=len(submesh.faces),
    )
    return copied


def _transform_point(
    vertex: tuple[float, float, float],
    matrix: tuple[float, ...],
) -> tuple[float, float, float]:
    x, y, z = vertex
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _transform_vector(
    vertex: tuple[float, float, float],
    matrix: tuple[float, ...],
) -> tuple[float, float, float]:
    x, y, z = vertex
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z,
        matrix[4] * x + matrix[5] * y + matrix[6] * z,
        matrix[8] * x + matrix[9] * y + matrix[10] * z,
    )


def _normalize_vec(value: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(value[0] * value[0] + value[1] * value[1] + value[2] * value[2])
    if length <= 1e-8:
        return (0.0, 1.0, 0.0)
    return (value[0] / length, value[1] / length, value[2] / length)


def _identity_matrix() -> tuple[float, ...]:
    return (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _multiply_matrix(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    values: list[float] = []
    for row in range(4):
        for column in range(4):
            values.append(
                left[row * 4 + 0] * right[0 * 4 + column]
                + left[row * 4 + 1] * right[1 * 4 + column]
                + left[row * 4 + 2] * right[2 * 4 + column]
                + left[row * 4 + 3] * right[3 * 4 + column]
            )
    return tuple(values)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return default


def _float_list(value: object, count: int, default: tuple[float, ...]) -> tuple[float, ...]:
    if isinstance(value, list) and len(value) >= count:
        try:
            return tuple(float(item) for item in value[:count])
        except Exception:
            return default
    return default


def _dedupe_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dedupe_paths(values: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for value in values:
        try:
            path = value.expanduser().resolve()
        except Exception:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _bbox(
    vertices: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not vertices:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xs, ys, zs = zip(*vertices)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _parse_float_list(text: str) -> list[float]:
    values: list[float] = []
    for raw_value in str(text or "").split():
        try:
            values.append(float(raw_value))
        except ValueError:
            continue
    return values


def _collada_image_paths(dae_path: Path) -> list[Path]:
    try:
        root = ET.parse(dae_path).getroot()
    except Exception:
        return []
    ns_uri = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    ns = {"c": ns_uri} if ns_uri else {}
    prefix = "c:" if ns_uri else ""
    paths: list[Path] = []
    for init_from in root.findall(f".//{prefix}library_images/{prefix}image/{prefix}init_from", ns):
        raw_text = str(init_from.text or "").strip()
        if not raw_text:
            continue
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = dae_path.parent / raw_text
        paths.append(candidate.expanduser().resolve())
    return paths


def _guess_scene_material_texture(scene_path: Path, material: str) -> str:
    material_key = str(material or "").strip().lower()
    if not material_key:
        return ""
    for root in (scene_path.parent, scene_path.parent / "textures", scene_path.parent.parent / "textures"):
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in SCENE_TEXTURE_SOURCE_EXTENSIONS:
                continue
            stem = candidate.stem.lower()
            if stem.startswith(material_key) and any(token in stem for token in ("albedo", "base", "diffuse", "color")):
                return candidate.resolve().as_posix()
    return ""
