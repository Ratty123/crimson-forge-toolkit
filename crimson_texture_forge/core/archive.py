from __future__ import annotations

import fnmatch
import hashlib
import os
import pickle
import re
import shutil
import struct
import tempfile
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    import lz4.block as lz4_block
except ImportError:
    lz4_block = None

try:
    import winreg
except ImportError:
    winreg = None

from crimson_texture_forge.constants import *
from crimson_texture_forge.models import *
from crimson_texture_forge.core.common import *
from crimson_texture_forge.core.pipeline import ensure_dds_preview_png, parse_dds

_PATHC_COLLECTION_CACHE: Dict[str, Tuple[str, "PathcCollection"]] = {}
_ARCHIVE_SCAN_CACHE_MAGIC = b"CTFARCH1"
_ARCHIVE_SCAN_CACHE_VERSION = 2
_ARCHIVE_SCAN_CACHE_SUPPORTED_VERSIONS = {1, 2}

def _rot32(value: int, shift: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << shift) | (value >> (32 - shift))) & 0xFFFFFFFF


def _add32(a: int, b: int) -> int:
    return (a + b) & 0xFFFFFFFF


def _sub32(a: int, b: int) -> int:
    return (a - b) & 0xFFFFFFFF


def _finalize_lookup3(a: int, b: int, c: int) -> Tuple[int, int, int]:
    c = _sub32(c ^ b, _rot32(b, 14))
    a = _sub32(a ^ c, _rot32(c, 11))
    b = _sub32(b ^ a, _rot32(a, 25))
    c = _sub32(c ^ b, _rot32(b, 16))
    a = _sub32(a ^ c, _rot32(c, 4))
    b = _sub32(b ^ a, _rot32(a, 14))
    c = _sub32(c ^ b, _rot32(b, 24))
    return a, b, c


def calculate_pa_checksum(value: bytes | str) -> int:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    length = len(data)
    remaining = length
    a = b = c = _add32(length, 0xDEBA1DCD)
    offset = 0

    while remaining > 12:
        a = _add32(a, struct.unpack_from("<I", data, offset)[0])
        b = _add32(b, struct.unpack_from("<I", data, offset + 4)[0])
        c = _add32(c, struct.unpack_from("<I", data, offset + 8)[0])
        a = _sub32(a, c)
        a ^= _rot32(c, 4)
        c = _add32(c, b)
        b = _sub32(b, a)
        b ^= _rot32(a, 6)
        a = _add32(a, c)
        c = _sub32(c, b)
        c ^= _rot32(b, 8)
        b = _add32(b, a)
        a = _sub32(a, c)
        a ^= _rot32(c, 16)
        c = _add32(c, b)
        b = _sub32(b, a)
        b ^= _rot32(a, 19)
        a = _add32(a, c)
        c = _sub32(c, b)
        c ^= _rot32(b, 4)
        b = _add32(b, a)
        offset += 12
        remaining -= 12

    if remaining == 0:
        return c

    tail = data[offset:] + (b"\x00" * (12 - remaining))
    a = _add32(a, struct.unpack_from("<I", tail, 0)[0])
    b = _add32(b, struct.unpack_from("<I", tail, 4)[0])
    c = _add32(c, struct.unpack_from("<I", tail, 8)[0])
    _, _, c = _finalize_lookup3(a, b, c)
    return c

def discover_pamt_files(package_root: Path) -> List[Path]:
    root = package_root.expanduser().resolve()
    if root.is_file() and root.suffix.lower() == ".pamt":
        return [root]
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Archive package root does not exist or is not a folder: {root}")
    files = sorted(path for path in root.rglob("*.pamt") if path.is_file())
    return files


def resolve_archive_scan_cache_path(package_root: Path, cache_root: Path) -> Path:
    try:
        resolved_root = package_root.expanduser().resolve()
    except OSError:
        resolved_root = package_root.expanduser()
    digest = hashlib.sha256(str(resolved_root).lower().encode("utf-8", errors="replace")).hexdigest()[:24]
    return cache_root / f"archive_scan_{digest}.bin"


def _archive_base_dir(package_root: Path) -> Path:
    try:
        resolved_root = package_root.expanduser().resolve()
    except OSError:
        resolved_root = package_root.expanduser()
    return resolved_root.parent if resolved_root.is_file() else resolved_root


