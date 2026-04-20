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
            "generator": generator_name,
            "generator_url": APP_REPOSITORY_URL,
            "game_build": (game_build or "").strip(),
            "include_paired_lod": bool(include_paired_lod),
            "asset_count": len(assets),
            "file_count": len(files),
            "files_root": "files",
            "assets": [_compact_value(dataclasses.asdict(asset)) for asset in assets],
            "files": [_compact_value(dataclasses.asdict(file_info)) for file_info in files],
        }
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

    title_banner = "*" * max(1, len(package_title))
    section_banner = "*" * 6
    readme_lines = [
        title_banner,
        package_title,
        title_banner,
        f"Author: {(package_info.author or '').strip() or '-'}",
        f"Version: {(package_info.version or '').strip() or '1.0'}",
        f"Generated: {created_utc}",
    ]
    if game_build.strip():
        readme_lines.append(f"Game Build: {game_build.strip()}")
    readme_lines.extend(
        [
            f"Loose Files: {len(files)}",
            section_banner,
            "CONTENTS",
            "  - files/<entry path> rebuilt mesh payloads",
            "  - manifest.json mesh package metadata",
            "  - info.json Crimson Forge Toolkit package metadata",
            section_banner,
            "FILES (MESH)",
        ]
    )
    for file_info in files:
        readme_lines.append(f"  - {file_info.path}")
    readme_lines.extend(
        [
            section_banner,
            "INSTALL",
            "  Put the contents of the files/ folder into your Crimson Desert mod manager, preferred one:",
            PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL,
            "",
            "",
            "MESH FILES",
        ]
    )
    for file_info in files:
        readme_lines.append(f"  - {file_info.path}")
    readme_lines.extend(["", f"Generated by {APP_TITLE}", APP_REPOSITORY_URL])
    readme_path = root / "README.txt"
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    return [manifest_path, readme_path, root / "info.json"]
