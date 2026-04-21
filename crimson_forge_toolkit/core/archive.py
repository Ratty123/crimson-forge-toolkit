from __future__ import annotations

import fnmatch
import hashlib
import math
import os
import pickle
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
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

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
except ImportError:
    Cipher = None
    algorithms = None

from crimson_forge_toolkit.constants import *
from crimson_forge_toolkit.models import *
from crimson_forge_toolkit.core.common import *
from crimson_forge_toolkit.core.model_preview import (
    build_pam_model_preview,
    build_pamlod_model_preview,
    ensure_model_preview_is_reasonable,
)
from crimson_forge_toolkit.core.archive_modding import (
    build_hkx_preview,
    build_mesh_preview_from_bytes,
    build_pab_preview,
)
from crimson_forge_toolkit.core.pipeline import ensure_dds_display_preview_png, parse_dds
from crimson_forge_toolkit.core.upscale_profiles import (
    classify_texture_type,
    derive_texture_group_key,
    infer_texture_semantics,
    normalize_texture_reference_for_sidecar_lookup,
    parse_texture_sidecar_bindings,
)

_PATHC_COLLECTION_CACHE: Dict[str, Tuple[str, "PathcCollection"]] = {}
_ARCHIVE_SCAN_CACHE_MAGIC = b"CTFARCH1"
_ARCHIVE_SCAN_CACHE_VERSION = 2
_ARCHIVE_SCAN_CACHE_LEGACY_DIRNAMES: Tuple[str, ...] = ("cache", "archive_scan_cache")
_MODEL_TEXTURE_DISPLAY_PREVIEW_MAX_DIMENSION = 4096
_MODEL_TEXTURE_VISIBLE_FAMILY_SUFFIXES: Tuple[str, ...] = (
    "",
    "_ct",
    "_color",
    "_col",
    "_albedo",
    "_basecolor",
    "_base_color",
    "_diffuse",
)


@dataclass(slots=True)
class _ArchiveModelSidecarTextureBinding:
    texture_path: str
    parameter_name: str = ""
    submesh_name: str = ""
    sidecar_path: str = ""


@dataclass(slots=True)
class _StructuredBinaryPreviewBundle:
    preview_text: str
    detail_lines: Tuple[str, ...] = ()
    related_references: Tuple[ArchiveModelTextureReference, ...] = ()
    metadata_label: str = ""

_COMMON_TECHNICAL_DDS_EXCLUDE_PATTERNS: Tuple[str, ...] = (
    "*_n.dds",
    "*_nm.dds",
    "*_nrm.dds",
    "*_normal.dds",
    "*_normalmap.dds",
    "*_sp.dds",
    "*_spec.dds",
    "*_specular.dds",
    "*_m.dds",
    "*_mask.dds",
    "*_orm.dds",
    "*_rma.dds",
    "*_mra.dds",
    "*_arm.dds",
    "*_ao.dds",
    "*_metal.dds",
    "*_metallic.dds",
    "*_rough.dds",
    "*_roughness.dds",
    "*_gloss.dds",
    "*_smooth.dds",
    "*_height.dds",
    "*_hgt.dds",
    "*_disp.dds",
    "*_displacement.dds",
    "*_dmap.dds",
    "*_bump.dds",
    "*_parallax.dds",
    "*_pom.dds",
    "*_ssdm.dds",
    "*_vector.dds",
    "*_dr.dds",
    "*_op.dds",
    "*_wn.dds",
    "*_flow.dds",
    "*_velocity.dds",
    "*_pos.dds",
    "*_position.dds",
    "*_pivot.dds",
    "*_depth.dds",
    "*_pivotpos.dds",
    "*_ma.dds",
    "*_mg.dds",
    "*_o.dds",
    "*_emi.dds",
    "*_emc.dds",
    "*_subsurface.dds",
    "*_1bit.dds",
    "*_mask_amg.dds",
    "*_d.dds",
)
_STRUCTURED_BINARY_IDENTIFIER_RE = re.compile(r"^[_A-Za-z][A-Za-z0-9_:<>-]{2,127}$")
_STRUCTURED_BINARY_ASSET_REFERENCE_RE = re.compile(
    r"(?i)(?:[a-z0-9_./-]+(?:/[a-z0-9_./-]+)*\.(?:dds|xml|pami|meshinfo|hkx|pam|pamlod|pac|pab|paa|pae|paem|paseq|paschedule|paschedulepath|pastage|prefab|seqmt|wem|bnk|mp4|bk2|json))"
)
_ARCHIVE_STRUCTURED_BINARY_PREVIEW_EXTENSIONS: Tuple[str, ...] = (
    ".bnk",
    ".binarygimmick",
    ".hkx",
    ".levelinfo",
    ".meshinfo",
    ".motionblending",
    ".paa",
    ".pae",
    ".paa_metabin",
    ".pabgb",
    ".paem",
    ".pagbg",
    ".pampg",
    ".paseq",
    ".paschedule",
    ".paschedulepath",
    ".pastage",
    ".uianiminit",
    ".pamlod",
    ".prefab",
    ".seqmt",
    ".wem",
)
_ARCHIVE_SCAN_CACHE_SUPPORTED_VERSIONS = {1, 2}
CHACHA20_HASH_INITVAL = 0x000C5EDE
CHACHA20_IV_XOR = 0x60616263
CHACHA20_XOR_DELTAS = (
    0x00000000,
    0x0A0A0A0A,
    0x0C0C0C0C,
    0x06060606,
    0x0E0E0E0E,
    0x0A0A0A0A,
    0x06060606,
    0x02020202,
)
_PRINTABLE_BINARY_STRING_RE = re.compile(rb"[\x20-\x7E]{4,}")
_TEXT_DDS_REFERENCE_RE = re.compile(r"[A-Za-z0-9_./\\-]{3,255}\.dds", re.IGNORECASE)

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


def hashlittle(data: bytes, initval: int = 0) -> int:
    length = len(data)
    remaining = length
    a = b = c = _add32(0xDEADBEEF + length, initval)
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

    tail = data[offset:] + (b"\x00" * 12)
    if remaining >= 12:
        c = _add32(c, struct.unpack_from("<I", tail, 8)[0])
    elif remaining >= 9:
        c = _add32(c, struct.unpack_from("<I", tail, 8)[0] & (0xFFFFFFFF >> (8 * (12 - remaining))))
    if remaining >= 8:
        b = _add32(b, struct.unpack_from("<I", tail, 4)[0])
    elif remaining >= 5:
        b = _add32(b, struct.unpack_from("<I", tail, 4)[0] & (0xFFFFFFFF >> (8 * (8 - remaining))))
    if remaining >= 4:
        a = _add32(a, struct.unpack_from("<I", tail, 0)[0])
    elif remaining >= 1:
        a = _add32(a, struct.unpack_from("<I", tail, 0)[0] & (0xFFFFFFFF >> (8 * (4 - remaining))))
    elif remaining == 0:
        return c

    c = _sub32(c ^ b, _rot32(b, 14))
    a = _sub32(a ^ c, _rot32(c, 11))
    b = _sub32(b ^ a, _rot32(a, 25))
    c = _sub32(c ^ b, _rot32(b, 16))
    a = _sub32(a ^ c, _rot32(c, 4))
    b = _sub32(b ^ a, _rot32(a, 14))
    c = _sub32(c ^ b, _rot32(b, 24))
    return c


def derive_chacha20_key_iv(filename: str) -> Tuple[bytes, bytes]:
    basename = Path(filename).name.lower().encode("utf-8", errors="replace")
    seed = hashlittle(basename, CHACHA20_HASH_INITVAL)
    nonce = struct.pack("<I", seed) * 4
    key_base = seed ^ CHACHA20_IV_XOR
    key = b"".join(struct.pack("<I", key_base ^ delta) for delta in CHACHA20_XOR_DELTAS)
    return key, nonce


def crypt_chacha20_filename(data: bytes, filename: str) -> bytes:
    if Cipher is None or algorithms is None:
        raise ValueError(
            "ChaCha20 support requires the cryptography package. Install it with: pip install cryptography"
        )
    key, nonce = derive_chacha20_key_iv(filename)
    cipher = Cipher(algorithms.ChaCha20(key, nonce), mode=None)
    return cipher.encryptor().update(data)


def _looks_like_plain_text_payload(data: bytes) -> bool:
    return try_decode_text_like_archive_data(data) is not None


def _looks_like_structured_binary_payload(extension: str, data: bytes) -> bool:
    head4 = data[:4]
    if extension == ".dds" and data.startswith(DDS_MAGIC):
        return True
    if head4 in {b"PAR ", b"PARC"}:
        return True
    if len(data) >= 16 and data[4:8] == b"TAG0" and data[12:16] == b"SDKV":
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return True
    return len(extract_binary_strings(data, sample_limit=16_384, max_strings=10)) >= 3


def _looks_like_decrypted_payload(entry: ArchiveEntry, data: bytes) -> bool:
    candidate = data
    if entry.compression_type == 2:
        if lz4_block is None:
            return False
        try:
            candidate = lz4_block.decompress(data, uncompressed_size=entry.orig_size)
        except Exception:
            return False
    elif entry.compression_type == 1 and entry.extension == ".dds":
        try:
            candidate = reconstruct_partial_dds(entry, data)
        except Exception:
            return False
    if _looks_like_plain_text_payload(candidate):
        return True
    if entry.extension in _ARCHIVE_STRUCTURED_BINARY_PREVIEW_EXTENSIONS or entry.extension in ARCHIVE_MODEL_EXTENSIONS:
        return _looks_like_structured_binary_payload(entry.extension, candidate)
    return entry.extension == ".dds" and candidate.startswith(DDS_MAGIC)


def try_decrypt_archive_entry_data(entry: ArchiveEntry, data: bytes) -> Tuple[bytes, Optional[str]]:
    if not entry.encrypted:
        return data, None
    if entry.encryption_type != 3:
        raise ValueError(f"Unsupported archive encryption type {entry.encryption_type} for {entry.path}")
    candidate = crypt_chacha20_filename(data, entry.basename)
    if not _looks_like_decrypted_payload(entry, candidate):
        raise ValueError(f"ChaCha20 decryption validation failed for {entry.path}")
    return candidate, "ChaCha20"

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


