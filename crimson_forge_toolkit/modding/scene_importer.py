"""Scene-file import helpers for static mesh replacement.

OBJ remains the strict round-trip format.  This module accepts broader scene
formats only for static replacement and normalizes them into ParsedMesh.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logging import get_logger
from .mesh_importer import import_obj
from .mesh_parser import ParsedMesh, SubMesh, _compute_smooth_normals

logger = get_logger("core.scene_importer")

SCENE_IMPORT_EXTENSIONS = {".obj", ".dae"}
SCENE_TEXTURE_SOURCE_EXTENSIONS = {".png", ".dds", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}


@dataclass(slots=True)
class _ColladaGeometry:
    geometry_id: str
    name: str
    primitives: list[SubMesh]


def import_scene_mesh(path: str | Path) -> ParsedMesh:
    source_path = Path(path).expanduser().resolve()
    suffix = source_path.suffix.lower()
    if suffix == ".obj":
        return import_obj(str(source_path))
    if suffix == ".dae":
        return import_dae(source_path)
    raise ValueError(f"Unsupported mesh import format: {source_path.suffix or source_path.name}")


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
    material_names = {
        str(submesh.material or submesh.name or "").strip().lower()
        for submesh in (mesh.submeshes if mesh is not None else [])
        if str(submesh.material or submesh.name or "").strip()
    }
    search_roots = [scene_path.parent, scene_path.parent / "textures", scene_path.parent.parent / "textures"]
    for root in search_roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in SCENE_TEXTURE_SOURCE_EXTENSIONS:
                continue
            stem = candidate.stem.lower()
            if any(stem.startswith(material_name) for material_name in material_names):
                discovered.append(candidate)
    unique: dict[str, Path] = {}
    for candidate in discovered:
        if candidate.is_file():
            unique.setdefault(str(candidate.resolve()).lower(), candidate.resolve())
    return tuple(unique.values())


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

