from __future__ import annotations

import dataclasses
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from cdmw.constants import (
    APP_REPOSITORY_URL,
    APP_TITLE,
    PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL,
)
from cdmw.models import ModPackageInfo


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
    is_new: bool = False
    generated_from: str = ""
    note: str = ""


@dataclasses.dataclass(slots=True)
class ModPackageExportOptions:
    manager_targets: tuple[str, ...] = ("json_mod_manager", "cdumm", "dmm", "crimson_sharp")
    structure: str = "game_relative"
    create_manifest_json: bool = True
    create_mod_json: bool = True
    create_modinfo_json: bool = True
    create_info_json: bool = True
    create_no_encrypt_file: bool = True
    create_zip: bool = False
    conflict_mode: str = ""
    target_language: str = ""
    files_dir: str = "files"


@dataclasses.dataclass(slots=True)
class ModPackageFinalizeResult:
    metadata_files: list[Path]
    zip_path: Path | None = None
    payload_root: Path | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True, slots=True)
class ModPackageMetadataArtifactInfo:
    key: str
    filename: str
    label: str
    description: str
    primary: bool = False


_MOD_MANAGER_PROFILE_LABELS = {
    "json_mod_manager": "JSON Mod Manager",
    "cdumm": "CDUMM",
    "dmm": "Definitive Mod Manager",
    "crimson_sharp": "Crimson Sharp",
}

MOD_PACKAGE_METADATA_ARTIFACTS: tuple[ModPackageMetadataArtifactInfo, ...] = (
    ModPackageMetadataArtifactInfo(
        key="manifest_json",
        filename="manifest.json",
        label="manifest.json",
        description=(
            "Primary Crimson Desert Mod Workbench manifest. It records the package kind, metadata, "
            "selected layout, manager targets, files directory, and new_paths declarations."
        ),
        primary=True,
    ),
    ModPackageMetadataArtifactInfo(
        key="mod_json",
        filename="mod.json",
        label="mod.json",
        description="Compatibility metadata for mod managers that look for a mod.json descriptor.",
    ),
    ModPackageMetadataArtifactInfo(
        key="modinfo_json",
        filename="modinfo.json",
        label="modinfo.json",
        description=(
            "Compatibility metadata for managers such as CDUMM. It includes normal mod info and, "
            "when applicable, conflict mode and target language."
        ),
    ),
    ModPackageMetadataArtifactInfo(
        key="info_json",
        filename="info.json",
        label="info.json",
        description="Compatibility copy of the structured package metadata for managers that look for info.json.",
    ),
    ModPackageMetadataArtifactInfo(
        key="no_encrypt",
        filename=".no_encrypt",
        label=".no_encrypt",
        description="Marker file used by some loose-file workflows to request non-encrypted handling.",
    ),
    ModPackageMetadataArtifactInfo(
        key="ready_zip",
        filename="Ready .zip",
        label="Ready .zip",
        description="Writes a zip beside the package folder containing the same generated package contents.",
    ),
)
MOD_PACKAGE_METADATA_ARTIFACTS_BY_KEY = {info.key: info for info in MOD_PACKAGE_METADATA_ARTIFACTS}
MOD_PACKAGE_METADATA_ARTIFACTS_BY_FILENAME = {
    info.filename: info for info in MOD_PACKAGE_METADATA_ARTIFACTS if info.filename not in {"Ready .zip"}
}


def mod_package_profile_uses_manager_metadata(profile: str) -> bool:
    normalized = str(profile or "universal").strip().lower()
    return normalized in {"universal", "cdumm", "ultimate", "ultimate_mods_manager"}


def mod_package_export_options_for_manager(profile: str) -> ModPackageExportOptions:
    normalized = str(profile or "universal").strip().lower()
    if normalized in {"dmm", "definitive", "definitive_mod_manager"}:
        return ModPackageExportOptions(manager_targets=("dmm",), structure="files_wrapper")
    if normalized in {"cdumm", "ultimate", "ultimate_mods_manager"}:
        return ModPackageExportOptions(manager_targets=("cdumm",), structure="files_wrapper")
    if normalized in {"json", "json_mod_manager", "jmm"}:
        return ModPackageExportOptions(manager_targets=("json_mod_manager",), structure="game_relative")
    if normalized in {"crimson_sharp", "sharp", "crimson_browser"}:
        return ModPackageExportOptions(manager_targets=("crimson_sharp",), structure="files_wrapper")
    return ModPackageExportOptions()