def _archive_relative_source_path(base_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _collect_archive_scan_sources(
    package_root: Path,
    *,
    pamt_files: Optional[Sequence[Path]] = None,
) -> Tuple[Path, List[Tuple[str, int, int]]]:
    base_dir = _archive_base_dir(package_root)
    files = list(pamt_files) if pamt_files is not None else discover_pamt_files(package_root)
    sources: List[Tuple[str, int, int]] = []
    for pamt_path in files:
        stat_result = pamt_path.stat()
        sources.append(
            (
                _archive_relative_source_path(base_dir, pamt_path),
                int(stat_result.st_size),
                int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
            )
        )
    return base_dir, sources


def _serialize_archive_scan_cache_payload(payload: dict) -> bytes:
    raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    if lz4_block is not None:
        return _ARCHIVE_SCAN_CACHE_MAGIC + b"L" + lz4_block.compress(raw, store_size=True)
    return _ARCHIVE_SCAN_CACHE_MAGIC + b"R" + raw


def _deserialize_archive_scan_cache_payload(blob: bytes) -> dict:
    if not blob.startswith(_ARCHIVE_SCAN_CACHE_MAGIC):
        raise ValueError("Archive cache header is not recognized.")
    mode = blob[len(_ARCHIVE_SCAN_CACHE_MAGIC) : len(_ARCHIVE_SCAN_CACHE_MAGIC) + 1]
    payload = blob[len(_ARCHIVE_SCAN_CACHE_MAGIC) + 1 :]
    if mode == b"L":
        if lz4_block is None:
            raise ValueError("Archive cache requires lz4, but python-lz4 is not available.")
        payload = lz4_block.decompress(payload)
    elif mode != b"R":
        raise ValueError("Archive cache compression mode is not supported.")
    data = pickle.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Archive cache payload is invalid.")
    return data


def save_archive_scan_cache(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = resolve_archive_scan_cache_path(package_root, cache_root)
    base_dir, sources = _collect_archive_scan_sources(package_root)

    rows = []
    total_entries = len(entries)
    update_every = 50_000 if total_entries >= 500_000 else 10_000 if total_entries >= 100_000 else 2_000
    for index, entry in enumerate(entries, start=1):
        raise_if_cancelled(stop_event)
        rows.append(
            (
                entry.path,
                _archive_relative_source_path(base_dir, entry.pamt_path),
                int(entry.offset),
                int(entry.comp_size),
                int(entry.orig_size),
                int(entry.flags),
                int(entry.paz_index),
            )
        )
        if on_progress and (index == 1 or index % update_every == 0 or index == total_entries):
            on_progress(index, max(total_entries, 1), f"Building archive cache... {index:,} / {total_entries:,} entries")

    payload = {
        "version": _ARCHIVE_SCAN_CACHE_VERSION,
        "package_root": str(package_root),
        "created_at": time.time(),
        "sources": sources,
        "rows": rows,
    }
    if on_log:
        on_log(f"Writing archive cache: {cache_path.name}")
    if on_progress:
        on_progress(0, 0, "Compressing archive cache...")
    blob = _serialize_archive_scan_cache_payload(payload)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temp_path.write_bytes(blob)
    temp_path.replace(cache_path)
    if on_progress:
        on_progress(1, 1, "Archive cache is ready.")
    if on_log:
        on_log(f"Archive cache updated: {cache_path}")
    return cache_path


def load_archive_scan_cache(
    package_root: Path,
    cache_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Optional[List[ArchiveEntry]]:
    cache_path = resolve_archive_scan_cache_path(package_root, cache_root)
    if not cache_path.exists():
        return None

    if on_progress:
        on_progress(0, 0, "Checking archive cache...")
    try:
        base_dir, current_sources = _collect_archive_scan_sources(package_root)
    except Exception as exc:
        if on_log:
            on_log(f"Archive cache check failed; will rescan instead: {exc}")
        return None

    try:
        data = _deserialize_archive_scan_cache_payload(cache_path.read_bytes())
    except Exception as exc:
        if on_log:
            on_log(f"Archive cache could not be read; will rescan instead: {exc}")
        return None

    if int(data.get("version", 0)) not in _ARCHIVE_SCAN_CACHE_SUPPORTED_VERSIONS:
        if on_log:
            on_log("Archive cache format changed; performing a full rescan.")
        return None

    cached_sources = data.get("sources")
    if not isinstance(cached_sources, list):
        if on_log:
            on_log("Archive cache is missing source metadata; performing a full rescan.")
        return None

    if cached_sources != current_sources:
        if on_log:
            on_log("Archive cache is out of date; archive indexes changed since the last scan.")
        return None

    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list):
        if on_log:
            on_log("Archive cache is missing entry rows; performing a full rescan.")
        return None

    total_rows = len(raw_rows)
    if on_log:
        on_log(f"Loading {total_rows:,} archive entries from cache...")
    if total_rows == 0:
        if on_progress:
            on_progress(1, 1, "Archive cache loaded. No entries were cached.")
        return []

    update_every = 50_000 if total_rows >= 500_000 else 10_000 if total_rows >= 100_000 else 2_000
    pamt_path_cache: Dict[str, Path] = {}
    paz_path_cache: Dict[Tuple[str, int], Path] = {}
    entries: List[ArchiveEntry] = []
    for index, row in enumerate(raw_rows, start=1):
        raise_if_cancelled(stop_event)
        if not isinstance(row, tuple) or len(row) != 7:
            raise ValueError("Archive cache row shape is invalid.")
        path, pamt_rel, offset, comp_size, orig_size, flags, paz_index = row
        pamt_rel_text = str(pamt_rel)
        pamt_path = pamt_path_cache.get(pamt_rel_text)
        if pamt_path is None:
            pamt_path = base_dir / pamt_rel_text
            pamt_path_cache[pamt_rel_text] = pamt_path
        paz_key = (pamt_rel_text, int(paz_index))
        paz_path = paz_path_cache.get(paz_key)
        if paz_path is None:
            paz_path = pamt_path.parent / f"{int(paz_index)}.paz"
            paz_path_cache[paz_key] = paz_path
        entries.append(
            ArchiveEntry(
                path=str(path),
                pamt_path=pamt_path,
                paz_file=paz_path,
                offset=int(offset),
                comp_size=int(comp_size),
                orig_size=int(orig_size),
                flags=int(flags),
                paz_index=int(paz_index),
            )
        )
        if on_progress and (index == 1 or index % update_every == 0 or index == total_rows):
            on_progress(index, total_rows, f"Loading archive cache... {index:,} / {total_rows:,} entries")

    if on_log:
        on_log(f"Loaded {len(entries):,} archive entries from cache.")
    return entries


def scan_archive_entries_cached(
    package_root: Path,
    cache_root: Path,
    *,
    force_refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[List[ArchiveEntry], str, Optional[Path]]:
    cache_path = resolve_archive_scan_cache_path(package_root, cache_root)
    if force_refresh:
        if on_log:
            on_log("Ignoring archive cache and performing a full rescan.")
    else:
        cached_entries = load_archive_scan_cache(
            package_root,
            cache_root,
            on_log=on_log,
            on_progress=on_progress,
            stop_event=stop_event,
        )
        if cached_entries is not None:
            return cached_entries, "cache", cache_path

    entries = scan_archive_entries(
        package_root,
        on_log=on_log,
        on_progress=on_progress,
        stop_event=stop_event,
    )
    try:
        cache_path = save_archive_scan_cache(
            package_root,
            cache_root,
            entries,
            on_log=on_log,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    except Exception as exc:
        if on_log:
            on_log(f"Warning: archive cache could not be written: {exc}")
        cache_path = None
    return entries, "scan", cache_path


def parse_steam_library_paths(libraryfolders_path: Path) -> List[Path]:
    if not libraryfolders_path.exists():
        return []
    try:
        text = libraryfolders_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    paths: List[Path] = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
        raw_path = match.group(1).replace("\\\\", "\\").strip()
        if raw_path:
            paths.append(Path(raw_path))
    return paths


def parse_steam_appmanifest_installdir(appmanifest_path: Path) -> Optional[str]:
    if not appmanifest_path.exists():
        return None
    try:
        text = appmanifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
    if not match:
        return None
    install_dir = match.group(1).replace("\\\\", "\\").strip()
    return install_dir or None


def _normalize_existing_path(path: Path) -> Optional[Path]:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser()
    if not resolved.exists():
        return None
    return resolved


def discover_steam_roots() -> List[Path]:
    candidates: set[Path] = set()
    env_candidates = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        r"C:\Steam",
    ]
    for raw in env_candidates:
        if not raw:
            continue
        raw_path = Path(raw)
        candidates.add(raw_path if raw_path.name.lower() == "steam" else raw_path / "Steam")

    if winreg is not None and os.name == "nt":
        registry_lookups = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath", "SteamExe")),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ("InstallPath", "SteamPath")),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", ("InstallPath", "SteamPath")),
        ]
        for hive, subkey, value_names in registry_lookups:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    for value_name in value_names:
                        try:
                            value, _value_type = winreg.QueryValueEx(key, value_name)
                        except OSError:
                            continue
                        if not value:
                            continue
                        candidate = Path(str(value))
                        if candidate.suffix.lower() == ".exe":
                            candidate = candidate.parent
                        candidates.add(candidate)
            except OSError:
                continue

    resolved: List[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved_candidate = candidate.expanduser().resolve()
        except OSError:
            resolved_candidate = candidate.expanduser()
        lowered = str(resolved_candidate).lower()
        if lowered in seen or not resolved_candidate.exists():
            continue
        seen.add(lowered)
        resolved.append(resolved_candidate)
    return sorted(resolved)


def discover_windows_drive_roots() -> List[Path]:
    if os.name != "nt":
        return []
    roots: List[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:\\")
        if candidate.exists():
            roots.append(candidate)
    return roots


def discover_non_steam_base_paths() -> List[Path]:
    candidates: set[Path] = set()
    env_candidates = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("ProgramW6432"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("USERPROFILE"),
        r"C:\Games",
        r"D:\Games",
        r"E:\Games",
        r"F:\Games",
    ]
    for raw in env_candidates:
        if not raw:
            continue
        normalized = _normalize_existing_path(Path(raw))
        if normalized is not None:
            candidates.add(normalized)

    for drive_root in discover_windows_drive_roots():
        normalized_root = _normalize_existing_path(drive_root)
        if normalized_root is None:
            continue
        candidates.add(normalized_root)
        try:
            for child in normalized_root.iterdir():
                if child.is_dir():
                    normalized_child = _normalize_existing_path(child)
                    if normalized_child is not None:
                        candidates.add(normalized_child)
        except OSError:
            continue

    return sorted(candidates)


def discover_non_steam_archive_package_roots(
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    explicit_env_vars = (
        "CRIMSON_TEXTURE_FORGE_PACKAGE_ROOT",
        "CRIMSON_DESERT_PACKAGE_ROOT",
    )
    candidates: set[Path] = set()

    for env_var in explicit_env_vars:
        raw_value = os.environ.get(env_var)
        if not raw_value:
            continue
        candidate = Path(raw_value)
        if looks_like_archive_package_root(candidate):
            normalized = _normalize_existing_path(candidate)
            if normalized is not None:
                candidates.add(normalized)
                if on_log:
                    on_log(f"Detected archive package root candidate from {env_var}: {normalized}")
        elif on_log:
            on_log(f"Ignoring {env_var}: path does not look like a valid Crimson Desert package root: {candidate}")

    game_dir_names = ("Crimson Desert", "CrimsonDesert")
    relative_patterns = (
        (),
        ("Games",),
        ("Steam", "steamapps", "common"),
        ("SteamLibrary", "steamapps", "common"),
        ("steamapps", "common"),
        ("Epic Games",),
    )

    for base_path in discover_non_steam_base_paths():
        for relative_parts in relative_patterns:
            for game_dir_name in game_dir_names:
                candidate = base_path.joinpath(*relative_parts, game_dir_name)
                if not looks_like_archive_package_root(candidate):
                    continue
                normalized = _normalize_existing_path(candidate)
                if normalized is not None:
                    candidates.add(normalized)

    store_container_names = (
        "XboxGames",
        "ModifiableWindowsApps",
        "WindowsApps",
    )
    store_candidate_suffixes = (
        (),
        ("Content",),
        ("Game",),
        ("Content", "Game"),
    )

    for drive_root in discover_windows_drive_roots():
        for container_name in store_container_names:
            candidate_container = drive_root / container_name
            if not candidate_container.exists() or not candidate_container.is_dir():
                continue

            direct_name_matches: List[Path] = []
            for game_dir_name in game_dir_names:
                direct_name_matches.extend(
                    [
                        candidate_container / game_dir_name,
                        candidate_container / f"{game_dir_name} Standard Edition",
                        candidate_container / f"{game_dir_name} Deluxe Edition",
                    ]
                )

            seen_container_children: set[str] = set()
            dynamic_child_matches: List[Path] = []
            try:
                for child in candidate_container.iterdir():
                    if not child.is_dir():
                        continue
                    child_key = child.name.lower()
                    if child_key in seen_container_children:
                        continue
                    seen_container_children.add(child_key)
                    lowered_name = child.name.lower()
                    if "crimson" in lowered_name and "desert" in lowered_name:
                        dynamic_child_matches.append(child)
            except OSError:
                continue

            for game_root in [*direct_name_matches, *dynamic_child_matches]:
                for suffix in store_candidate_suffixes:
                    candidate = game_root.joinpath(*suffix)
                    if not looks_like_archive_package_root(candidate):
                        continue
                    normalized = _normalize_existing_path(candidate)
                    if normalized is not None:
                        candidates.add(normalized)

    return sorted(candidates)


def looks_like_archive_package_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        if next(path.glob("*.pamt"), None) is not None:
            return True
        for child in path.iterdir():
            if not child.is_dir() or not re.fullmatch(r"\d{4}", child.name):
                continue
            if next(child.glob("*.pamt"), None) is not None:
                return True
    except OSError:
        return False
    return False


def autodetect_archive_package_roots(
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    if on_log:
        on_log("Checking Steam libraries and common custom install locations...")
    library_roots: set[Path] = set()
    for steam_root in discover_steam_roots():
        library_roots.add(steam_root)
        for library_file in (
            steam_root / "steamapps" / "libraryfolders.vdf",
            steam_root / "config" / "libraryfolders.vdf",
        ):
            for library_root in parse_steam_library_paths(library_file):
                library_roots.add(library_root)

    candidates: set[Path] = set()
    for library_root in sorted(library_roots):
        manifest_path = library_root / "steamapps" / f"appmanifest_{CRIMSON_DESERT_STEAM_APP_ID}.acf"
        manifest_install_dir = parse_steam_appmanifest_installdir(manifest_path)
        possible_dirs: List[Path] = []
        if manifest_install_dir:
            possible_dirs.append(library_root / "steamapps" / "common" / manifest_install_dir)
        possible_dirs.append(library_root / "steamapps" / "common" / "Crimson Desert")

        for candidate in possible_dirs:
            if looks_like_archive_package_root(candidate):
                try:
                    resolved_candidate = candidate.resolve()
                except OSError:
                    resolved_candidate = candidate
                candidates.add(resolved_candidate)

    for candidate in discover_non_steam_archive_package_roots(on_log=on_log):
        candidates.add(candidate)

    if on_log:
        if candidates:
            for candidate in sorted(candidates):
                on_log(f"Detected archive package root candidate: {candidate}")
        else:
            on_log("No valid Crimson Desert archive package roots were auto-detected.")

    return sorted(candidates)


class VfsPathResolver:
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


def parse_archive_pamt(pamt_path: Path, paz_dir: Optional[Path] = None) -> List[ArchiveEntry]:
    data = pamt_path.read_bytes()
    resolved_paz_dir = paz_dir if paz_dir is not None else pamt_path.parent
    size = len(data)
    if size < 12:
        raise ValueError(f"{pamt_path} is too small to be a valid .pamt file.")

    off = 0
    _header_crc, paz_count, _unknown = struct.unpack_from("<III", data, off)
    off += 12

    paz_table_size = paz_count * 12
    if off + paz_table_size > size:
        raise ValueError(f"{pamt_path.name} paz table is truncated.")
    paz_indices = list(range(paz_count))
    off += paz_table_size

    if off + 4 > size:
        raise ValueError(f"{pamt_path.name} directory block length is truncated.")
    dir_block_size = read_u32_le(data, off)
    off += 4
    directory_data = data[off : off + dir_block_size]
    if len(directory_data) != dir_block_size:
        raise ValueError(f"{pamt_path.name} directory block is truncated.")
    off += dir_block_size

    if off + 4 > size:
        raise ValueError(f"{pamt_path.name} file-name block length is truncated.")
    file_name_block_size = read_u32_le(data, off)
    off += 4
    file_names = data[off : off + file_name_block_size]
    if len(file_names) != file_name_block_size:
        raise ValueError(f"{pamt_path.name} file-name block is truncated.")
    off += file_name_block_size

    if off + 4 > size:
        raise ValueError(f"{pamt_path.name} folder table length is truncated.")
    folder_count = read_u32_le(data, off)
    off += 4
    folder_table_size = folder_count * 16
    if off + folder_table_size > size:
        raise ValueError(f"{pamt_path.name} folder table is truncated.")
    folders = list(struct.iter_unpack("<IIII", data[off : off + folder_table_size]))
    off += folder_table_size

    if off + 4 > size:
        raise ValueError(f"{pamt_path.name} file table length is truncated.")
    file_count = read_u32_le(data, off)
    off += 4
    file_table_size = file_count * struct.calcsize("<IIIIHH")
    if off + file_table_size > size:
        raise ValueError(f"{pamt_path.name} file table is truncated.")
    files = list(struct.iter_unpack("<IIIIHH", data[off : off + file_table_size]))

    resolver = VfsPathResolver(file_names)
    dir_resolver = VfsPathResolver(directory_data)
    folder_ranges = sorted(
        (
            file_start_index,
            file_start_index + folder_file_count,
            dir_resolver.get_full_path(name_offset).replace("\\", "/").strip("/"),
        )
        for _folder_hash, name_offset, file_start_index, folder_file_count in folders
        if folder_file_count > 0
    )

    entries: List[ArchiveEntry] = []
    for entry_index, (name_offset, paz_offset, comp_size, orig_size, paz_index, flags) in enumerate(files):
        relative_path = resolver.get_full_path(name_offset).replace("\\", "/").strip("/")
        guessed_dir = ""
        for start, end, candidate_dir in folder_ranges:
            if start <= entry_index < end:
                guessed_dir = candidate_dir
                break
            if start > entry_index:
                break
        full_path = f"{guessed_dir}/{relative_path}".strip("/") if guessed_dir else relative_path
        if paz_index >= len(paz_indices):
            raise ValueError(f"Invalid PAZ index {paz_index} for {pamt_path}")
        paz_file = resolved_paz_dir / f"{paz_indices[paz_index]}.paz"
        entries.append(
            ArchiveEntry(
                path=full_path,
                pamt_path=pamt_path,
                paz_file=paz_file,
                offset=paz_offset,
                comp_size=comp_size,
                orig_size=orig_size,
                flags=flags,
                paz_index=paz_index,
            )
        )

    return entries


def scan_archive_entries(
    package_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[ArchiveEntry]:
    pamt_files = discover_pamt_files(package_root)
    if not pamt_files:
        raise ValueError(f"No .pamt files were found under {package_root}.")

    all_entries: List[ArchiveEntry] = []
    total_pmts = len(pamt_files)
    if on_log:
        on_log(f"Found {total_pmts:,} archive index file(s).")
    if on_progress:
        on_progress(0, total_pmts, f"0 / {total_pmts} archive indexes | 0 entries found")
    for index, pamt_path in enumerate(pamt_files, start=1):
        raise_if_cancelled(stop_event)
        try:
            relative_label = pamt_path.relative_to(package_root).as_posix()
        except ValueError:
            relative_label = pamt_path.name

        if on_log:
            on_log(f"[{index}/{total_pmts}] Parsing {relative_label}...")

        parse_started = time.monotonic()
        heartbeat_stop = threading.Event()
        heartbeat_thread: Optional[threading.Thread] = None

        if on_progress:
            on_progress(
                index - 1,
                total_pmts,
                f"Parsing {index} / {total_pmts}: {relative_label} | {len(all_entries):,} entries found",
            )

            def emit_parse_heartbeat() -> None:
                while not heartbeat_stop.wait(1.0):
                    elapsed = max(1, int(time.monotonic() - parse_started))
                    on_progress(
                        index - 1,
                        total_pmts,
                        f"Parsing {index} / {total_pmts}: {relative_label} | {len(all_entries):,} entries found | still working ({elapsed}s elapsed)",
                    )

            heartbeat_thread = threading.Thread(target=emit_parse_heartbeat, daemon=True)
            heartbeat_thread.start()

        try:
            entries = parse_archive_pamt(pamt_path)
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=0.2)

        all_entries.extend(entries)
        parse_elapsed = time.monotonic() - parse_started
        if on_log:
            on_log(f"[{index}/{total_pmts}] Parsed {relative_label} -> {len(entries):,} entries in {parse_elapsed:.1f}s")
        if on_progress:
            on_progress(
                index,
                total_pmts,
                f"{index} / {total_pmts} archive indexes | {len(all_entries):,} entries found | last: {relative_label}",
            )

    return all_entries


def archive_entry_matches_filter(entry: ArchiveEntry, filter_text: str, extension_filter: str) -> bool:
    normalized_extension = normalize_archive_extension_filter(extension_filter)
    if normalized_extension and normalized_extension not in {"*", "all", ".*"}:
        if entry.extension != normalized_extension:
            return False

    text = filter_text.strip().lower()
    if not text:
        return True

    path_lower = entry.path.lower()
    basename_lower = entry.basename.lower()
    if any(char in text for char in "*?[]"):
        return fnmatch.fnmatch(path_lower, text) or fnmatch.fnmatch(basename_lower, text)
    return text in path_lower or text in basename_lower


def normalize_archive_extension_filter(extension_filter: str) -> str:
    normalized_extension = extension_filter.strip().lower()
    if not normalized_extension or normalized_extension in {"*", "all", ".*"}:
        return normalized_extension
    return normalized_extension if normalized_extension.startswith(".") else f".{normalized_extension}"


def archive_entry_role(entry: ArchiveEntry) -> str:
    path_lower = entry.path.lower()
    basename_lower = entry.basename.lower()
    extension = entry.extension

    if extension in ARCHIVE_MODEL_EXTENSIONS or extension in {".hkx"}:
        return "model"
    if "/ui/" in path_lower or basename_lower.startswith("ui_"):
        return "ui"
    if "impostor" in path_lower:
        return "impostor"
    if extension in ARCHIVE_IMAGE_EXTENSIONS or "/texture/" in path_lower:
        if any(token in basename_lower for token in ("_n.", "_normal", "_nrm", "_normalmap")):
            return "normal"
        if any(token in basename_lower for token in ("_sp.", "_rough", "_metal", "_mask", "_orm", "_ao", "_depth", "_disp")):
            return "material"
        return "image"
    if extension in ARCHIVE_TEXT_EXTENSIONS:
        return "text"
    return "other"


def archive_entry_is_previewable(entry: ArchiveEntry) -> bool:
    extension = entry.extension
    return extension in ARCHIVE_IMAGE_EXTENSIONS or extension in ARCHIVE_TEXT_EXTENSIONS or extension in ARCHIVE_MODEL_EXTENSIONS or extension in {".hkx"}


def archive_entry_matches_advanced_filters(
    entry: ArchiveEntry,
    *,
    package_filter_text: str,
    structure_filter: str,
    role_filter: str,
    min_size_kb: int,
    previewable_only: bool,
) -> bool:
    package_filter = package_filter_text.strip().lower()
    if package_filter and package_filter not in entry.package_label.lower() and package_filter not in str(entry.pamt_path).lower():
        return False

    if min_size_kb > 0 and entry.orig_size < min_size_kb * 1024:
        return False

    if previewable_only and not archive_entry_is_previewable(entry):
        return False

    normalized_structure = normalize_archive_structure_filter_value(structure_filter)
    if normalized_structure:
        if normalized_structure not in archive_entry_structure_prefixes(entry):
            return False

    normalized_role = role_filter.strip().lower()
    if normalized_role and normalized_role != "all":
        entry_role = archive_entry_role(entry)
        if normalized_role == "texture":
            if entry_role not in {"image", "normal", "material", "impostor", "ui"}:
                return False
        elif entry_role != normalized_role:
            return False

    return True


def filter_archive_entries(
    entries: Sequence[ArchiveEntry],
    *,
    filter_text: str,
    extension_filter: str,
    package_filter_text: str,
    structure_filter: str,
    role_filter: str,
    min_size_kb: int,
    previewable_only: bool,
) -> List[ArchiveEntry]:
    normalized_extension = normalize_archive_extension_filter(extension_filter)
    text = filter_text.strip().lower()
    wildcard_filter = any(char in text for char in "*?[]")
    package_filter = package_filter_text.strip().lower()
    min_size_bytes = min_size_kb * 1024 if min_size_kb > 0 else 0
    normalized_structure = normalize_archive_structure_filter_value(structure_filter)
    normalized_role = role_filter.strip().lower()
    require_role = bool(normalized_role and normalized_role != "all")

    filtered: List[ArchiveEntry] = []
    for entry in entries:
        if normalized_extension and normalized_extension not in {"*", "all", ".*"} and entry.extension != normalized_extension:
            continue

        if text:
            path_lower = entry.path.lower()
            basename_lower = entry.basename.lower()
            if wildcard_filter:
                if not (fnmatch.fnmatch(path_lower, text) or fnmatch.fnmatch(basename_lower, text)):
                    continue
            elif text not in path_lower and text not in basename_lower:
                continue

        if package_filter:
            package_label_lower = entry.package_label.lower()
            pamt_path_lower = str(entry.pamt_path).lower()
            if package_filter not in package_label_lower and package_filter not in pamt_path_lower:
                continue

        if min_size_bytes and entry.orig_size < min_size_bytes:
            continue

        if previewable_only and not archive_entry_is_previewable(entry):
            continue

        if normalized_structure and normalized_structure not in archive_entry_structure_prefixes(entry):
            continue

        if require_role:
            entry_role = archive_entry_role(entry)
            if normalized_role == "texture":
                if entry_role not in {"image", "normal", "material", "impostor", "ui"}:
                    continue
            elif entry_role != normalized_role:
                continue

        filtered.append(entry)

    return filtered


def count_archive_entries_with_extension(
    entries: Sequence[ArchiveEntry],
    extension_filter: str,
) -> int:
    normalized_extension = normalize_archive_extension_filter(extension_filter)
    if not normalized_extension or normalized_extension in {"*", "all", ".*"}:
        return len(entries)
    return sum(1 for entry in entries if entry.extension == normalized_extension)


def normalize_archive_structure_filter_value(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    return "/".join(
        part.lower()
        for part in raw.split("/")
        if part not in {"", ".", ".."}
    )


def archive_entry_path_parts(entry: ArchiveEntry) -> Tuple[str, ...]:
    return tuple(
        part
        for part in entry.path.replace("\\", "/").split("/")
        if part not in {"", ".", ".."}
    )


def archive_entry_folder_parts(entry: ArchiveEntry) -> Tuple[str, ...]:
    package_dir = entry.pamt_path.parent.name.strip().lower() or "package"
    parent_parts = tuple(part.lower() for part in archive_entry_path_parts(entry)[:-1])
    return (package_dir, *parent_parts)


def archive_entry_structure_prefixes(entry: ArchiveEntry) -> Tuple[str, ...]:
    parts = archive_entry_folder_parts(entry)
    return tuple("/".join(parts[: index + 1]) for index in range(len(parts)))


def build_archive_structure_children_map(entries: Sequence[ArchiveEntry]) -> Dict[str, List[Tuple[str, int]]]:
    child_counts: Dict[str, Dict[str, int]] = defaultdict(dict)

    for entry in entries:
        parts = archive_entry_folder_parts(entry)
        parent = ""
        for index in range(len(parts)):
            child_value = "/".join(parts[: index + 1])
            parent_counts = child_counts[parent]
            parent_counts[child_value] = parent_counts.get(child_value, 0) + 1
            parent = child_value

    def leaf_sort_key(value: str) -> Tuple[int, int, str]:
        leaf = value.rsplit("/", 1)[-1]
        if leaf.isdigit():
            return (0, int(leaf), leaf)
        return (1, 0, leaf)

    return {
        parent: sorted(children.items(), key=lambda item: leaf_sort_key(item[0]))
        for parent, children in child_counts.items()
    }


def build_archive_tree_index(
    entries: Sequence[ArchiveEntry],
) -> Tuple[
    Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]],
    Dict[Tuple[str, ...], List[int]],
    Dict[Tuple[str, ...], List[int]],
]:
    child_folder_sets: Dict[Tuple[str, ...], Dict[Tuple[str, ...], str]] = defaultdict(dict)
    direct_files: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
    folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = defaultdict(list)

    for index, entry in enumerate(entries):
        parts = archive_entry_path_parts(entry)
        if not parts:
            continue

        folder_key = parts[:-1]
        direct_files[folder_key].append(index)
        folder_entry_indexes[()].append(index)

        parent_key: Tuple[str, ...] = ()
        for depth, part in enumerate(folder_key):
            child_key = folder_key[: depth + 1]
            child_folder_sets[parent_key][child_key] = part
            folder_entry_indexes[child_key].append(index)
            parent_key = child_key

    def folder_sort_key(item: Tuple[Tuple[str, ...], str]) -> Tuple[int, int, str]:
        _child_key, leaf = item
        if leaf.isdigit():
            return (0, int(leaf), leaf)
        return (1, 0, leaf)

    child_folders = {
        parent: sorted(
            ((leaf, child_key) for child_key, leaf in children.items()),
            key=lambda item: folder_sort_key((item[1], item[0])),
        )
        for parent, children in child_folder_sets.items()
    }
    direct_files_by_folder = {
        folder_key: sorted(
            indexes,
            key=lambda idx: entries[idx].basename.lower(),
        )
        for folder_key, indexes in direct_files.items()
    }
    return child_folders, direct_files_by_folder, dict(folder_entry_indexes)


def prepare_archive_browser_state(
    entries: Sequence[ArchiveEntry],
    *,
    filter_text: str,
    extension_filter: str,
    package_filter_text: str,
    structure_filter: str,
    role_filter: str,
    min_size_kb: int,
    previewable_only: bool,
    build_structure_children: bool = True,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    total_steps = 3 if build_structure_children else 2
    structure_children: Dict[str, List[Tuple[str, int]]] = {}
    if build_structure_children:
        if on_progress:
            on_progress(1, total_steps, "Building folder filters from archive entries...")
        structure_children = build_archive_structure_children_map(entries)

    if on_progress:
        on_progress(2 if build_structure_children else 1, total_steps, "Applying archive filters...")
    filtered_entries = filter_archive_entries(
        entries,
        filter_text=filter_text,
        extension_filter=extension_filter,
        package_filter_text=package_filter_text,
        structure_filter=structure_filter,
        role_filter=role_filter,
        min_size_kb=min_size_kb,
        previewable_only=previewable_only,
    )

    if on_progress:
        on_progress(total_steps, total_steps, "Indexing archive browser tree...")
    tree_child_folders, tree_direct_files, folder_entry_indexes = build_archive_tree_index(filtered_entries)
    dds_count = sum(1 for entry in filtered_entries if entry.extension == ".dds")

    return {
        "structure_children": structure_children,
        "filtered_entries": filtered_entries,
        "tree_child_folders": tree_child_folders,
        "tree_direct_files": tree_direct_files,
        "tree_folder_entry_indexes": folder_entry_indexes,
        "dds_count": dds_count,
    }


class PathcCollection:
    def __init__(self, path: Path) -> None:
        raw = path.read_bytes()
        if len(raw) < 32:
            raise ValueError(f"{path} is too small to be a valid .pathc file.")
        (
            _reserved0,
            header_size,
            header_count,
            entry_count,
            collision_entry_count,
            filenames_length,
        ) = struct.unpack_from("<QIIIII", raw, 0)
        offset = struct.calcsize("<QIIIII")
        self.header_size = header_size
        self.headers: List[bytes] = []
        for _ in range(header_count):
            header = raw[offset : offset + header_size]
            if len(header) != header_size:
                raise ValueError(f"{path.name} texture header block is truncated.")
            self.headers.append(header)
            offset += header_size
        checksums: List[int] = []
        for _ in range(entry_count):
            if offset + 4 > len(raw):
                raise ValueError(f"{path.name} checksum table is truncated.")
            checksums.append(struct.unpack_from("<I", raw, offset)[0])
            offset += 4
        entries: List[PathcEntry] = []
        for _ in range(entry_count):
            if offset + 20 > len(raw):
                raise ValueError(f"{path.name} entry table is truncated.")
            texture_header_index, collision_start_index, collision_end_index, compressed_block_infos = struct.unpack_from(
                "<HBB16s",
                raw,
                offset,
            )
            entries.append(
                PathcEntry(
                    texture_header_index=texture_header_index,
                    collision_start_index=collision_start_index,
                    collision_end_index=collision_end_index,
                    compressed_block_infos=compressed_block_infos,
                )
            )
            offset += 20
        self.entries = {checksum: entry for checksum, entry in zip(checksums, entries)}
        collision_entries: List[PathcCollisionEntry] = []
        for _ in range(collision_entry_count):
            if offset + 24 > len(raw):
                raise ValueError(f"{path.name} collision table is truncated.")
            filename_offset, texture_header_index, unknown0, compressed_block_infos = struct.unpack_from(
                "<IHH16s",
                raw,
                offset,
            )
            collision_entries.append(
                PathcCollisionEntry(
                    filename_offset=filename_offset,
                    texture_header_index=texture_header_index,
                    unknown0=unknown0,
                    compressed_block_infos=compressed_block_infos,
                )
            )
            offset += 24
        filenames = raw[offset : offset + filenames_length]
        if len(filenames) != filenames_length:
            raise ValueError(f"{path.name} filename table is truncated.")
        self.hash_collision_entries: Dict[str, PathcCollisionEntry] = {}
        for entry in collision_entries:
            end = filenames.find(b"\x00", entry.filename_offset)
            if end < 0:
                end = len(filenames)
            name = filenames[entry.filename_offset:end].decode("utf-8", errors="replace")
            self.hash_collision_entries[name] = entry

    def get_file_header(self, path: str) -> bytes:
        normalized = path.replace("\\", "/").lstrip("/")
        checksum = calculate_pa_checksum(f"/{normalized}")
        entry = self.entries.get(checksum)
        if entry is None:
            raise KeyError(normalized)
        if entry.texture_header_index != 0xFFFF:
            header = self.headers[entry.texture_header_index]
            compressed_block_infos = entry.compressed_block_infos
        else:
            collision_entry = self.hash_collision_entries.get(normalized)
            if collision_entry is None:
                raise KeyError(normalized)
            header = self.headers[collision_entry.texture_header_index]
            compressed_block_infos = collision_entry.compressed_block_infos
        if self.header_size == 0x94:
            return header[:0x20] + compressed_block_infos + header[0x30:]
        return header


def load_pathc_collection(path: Path) -> PathcCollection:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
    cache_key = str(resolved).lower()
    cached = _PATHC_COLLECTION_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    collection = PathcCollection(resolved)
    _PATHC_COLLECTION_CACHE[cache_key] = (stamp, collection)
    return collection


def resolve_archive_meta_root(entry: ArchiveEntry) -> Path:
    return entry.pamt_path.parent.parent / "meta"


def resolve_archive_pathc_path(entry: ArchiveEntry) -> Path:
    return resolve_archive_meta_root(entry) / "0.pathc"


def get_archive_partial_dds_header(entry: ArchiveEntry) -> bytes:
    pathc_path = resolve_archive_pathc_path(entry)
    if not pathc_path.is_file():
        raise ValueError(f"Partial DDS metadata was not found: {pathc_path}")
    collection = load_pathc_collection(pathc_path)
    candidates = [
        entry.path.replace("\\", "/").lstrip("/"),
        PurePosixPath(entry.path.replace("\\", "/")).as_posix().lstrip("/"),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return collection.get_file_header(candidate)
        except KeyError:
            continue
    raise ValueError(f"Partial DDS header not found in {pathc_path} for {entry.path}")


def _dds_bytes_per_block(dxgi_format: int, four_cc: bytes) -> Optional[int]:
    block_8_formats = {71, 72, 80, 81}
    block_16_formats = {74, 75, 77, 78, 83, 84, 94, 95, 98, 99}
    if dxgi_format in block_8_formats:
        return 8
    if dxgi_format in block_16_formats:
        return 16
    four_cc_upper = four_cc.upper()
    if four_cc_upper in {b"DXT1", b"BC4U", b"BC4S", b"ATI1"}:
        return 8
    if four_cc_upper in {b"DXT3", b"DXT5", b"BC5U", b"BC5S", b"ATI2", b"RXGB"}:
        return 16
    return None


def _dds_surface_size(width: int, height: int, dxgi_format: int, four_cc: bytes) -> int:
    bytes_per_block = _dds_bytes_per_block(dxgi_format, four_cc)
    if bytes_per_block is None:
        raise ValueError(f"Unsupported DDS partial compression format: DXGI={dxgi_format} FOURCC={four_cc!r}")
    block_w = max(1, (max(1, width) + 3) // 4)
    block_h = max(1, (max(1, height) + 3) // 4)
    return block_w * block_h * bytes_per_block


def reconstruct_partial_dds(entry: ArchiveEntry, data: bytes) -> bytes:
    header = get_archive_partial_dds_header(entry)
    if len(header) < 0x80 or header[:4] != DDS_MAGIC:
        raise ValueError("Partial DDS header is missing or invalid.")
    (
        _header_size,
        _flags,
        height,
        width,
        _pitch_or_linear_size,
        depth,
        mip_map_count,
        *reserved1_and_rest,
    ) = struct.unpack_from("<IIIIIII11I", header, 4)
    reserved1 = reserved1_and_rest[:11]
    ddspf_four_cc = header[84:88]
    caps2 = struct.unpack_from("<I", header, 112)[0]
    is_dx10 = ddspf_four_cc == b"DX10"
    header_size = 0x94 if is_dx10 else 0x80
    dxgi_format = struct.unpack_from("<I", header, 0x80)[0] if is_dx10 and len(header) >= 0x94 else 0
    dx10_array_size = struct.unpack_from("<I", header, 0x8C)[0] if is_dx10 and len(header) >= 0x94 else 1

    multi_chunk_supported_0 = dx10_array_size < 2 if is_dx10 else True
    multi_chunk_supported_1 = mip_map_count > 5 and (caps2 == 0 and depth < 2)
    use_single_chunk = not multi_chunk_supported_0 or not multi_chunk_supported_1

    if use_single_chunk:
        compressed_block_sizes = [reserved1[0]]
        decompressed_block_sizes = [reserved1[1]]
    else:
        compressed_block_sizes = list(reserved1[:4])
        decompressed_block_sizes: List[int] = []
        current_width = max(1, width)
        current_height = max(1, height)
        for _ in range(min(4, max(1, mip_map_count))):
            decompressed_block_sizes.append(_dds_surface_size(current_width, current_height, dxgi_format, ddspf_four_cc))
            current_width = max(1, current_width >> 1)
            current_height = max(1, current_height >> 1)

    current_data_offset = header_size
    output_data = bytearray(header[:header_size])
    for compressed_size, decompressed_size in zip(compressed_block_sizes, decompressed_block_sizes):
        if compressed_size <= 0 or decompressed_size <= 0:
            continue
        if compressed_size == decompressed_size:
            block = data[current_data_offset : current_data_offset + decompressed_size]
            if len(block) != decompressed_size:
                raise ValueError("Partial DDS block is truncated.")
            output_data.extend(block)
            current_data_offset += decompressed_size
            continue
        if lz4_block is None:
            raise ValueError("This entry uses Partial DDS reconstruction, but the lz4 Python package is not installed.")
        compressed_data = data[current_data_offset : current_data_offset + compressed_size]
        if len(compressed_data) != compressed_size:
            raise ValueError("Partial DDS block is truncated.")
        output_data.extend(lz4_block.decompress(compressed_data, uncompressed_size=decompressed_size))
        current_data_offset += compressed_size
    if current_data_offset < len(data):
        output_data.extend(data[current_data_offset:])
    return bytes(output_data)


def sanitize_archive_entry_output_path(entry: ArchiveEntry, output_root: Path) -> Path:
    pure_path = PurePosixPath(entry.path.replace("\\", "/"))
    safe_parts = [part for part in pure_path.parts if part not in {"", ".", ".."}]
    if not safe_parts:
        raise ValueError(f"Archive entry has an invalid path: {entry.path}")
    package_root = entry.pamt_path.parent.name.strip() or "package"
    return output_root.joinpath(package_root, *safe_parts)


def find_available_output_path(target_path: Path, reserved_paths: Optional[set[str]] = None) -> Path:
    reserved = reserved_paths or set()
    if str(target_path).lower() not in reserved and not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        lowered = str(candidate).lower()
        if lowered not in reserved and not candidate.exists():
            return candidate
        counter += 1


def read_archive_entry_raw_data(entry: ArchiveEntry) -> bytes:
    if not entry.paz_file.exists():
        raise ValueError(f"Missing PAZ file: {entry.paz_file}")

    read_size = entry.comp_size if entry.compressed else entry.orig_size
    with entry.paz_file.open("rb") as handle:
        handle.seek(entry.offset)
        data = handle.read(read_size)
    return data


def maybe_reconstruct_sparse_dds(entry: ArchiveEntry, data: bytes) -> Optional[Tuple[bytes, str]]:
    if entry.extension != ".dds":
        return None
    if not data.startswith(DDS_MAGIC):
        return None
    if len(data) >= entry.orig_size:
        return None
    padded = data + (b"\x00" * (entry.orig_size - len(data)))
    return padded, "SparseDDS"


def read_archive_entry_data(entry: ArchiveEntry) -> Tuple[bytes, bool, str]:
    data = read_archive_entry_raw_data(entry)

    decompressed = False
    note = ""
    if entry.compressed:
        if entry.compression_type == 1 and entry.extension == ".dds":
            data = reconstruct_partial_dds(entry, data)
            decompressed = True
            note = "PartialDDS"
        elif entry.compression_type == 2:
            if lz4_block is None:
                raise ValueError("This entry uses LZ4 compression, but the lz4 Python package is not installed.")
            data = lz4_block.decompress(data, uncompressed_size=entry.orig_size)
            decompressed = True
            note = "LZ4"
        else:
            reconstructed = maybe_reconstruct_sparse_dds(entry, data)
            if reconstructed is not None:
                data, note = reconstructed
            else:
                raise ValueError(f"Unsupported archive compression type {entry.compression_type} for {entry.path}")

    return data, decompressed, note


def extract_archive_entry(
    entry: ArchiveEntry,
    output_root: Path,
) -> Tuple[Path, bool, str]:
    data, decompressed, note = read_archive_entry_data(entry)
    out_path = output_root
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path, decompressed, note


def extract_archive_entries(
    entries: Sequence[ArchiveEntry],
    output_root: Path,
    *,
    collision_mode: str = "overwrite",
    on_log: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, int]:
    output_root.mkdir(parents=True, exist_ok=True)
    total = len(entries)
    extracted = 0
    decompressed = 0
    failed = 0
    duplicate_targets: Dict[str, int] = defaultdict(int)
    renamed = 0
    used_targets: set[str] = set()

    for entry in entries:
        try:
            target_path = sanitize_archive_entry_output_path(entry, output_root)
            duplicate_targets[str(target_path).lower()] += 1
        except Exception:
            continue

    duplicate_count = sum(1 for count in duplicate_targets.values() if count > 1)
    if duplicate_count and on_log:
        on_log(
            f"Warning: {duplicate_count} extracted path(s) are duplicated across selected archive entries. "
            "Later entries will overwrite earlier extracted files."
        )

    for index, entry in enumerate(entries, start=1):
        raise_if_cancelled(stop_event)
        try:
            target_path = sanitize_archive_entry_output_path(entry, output_root)
            if collision_mode == "rename":
                resolved_path = find_available_output_path(target_path, used_targets)
                if resolved_path != target_path:
                    renamed += 1
            else:
                resolved_path = target_path
            used_targets.add(str(resolved_path).lower())
            out_path, was_decompressed, note = extract_archive_entry(entry, resolved_path)
            extracted += 1
            if was_decompressed:
                decompressed += 1
            if on_log:
                flags = []
                if note and note not in flags:
                    flags.append(note)
                elif was_decompressed:
                    flags.append("Decompressed")
                if collision_mode == "rename" and out_path != target_path:
                    flags.append("Renamed")
                extra = f" [{' '.join(flags)}]" if flags else ""
                on_log(f"[{index}/{total}] EXTRACT {entry.path}{extra} -> {out_path}")
        except Exception as exc:
            failed += 1
            if on_log:
                on_log(f"[{index}/{total}] FAIL {entry.path} -> {exc}")

    return {
        "total": total,
        "extracted": extracted,
        "decompressed": decompressed,
        "renamed": renamed,
        "failed": failed,
    }


def directory_has_contents(path: Path) -> bool:
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def clear_directory_contents(path: Path) -> None:
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing to clear root directory: {resolved}")
    for child in resolved.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def count_existing_archive_targets(entries: Sequence[ArchiveEntry], output_root: Path) -> int:
    return sum(1 for entry in entries if sanitize_archive_entry_output_path(entry, output_root).exists())


def format_byte_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    units = ("KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        value /= 1024.0
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{size} B"


def sanitize_cache_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\\\|?*]+', "_", name).strip(" .")
    return sanitized or "preview.bin"


def build_archive_entry_metadata_summary(entry: ArchiveEntry) -> str:
    flags: List[str] = []
    if entry.compressed:
        flags.append(entry.compression_label)
    if entry.encrypted:
        flags.append("Encrypted")
    flags_text = f" | {' | '.join(flags)}" if flags else ""
    return (
        f"{entry.extension or 'no extension'} | {format_byte_size(entry.orig_size)}"
        f" | Stored {format_byte_size(entry.comp_size)}{flags_text}"
    )


def build_archive_entry_detail_text(entry: ArchiveEntry, extra_detail: str = "") -> str:
    lines = [
        f"Path: {entry.path}",
        f"Package: {entry.package_label}",
        f"PAMT: {entry.pamt_path}",
        f"PAZ: {entry.paz_file}",
        f"Offset: {entry.offset:,}",
        f"Original size: {entry.orig_size:,} bytes ({format_byte_size(entry.orig_size)})",
        f"Stored size: {entry.comp_size:,} bytes ({format_byte_size(entry.comp_size)})",
        f"Compression: {entry.compression_label}",
        f"Encrypted: {'Yes' if entry.encrypted else 'No'}",
    ]
    if extra_detail.strip():
        lines.extend(["", extra_detail.strip()])
    return "\n".join(lines)


def ensure_archive_preview_source(entry: ArchiveEntry) -> Tuple[Path, str]:
    try:
        pamt_stat = entry.pamt_path.stat()
        pamt_stamp = f"{pamt_stat.st_size}:{pamt_stat.st_mtime_ns}"
    except OSError:
        pamt_stamp = "missing"
    try:
        paz_stat = entry.paz_file.stat()
        paz_stamp = f"{paz_stat.st_size}:{paz_stat.st_mtime_ns}"
    except OSError:
        paz_stamp = "missing"
    pathc_stamp = ""
    if entry.extension == ".dds" and entry.compression_type == 1:
        try:
            pathc_path = resolve_archive_pathc_path(entry)
            pathc_stat = pathc_path.stat()
            pathc_stamp = f"|{pathc_path.resolve()}|{pathc_stat.st_size}:{pathc_stat.st_mtime_ns}"
        except OSError:
            pathc_stamp = "|missing_pathc"

    cache_key = hashlib.sha256(
        (
            f"{entry.path}|{entry.pamt_path.resolve()}|{pamt_stamp}|{entry.paz_file.resolve()}|{paz_stamp}|"
            f"{entry.offset}|{entry.comp_size}|{entry.orig_size}|{entry.flags}{pathc_stamp}"
        ).encode("utf-8")
    ).hexdigest()
    suffix = Path(entry.path).suffix or ".bin"
    filename = sanitize_cache_filename(f"{Path(entry.path).stem}{suffix}")
    cache_dir = Path(tempfile.gettempdir()) / APP_NAME / "archive_preview_cache" / cache_key
    target_path = cache_dir / filename
    if target_path.exists() and target_path.stat().st_size > 0:
        note_path = cache_dir / ".note"
        note = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        return target_path, note

    cache_dir.mkdir(parents=True, exist_ok=True)
    data, _decompressed, note = read_archive_entry_data(entry)
    target_path.write_bytes(data)
    if note:
        (cache_dir / ".note").write_text(note, encoding="utf-8")
    return target_path, note


def iter_archive_loose_file_candidates(
    entry: ArchiveEntry,
    search_roots: Sequence[Path],
) -> Sequence[Path]:
    pure_path = PurePosixPath(entry.path.replace("\\", "/"))
    safe_parts = [part for part in pure_path.parts if part not in {"", ".", ".."}]
    if not safe_parts:
        return []

    package_root = entry.pamt_path.parent.name.strip()
    candidates: List[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:
            continue
        if not resolved_root.exists() or not resolved_root.is_dir():
            continue
        root_candidates = [resolved_root.joinpath(*safe_parts)]
        if package_root:
            root_candidates.append(resolved_root.joinpath(package_root, *safe_parts))
        for candidate in root_candidates:
            lowered = str(candidate).lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            if candidate.exists() and candidate.is_file():
                candidates.append(candidate)
    return candidates


def build_loose_archive_preview_assets(
    texconv_path: Optional[Path],
    loose_path: Path,
) -> Tuple[str, str, str]:
    resolved_path = loose_path.expanduser().resolve()
    suffix = resolved_path.suffix.lower()
    detail = f"Loose file preview from: {resolved_path}"

    if suffix == ".dds":
        try:
            dds_info = parse_dds(resolved_path)
            metadata_summary = (
                f"Loose DDS | Format: {dds_info.texconv_format} | "
                f"Size: {dds_info.width}x{dds_info.height} | Mips: {dds_info.mip_count}"
            )
        except Exception:
            metadata_summary = f"Loose DDS | {resolved_path.name}"
        if texconv_path is None:
            return "", metadata_summary, detail + "\nSet texconv.exe to enable DDS loose-file previews."
        preview_png = ensure_dds_preview_png(texconv_path.resolve(), resolved_path)
        return str(preview_png), metadata_summary, detail

    if suffix in ARCHIVE_IMAGE_EXTENSIONS:
        return str(resolved_path), f"Loose image | {resolved_path.name}", detail

    return "", f"Loose file | {resolved_path.name}", detail + "\nThis loose file type cannot be previewed as an image."


def format_binary_header_preview(data: bytes) -> str:
    if not data:
        return "No bytes available."
    lines: List[str] = []
    for offset in range(0, min(len(data), ARCHIVE_BINARY_HEX_PREVIEW_LIMIT), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{value:02X}" for value in chunk)
        ascii_part = "".join(chr(value) if 32 <= value <= 126 else "." for value in chunk)
        lines.append(f"{offset:04X}  {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)


def summarize_obj_text(content: str) -> str:
    vertices = 0
    texcoords = 0
    normals = 0
    faces = 0
    for raw_line in content.splitlines():
        line = raw_line.lstrip()
        if line.startswith("v "):
            vertices += 1
        elif line.startswith("vt "):
            texcoords += 1
        elif line.startswith("vn "):
            normals += 1
        elif line.startswith("f "):
            faces += 1
    return f"OBJ summary: {vertices:,} vertices, {texcoords:,} UVs, {normals:,} normals, {faces:,} faces."


def build_archive_preview_result(
    texconv_path: Optional[Path],
    entry: Optional[ArchiveEntry],
    loose_search_roots: Optional[Sequence[Path]] = None,
) -> ArchivePreviewResult:
    if entry is None:
        return ArchivePreviewResult(
            status="missing",
            title="Archive Preview",
            metadata_summary="Nothing selected.",
            detail_text="Select an archive file or folder to preview it here.",
            preferred_view="info",
        )

    metadata_summary = build_archive_entry_metadata_summary(entry)
    extension = entry.extension
    loose_file_path = ""
    loose_preview_image_path = ""
    loose_preview_title = ""
    loose_preview_metadata_summary = ""
    loose_preview_detail_text = ""

    if loose_search_roots:
        loose_candidates = list(iter_archive_loose_file_candidates(entry, loose_search_roots))
        if loose_candidates:
            loose_candidate = loose_candidates[0]
            loose_file_path = str(loose_candidate)
            loose_preview_title = f"{entry.basename} (Loose file)"
            try:
                (
                    loose_preview_image_path,
                    loose_preview_metadata_summary,
                    loose_preview_detail_text,
                ) = build_loose_archive_preview_assets(texconv_path, loose_candidate)
            except Exception as exc:
                loose_preview_metadata_summary = f"Loose file | {loose_candidate.name}"
                loose_preview_detail_text = (
                    f"Loose file candidate found at {loose_candidate}, but preview failed: {exc}"
                )
            if len(loose_candidates) > 1:
                loose_preview_detail_text += (
                    f"\n\nAdditional loose candidates found: {len(loose_candidates) - 1}"
                )

    try:
        if extension == ".dds":
            source_path, note = ensure_archive_preview_source(entry)
            warning_badge = ""
            warning_text = ""
            extra_detail_parts: List[str] = []
            if note == "PartialDDS":
                extra_detail_parts.append(
                    "Type 1 DDS reconstructed successfully using meta/0.pathc partial-header metadata."
                )
            elif note == "SparseDDS":
                warning_badge = "Type 1 DDS: Unsupported Preview"
                warning_text = (
                    "This archive DDS is stored as truncated type 1 data. "
                    "The image shown here is a padded best-effort preview and may be corrupted, noisy, or incomplete."
                )
                extra_detail_parts.append(warning_text)
                if loose_file_path:
                    extra_detail_parts.append(f"Loose file candidate found: {loose_file_path}")
            if texconv_path is None:
                return ArchivePreviewResult(
                    status="missing",
                    title=entry.basename,
                    metadata_summary=metadata_summary,
                    detail_text=build_archive_entry_detail_text(
                        entry,
                        "\n".join(
                            part
                            for part in [
                                "Set texconv.exe in the Workflow tab to enable DDS image previews.",
                                *extra_detail_parts,
                            ]
                            if part
                        ),
                    ),
                    preferred_view="info",
                    warning_badge=warning_badge,
                    warning_text=warning_text,
                    loose_file_path=loose_file_path,
                    loose_preview_image_path=loose_preview_image_path,
                    loose_preview_title=loose_preview_title,
                    loose_preview_metadata_summary=loose_preview_metadata_summary,
                    loose_preview_detail_text=loose_preview_detail_text,
                )
            preview_png = ensure_dds_preview_png(texconv_path.resolve(), source_path.resolve())
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(entry, "\n\n".join(extra_detail_parts)),
                preview_image_path=str(preview_png),
                preferred_view="image",
                warning_badge=warning_badge,
                warning_text=warning_text,
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in ARCHIVE_IMAGE_EXTENSIONS:
            source_path, note = ensure_archive_preview_source(entry)
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(
                    entry,
                    "Preview fallback: sparse DDS padding was applied." if note == "SparseDDS" else "",
                ),
                preview_image_path=str(source_path),
                preferred_view="image",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        data, _decompressed, note = read_archive_entry_data(entry)
        if entry.encrypted and extension == ".xml":
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(
                    entry,
                    "This XML entry appears to be encrypted. Raw preview is not shown here.",
                ),
                preferred_view="info",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in ARCHIVE_TEXT_EXTENSIONS:
            preview_bytes = data[:ARCHIVE_TEXT_PREVIEW_LIMIT]
            text = preview_bytes.decode("utf-8", errors="replace")
            extra_note = ""
            if len(data) > ARCHIVE_TEXT_PREVIEW_LIMIT:
                extra_note = f"\n\nPreview truncated to {format_byte_size(ARCHIVE_TEXT_PREVIEW_LIMIT)}."
            if extension == ".obj":
                summary_text = summarize_obj_text(text)
                extra_note = f"{summary_text}{extra_note}"
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(
                    entry,
                    "\n".join(part for part in [("Preview fallback: sparse DDS padding was applied." if note == "SparseDDS" else ""), extra_note] if part),
                ),
                preview_text=text,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        info_extra = ""
        if note == "SparseDDS":
            info_extra = "Preview fallback: sparse DDS padding was applied."
        if extension in ARCHIVE_MODEL_EXTENSIONS:
            info_extra = "\n".join(part for part in [info_extra, "Visual preview is not available for this model format yet."] if part)
        header_preview = format_binary_header_preview(data[:ARCHIVE_BINARY_HEX_PREVIEW_LIMIT])
        detail_text = build_archive_entry_detail_text(
            entry,
            f"{info_extra}\n\nBinary header preview:\n{header_preview}".strip(),
        )
        return ArchivePreviewResult(
            status="ok",
            title=entry.basename,
            metadata_summary=metadata_summary,
            detail_text=detail_text,
            preferred_view="info",
            loose_file_path=loose_file_path,
            loose_preview_image_path=loose_preview_image_path,
            loose_preview_title=loose_preview_title,
            loose_preview_metadata_summary=loose_preview_metadata_summary,
            loose_preview_detail_text=loose_preview_detail_text,
        )
    except Exception as exc:
        return ArchivePreviewResult(
            status="error",
            title=entry.basename,
            metadata_summary=metadata_summary,
            detail_text=build_archive_entry_detail_text(entry, f"Preview failed: {exc}"),
            preferred_view="info",
            loose_file_path=loose_file_path,
            loose_preview_image_path=loose_preview_image_path,
            loose_preview_title=loose_preview_title,
            loose_preview_metadata_summary=loose_preview_metadata_summary,
            loose_preview_detail_text=loose_preview_detail_text,
        )