def _candidate_archive_scan_cache_paths(package_root: Path, cache_root: Path) -> List[Path]:
    try:
        resolved_cache_root = cache_root.expanduser().resolve()
    except OSError:
        resolved_cache_root = cache_root.expanduser()

    root_candidates = [resolved_cache_root]
    sibling_parent = resolved_cache_root.parent
    for dirname in _ARCHIVE_SCAN_CACHE_LEGACY_DIRNAMES:
        root_candidates.append(sibling_parent / dirname)

    cache_paths: List[Path] = []
    seen: set[str] = set()
    for candidate_root in root_candidates:
        normalized_root = str(candidate_root).strip()
        if not normalized_root:
            continue
        lowered_root = normalized_root.lower()
        if lowered_root in seen:
            continue
        seen.add(lowered_root)
        cache_paths.append(resolve_archive_scan_cache_path(package_root, candidate_root))
    return cache_paths


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
    resolved_base_dir = base_dir.resolve()
    pamt_rel_cache: Dict[Path, str] = {}

    rows = []
    total_entries = len(entries)
    update_every = 50_000 if total_entries >= 500_000 else 10_000 if total_entries >= 100_000 else 2_000
    for index, entry in enumerate(entries, start=1):
        raise_if_cancelled(stop_event)
        pamt_rel_text = pamt_rel_cache.get(entry.pamt_path)
        if pamt_rel_text is None:
            try:
                pamt_rel_text = entry.pamt_path.resolve().relative_to(resolved_base_dir).as_posix()
            except (OSError, ValueError):
                pamt_rel_text = entry.pamt_path.name
            pamt_rel_cache[entry.pamt_path] = pamt_rel_text
        rows.append(
            (
                entry.path,
                pamt_rel_text,
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
    candidate_paths = _candidate_archive_scan_cache_paths(package_root, cache_root)
    preferred_cache_path = candidate_paths[0]
    existing_candidate_paths = [candidate for candidate in candidate_paths if candidate.exists()]
    if not existing_candidate_paths:
        return None

    if on_progress:
        on_progress(0, 0, "Checking archive cache...")
    try:
        base_dir, current_sources = _collect_archive_scan_sources(package_root)
    except Exception as exc:
        if on_log:
            on_log(f"Archive cache check failed; will rescan instead: {exc}")
        return None

    last_failure_message = "Archive cache is unavailable; performing a full rescan."
    for cache_path in existing_candidate_paths:
        cache_label = "archive cache" if cache_path == preferred_cache_path else f"legacy archive cache at {cache_path.parent}"
        if cache_path != preferred_cache_path and on_log:
            on_log(f"Trying {cache_label}: {cache_path.name}")

        try:
            data = _deserialize_archive_scan_cache_payload(cache_path.read_bytes())
        except Exception as exc:
            last_failure_message = f"{cache_label.capitalize()} could not be read; will try another cache or rescan: {exc}"
            if on_log:
                on_log(last_failure_message)
            continue

        if int(data.get("version", 0)) not in _ARCHIVE_SCAN_CACHE_SUPPORTED_VERSIONS:
            last_failure_message = f"{cache_label.capitalize()} format changed; will try another cache or rescan."
            if on_log:
                on_log(last_failure_message)
            continue

        cached_sources = data.get("sources")
        if not isinstance(cached_sources, list):
            last_failure_message = f"{cache_label.capitalize()} is missing source metadata; will try another cache or rescan."
            if on_log:
                on_log(last_failure_message)
            continue

        if cached_sources != current_sources:
            last_failure_message = f"{cache_label.capitalize()} is out of date; archive indexes changed since the last scan."
            if on_log:
                on_log(last_failure_message)
            continue

        raw_rows = data.get("rows")
        if not isinstance(raw_rows, list):
            last_failure_message = f"{cache_label.capitalize()} is missing entry rows; will try another cache or rescan."
            if on_log:
                on_log(last_failure_message)
            continue

        total_rows = len(raw_rows)
        if on_log:
            on_log(f"Loading {total_rows:,} archive entries from cache...")
        if total_rows == 0:
            if on_progress:
                on_progress(1, 1, "Archive cache loaded. No entries were cached.")
            return []

        try:
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
        except Exception as exc:
            last_failure_message = f"{cache_label.capitalize()} could not be loaded; will try another cache or rescan: {exc}"
            if on_log:
                on_log(last_failure_message)
            continue

        if cache_path != preferred_cache_path:
            try:
                preferred_cache_path.parent.mkdir(parents=True, exist_ok=True)
                if not preferred_cache_path.exists():
                    shutil.copy2(cache_path, preferred_cache_path)
                if on_log:
                    on_log(f"Migrated archive cache to preferred location: {preferred_cache_path}")
            except Exception as exc:
                if on_log:
                    on_log(f"Loaded archive cache from legacy location, but migration failed: {exc}")

        if on_log:
            on_log(f"Loaded {len(entries):,} archive entries from cache.")
        return entries

    if on_log:
        on_log(last_failure_message)
    return None


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
        "crimson_forge_toolkit_PACKAGE_ROOT",
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
    paz_files = [resolved_paz_dir / f"{paz_indices[index]}.paz" for index in range(len(paz_indices))]

    entries: List[ArchiveEntry] = []
    folder_cursor = 0
    for entry_index, (name_offset, paz_offset, comp_size, orig_size, paz_index, flags) in enumerate(files):
        relative_path = resolver.get_full_path(name_offset).replace("\\", "/").strip("/")
        guessed_dir = ""
        while folder_cursor < len(folder_ranges) and entry_index >= folder_ranges[folder_cursor][1]:
            folder_cursor += 1
        if folder_cursor < len(folder_ranges):
            start, end, candidate_dir = folder_ranges[folder_cursor]
            if start <= entry_index < end:
                guessed_dir = candidate_dir
        full_path = f"{guessed_dir}/{relative_path}".strip("/") if guessed_dir else relative_path
        if paz_index >= len(paz_files):
            raise ValueError(f"Invalid PAZ index {paz_index} for {pamt_path}")
        entries.append(
            ArchiveEntry(
                path=full_path,
                pamt_path=pamt_path,
                paz_file=paz_files[paz_index],
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
    extension = entry.extension

    if extension in ARCHIVE_MODEL_EXTENSIONS or extension in {".hkx"}:
        return "model"
    if extension in ARCHIVE_VIDEO_EXTENSIONS:
        return "video"
    if extension in ARCHIVE_AUDIO_EXTENSIONS:
        return "audio"
    if "/ui/" in path_lower or entry.basename.lower().startswith("ui_"):
        return "ui"
    if "impostor" in path_lower:
        return "impostor"
    if extension in ARCHIVE_IMAGE_EXTENSIONS or "/texture/" in path_lower:
        texture_type = classify_texture_type(entry.path)
        if texture_type == "normal":
            return "normal"
        if texture_type in {"mask", "roughness", "height", "vector", "emissive"}:
            return "material"
        return "image"
    if extension in ARCHIVE_TEXT_EXTENSIONS:
        return "text"
    return "other"


def archive_entry_is_previewable(entry: ArchiveEntry) -> bool:
    extension = entry.extension
    return (
        extension in ARCHIVE_IMAGE_EXTENSIONS
        or extension in ARCHIVE_AUDIO_EXTENSIONS
        or extension in ARCHIVE_VIDEO_EXTENSIONS
        or extension in ARCHIVE_TEXT_EXTENSIONS
        or extension in ARCHIVE_MODEL_EXTENSIONS
        or extension in _ARCHIVE_STRUCTURED_BINARY_PREVIEW_EXTENSIONS
    )


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


def _split_archive_filter_patterns(text: str) -> Tuple[str, ...]:
    if not text:
        return ()
    raw_parts = re.split(r"[;\r\n,]+", text)
    parts = [part.strip().lower() for part in raw_parts if part and part.strip()]
    return tuple(parts)


def _archive_entry_matches_text_pattern(path_lower: str, basename_lower: str, pattern: str) -> bool:
    if not pattern:
        return False
    if any(char in pattern for char in "*?[]"):
        return fnmatch.fnmatch(path_lower, pattern) or fnmatch.fnmatch(basename_lower, pattern)
    return pattern in path_lower or pattern in basename_lower


def filter_archive_entries(
    entries: Sequence[ArchiveEntry],
    *,
    filter_text: str,
    exclude_filter_text: str,
    extension_filter: str,
    package_filter_text: str,
    structure_filter: str,
    role_filter: str,
    exclude_common_technical_suffixes: bool,
    min_size_kb: int,
    previewable_only: bool,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[ArchiveEntry]:
    normalized_extension = normalize_archive_extension_filter(extension_filter)
    text = filter_text.strip().lower()
    include_patterns = _split_archive_filter_patterns(text)
    wildcard_pattern = include_patterns[0] if include_patterns else ""
    wildcard_filter = len(include_patterns) == 1 and any(char in include_patterns[0] for char in "*?[]")
    exclude_patterns = list(_split_archive_filter_patterns(exclude_filter_text))
    if exclude_common_technical_suffixes:
        exclude_patterns.extend(_COMMON_TECHNICAL_DDS_EXCLUDE_PATTERNS)
    package_filter = package_filter_text.strip().lower()
    min_size_bytes = min_size_kb * 1024 if min_size_kb > 0 else 0
    normalized_structure = normalize_archive_structure_filter_value(structure_filter)
    normalized_role = role_filter.strip().lower()
    require_role = bool(normalized_role and normalized_role != "all")
    total_entries = len(entries)
    progress_total = max(total_entries, 1)
    update_every = 50_000 if total_entries >= 500_000 else 10_000 if total_entries >= 100_000 else 2_000

    if on_progress:
        on_progress(0 if total_entries > 0 else 1, progress_total, f"Applying archive filters... 0 / {total_entries:,} entries")

    filtered: List[ArchiveEntry] = []
    for index, entry in enumerate(entries, start=1):
        if stop_event is not None and (index == 1 or index % 2048 == 0):
            raise_if_cancelled(stop_event)
        if normalized_extension and normalized_extension not in {"*", "all", ".*"} and entry.extension != normalized_extension:
            matched = False
        else:
            matched = True

        if matched and text:
            path_lower = entry.path.lower()
            basename_lower = entry.basename.lower()
            if len(include_patterns) > 1:
                matched = any(_archive_entry_matches_text_pattern(path_lower, basename_lower, pattern) for pattern in include_patterns)
            elif wildcard_filter:
                matched = fnmatch.fnmatch(path_lower, wildcard_pattern) or fnmatch.fnmatch(basename_lower, wildcard_pattern)
            else:
                matched = text in path_lower or text in basename_lower

            if matched and exclude_patterns:
                matched = not any(
                    _archive_entry_matches_text_pattern(path_lower, basename_lower, pattern)
                    for pattern in exclude_patterns
                )
        elif matched and exclude_patterns:
            path_lower = entry.path.lower()
            basename_lower = entry.basename.lower()
            matched = not any(_archive_entry_matches_text_pattern(path_lower, basename_lower, pattern) for pattern in exclude_patterns)

        if matched and package_filter:
            package_label_lower = entry.package_label.lower()
            pamt_path_lower = str(entry.pamt_path).lower()
            matched = package_filter in package_label_lower or package_filter in pamt_path_lower

        if matched and min_size_bytes and entry.orig_size < min_size_bytes:
            matched = False

        if matched and previewable_only and not archive_entry_is_previewable(entry):
            matched = False

        if matched and normalized_structure and normalized_structure not in archive_entry_structure_prefixes(entry):
            matched = False

        if matched and require_role:
            entry_role = archive_entry_role(entry)
            if normalized_role == "texture":
                matched = entry_role in {"image", "normal", "material", "impostor", "ui"}
            else:
                matched = entry_role == normalized_role

        if matched:
            filtered.append(entry)

        if on_progress and (index == 1 or index % update_every == 0 or index == total_entries):
            on_progress(index, progress_total, f"Applying archive filters... {index:,} / {total_entries:,} entries")

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


def build_archive_entry_path_index(entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
    index: Dict[str, List[ArchiveEntry]] = {}
    for archive_entry in entries:
        normalized_path = archive_entry.path.replace("\\", "/").strip().lower()
        index.setdefault(normalized_path, []).append(archive_entry)
    return index


def build_archive_entry_basename_index(entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
    index: Dict[str, List[ArchiveEntry]] = {}
    for archive_entry in entries:
        basename = PurePosixPath(archive_entry.path.replace("\\", "/")).name.strip().lower()
        if not basename:
            continue
        index.setdefault(basename, []).append(archive_entry)
    return index


def build_archive_entry_extension_index(entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
    index: Dict[str, List[ArchiveEntry]] = {}
    for archive_entry in entries:
        extension = normalize_archive_extension_filter(archive_entry.extension)
        if not extension:
            continue
        index.setdefault(extension, []).append(archive_entry)
    return index


def build_archive_structure_children_map(entries: Sequence[ArchiveEntry]) -> Dict[str, List[Tuple[str, int]]]:
    child_counts: Dict[str, Dict[str, int]] = defaultdict(dict)
    folder_counts: Dict[Tuple[str, ...], int] = defaultdict(int)
    package_dir_cache: Dict[Path, str] = {}
    folder_parts_cache: Dict[str, Tuple[str, ...]] = {"": ()}

    for entry in entries:
        package_dir = package_dir_cache.get(entry.pamt_path)
        if package_dir is None:
            package_dir = entry.pamt_path.parent.name.strip().lower() or "package"
            package_dir_cache[entry.pamt_path] = package_dir
        normalized_path = entry.path.replace("\\", "/").lower()
        folder_text, _, _basename = normalized_path.rpartition("/")
        raw_parts = folder_parts_cache.get(folder_text)
        if raw_parts is None:
            raw_parts = tuple(
                part
                for part in folder_text.split("/")
                if part not in {"", ".", ".."}
            )
            folder_parts_cache[folder_text] = raw_parts
        folder_counts[(package_dir, *raw_parts)] += 1

    for parts, count in folder_counts.items():
        parent = ""
        child_value = ""
        for part in parts:
            child_value = f"{child_value}/{part}" if child_value else part
            parent_counts = child_counts[parent]
            parent_counts[child_value] = parent_counts.get(child_value, 0) + count
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
    *,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[
    Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]],
    Dict[Tuple[str, ...], List[int]],
    Dict[Tuple[str, ...], List[int]],
]:
    child_folder_sets: Dict[Tuple[str, ...], Dict[Tuple[str, ...], str]] = defaultdict(dict)
    direct_files: Dict[Tuple[str, ...], List[Tuple[str, int]]] = defaultdict(list)
    folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
    folder_key_cache: Dict[str, Tuple[str, ...]] = {"": ()}
    folder_hierarchy_cache: Dict[Tuple[str, ...], Tuple[Tuple[Tuple[str, ...], Tuple[str, ...], str], ...]] = {(): ()}
    total_entries = len(entries)
    progress_total = max(total_entries, 1)
    update_every = 50_000 if total_entries >= 500_000 else 10_000 if total_entries >= 100_000 else 2_000

    if on_progress:
        on_progress(0 if total_entries > 0 else 1, progress_total, f"Indexing archive browser tree... 0 / {total_entries:,} entries")

    for index, entry in enumerate(entries):
        current = index + 1
        if stop_event is not None and (current == 1 or current % 2048 == 0):
            raise_if_cancelled(stop_event)
        normalized_path = entry.path.replace("\\", "/")
        folder_text, _, basename = normalized_path.rpartition("/")
        if not basename:
            basename = normalized_path
        folder_key = folder_key_cache.get(folder_text)
        if folder_key is None:
            folder_key = tuple(
                part
                for part in folder_text.split("/")
                if part not in {"", ".", ".."}
            )
            folder_key_cache[folder_text] = folder_key
        if not folder_key and basename in {"", ".", ".."}:
            continue

        direct_files[folder_key].append((basename.lower(), index))
        folder_entry_indexes[()].append(index)
        hierarchy = folder_hierarchy_cache.get(folder_key)
        if hierarchy is None:
            parent_key: Tuple[str, ...] = ()
            built_hierarchy: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
            child_key_parts: List[str] = []
            for part in folder_key:
                child_key_parts.append(part)
                child_key = tuple(child_key_parts)
                built_hierarchy.append((parent_key, child_key, part))
                parent_key = child_key
            hierarchy = tuple(built_hierarchy)
            folder_hierarchy_cache[folder_key] = hierarchy
        for parent_key, child_key, part in hierarchy:
            child_folder_sets[parent_key][child_key] = part
            folder_entry_indexes[child_key].append(index)

        if on_progress and (current == 1 or current % update_every == 0 or current == total_entries):
            on_progress(current, progress_total, f"Indexing archive browser tree... {current:,} / {total_entries:,} entries")

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
            key=lambda item: item[0],
        )
        for folder_key, indexes in direct_files.items()
    }
    direct_file_indexes = {
        folder_key: [index for _basename, index in sorted_items]
        for folder_key, sorted_items in direct_files_by_folder.items()
    }
    return child_folders, direct_file_indexes, dict(folder_entry_indexes)


def prepare_archive_browser_state(
    entries: Sequence[ArchiveEntry],
    *,
    filter_text: str,
    exclude_filter_text: str,
    extension_filter: str,
    package_filter_text: str,
    structure_filter: str,
    role_filter: str,
    exclude_common_technical_suffixes: bool,
    min_size_kb: int,
    previewable_only: bool,
    build_structure_children: bool = True,
    build_tree_index: bool = True,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    total_steps = 1 + (1 if build_structure_children else 0) + (1 if build_tree_index else 0)
    current_step = 0
    structure_children: Dict[str, List[Tuple[str, int]]] = {}
    if build_structure_children:
        raise_if_cancelled(stop_event)
        current_step += 1
        if on_progress:
            on_progress(current_step, total_steps, "Building folder filters from archive entries...")
        structure_children = build_archive_structure_children_map(entries)

    raise_if_cancelled(stop_event)
    current_step += 1
    if on_progress:
        on_progress(current_step, total_steps, "Applying archive filters...")
    filtered_entries = filter_archive_entries(
        entries,
        filter_text=filter_text,
        exclude_filter_text=exclude_filter_text,
        extension_filter=extension_filter,
        package_filter_text=package_filter_text,
        structure_filter=structure_filter,
        role_filter=role_filter,
        exclude_common_technical_suffixes=exclude_common_technical_suffixes,
        min_size_kb=min_size_kb,
        previewable_only=previewable_only,
        on_progress=on_progress,
        stop_event=stop_event,
    )

    tree_child_folders: Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]] = {}
    tree_direct_files: Dict[Tuple[str, ...], List[int]] = {}
    folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = {}
    if build_tree_index:
        raise_if_cancelled(stop_event)
        current_step += 1
        if on_progress:
            on_progress(current_step, total_steps, "Indexing archive browser tree...")
        tree_child_folders, tree_direct_files, folder_entry_indexes = build_archive_tree_index(
            filtered_entries,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    dds_count = sum(1 for entry in filtered_entries if entry.extension == ".dds")

    return {
        "structure_children": structure_children,
        "filtered_entries": filtered_entries,
        "tree_child_folders": tree_child_folders,
        "tree_direct_files": tree_direct_files,
        "tree_folder_entry_indexes": folder_entry_indexes,
        "tree_index_ready": build_tree_index,
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


def _dds_uncompressed_surface_size(
    width: int,
    height: int,
    pf_flags: int,
    rgb_bit_count: int,
    *,
    pitch_or_linear_size: int = 0,
    mip_level: int = 0,
) -> Optional[int]:
    if width <= 0 or height <= 0:
        return None
    if pf_flags & (DDPF_LUMINANCE | DDPF_RGB | DDPF_ALPHAPIXELS | DDPF_ALPHA):
        if rgb_bit_count > 0 and rgb_bit_count % 8 == 0:
            return width * height * max(1, rgb_bit_count // 8)
    if pitch_or_linear_size > 0:
        row_pitch = max(1, pitch_or_linear_size >> max(0, mip_level))
        return row_pitch * max(1, height)
    return None


def _dds_surface_size(
    width: int,
    height: int,
    dxgi_format: int,
    four_cc: bytes,
    *,
    pf_flags: int = 0,
    rgb_bit_count: int = 0,
    pitch_or_linear_size: int = 0,
    mip_level: int = 0,
) -> int:
    bytes_per_block = _dds_bytes_per_block(dxgi_format, four_cc)
    if bytes_per_block is not None:
        block_w = max(1, (max(1, width) + 3) // 4)
        block_h = max(1, (max(1, height) + 3) // 4)
        return block_w * block_h * bytes_per_block
    raw_surface_size = _dds_uncompressed_surface_size(
        width,
        height,
        pf_flags,
        rgb_bit_count,
        pitch_or_linear_size=pitch_or_linear_size,
        mip_level=mip_level,
    )
    if raw_surface_size is not None:
        return raw_surface_size
    raise ValueError(
        f"Unsupported DDS partial compression format: DXGI={dxgi_format} FOURCC={four_cc!r}"
    )


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
    pf_flags = struct.unpack_from("<I", header, 80)[0]
    ddspf_four_cc = header[84:88]
    rgb_bit_count = struct.unpack_from("<I", header, 88)[0]
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
            decompressed_block_sizes.append(
                _dds_surface_size(
                    current_width,
                    current_height,
                    dxgi_format,
                    ddspf_four_cc,
                    pf_flags=pf_flags,
                    rgb_bit_count=rgb_bit_count,
                    pitch_or_linear_size=_pitch_or_linear_size,
                    mip_level=len(decompressed_block_sizes),
                )
            )
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


def read_archive_entry_raw_data(
    entry: ArchiveEntry,
    stop_event: Optional[threading.Event] = None,
) -> bytes:
    raise_if_cancelled(stop_event)
    if not entry.paz_file.exists():
        raise ValueError(f"Missing PAZ file: {entry.paz_file}")

    read_size = entry.comp_size if entry.compressed else entry.orig_size
    with entry.paz_file.open("rb") as handle:
        handle.seek(entry.offset)
        data = handle.read(read_size)
    raise_if_cancelled(stop_event)
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


def _maybe_decompress_partial_par_container(
    entry: ArchiveEntry,
    data: bytes,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Optional[Tuple[bytes, str]]:
    if lz4_block is None:
        return None
    if entry.compression_type != 1 or len(data) < 0x50 or not data.startswith(b"PAR "):
        return None

    slots: List[Tuple[int, int, int]] = []
    file_offset = 0x50
    rebuilt_size = 0x50
    saw_compressed_section = False

    for slot in range(8):
        raise_if_cancelled(stop_event)
        slot_offset = 0x10 + slot * 8
        comp_size = struct.unpack_from("<I", data, slot_offset)[0]
        decomp_size = struct.unpack_from("<I", data, slot_offset + 4)[0]
        if decomp_size <= 0:
            continue

        chunk_size = comp_size if comp_size > 0 else decomp_size
        if chunk_size <= 0:
            return None
        if decomp_size > entry.orig_size or rebuilt_size + decomp_size > entry.orig_size:
            return None
        if file_offset + chunk_size > len(data):
            return None

        slots.append((comp_size, decomp_size, file_offset))
        file_offset += chunk_size
        rebuilt_size += decomp_size
        if comp_size > 0:
            saw_compressed_section = True

    if not saw_compressed_section:
        return None
    if file_offset != len(data) or rebuilt_size != entry.orig_size:
        return None

    rebuilt = bytearray(data[:0x50])
    for comp_size, decomp_size, chunk_offset in slots:
        raise_if_cancelled(stop_event)
        chunk_size = comp_size if comp_size > 0 else decomp_size
        chunk = data[chunk_offset : chunk_offset + chunk_size]
        if comp_size > 0:
            try:
                chunk = lz4_block.decompress(chunk, uncompressed_size=decomp_size)
            except Exception:
                return None
            if len(chunk) != decomp_size:
                return None
        rebuilt.extend(chunk)

    if len(rebuilt) != entry.orig_size:
        return None

    # Preserve section sizes but clear the stored compressed lengths so the
    # rebuilt payload behaves like a normal decompressed PAR for downstream parsers.
    for slot in range(8):
        struct.pack_into("<I", rebuilt, 0x10 + slot * 8, 0)

    return bytes(rebuilt), "PartialPAR"


def read_archive_entry_data(
    entry: ArchiveEntry,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bool, str]:
    data = read_archive_entry_raw_data(entry, stop_event=stop_event)

    decompressed = False
    note = ""
    if entry.encrypted:
        raise_if_cancelled(stop_event)
        data, decrypt_note = try_decrypt_archive_entry_data(entry, data)
        if decrypt_note:
            note = decrypt_note
        raise_if_cancelled(stop_event)
    if entry.compressed:
        if entry.compression_type == 1:
            partial_par = _maybe_decompress_partial_par_container(
                entry,
                data,
                stop_event=stop_event,
            )
            if partial_par is not None:
                data, partial_note = partial_par
                decompressed = True
                note = ",".join(part for part in [note, partial_note] if part)
            elif entry.extension == ".dds":
                raise_if_cancelled(stop_event)
                data = reconstruct_partial_dds(entry, data)
                decompressed = True
                note = ",".join(part for part in [note, "PartialDDS"] if part)
            else:
                note = ",".join(
                    part
                    for part in [note, "PartialRaw"]
                    if part
                )
        elif entry.compression_type == 2:
            if lz4_block is None:
                raise ValueError("This entry uses LZ4 compression, but the lz4 Python package is not installed.")
            raise_if_cancelled(stop_event)
            data = lz4_block.decompress(data, uncompressed_size=entry.orig_size)
            decompressed = True
            note = ",".join(part for part in [note, "LZ4"] if part)
        else:
            reconstructed = maybe_reconstruct_sparse_dds(entry, data)
            if reconstructed is not None:
                data, sparse_note = reconstructed
                note = ",".join(part for part in [note, sparse_note] if part)
            else:
                raise ValueError(f"Unsupported archive compression type {entry.compression_type} for {entry.path}")
        raise_if_cancelled(stop_event)

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


def _background_delete_directory(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["cmd.exe", "/d", "/c", "rmdir", "/s", "/q", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return
    shutil.rmtree(path, ignore_errors=True)


def clear_directory_contents(path: Path) -> None:
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing to clear root directory: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    children = list(resolved.iterdir())
    if not children:
        return

    trash_root = Path(
        tempfile.mkdtemp(
            prefix=f"__ctf_pending_delete_{resolved.name}_",
            dir=str(resolved.parent),
        )
    )

    try:
        for child in children:
            target = trash_root / child.name
            suffix = 1
            while target.exists():
                target = trash_root / f"{child.name}.{suffix}"
                suffix += 1
            try:
                child.replace(target)
            except OSError:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        _background_delete_directory(trash_root)
    except Exception:
        shutil.rmtree(trash_root, ignore_errors=True)
        raise


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


def _decode_dds_fourcc(fourcc: bytes) -> str:
    if not fourcc:
        return "-"
    try:
        text = fourcc.decode("ascii", errors="strict")
    except Exception:
        text = ""
    if text and all(32 <= ord(ch) <= 126 for ch in text):
        return text
    return "0x" + fourcc.hex().upper()


def _decode_dds_resource_dimension(value: int) -> str:
    return {
        0: "Unknown",
        1: "Buffer",
        2: "Texture1D",
        3: "Texture2D",
        4: "Texture3D",
    }.get(int(value), f"Unknown ({value})")


def _decode_dds_alpha_mode(value: int) -> str:
    return {
        0: "Unknown",
        1: "Straight",
        2: "Premultiplied",
        3: "Opaque",
        4: "Custom",
    }.get(int(value), f"Unknown ({value})")


def _decode_flag_names(value: int, mapping: Sequence[Tuple[int, str]]) -> str:
    names = [label for mask, label in mapping if value & mask]
    return ", ".join(names) if names else "-"


def _format_u32_list(values: Sequence[int]) -> str:
    if not values:
        return "-"
    return ", ".join(f"0x{int(value):08X}" for value in values)


def _format_hex_dump(data: bytes) -> str:
    if not data:
        return "-"
    lines: List[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{byte:02X}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        lines.append(f"  {offset:04X}  {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dds_resource_type_from_caps(caps2: int) -> str:
    if caps2 & 0x00000200:
        return "Cubemap"
    if caps2 & 0x00200000:
        return "Texture3D"
    return "Texture2D"


def build_dds_header_detail_text(
    dds_path: Path,
    dds_info: Optional[DdsInfo] = None,
    *,
    logical_path: str = "",
    sidecar_texts: Sequence[str] = (),
) -> str:
    resolved_info = dds_info if dds_info is not None else parse_dds(dds_path)
    with dds_path.open("rb") as handle:
        blob = handle.read(148)
    if len(blob) < 128 or blob[:4] != DDS_MAGIC:
        raise ValueError("Missing DDS header.")

    header_magic = blob[:4]
    header = blob[4:128]
    header_size = struct.unpack_from("<I", header, 0)[0]
    header_flags = struct.unpack_from("<I", header, 4)[0]
    pitch_or_linear_size = struct.unpack_from("<I", header, 16)[0]
    depth = struct.unpack_from("<I", header, 20)[0]
    reserved1 = list(struct.unpack_from("<11I", header, 28))
    pf_flags = struct.unpack_from("<I", header, 76)[0]
    fourcc = header[80:84]
    rgb_bit_count = struct.unpack_from("<I", header, 84)[0]
    r_mask = struct.unpack_from("<I", header, 88)[0]
    g_mask = struct.unpack_from("<I", header, 92)[0]
    b_mask = struct.unpack_from("<I", header, 96)[0]
    a_mask = struct.unpack_from("<I", header, 100)[0]
    caps = struct.unpack_from("<I", header, 104)[0]
    caps2 = struct.unpack_from("<I", header, 108)[0]
    caps3 = struct.unpack_from("<I", header, 112)[0]
    caps4 = struct.unpack_from("<I", header, 116)[0]
    semantic_path_value = str(logical_path or dds_path).strip() or str(dds_path)
    semantic = infer_texture_semantics(
        semantic_path_value,
        sidecar_texts=sidecar_texts,
        original_texconv_format=resolved_info.texconv_format,
        has_alpha=resolved_info.has_alpha,
    )
    texture_type_hint = str(getattr(semantic, "texture_type", "") or "").strip().lower() or classify_texture_type(semantic_path_value)
    semantic_subtype = str(getattr(semantic, "semantic_subtype", "") or "").strip().lower()
    semantic_confidence = int(getattr(semantic, "confidence", 0) or 0)
    semantic_evidence = list(getattr(semantic, "evidence", ()) or [])
    is_dx10 = fourcc == b"DX10" and len(blob) >= 148
    dxgi_format = struct.unpack_from("<I", blob, 128)[0] if is_dx10 else 0
    resource_dimension = struct.unpack_from("<I", blob, 132)[0] if is_dx10 else 0
    misc_flag = struct.unpack_from("<I", blob, 136)[0] if is_dx10 else 0
    array_size = struct.unpack_from("<I", blob, 140)[0] if is_dx10 else 1
    misc_flags2 = struct.unpack_from("<I", blob, 144)[0] if is_dx10 else 0
    resource_type = _decode_dds_resource_dimension(resource_dimension) if is_dx10 else _dds_resource_type_from_caps(caps2)
    expected_mips = max(1, int(math.floor(math.log2(max(1, resolved_info.width, resolved_info.height, depth or 1)))) + 1)
    block_bytes = _dds_bytes_per_block(dxgi_format, fourcc)
    cube_face_count = 1
    if is_dx10 and (misc_flag & 0x4):
        cube_face_count = 6
    elif caps2 & 0x00000200:
        cube_face_count = sum(
            1
            for mask in (0x00000400, 0x00000800, 0x00001000, 0x00002000, 0x00004000, 0x00008000)
            if caps2 & mask
        ) or 6
    surface_instance_count = max(1, array_size) * max(1, cube_face_count)
    top_level_surface_bytes_text = "-"
    total_surface_bytes_text = "-"
    try:
        cur_w = max(1, resolved_info.width)
        cur_h = max(1, resolved_info.height)
        top_level_surface_bytes = _dds_surface_size(
            cur_w,
            cur_h,
            dxgi_format,
            fourcc,
            pf_flags=pf_flags,
            rgb_bit_count=rgb_bit_count,
            pitch_or_linear_size=pitch_or_linear_size,
            mip_level=0,
        )
        total_surface_bytes = 0
        for mip_index in range(max(1, resolved_info.mip_count)):
            total_surface_bytes += _dds_surface_size(
                cur_w,
                cur_h,
                dxgi_format,
                fourcc,
                pf_flags=pf_flags,
                rgb_bit_count=rgb_bit_count,
                pitch_or_linear_size=pitch_or_linear_size,
                mip_level=mip_index,
            )
            cur_w = max(1, cur_w >> 1)
            cur_h = max(1, cur_h >> 1)
        top_level_surface_bytes *= surface_instance_count
        total_surface_bytes *= surface_instance_count
        top_level_surface_bytes_text = f"{top_level_surface_bytes:,}"
        total_surface_bytes_text = f"{total_surface_bytes:,}"
    except Exception:
        pass
    file_sha256 = _sha256_path(dds_path)
    header_bytes = blob[:148] if is_dx10 else blob[:128]
    ddsd_flags = _decode_flag_names(
        header_flags,
        (
            (0x00000001, "CAPS"),
            (0x00000002, "HEIGHT"),
            (0x00000004, "WIDTH"),
            (0x00000008, "PITCH"),
            (0x00001000, "PIXELFORMAT"),
            (0x00020000, "MIPMAPCOUNT"),
            (0x00080000, "LINEARSIZE"),
            (0x00800000, "DEPTH"),
        ),
    )
    pixel_flag_names = _decode_flag_names(
        pf_flags,
        (
            (DDPF_ALPHAPIXELS, "ALPHAPIXELS"),
            (DDPF_ALPHA, "ALPHA"),
            (DDPF_FOURCC, "FOURCC"),
            (DDPF_RGB, "RGB"),
            (DDPF_LUMINANCE, "LUMINANCE"),
        ),
    )
    caps_names = _decode_flag_names(
        caps,
        (
            (0x00000008, "COMPLEX"),
            (0x00001000, "TEXTURE"),
            (0x00400000, "MIPMAP"),
        ),
    )
    caps2_names = _decode_flag_names(
        caps2,
        (
            (0x00000200, "CUBEMAP"),
            (0x00000400, "CUBEMAP_POSITIVEX"),
            (0x00000800, "CUBEMAP_NEGATIVEX"),
            (0x00001000, "CUBEMAP_POSITIVEY"),
            (0x00002000, "CUBEMAP_NEGATIVEY"),
            (0x00004000, "CUBEMAP_POSITIVEZ"),
            (0x00008000, "CUBEMAP_NEGATIVEZ"),
            (0x00200000, "VOLUME"),
        ),
    )

    lines = [
        "DDS metadata:",
        f"- Format: {resolved_info.texconv_format}",
        f"- Dimensions: {resolved_info.width}x{resolved_info.height}",
        f"- Mip levels: {resolved_info.mip_count}",
        f"- Mip chain complete: {'Yes' if resolved_info.mip_count >= expected_mips else 'No'} ({resolved_info.mip_count}/{expected_mips} expected)",
        f"- Alpha: {'Yes' if resolved_info.has_alpha else 'No'}",
        f"- Colorspace intent: {resolved_info.colorspace_intent}",
        f"- Precision-sensitive: {'Yes' if resolved_info.precision_sensitive else 'No'}",
        f"- Texture type hint: {texture_type_hint}",
        f"- Semantic subtype: {semantic_subtype or '-'}",
        f"- Semantic confidence: {semantic_confidence}",
        f"- Semantic evidence: {semantic_evidence[0] if semantic_evidence else '-'}",
        f"- Resource type: {resource_type}",
        f"- DX10 header present: {'Yes' if is_dx10 else 'No'}",
        f"- DDS magic: {header_magic.decode('ascii', errors='replace')!r}",
        f"- Header size field: {header_size}",
        f"- Header flags: 0x{header_flags:08X}",
        f"- Header flag names: {ddsd_flags}",
        f"- Pitch / linear size: {pitch_or_linear_size:,}",
        f"- Depth: {depth or 1}",
        f"- Pixel format flags: 0x{pf_flags:08X}",
        f"- Pixel format names: {pixel_flag_names}",
        f"- FOURCC: {_decode_dds_fourcc(fourcc)}",
        f"- RGB bit count: {rgb_bit_count}",
        f"- Channel masks: R=0x{r_mask:08X} G=0x{g_mask:08X} B=0x{b_mask:08X} A=0x{a_mask:08X}",
        f"- Caps: 0x{caps:08X}",
        f"- Caps names: {caps_names}",
        f"- Caps2: 0x{caps2:08X}",
        f"- Caps2 names: {caps2_names}",
        f"- Caps3: 0x{caps3:08X}",
        f"- Caps4: 0x{caps4:08X}",
        f"- Block compression: {f'{block_bytes} bytes per 4x4 block' if block_bytes is not None else 'Uncompressed / direct pixel layout'}",
        f"- Surface instances: {surface_instance_count}",
        f"- Estimated top-level surface bytes: {top_level_surface_bytes_text}",
        f"- Estimated total surface bytes across listed mips: {total_surface_bytes_text}",
        f"- Resolved DDS file size: {dds_path.stat().st_size:,} bytes",
        f"- SHA-256: {file_sha256}",
        f"- Reserved1 values: {_format_u32_list(reserved1)}",
    ]

    if is_dx10:
        lines.extend(
            [
                "- DX10 header:",
                f"  - DXGI format id: {dxgi_format}",
                f"  - Resource dimension: {_decode_dds_resource_dimension(resource_dimension)}",
                f"  - Array size: {array_size}",
                f"  - Misc flag: 0x{misc_flag:08X}",
                f"  - Misc flags2: 0x{misc_flags2:08X}",
                f"  - Alpha mode: {_decode_dds_alpha_mode(misc_flags2 & 0x7)}",
                f"  - Texture cube flag: {'Yes' if (misc_flag & 0x4) else 'No'}",
            ]
        )
    lines.extend(
        [
            "- Header hex dump:",
            _format_hex_dump(header_bytes),
        ]
    )
    return "\n".join(lines)


def ensure_archive_preview_source(
    entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[Path, str]:
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
    data, _decompressed, note = read_archive_entry_data(entry, stop_event=stop_event)
    target_path.write_bytes(data)
    if note:
        (cache_dir / ".note").write_text(note, encoding="utf-8")
    return target_path, note


def _normalize_model_texture_reference(value: str) -> str:
    return PurePosixPath(str(value or "").replace("\\", "/")).as_posix().strip().lower()


def _normalize_model_submesh_reference(value: str) -> str:
    raw_text = str(value or "").replace("\\", "/").strip().lower()
    if not raw_text:
        return ""
    basename = PurePosixPath(raw_text).name or raw_text
    normalized = re.sub(r"[^a-z0-9]+", "", basename)
    if normalized:
        return normalized
    return re.sub(r"[^a-z0-9]+", "", raw_text)


def extract_binary_dds_references(
    data: bytes,
    *,
    sample_limit: int = 262_144,
    max_strings: int = 96,
) -> List[str]:
    references: List[str] = []
    seen: set[str] = set()
    string_candidates = extract_binary_strings(
        data,
        sample_limit=sample_limit,
        max_strings=max(max_strings * 2, 48),
    )
    for text in string_candidates:
        for match in _TEXT_DDS_REFERENCE_RE.finditer(text):
            raw_text = str(match.group(0) or "").strip().strip("\x00")
            if not raw_text or not any(char.isalpha() for char in raw_text):
                continue
            normalized = _normalize_model_texture_reference(raw_text)
            if not normalized or not normalized.endswith(".dds") or normalized in seen:
                continue
            seen.add(normalized)
            references.append(raw_text.replace("\\", "/"))
            if len(references) >= max_strings:
                return references
    return references


def _humanize_model_texture_hint(semantic_hint: str) -> str:
    raw_text = str(semantic_hint or "").strip().lstrip("_")
    if not raw_text:
        return ""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_text)
    spaced = re.sub(r"[_\s]+", " ", spaced).strip()
    if not spaced:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in spaced.split())


def _model_texture_hint_priority(semantic_hint: str) -> Optional[Tuple[int, int]]:
    normalized = str(semantic_hint or "").strip().lower().replace("_", "")
    if not normalized:
        return None

    technical_tokens = (
        "normal",
        "height",
        "displacement",
        "materialtexture",
        "materialmask",
        "detailmask",
        "masktexture",
        "roughness",
        "metallic",
        "occlusion",
        "opacity",
        "screenspacedisplacement",
        "specular",
    )
    if any(token in normalized for token in technical_tokens):
        return (0, 0)

    if any(
        token in normalized
        for token in (
            "overlaycolor",
            "colortexture",
            "diffuse",
            "albedo",
            "basecolor",
            "emissive",
            "tintcolor",
        )
    ):
        return (6, 3)
    if "color" in normalized or "overlay" in normalized or "tint" in normalized:
        return (6, 2)
    return None


def _score_model_sidecar_entry_candidate(source_entry: ArchiveEntry, candidate: ArchiveEntry) -> Tuple[int, int, int]:
    normalized_candidate = _normalize_model_texture_reference(candidate.path)
    source_path = _normalize_model_texture_reference(source_entry.path)
    source_root = PurePosixPath(source_path).parts[:1]
    candidate_root = PurePosixPath(normalized_candidate).parts[:1]
    score_value = 0
    if candidate.pamt_path == source_entry.pamt_path:
        score_value += 10
    if candidate.pamt_path.parent == source_entry.pamt_path.parent:
        score_value += 6
    if "/texture/" in normalized_candidate:
        score_value += 8
    if candidate_root and source_root and candidate_root == source_root:
        score_value += 4
    source_extension = str(source_entry.extension or "").strip().lower()
    if source_extension in {".pam", ".pamlod"} and normalized_candidate.endswith(".pami"):
        extension_priority = 2
    elif normalized_candidate.endswith(".xml"):
        extension_priority = 1
    else:
        extension_priority = 0
    return score_value, -len(candidate.path), extension_priority


def _score_model_related_entry_candidate(source_entry: ArchiveEntry, candidate: ArchiveEntry) -> Tuple[int, int, int]:
    normalized_candidate = _normalize_model_texture_reference(candidate.path)
    source_path = _normalize_model_texture_reference(source_entry.path)
    source_root = PurePosixPath(source_path).parts[:1]
    candidate_root = PurePosixPath(normalized_candidate).parts[:1]
    score_value = 0
    if candidate.pamt_path == source_entry.pamt_path:
        score_value += 10
    if candidate.pamt_path.parent == source_entry.pamt_path.parent:
        score_value += 6
    if candidate_root and source_root and candidate_root == source_root:
        score_value += 4
    source_extension = str(source_entry.extension or "").strip().lower()
    candidate_extension = str(candidate.extension or "").strip().lower()
    extension_priority = 0
    if source_extension == ".pam":
        if candidate_extension == ".pamlod":
            extension_priority = 6
        elif candidate_extension == ".pami":
            extension_priority = 5
        elif candidate_extension == ".xml":
            extension_priority = 4
        elif candidate_extension == ".meshinfo":
            extension_priority = 3
        elif candidate_extension == ".hkx":
            extension_priority = 2
    elif source_extension == ".pamlod":
        if candidate_extension == ".pam":
            extension_priority = 6
        elif candidate_extension == ".pami":
            extension_priority = 5
        elif candidate_extension == ".xml":
            extension_priority = 4
        elif candidate_extension == ".meshinfo":
            extension_priority = 3
        elif candidate_extension == ".hkx":
            extension_priority = 2
    elif source_extension == ".pac":
        if candidate_extension == ".pab":
            extension_priority = 7
        elif candidate_extension == ".xml":
            extension_priority = 6
        elif candidate_extension == ".meshinfo":
            extension_priority = 5
        elif candidate_extension == ".hkx":
            extension_priority = 4
    elif source_extension == ".meshinfo":
        if candidate_extension in {".pam", ".pamlod", ".pac"}:
            extension_priority = 7
        elif candidate_extension == ".hkx":
            extension_priority = 6
        elif candidate_extension == ".xml":
            extension_priority = 5
        elif candidate_extension == ".pami":
            extension_priority = 4
    elif source_extension == ".pab":
        if candidate_extension == ".pac":
            extension_priority = 7
        elif candidate_extension == ".hkx":
            extension_priority = 6
        elif candidate_extension == ".meshinfo":
            extension_priority = 5
        elif candidate_extension == ".xml":
            extension_priority = 4
    elif source_extension in {".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
        if candidate_extension in {".hkx", ".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
            extension_priority = 6
        elif candidate_extension == ".xml":
            extension_priority = 5
    elif candidate_extension in {".xml", ".pami", ".meshinfo", ".hkx"}:
        extension_priority = 2
    return score_value, -len(candidate.path), extension_priority


def _find_archive_model_related_entries(
    source_entry: ArchiveEntry,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
) -> Tuple[ArchiveEntry, ...]:
    if archive_entries_by_basename is None:
        return ()

    normalized_path = source_entry.path.replace("\\", "/").strip()
    basename = PurePosixPath(normalized_path).name.strip()
    source_stem = PurePosixPath(normalized_path).stem.strip()
    if not basename:
        return ()

    source_extension = str(source_entry.extension or "").strip().lower()
    target_basenames: set[str] = set()

    def add_target(raw_value: str) -> None:
        candidate = str(raw_value or "").strip().lower()
        if candidate:
            target_basenames.add(candidate)

    add_target(f"{basename}.xml")
    if source_stem:
        add_target(f"{source_stem}.xml")
        add_target(f"{source_stem}.hkx")
        add_target(f"{source_stem}.meshinfo")
        if source_extension in {".pam", ".pamlod"}:
            add_target(f"{source_stem}.pami")
        if source_extension == ".pam":
            add_target(f"{source_stem}.pamlod")
            if source_stem.endswith("_breakable"):
                add_target(f"{source_stem[:-10]}.pamlod")
        elif source_extension == ".pamlod":
            add_target(f"{source_stem}.pam")
        elif source_extension == ".pac":
            add_target(f"{source_stem}.pab")
        elif source_extension == ".meshinfo":
            add_target(f"{source_stem}.pam")
            add_target(f"{source_stem}.pamlod")
            add_target(f"{source_stem}.pac")
            add_target(f"{source_stem}.pami")
        elif source_extension == ".pab":
            add_target(f"{source_stem}.pac")
        elif source_extension in {".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
            for related_extension in (".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage", ".xml", ".hkx"):
                add_target(f"{source_stem}{related_extension}")
    add_target(f"{basename}.hkx")
    add_target(f"{basename}.meshinfo")

    candidates: List[ArchiveEntry] = []
    for target_basename in target_basenames:
        for candidate in archive_entries_by_basename.get(target_basename, ()):
            if candidate.path == source_entry.path:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        return ()
    candidates.sort(key=lambda candidate: _score_model_related_entry_candidate(source_entry, candidate), reverse=True)
    return tuple(candidates[:12])


def _find_archive_model_sidecar_entries(
    source_entry: ArchiveEntry,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
) -> Tuple[ArchiveEntry, ...]:
    candidates = [
        candidate
        for candidate in _find_archive_model_related_entries(source_entry, archive_entries_by_basename)
        if candidate.extension in {".xml", ".pami"}
    ]
    if not candidates:
        return ()
    candidates.sort(key=lambda candidate: _score_model_sidecar_entry_candidate(source_entry, candidate), reverse=True)
    return tuple(candidates[:4])


def _parse_archive_model_sidecar_texture_bindings(
    sidecar_text: str,
    *,
    sidecar_path: str,
) -> Tuple[_ArchiveModelSidecarTextureBinding, ...]:
    parsed_bindings = parse_texture_sidecar_bindings(sidecar_text, sidecar_path=sidecar_path)
    return tuple(
        _ArchiveModelSidecarTextureBinding(
            texture_path=binding.texture_path,
            parameter_name=binding.parameter_name,
            submesh_name=binding.submesh_name,
            sidecar_path=binding.sidecar_path,
        )
        for binding in parsed_bindings
    )


def _extract_archive_model_sidecar_texture_references(
    source_entry: ArchiveEntry,
    *,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
    stop_event: Optional[threading.Event] = None,
) -> Tuple[
    Tuple[_ArchiveModelSidecarTextureBinding, ...],
    Tuple[str, ...],
    Dict[str, Tuple[str, ...]],
    Dict[str, Tuple[str, ...]],
]:
    bindings: List[_ArchiveModelSidecarTextureBinding] = []
    sidecar_paths: List[str] = []
    seen_binding_keys: set[Tuple[str, str, str]] = set()
    sidecar_texts_by_normalized_path: Dict[str, List[str]] = defaultdict(list)
    sidecar_texts_by_basename: Dict[str, List[str]] = defaultdict(list)
    for sidecar_entry in _find_archive_model_sidecar_entries(source_entry, archive_entries_by_basename):
        raise_if_cancelled(stop_event)
        try:
            sidecar_data, _decompressed, _note = read_archive_entry_data(sidecar_entry, stop_event=stop_event)
        except Exception:
            continue
        text = try_decode_text_like_archive_data(sidecar_data)
        if text is None:
            continue
        parsed_bindings = _parse_archive_model_sidecar_texture_bindings(text, sidecar_path=sidecar_entry.path)
        if not parsed_bindings:
            continue
        sidecar_paths.append(sidecar_entry.path)
        for binding in parsed_bindings:
            normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
            key = (
                normalized_texture_path,
                str(binding.submesh_name or "").strip().lower(),
                str(binding.parameter_name or "").strip().lower(),
            )
            if normalized_texture_path:
                sidecar_texts_by_normalized_path[normalized_texture_path].append(text)
                texture_basename = PurePosixPath(normalized_texture_path).name
                if texture_basename:
                    sidecar_texts_by_basename[texture_basename].append(text)
            if key in seen_binding_keys:
                continue
            seen_binding_keys.add(key)
            bindings.append(binding)
    return (
        tuple(bindings),
        tuple(sidecar_paths),
        {key: tuple(values) for key, values in sidecar_texts_by_normalized_path.items()},
        {key: tuple(values) for key, values in sidecar_texts_by_basename.items()},
    )


def _iter_parsed_model_submeshes(parsed_mesh: Optional[object]) -> List[object]:
    if parsed_mesh is None:
        return []
    if str(getattr(parsed_mesh, "format", "") or "").strip().lower() == "pamlod":
        lod_levels = getattr(parsed_mesh, "lod_levels", None) or [[]]
        return list(lod_levels[0] or [])
    return list(getattr(parsed_mesh, "submeshes", ()) or [])


def _iter_model_submesh_reference_candidates(*values: str) -> Tuple[str, ...]:
    ordered_candidates: List[str] = []
    seen: set[str] = set()

    def add_candidate(raw_value: str) -> None:
        normalized = _normalize_model_submesh_reference(raw_value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        ordered_candidates.append(normalized)

    for raw_value in values:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            continue
        add_candidate(raw_text)
        pure_path = PurePosixPath(raw_text.replace("\\", "/"))
        basename = pure_path.name
        stem = pure_path.stem
        if basename and basename != raw_text:
            add_candidate(basename)
        if stem and stem not in {raw_text, basename}:
            add_candidate(stem)
    return tuple(ordered_candidates)


def _iter_model_texture_family_reference_candidates(group_key: str) -> Tuple[str, ...]:
    normalized_group_key = _normalize_model_texture_reference(group_key)
    if not normalized_group_key:
        return ()

    ordered_candidates: List[str] = []
    seen: set[str] = set()

    def add_candidate(raw_value: str) -> None:
        normalized = _normalize_model_texture_reference(raw_value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        ordered_candidates.append(normalized)

    if "/" in normalized_group_key:
        folder, _, family_name = normalized_group_key.rpartition("/")
    else:
        folder, family_name = "", normalized_group_key
    family_name = family_name.strip()
    if not family_name:
        return ()

    for suffix in _MODEL_TEXTURE_VISIBLE_FAMILY_SUFFIXES:
        basename = f"{family_name}{suffix}.dds"
        add_candidate(basename)
        if folder:
            add_candidate(f"{folder}/{basename}")

    return tuple(ordered_candidates)


def _iter_model_texture_reference_candidates(
    texture_name: str,
    material_name: str = "",
) -> Tuple[str, ...]:
    ordered_candidates: List[str] = []
    seen: set[str] = set()

    def add_candidate(raw_value: str) -> None:
        normalized = _normalize_model_texture_reference(raw_value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        ordered_candidates.append(normalized)

    for raw_value in (texture_name, material_name):
        normalized = _normalize_model_texture_reference(raw_value)
        if not normalized:
            continue
        add_candidate(normalized)
        basename = PurePosixPath(normalized).name
        stem = PurePosixPath(normalized).stem
        suffix = PurePosixPath(normalized).suffix.lower()
        if basename:
            add_candidate(basename)
        if stem:
            add_candidate(stem)
        if suffix != ".dds":
            add_candidate(f"{normalized}.dds")
            if basename:
                add_candidate(f"{basename}.dds")
            if stem:
                add_candidate(f"{stem}.dds")

    return tuple(ordered_candidates)


def _looks_like_technical_model_texture(texture_path: str) -> bool:
    normalized = _normalize_model_texture_reference(texture_path)
    if not normalized:
        return False
    basename = PurePosixPath(normalized).name
    for pattern in _COMMON_TECHNICAL_DDS_EXCLUDE_PATTERNS:
        if (basename and fnmatch.fnmatch(basename, pattern)) or fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def _has_explicit_model_texture_reference(*values: str) -> bool:
    for raw_value in values:
        normalized = _normalize_model_texture_reference(raw_value)
        if normalized.endswith(".dds"):
            return True
    return False


def _is_visible_model_texture_type(texture_type: str) -> bool:
    return str(texture_type or "").strip().lower() in {"color", "ui", "emissive", "impostor"}


def _resolve_model_texture_semantics(
    texture_path: str,
    *,
    family_members: Sequence[str] = (),
    sidecar_texts: Sequence[str] = (),
) -> Tuple[str, str, int]:
    semantic = infer_texture_semantics(
        texture_path,
        family_members=family_members,
        sidecar_texts=sidecar_texts,
    )
    texture_type = str(getattr(semantic, "texture_type", "") or "").strip().lower() or "unknown"
    semantic_subtype = str(getattr(semantic, "semantic_subtype", "") or "").strip().lower() or texture_type
    confidence = int(getattr(semantic, "confidence", 0) or 0)
    if texture_type == "unknown":
        normalized = _normalize_model_texture_reference(texture_path)
        if normalized.endswith(".dds") and not _looks_like_technical_model_texture(normalized):
            return "color", "albedo", max(confidence, 64)
    return texture_type, semantic_subtype, confidence


def _score_model_texture_archive_candidate(
    source_entry: ArchiveEntry,
    candidate: ArchiveEntry,
    reference_candidates: Sequence[str],
) -> Tuple[int, int]:
    score_value = 0
    normalized_candidate_path = _normalize_model_texture_reference(candidate.path)
    candidate_basename = PurePosixPath(normalized_candidate_path).name
    for reference_index, normalized_reference in enumerate(reference_candidates):
        reference_basename = PurePosixPath(normalized_reference).name
        if normalized_candidate_path == normalized_reference:
            score_value += max(8, 24 - reference_index)
            break
        if candidate_basename and candidate_basename == reference_basename:
            score_value += max(4, 16 - reference_index)
            break
    if candidate.pamt_path == source_entry.pamt_path:
        score_value += 8
    if candidate.pamt_path.parent == source_entry.pamt_path.parent:
        score_value += 4
    if candidate.paz_file == source_entry.paz_file:
        score_value += 2
    if "/texture/" in normalized_candidate_path:
        score_value += 1
    return score_value, -len(candidate.path)


def _collect_model_texture_archive_entry_candidates(
    source_entry: ArchiveEntry,
    texture_name: str,
    material_name: str,
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]],
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
    *,
    expand_family_candidates: bool = True,
) -> List[Tuple[ArchiveEntry, Tuple[int, int]]]:
    reference_candidates = _iter_model_texture_reference_candidates(texture_name, material_name)
    if not reference_candidates:
        return []

    expanded_reference_candidates: List[str] = list(reference_candidates)
    if expand_family_candidates:
        seen_expanded = set(expanded_reference_candidates)
        for normalized_reference in reference_candidates:
            group_key = derive_texture_group_key(normalized_reference)
            for family_reference in _iter_model_texture_family_reference_candidates(group_key):
                if family_reference in seen_expanded:
                    continue
                seen_expanded.add(family_reference)
                expanded_reference_candidates.append(family_reference)

    candidates: List[ArchiveEntry] = []
    for normalized_reference in expanded_reference_candidates:
        if texture_entries_by_normalized_path is not None:
            for candidate in texture_entries_by_normalized_path.get(normalized_reference, []):
                if candidate.extension == ".dds" and candidate not in candidates:
                    candidates.append(candidate)

        basename = PurePosixPath(normalized_reference).name
        if texture_entries_by_basename is not None and basename:
            for candidate in texture_entries_by_basename.get(basename, []):
                if candidate.extension == ".dds" and candidate not in candidates:
                    candidates.append(candidate)

    if not candidates:
        return []

    scored_candidates = [
        (candidate, _score_model_texture_archive_candidate(source_entry, candidate, reference_candidates))
        for candidate in candidates
    ]
    scored_candidates.sort(key=lambda item: item[1], reverse=True)
    return scored_candidates


def _model_texture_semantic_priority(texture_type: str, semantic_subtype: str) -> Tuple[int, int]:
    normalized_type = str(texture_type or "").strip().lower()
    normalized_subtype = str(semantic_subtype or "").strip().lower()
    if normalized_type == "color":
        subtype_priority = {
            "albedo": 4,
            "albedo_variant": 3,
            "diffuse": 2,
        }.get(normalized_subtype, 1)
        return 6, subtype_priority
    if normalized_type == "ui":
        return 5, 0
    if normalized_type == "emissive":
        return 4, 0
    if normalized_type == "impostor":
        return 3, 0
    if normalized_type == "unknown":
        return 2, 0
    if normalized_type == "mask" and normalized_subtype in {"detail_support", "grayscale_data"}:
        return 1, 0
    return 0, 0


def _resolve_model_texture_archive_entry(
    source_entry: ArchiveEntry,
    texture_name: str,
    material_name: str,
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]],
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
    *,
    semantic_hint: str = "",
    expand_family_candidates: Optional[bool] = None,
    allow_technical_match: bool = False,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> Tuple[Optional[ArchiveEntry], str]:
    if expand_family_candidates is None:
        expand_family_candidates = not _has_explicit_model_texture_reference(texture_name, material_name)
    scored_candidates = _collect_model_texture_archive_entry_candidates(
        source_entry,
        texture_name,
        material_name,
        texture_entries_by_normalized_path,
        texture_entries_by_basename,
        expand_family_candidates=expand_family_candidates,
    )
    if not scored_candidates:
        return None, "missing"

    family_members_by_group: Dict[str, Tuple[str, ...]] = defaultdict(tuple)
    grouped_family_members: Dict[str, List[str]] = defaultdict(list)
    for candidate, _direct_score in scored_candidates:
        grouped_family_members[derive_texture_group_key(candidate.path)].append(candidate.path)
    for group_key, members in grouped_family_members.items():
        family_members_by_group[group_key] = tuple(members)

    best_candidate: Optional[ArchiveEntry] = None
    best_candidate_key: Optional[Tuple[int, int, int, Tuple[int, int]]] = None
    best_candidate_priority = (0, 0)
    hint_priority = _model_texture_hint_priority(semantic_hint)
    for candidate, direct_score in scored_candidates:
        group_key = derive_texture_group_key(candidate.path)
        candidate_normalized_path = normalize_texture_reference_for_sidecar_lookup(candidate.path)
        sidecar_texts = tuple(sidecar_texts_by_normalized_path.get(candidate_normalized_path, ())) if (
            sidecar_texts_by_normalized_path is not None and candidate_normalized_path
        ) else ()
        if not sidecar_texts and sidecar_texts_by_basename is not None:
            sidecar_texts = tuple(
                sidecar_texts_by_basename.get(PurePosixPath(candidate.path.replace("\\", "/")).name.lower(), ())
            )
        texture_type, semantic_subtype, confidence = _resolve_model_texture_semantics(
            candidate.path,
            family_members=family_members_by_group.get(group_key, (candidate.path,)),
            sidecar_texts=sidecar_texts,
        )
        semantic_priority = _model_texture_semantic_priority(
            texture_type,
            semantic_subtype,
        )
        if hint_priority is not None and hint_priority > semantic_priority:
            semantic_priority = hint_priority
        sort_key = (
            semantic_priority[0],
            semantic_priority[1],
            confidence,
            direct_score,
        )
        if best_candidate_key is None or sort_key > best_candidate_key:
            best_candidate = candidate
            best_candidate_key = sort_key
            best_candidate_priority = semantic_priority

    if best_candidate is None:
        return None, "missing"
    if allow_technical_match and best_candidate_priority[0] <= 0:
        return best_candidate, "resolved"
    if best_candidate_priority[0] <= 0:
        return None, "technical_only"
    return best_candidate, "resolved"


def _ensure_archive_model_texture_preview_path(
    resolved_texconv_path: Path,
    texture_entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> str:
    texture_source_path, _texture_note = ensure_archive_preview_source(
        texture_entry,
        stop_event=stop_event,
    )
    dds_info: Optional[DdsInfo] = None
    try:
        dds_info = parse_dds(texture_source_path)
    except Exception:
        dds_info = None
    preview_path = ensure_dds_display_preview_png(
        resolved_texconv_path,
        texture_source_path.resolve(),
        dds_info=dds_info,
        max_dimension=_MODEL_TEXTURE_DISPLAY_PREVIEW_MAX_DIMENSION,
        stop_event=stop_event,
    )
    return str(preview_path)


def _attach_model_sidecar_texture_preview_paths(
    texconv_path: Optional[Path],
    source_entry: ArchiveEntry,
    model_preview: Optional[ModelPreviewData],
    *,
    parsed_mesh: Optional[object],
    sidecar_texture_bindings: Sequence[_ArchiveModelSidecarTextureBinding],
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    if texconv_path is None or model_preview is None or not model_preview.meshes or not sidecar_texture_bindings:
        return []

    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    resolved_texconv_path = texconv_path.expanduser().resolve()
    resolved_by_submesh: Dict[str, Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]] = {}
    global_visible_bindings: List[Tuple[ArchiveEntry, str, str]] = []
    fallback_visible_bindings: List[Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]] = []
    seen_fallback_binding_keys: set[Tuple[str, str, str]] = set()
    seen_global_binding_keys: set[Tuple[str, str]] = set()
    sidecar_paths: List[str] = []
    promoted_anonymous_fallback = False
    force_unflipped_preview = str(getattr(source_entry, "extension", "") or "").lower() == ".pac"

    for binding in sidecar_texture_bindings:
        raise_if_cancelled(stop_event)
        submesh_keys = _iter_model_submesh_reference_candidates(binding.submesh_name)
        texture_entry, resolution_status = _resolve_model_texture_archive_entry(
            source_entry,
            binding.texture_path,
            binding.submesh_name,
            texture_entries_by_normalized_path,
            texture_entries_by_basename,
            semantic_hint=binding.parameter_name,
            expand_family_candidates=False,
            allow_technical_match=True,
            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename=sidecar_texts_by_basename,
        )
        if texture_entry is None or resolution_status != "resolved":
            continue
        candidate_normalized_path = normalize_texture_reference_for_sidecar_lookup(texture_entry.path)
        sidecar_texts = tuple(sidecar_texts_by_normalized_path.get(candidate_normalized_path, ())) if (
            sidecar_texts_by_normalized_path is not None and candidate_normalized_path
        ) else ()
        if not sidecar_texts and sidecar_texts_by_basename is not None:
            sidecar_texts = tuple(
                sidecar_texts_by_basename.get(PurePosixPath(texture_entry.path.replace("\\", "/")).name.lower(), ())
            )
        texture_type, semantic_subtype, confidence = _resolve_model_texture_semantics(texture_entry.path)
        if sidecar_texts:
            texture_type, semantic_subtype, confidence = _resolve_model_texture_semantics(
                texture_entry.path,
                sidecar_texts=sidecar_texts,
            )
        if not _is_visible_model_texture_type(texture_type):
            continue
        priority = _model_texture_hint_priority(binding.parameter_name) or _model_texture_semantic_priority(
            texture_type,
            semantic_subtype,
        )
        candidate_key = (priority[0], priority[1], confidence, -len(texture_entry.path))
        fallback_binding_key = (
            _normalize_model_texture_reference(texture_entry.path),
            str(binding.parameter_name or "").strip().lower(),
            _normalize_model_submesh_reference(binding.submesh_name),
        )
        if fallback_binding_key not in seen_fallback_binding_keys:
            seen_fallback_binding_keys.add(fallback_binding_key)
            fallback_visible_bindings.append(
                (
                    candidate_key,
                    texture_entry,
                    binding.parameter_name,
                    binding.submesh_name,
                )
            )
        if submesh_keys:
            for submesh_key in submesh_keys:
                existing = resolved_by_submesh.get(submesh_key)
                if existing is None or candidate_key > existing[0]:
                    resolved_by_submesh[submesh_key] = (
                        candidate_key,
                        texture_entry,
                        binding.parameter_name,
                        binding.submesh_name,
                    )
        else:
            global_key = (
                _normalize_model_texture_reference(texture_entry.path),
                str(binding.parameter_name or "").strip().lower(),
            )
            if global_key not in seen_global_binding_keys:
                seen_global_binding_keys.add(global_key)
                global_visible_bindings.append((texture_entry, binding.parameter_name, binding.submesh_name))
        if binding.sidecar_path and binding.sidecar_path not in sidecar_paths:
            sidecar_paths.append(binding.sidecar_path)

    assigned_count = 0
    unresolved_meshes: List[ModelPreviewMesh] = []
    for mesh_index, mesh in enumerate(model_preview.meshes):
        raise_if_cancelled(stop_event)
        if str(getattr(mesh, "preview_texture_path", "") or "").strip():
            continue
        parsed_submesh = parsed_submeshes[mesh_index] if mesh_index < len(parsed_submeshes) else None
        candidate_keys = _iter_model_submesh_reference_candidates(
            str(getattr(parsed_submesh, "name", "") or ""),
            str(getattr(parsed_submesh, "material", "") or ""),
            str(getattr(parsed_submesh, "texture", "") or ""),
            str(getattr(mesh, "material_name", "") or ""),
            str(getattr(mesh, "texture_name", "") or ""),
        )
        best_match: Optional[Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]] = None
        for candidate_key_text in candidate_keys:
            resolved = resolved_by_submesh.get(candidate_key_text)
            if resolved is None:
                continue
            if best_match is None or resolved[0] > best_match[0]:
                best_match = resolved
        if best_match is None:
            unresolved_meshes.append(mesh)
            continue
        _candidate_key, texture_entry, _parameter_name, submesh_name = best_match
        try:
            mesh.preview_texture_path = _ensure_archive_model_texture_preview_path(
                resolved_texconv_path,
                texture_entry,
                stop_event=stop_event,
            )
            if force_unflipped_preview:
                mesh.preview_texture_flip_vertical = False
            current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
            if not current_texture_name or not current_texture_name.lower().endswith(".dds"):
                mesh.texture_name = texture_entry.path
            current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
            if submesh_name and not current_material_name:
                mesh.material_name = submesh_name
            assigned_count += 1
        except RunCancelled:
            raise
        except Exception:
            continue

    if not global_visible_bindings and unresolved_meshes and fallback_visible_bindings:
        unique_named_sidecar_submeshes = {
            _normalize_model_submesh_reference(submesh_name)
            for _candidate_key, _texture_entry, _parameter_name, submesh_name in fallback_visible_bindings
            if _normalize_model_submesh_reference(submesh_name)
        }
        should_promote_fallback = (
            len(unresolved_meshes) == 1
            or len(model_preview.meshes) == 1
            or len(parsed_submeshes) <= 1
            or len(unique_named_sidecar_submeshes) == 1
        )
        if should_promote_fallback:
            fallback_visible_bindings.sort(key=lambda item: item[0], reverse=True)
            _candidate_key, texture_entry, parameter_name, submesh_name = fallback_visible_bindings[0]
            global_visible_bindings.append((texture_entry, parameter_name, submesh_name))
            promoted_anonymous_fallback = True

    if global_visible_bindings and unresolved_meshes:
        if len(global_visible_bindings) == 1:
            texture_entry, _parameter_name, submesh_name = global_visible_bindings[0]
            for mesh in unresolved_meshes:
                raise_if_cancelled(stop_event)
                if str(getattr(mesh, "preview_texture_path", "") or "").strip():
                    continue
                try:
                    mesh.preview_texture_path = _ensure_archive_model_texture_preview_path(
                        resolved_texconv_path,
                        texture_entry,
                        stop_event=stop_event,
                    )
                    if force_unflipped_preview:
                        mesh.preview_texture_flip_vertical = False
                    current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
                    if not current_texture_name or not current_texture_name.lower().endswith(".dds"):
                        mesh.texture_name = texture_entry.path
                    current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
                    if submesh_name and not current_material_name:
                        mesh.material_name = submesh_name
                    assigned_count += 1
                except RunCancelled:
                    raise
                except Exception:
                    continue
        else:
            binding_index = 0
            for mesh in unresolved_meshes:
                raise_if_cancelled(stop_event)
                if str(getattr(mesh, "preview_texture_path", "") or "").strip():
                    continue
                if binding_index >= len(global_visible_bindings):
                    break
                texture_entry, _parameter_name, submesh_name = global_visible_bindings[binding_index]
                binding_index += 1
                try:
                    mesh.preview_texture_path = _ensure_archive_model_texture_preview_path(
                        resolved_texconv_path,
                        texture_entry,
                        stop_event=stop_event,
                    )
                    if force_unflipped_preview:
                        mesh.preview_texture_flip_vertical = False
                    current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
                    if not current_texture_name or not current_texture_name.lower().endswith(".dds"):
                        mesh.texture_name = texture_entry.path
                    current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
                    if submesh_name and not current_material_name:
                        mesh.material_name = submesh_name
                    assigned_count += 1
                except RunCancelled:
                    raise
                except Exception:
                    continue

    if assigned_count <= 0:
        return []
    sidecar_suffix = f" from {', '.join(sidecar_paths[:2])}" if sidecar_paths else ""
    if len(sidecar_paths) > 2:
        sidecar_suffix += " ..."
    info_lines = [
        f"Applied {assigned_count:,} textured preview binding(s) from companion material sidecar data{sidecar_suffix}."
    ]
    if promoted_anonymous_fallback:
        info_lines.append(
            "Used a sidecar texture fallback because the recovered mesh preview did not preserve a reliable submesh/material name match."
        )
    return info_lines


def _attach_model_texture_preview_paths(
    texconv_path: Optional[Path],
    source_entry: ArchiveEntry,
    model_preview: Optional[ModelPreviewData],
    *,
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    if texconv_path is None or model_preview is None or not model_preview.meshes:
        return []

    resolved_texconv_path = texconv_path.expanduser().resolve()
    preview_cache: Dict[str, str] = {}
    resolved_count = 0
    unresolved_lookup_count = 0
    technical_skip_count = 0
    preview_failure_count = 0
    sidecar_bound_count = 0
    unresolved_lookup_names: List[str] = []
    technical_skip_names: List[str] = []
    preview_failure_names: List[str] = []
    force_unflipped_preview = str(getattr(source_entry, "extension", "") or "").lower() == ".pac"

    for mesh in model_preview.meshes:
        raise_if_cancelled(stop_event)
        if str(getattr(mesh, "preview_texture_path", "") or "").strip():
            resolved_count += 1
            sidecar_bound_count += 1
            continue
        texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        material_name = str(getattr(mesh, "material_name", "") or "").strip()
        texture_label = texture_name or material_name
        if not texture_label:
            continue

        texture_entry, resolution_status = _resolve_model_texture_archive_entry(
            source_entry,
            texture_name,
            material_name,
            texture_entries_by_normalized_path,
            texture_entries_by_basename,
            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename=sidecar_texts_by_basename,
        )
        if texture_entry is None:
            if resolution_status == "technical_only":
                technical_skip_count += 1
                if texture_label not in technical_skip_names and len(technical_skip_names) < 5:
                    technical_skip_names.append(texture_label)
            else:
                unresolved_lookup_count += 1
                if texture_label not in unresolved_lookup_names and len(unresolved_lookup_names) < 5:
                    unresolved_lookup_names.append(texture_label)
            continue

        cache_key = _normalize_model_texture_reference(texture_entry.path)
        preview_path_text = preview_cache.get(cache_key, "")
        if not preview_path_text:
            try:
                preview_path_text = _ensure_archive_model_texture_preview_path(
                    resolved_texconv_path,
                    texture_entry,
                    stop_event=stop_event,
                )
                preview_cache[cache_key] = preview_path_text
            except RunCancelled:
                raise
            except Exception:
                preview_failure_count += 1
                if texture_label not in preview_failure_names and len(preview_failure_names) < 5:
                    preview_failure_names.append(texture_label)
                continue

        mesh.preview_texture_path = preview_path_text
        if force_unflipped_preview:
            mesh.preview_texture_flip_vertical = False
        resolved_count += 1

    info_lines: List[str] = []
    if resolved_count > 0:
        if sidecar_bound_count > 0 and sidecar_bound_count >= resolved_count:
            info_lines.append(
                f"Resolved {resolved_count:,} mesh texture preview(s) for textured shading and export using sidecar-aware material bindings."
            )
        elif sidecar_bound_count > 0:
            info_lines.append(
                f"Resolved {resolved_count:,} mesh texture preview(s) for textured shading and export "
                f"({sidecar_bound_count:,} via sidecar-aware bindings, remaining matches via semantic base-color fallback)."
            )
        else:
            info_lines.append(
                f"Resolved {resolved_count:,} mesh texture preview(s) for textured shading and export using semantic base-color selection only."
            )
    if unresolved_lookup_count > 0:
        lookup_suffix = f" Examples: {', '.join(unresolved_lookup_names)}." if unresolved_lookup_names else ""
        info_lines.append(
            f"{unresolved_lookup_count:,} referenced texture(s) could not be found in the archive index.{lookup_suffix}"
        )
    if technical_skip_count > 0:
        technical_suffix = f" Examples: {', '.join(technical_skip_names)}." if technical_skip_names else ""
        info_lines.append(
            f"{technical_skip_count:,} mesh texture reference(s) were skipped because only technical DDS matches were found.{technical_suffix}"
        )
    if preview_failure_count > 0:
        failure_suffix = f" Examples: {', '.join(preview_failure_names)}." if preview_failure_names else ""
        info_lines.append(
            f"{preview_failure_count:,} resolved texture(s) failed during DDS-to-PNG preview generation.{failure_suffix}"
        )
    return info_lines


def _describe_model_texture_semantic_label(
    texture_path: str,
    *,
    semantic_hint: str = "",
    sidecar_texts: Sequence[str] = (),
) -> str:
    hint_label = _humanize_model_texture_hint(semantic_hint)
    if hint_label:
        return hint_label
    texture_type_raw, subtype_raw, _confidence = _resolve_model_texture_semantics(
        texture_path,
        sidecar_texts=sidecar_texts,
    )
    texture_type = str(texture_type_raw or "").strip().replace("_", " ")
    subtype = str(subtype_raw or "").strip().replace("_", " ")
    if not texture_type or texture_type.lower() == "unknown":
        return hint_label
    hint_priority = _model_texture_hint_priority(semantic_hint)
    if hint_label and hint_priority is not None and hint_priority[0] >= 5 and texture_type.lower() not in {"color", "ui", "emissive"}:
        return hint_label
    if subtype and subtype.lower() not in {"unknown", texture_type.lower()}:
        return f"{texture_type.title()} / {subtype.title()}"
    return texture_type.title()


def _describe_model_related_file_label(entry: ArchiveEntry) -> str:
    extension = str(entry.extension or "").strip().lower()
    if extension == ".pam":
        return "Companion PAM"
    if extension == ".pamlod":
        return "Companion PAMLOD"
    if extension == ".pac":
        return "Companion PAC"
    if extension == ".pab":
        return "Companion PAB"
    if extension == ".xml":
        return "Companion XML"
    if extension == ".pami":
        return "Companion PAMI"
    if extension == ".hkx":
        return "Companion HKX"
    if extension == ".meshinfo":
        return "Companion MeshInfo"
    if extension == ".paa":
        return "Companion PAA"
    if extension in {".pae", ".paem"}:
        return "Companion Effect"
    if extension:
        return f"Companion {extension.lstrip('.').upper()}"
    return "Related File"


def _merge_model_reference_semantic_label(
    existing_label: str,
    new_label: str,
    *,
    existing_hint: str = "",
    new_hint: str = "",
) -> str:
    current = str(existing_label or "").strip()
    incoming = str(new_label or "").strip()
    if not current:
        return incoming
    if not incoming or incoming == current:
        return current
    if not str(existing_hint or "").strip() and str(new_hint or "").strip():
        return incoming
    if str(existing_hint or "").strip() and not str(new_hint or "").strip():
        return current
    parts = [part.strip() for part in current.split(" | ") if part.strip()]
    if incoming not in parts:
        parts.append(incoming)
    return " | ".join(parts)


def _model_reference_status_rank(status: str) -> int:
    normalized = str(status or "").strip().lower()
    if normalized == "resolved":
        return 3
    if normalized == "technical_only":
        return 2
    return 1


def build_archive_model_texture_references(
    source_entry: ArchiveEntry,
    model_preview: Optional[ModelPreviewData],
    *,
    parsed_mesh: Optional[object] = None,
    binary_texture_references: Sequence[str] = (),
    sidecar_texture_references: Sequence[_ArchiveModelSidecarTextureBinding] = (),
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> List[ArchiveModelTextureReference]:
    preview_meshes = list(getattr(model_preview, "meshes", ()) or [])
    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    related_companion_entries = (
        _find_archive_model_related_entries(source_entry, texture_entries_by_basename)
        if texture_entries_by_basename is not None
        else ()
    )

    if (
        not preview_meshes
        and not parsed_submeshes
        and not binary_texture_references
        and not sidecar_texture_references
        and not related_companion_entries
    ):
        return []

    references: Dict[Tuple[str, str], ArchiveModelTextureReference] = {}
    ordered_keys: List[Tuple[str, str]] = []

    for related_entry in related_companion_entries:
        related_key = ("sidecar", _normalize_model_texture_reference(related_entry.path))
        if related_key in references:
            continue
        references[related_key] = ArchiveModelTextureReference(
            reference_name=PurePosixPath(related_entry.path.replace("\\", "/")).name,
            semantic_label=_describe_model_related_file_label(related_entry),
            resolution_status="resolved",
            resolved_archive_path=related_entry.path,
            resolved_package_label=related_entry.package_label,
            resolved_entry=related_entry,
            usage_count=1,
            reference_kind="sidecar",
        )
        ordered_keys.append(related_key)

    candidates: List[Tuple[str, str, str, str]] = []
    seen_candidate_keys: set[Tuple[str, str, str]] = set()
    for binding in sidecar_texture_references:
        texture_name = str(binding.texture_path or "").strip()
        material_name = str(binding.submesh_name or binding.parameter_name or "").strip()
        semantic_hint = str(binding.parameter_name or "").strip()
        key = (
            _normalize_model_texture_reference(texture_name),
            _normalize_model_texture_reference(material_name),
            str(semantic_hint or "").strip().lower(),
        )
        if not texture_name or key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidates.append((texture_name, material_name, "", semantic_hint))
    for mesh in preview_meshes:
        texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        material_name = str(getattr(mesh, "material_name", "") or "").strip()
        key = (
            _normalize_model_texture_reference(texture_name),
            _normalize_model_texture_reference(material_name),
            "",
        )
        seen_candidate_keys.add(key)
        candidates.append(
            (
                texture_name,
                material_name,
                str(getattr(mesh, "preview_texture_path", "") or "").strip(),
                "",
            )
        )
    for submesh in parsed_submeshes:
        texture_name = str(getattr(submesh, "texture", "") or "").strip()
        material_name = str(getattr(submesh, "material", "") or "").strip()
        key = (
            _normalize_model_texture_reference(texture_name),
            _normalize_model_texture_reference(material_name),
            "",
        )
        if key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidates.append((texture_name, material_name, "", ""))
    for raw_reference in binary_texture_references:
        texture_name = str(raw_reference or "").strip()
        if not texture_name:
            continue
        key = (_normalize_model_texture_reference(texture_name), "", "")
        if key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidates.append((texture_name, "", "", ""))

    for texture_name, material_name, preview_texture_path, semantic_hint in candidates:
        reference_name = texture_name or material_name
        if not reference_name:
            continue

        texture_entry, resolution_status = _resolve_model_texture_archive_entry(
            source_entry,
            texture_name,
            material_name,
            texture_entries_by_normalized_path,
            texture_entries_by_basename,
            semantic_hint=semantic_hint,
            expand_family_candidates=not _has_explicit_model_texture_reference(texture_name, material_name),
            allow_technical_match=True,
            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename=sidecar_texts_by_basename,
        )
        resolved_archive_path = texture_entry.path if texture_entry is not None else ""
        reference_key_value = _normalize_model_texture_reference(resolved_archive_path or reference_name)
        key = ("texture", reference_key_value)
        sidecar_texts: Tuple[str, ...] = ()
        normalized_reference_path = normalize_texture_reference_for_sidecar_lookup(resolved_archive_path or reference_name)
        if sidecar_texts_by_normalized_path is not None and normalized_reference_path:
            sidecar_texts = tuple(sidecar_texts_by_normalized_path.get(normalized_reference_path, ()))
        if not sidecar_texts and sidecar_texts_by_basename is not None:
            reference_basename = PurePosixPath(
                (resolved_archive_path or reference_name).replace("\\", "/")
            ).name.lower()
            if reference_basename:
                sidecar_texts = tuple(sidecar_texts_by_basename.get(reference_basename, ()))
        semantic_label = _describe_model_texture_semantic_label(
            resolved_archive_path or reference_name,
            semantic_hint=semantic_hint,
            sidecar_texts=sidecar_texts,
        )
        resolved_package_label = texture_entry.package_label if texture_entry is not None else ""
        existing = references.get(key)
        if existing is None:
            references[key] = ArchiveModelTextureReference(
                reference_name=reference_name,
                material_name=material_name,
                semantic_label=semantic_label,
                semantic_hint=semantic_hint,
                sidecar_texts=sidecar_texts,
                resolution_status=resolution_status,
                resolved_archive_path=resolved_archive_path,
                resolved_package_label=resolved_package_label,
                resolved_entry=texture_entry,
                preview_texture_path=preview_texture_path,
                usage_count=1,
                reference_kind="texture",
            )
            ordered_keys.append(key)
            continue

        existing.usage_count += 1
        if material_name and not existing.material_name:
            existing.material_name = material_name
        if preview_texture_path and not existing.preview_texture_path:
            existing.preview_texture_path = preview_texture_path
        if texture_entry is not None and (
            existing.resolved_entry is None
            or _model_reference_status_rank(resolution_status) > _model_reference_status_rank(existing.resolution_status)
        ):
            existing.resolved_entry = texture_entry
            existing.resolved_archive_path = texture_entry.path
            existing.resolved_package_label = texture_entry.package_label
            existing.resolution_status = resolution_status
        elif _model_reference_status_rank(resolution_status) > _model_reference_status_rank(existing.resolution_status):
            existing.resolution_status = resolution_status
        if semantic_label:
            existing.semantic_label = _merge_model_reference_semantic_label(
                existing.semantic_label,
                semantic_label,
                existing_hint=existing.semantic_hint,
                new_hint=semantic_hint,
            )
        if semantic_hint and semantic_hint != existing.semantic_hint:
            existing.semantic_hint = " | ".join(
                part
                for part in [existing.semantic_hint.strip(), semantic_hint.strip()]
                if part
            )
        if sidecar_texts:
            merged_sidecar_texts = list(existing.sidecar_texts)
            for text in sidecar_texts:
                if text not in merged_sidecar_texts:
                    merged_sidecar_texts.append(text)
            existing.sidecar_texts = tuple(merged_sidecar_texts)

    return [references[key] for key in ordered_keys]


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
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[str, str, str]:
    resolved_path = loose_path.expanduser().resolve()
    suffix = resolved_path.suffix.lower()
    detail = f"Loose file preview from: {resolved_path}"
    raise_if_cancelled(stop_event)

    if suffix == ".dds":
        dds_info = None
        parse_error: Optional[Exception] = None
        try:
            dds_info = parse_dds(resolved_path)
            metadata_summary = (
                f"Loose DDS | Format: {dds_info.texconv_format} | "
                f"Size: {dds_info.width}x{dds_info.height} | Mips: {dds_info.mip_count}"
            )
        except Exception as exc:
            parse_error = exc
            metadata_summary = f"Loose DDS | {resolved_path.name}"
        if texconv_path is None:
            extra = f"\nDDS metadata unavailable: {parse_error}" if parse_error is not None else ""
            return "", metadata_summary, detail + extra + "\nSet texconv.exe to enable DDS loose-file previews."
        preview_png = ensure_dds_display_preview_png(
            texconv_path.resolve(),
            resolved_path,
            dds_info=dds_info,
            stop_event=stop_event,
        )
        if parse_error is not None:
            detail += f"\nDDS metadata unavailable: {parse_error}"
        return str(preview_png), metadata_summary, detail

    if suffix in ARCHIVE_IMAGE_EXTENSIONS:
        return str(resolved_path), f"Loose image | {resolved_path.name}", detail

    return "", f"Loose file | {resolved_path.name}", detail + "\nThis loose file type cannot be previewed as an image."


def _format_media_duration_millis(duration_ms: int) -> str:
    total_seconds = max(0, int(duration_ms // 1000))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def _runtime_search_roots() -> List[Path]:
    roots: List[Path] = []
    seen: set[str] = set()

    def add_root(candidate: Optional[Path]) -> None:
        if candidate is None:
            return
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            resolved = candidate.expanduser()
        lowered = str(resolved).lower()
        if not lowered or lowered in seen:
            return
        seen.add(lowered)
        roots.append(resolved)

    if getattr(sys, "frozen", False):
        add_root(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            add_root(Path(str(meipass)))
    add_root(Path(__file__).resolve().parents[2])
    return roots


def _resolve_vgmstream_cli_path() -> Optional[Path]:
    candidate_names = ("vgmstream-cli.exe", "test.exe")
    for root in _runtime_search_roots():
        for relative_dir in ("vgmstream", ".tools/vgmstream"):
            base_dir = root / relative_dir
            for candidate_name in candidate_names:
                candidate_path = base_dir / candidate_name
                if candidate_path.is_file():
                    return candidate_path
    return None


def _decode_wem_with_vgmstream(
    source_path: Path,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[Optional[Path], str]:
    cli_path = _resolve_vgmstream_cli_path()
    if cli_path is None:
        return None, "Bundled vgmstream decoder is not available in this build."

    output_path = source_path.with_name(f"{sanitize_cache_filename(source_path.stem)}.vgmstream.wav")
    if output_path.exists():
        try:
            if output_path.stat().st_size > 44 and output_path.stat().st_mtime_ns >= source_path.stat().st_mtime_ns:
                return output_path, "Decoded for playback with bundled vgmstream-cli."
        except OSError:
            pass

    command = [str(cli_path), "-o", str(output_path), str(source_path)]
    popen_kwargs: Dict[str, object] = {
        "cwd": str(cli_path.parent),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if creation_flags:
            popen_kwargs["creationflags"] = creation_flags
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        popen_kwargs["startupinfo"] = startup_info
    process = subprocess.Popen(
        command,
        **popen_kwargs,
    )
    try:
        while True:
            try:
                return_code = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                raise_if_cancelled(stop_event)
        stderr_text = ""
        if process.stderr is not None:
            try:
                stderr_text = process.stderr.read().strip()
            except Exception:
                stderr_text = ""
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        raise
    finally:
        if process.stderr is not None:
            try:
                process.stderr.close()
            except Exception:
                pass

    if return_code != 0 or not output_path.exists():
        return None, stderr_text or "vgmstream-cli could not decode this Wwise stream."
    return output_path, "Decoded for playback with bundled vgmstream-cli."


def _ensure_media_preview_source_path(
    source_path: Path,
    declared_extension: str,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[Path, str]:
    resolved_source = source_path.expanduser().resolve()
    normalized_extension = str(declared_extension or resolved_source.suffix).strip().lower()
    if normalized_extension != ".wem":
        return resolved_source, ""

    decoded_wav_path, decode_note = _decode_wem_with_vgmstream(
        resolved_source,
        stop_event=stop_event,
    )
    if decoded_wav_path is not None:
        return decoded_wav_path, decode_note

    raise_if_cancelled(stop_event)
    try:
        with resolved_source.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return resolved_source, decode_note
    if len(header) < 12 or not header.startswith(b"RIFF") or header[8:12] != b"WAVE":
        return resolved_source, decode_note

    alias_path = resolved_source.with_suffix(".wav")
    if alias_path == resolved_source:
        return resolved_source, decode_note
    if alias_path.exists() and alias_path.stat().st_size == resolved_source.stat().st_size:
        return alias_path, decode_note

    shutil.copy2(resolved_source, alias_path)
    return alias_path, decode_note


def _iter_riff_chunks(
    data: bytes,
    *,
    max_chunks: int = 32,
) -> List[Tuple[str, int, int]]:
    chunks: List[Tuple[str, int, int]] = []
    if len(data) < 12 or not data.startswith(b"RIFF"):
        return chunks
    offset = 12
    while offset + 8 <= len(data) and len(chunks) < max_chunks:
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        chunk_name = chunk_id.decode("ascii", errors="replace")
        data_offset = offset + 8
        if data_offset > len(data):
            break
        chunks.append((chunk_name, chunk_size, data_offset))
        next_offset = data_offset + chunk_size
        if next_offset <= offset:
            break
        offset = next_offset + (chunk_size % 2)
    return chunks


def _build_wem_media_preview_detail_text(
    source_path: Path,
    data: bytes,
    *,
    loose: bool,
    playback_source_path: Optional[Path] = None,
    playback_note: str = "",
) -> Tuple[str, str]:
    resolved_source = source_path.expanduser().resolve()
    metadata_summary = f"{'Loose' if loose else 'Archive'} Wwise audio | {resolved_source.name}"
    detail_lines = [f"{'Loose file' if loose else 'Archive preview source'}: {resolved_source}"]
    if playback_source_path is not None:
        resolved_playback = playback_source_path.expanduser().resolve()
        if resolved_playback != resolved_source:
            detail_lines.append(f"Playback source: {resolved_playback}")
    if playback_note:
        detail_lines.append(playback_note)
    if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        detail_lines.append("Container sniffing did not confirm a RIFF/WAVE-style Wwise stream. Playback support may depend on the local multimedia backend.")
        return metadata_summary, "\n".join(detail_lines)

    detail_lines.append("Detected RIFF/WAVE-style Wwise audio container.")
    fmt_channels = None
    fmt_sample_rate = None
    fmt_bits_per_sample = None
    chunk_names: List[str] = []
    for chunk_name, chunk_size, chunk_offset in _iter_riff_chunks(data):
        chunk_names.append(f"{chunk_name} ({chunk_size:,} B)")
        if chunk_name == "fmt " and chunk_size >= 16 and chunk_offset + 16 <= len(data):
            try:
                _audio_format, fmt_channels, fmt_sample_rate, _byte_rate, _block_align, fmt_bits_per_sample = struct.unpack_from(
                    "<HHIIHH",
                    data,
                    chunk_offset,
                )
            except struct.error:
                fmt_channels = None
                fmt_sample_rate = None
                fmt_bits_per_sample = None
    if fmt_channels is not None and fmt_sample_rate is not None:
        metadata_summary = (
            f"{metadata_summary} | {fmt_channels} ch | {fmt_sample_rate:,} Hz"
            + (f" | {fmt_bits_per_sample}-bit" if fmt_bits_per_sample is not None else "")
        )
    if chunk_names:
        detail_lines.append("RIFF chunks: " + ", ".join(chunk_names[:12]))
    detail_lines.append(
        "Playback is best-effort through Qt Multimedia. Some Wwise `.wem` variants may still fail if the local backend cannot decode them."
    )
    return metadata_summary, "\n".join(detail_lines)


def _build_mp4_media_preview_detail_text(
    source_path: Path,
    *,
    loose: bool,
) -> Tuple[str, str]:
    resolved_source = source_path.expanduser().resolve()
    metadata_summary = f"{'Loose' if loose else 'Archive'} video | {resolved_source.name}"
    detail_lines = [
        f"{'Loose file' if loose else 'Archive preview source'}: {resolved_source}",
        "Embedded playback uses Qt Multimedia.",
    ]
    return metadata_summary, "\n".join(detail_lines)


def build_loose_archive_media_preview_assets(
    loose_path: Path,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[str, str, str, str]:
    resolved_path = loose_path.expanduser().resolve()
    suffix = resolved_path.suffix.lower()
    raise_if_cancelled(stop_event)

    if suffix in ARCHIVE_VIDEO_EXTENSIONS:
        metadata_summary, detail_text = _build_mp4_media_preview_detail_text(resolved_path, loose=True)
        return str(resolved_path), "video", metadata_summary, detail_text

    if suffix in ARCHIVE_AUDIO_EXTENSIONS:
        media_source, playback_note = _ensure_media_preview_source_path(
            resolved_path,
            suffix,
            stop_event=stop_event,
        )
        try:
            with resolved_path.open("rb") as handle:
                sample = handle.read(131072)
        except OSError:
            sample = b""
        metadata_summary, detail_text = _build_wem_media_preview_detail_text(
            resolved_path,
            sample,
            loose=True,
            playback_source_path=media_source,
            playback_note=playback_note,
        )
        return str(media_source), "audio", metadata_summary, detail_text

    return "", "", f"Loose file | {resolved_path.name}", f"Loose file preview from: {resolved_path}"


def _iter_bnk_chunks(
    data: bytes,
    *,
    max_chunks: int = 32,
) -> List[Tuple[str, int, int]]:
    chunks: List[Tuple[str, int, int]] = []
    offset = 0
    while offset + 8 <= len(data) and len(chunks) < max_chunks:
        chunk_name = data[offset : offset + 4].decode("ascii", errors="replace")
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        data_offset = offset + 8
        if data_offset + chunk_size > len(data):
            break
        chunks.append((chunk_name, chunk_size, data_offset))
        next_offset = data_offset + chunk_size
        aligned_offset = (next_offset + 3) & ~3
        if aligned_offset <= offset:
            break
        offset = aligned_offset
    return chunks


def build_bnk_soundbank_preview(data: bytes) -> Tuple[str, str]:
    if len(data) < 8 or data[:4] != b"BKHD":
        return "", ""

    chunk_rows = _iter_bnk_chunks(data)
    if not chunk_rows:
        return "Detected Wwise soundbank container.", "Wwise soundbank preview is limited because the bank does not expose readable chunk boundaries."

    detail_lines = ["Detected Wwise soundbank container."]
    preview_lines = ["Wwise soundbank summary:"]
    chunk_descriptions: List[str] = []
    embedded_media_count = 0
    embedded_media_examples: List[str] = []
    hirc_object_count = None
    bank_version = None
    bank_id = None

    for chunk_name, chunk_size, chunk_offset in chunk_rows:
        chunk_descriptions.append(f"{chunk_name} ({chunk_size:,} B)")
        if chunk_name == "BKHD" and chunk_size >= 8:
            try:
                bank_version, bank_id = struct.unpack_from("<II", data, chunk_offset)
            except struct.error:
                bank_version = None
                bank_id = None
        elif chunk_name == "DIDX" and chunk_size >= 12:
            embedded_media_count = chunk_size // 12
            preview_lines.append(f"- Embedded media entries: {embedded_media_count:,}")
            for media_index in range(min(8, embedded_media_count)):
                media_id, media_offset, media_size = struct.unpack_from("<III", data, chunk_offset + media_index * 12)
                embedded_media_examples.append(
                    f"{media_id} @ {media_offset:,} ({format_byte_size(media_size)})"
                )
        elif chunk_name == "HIRC" and chunk_size >= 4:
            try:
                hirc_object_count = struct.unpack_from("<I", data, chunk_offset)[0]
            except struct.error:
                hirc_object_count = None

    if bank_version is not None:
        preview_lines.append(f"- Bank version: {bank_version}")
    if bank_id is not None:
        preview_lines.append(f"- Bank id: {bank_id}")
    preview_lines.append(f"- Top-level chunks: {', '.join(chunk_name for chunk_name, _chunk_size, _chunk_offset in chunk_rows)}")
    if hirc_object_count is not None:
        preview_lines.append(f"- HIRC objects: {hirc_object_count:,}")
    if embedded_media_examples:
        preview_lines.append("- First embedded media ids:")
        preview_lines.extend(f"  {example}" for example in embedded_media_examples)

    readable_strings = extract_binary_strings(data, sample_limit=262144, max_strings=24)
    if readable_strings:
        preview_lines.append("- Readable strings:")
        preview_lines.extend(f"  {text}" for text in readable_strings[:16])

    detail_lines.append("Top-level chunks: " + ", ".join(chunk_descriptions[:16]))
    if embedded_media_count:
        detail_lines.append(
            f"Embedded media index contains {embedded_media_count:,} item(s). These can be inspected, but direct bank playback is not exposed yet."
        )
    else:
        detail_lines.append("No embedded media index entries were detected in the top-level DIDX chunk.")

    return "\n".join(preview_lines), "\n".join(detail_lines)


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


def try_decode_text_like_archive_data(data: bytes) -> Optional[str]:
    if not data:
        return None

    preview_bytes = data[:ARCHIVE_TEXT_PREVIEW_LIMIT]
    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if preview_bytes.startswith(bom):
            text = preview_bytes.decode(encoding, errors="replace")
            return text if text.strip("\ufeff\r\n\t ") else None

    sample = preview_bytes[:4096]
    if not sample:
        return None
    if sample.count(0) > max(2, len(sample) // 100):
        return None

    printable_count = sum(1 for value in sample if value in (9, 10, 13) or 32 <= value <= 126)
    likely_text = printable_count / max(len(sample), 1) >= 0.92
    stripped_sample = sample.lstrip(b"\xef\xbb\xbf\r\n\t ")
    if not likely_text and not stripped_sample.startswith((b"<?xml", b"<", b"{", b"[")):
        return None

    text = preview_bytes.decode("utf-8", errors="replace")
    non_whitespace = [char for char in text[:1024] if not char.isspace()]
    if not non_whitespace:
        return None
    control_count = sum(1 for char in non_whitespace if ord(char) < 32 and char not in "\r\n\t")
    if control_count > max(2, len(non_whitespace) // 20):
        return None
    return text


def extract_binary_strings(data: bytes, *, sample_limit: int = 131_072, max_strings: int = 48) -> List[str]:
    sample = data[:sample_limit]
    strings: List[str] = []
    seen: set[str] = set()
    for match in _PRINTABLE_BINARY_STRING_RE.finditer(sample):
        text = match.group().decode("ascii", errors="ignore").strip()
        letter_count = sum(1 for char in text if char.isalpha())
        if len(text) < 4 or text in seen or letter_count == 0:
            continue
        allowed_char_count = sum(1 for char in text if char.isalnum() or char in " _./:-[](){}")
        if allowed_char_count / max(len(text), 1) < 0.85:
            continue
        if len(text) < 12 and letter_count < 4:
            continue
        if "_" not in text and "/" not in text and "::" not in text and " " not in text and len(text) < 12:
            continue
        if len(text) > 160:
            text = text[:157] + "..."
        seen.add(text)
        strings.append(text)
        if len(strings) >= max_strings:
            break
    return strings


def build_binary_strings_preview(data: bytes, *, sample_limit: int = 131_072, max_strings: int = 48) -> str:
    strings = extract_binary_strings(data, sample_limit=sample_limit, max_strings=max_strings)
    if not strings:
        return ""
    scanned_size = min(len(data), sample_limit)
    lines = [f"Readable strings from the first {format_byte_size(scanned_size)} of binary data:"]
    lines.extend(strings)
    if len(data) > sample_limit:
        lines.extend(["", "String scan truncated to keep the preview responsive."])
    return "\n".join(lines)


def _looks_like_structured_field_name(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 3 or len(text) > 128:
        return False
    if "/" in text or "\\" in text:
        return False
    if "." in text and "::" not in text:
        return False
    if " " in text or "\t" in text:
        return False
    if not _STRUCTURED_BINARY_IDENTIFIER_RE.fullmatch(text):
        return False
    return any(character.isalpha() for character in text)


def _extract_binary_asset_references(
    data: bytes,
    *,
    sample_limit: int = 262_144,
    max_references: int = 64,
) -> List[str]:
    references: List[str] = []
    seen: set[str] = set()
    for text in extract_binary_strings(data, sample_limit=sample_limit, max_strings=max(max_references * 6, 96)):
        for match in _STRUCTURED_BINARY_ASSET_REFERENCE_RE.finditer(text):
            raw_text = str(match.group(0) or "").strip().strip("\x00").replace("\\", "/")
            if not raw_text or not any(character.isalpha() for character in raw_text):
                continue
            normalized = _normalize_model_texture_reference(raw_text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            references.append(raw_text)
            if len(references) >= max_references:
                return references
    return references


def _structured_field_type_hint(name: str) -> str:
    normalized = str(name or "").strip().lstrip("_").lower()
    if not normalized:
        return "field"
    if "reflectobject" in normalized or normalized.endswith("ptr") or "referencepath" in normalized:
        return "object ref"
    if "list" in normalized or "container" in normalized or "array" in normalized:
        return "list"
    if normalized.startswith(("is", "use", "enable", "disable", "auto", "apply", "has")):
        return "bool"
    if any(token in normalized for token in ("boundingbox", "bbox", "bound", "extent", "position", "rotation", "scale", "offset", "radius")):
        return "vector"
    if normalized.endswith(("type", "enum", "flag", "flags", "layer", "group")):
        return "enum/flag"
    return "field"


def _group_meshinfo_field_name(name: str) -> str:
    normalized = str(name or "").strip().lstrip("_").lower()
    if not normalized:
        return "Misc"
    if any(token in normalized for token in ("boundingbox", "bbox", "bound", "extent", "volume", "radius", "min", "max")):
        return "Bounds"
    if any(token in normalized for token in ("socket", "anchor", "attach")):
        return "Sockets"
    if any(token in normalized for token in ("tree", "branch", "cutting")):
        return "Tree"
    if any(token in normalized for token in ("break", "support", "fade", "convex", "fracture")):
        return "Breakable"
    if any(token in normalized for token in ("collision", "collidable", "constraint", "group", "layer")):
        return "Collision"
    if any(token in normalized for token in ("physics", "motion", "mass", "buoyancy", "dynamic", "pbd", "wind", "material")):
        return "Physics"
    if any(token in normalized for token in ("reflectobject", "vector", "container", "custom", "gamedata", "node")):
        return "Data Model"
    return "Misc"


def _group_animation_field_name(name: str) -> str:
    normalized = str(name or "").strip().lstrip("_").lower()
    if not normalized:
        return "Misc"
    if any(token in normalized for token in ("animation", "clip", "frame", "curve", "track", "event")):
        return "Animation"
    if any(token in normalized for token in ("motion", "blend", "space", "parameter")):
        return "Motion / Blend"
    if any(token in normalized for token in ("emitter", "effect", "particle")):
        return "Emitter / Effect"
    if any(token in normalized for token in ("scene", "object", "node", "prefab")):
        return "Scene / Object"
    if any(token in normalized for token in ("resource", "texture", "material", "sound", "audio", "video")):
        return "Resources"
    return "Misc"


def _build_grouped_structured_section_lines(
    field_names: Sequence[str],
    *,
    group_func: Callable[[str], str],
    section_order: Sequence[str],
    per_section_limit: int = 24,
) -> List[str]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    for name in sorted({str(item or "").strip() for item in field_names if str(item or "").strip()}, key=str.casefold):
        grouped[group_func(name)].append(name)

    lines: List[str] = []
    for section_name in section_order:
        section_fields = grouped.get(section_name, [])
        if not section_fields:
            continue
        lines.extend(["", f"{section_name} ({len(section_fields)})"])
        for field_name in section_fields[:per_section_limit]:
            lines.append(f"  [{_structured_field_type_hint(field_name)}] {field_name}")
        if len(section_fields) > per_section_limit:
            lines.append(f"  ... {len(section_fields) - per_section_limit} more")

    remaining_sections = [
        section_name
        for section_name, section_fields in grouped.items()
        if section_name not in section_order and section_fields
    ]
    for section_name in sorted(remaining_sections, key=str.casefold):
        section_fields = grouped.get(section_name, [])
        if not section_fields:
            continue
        lines.extend(["", f"{section_name} ({len(section_fields)})"])
        for field_name in section_fields[:per_section_limit]:
            lines.append(f"  [{_structured_field_type_hint(field_name)}] {field_name}")
        if len(section_fields) > per_section_limit:
            lines.append(f"  ... {len(section_fields) - per_section_limit} more")

    return lines


def _score_related_reference_candidate(
    source_entry: ArchiveEntry,
    candidate: ArchiveEntry,
    *,
    reference_name: str = "",
) -> Tuple[int, int, int]:
    normalized_reference = _normalize_model_texture_reference(reference_name)
    normalized_candidate = _normalize_model_texture_reference(candidate.path)
    reference_basename = PurePosixPath(normalized_reference).name if normalized_reference else ""
    candidate_basename = PurePosixPath(normalized_candidate).name
    source_root = PurePosixPath(_normalize_model_texture_reference(source_entry.path)).parts[:1]
    candidate_root = PurePosixPath(normalized_candidate).parts[:1]
    score_value = 0
    if normalized_reference and normalized_candidate == normalized_reference:
        score_value += 20
    if reference_basename and candidate_basename == reference_basename:
        score_value += 10
    if candidate.pamt_path == source_entry.pamt_path:
        score_value += 8
    if candidate.pamt_path.parent == source_entry.pamt_path.parent:
        score_value += 5
    if source_root and candidate_root and source_root == candidate_root:
        score_value += 3
    return score_value, -len(candidate.path), 0


def _resolve_related_archive_entry(
    source_entry: ArchiveEntry,
    reference_name: str,
    *,
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> Optional[ArchiveEntry]:
    normalized_reference = _normalize_model_texture_reference(reference_name)
    candidates: List[ArchiveEntry] = []
    seen_paths: set[str] = set()

    if archive_entries_by_normalized_path is not None and normalized_reference:
        for candidate in archive_entries_by_normalized_path.get(normalized_reference, ()):
            normalized_candidate = _normalize_model_texture_reference(candidate.path)
            if normalized_candidate in seen_paths or normalized_candidate == _normalize_model_texture_reference(source_entry.path):
                continue
            seen_paths.add(normalized_candidate)
            candidates.append(candidate)

    reference_basename = PurePosixPath(normalized_reference or reference_name.replace("\\", "/")).name.lower()
    if archive_entries_by_basename is not None and reference_basename:
        for candidate in archive_entries_by_basename.get(reference_basename, ()):
            normalized_candidate = _normalize_model_texture_reference(candidate.path)
            if normalized_candidate in seen_paths or normalized_candidate == _normalize_model_texture_reference(source_entry.path):
                continue
            seen_paths.add(normalized_candidate)
            candidates.append(candidate)

    if not candidates:
        return None

    candidates.sort(
        key=lambda candidate: _score_related_reference_candidate(
            source_entry,
            candidate,
            reference_name=reference_name,
        ),
        reverse=True,
    )
    return candidates[0]


def _describe_generic_related_reference_label(reference_name: str, resolved_entry: Optional[ArchiveEntry] = None) -> str:
    extension = str(getattr(resolved_entry, "extension", "") or PurePosixPath(reference_name.replace("\\", "/")).suffix).strip().lower()
    if extension == ".dds":
        semantic_label = _describe_model_texture_semantic_label(reference_name)
        return semantic_label or "Texture / DDS"
    if extension == ".xml":
        return "Related XML"
    if extension == ".pami":
        return "Related PAMI"
    if extension == ".meshinfo":
        return "Related MeshInfo"
    if extension == ".hkx":
        return "Related HKX"
    if extension == ".pab":
        return "Related PAB"
    if extension == ".pac":
        return "Related PAC"
    if extension == ".pam":
        return "Related PAM"
    if extension == ".pamlod":
        return "Related PAMLOD"
    if extension == ".paa":
        return "Related PAA"
    if extension in {".pae", ".paem"}:
        return "Related Effect"
    if extension:
        return f"Related {extension.lstrip('.').upper()}"
    return "Related File"


def build_archive_related_file_references(
    source_entry: ArchiveEntry,
    *,
    explicit_reference_names: Sequence[str] = (),
    companion_entries: Sequence[ArchiveEntry] = (),
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveModelTextureReference, ...]:
    references: Dict[Tuple[str, str], ArchiveModelTextureReference] = {}
    ordered_keys: List[Tuple[str, str]] = []

    for companion_entry in companion_entries:
        normalized_path = _normalize_model_texture_reference(companion_entry.path)
        if not normalized_path:
            continue
        key = ("file", normalized_path)
        if key in references:
            continue
        references[key] = ArchiveModelTextureReference(
            reference_name=PurePosixPath(companion_entry.path.replace("\\", "/")).name,
            semantic_label=_describe_model_related_file_label(companion_entry),
            resolution_status="resolved",
            resolved_archive_path=companion_entry.path,
            resolved_package_label=companion_entry.package_label,
            resolved_entry=companion_entry,
            usage_count=1,
            reference_kind="sidecar",
        )
        ordered_keys.append(key)

    for raw_reference_name in explicit_reference_names:
        reference_name = str(raw_reference_name or "").strip().replace("\\", "/")
        if not reference_name:
            continue
        resolved_entry = _resolve_related_archive_entry(
            source_entry,
            reference_name,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
        )
        normalized_key_value = _normalize_model_texture_reference(
            resolved_entry.path if isinstance(resolved_entry, ArchiveEntry) else reference_name
        )
        if not normalized_key_value or normalized_key_value == _normalize_model_texture_reference(source_entry.path):
            continue
        key = ("file", normalized_key_value)
        if key not in references:
            references[key] = ArchiveModelTextureReference(
                reference_name=reference_name,
                semantic_label=_describe_generic_related_reference_label(reference_name, resolved_entry),
                resolution_status="resolved" if isinstance(resolved_entry, ArchiveEntry) else "missing",
                resolved_archive_path=resolved_entry.path if isinstance(resolved_entry, ArchiveEntry) else "",
                resolved_package_label=resolved_entry.package_label if isinstance(resolved_entry, ArchiveEntry) else "",
                resolved_entry=resolved_entry if isinstance(resolved_entry, ArchiveEntry) else None,
                usage_count=1,
                reference_kind="file",
            )
            ordered_keys.append(key)
            continue
        references[key].usage_count += 1
        if reference_name and not references[key].reference_name:
            references[key].reference_name = reference_name
        if isinstance(resolved_entry, ArchiveEntry) and references[key].resolved_entry is None:
            references[key].resolved_entry = resolved_entry
            references[key].resolved_archive_path = resolved_entry.path
            references[key].resolved_package_label = resolved_entry.package_label
            references[key].resolution_status = "resolved"

    return tuple(references[key] for key in ordered_keys)


def build_meshinfo_preview(
    data: bytes,
    virtual_path: str,
    *,
    source_entry: Optional[ArchiveEntry] = None,
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> _StructuredBinaryPreviewBundle:
    strings = extract_binary_strings(data, sample_limit=262_144, max_strings=256)
    field_names = sorted({text for text in strings if _looks_like_structured_field_name(text)}, key=str.casefold)
    asset_references = _extract_binary_asset_references(data, sample_limit=262_144, max_references=64)
    companion_entries = (
        _find_archive_model_related_entries(source_entry, archive_entries_by_basename)
        if source_entry is not None and archive_entries_by_basename is not None
        else ()
    )
    related_references = (
        build_archive_related_file_references(
            source_entry,
            explicit_reference_names=asset_references,
            companion_entries=companion_entries,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
        )
        if source_entry is not None
        else ()
    )
    lines = [f"MeshInfo inspector for {virtual_path}", "", "Summary:"]
    lines.append(f"- Field-like entries: {len(field_names):,}")
    lines.append(f"- Related asset hints: {len(asset_references):,}")
    if companion_entries:
        lines.append(f"- Same-stem companion files: {len(companion_entries):,}")

    lines.extend(
        _build_grouped_structured_section_lines(
            field_names,
            group_func=_group_meshinfo_field_name,
            section_order=("Physics", "Breakable", "Tree", "Collision", "Sockets", "Bounds", "Data Model", "Misc"),
        )
    )
    if asset_references:
        lines.extend(["", "Detected asset references:"])
        lines.extend(f"  - {reference}" for reference in asset_references[:24])
        if len(asset_references) > 24:
            lines.append(f"  ... {len(asset_references) - 24} more")

    detail_lines = [
        f"Detected {len(field_names):,} field-like identifier(s) from the preview sample.",
        "Fields are deduped, sorted, and grouped heuristically from readable binary strings.",
    ]
    if asset_references:
        detail_lines.append(f"Detected {len(asset_references):,} related asset reference(s).")
    if companion_entries:
        detail_lines.append(f"Matched {len(companion_entries):,} same-stem companion archive file(s).")

    return _StructuredBinaryPreviewBundle(
        preview_text="\n".join(lines),
        detail_lines=tuple(detail_lines),
        related_references=related_references,
        metadata_label="Mesh Metadata",
    )


def build_par_structured_preview(
    data: bytes,
    virtual_path: str,
    *,
    extension: str,
    source_entry: Optional[ArchiveEntry] = None,
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> _StructuredBinaryPreviewBundle:
    strings = extract_binary_strings(data, sample_limit=262_144, max_strings=224)
    field_names = sorted({text for text in strings if _looks_like_structured_field_name(text)}, key=str.casefold)
    asset_references = _extract_binary_asset_references(data, sample_limit=262_144, max_references=64)
    markers = [
        marker
        for marker in ("AnimationMetaData", "ParameterizedMotionSpace", "Sequencer", "SceneObject", "EmitterData")
        if marker in data[:16_384].decode("latin-1", errors="ignore")
        or marker in strings
    ]
    companion_entries = (
        _find_archive_model_related_entries(source_entry, archive_entries_by_basename)
        if source_entry is not None and archive_entries_by_basename is not None
        else ()
    )
    related_references = (
        build_archive_related_file_references(
            source_entry,
            explicit_reference_names=asset_references,
            companion_entries=companion_entries,
            archive_entries_by_normalized_path=archive_entries_by_normalized_path,
            archive_entries_by_basename=archive_entries_by_basename,
        )
        if source_entry is not None
        else ()
    )

    normalized_extension = str(extension or "").strip().lower()
    if normalized_extension == ".paa":
        title = "PAA animation inspector"
        metadata_label = "Animation"
    elif normalized_extension in {".pae", ".paem"}:
        title = "PAE effect inspector"
        metadata_label = "Effect"
    else:
        title = f"{normalized_extension.lstrip('.').upper()} structured inspector"
        metadata_label = "Structured Binary"

    lines = [f"{title} for {virtual_path}", "", "Summary:"]
    lines.append(f"- Field-like entries: {len(field_names):,}")
    if markers:
        lines.append(f"- Detected markers: {', '.join(markers)}")
    if asset_references:
        lines.append(f"- Related asset hints: {len(asset_references):,}")
    if companion_entries:
        lines.append(f"- Same-stem companion files: {len(companion_entries):,}")

    lines.extend(
        _build_grouped_structured_section_lines(
            field_names,
            group_func=_group_animation_field_name,
            section_order=("Animation", "Motion / Blend", "Emitter / Effect", "Scene / Object", "Resources", "Misc"),
        )
    )
    if asset_references:
        lines.extend(["", "Detected asset references:"])
        lines.extend(f"  - {reference}" for reference in asset_references[:24])
        if len(asset_references) > 24:
            lines.append(f"  ... {len(asset_references) - 24} more")

    detail_lines = [
        f"Detected {len(field_names):,} field-like identifier(s) from the preview sample.",
    ]
    if markers:
        detail_lines.append(f"Detected structured marker(s): {', '.join(markers)}.")
    if asset_references:
        detail_lines.append(f"Detected {len(asset_references):,} related asset reference(s).")
    if normalized_extension == ".paa":
        detail_lines.append("This inspector summarizes animation-side metadata and readable markers. Real animation playback is not implemented yet.")
    elif normalized_extension in {".pae", ".paem"}:
        detail_lines.append("This inspector summarizes effect/emitter-side metadata and readable markers. Real particle or timeline playback is not implemented yet.")

    return _StructuredBinaryPreviewBundle(
        preview_text="\n".join(lines),
        detail_lines=tuple(detail_lines),
        related_references=related_references,
        metadata_label=metadata_label,
    )


def describe_archive_binary_content(extension: str, data: bytes) -> str:
    head4 = data[:4]
    if head4 == b"BKHD":
        return "Detected Wwise soundbank data."
    if head4 == b"PAR ":
        if extension == ".pac":
            return "Detected PAR skinned mesh data."
        if extension == ".pab":
            return "Detected PAR skeleton data."
        if extension == ".pat":
            return "Detected PAR model data. Visual model preview is not available yet."
        if extension == ".pam":
            return "Detected PAR mesh data."
        if extension == ".pamlod":
            return "Detected PAR mesh LOD data."
        if extension == ".paa":
            return "Detected PAR animation data. Visual animation preview is not available yet."
        if extension in {".pae", ".paem"}:
            return "Detected PAR effect or emitter data. Real effect playback is not available yet."
        return "Detected PAR-family binary data."
    if head4 == b"PARC":
        return "Detected PARC structured container data."
    if len(data) >= 16 and data[4:8] == b"TAG0" and data[12:16] == b"SDKV":
        return "Detected Havok tagfile data. Visual animation or skeleton preview is not available yet."
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "Detected RIFF/WAVE audio data, likely Wwise `.wem`."
    if b"EmitterData" in data[:4096]:
        return "Structured emitter or effect data detected."
    if b"SceneObject" in data[:4096]:
        return "Structured scene or prefab metadata detected."
    if b"AnimationMetaData" in data[:4096]:
        return "Animation metadata detected."
    if b"ParameterizedMotionSpace" in data[:4096]:
        return "Animation motion-blending metadata detected."
    if b"Sequencer" in data[:4096]:
        return "Structured sequencer data detected."
    if extension == ".pabgb":
        return "Structured gameplay or table-like binary data detected."
    if extension == ".meshinfo":
        return "Structured mesh metadata detected."
    if extension in {".pae", ".paem"}:
        return "Structured emitter or effect data detected."
    if extension == ".levelinfo":
        return "Structured level metadata detected."
    if extension == ".prefab":
        return "Structured prefab metadata detected."
    return ""


def build_archive_binary_preview_payload(
    entry: ArchiveEntry,
    data: bytes,
    *,
    info_extra: str = "",
) -> Tuple[str, str, str]:
    text_preview = try_decode_text_like_archive_data(data)
    if text_preview:
        extra_parts = [part for part in [info_extra, "Binary content was sniffed as plain text."] if part]
        if len(data) > ARCHIVE_TEXT_PREVIEW_LIMIT:
            extra_parts.append(f"Preview truncated to {format_byte_size(ARCHIVE_TEXT_PREVIEW_LIMIT)}.")
        return "text", text_preview, "\n\n".join(extra_parts)

    strings_preview = build_binary_strings_preview(data)
    hint_text = describe_archive_binary_content(entry.extension, data)
    extra_parts = [part for part in [info_extra, hint_text] if part]
    if strings_preview:
        extra_parts.append(strings_preview)
        return "text", strings_preview, "\n\n".join(extra_parts)
    return "info", "", "\n\n".join(extra_parts)


def parse_archive_note_flags(note: str) -> set[str]:
    return {part.strip() for part in note.split(",") if part.strip()}


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


def _build_model_preview_summary_text(path: str, model_preview: ModelPreviewData) -> str:
    if getattr(model_preview, "format", "").lower() == "pamlod":
        lod_index = getattr(model_preview, "lod_index", -1)
        lod_count = getattr(model_preview, "lod_count", 0)
        lod_label = f"LOD {lod_index + 1}" if lod_index >= 0 else "LOD"
        if lod_count > 0 and lod_index >= 0:
            lod_label = f"{lod_label} of {lod_count}"
        return (
            f"{path}\n"
            f"{lod_label}\n"
            f"{model_preview.vertex_count:,} vertices\n"
            f"{model_preview.face_count:,} faces"
        )
    return (
        f"{path}\n"
        f"{model_preview.mesh_count:,} submesh(es)\n"
        f"{model_preview.vertex_count:,} vertices\n"
        f"{model_preview.face_count:,} faces"
    )


def _retarget_model_preview(model_preview: ModelPreviewData, path: str) -> None:
    model_preview.path = path
    model_preview.summary = _build_model_preview_summary_text(path, model_preview)


def _inspect_pam_declared_geometry(data: bytes) -> Tuple[int, int]:
    if len(data) < 64 or data[:4] != b"PAR ":
        return 0, 0
    mesh_count = struct.unpack_from("<I", data, 16)[0]
    declared_index_count = 0
    for index in range(mesh_count):
        entry_offset = 1040 + index * 536
        if entry_offset + 8 > len(data):
            break
        declared_index_count += struct.unpack_from("<I", data, entry_offset + 4)[0]
    return mesh_count, declared_index_count


def _pam_preview_looks_incomplete(data: bytes, model_preview: ModelPreviewData) -> bool:
    declared_mesh_count, declared_index_count = _inspect_pam_declared_geometry(data)
    if declared_mesh_count > 0 and model_preview.mesh_count < declared_mesh_count:
        return True
    if declared_index_count > 0 and (model_preview.face_count * 3) < int(declared_index_count * 0.85):
        return True
    return False


def _build_pam_model_preview_with_fallback(
    entry: ArchiveEntry,
    data: bytes,
    note_flags: set[str],
    *,
    companion_entry: Optional[ArchiveEntry] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[ModelPreviewData, List[str]]:
    info_extra_parts: List[str] = []
    recovery_errors: List[str] = []
    raw_model_preview: Optional[ModelPreviewData] = None
    skip_padded_recovery = False

    try:
        candidate_raw_model_preview = build_pam_model_preview(entry, data, stop_event=stop_event)
        ensure_model_preview_is_reasonable(candidate_raw_model_preview, stop_event=stop_event)
        raw_model_preview = candidate_raw_model_preview
        if (
            "PartialRaw" in note_flags
            and companion_entry is not None
            and _pam_preview_looks_incomplete(data, raw_model_preview)
        ):
            info_extra_parts.append(
                "Stored PAM geometry recovery looks incomplete for this Partial entry; a companion PAMLOD preview will be preferred when available."
            )
        else:
            return raw_model_preview, info_extra_parts
    except RunCancelled:
        raise
    except Exception as exc:
        raw_error_text = str(exc)
        recovery_errors.append(f"Stored PAM geometry recovery failed: {raw_error_text}")
        if "suppressed" in raw_error_text.lower() or "scrambled" in raw_error_text.lower():
            skip_padded_recovery = True

    if companion_entry is not None:
        try:
            companion_data, _companion_decompressed, companion_note = read_archive_entry_data(
                companion_entry,
                stop_event=stop_event,
            )
            model_preview = build_pamlod_model_preview(companion_entry, companion_data, stop_event=stop_event)
            ensure_model_preview_is_reasonable(model_preview, stop_event=stop_event)
            _retarget_model_preview(model_preview, entry.path)
            info_extra_parts.append(
                f"Visual model preview uses companion {companion_entry.basename} geometry because the selected PAM payload did not yield a complete renderable mesh preview."
            )
            companion_note_flags = parse_archive_note_flags(companion_note)
            if "ChaCha20" in companion_note_flags:
                info_extra_parts.append("Companion PAMLOD geometry was decrypted via deterministic ChaCha20 filename derivation.")
            return model_preview, info_extra_parts
        except RunCancelled:
            raise
        except Exception as exc:
            recovery_errors.append(f"Companion PAMLOD recovery failed: {exc}")

    if "PartialRaw" in note_flags and len(data) < entry.orig_size and not skip_padded_recovery:
        try:
            padded_data = data + (b"\x00" * (entry.orig_size - len(data)))
            model_preview = build_pam_model_preview(entry, padded_data, stop_event=stop_event)
            ensure_model_preview_is_reasonable(model_preview, stop_event=stop_event)
            info_extra_parts.append(
                "Visual model preview uses zero-padded Partial reconstruction because the stored PAM payload is incomplete."
            )
            return model_preview, info_extra_parts
        except RunCancelled:
            raise
        except Exception as exc:
            recovery_errors.append(f"Zero-padded Partial reconstruction failed: {exc}")

    if raw_model_preview is not None:
        info_extra_parts.append(
            "Stored PAM geometry preview is being shown even though the recovered mesh set appears incomplete."
        )
        return raw_model_preview, info_extra_parts

    if "PartialRaw" in note_flags and len(data) < entry.orig_size:
        recovery_errors.append("Stored Partial payload appears truncated beyond the geometry data needed for preview.")
    raise ValueError("; ".join(recovery_errors) if recovery_errors else "PAM geometry could not be recovered.")


def _build_pamlod_model_preview_with_fallback(
    entry: ArchiveEntry,
    data: bytes,
    note_flags: set[str],
    *,
    companion_entry: Optional[ArchiveEntry] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[ModelPreviewData, List[str]]:
    info_extra_parts: List[str] = []
    recovery_errors: List[str] = []

    try:
        model_preview = build_pamlod_model_preview(entry, data, stop_event=stop_event)
        ensure_model_preview_is_reasonable(model_preview, stop_event=stop_event)
        return model_preview, info_extra_parts
    except RunCancelled:
        raise
    except Exception as exc:
        recovery_errors.append(f"Stored PAMLOD geometry recovery failed: {exc}")

    if companion_entry is not None:
        try:
            companion_data, _companion_decompressed, companion_note = read_archive_entry_data(
                companion_entry,
                stop_event=stop_event,
            )
            model_preview = build_pam_model_preview(companion_entry, companion_data, stop_event=stop_event)
            ensure_model_preview_is_reasonable(model_preview, stop_event=stop_event)
            _retarget_model_preview(model_preview, entry.path)
            info_extra_parts.append(
                f"Visual model preview uses companion {companion_entry.basename} geometry because the selected PAMLOD payload did not yield a complete renderable LOD preview."
            )
            companion_note_flags = parse_archive_note_flags(companion_note)
            if "ChaCha20" in companion_note_flags:
                info_extra_parts.append("Companion PAM geometry was decrypted via deterministic ChaCha20 filename derivation.")
            return model_preview, info_extra_parts
        except RunCancelled:
            raise
        except Exception as exc:
            recovery_errors.append(f"Companion PAM recovery failed: {exc}")

    if "PartialRaw" in note_flags and len(data) < entry.orig_size:
        try:
            padded_data = data + (b"\x00" * (entry.orig_size - len(data)))
            model_preview = build_pamlod_model_preview(entry, padded_data, stop_event=stop_event)
            ensure_model_preview_is_reasonable(model_preview, stop_event=stop_event)
            info_extra_parts.append(
                "Visual model preview uses zero-padded Partial reconstruction because the stored PAMLOD payload is incomplete."
            )
            return model_preview, info_extra_parts
        except RunCancelled:
            raise
        except Exception as exc:
            recovery_errors.append(f"Zero-padded PAMLOD reconstruction failed: {exc}")
        recovery_errors.append("Stored Partial payload appears truncated beyond the geometry data needed for preview.")

    raise ValueError("; ".join(recovery_errors) if recovery_errors else "PAMLOD geometry could not be recovered.")


def _build_pac_model_preview_with_fallback(
    entry: ArchiveEntry,
    data: bytes,
    note_flags: set[str],
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[ModelPreviewData, ParsedMesh, List[str]]:
    info_extra_parts: List[str] = []
    recovery_errors: List[str] = []

    try:
        model_preview, parsed_mesh = build_mesh_preview_from_bytes(data, entry.path)
        return model_preview, parsed_mesh, info_extra_parts
    except RunCancelled:
        raise
    except Exception as exc:
        recovery_errors.append(f"Stored PAC geometry recovery failed: {exc}")

    if "PartialRaw" in note_flags and len(data) < entry.orig_size:
        try:
            padded_data = data + (b"\x00" * (entry.orig_size - len(data)))
            model_preview, parsed_mesh = build_mesh_preview_from_bytes(padded_data, entry.path)
            info_extra_parts.append(
                "Visual model preview uses zero-padded Partial reconstruction because the stored PAC payload is incomplete."
            )
            return model_preview, parsed_mesh, info_extra_parts
        except RunCancelled:
            raise
        except Exception as exc:
            recovery_errors.append(f"Zero-padded PAC reconstruction failed: {exc}")
        recovery_errors.append("Stored Partial payload appears truncated beyond the geometry data needed for preview.")

    raise ValueError("; ".join(recovery_errors) if recovery_errors else "PAC geometry could not be recovered.")


def build_archive_preview_result(
    texconv_path: Optional[Path],
    entry: Optional[ArchiveEntry],
    loose_search_roots: Optional[Sequence[Path]] = None,
    *,
    companion_entry: Optional[ArchiveEntry] = None,
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    include_loose_preview_assets: bool = True,
    semantic_sidecar_texts: Sequence[str] = (),
    stop_event: Optional[threading.Event] = None,
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
    loose_preview_media_path = ""
    loose_preview_media_kind = ""
    loose_preview_title = ""
    loose_preview_metadata_summary = ""
    loose_preview_detail_text = ""

    if loose_search_roots:
        loose_candidates = list(iter_archive_loose_file_candidates(entry, loose_search_roots))
        if loose_candidates:
            loose_candidate = loose_candidates[0]
            loose_file_path = str(loose_candidate)
            loose_preview_title = f"{entry.basename} (Loose file)"
            if include_loose_preview_assets:
                try:
                    if loose_candidate.suffix.lower() in ARCHIVE_AUDIO_EXTENSIONS.union(ARCHIVE_VIDEO_EXTENSIONS):
                        (
                            loose_preview_media_path,
                            loose_preview_media_kind,
                            loose_preview_metadata_summary,
                            loose_preview_detail_text,
                        ) = build_loose_archive_media_preview_assets(
                            loose_candidate,
                            stop_event=stop_event,
                        )
                    else:
                        (
                            loose_preview_image_path,
                            loose_preview_metadata_summary,
                            loose_preview_detail_text,
                        ) = build_loose_archive_preview_assets(
                            texconv_path,
                            loose_candidate,
                            stop_event=stop_event,
                        )
                except RunCancelled:
                    raise
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
        if extension in ARCHIVE_VIDEO_EXTENSIONS:
            source_path, note = ensure_archive_preview_source(entry, stop_event=stop_event)
            metadata_summary, media_detail = _build_mp4_media_preview_detail_text(source_path, loose=False)
            extra_detail_parts: List[str] = []
            if "ChaCha20" in parse_archive_note_flags(note):
                extra_detail_parts.append("Archive payload decrypted via deterministic ChaCha20 filename derivation.")
            extra_detail_parts.append(media_detail)
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(entry, "\n\n".join(part for part in extra_detail_parts if part)),
                preview_media_path=str(source_path.resolve()),
                preview_media_kind="video",
                preferred_view="media",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in ARCHIVE_AUDIO_EXTENSIONS:
            source_path, note = ensure_archive_preview_source(entry, stop_event=stop_event)
            media_source, playback_note = _ensure_media_preview_source_path(
                source_path,
                extension,
                stop_event=stop_event,
            )
            try:
                with source_path.open("rb") as handle:
                    audio_sample = handle.read(131072)
            except OSError:
                audio_sample = b""
            metadata_summary, media_detail = _build_wem_media_preview_detail_text(
                source_path,
                audio_sample,
                loose=False,
                playback_source_path=media_source,
                playback_note=playback_note,
            )
            extra_detail_parts: List[str] = []
            if "ChaCha20" in parse_archive_note_flags(note):
                extra_detail_parts.append("Archive payload decrypted via deterministic ChaCha20 filename derivation.")
            extra_detail_parts.append(media_detail)
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(entry, "\n\n".join(part for part in extra_detail_parts if part)),
                preview_media_path=str(media_source),
                preview_media_kind="audio",
                preferred_view="media",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension == ".dds":
            source_path, note = ensure_archive_preview_source(entry, stop_event=stop_event)
            note_flags = parse_archive_note_flags(note)
            warning_badge = ""
            warning_text = ""
            extra_detail_parts: List[str] = []
            dds_info: Optional[DdsInfo] = None
            try:
                dds_info = parse_dds(source_path)
                metadata_summary = (
                    f"{metadata_summary} | {dds_info.texconv_format} | "
                    f"{dds_info.width}x{dds_info.height} | Mips {dds_info.mip_count}"
                )
                extra_detail_parts.append(
                    build_dds_header_detail_text(
                        source_path,
                        dds_info,
                        logical_path=entry.path,
                        sidecar_texts=semantic_sidecar_texts,
                    )
                )
            except Exception as exc:
                extra_detail_parts.append(f"DDS metadata unavailable: {exc}")
            if "PartialDDS" in note_flags:
                extra_detail_parts.append(
                    "Type 1 DDS reconstructed successfully using meta/0.pathc partial-header metadata."
                )
            elif "SparseDDS" in note_flags:
                warning_badge = "Type 1 DDS: Unsupported Preview"
                warning_text = (
                    "This archive DDS is stored as truncated type 1 data. "
                    "The image shown here is a padded best-effort preview and may be corrupted, noisy, or incomplete."
                )
                extra_detail_parts.append(warning_text)
                if loose_file_path:
                    extra_detail_parts.append(f"Loose file candidate found: {loose_file_path}")
            if "ChaCha20" in note_flags:
                extra_detail_parts.append("Archive payload decrypted via deterministic ChaCha20 filename derivation.")
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
                    loose_preview_media_path=loose_preview_media_path,
                    loose_preview_media_kind=loose_preview_media_kind,
                    loose_preview_title=loose_preview_title,
                    loose_preview_metadata_summary=loose_preview_metadata_summary,
                    loose_preview_detail_text=loose_preview_detail_text,
                )
            preview_png = ensure_dds_display_preview_png(
                texconv_path.resolve(),
                source_path.resolve(),
                dds_info=dds_info,
                stop_event=stop_event,
            )
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
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in ARCHIVE_IMAGE_EXTENSIONS:
            source_path, note = ensure_archive_preview_source(entry, stop_event=stop_event)
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(
                    entry,
                    "Preview fallback: sparse DDS padding was applied."
                    if "SparseDDS" in parse_archive_note_flags(note)
                    else "",
                ),
                preview_image_path=str(source_path),
                preferred_view="image",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        data, _decompressed, note = read_archive_entry_data(entry, stop_event=stop_event)
        note_flags = parse_archive_note_flags(note)

        if extension == ".bnk":
            bnk_preview_text, bnk_detail_text = build_bnk_soundbank_preview(data)
            detail_extra = "\n\n".join(
                part
                for part in [
                    (
                        "Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes."
                        if "PartialRaw" in note_flags
                        else ""
                    ),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    bnk_detail_text,
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | Wwise SoundBank",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=bnk_preview_text or build_binary_strings_preview(data),
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension == ".pab":
            skeleton_preview = build_pab_preview(data, entry.path)
            related_references = build_archive_related_file_references(
                entry,
                explicit_reference_names=_extract_binary_asset_references(data, sample_limit=262_144, max_references=48),
                companion_entries=(
                    _find_archive_model_related_entries(entry, texture_entries_by_basename)
                    if texture_entries_by_basename is not None
                    else ()
                ),
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(skeleton_preview.detail_lines),
                    ("Companion and related files are listed below." if related_references else ""),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | Skeleton",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=skeleton_preview.preview_text,
                model_texture_references=related_references,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension == ".meshinfo":
            meshinfo_preview = build_meshinfo_preview(
                data,
                entry.path,
                source_entry=entry,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(meshinfo_preview.detail_lines),
                    ("Companion and related files are listed below." if meshinfo_preview.related_references else ""),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | {meshinfo_preview.metadata_label or 'Mesh Metadata'}",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=meshinfo_preview.preview_text,
                model_texture_references=meshinfo_preview.related_references,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in {".paa", ".pae", ".paem"}:
            structured_preview = build_par_structured_preview(
                data,
                entry.path,
                extension=extension,
                source_entry=entry,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(structured_preview.detail_lines),
                    ("Companion and related files are listed below." if structured_preview.related_references else ""),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | {structured_preview.metadata_label or 'Structured Binary'}",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=structured_preview.preview_text,
                model_texture_references=structured_preview.related_references,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension == ".hkx":
            hkx_preview = build_hkx_preview(data, entry.path)
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(hkx_preview.detail_lines),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | Havok",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=hkx_preview.preview_text,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in ARCHIVE_TEXT_EXTENSIONS:
            preview_bytes = data[:ARCHIVE_TEXT_PREVIEW_LIMIT]
            text = try_decode_text_like_archive_data(data) or preview_bytes.decode("utf-8", errors="replace")
            extra_note = ""
            if len(data) > ARCHIVE_TEXT_PREVIEW_LIMIT:
                extra_note = f"\n\nPreview truncated to {format_byte_size(ARCHIVE_TEXT_PREVIEW_LIMIT)}."
            if "PartialRaw" in note_flags:
                extra_note = "\n\n".join(
                    part
                    for part in [
                        "Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes.",
                        extra_note.strip(),
                    ]
                    if part
                )
            if "ChaCha20" in note_flags:
                extra_note = "\n\n".join(
                    part for part in ["Decrypted via deterministic ChaCha20 filename derivation.", extra_note.strip()] if part
                )
            if extension == ".obj":
                summary_text = summarize_obj_text(text)
                extra_note = "\n\n".join(part for part in [summary_text, extra_note.strip()] if part)
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=metadata_summary,
                detail_text=build_archive_entry_detail_text(
                    entry,
                    "\n\n".join(
                        part
                        for part in [
                            ("Preview fallback: sparse DDS padding was applied." if "SparseDDS" in note_flags else ""),
                            extra_note.strip(),
                        ]
                        if part
                    ),
                ),
                preview_text=text,
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        info_extra_parts: List[str] = []
        if "SparseDDS" in note_flags:
            info_extra_parts.append("Preview fallback: sparse DDS padding was applied.")
        if "PartialPAR" in note_flags:
            info_extra_parts.append(
                "Archive entry uses Partial PAR storage; preview uses reconstructed decompressed sections."
            )
        if "PartialRaw" in note_flags:
            info_extra_parts.append(
                "Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes."
            )
        if "ChaCha20" in note_flags:
            info_extra_parts.append("Decrypted via deterministic ChaCha20 filename derivation.")
        model_preview = None
        model_texture_references: Tuple[ArchiveModelTextureReference, ...] = ()
        model_preview_error = ""
        parsed_mesh_for_references = None
        binary_texture_references: Tuple[str, ...] = ()
        sidecar_texture_references: Tuple[_ArchiveModelSidecarTextureBinding, ...] = ()
        sidecar_reference_paths: Tuple[str, ...] = ()
        sidecar_texts_by_normalized_path: Dict[str, Tuple[str, ...]] = {}
        sidecar_texts_by_basename: Dict[str, Tuple[str, ...]] = {}
        if extension in ARCHIVE_MODEL_EXTENSIONS:
            binary_texture_references = tuple(extract_binary_dds_references(data))
            (
                sidecar_texture_references,
                sidecar_reference_paths,
                sidecar_texts_by_normalized_path,
                sidecar_texts_by_basename,
            ) = _extract_archive_model_sidecar_texture_references(
                entry,
                archive_entries_by_basename=texture_entries_by_basename,
                stop_event=stop_event,
            )
            if sidecar_texture_references:
                sidecar_count = len(sidecar_texture_references)
                sidecar_suffix = f" from {', '.join(sidecar_reference_paths[:2])}" if sidecar_reference_paths else ""
                if len(sidecar_reference_paths) > 2:
                    sidecar_suffix += " ..."
                info_extra_parts.append(
                    f"Companion material sidecar data contributed {sidecar_count:,} texture binding(s){sidecar_suffix}."
                )
                if extension in {".pam", ".pamlod", ".pac"}:
                    info_extra_parts.append(
                        "Companion sidecar data only describes material and texture bindings. Geometry preview still depends on recovering a renderable mesh layout from the selected payload or its mesh companion."
                    )
        if extension == ".pam":
            try:
                model_preview, model_info = _build_pam_model_preview_with_fallback(
                    entry,
                    data,
                    note_flags,
                    companion_entry=companion_entry,
                    stop_event=stop_event,
                )
                if getattr(model_preview, "format", "").lower() == "pamlod":
                    lod_label = (
                        f"LOD {model_preview.lod_index + 1} of {model_preview.lod_count}"
                        if getattr(model_preview, "lod_count", 0) > 0 and getattr(model_preview, "lod_index", -1) >= 0
                        else "highest-detail LOD"
                    )
                    metadata_summary = f"{metadata_summary} | {lod_label} | {model_preview.face_count:,} faces"
                else:
                    metadata_summary = (
                        f"{metadata_summary} | {model_preview.mesh_count:,} submesh(es)"
                        f" | {model_preview.face_count:,} faces"
                    )
                info_extra_parts.extend(model_info)
                if getattr(model_preview, "format", "").lower() == "pamlod":
                    info_extra_parts.append(
                        "Geometry preview uses the highest-detail recovered companion PAMLOD LOD only; lower-detail LODs are not stacked in the preview. "
                        "Texture and material references remain listed below."
                    )
                else:
                    info_extra_parts.append(
                        "Geometry preview uses recovered PAM submeshes with temporary material colors. "
                        "Texture and material references remain listed below."
                    )
            except RunCancelled:
                raise
            except Exception as exc:
                model_preview_error = str(exc)
                info_extra_parts.append(f"Visual model preview failed to recover geometry: {exc}")
        elif extension == ".pamlod":
            try:
                model_preview, model_info = _build_pamlod_model_preview_with_fallback(
                    entry,
                    data,
                    note_flags,
                    companion_entry=companion_entry,
                    stop_event=stop_event,
                )
                if getattr(model_preview, "format", "").lower() == "pam":
                    metadata_summary = (
                        f"{metadata_summary} | {model_preview.mesh_count:,} submesh(es)"
                        f" | {model_preview.face_count:,} faces"
                    )
                else:
                    lod_label = (
                        f"LOD {model_preview.lod_index + 1} of {model_preview.lod_count}"
                        if getattr(model_preview, "lod_count", 0) > 0 and getattr(model_preview, "lod_index", -1) >= 0
                        else "highest-detail LOD"
                    )
                    metadata_summary = f"{metadata_summary} | {lod_label} | {model_preview.face_count:,} faces"
                info_extra_parts.extend(model_info)
                if getattr(model_preview, "format", "").lower() == "pam":
                    info_extra_parts.append(
                        "Geometry preview uses recovered companion PAM submeshes with temporary material colors. "
                        "Texture and material references remain listed below."
                    )
                else:
                    info_extra_parts.append(
                        "Geometry preview uses the highest-detail recovered PAMLOD LOD only; lower-detail LODs are not stacked in the preview. "
                        "Texture and material references remain listed below."
                    )
            except RunCancelled:
                raise
            except Exception as exc:
                model_preview_error = str(exc)
                info_extra_parts.append(f"Visual model preview failed to recover geometry: {exc}")
        elif extension == ".pac":
            try:
                model_preview, parsed_mesh, model_info = _build_pac_model_preview_with_fallback(
                    entry,
                    data,
                    note_flags,
                    stop_event=stop_event,
                )
                parsed_mesh_for_references = parsed_mesh
                metadata_summary = (
                    f"{metadata_summary} | {model_preview.mesh_count:,} submesh(es)"
                    f" | {model_preview.face_count:,} faces"
                )
                info_extra_parts.extend(model_info)
                info_extra_parts.append(
                    "Geometry preview uses recovered PAC skinned mesh data. Texture and material references remain listed below."
                )
                if getattr(parsed_mesh, "has_bones", False):
                    unique_bones = {
                        int(bone_index)
                        for submesh in getattr(parsed_mesh, "submeshes", [])
                        for palette in getattr(submesh, "bone_indices", [])
                        for bone_index in palette
                        if int(bone_index) >= 0
                    }
                    if unique_bones:
                        info_extra_parts.append(
                            f"Recovered skinning data referencing {len(unique_bones):,} bone slot(s)."
                        )
                unique_material_names = {
                    str(getattr(submesh, "material", "") or "").strip()
                    for submesh in getattr(parsed_mesh, "submeshes", ())
                    if str(getattr(submesh, "material", "") or "").strip()
                }
                unique_texture_names = {
                    str(getattr(submesh, "texture", "") or "").strip()
                    for submesh in getattr(parsed_mesh, "submeshes", ())
                    if str(getattr(submesh, "texture", "") or "").strip()
                }
                if getattr(parsed_mesh, "has_uvs", False):
                    info_extra_parts.append("Recovered UV coordinates for textured preview and export.")
                if unique_material_names:
                    info_extra_parts.append(f"Recovered {len(unique_material_names):,} material slot name(s) from the PAC payload.")
                if unique_texture_names:
                    info_extra_parts.append(f"Recovered {len(unique_texture_names):,} embedded texture reference name(s) from the PAC payload.")
                if texture_entries_by_basename is not None:
                    companion_pab_entries = [
                        related_entry
                        for related_entry in _find_archive_model_related_entries(entry, texture_entries_by_basename)
                        if related_entry.extension == ".pab"
                    ]
                    if companion_pab_entries:
                        info_extra_parts.append(f"Matching skeleton companion detected: {companion_pab_entries[0].path}")
            except Exception as exc:
                model_preview_error = str(exc)
                info_extra_parts.append(f"Visual model preview failed to recover geometry: {exc}")
        elif extension in ARCHIVE_MODEL_EXTENSIONS:
            info_extra_parts.append("Visual preview is not available for this model format yet.")
        if (
            model_preview is not None
            and sidecar_texture_references
            and parsed_mesh_for_references is None
            and extension in ARCHIVE_MODEL_EXTENSIONS
        ):
            try:
                from crimson_forge_toolkit.modding.mesh_parser import parse_mesh

                parsed_mesh_for_references = parse_mesh(data, entry.path)
            except RunCancelled:
                raise
            except Exception:
                parsed_mesh_for_references = None
        if model_preview is not None:
            if texconv_path is None:
                if any(
                    str(getattr(mesh, "texture_name", "") or "").strip().lower().endswith(".dds")
                    for mesh in model_preview.meshes
                ):
                    info_extra_parts.append(
                        "Set texconv.exe in the Workflow tab to enable textured model shading and PNG-backed model export."
                    )
            else:
                if sidecar_texture_references:
                    info_extra_parts.extend(
                        _attach_model_sidecar_texture_preview_paths(
                            texconv_path,
                            entry,
                            model_preview,
                            parsed_mesh=parsed_mesh_for_references,
                            sidecar_texture_bindings=sidecar_texture_references,
                            texture_entries_by_normalized_path=texture_entries_by_normalized_path,
                            texture_entries_by_basename=texture_entries_by_basename,
                            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                            sidecar_texts_by_basename=sidecar_texts_by_basename,
                            stop_event=stop_event,
                        )
                    )
                info_extra_parts.extend(
                    _attach_model_texture_preview_paths(
                        texconv_path,
                        entry,
                        model_preview,
                        texture_entries_by_normalized_path=texture_entries_by_normalized_path,
                        texture_entries_by_basename=texture_entries_by_basename,
                        sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                        sidecar_texts_by_basename=sidecar_texts_by_basename,
                        stop_event=stop_event,
                    )
                )
        if extension in ARCHIVE_MODEL_EXTENSIONS and parsed_mesh_for_references is None:
            try:
                from crimson_forge_toolkit.modding.mesh_parser import parse_mesh

                parsed_mesh_for_references = parse_mesh(data, entry.path)
            except RunCancelled:
                raise
            except Exception:
                parsed_mesh_for_references = None
        if model_preview is not None or parsed_mesh_for_references is not None or binary_texture_references or sidecar_texture_references:
            model_texture_references = tuple(
                build_archive_model_texture_references(
                    entry,
                    model_preview,
                    parsed_mesh=parsed_mesh_for_references,
                    binary_texture_references=binary_texture_references,
                    sidecar_texture_references=sidecar_texture_references,
                    texture_entries_by_normalized_path=texture_entries_by_normalized_path,
                    texture_entries_by_basename=texture_entries_by_basename,
                    sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                    sidecar_texts_by_basename=sidecar_texts_by_basename,
                )
            )
        preferred_view, preview_text, info_extra = build_archive_binary_preview_payload(
            entry,
            data,
            info_extra="\n".join(info_extra_parts),
        )
        header_preview = format_binary_header_preview(data[:ARCHIVE_BINARY_HEX_PREVIEW_LIMIT])
        detail_text = build_archive_entry_detail_text(
            entry,
            "\n\n".join(part for part in [info_extra, f"Binary header preview:\n{header_preview}"] if part).strip(),
        )
        return ArchivePreviewResult(
            status="ok",
            title=entry.basename,
            metadata_summary=metadata_summary,
            detail_text=detail_text,
            preview_text=preview_text,
            preview_model=model_preview,
            model_texture_references=model_texture_references,
            preferred_view="model" if model_preview is not None else preferred_view,
            warning_badge="Model preview fallback" if model_preview is None and model_preview_error else "",
            warning_text=model_preview_error if model_preview is None and model_preview_error else "",
            loose_file_path=loose_file_path,
            loose_preview_image_path=loose_preview_image_path,
            loose_preview_media_path=loose_preview_media_path,
            loose_preview_media_kind=loose_preview_media_kind,
            loose_preview_title=loose_preview_title,
            loose_preview_metadata_summary=loose_preview_metadata_summary,
            loose_preview_detail_text=loose_preview_detail_text,
        )
    except RunCancelled:
        raise
    except Exception as exc:
        try:
            raw_data = read_archive_entry_raw_data(entry)
        except Exception:
            raw_data = b""
        preferred_view = "info"
        preview_text = ""
        raw_extra_parts = [
            f"Decoded preview failed: {exc}",
            "Showing raw stored bytes instead.",
        ]
        if raw_data:
            raw_preferred_view, raw_preview_text, raw_extra = build_archive_binary_preview_payload(
                entry,
                raw_data,
            )
            preferred_view = raw_preferred_view
            preview_text = raw_preview_text
            if raw_extra:
                raw_extra_parts.append(raw_extra)
        raw_header_preview = format_binary_header_preview(raw_data[:ARCHIVE_BINARY_HEX_PREVIEW_LIMIT])
        return ArchivePreviewResult(
            status="ok",
            title=entry.basename,
            metadata_summary=metadata_summary,
            detail_text=build_archive_entry_detail_text(
                entry,
                "\n\n".join(part for part in [*raw_extra_parts, f"Binary header preview:\n{raw_header_preview}"] if part),
            ),
            preview_text=preview_text,
            preferred_view=preferred_view,
            warning_badge="Raw bytes",
            warning_text="Showing raw stored bytes because the decoded preview path failed.",
            loose_file_path=loose_file_path,
            loose_preview_image_path=loose_preview_image_path,
            loose_preview_media_path=loose_preview_media_path,
            loose_preview_media_kind=loose_preview_media_kind,
            loose_preview_title=loose_preview_title,
            loose_preview_metadata_summary=loose_preview_metadata_summary,
            loose_preview_detail_text=loose_preview_detail_text,
        )

