from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from crimson_forge_toolkit.constants import (
    APP_REPOSITORY_URL,
    APP_TITLE,
    PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL,
)
from crimson_forge_toolkit.models import ModPackageInfo


_KNOWN_MOD_CONTENT_ROOTS = {
    "character",
    "effect",
    "gamedata",
    "leveldata",
    "meta",
    "object",
    "tree",
    "ui",
    "vehicle",
    "world",
}


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


def _compact_mapping(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }


def normalize_mod_package_payload_path(path_value: str | Path) -> PurePosixPath:
    normalized = str(path_value or "").replace("\\", "/").strip().strip("/")
    if not normalized:
        return PurePosixPath()
    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if not parts:
        return PurePosixPath()

    lowered_parts = [part.lower() for part in parts]
    if "files" in lowered_parts:
        files_index = lowered_parts.index("files")
        parts = parts[files_index + 1 :]
        lowered_parts = lowered_parts[files_index + 1 :]
    if parts and re.fullmatch(r"\d{4}", parts[0]):
        parts = parts[1:]
        lowered_parts = lowered_parts[1:]
    if len(parts) >= 2 and lowered_parts[0] == "gamedata" and lowered_parts[1] in _KNOWN_MOD_CONTENT_ROOTS:
        parts = parts[1:]
        lowered_parts = lowered_parts[1:]
    while len(parts) > 1 and lowered_parts and lowered_parts[0] not in _KNOWN_MOD_CONTENT_ROOTS:
        parts = parts[1:]
        lowered_parts = lowered_parts[1:]
    return PurePosixPath(*parts)


def is_mod_package_payload_path(path_value: str | Path) -> bool:
    normalized = normalize_mod_package_payload_path(path_value)
    if not normalized.parts:
        return False
    if any(part.startswith(".") for part in normalized.parts):
        return False
    if len(normalized.parts) == 1 and normalized.name.lower() in {"manifest.json", "mod.json", "info.json", "readme.txt"}:
        return False
    return True


def write_mod_package_readme(
    root: Path,
    package_info: ModPackageInfo,
    *,
    created_utc: str,
    overview: str,
    loose_file_count: int,
    asset_count: int | None = None,
    include_paired_lod: bool | None = None,
    create_no_encrypt_file: bool = True,
    manifest_label: str = "Structured package metadata",
) -> Path:
    def _add_blank_line(lines: list[str]) -> None:
        if lines and lines[-1] != "":
            lines.append("")

    def _add_section(lines: list[str], title: str) -> None:
        _add_blank_line(lines)
        lines.append(title)
        lines.append("=" * len(title))

    def _append_field(lines: list[str], label: str, value: str) -> None:
        lines.append(f"{label:<16}: {value}")

    title = (package_info.title or "").strip() or "Crimson Forge Toolkit Mod"
    version = (package_info.version or "").strip() or "1.0"
    author = (package_info.author or "").strip() or "-"
    description = (package_info.description or "").strip()

    lines: list[str] = [
        title,
        "=" * len(title),
        "",
    ]
    _append_field(lines, "Author", author)
    _append_field(lines, "Version", version)
    _append_field(lines, "Generated (UTC)", created_utc)
    _append_field(lines, "Generator", APP_TITLE)
    _append_field(lines, "Repository", APP_REPOSITORY_URL)

    if description:
        _add_section(lines, "Description")
        lines.append(description)

    _add_section(lines, "Overview")
    lines.append(overview)

    _add_section(lines, "Package Summary")
    _append_field(lines, "Loose file count", str(loose_file_count))
    if asset_count is not None:
        _append_field(lines, "Asset count", str(asset_count))
    if include_paired_lod is not None:
        _append_field(lines, "Paired LOD", "Yes" if include_paired_lod else "No")

    _add_section(lines, "Included Package Files")
    lines.append(f"manifest.json  {manifest_label}")
    if create_no_encrypt_file:
        lines.append(".no_encrypt    Marks the package for non-encrypted handling")

    _add_section(lines, "Installation")
    lines.append("1. Copy or import the contents of the folder into your Crimson Desert mod manager.")
    lines.append("2. Deploy or enable the mod through your preferred mod manager.")
    lines.append("3. Verify that the updated mesh loads correctly in game.")
    lines.append("")
    lines.append("Preferred mod manager:")
    lines.append(PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL)

    _add_section(lines, "Notes")
    lines.append("This package was generated automatically by Crimson Forge Toolkit.")
    lines.append("Use manifest.json for structured metadata and validation.")

    readme_path = root / "README.txt"
    readme_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return readme_path


def write_mod_package_manifest(
    root: Path,
    package_info: ModPackageInfo,
    *,
    kind: str = "loose_mod",
    extra_fields: dict[str, object] | None = None,
    create_no_encrypt_file: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    no_encrypt_path = root / ".no_encrypt"
    if create_no_encrypt_file:
        no_encrypt_path.touch()
    elif no_encrypt_path.exists():
        no_encrypt_path.unlink()

    created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = _compact_mapping(
        {
            "format": "v1",
            "schema_version": 1,
            "kind": kind,
            "game": "Crimson Desert",
            "title": (package_info.title or "").strip() or "Crimson Forge Toolkit Mod",
            "author": (package_info.author or "").strip(),
            "version": (package_info.version or "").strip() or "1.0",
            "description": (package_info.description or "").strip(),
            "nexus_url": (package_info.nexus_url or "").strip(),
            "created_utc": created_utc,
        }
    )
    if extra_fields:
        payload.update(_compact_mapping(dict(extra_fields)))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_mod_package_readme(
        root,
        package_info,
        created_utc=created_utc,
        overview="This package contains loose file replacements generated by Crimson Forge Toolkit.",
        loose_file_count=int(payload.get("file_count", 0) or 0),
        create_no_encrypt_file=create_no_encrypt_file,
        manifest_label="Structured package metadata",
    )
    return manifest_path


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
    created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    package_title = (package_info.title or "").strip() or "Crimson Forge Toolkit Mesh Mod"
    normalized_game_build = (game_build or "").strip()
    root.mkdir(parents=True, exist_ok=True)
    no_encrypt_path = root / ".no_encrypt"
    if create_no_encrypt_file:
        no_encrypt_path.touch()
    elif no_encrypt_path.exists():
        no_encrypt_path.unlink()

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
            "nexus_url": (package_info.nexus_url or "").strip(),
            "created_utc": created_utc,
            "game_build": normalized_game_build,
            "include_paired_lod": bool(include_paired_lod),
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
    readme_path = write_mod_package_readme(
        root,
        package_info,
        created_utc=created_utc,
        overview="This package contains loose mesh replacement files generated from an OBJ import workflow.",
        loose_file_count=len(files),
        asset_count=len(assets),
        include_paired_lod=bool(include_paired_lod),
        create_no_encrypt_file=create_no_encrypt_file,
        manifest_label="Structured mesh package metadata",
    )
    return [manifest_path, readme_path]
