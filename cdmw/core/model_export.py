from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cdmw.models import ModelPreviewData, ModelPreviewMesh

_INVALID_EXPORT_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def export_model_preview_to_obj(model_preview: ModelPreviewData, output_obj_path: Path) -> Path:
    resolved_output = output_obj_path.expanduser().resolve()
    if resolved_output.suffix.lower() != ".obj":
        resolved_output = resolved_output.with_suffix(".obj")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)

    mtl_path = resolved_output.with_suffix(".mtl")
    texture_dir = resolved_output.parent / f"{resolved_output.stem}_textures"

    obj_lines: List[str] = [
        "# Exported by Crimson Desert Mod Workbench",
        f"mtllib {mtl_path.name}",
    ]
    mtl_lines: List[str] = ["# Exported by Crimson Desert Mod Workbench"]

    vertex_offset = 1
    texcoord_offset = 1
    normal_offset = 1
    exported_textures: Dict[str, str] = {}

    for mesh_index, mesh in enumerate(model_preview.meshes, start=1):
        positions = list(mesh.positions or [])
        indices = list(mesh.indices or [])
        if not positions or not indices:
            continue

        object_name = _sanitize_export_name(
            mesh.material_name or mesh.texture_name or f"mesh_{mesh_index:03d}",
            fallback=f"mesh_{mesh_index:03d}",
        )
        material_name = f"{object_name}_{mesh_index:03d}"
        obj_lines.append(f"o {object_name}")
        obj_lines.append(f"g {object_name}")

        for x, y, z in _restore_model_positions(model_preview, positions):
            obj_lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")

        has_texcoords = len(mesh.texture_coordinates) == len(positions)
        if has_texcoords:
            for u, v in mesh.texture_coordinates:
                obj_lines.append(f"vt {u:.6f} {v:.6f}")

        has_normals = len(mesh.normals) == len(positions)
        if has_normals:
            for nx, ny, nz in mesh.normals:
                obj_lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")

        obj_lines.append(f"usemtl {material_name}")
        for triangle_index in range(0, len(indices) - 2, 3):
            a = indices[triangle_index]
            b = indices[triangle_index + 1]
            c = indices[triangle_index + 2]
            if (
                a < 0
                or b < 0
                or c < 0
                or a >= len(positions)
                or b >= len(positions)
                or c >= len(positions)
            ):
                continue
            obj_lines.append(
                "f "
                + " ".join(
                    _format_obj_face_vertex(
                        vertex_offset + vertex_index,
                        texcoord_offset + vertex_index if has_texcoords else None,
                        normal_offset + vertex_index if has_normals else None,
                    )
                    for vertex_index in (a, b, c)
                )
            )

        exported_texture_name = _export_mesh_texture(mesh, texture_dir, exported_textures)
        mtl_lines.extend(
            _build_material_block(
                material_name,
                mesh,
                exported_texture_name,
            )
        )

        vertex_offset += len(positions)
        if has_texcoords:
            texcoord_offset += len(positions)
        if has_normals:
            normal_offset += len(positions)

    resolved_output.write_text("\n".join(obj_lines) + "\n", encoding="utf-8")
    mtl_path.write_text("\n".join(mtl_lines) + "\n", encoding="utf-8")
    return resolved_output


def _restore_model_positions(
    model_preview: ModelPreviewData,
    positions: List[Tuple[float, float, float]],
) -> List[Tuple[float, float, float]]:
    center_x, center_y, center_z = tuple(model_preview.normalization_center or (0.0, 0.0, 0.0))
    scale = float(model_preview.normalization_scale or 1.0)
    if abs(scale) <= 1e-9:
        scale = 1.0
    return [
        (
            (x / scale) + center_x,
            (y / scale) + center_y,
            (z / scale) + center_z,
        )
        for x, y, z in positions
    ]


def _format_obj_face_vertex(
    vertex_index: int,
    texcoord_index: Optional[int],
    normal_index: Optional[int],
) -> str:
    if texcoord_index is not None and normal_index is not None:
        return f"{vertex_index}/{texcoord_index}/{normal_index}"
    if texcoord_index is not None:
        return f"{vertex_index}/{texcoord_index}"
    if normal_index is not None:
        return f"{vertex_index}//{normal_index}"
    return str(vertex_index)


def _build_material_block(
    material_name: str,
    mesh: ModelPreviewMesh,
    exported_texture_name: str,
) -> List[str]:
    lines = [
        "",
        f"newmtl {material_name}",
        "Kd 1.000000 1.000000 1.000000",
        "Ka 0.000000 0.000000 0.000000",
        "Ks 0.000000 0.000000 0.000000",
        "d 1.0",
        "illum 2",
    ]
    if mesh.texture_name:
        lines.append(f"# Source texture: {mesh.texture_name}")
    if mesh.material_name:
        lines.append(f"# Source material: {mesh.material_name}")
    if exported_texture_name:
        lines.append(f"map_Kd {exported_texture_name}")
    return lines


def _export_mesh_texture(
    mesh: ModelPreviewMesh,
    texture_dir: Path,
    exported_textures: Dict[str, str],
) -> str:
    texture_source_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
    texture_image = getattr(mesh, "preview_texture_image", None)

    if not texture_source_path and texture_image is None:
        return ""

    texture_dir.mkdir(parents=True, exist_ok=True)
    cache_key = texture_source_path or f"in_memory::{mesh.texture_name or mesh.material_name or 'texture'}"
    existing_name = exported_textures.get(cache_key)
    if existing_name:
        return existing_name

    preferred_stem = _sanitize_export_name(
        Path(mesh.texture_name or mesh.material_name or "texture").stem,
        fallback="texture",
    )
    output_name = f"{preferred_stem}.png"
    output_path = texture_dir / output_name
    suffix = 1
    while output_path.exists() and cache_key not in exported_textures:
        output_name = f"{preferred_stem}_{suffix:02d}.png"
        output_path = texture_dir / output_name
        suffix += 1

    if texture_source_path:
        source_path = Path(texture_source_path)
        if source_path.is_file():
            shutil.copy2(source_path, output_path)
            exported_textures[cache_key] = f"{texture_dir.name}/{output_name}"
            return exported_textures[cache_key]

    if texture_image is not None and hasattr(texture_image, "save"):
        if bool(texture_image.save(str(output_path), "PNG")):
            exported_textures[cache_key] = f"{texture_dir.name}/{output_name}"
            return exported_textures[cache_key]

    return ""


def _sanitize_export_name(value: str, *, fallback: str) -> str:
    cleaned = _INVALID_EXPORT_NAME_RE.sub("_", (value or "").strip()).strip("._")
    return cleaned or fallback
