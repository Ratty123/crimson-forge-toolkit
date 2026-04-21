from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from crimson_forge_toolkit.constants import (
    APP_REPOSITORY_URL,
    APP_TITLE,
    PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL,
)
from crimson_forge_toolkit.models import ModPackageInfo


@dataclasses.dataclass(slots=True)
class MeshLooseModAsset:
    entry_path: str
    package_group: str
    format: str
    obj_path: str = ""
    vertices: int = 0
    faces: int = 0
    submeshes: int = 0
    generated_from: str = ""
    note: str = ""


@dataclasses.dataclass(slots=True)
class MeshLooseModFile:
    path: str
    package_group: str
    format: str
    generated_from: str = ""
    note: str = ""


def sanitize_mod_package_folder_name(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\\\|?*]+', "_", name).strip(" .")
    return sanitized or "Crimson Forge Toolkit Mod"


def resolve_mod_package_root(parent_root: Path, package_info: ModPackageInfo) -> Path:
    package_title = (package_info.title or "").strip() or "Crimson Forge Toolkit Mod"
    return parent_root / sanitize_mod_package_folder_name(package_title)


def write_mod_package_info(
    root: Path,
    package_info: ModPackageInfo,
    *,
    create_no_encrypt_file: bool = True,
) -> None:
    def _compact_mapping(payload: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in payload.items()
            if value not in ("", None, [], {})
        }

    root.mkdir(parents=True, exist_ok=True)
    no_encrypt_path = root / ".no_encrypt"
    if create_no_encrypt_file:
        no_encrypt_path.touch()
    elif no_encrypt_path.exists():
        no_encrypt_path.unlink()

    payload = {"modinfo": _compact_mapping(dataclasses.asdict(package_info))}
    (root / "info.json").write_text(json.dumps(payload, indent=4), encoding="utf-8")