def sanitize_mod_package_folder_name(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\\\|?*]+', "_", name).strip(" .")
    return sanitized or "Crimson Desert Mod Workbench Mod"


def resolve_mod_package_root(parent_root: Path, package_info: ModPackageInfo) -> Path:
    package_title = (package_info.title or "").strip() or "Crimson Desert Mod Workbench Mod"
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
    if len(normalized.parts) == 1 and normalized.name.lower() in {"manifest.json", "mod.json", "modinfo.json", "info.json", "readme.txt"}:
        return False
    return True


def _payload_path_text(path_value: str | Path) -> str:
    normalized = normalize_mod_package_payload_path(path_value)
    if not normalized.parts:
        return ""
    if any(part.startswith(".") for part in normalized.parts):
        return ""
    return normalized.as_posix().strip("/")


def _compact_nested_value(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            compacted = _compact_nested_value(item)
            if compacted in ("", None, [], {}):
                continue
            result[str(key)] = compacted
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            compacted = _compact_nested_value(item)
            if compacted in ("", None, [], {}):
                continue
            result.append(compacted)
        return result
    return value


def _is_same_or_child_payload_path(candidate: str, prefix: str) -> bool:
    candidate = candidate.strip("/")
    prefix = prefix.strip("/")
    return bool(candidate and prefix and (candidate == prefix or candidate.startswith(f"{prefix}/")))


def normalize_mod_package_new_path_prefixes(
    new_file_paths: Sequence[str | Path],
    *,
    all_payload_paths: Sequence[str | Path] | None = None,
) -> list[str]:
    new_paths: list[str] = []
    seen_new: set[str] = set()
    for path_value in new_file_paths:
        path_text = _payload_path_text(path_value)
        if not path_text or path_text in seen_new:
            continue
        seen_new.add(path_text)
        new_paths.append(path_text)

    if not new_paths:
        return []

    all_paths: list[str] = []
    seen_all: set[str] = set()
    for path_value in all_payload_paths or new_paths:
        path_text = _payload_path_text(path_value)
        if not path_text or path_text in seen_all:
            continue
        seen_all.add(path_text)
        all_paths.append(path_text)
    if not all_paths:
        all_paths = list(new_paths)
        seen_all = set(new_paths)

    new_path_set = set(new_paths)
    prefixes: list[str] = []

    def _append_prefix(prefix: str) -> None:
        normalized_prefix = prefix.strip("/")
        if not normalized_prefix:
            return
        for existing in prefixes:
            if _is_same_or_child_payload_path(normalized_prefix, existing):
                return
        prefixes[:] = [
            existing
            for existing in prefixes
            if not _is_same_or_child_payload_path(existing, normalized_prefix)
        ]
        prefixes.append(normalized_prefix)

    for new_path in new_paths:
        parts = PurePosixPath(new_path).parts
        selected_prefix = new_path
        for length in range(2, len(parts)):
            candidate_prefix = PurePosixPath(*parts[:length]).as_posix()
            covered_paths = [
                payload_path
                for payload_path in all_paths
                if _is_same_or_child_payload_path(payload_path, candidate_prefix)
            ]
            if len(covered_paths) > 1 and all(payload_path in new_path_set for payload_path in covered_paths):
                selected_prefix = candidate_prefix
                break
        _append_prefix(selected_prefix)

    return prefixes


def _normalize_manager_targets(values: Sequence[str]) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        targets.append(normalized)
    return targets


def _safe_files_dir(value: str) -> str:
    normalized = str(value or "files").replace("\\", "/").strip().strip("/")
    if not normalized or normalized.startswith(".") or "/" in normalized:
        return "files"
    return normalized


def _common_mod_package_fields(
    package_info: ModPackageInfo,
    *,
    files_dir_value: str,
    manager_targets: Sequence[str],
    new_path_prefixes: Sequence[str],
) -> dict[str, object]:
    title = (package_info.title or "").strip() or "Crimson Desert Mod Workbench Mod"
    return _compact_nested_value(
        {
            "name": title,
            "title": title,
            "game": "Crimson Desert",
            "version": (package_info.version or "").strip() or "1.0",
            "author": (package_info.author or "").strip(),
            "description": (package_info.description or "").strip(),
            "nexus_url": (package_info.nexus_url or "").strip(),
            "generator": APP_TITLE,
            "files_dir": files_dir_value,
            "manager_targets": list(manager_targets),
            "manager_target_labels": [_MOD_MANAGER_PROFILE_LABELS.get(target, target) for target in manager_targets],
            "new_paths": list(new_path_prefixes),
        }
    )  # type: ignore[return-value]


def _modinfo_payload(
    package_info: ModPackageInfo,
    options: ModPackageExportOptions,
    *,
    files_dir_value: str,
    manager_targets: Sequence[str],
    new_path_prefixes: Sequence[str],
) -> dict[str, object]:
    payload = dict(
        _common_mod_package_fields(
            package_info,
            files_dir_value=files_dir_value,
            manager_targets=manager_targets,
            new_path_prefixes=new_path_prefixes,
        )
    )
    if "cdumm" in set(manager_targets):
        payload["conflict_mode"] = (options.conflict_mode or "").strip()
        payload["target_language"] = (options.target_language or "").strip()
    return _compact_nested_value(payload)  # type: ignore[return-value]


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _payload_paths_under_root(root: Path, payload_paths: Sequence[str | Path]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    resolved_root = root.expanduser().resolve()
    for value in payload_paths:
        path_text = _payload_path_text(value)
        if not path_text:
            continue
        path = root.joinpath(*PurePosixPath(path_text).parts)
        try:
            resolved_path = path.expanduser().resolve()
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        key = str(resolved_path).lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _discover_payload_paths_under_root(root: Path) -> list[str]:
    ignored_names = {
        ".no_encrypt",
        "README.txt",
        "info.json",
        "manifest.json",
        "mod.json",
        "modinfo.json",
    }
    paths: list[str] = []
    if not root.exists():
        return paths
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in ignored_names:
            continue
        try:
            relative_path = path.relative_to(root)
        except ValueError:
            continue
        paths.append(relative_path.as_posix())
    return paths


def _move_payloads_to_files_dir(root: Path, payload_paths: Sequence[str | Path], files_dir_name: str) -> list[str]:
    moved: list[str] = []
    files_root = root / files_dir_name
    for source_path in _payload_paths_under_root(root, payload_paths):
        if not source_path.is_file():
            continue
        rel_text = _payload_path_text(source_path.relative_to(root))
        if not rel_text or rel_text.startswith(f"{files_dir_name}/"):
            continue
        target_path = files_root.joinpath(*PurePosixPath(rel_text).parts)
        if source_path.resolve() == target_path.resolve():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        shutil.move(str(source_path), str(target_path))
        moved.append(rel_text)
    return moved


def _write_package_zip(root: Path) -> Path:
    zip_path = root.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, path.relative_to(root).as_posix())
    return zip_path


def _metadata_package_file_lines(paths: Sequence[Path]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for path in paths:
        name = path.name
        if name in seen:
            continue
        seen.add(name)
        artifact = MOD_PACKAGE_METADATA_ARTIFACTS_BY_FILENAME.get(name)
        if artifact is None:
            continue
        lines.append(f"{name:<14} {artifact.description}")
    return lines


def finalize_mod_package_export(
    root: Path,
    package_info: ModPackageInfo,
    *,
    kind: str = "loose_mod",
    payload_paths: Sequence[str | Path] = (),
    new_file_paths: Sequence[str | Path] = (),
    extra_fields: dict[str, object] | None = None,
    options: ModPackageExportOptions | None = None,
    created_utc: str | None = None,
) -> ModPackageFinalizeResult:
    root.mkdir(parents=True, exist_ok=True)
    resolved_options = options or ModPackageExportOptions()
    files_dir_name = _safe_files_dir(resolved_options.files_dir)
    normalized_structure = str(resolved_options.structure or "game_relative").strip().lower()
    files_dir_value = files_dir_name if normalized_structure == "files_wrapper" else "."
    payload_root = root / files_dir_name if normalized_structure == "files_wrapper" else root

    if normalized_structure == "files_wrapper" and payload_paths:
        _move_payloads_to_files_dir(root, payload_paths, files_dir_name)

    new_path_prefixes = normalize_mod_package_new_path_prefixes(
        new_file_paths,
        all_payload_paths=payload_paths,
    )
    manager_targets = _normalize_manager_targets(resolved_options.manager_targets)
    created = created_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    modinfo = _modinfo_payload(
        package_info,
        resolved_options,
        files_dir_value=files_dir_value,
        manager_targets=manager_targets,
        new_path_prefixes=new_path_prefixes,
    )
    metadata_files: list[Path] = []
    common_fields = _common_mod_package_fields(
        package_info,
        files_dir_value=files_dir_value,
        manager_targets=manager_targets,
        new_path_prefixes=new_path_prefixes,
    )

    manifest_payload = _compact_nested_value(
        {
            "format": "crimson_browser_mod_v1" if normalized_structure == "files_wrapper" else "v1",
            "schema_version": 1,
            "kind": kind,
            "id": sanitize_mod_package_folder_name(str(modinfo.get("name") or root.name)),
            **common_fields,
            "created_utc": created,
            **dict(extra_fields or {}),
        }
    )

    if resolved_options.create_no_encrypt_file:
        no_encrypt_path = root / ".no_encrypt"
        no_encrypt_path.touch()
        metadata_files.append(no_encrypt_path)
    else:
        no_encrypt_path = root / ".no_encrypt"
        if no_encrypt_path.exists():
            no_encrypt_path.unlink()

    if resolved_options.create_manifest_json:
        metadata_files.append(_write_json(root / "manifest.json", manifest_payload))

    mod_json_payload = _compact_nested_value(
        {
            "format": "crimson_desert_mod",
            "schema_version": 1,
            **common_fields,
            "modinfo": modinfo,
        }
    )
    if resolved_options.create_mod_json:
        metadata_files.append(_write_json(root / "mod.json", mod_json_payload))
    if resolved_options.create_modinfo_json:
        metadata_files.append(_write_json(root / "modinfo.json", modinfo))
    if resolved_options.create_info_json:
        metadata_files.append(_write_json(root / "info.json", manifest_payload))

    zip_path = _write_package_zip(root) if resolved_options.create_zip else None
    return ModPackageFinalizeResult(
        metadata_files=metadata_files,
        zip_path=zip_path,
        payload_root=payload_root,
    )


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
    metadata_files: Sequence[Path] = (),
    ready_zip_path: Path | None = None,
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

    title = (package_info.title or "").strip() or "Crimson Desert Mod Workbench Mod"
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
    metadata_lines = _metadata_package_file_lines(metadata_files)
    if metadata_lines:
        lines.extend(metadata_lines)
    else:
        lines.append(f"manifest.json  {manifest_label}")
        if create_no_encrypt_file:
            lines.append(".no_encrypt    Marks the package for non-encrypted handling")
    if ready_zip_path is not None:
        lines.append(f"{ready_zip_path.name:<14} Ready-to-import zip written beside this folder.")

    _add_section(lines, "Installation")
    lines.append("1. Copy or import the contents of the folder into your Crimson Desert mod manager.")
    lines.append("2. Deploy or enable the mod through your preferred mod manager.")
    lines.append("3. Verify that the updated mesh loads correctly in game.")
    lines.append("")
    lines.append("Preferred mod manager:")
    lines.append(PREFERRED_CRIMSON_DESERT_MOD_MANAGER_URL)

    _add_section(lines, "Notes")
    lines.append("This package was generated automatically by Crimson Desert Mod Workbench.")
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
    new_file_paths: Sequence[str | Path] = (),
    all_payload_paths: Sequence[str | Path] = (),
    export_options: ModPackageExportOptions | None = None,
    create_no_encrypt_file: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    resolved_export_options = export_options or ModPackageExportOptions(
        create_no_encrypt_file=create_no_encrypt_file,
    )
    effective_payload_paths: Sequence[str | Path] = all_payload_paths or _discover_payload_paths_under_root(root)
    normalized_structure = (
        resolved_export_options.structure
        if resolved_export_options.structure in {"game_relative", "files_wrapper"}
        else "game_relative"
    )
    files_dir_name = _safe_files_dir(resolved_export_options.files_dir)
    files_dir_value = files_dir_name if normalized_structure == "files_wrapper" else "."
    manager_targets = _normalize_manager_targets(resolved_export_options.manager_targets)
    new_path_prefixes = normalize_mod_package_new_path_prefixes(
        new_file_paths,
        all_payload_paths=effective_payload_paths,
    )
    common_fields = _common_mod_package_fields(
        package_info,
        files_dir_value=files_dir_value,
        manager_targets=manager_targets,
        new_path_prefixes=new_path_prefixes,
    )

    created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = _compact_mapping(
        {
            "format": "crimson_browser_mod_v1" if normalized_structure == "files_wrapper" else "v1",
            "schema_version": 1,
            "kind": kind,
            **common_fields,
            "created_utc": created_utc,
            "structure": normalized_structure,
        }
    )
    if extra_fields:
        payload.update(_compact_mapping(dict(extra_fields)))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    metadata_options = dataclasses.replace(
        resolved_export_options,
        create_manifest_json=False,
        create_no_encrypt_file=create_no_encrypt_file,
        create_zip=False,
    )
    finalized = finalize_mod_package_export(
        root,
        package_info,
        kind=kind,
        payload_paths=effective_payload_paths,
        new_file_paths=new_file_paths,
        extra_fields=extra_fields,
        options=metadata_options,
        created_utc=created_utc,
    )
    ready_zip_path = root.with_suffix(".zip") if resolved_export_options.create_zip else None
    metadata_files = [manifest_path, *[path for path in finalized.metadata_files if path.name != "manifest.json"]]
    write_mod_package_readme(
        root,
        package_info,
        created_utc=created_utc,
        overview="This package contains loose file replacements generated by Crimson Desert Mod Workbench.",
        loose_file_count=int(payload.get("file_count", 0) or 0),
        create_no_encrypt_file=create_no_encrypt_file,
        manifest_label="Structured package metadata",
        metadata_files=metadata_files,
        ready_zip_path=ready_zip_path,
    )
    if ready_zip_path is not None:
        _write_package_zip(root)
    return manifest_path


def write_mesh_loose_mod_package_metadata(
    root: Path,
    package_info: ModPackageInfo,
    *,
    assets: Sequence[MeshLooseModAsset],
    files: Sequence[MeshLooseModFile],
    include_paired_lod: bool,
    export_options: ModPackageExportOptions | None = None,
    create_no_encrypt_file: bool = True,
    game_build: str = "",
) -> list[Path]:
    created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resolved_export_options = export_options or ModPackageExportOptions(
        create_no_encrypt_file=create_no_encrypt_file,
    )
    normalized_game_build = (game_build or "").strip()
    normalized_structure = (
        resolved_export_options.structure
        if resolved_export_options.structure in {"game_relative", "files_wrapper"}
        else "game_relative"
    )
    files_dir_name = _safe_files_dir(resolved_export_options.files_dir)
    files_dir_value = files_dir_name if normalized_structure == "files_wrapper" else "."
    manager_targets = _normalize_manager_targets(resolved_export_options.manager_targets)
    file_paths = [file_info.path for file_info in files]
    new_path_prefixes = normalize_mod_package_new_path_prefixes(
        [file_info.path for file_info in files if bool(getattr(file_info, "is_new", False))],
        all_payload_paths=file_paths,
    )
    common_fields = _common_mod_package_fields(
        package_info,
        files_dir_value=files_dir_value,
        manager_targets=manager_targets,
        new_path_prefixes=new_path_prefixes,
    )
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
            "format": "crimson_browser_mod_v1" if normalized_structure == "files_wrapper" else "v1",
            "schema_version": 1,
            "kind": "mesh_loose_mod",
            **common_fields,
            "created_utc": created_utc,
            "structure": normalized_structure,
            "game_build": normalized_game_build,
            "include_paired_lod": bool(include_paired_lod),
            "asset_count": len(assets),
            "file_count": len(files),
            "new_paths": new_path_prefixes,
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
                        "is_new": True if file_info.is_new else None,
                        "note": file_info.note,
                    }
                )
                for file_info in files
            ],
        }
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")
    finalized = finalize_mod_package_export(
        root,
        package_info,
        kind="mesh_loose_mod",
        payload_paths=file_paths,
        new_file_paths=[file_info.path for file_info in files if bool(getattr(file_info, "is_new", False))],
        extra_fields={
            "game_build": normalized_game_build,
            "include_paired_lod": bool(include_paired_lod),
            "asset_count": len(assets),
            "file_count": len(files),
        },
        options=dataclasses.replace(
            resolved_export_options,
            create_manifest_json=False,
            create_no_encrypt_file=create_no_encrypt_file,
            create_zip=False,
        ),
        created_utc=created_utc,
    )
    ready_zip_path = root.with_suffix(".zip") if resolved_export_options.create_zip else None
    metadata_files = [manifest_path, *[path for path in finalized.metadata_files if path.name != "manifest.json"]]
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
        metadata_files=metadata_files,
        ready_zip_path=ready_zip_path,
    )
    if ready_zip_path is not None:
        ready_zip_path = _write_package_zip(root)
    return [manifest_path, readme_path, *[path for path in finalized.metadata_files if path.name != "manifest.json"]]
