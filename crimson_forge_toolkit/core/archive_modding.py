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


def _safe_log(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        log(message)


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
    on_log: Optional[Callable[[str], None]] = None,
) -> ArchiveLooseExportResult:
    from crimson_forge_toolkit.core.mod_package import (
        MeshLooseModAsset,
        MeshLooseModFile,
        resolve_mod_package_root,
        write_mesh_loose_mod_package_metadata,
    )

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

    summary_lines = [
        f"Path: {entry.path}",
        f"Format: {parsed_mesh.format.upper()}",
        f"Submeshes: {len(parsed_mesh.submeshes):,}",
        f"Vertices: {parsed_mesh.total_vertices:,}",
        f"Faces: {parsed_mesh.total_faces:,}",
    ]
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


def build_mesh_import_preview(
    entry: ArchiveEntry,
    obj_path: Path,
    *,
    archive_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texconv_path: Optional[Path] = None,
    texture_entries_by_normalized_path: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
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
    sidecar_texture_references: Tuple[object, ...] = ()
    sidecar_reference_paths: Tuple[str, ...] = ()
    if texture_entries_by_basename is not None:
        sidecar_texture_references, sidecar_reference_paths = _extract_archive_model_sidecar_texture_references(
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
        )
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
    detail_lines.append(f"Root bones: {len(root_bones):,}")
    lines.append("")
    lines.append("Bone hierarchy:")
    for bone in skeleton.bones[:128]:
        parent_text = "root" if bone.parent_index < 0 else f"parent {bone.parent_index}"
        lines.append(f"[{bone.index:03d}] {bone.name or '<unnamed>'} ({parent_text})")
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