def write_mesh_loose_mod_package_metadata(
    root: Path,
    package_info: ModPackageInfo,
    *,
    assets: Sequence[MeshLooseModAsset],
    files: Sequence[MeshLooseModFile],
    include_paired_lod: bool,
    create_no_encrypt_file: bool = True,
    game_build: str = "",
) -> list[Path]:
    write_mod_package_info(root, package_info, create_no_encrypt_file=create_no_encrypt_file)

    created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    package_title = (package_info.title or "").strip() or "Crimson Forge Toolkit Mesh Mod"
    generator_name = APP_TITLE
    author = (package_info.author or "").strip() or "-"
    version = (package_info.version or "").strip() or "1.0"
    description = (package_info.description or "").strip()
    normalized_game_build = (game_build or "").strip()

    def _compact_value(value: object) -> object:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                compacted = _compact_value(item)
                if compacted in ("", None, [], {}):
                    continue
                result[key] = compacted
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                compacted = _compact_value(item)
                if compacted in ("", None, [], {}):
                    continue
                result.append(compacted)
            return result
        return value

    manifest_payload = _compact_value(
        {
            "format": "v1",
            "schema_version": 1,
            "kind": "mesh_loose_mod",
            "game": "Crimson Desert",
            "title": package_title,
            "author": (package_info.author or "").strip(),
            "version": (package_info.version or "").strip() or "1.0",
            "description": (package_info.description or "").strip(),
            "created_utc": created_utc,
            "game_build": normalized_game_build,
            "include_paired_lod": bool(include_paired_lod),
            "files_root": "files",
            "asset_count": len(assets),
            "file_count": len(files),
            "assets": [
                _compact_value(
                    {
                        "entry_path": asset.entry_path,
                        "package_group": asset.package_group,
                        "format": asset.format,
                        "obj_path": asset.obj_path,
                        "vertices": asset.vertices,
                        "faces": asset.faces,
                        "submeshes": asset.submeshes,
                        "note": asset.note,
                    }
                )
                for asset in assets
            ],
            "files": [
                _compact_value(
                    {
                        "path": file_info.path,
                        "package_group": file_info.package_group,
                        "format": file_info.format,
                        "note": file_info.note,
                    }
                )
                for file_info in files
            ],
        }
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    def _add_blank_line(lines: list[str]) -> None:
        if lines and lines[-1] != "":
            lines.append("")

    def _add_section(lines: list[str], title: str) -> None:
        _add_blank_line(lines)
        lines.append(title)
        lines.append("=" * len(title))

    def _append_field(lines: list[str], label: str, value: str) -> None:
        lines.append(f"{label:<16}: {value}")

    readme_lines: list[str] = [
        package_title,
        "=" * len(package_title),
        "",
    ]

    _append_field(readme_lines, "Author", author)
    _append_field(readme_lines, "Version", version)
    _append_field(readme_lines, "Generated (UTC)", created_utc)
    if normalized_game_build:
        _append_field(readme_lines, "Game Build", normalized_game_build)
    _append_field(readme_lines, "Generator", APP_TITLE)
    _append_field(readme_lines, "Repository", APP_REPOSITORY_URL)

    if description:
        _add_section(readme_lines, "Description")
        readme_lines.append(description)

    _add_section(readme_lines, "Overview")
    readme_lines.append(
        "This package contains loose mesh replacement files generated from an OBJ import workflow."
    )

    _add_section(readme_lines, "Package Summary")
    _append_field(readme_lines, "Loose file count", str(len(files)))
    _append_field(readme_lines, "Asset count", str(len(assets)))
    _append_field(readme_lines, "Paired LOD", "Yes" if include_paired_lod else "No")
    _append_field(readme_lines, "Files root", "files/")

    _add_section(readme_lines, "Included Package Files")
    readme_lines.append("files/         Rebuilt loose mesh payloads")
    readme_lines.append("manifest.json  Structured mesh package metadata")
    readme_lines.append("info.json      Package metadata")
    if create_no_encrypt_file:
        readme_lines.append(".no_encrypt    Marks the package for non-encrypted handling")

    _add_section(readme_lines, "Installation")
    readme_lines.append("1. Copy or import the contents of the files/ folder into your Crimson Desert mod manager.")
    readme_lines.append("2. Deploy or enable the mod through your preferred mod manager.")
    readme_lines.append("3. Verify that the updated mesh loads correctly in game.")
    readme_lines.append("")
    readme_lines.append("Preferred mod manager:")
    readme_lines.append(PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL)

    _add_section(readme_lines, "Loose Mesh Files")
    if files:
        for file_info in files:
            readme_lines.append(f"- {file_info.path}")
            readme_lines.append(f"  Package group: {file_info.package_group}")
            readme_lines.append(f"  Format: {file_info.format}")
            if file_info.generated_from:
                readme_lines.append(f"  Generated from: {file_info.generated_from}")
            if file_info.note:
                readme_lines.append(f"  Note: {file_info.note}")
    else:
        readme_lines.append("No loose mesh files were recorded.")

    if assets:
        _add_section(readme_lines, "Source Assets")
        for asset in assets:
            readme_lines.append(f"- {asset.entry_path}")
            readme_lines.append(f"  Package group: {asset.package_group}")
            readme_lines.append(f"  Format: {asset.format}")
            if asset.obj_path:
                readme_lines.append(f"  Source OBJ: {asset.obj_path}")
            if asset.generated_from:
                readme_lines.append(f"  Generated from: {asset.generated_from}")
            if asset.vertices:
                readme_lines.append(f"  Vertices: {asset.vertices:,}")
            if asset.faces:
                readme_lines.append(f"  Faces: {asset.faces:,}")
            if asset.submeshes:
                readme_lines.append(f"  Submeshes: {asset.submeshes:,}")
            if asset.note:
                readme_lines.append(f"  Note: {asset.note}")

    _add_section(readme_lines, "Notes")
    readme_lines.append("This package was generated automatically by Crimson Forge Toolkit.")
    readme_lines.append("Use manifest.json for structured metadata and validation.")
    if include_paired_lod:
        readme_lines.append("A paired LOD file was included as part of the mesh workflow.")

    readme_path = root / "README.txt"
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    return [manifest_path, readme_path, root / "info.json"]
