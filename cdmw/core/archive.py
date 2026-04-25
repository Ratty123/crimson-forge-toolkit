from __future__ import annotations

import fnmatch
import hashlib
import html
import json
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterator, Mapping
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple

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

from cdmw.constants import *
from cdmw.models import *
from cdmw.core.common import *
from cdmw.core.model_preview import (
    build_pam_model_preview,
    build_pamlod_model_preview,
    ensure_model_preview_is_reasonable,
)
from cdmw.core.archive_modding import (
    build_hkx_preview,
    build_mesh_preview_from_bytes,
    build_pab_preview,
)
from cdmw.core.pipeline import ensure_dds_display_preview_png, parse_dds
from cdmw.core.upscale_profiles import (
    classify_texture_type,
    derive_texture_group_key,
    infer_texture_semantics,
    normalize_texture_reference_for_sidecar_lookup,
    parse_texture_sidecar_bindings,
)
from cdmw.modding.skeleton_parser import iter_pab_candidate_basenames

if TYPE_CHECKING:
    from cdmw.modding.mesh_parser import ParsedMesh

_PATHC_COLLECTION_CACHE: Dict[str, Tuple[str, "PathcCollection"]] = {}
_ARCHIVE_SCAN_CACHE_MAGIC = b"CTFARCH1"
_ARCHIVE_SCAN_CACHE_VERSION = 2
_ARCHIVE_SCAN_CACHE_LEGACY_DIRNAMES: Tuple[str, ...] = ("cache", "archive_scan_cache")
_ARCHIVE_SIDECAR_CACHE_MAGIC = b"CTFSIDE1"
_ARCHIVE_SIDECAR_CACHE_VERSION = 8
# Keep model textures closer to their source resolution in the 3D preview.
# This only affects the PNG preview cache used for model shading; low-quality
# mode still downsamples at upload time inside the OpenGL widget.
_MODEL_TEXTURE_DISPLAY_PREVIEW_MAX_DIMENSION = clamp_model_preview_render_settings().preview_texture_max_dimension
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


class LazyArchiveEntryRowIndex(Mapping[str, Sequence[ArchiveEntry]]):
    def __init__(
        self,
        rows: Optional[Dict[str, Tuple[int, ...]]],
        entries: Sequence[ArchiveEntry],
    ) -> None:
        self._rows: Dict[str, Tuple[int, ...]] = {
            str(key or "").strip().lower(): tuple(int(index) for index in value)
            for key, value in (rows or {}).items()
            if str(key or "").strip()
        }
        self._entries = list(entries)
        self._resolved: OrderedDict[str, Tuple[ArchiveEntry, ...]] = OrderedDict()
        self._resolved_limit = 4096

    def __getitem__(self, key: str) -> Sequence[ArchiveEntry]:
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in self._rows:
            raise KeyError(key)
        cached = self._resolved.get(normalized_key)
        if cached is not None:
            self._resolved.move_to_end(normalized_key)
            return cached
        resolved_entries: List[ArchiveEntry] = []
        seen_indexes: set[int] = set()
        for raw_index in self._rows.get(normalized_key, ()):
            entry_index = int(raw_index)
            if entry_index < 0 or entry_index >= len(self._entries) or entry_index in seen_indexes:
                continue
            seen_indexes.add(entry_index)
            resolved_entries.append(self._entries[entry_index])
        resolved_tuple = tuple(resolved_entries)
        self._resolved[normalized_key] = resolved_tuple
        self._resolved.move_to_end(normalized_key)
        while len(self._resolved) > self._resolved_limit:
            self._resolved.popitem(last=False)
        return resolved_tuple

    def __iter__(self) -> Iterator[str]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, key: object, default: Optional[Sequence[ArchiveEntry]] = None) -> Optional[Sequence[ArchiveEntry]]:
        normalized_key = str(key or "").strip().lower()
        if normalized_key not in self._rows:
            return default
        return self[normalized_key]

    @property
    def row_count(self) -> int:
        return len(self._rows)
_MODEL_TEXTURE_SUPPORT_FAMILY_SUFFIXES: Dict[str, Tuple[str, ...]] = {
    "normal": (
        "_n",
        "_normal",
        "_normalmap",
    ),
    "material": (
        "_sp",
        "_material",
        "_mask",
        "_ma",
        "_mg",
        "_m",
        "_orm",
        "_mra",
        "_rma",
        "_arm",
        "_ao",
        "_spec",
        "_specular",
    ),
    "height": (
        "_disp",
        "_displacement",
        "_height",
        "_hgt",
        "_dmap",
        "_bump",
        "_parallax",
        "_pom",
        "_ssdm",
    ),
}


def set_model_texture_display_preview_max_dimension(value: int) -> None:
    global _MODEL_TEXTURE_DISPLAY_PREVIEW_MAX_DIMENSION
    settings = clamp_model_preview_render_settings(
        ModelPreviewRenderSettings(preview_texture_max_dimension=int(value))
    )
    _MODEL_TEXTURE_DISPLAY_PREVIEW_MAX_DIMENSION = int(settings.preview_texture_max_dimension)
_ARCHIVE_TEXTURE_FAMILY_SUFFIXES: Tuple[str, ...] = (
    "",
    "_ct",
    "_color",
    "_col",
    "_albedo",
    "_basecolor",
    "_base_color",
    "_diffuse",
    "_n",
    "_normal",
    "_normalmap",
    "_sp",
    "_spec",
    "_specular",
    "_m",
    "_mask",
    "_ma",
    "_mg",
    "_orm",
    "_mra",
    "_rma",
    "_arm",
    "_ao",
    "_o",
    "_height",
    "_hgt",
    "_disp",
    "_displacement",
    "_dmap",
    "_d",
    "_bump",
    "_parallax",
    "_pom",
    "_ssdm",
    "_em",
    "_emi",
    "_emissive",
    "_glow",
    "_material",
    "_mat",
)


@dataclass(slots=True)
class _ArchiveModelSidecarTextureBinding:
    texture_path: str
    parameter_name: str = ""
    submesh_name: str = ""
    sidecar_path: str = ""
    sidecar_kind: str = ""
    linked_mesh_path: str = ""
    part_name: str = ""
    material_name: str = ""
    shader_family: str = ""
    texture_role: str = ""
    visualization_state: str = ""
    resolved_texture_exists: bool = False
    represent_color: Tuple[float, float, float] = ()
    tint_color: Tuple[float, float, float] = ()
    brightness: float = 1.0
    uv_scale: float = 1.0
    tile_type: str = ""


@dataclass(slots=True)
class _StructuredBinaryPreviewBundle:
    preview_text: str
    detail_lines: Tuple[str, ...] = ()
    related_references: Tuple[ArchiveModelTextureReference, ...] = ()
    metadata_label: str = ""


_MODEL_SIDECAR_PARSE_CACHE_LIMIT = 512
_MODEL_SIDECAR_PARSE_CACHE: OrderedDict[
    Tuple[object, ...],
    Tuple[Tuple["_ArchiveModelSidecarTextureBinding", ...], Tuple[str, ...], Dict[str, Tuple[str, ...]], Dict[str, Tuple[str, ...]]],
] = OrderedDict()
_MODEL_SIDECAR_PARSE_CACHE_LOCK = threading.Lock()

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
_STRUCTURED_BINARY_ASSET_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\-]+")
_STRUCTURED_BINARY_ASSET_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_STRUCTURED_BINARY_ASSET_REFERENCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".dds",
        ".xml",
        ".pac_xml",
        ".pam_xml",
        ".pamlod_xml",
        ".prefabdata_xml",
        ".pami",
        ".meshinfo",
        ".hkx",
        ".pam",
        ".pamlod",
        ".pac",
        ".pab",
        ".pabc",
        ".papr",
        ".paa",
        ".pae",
        ".paem",
        ".paseq",
        ".paschedule",
        ".paschedulepath",
        ".pastage",
        ".prefab",
        ".seqmt",
        ".wem",
        ".bnk",
        ".mp4",
        ".bk2",
        ".json",
    }
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
_ARCHIVE_SIDECAR_CACHE_SUPPORTED_VERSIONS = {8}
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

_ARCHIVE_MATERIAL_SIDECAR_EXTENSIONS: frozenset[str] = frozenset({".pami", ".pac_xml", ".pam_xml", ".pamlod_xml"})
_ARCHIVE_METADATA_XML_EXTENSIONS: frozenset[str] = frozenset({".xml", ".prefabdata_xml"})
_ARCHIVE_XML_LIKE_EXTENSIONS: frozenset[str] = _ARCHIVE_MATERIAL_SIDECAR_EXTENSIONS | _ARCHIVE_METADATA_XML_EXTENSIONS
_ARCHIVE_SCAN_IGNORED_TOP_LEVEL_DIRS: frozenset[str] = frozenset({"cdmods", "_jmm_backups"})
_ARCHIVE_SIDECAR_TEXTURE_ATTR_RE = re.compile(
    r"""\b(?:_path|path|Path|Value|_value|value|File|file|_file|Texture|texture)\s*=\s*(['"])(?P<value>[^'"<>]{1,1024}?\.(?:dds|png|jpg|jpeg|tga|bmp|tif|tiff))\1""",
    re.IGNORECASE,
)
_ARCHIVE_TEXTURE_BYTES_RE = re.compile(br"\.(?:dds|png|jpg|jpeg|tga|bmp|tif|tiff)", re.IGNORECASE)


def _is_material_sidecar_extension(extension: str, basename: str = "") -> bool:
    normalized_extension = str(extension or "").strip().lower()
    normalized_basename = str(basename or "").strip().lower()
    if normalized_extension in _ARCHIVE_MATERIAL_SIDECAR_EXTENSIONS:
        return True
    if normalized_extension == ".xml" and normalized_basename.endswith((".pac.xml", ".pam.xml", ".pamlod.xml")):
        return True
    return False
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


def _looks_like_paloc_payload(data: bytes) -> bool:
    if len(data) < 16:
        return False
    pos = 0
    matches = 0
    scan_limit = min(len(data), 4_000_000)
    while pos + 8 < scan_limit and matches < 8:
        try:
            slen = struct.unpack_from("<I", data, pos)[0]
        except struct.error:
            break
        if slen == 0 or slen > 50_000 or pos + 4 + slen > len(data):
            pos += 1
            continue
        key_bytes = data[pos + 4 : pos + 4 + slen]
        if not (6 <= slen <= 20 and all(0x30 <= value <= 0x39 for value in key_bytes)):
            pos += 1
            continue
        text_pos = pos + 4 + slen
        if text_pos + 4 >= len(data):
            pos += 1
            continue
        text_len = struct.unpack_from("<I", data, text_pos)[0]
        if not (0 < text_len < 50_000 and text_pos + 4 + text_len <= len(data)):
            pos += 1
            continue
        text_bytes = data[text_pos + 4 : text_pos + 4 + text_len]
        try:
            text_bytes.decode("utf-8")
        except UnicodeDecodeError:
            pos += 1
            continue
        matches += 1
        pos = text_pos + 4 + text_len
    return matches >= 2


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
    if entry.extension == ".paloc" and _looks_like_paloc_payload(candidate):
        return True
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
        if entry.extension in _ARCHIVE_XML_LIKE_EXTENSIONS and _looks_like_decrypted_payload(entry, data):
            return data, "ChaCha20FlagMismatch"
        raise ValueError(f"ChaCha20 decryption validation failed for {entry.path}")
    return candidate, "ChaCha20"

def discover_pamt_files(package_root: Path) -> List[Path]:
    root = package_root.expanduser().resolve()
    if root.is_file() and root.suffix.lower() == ".pamt":
        return [root]
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Archive package root does not exist or is not a folder: {root}")
    files: List[Path] = []
    for path in root.rglob("*.pamt"):
        if not path.is_file():
            continue
        try:
            top_level_dir = path.relative_to(root).parts[0].lower()
        except (IndexError, ValueError):
            top_level_dir = ""
        if top_level_dir in _ARCHIVE_SCAN_IGNORED_TOP_LEVEL_DIRS:
            continue
        files.append(path)
    files.sort()
    return files


def resolve_archive_scan_cache_path(package_root: Path, cache_root: Path) -> Path:
    try:
        resolved_root = package_root.expanduser().resolve()
    except OSError:
        resolved_root = package_root.expanduser()
    digest = hashlib.sha256(str(resolved_root).lower().encode("utf-8", errors="replace")).hexdigest()[:24]
    return cache_root / f"archive_scan_{digest}.bin"


def resolve_archive_sidecar_cache_path(package_root: Path, cache_root: Path) -> Path:
    try:
        resolved_root = package_root.expanduser().resolve()
    except OSError:
        resolved_root = package_root.expanduser()
    digest = hashlib.sha256(str(resolved_root).lower().encode("utf-8", errors="replace")).hexdigest()[:24]
    return cache_root / f"archive_sidecars_{digest}.bin"


def resolve_archive_sidecar_cache_metadata_path(package_root: Path, cache_root: Path) -> Path:
    return resolve_archive_sidecar_cache_path(package_root, cache_root).with_suffix(".meta.json")


def resolve_crimson_desert_executable(package_root: Path) -> Optional[Path]:
    base_dir = _archive_base_dir(package_root)
    candidate_roots: List[Path] = []
    for candidate_root in (base_dir, *base_dir.parents[:4]):
        normalized = str(candidate_root).strip().lower()
        if not normalized or any(str(existing).strip().lower() == normalized for existing in candidate_roots):
            continue
        candidate_roots.append(candidate_root)

    for candidate_root in candidate_roots:
        for relative_path in (
            Path("bin64") / "CrimsonDesert.exe",
            Path("CrimsonDesert.exe"),
        ):
            candidate = candidate_root / relative_path
            if candidate.is_file():
                try:
                    return candidate.expanduser().resolve()
                except OSError:
                    return candidate.expanduser()
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def invalidate_archive_browser_cache(
    package_root: Path,
    cache_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    try:
        resolved_cache_root = cache_root.expanduser().resolve()
    except OSError:
        resolved_cache_root = cache_root.expanduser()

    candidate_roots = [resolved_cache_root]
    sibling_parent = resolved_cache_root.parent
    for dirname in _ARCHIVE_SCAN_CACHE_LEGACY_DIRNAMES:
        candidate_roots.append(sibling_parent / dirname)

    cache_paths: List[Path] = []
    seen: set[str] = set()
    for candidate_root in candidate_roots:
        for candidate_path in (
            resolve_archive_scan_cache_path(package_root, candidate_root),
            resolve_archive_sidecar_cache_path(package_root, candidate_root),
            resolve_archive_sidecar_cache_metadata_path(package_root, candidate_root),
        ):
            normalized_path = str(candidate_path).strip().lower()
            if not normalized_path or normalized_path in seen:
                continue
            seen.add(normalized_path)
            cache_paths.append(candidate_path)

    deleted_paths: List[Path] = []
    for cache_path in cache_paths:
        if not cache_path.exists():
            continue
        try:
            cache_path.unlink()
            deleted_paths.append(cache_path)
        except OSError as exc:
            if on_log:
                on_log(f"Warning: could not delete archive cache file {cache_path}: {exc}")

    return deleted_paths


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


def _collect_archive_scan_sources_from_entries(
    package_root: Path,
    entries: Sequence[ArchiveEntry],
) -> Tuple[Path, List[Tuple[str, int, int]]]:
    base_dir = _archive_base_dir(package_root)
    unique_archive_paths: Dict[str, Path] = {}
    for entry in entries:
        for raw_path in (getattr(entry, "pamt_path", None), getattr(entry, "paz_file", None)):
            if raw_path is None:
                continue
            try:
                resolved_path = Path(raw_path).expanduser().resolve()
            except OSError:
                resolved_path = Path(raw_path).expanduser()
            normalized_key = str(resolved_path).strip().lower()
            if not normalized_key or normalized_key in unique_archive_paths:
                continue
            unique_archive_paths[normalized_key] = resolved_path

    sources: List[Tuple[str, int, int]] = []
    for archive_path in sorted(unique_archive_paths.values(), key=lambda value: str(value).lower()):
        stat_result = archive_path.stat()
        sources.append(
            (
                _archive_relative_source_path(base_dir, archive_path),
                int(stat_result.st_size),
                int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
            )
        )
    return base_dir, sources


def _normalize_archive_source_rows(rows: object) -> Optional[List[Tuple[str, int, int]]]:
    if not isinstance(rows, list):
        return None
    normalized_rows: List[Tuple[str, int, int]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            return None
        relative_path, raw_size, raw_mtime_ns = row
        normalized_rows.append((str(relative_path), int(raw_size), int(raw_mtime_ns)))
    return normalized_rows


def _serialize_cache_payload(payload: dict, *, magic: bytes, compress: Optional[bool] = None) -> bytes:
    raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    use_compression = lz4_block is not None if compress is None else bool(compress and lz4_block is not None)
    if use_compression:
        return magic + b"L" + lz4_block.compress(raw, store_size=True)
    return magic + b"R" + raw


def _deserialize_cache_payload(blob: bytes, *, magic: bytes, invalid_message: str) -> dict:
    if not blob.startswith(magic):
        raise ValueError(invalid_message)
    mode = blob[len(magic) : len(magic) + 1]
    payload = blob[len(magic) + 1 :]
    if mode == b"L":
        if lz4_block is None:
            raise ValueError("Compressed cache requires lz4, but python-lz4 is not available.")
        payload = lz4_block.decompress(payload)
    elif mode != b"R":
        raise ValueError("Cache compression mode is not supported.")
    data = pickle.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Cache payload is invalid.")
    return data


def _deserialize_cache_payload_from_path(
    cache_path: Path,
    *,
    magic: bytes,
    invalid_message: str,
) -> dict:
    with cache_path.open("rb") as handle:
        header = handle.read(len(magic) + 1)
        if len(header) < len(magic) + 1 or not header.startswith(magic):
            raise ValueError(invalid_message)
        mode = header[len(magic) : len(magic) + 1]
        if mode == b"R":
            data = pickle.load(handle)
            if not isinstance(data, dict):
                raise ValueError("Cache payload is invalid.")
            return data
        payload = handle.read()
    return _deserialize_cache_payload(header + payload, magic=magic, invalid_message=invalid_message)


def _write_raw_pickle_cache_payload_to_path(
    cache_path: Path,
    *,
    magic: bytes,
    payload: dict,
) -> None:
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with temp_path.open("wb") as handle:
        handle.write(magic)
        handle.write(b"R")
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    temp_path.replace(cache_path)


def _serialize_archive_scan_cache_payload(payload: dict) -> bytes:
    return _serialize_cache_payload(payload, magic=_ARCHIVE_SCAN_CACHE_MAGIC)


def _serialize_archive_sidecar_cache_payload(payload: dict) -> bytes:
    # Sidecar caches are loaded after the archive browser becomes usable, so
    # faster writes/reads are more valuable than smaller files here.
    return _serialize_cache_payload(payload, magic=_ARCHIVE_SIDECAR_CACHE_MAGIC, compress=False)


def _deserialize_archive_scan_cache_payload(blob: bytes) -> dict:
    return _deserialize_cache_payload(
        blob,
        magic=_ARCHIVE_SCAN_CACHE_MAGIC,
        invalid_message="Archive cache header is not recognized.",
    )


def _deserialize_archive_sidecar_cache_payload(blob: bytes) -> dict:
    return _deserialize_cache_payload(
        blob,
        magic=_ARCHIVE_SIDECAR_CACHE_MAGIC,
        invalid_message="Texture sidecar cache header is not recognized.",
    )


def _write_archive_sidecar_cache_metadata(
    metadata_path: Path,
    *,
    version: int,
    sources: Sequence[Tuple[str, int, int]],
    entry_count: int,
) -> None:
    payload = {
        "version": int(version),
        "created_at": time.time(),
        "entry_count": int(entry_count),
        "sources": [[relative_path, int(size), int(mtime_ns)] for relative_path, size, mtime_ns in sources],
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(metadata_path)


def _read_archive_sidecar_cache_metadata(metadata_path: Path) -> Optional[dict]:
    if not metadata_path.is_file():
        return None
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Texture sidecar cache metadata is invalid.")
    return payload


def _archive_entry_cache_signature(package_root: Path, entry: ArchiveEntry) -> Tuple[object, ...]:
    base_dir = _archive_base_dir(package_root)
    paz_path = Path(getattr(entry, "paz_file", ""))
    try:
        paz_stat = paz_path.stat()
        paz_stamp = (
            int(paz_stat.st_size),
            int(getattr(paz_stat, "st_mtime_ns", int(paz_stat.st_mtime * 1_000_000_000))),
        )
    except OSError:
        paz_stamp = (0, 0)
    return (
        str(getattr(entry, "path", "") or "").replace("\\", "/"),
        _archive_relative_source_path(base_dir, Path(getattr(entry, "pamt_path", ""))),
        _archive_relative_source_path(base_dir, paz_path),
        paz_stamp,
        int(getattr(entry, "offset", 0)),
        int(getattr(entry, "comp_size", 0)),
        int(getattr(entry, "orig_size", 0)),
        int(getattr(entry, "flags", 0)),
        int(getattr(entry, "paz_index", 0)),
    )


def _build_archive_entry_cache_signatures(
    package_root: Path,
    entries: Sequence[ArchiveEntry],
) -> Tuple[Tuple[object, ...], ...]:
    return tuple(_archive_entry_cache_signature(package_root, entry) for entry in entries)


def _record_timing(
    timings: Optional[Dict[str, float]],
    key: str,
    started_at: float,
) -> None:
    if timings is None:
        return
    timings[key] = max(0.0, float(time.perf_counter() - started_at))


def save_archive_scan_cache(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Path:
    started_at = time.perf_counter()
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
    _record_timing(timings, "cache_write_s", started_at)
    return cache_path


def load_archive_scan_cache(
    package_root: Path,
    cache_root: Path,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Optional[List[ArchiveEntry]]:
    check_started_at = time.perf_counter()
    candidate_paths = _candidate_archive_scan_cache_paths(package_root, cache_root)
    preferred_cache_path = candidate_paths[0]
    existing_candidate_paths = [candidate for candidate in candidate_paths if candidate.exists()]
    if not existing_candidate_paths:
        if timings is not None:
            timings.setdefault("cache_check_s", max(0.0, float(time.perf_counter() - check_started_at)))
            timings.setdefault("cache_load_s", 0.0)
        return None

    if on_progress:
        on_progress(0, 0, "Checking archive cache...")
    try:
        base_dir, current_sources = _collect_archive_scan_sources(package_root)
    except Exception as exc:
        if on_log:
            on_log(f"Archive cache check failed; will rescan instead: {exc}")
        if timings is not None:
            timings.setdefault("cache_check_s", max(0.0, float(time.perf_counter() - check_started_at)))
            timings.setdefault("cache_load_s", 0.0)
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
            if timings is not None:
                timings["cache_check_s"] = max(0.0, float(time.perf_counter() - check_started_at))
                timings["cache_load_s"] = 0.0
            return []

        try:
            if timings is not None:
                timings["cache_check_s"] = max(0.0, float(time.perf_counter() - check_started_at))
            load_started_at = time.perf_counter()
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
            _record_timing(timings, "cache_load_s", load_started_at)
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
    if timings is not None:
        timings.setdefault("cache_check_s", max(0.0, float(time.perf_counter() - check_started_at)))
        timings.setdefault("cache_load_s", 0.0)
    return None


def scan_archive_entries_cached(
    package_root: Path,
    cache_root: Path,
    *,
    force_refresh: bool = False,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[List[ArchiveEntry], str, Optional[Path], Dict[str, float]]:
    started_at = time.perf_counter()
    timings: Dict[str, float] = {}
    cache_path = resolve_archive_scan_cache_path(package_root, cache_root)
    if force_refresh:
        if on_log:
            on_log("Ignoring archive cache and performing a full rescan.")
        timings["cache_check_s"] = 0.0
        timings["cache_load_s"] = 0.0
    else:
        cached_entries = load_archive_scan_cache(
            package_root,
            cache_root,
            on_log=on_log,
            on_progress=on_progress,
            stop_event=stop_event,
            timings=timings,
        )
        if cached_entries is not None:
            timings.setdefault("archive_scan_s", 0.0)
            timings.setdefault("cache_write_s", 0.0)
            timings["total_s"] = max(0.0, float(time.perf_counter() - started_at))
            return cached_entries, "cache", cache_path, timings

    scan_started_at = time.perf_counter()
    entries = scan_archive_entries(
        package_root,
        on_log=on_log,
        on_progress=on_progress,
        stop_event=stop_event,
    )
    _record_timing(timings, "archive_scan_s", scan_started_at)
    try:
        cache_path = save_archive_scan_cache(
            package_root,
            cache_root,
            entries,
            on_log=on_log,
            on_progress=on_progress,
            stop_event=stop_event,
            timings=timings,
        )
    except Exception as exc:
        if on_log:
            on_log(f"Warning: archive cache could not be written: {exc}")
        cache_path = None
        timings.setdefault("cache_write_s", 0.0)
    timings["total_s"] = max(0.0, float(time.perf_counter() - started_at))
    return entries, "scan", cache_path, timings


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
        "CDMW_PACKAGE_ROOT",
        "CRIMSON_DESERT_PACKAGE_ROOT",
        "cdmw_PACKAGE_ROOT",
        "crimson_forge_toolkit_PACKAGE_ROOT",
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


def _looks_like_archive_index_container(path: Path) -> bool:
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


def looks_like_archive_package_root(path: Path) -> bool:
    if _looks_like_archive_index_container(path):
        return True
    game_files_root = path / "game_files"
    return _looks_like_archive_index_container(game_files_root)


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
    if extension == ".pathc":
        return "metadata"
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
        or extension == ".pathc"
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


def _archive_entry_item_alias_text(entry: ArchiveEntry, item_search_aliases: Optional[Mapping[str, str]]) -> str:
    if not item_search_aliases:
        return ""
    stem = PurePosixPath(entry.basename.replace("\\", "/")).stem.lower()
    if not stem:
        return ""
    keys = [stem]
    for suffix in ("_l", "_r", "_u", "_s", "_t", "_index01", "_index02", "_index03"):
        if stem.endswith(suffix):
            keys.append(stem[: -len(suffix)])
            break
    aliases: List[str] = []
    seen: set[str] = set()
    for key in keys:
        alias = str(item_search_aliases.get(key, "") or "").strip().lower()
        if alias and alias not in seen:
            aliases.append(alias)
            seen.add(alias)
    return " ".join(aliases)


def _archive_entry_matches_text_pattern(path_lower: str, basename_lower: str, pattern: str, alias_lower: str = "") -> bool:
    if not pattern:
        return False
    if any(char in pattern for char in "*?[]"):
        return (
            fnmatch.fnmatch(path_lower, pattern)
            or fnmatch.fnmatch(basename_lower, pattern)
            or bool(alias_lower and fnmatch.fnmatch(alias_lower, pattern))
        )
    return pattern in path_lower or pattern in basename_lower or bool(alias_lower and pattern in alias_lower)


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
    item_search_aliases: Optional[Mapping[str, str]] = None,
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
            alias_lower = _archive_entry_item_alias_text(entry, item_search_aliases)
            if len(include_patterns) > 1:
                matched = any(
                    _archive_entry_matches_text_pattern(path_lower, basename_lower, pattern, alias_lower)
                    for pattern in include_patterns
                )
            elif wildcard_filter:
                matched = (
                    fnmatch.fnmatch(path_lower, wildcard_pattern)
                    or fnmatch.fnmatch(basename_lower, wildcard_pattern)
                    or bool(alias_lower and fnmatch.fnmatch(alias_lower, wildcard_pattern))
                )
            else:
                matched = text in path_lower or text in basename_lower or bool(alias_lower and text in alias_lower)

            if matched and exclude_patterns:
                matched = not any(
                    _archive_entry_matches_text_pattern(path_lower, basename_lower, pattern, alias_lower)
                    for pattern in exclude_patterns
                )
        elif matched and exclude_patterns:
            path_lower = entry.path.lower()
            basename_lower = entry.basename.lower()
            alias_lower = _archive_entry_item_alias_text(entry, item_search_aliases)
            matched = not any(
                _archive_entry_matches_text_pattern(path_lower, basename_lower, pattern, alias_lower)
                for pattern in exclude_patterns
            )

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


def _extract_archive_sidecar_texture_lookup_paths(sidecar_text: str) -> Tuple[str, ...]:
    if not sidecar_text:
        return ()

    texture_paths: List[str] = []
    seen_paths: set[str] = set()

    for match in _ARCHIVE_SIDECAR_TEXTURE_ATTR_RE.finditer(sidecar_text):
        texture_path = html.unescape(str(match.group("value") or "")).replace("\\", "/").strip()
        normalized_texture = normalize_texture_reference_for_sidecar_lookup(texture_path)
        if not normalized_texture or normalized_texture in seen_paths:
            continue
        seen_paths.add(normalized_texture)
        texture_paths.append(normalized_texture)
    return tuple(texture_paths)


def _build_archive_texture_sidecar_path_rows_for_group(
    group_entries: Sequence[Tuple[int, ArchiveEntry]],
    *,
    stop_event: Optional[threading.Event] = None,
    on_entry_processed: Optional[Callable[[int], None]] = None,
) -> Dict[str, List[int]]:
    path_rows_lists: Dict[str, List[int]] = defaultdict(list)
    if not group_entries:
        return path_rows_lists

    paz_path = group_entries[0][1].paz_file
    try:
        with paz_path.open("rb") as handle:
            for entry_index, entry in group_entries:
                raise_if_cancelled(stop_event)
                try:
                    raw_data, _decompressed, _note = _read_archive_entry_data_from_handle(
                        handle,
                        entry,
                        stop_event=stop_event,
                    )
                except RunCancelled:
                    raise
                except Exception:
                    if on_entry_processed is not None:
                        on_entry_processed(1)
                    continue
                if not raw_data or _ARCHIVE_TEXTURE_BYTES_RE.search(raw_data) is None:
                    if on_entry_processed is not None:
                        on_entry_processed(1)
                    continue
                text = try_decode_text_like_archive_data(raw_data)
                if not text:
                    if on_entry_processed is not None:
                        on_entry_processed(1)
                    continue
                for normalized_texture in _extract_archive_sidecar_texture_lookup_paths(text):
                    path_rows_lists[normalized_texture].append(entry_index)
                if on_entry_processed is not None:
                    on_entry_processed(1)
    except RunCancelled:
        raise
    except Exception:
        return {}
    return path_rows_lists


def build_archive_texture_sidecar_path_rows(
    entries: Sequence[ArchiveEntry],
    *,
    worker_count: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    progress_label: str = "Indexing archive texture sidecars...",
    timings: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[int, ...]]:
    grouped_sidecar_entries: Dict[str, List[Tuple[int, ArchiveEntry]]] = defaultdict(list)
    total_sidecars = 0
    for entry_index, entry in enumerate(entries):
        entry_basename = PurePosixPath(entry.path.replace("\\", "/")).name.lower()
        if not _is_material_sidecar_extension(entry.extension, entry_basename):
            continue
        paz_key = str(entry.paz_file).strip().lower()
        grouped_sidecar_entries[paz_key].append((entry_index, entry))
        total_sidecars += 1
    if total_sidecars <= 0:
        return {}

    path_rows_lists: Dict[str, List[int]] = defaultdict(list)
    progress_interval = max(total_sidecars // 100, 1) if total_sidecars > 0 else 1
    processed_count = 0
    progress_lock = threading.Lock()
    sorted_groups = [
        (paz_key, sorted(grouped_sidecar_entries[paz_key], key=lambda item: item[1].offset))
        for paz_key in sorted(grouped_sidecar_entries)
    ]
    try:
        configured_workers = int(
            worker_count
            if worker_count is not None
            else os.environ.get("CDMW_ARCHIVE_SIDECAR_WORKERS")
            or os.environ.get("CFT_ARCHIVE_SIDECAR_WORKERS", "0")
        )
    except ValueError:
        configured_workers = 0
    if configured_workers <= 0:
        configured_workers = min(12, max(4, (os.cpu_count() or 2) - 1), max(1, len(sorted_groups)))
    worker_count = min(max(configured_workers, 1), 16, max(1, len(sorted_groups)))
    if timings is not None:
        timings["sidecar_count"] = float(total_sidecars)
        timings["sidecar_group_count"] = float(len(sorted_groups))
        timings["sidecar_worker_count"] = float(worker_count)

    def merge_group_rows(group_rows: Dict[str, List[int]]) -> None:
        for normalized_texture, entry_indexes in group_rows.items():
            if entry_indexes:
                path_rows_lists[normalized_texture].extend(entry_indexes)

    def publish_progress(force: bool = False) -> None:
        if on_progress is None:
            return
        if force or processed_count == total_sidecars or processed_count % progress_interval == 0:
            on_progress(
                processed_count,
                total_sidecars,
                f"{progress_label} {processed_count:,} / {total_sidecars:,}",
            )

    def mark_entries_processed(count: int = 1) -> None:
        nonlocal processed_count
        if count <= 0:
            return
        with progress_lock:
            processed_count = min(total_sidecars, processed_count + int(count))
            publish_progress(force=False)

    if worker_count <= 1 or total_sidecars < 2_000:
        for _paz_key, group_entries in sorted_groups:
            raise_if_cancelled(stop_event)
            group_rows = _build_archive_texture_sidecar_path_rows_for_group(
                group_entries,
                stop_event=stop_event,
                on_entry_processed=mark_entries_processed,
            )
            merge_group_rows(group_rows)
            publish_progress(force=True)
    else:
        group_results: Dict[str, Dict[str, List[int]]] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ArchiveSidecarIndex") as executor:
            future_by_key = {
                executor.submit(
                    _build_archive_texture_sidecar_path_rows_for_group,
                    group_entries,
                    stop_event=stop_event,
                    on_entry_processed=mark_entries_processed,
                ): (paz_key, len(group_entries))
                for paz_key, group_entries in sorted_groups
            }
            for future in as_completed(future_by_key):
                paz_key, group_count = future_by_key[future]
                raise_if_cancelled(stop_event)
                try:
                    group_results[paz_key] = future.result()
                except RunCancelled:
                    raise
                except Exception:
                    group_results[paz_key] = {}
                    mark_entries_processed(group_count)
                publish_progress(force=True)
        for paz_key, _group_entries in sorted_groups:
            merge_group_rows(group_results.get(paz_key, {}))

    return {key: tuple(value) for key, value in path_rows_lists.items() if value}


def _build_archive_texture_sidecar_path_rows_for_indices(
    entries: Sequence[ArchiveEntry],
    entry_indices: Sequence[int],
    *,
    worker_count: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    progress_label: str = "Indexing changed archive texture sidecars...",
) -> Dict[str, Tuple[int, ...]]:
    grouped_sidecar_entries: Dict[str, List[Tuple[int, ArchiveEntry]]] = defaultdict(list)
    for raw_index in entry_indices:
        entry_index = int(raw_index)
        if entry_index < 0 or entry_index >= len(entries):
            continue
        entry = entries[entry_index]
        entry_basename = PurePosixPath(entry.path.replace("\\", "/")).name.lower()
        if not _is_material_sidecar_extension(entry.extension, entry_basename):
            continue
        paz_key = str(entry.paz_file).strip().lower()
        grouped_sidecar_entries[paz_key].append((entry_index, entry))
    total_sidecars = sum(len(group_entries) for group_entries in grouped_sidecar_entries.values())
    if total_sidecars <= 0:
        return {}

    path_rows_lists: Dict[str, List[int]] = defaultdict(list)
    processed_count = 0
    progress_lock = threading.Lock()
    if on_progress is not None:
        on_progress(0, total_sidecars, f"{progress_label} 0 / {total_sidecars:,}")
    configured_workers = int(worker_count or 0)
    if configured_workers <= 0:
        configured_workers = min(12, max(4, (os.cpu_count() or 2) - 1), max(1, len(grouped_sidecar_entries)))
    configured_workers = min(max(configured_workers, 1), 16, max(1, len(grouped_sidecar_entries)))
    sorted_groups = [
        (paz_key, sorted(grouped_sidecar_entries[paz_key], key=lambda item: item[1].offset))
        for paz_key in sorted(grouped_sidecar_entries)
    ]

    def mark_entries_processed(count: int = 1) -> None:
        nonlocal processed_count
        if count <= 0:
            return
        with progress_lock:
            processed_count = min(total_sidecars, processed_count + int(count))
            if on_progress is not None:
                on_progress(
                    processed_count,
                    total_sidecars,
                    f"{progress_label} {processed_count:,} / {total_sidecars:,}",
                )

    if configured_workers <= 1 or total_sidecars < 2_000:
        for paz_key, group_entries in sorted_groups:
            del paz_key
            raise_if_cancelled(stop_event)
            group_rows = _build_archive_texture_sidecar_path_rows_for_group(
                group_entries,
                stop_event=stop_event,
                on_entry_processed=mark_entries_processed,
            )
            for normalized_texture, row_indices in group_rows.items():
                if row_indices:
                    path_rows_lists[normalized_texture].extend(row_indices)
            if on_progress is not None:
                on_progress(
                    processed_count,
                    total_sidecars,
                    f"{progress_label} {processed_count:,} / {total_sidecars:,}",
                )
    else:
        with ThreadPoolExecutor(max_workers=configured_workers, thread_name_prefix="ArchiveSidecarIndex") as executor:
            future_by_count = {
                executor.submit(
                    _build_archive_texture_sidecar_path_rows_for_group,
                    group_entries,
                    stop_event=stop_event,
                    on_entry_processed=mark_entries_processed,
                ): len(group_entries)
                for _paz_key, group_entries in sorted_groups
            }
            for future in as_completed(future_by_count):
                group_count = future_by_count[future]
                raise_if_cancelled(stop_event)
                try:
                    group_rows = future.result()
                except RunCancelled:
                    raise
                except Exception:
                    group_rows = {}
                    mark_entries_processed(group_count)
                for normalized_texture, row_indices in group_rows.items():
                    if row_indices:
                        path_rows_lists[normalized_texture].extend(row_indices)
                if on_progress is not None:
                    on_progress(
                        processed_count,
                        total_sidecars,
                        f"{progress_label} {processed_count:,} / {total_sidecars:,}",
                    )
    return {key: tuple(value) for key, value in path_rows_lists.items() if value}


def _incremental_archive_texture_sidecar_path_rows(
    package_root: Path,
    entries: Sequence[ArchiveEntry],
    cached_path_rows: Dict[str, Tuple[int, ...]],
    cached_entry_signatures: object,
    *,
    worker_count: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Tuple[int, ...]]]:
    if not isinstance(cached_entry_signatures, (list, tuple)):
        return None
    try:
        old_signatures = tuple(tuple(signature) for signature in cached_entry_signatures)
    except Exception:
        return None
    current_signatures = _build_archive_entry_cache_signatures(package_root, entries)

    current_by_signature: Dict[Tuple[object, ...], int] = {}
    duplicate_current_signatures: set[Tuple[object, ...]] = set()
    for current_index, signature in enumerate(current_signatures):
        if signature in current_by_signature:
            duplicate_current_signatures.add(signature)
            continue
        current_by_signature[signature] = current_index
    for signature in duplicate_current_signatures:
        current_by_signature.pop(signature, None)

    old_to_current: Dict[int, int] = {}
    reused_current_indices: set[int] = set()
    for old_index, signature in enumerate(old_signatures):
        current_index = current_by_signature.get(signature)
        if current_index is None:
            continue
        old_to_current[old_index] = current_index
        reused_current_indices.add(current_index)

    changed_sidecar_indices = [
        index
        for index, entry in enumerate(entries)
        if _is_material_sidecar_extension(
            entry.extension,
            PurePosixPath(entry.path.replace("\\", "/")).name.lower(),
        )
        and index not in reused_current_indices
    ]
    if old_to_current and not changed_sidecar_indices and len(current_signatures) == len(old_signatures):
        if on_log is not None:
            on_log("Texture sidecar cache metadata changed, but all sidecar rows remapped without rescanning.")
    elif on_log is not None:
        on_log(
            "Texture sidecar cache is partially out of date; "
            f"reusing {len(reused_current_indices):,} unchanged entries, rescanning {len(changed_sidecar_indices):,} sidecar entries."
        )

    merge_started_at = time.perf_counter()
    remapped_rows_lists: Dict[str, List[int]] = defaultdict(list)
    for normalized_texture, old_indices in cached_path_rows.items():
        for old_index in old_indices:
            current_index = old_to_current.get(int(old_index))
            if current_index is not None:
                remapped_rows_lists[normalized_texture].append(current_index)
    _record_timing(timings, "incremental_remap_s", merge_started_at)

    scan_started_at = time.perf_counter()
    changed_rows = _build_archive_texture_sidecar_path_rows_for_indices(
        entries,
        changed_sidecar_indices,
        worker_count=worker_count,
        stop_event=stop_event,
        on_progress=on_progress,
    )
    _record_timing(timings, "incremental_scan_s", scan_started_at)
    for normalized_texture, current_indices in changed_rows.items():
        remapped_rows_lists[normalized_texture].extend(int(index) for index in current_indices)

    return {
        key: tuple(dict.fromkeys(value))
        for key, value in remapped_rows_lists.items()
        if value
    }


def _build_archive_sidecar_basename_rows_from_path_rows(
    path_rows: Dict[str, Tuple[int, ...]],
) -> Dict[str, Tuple[int, ...]]:
    basename_rows_lists: Dict[str, List[int]] = defaultdict(list)
    for normalized_texture, raw_indexes in path_rows.items():
        texture_basename = PurePosixPath(str(normalized_texture or "").strip().lower()).name
        if not texture_basename or not raw_indexes:
            continue
        basename_rows_lists[texture_basename].extend(int(index) for index in raw_indexes)
    return {key: tuple(value) for key, value in basename_rows_lists.items() if value}


def build_archive_texture_sidecar_basename_rows(
    path_rows: Dict[str, Tuple[int, ...]],
) -> Dict[str, Tuple[int, ...]]:
    return _build_archive_sidecar_basename_rows_from_path_rows(path_rows)


def resolve_archive_texture_sidecar_entry_rows(
    rows: object,
    entries: Sequence[ArchiveEntry],
) -> Dict[str, List[ArchiveEntry]]:
    return _deserialize_archive_sidecar_entry_rows(rows, entries)


def build_lazy_archive_texture_sidecar_entry_index(
    rows: Optional[Dict[str, Tuple[int, ...]]],
    entries: Sequence[ArchiveEntry],
) -> LazyArchiveEntryRowIndex:
    return LazyArchiveEntryRowIndex(rows, entries)


def build_archive_texture_sidecar_entry_index(
    entries: Sequence[ArchiveEntry],
    *,
    worker_count: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    progress_label: str = "Indexing archive texture sidecars...",
) -> Tuple[Dict[str, List[ArchiveEntry]], Dict[str, List[ArchiveEntry]]]:
    path_rows = build_archive_texture_sidecar_path_rows(
        entries,
        worker_count=worker_count,
        stop_event=stop_event,
        on_progress=on_progress,
        progress_label=progress_label,
    )
    if not path_rows:
        return {}, {}
    basename_rows = _build_archive_sidecar_basename_rows_from_path_rows(path_rows)
    return (
        _deserialize_archive_sidecar_entry_rows(path_rows, entries),
        _deserialize_archive_sidecar_entry_rows(basename_rows, entries),
    )


def _serialize_archive_sidecar_entry_rows(
    index: Dict[str, List[ArchiveEntry]],
    *,
    entry_positions_by_identity: Dict[int, int],
) -> Dict[str, Tuple[int, ...]]:
    rows: Dict[str, Tuple[int, ...]] = {}
    for key, entries_for_key in index.items():
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        entry_indexes: List[int] = []
        seen_indexes: set[int] = set()
        for entry in entries_for_key:
            entry_index = entry_positions_by_identity.get(id(entry))
            if entry_index is None or entry_index in seen_indexes:
                continue
            seen_indexes.add(entry_index)
            entry_indexes.append(entry_index)
        if entry_indexes:
            rows[normalized_key] = tuple(entry_indexes)
    return rows


def _deserialize_archive_sidecar_entry_rows(
    rows: object,
    entries: Sequence[ArchiveEntry],
) -> Dict[str, List[ArchiveEntry]]:
    if not isinstance(rows, dict):
        raise ValueError("Texture sidecar cache rows are invalid.")
    resolved_entries = list(entries)
    entry_count = len(resolved_entries)
    index: Dict[str, List[ArchiveEntry]] = {}
    for key, raw_indexes in rows.items():
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        if not isinstance(raw_indexes, (list, tuple)):
            raise ValueError("Texture sidecar cache entry references are invalid.")
        resolved_for_key: List[ArchiveEntry] = []
        seen_indexes: set[int] = set()
        for raw_index in raw_indexes:
            entry_index = int(raw_index)
            if entry_index < 0 or entry_index >= entry_count:
                raise ValueError("Texture sidecar cache entry index is out of range.")
            if entry_index in seen_indexes:
                continue
            seen_indexes.add(entry_index)
            resolved_for_key.append(resolved_entries[entry_index])
        if resolved_for_key:
            index[normalized_key] = resolved_for_key
    return index


def save_archive_texture_sidecar_cache(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    entries_by_texture_path: Optional[Dict[str, List[ArchiveEntry]]] = None,
    entries_by_texture_basename: Optional[Dict[str, List[ArchiveEntry]]] = None,
    path_rows: Optional[Dict[str, Tuple[int, ...]]] = None,
    basename_rows: Optional[Dict[str, Tuple[int, ...]]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Path:
    started_at = time.perf_counter()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = resolve_archive_sidecar_cache_path(package_root, cache_root)
    metadata_path = resolve_archive_sidecar_cache_metadata_path(package_root, cache_root)
    _base_dir, sources = _collect_archive_scan_sources_from_entries(package_root, entries)
    if on_progress is not None:
        on_progress(0, 0, "Writing texture sidecar cache...")
    raise_if_cancelled(stop_event)
    entry_positions_by_identity: Optional[Dict[int, int]] = None
    if path_rows is None:
        if entries_by_texture_path is None:
            raise ValueError("entries_by_texture_path is required when path_rows is not provided.")
        entry_positions_by_identity = {id(entry): index for index, entry in enumerate(entries)}
        path_rows = _serialize_archive_sidecar_entry_rows(
            entries_by_texture_path,
            entry_positions_by_identity=entry_positions_by_identity,
        )
    if basename_rows is None:
        if entries_by_texture_basename is not None:
            if entry_positions_by_identity is None:
                entry_positions_by_identity = {id(entry): index for index, entry in enumerate(entries)}
            basename_rows = _serialize_archive_sidecar_entry_rows(
                entries_by_texture_basename,
                entry_positions_by_identity=entry_positions_by_identity,
            )
        else:
            basename_rows = _build_archive_sidecar_basename_rows_from_path_rows(path_rows)
    payload = {
        "version": _ARCHIVE_SIDECAR_CACHE_VERSION,
        "created_at": time.time(),
        "sources": sources,
        "entry_count": len(entries),
        "path_rows": path_rows,
        "basename_rows": basename_rows,
    }
    _write_raw_pickle_cache_payload_to_path(
        cache_path,
        magic=_ARCHIVE_SIDECAR_CACHE_MAGIC,
        payload=payload,
    )
    try:
        _write_archive_sidecar_cache_metadata(
            metadata_path,
            version=_ARCHIVE_SIDECAR_CACHE_VERSION,
            sources=sources,
            entry_count=len(entries),
        )
    except Exception as exc:
        if on_log is not None:
            on_log(f"Warning: texture sidecar cache metadata could not be written: {exc}")
    if on_progress is not None:
        on_progress(1, 1, "Texture sidecar cache is ready.")
    if on_log is not None:
        on_log(f"Texture sidecar cache updated: {cache_path}")
    _record_timing(timings, "cache_write_s", started_at)
    return cache_path


def load_archive_texture_sidecar_cache_rows(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    worker_count: Optional[int] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Optional[Tuple[Dict[str, Tuple[int, ...]], Dict[str, Tuple[int, ...]]]]:
    check_started_at = time.perf_counter()
    cache_path = resolve_archive_sidecar_cache_path(package_root, cache_root)
    metadata_path = resolve_archive_sidecar_cache_metadata_path(package_root, cache_root)
    if not cache_path.exists():
        if timings is not None:
            timings.setdefault("cache_check_s", max(0.0, float(time.perf_counter() - check_started_at)))
            timings.setdefault("cache_load_s", 0.0)
        return None
    if on_progress is not None:
        on_progress(0, 0, "Checking texture sidecar cache...")
    try:
        _base_dir, current_sources = _collect_archive_scan_sources_from_entries(package_root, entries)
    except Exception as exc:
        if on_log is not None:
            on_log(f"Texture sidecar cache check failed; rebuilding it now: {exc}")
        if timings is not None:
            timings.setdefault("cache_check_s", max(0.0, float(time.perf_counter() - check_started_at)))
            timings.setdefault("cache_load_s", 0.0)
        return None

    metadata_payload: Optional[dict] = None
    metadata_matches_current_archives = True
    if metadata_path.exists():
        try:
            metadata_payload = _read_archive_sidecar_cache_metadata(metadata_path)
        except Exception as exc:
            if on_log is not None:
                on_log(f"Texture sidecar cache metadata could not be read; falling back to the full cache payload: {exc}")

    if metadata_payload is not None:
        cached_version = int(metadata_payload.get("version", 0))
        if cached_version not in _ARCHIVE_SIDECAR_CACHE_SUPPORTED_VERSIONS:
            if on_log is not None:
                on_log("Texture sidecar cache metadata format changed; rebuilding it now.")
            return None
        cached_sources = _normalize_archive_source_rows(metadata_payload.get("sources"))
        if cached_sources is None or cached_sources != current_sources:
            metadata_matches_current_archives = False
        cached_entry_count = int(metadata_payload.get("entry_count", -1))
        if cached_entry_count != len(entries):
            metadata_matches_current_archives = False
        if not metadata_matches_current_archives:
            if on_log is not None:
                on_log("Texture sidecar cache is out of date; rebuilding it now.")
            if timings is not None:
                timings["cache_check_s"] = max(0.0, float(time.perf_counter() - check_started_at))
                timings.setdefault("cache_load_s", 0.0)
            return None

    if on_progress is not None:
        on_progress(0, 0, "Loading texture sidecar cache...")
    try:
        if timings is not None:
            timings["cache_check_s"] = max(0.0, float(time.perf_counter() - check_started_at))
        load_started_at = time.perf_counter()
        data = _deserialize_cache_payload_from_path(
            cache_path,
            magic=_ARCHIVE_SIDECAR_CACHE_MAGIC,
            invalid_message="Texture sidecar cache header is not recognized.",
        )
    except Exception as exc:
        if on_log is not None:
            on_log(f"Texture sidecar cache could not be read; rebuilding it now: {exc}")
        return None

    if int(data.get("version", 0)) not in _ARCHIVE_SIDECAR_CACHE_SUPPORTED_VERSIONS:
        if on_log is not None:
            on_log("Texture sidecar cache format changed; rebuilding it now.")
        return None

    if metadata_payload is None:
        cached_sources = _normalize_archive_source_rows(data.get("sources"))
        if cached_sources is None or cached_sources != current_sources:
            if on_log is not None:
                on_log("Texture sidecar cache archive stamps changed; rebuilding it now.")
            return None
        cached_entry_count = int(data.get("entry_count", -1))
        if cached_entry_count != len(entries):
            if on_log is not None:
                on_log("Texture sidecar cache entry count changed; rebuilding it now.")
            return None

    try:
        raise_if_cancelled(stop_event)
        raw_path_rows = {
            str(key or "").strip().lower(): tuple(int(index) for index in value)
            for key, value in (data.get("path_rows", {}) or {}).items()
            if isinstance(value, (list, tuple)) and str(key or "").strip()
        }
        raw_basename_rows = data.get("basename_rows")
        if isinstance(raw_basename_rows, dict):
            basename_rows = {
                str(key or "").strip().lower(): tuple(int(index) for index in value)
                for key, value in raw_basename_rows.items()
                if isinstance(value, (list, tuple)) and str(key or "").strip()
            }
        else:
            basename_rows = _build_archive_sidecar_basename_rows_from_path_rows(raw_path_rows)
        _record_timing(timings, "cache_load_s", load_started_at)
    except Exception as exc:
        if on_log is not None:
            on_log(f"Texture sidecar cache could not be applied; rebuilding it now: {exc}")
        return None

    if metadata_payload is None:
        try:
            _write_archive_sidecar_cache_metadata(
                metadata_path,
                version=int(data.get("version", _ARCHIVE_SIDECAR_CACHE_VERSION)),
                sources=current_sources,
                entry_count=len(entries),
            )
        except Exception:
            pass

    if on_progress is not None:
        on_progress(1, 1, "Texture sidecar cache loaded.")
    if on_log is not None:
        on_log("Loaded texture sidecar bindings from cache.")
    return raw_path_rows, basename_rows


def load_archive_texture_sidecar_cache(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    worker_count: Optional[int] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    timings: Optional[Dict[str, float]] = None,
) -> Optional[Tuple[Dict[str, List[ArchiveEntry]], Dict[str, List[ArchiveEntry]]]]:
    cached_rows = load_archive_texture_sidecar_cache_rows(
        package_root,
        cache_root,
        entries,
        worker_count=worker_count,
        on_log=on_log,
        on_progress=on_progress,
        stop_event=stop_event,
        timings=timings,
    )
    if cached_rows is None:
        return None
    path_rows, basename_rows = cached_rows
    return (
        _deserialize_archive_sidecar_entry_rows(path_rows, entries),
        _deserialize_archive_sidecar_entry_rows(basename_rows, entries),
    )


def build_archive_texture_sidecar_entry_index_cached(
    package_root: Path,
    cache_root: Path,
    entries: Sequence[ArchiveEntry],
    *,
    worker_count: Optional[int] = None,
    stop_event: Optional[threading.Event] = None,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[Dict[str, List[ArchiveEntry]], Dict[str, List[ArchiveEntry]], str, Optional[Path]]:
    cache_path = resolve_archive_sidecar_cache_path(package_root, cache_root)
    cached = load_archive_texture_sidecar_cache(
        package_root,
        cache_root,
        entries,
        worker_count=worker_count,
        on_log=on_log,
        on_progress=on_progress,
        stop_event=stop_event,
    )
    if cached is not None:
        entries_by_texture_path, entries_by_texture_basename = cached
        return entries_by_texture_path, entries_by_texture_basename, "cache", cache_path

    if on_log is not None:
        on_log("Indexing texture sidecar bindings for related-file discovery...")
    path_rows = build_archive_texture_sidecar_path_rows(
        entries,
        worker_count=worker_count,
        stop_event=stop_event,
        on_progress=on_progress,
    )
    basename_rows = _build_archive_sidecar_basename_rows_from_path_rows(path_rows)
    entries_by_texture_path = _deserialize_archive_sidecar_entry_rows(path_rows, entries) if path_rows else {}
    entries_by_texture_basename = (
        _deserialize_archive_sidecar_entry_rows(basename_rows, entries) if basename_rows else {}
    )
    try:
        cache_path = save_archive_texture_sidecar_cache(
            package_root,
            cache_root,
            entries,
            path_rows=path_rows,
            basename_rows=basename_rows if int(_ARCHIVE_SIDECAR_CACHE_VERSION) <= 1 else None,
            entries_by_texture_path=entries_by_texture_path,
            entries_by_texture_basename=entries_by_texture_basename,
            on_log=on_log,
            on_progress=on_progress,
            stop_event=stop_event,
        )
    except Exception as exc:
        if on_log is not None:
            on_log(f"Warning: texture sidecar cache could not be written: {exc}")
        cache_path = None
    return entries_by_texture_path, entries_by_texture_basename, "scan", cache_path


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
    Dict[Tuple[str, ...], Tuple[int, int, int]],
]:
    child_folder_sets: Dict[Tuple[str, ...], Dict[Tuple[str, ...], str]] = defaultdict(dict)
    direct_files: Dict[Tuple[str, ...], List[Tuple[str, int]]] = defaultdict(list)
    folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = defaultdict(list)
    folder_preview_stats: Dict[Tuple[str, ...], List[int]] = defaultdict(lambda: [0, 0, 0])
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
        root_stats = folder_preview_stats[()]
        root_stats[0] += 1
        root_stats[1] += int(entry.orig_size)
        root_stats[2] += int(entry.comp_size)
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
            folder_stats = folder_preview_stats[child_key]
            folder_stats[0] += 1
            folder_stats[1] += int(entry.orig_size)
            folder_stats[2] += int(entry.comp_size)

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
    normalized_folder_preview_stats = {
        folder_key: (int(stats[0]), int(stats[1]), int(stats[2]))
        for folder_key, stats in folder_preview_stats.items()
    }
    return child_folders, direct_file_indexes, dict(folder_entry_indexes), normalized_folder_preview_stats


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
    item_search_aliases: Optional[Mapping[str, str]] = None,
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
        item_search_aliases=item_search_aliases,
        on_progress=on_progress,
        stop_event=stop_event,
    )

    tree_child_folders: Dict[Tuple[str, ...], List[Tuple[str, Tuple[str, ...]]]] = {}
    tree_direct_files: Dict[Tuple[str, ...], List[int]] = {}
    folder_entry_indexes: Dict[Tuple[str, ...], List[int]] = {}
    folder_preview_stats: Dict[Tuple[str, ...], Tuple[int, int, int]] = {}
    if build_tree_index:
        raise_if_cancelled(stop_event)
        current_step += 1
        if on_progress:
            on_progress(current_step, total_steps, "Indexing archive browser tree...")
        tree_child_folders, tree_direct_files, folder_entry_indexes, folder_preview_stats = build_archive_tree_index(
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
        "tree_folder_preview_stats": folder_preview_stats,
        "tree_index_ready": build_tree_index,
        "dds_count": dds_count,
    }


class PathcCollection:
    def __init__(self, path: Path, raw_data: Optional[bytes] = None) -> None:
        raw = path.read_bytes() if raw_data is None else bytes(raw_data)
        if len(raw) < 32:
            raise ValueError(f"{path} is too small to be a valid .pathc file.")
        self.path = path
        self.raw_size = len(raw)
        (
            _reserved0,
            header_size,
            header_count,
            entry_count,
            collision_entry_count,
            filenames_length,
        ) = struct.unpack_from("<QIIIII", raw, 0)
        self.reserved0 = _reserved0
        offset = struct.calcsize("<QIIIII")
        self.header_size = header_size
        self.header_count = header_count
        self.entry_count = entry_count
        self.collision_entry_count = collision_entry_count
        self.filenames_length = filenames_length
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
        self.checksums = tuple(checksums)
        entries: List[PathcEntry] = []
        for entry_index in range(entry_count):
            if offset + 20 > len(raw):
                raise ValueError(f"{path.name} entry table is truncated.")
            texture_header_index, collision_start_index, collision_end_index, compressed_block_infos = struct.unpack_from(
                "<HBB16s",
                raw,
                offset,
            )
            checksum = checksums[entry_index] if entry_index < len(checksums) else 0
            entries.append(
                PathcEntry(
                    texture_header_index=texture_header_index,
                    collision_start_index=collision_start_index,
                    collision_end_index=collision_end_index,
                    compressed_block_infos=compressed_block_infos,
                    checksum=checksum,
                )
            )
            offset += 20
        self.entries = {checksum: entry for checksum, entry in zip(checksums, entries)}
        self.entry_rows = tuple(entries)
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
        self.filename_blob = filenames
        self.hash_collision_entries: Dict[str, PathcCollisionEntry] = {}
        for entry in collision_entries:
            end = filenames.find(b"\x00", entry.filename_offset)
            if end < 0:
                end = len(filenames)
            name = filenames[entry.filename_offset:end].decode("utf-8", errors="replace")
            entry.path = name
            self.hash_collision_entries[name] = entry
        self.collision_entries = tuple(collision_entries)
        self.direct_mapping_count = 0
        self.collision_mapping_count = 0
        self.invalid_mapping_count = 0
        self.unknown_mapping_count = 0
        for entry in self.entry_rows:
            if entry.texture_header_index != 0xFFFF:
                if 0 <= int(entry.texture_header_index) < len(self.headers):
                    self.direct_mapping_count += 1
                else:
                    self.invalid_mapping_count += 1
                continue
            if int(entry.collision_start_index) < int(entry.collision_end_index):
                self.collision_mapping_count += 1
            else:
                self.unknown_mapping_count += 1

    def get_file_header(self, path: str) -> bytes:
        lookup = self.lookup_file(path)
        if lookup.mapping_mode not in {"direct", "collision"} or lookup.texture_header_index < 0:
            raise KeyError(lookup.normalized_path)
        header = self.headers[lookup.texture_header_index]
        compressed_block_infos = lookup.compressed_block_infos
        if self.header_size == 0x94:
            return header[:0x20] + compressed_block_infos + header[0x30:]
        return header

    def lookup_file(self, path: str) -> PathcLookupResult:
        normalized = str(path or "").replace("\\", "/").lstrip("/")
        checksum = calculate_pa_checksum(f"/{normalized}")
        entry = self.entries.get(checksum)
        if entry is None:
            return PathcLookupResult(
                normalized_path=normalized,
                checksum=checksum,
                mapping_mode="missing",
                message="No PATHC hash entry matched this path.",
            )
        if entry.texture_header_index != 0xFFFF:
            header_index = int(entry.texture_header_index)
            if 0 <= header_index < len(self.headers):
                return PathcLookupResult(
                    normalized_path=normalized,
                    checksum=checksum,
                    mapping_mode="direct",
                    texture_header_index=header_index,
                    header_size=self.header_size,
                    compressed_block_infos=entry.compressed_block_infos,
                )
            return PathcLookupResult(
                normalized_path=normalized,
                checksum=checksum,
                mapping_mode="invalid",
                texture_header_index=header_index,
                header_size=self.header_size,
                compressed_block_infos=entry.compressed_block_infos,
                message="Direct PATHC header index is outside the header table.",
            )

        collision_entry = self.hash_collision_entries.get(normalized)
        if collision_entry is None:
            return PathcLookupResult(
                normalized_path=normalized,
                checksum=checksum,
                mapping_mode="missing",
                texture_header_index=-1,
                header_size=self.header_size,
                compressed_block_infos=entry.compressed_block_infos,
                message="PATHC hash entry uses collision mapping, but no collision path matched this file.",
            )
        header_index = int(collision_entry.texture_header_index)
        if not (0 <= header_index < len(self.headers)):
            return PathcLookupResult(
                normalized_path=normalized,
                checksum=checksum,
                mapping_mode="invalid",
                texture_header_index=header_index,
                header_size=self.header_size,
                compressed_block_infos=collision_entry.compressed_block_infos,
                collision_path=collision_entry.path,
                message="Collision PATHC header index is outside the header table.",
            )
        return PathcLookupResult(
            normalized_path=normalized,
            checksum=checksum,
            mapping_mode="collision",
            texture_header_index=header_index,
            header_size=self.header_size,
            compressed_block_infos=collision_entry.compressed_block_infos,
            collision_path=collision_entry.path,
        )

    def iter_collision_samples(self, limit: int = 16) -> Tuple[PathcCollisionEntry, ...]:
        return tuple(self.collision_entries[: max(0, int(limit))])


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


def _format_pathc_block_infos(block_infos: bytes) -> str:
    if len(block_infos) < 16:
        return block_infos.hex(" ").upper() if block_infos else "none"
    values = struct.unpack_from("<4I", block_infos, 0)
    return ", ".join(f"mip{i}={value:,}" for i, value in enumerate(values))


def _format_pathc_lookup_detail(lookup: PathcLookupResult) -> str:
    lines = [
        "PATHC Lookup:",
        f"- Path: {lookup.normalized_path or '-'}",
        f"- Hash/checksum: 0x{lookup.checksum:08X}",
        f"- Mapping: {lookup.mapping_mode}",
    ]
    if lookup.texture_header_index >= 0:
        lines.append(f"- Texture header index: {lookup.texture_header_index:,}")
    if lookup.header_size:
        lines.append(f"- Header record size: {lookup.header_size:,} bytes")
    if lookup.compressed_block_infos:
        lines.append(f"- First-four-mip / block metadata: {_format_pathc_block_infos(lookup.compressed_block_infos)}")
    if lookup.collision_path:
        lines.append(f"- Collision path: {lookup.collision_path}")
    if lookup.message:
        lines.append(f"- Note: {lookup.message}")
    return "\n".join(lines)


def build_archive_pathc_preview(data: bytes, virtual_path: str) -> _StructuredBinaryPreviewBundle:
    collection = PathcCollection(Path(PurePosixPath(virtual_path.replace("\\", "/")).name or "0.pathc"), raw_data=data)
    lines = [
        f"PATHC texture path index preview for {virtual_path}",
        "",
        "Summary:",
        f"- Header record size: {collection.header_size:,} bytes",
        f"- DDS template/header records: {collection.header_count:,}",
        f"- Path hash entries: {collection.entry_count:,}",
        f"- Collision path entries: {collection.collision_entry_count:,}",
        f"- Filename table size: {collection.filenames_length:,} bytes",
        f"- Direct mappings: {collection.direct_mapping_count:,}",
        f"- Collision mappings: {collection.collision_mapping_count:,}",
        f"- Unknown mappings: {collection.unknown_mapping_count:,}",
        f"- Invalid mappings: {collection.invalid_mapping_count:,}",
    ]
    collision_samples = collection.iter_collision_samples(limit=16)
    if collision_samples:
        lines.extend(["", "Collision path samples:"])
        for index, collision_entry in enumerate(collision_samples, start=1):
            block_info_text = _format_pathc_block_infos(collision_entry.compressed_block_infos)
            lines.append(
                f"- [{index:02d}] header={collision_entry.texture_header_index} "
                f"offset={collision_entry.filename_offset} path={collision_entry.path or '<empty>'} "
                f"blocks=({block_info_text})"
            )
        if len(collection.collision_entries) > len(collision_samples):
            lines.append(f"... {len(collection.collision_entries) - len(collision_samples):,} more collision path(s)")
    else:
        lines.extend(["", "Collision path samples:", "- None"])

    detail_lines = (
        f"PATHC contains {collection.header_count:,} DDS template/header record(s).",
        f"PATHC contains {collection.entry_count:,} path hash entry/entries.",
        f"Mapping types: direct={collection.direct_mapping_count:,}, collision={collection.collision_mapping_count:,}, "
        f"unknown={collection.unknown_mapping_count:,}, invalid={collection.invalid_mapping_count:,}.",
        "This inspector is read-only and does not change DDS reconstruction or mod packaging.",
    )
    return _StructuredBinaryPreviewBundle(
        preview_text="\n".join(lines),
        detail_lines=detail_lines,
        metadata_label="PATHC Texture Index",
    )


def build_archive_pathc_lookup_detail_for_entry(entry: ArchiveEntry) -> str:
    try:
        pathc_path = resolve_archive_pathc_path(entry)
        if not pathc_path.is_file():
            return ""
        collection = load_pathc_collection(pathc_path)
        return _format_pathc_lookup_detail(collection.lookup_file(entry.path))
    except Exception as exc:
        return f"PATHC Lookup:\n- Unavailable: {exc}"


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


def _read_archive_entry_raw_data_from_handle(
    handle: BinaryIO,
    entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> bytes:
    raise_if_cancelled(stop_event)
    read_size = entry.comp_size if entry.compressed else entry.orig_size
    handle.seek(entry.offset)
    data = handle.read(read_size)
    raise_if_cancelled(stop_event)
    return data


def read_archive_entry_raw_data(
    entry: ArchiveEntry,
    stop_event: Optional[threading.Event] = None,
) -> bytes:
    raise_if_cancelled(stop_event)
    if not entry.paz_file.exists():
        raise ValueError(f"Missing PAZ file: {entry.paz_file}")

    with entry.paz_file.open("rb") as handle:
        return _read_archive_entry_raw_data_from_handle(handle, entry, stop_event=stop_event)


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


def _decode_archive_entry_data(
    entry: ArchiveEntry,
    data: bytes,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bool, str]:
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


def read_archive_entry_data(
    entry: ArchiveEntry,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bool, str]:
    data = read_archive_entry_raw_data(entry, stop_event=stop_event)
    return _decode_archive_entry_data(entry, data, stop_event=stop_event)


def _read_archive_entry_data_from_handle(
    handle: BinaryIO,
    entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bool, str]:
    data = _read_archive_entry_raw_data_from_handle(handle, entry, stop_event=stop_event)
    return _decode_archive_entry_data(entry, data, stop_event=stop_event)


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
    raw_text = str(value or "").replace("\\", "/").strip().lower()
    if not raw_text or raw_text == ".":
        return ""
    normalized = PurePosixPath(raw_text).as_posix().strip().lower()
    if normalized == ".":
        return ""
    return normalized


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

    channel_priority = 0
    for suffix, priority in (
        ("texturer", 3),
        ("maskr", 3),
        ("textureg", 2),
        ("maskg", 2),
        ("textureb", 1),
        ("maskb", 1),
        ("texturea", 0),
        ("maska", 0),
    ):
        if normalized.endswith(suffix):
            channel_priority = priority
            break

    if "diffusetexture" in normalized:
        return (6, 4 + channel_priority)
    if "diffusemask" in normalized:
        return (6, 1 + channel_priority)
    if "overlaycolor" in normalized:
        return (5, 2)
    if any(
        token in normalized
        for token in (
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


def _normalize_model_visible_texture_mode(visible_texture_mode: str) -> str:
    normalized_mode = str(visible_texture_mode or "").strip().lower()
    if normalized_mode not in MODEL_PREVIEW_VISIBLE_TEXTURE_MODES:
        return ModelPreviewRenderSettings().visible_texture_mode
    return normalized_mode


def _classify_model_sidecar_visible_binding(semantic_hint: str, texture_path: str) -> str:
    normalized_hint = str(semantic_hint or "").strip().lower().replace("_", "")
    texture_basename = PurePosixPath(str(texture_path or "").replace("\\", "/")).stem.lower()

    technical_tokens = (
        "normal",
        "height",
        "displacement",
        "material",
        "roughness",
        "metallic",
        "ambientocclusion",
        "occlusion",
        "opacity",
        "specular",
        "orm",
        "rma",
        "mra",
        "arm",
        "ao",
    )
    technical_suffixes = (
        "_n",
        "_normal",
        "_normalmap",
        "_disp",
        "_displacement",
        "_height",
        "_hgt",
        "_dmap",
        "_parallax",
        "_pom",
        "_ssdm",
        "_mask",
        "_ma",
        "_mg",
        "_sp",
        "_orm",
        "_rma",
        "_mra",
        "_arm",
        "_ao",
        "_spec",
        "_specular",
        "_roughness",
        "_metallic",
    )
    if any(token in normalized_hint for token in technical_tokens):
        return "technical"
    if normalized_hint in {"colorblendingmasktexture", "detailmasktexture"}:
        return "technical"
    if "mask" in normalized_hint and not any(
        token in normalized_hint for token in ("diffuse", "albedo", "color", "colour", "overlay", "emissive")
    ):
        return "technical"
    if texture_basename.endswith(technical_suffixes):
        return "technical"

    layer_tokens = (
        "grime",
        "detail",
        "layer",
        "blend",
        "decal",
    )
    if any(token in normalized_hint for token in layer_tokens):
        return "layer_visible"

    primary_tokens = (
        "basecolor",
        "basecolour",
        "albedo",
        "diffuse",
        "colortexture",
        "overlaycolor",
        "base",
    )
    if "overlaycolor" in normalized_hint:
        return "visible_generic"
    if any(token in normalized_hint for token in primary_tokens):
        return "primary_visible"

    generic_tokens = (
        "color",
        "colour",
        "overlay",
        "tint",
        "emissive",
    )
    if any(token in normalized_hint for token in generic_tokens):
        return "visible_generic"

    if not normalized_hint:
        return "visible_generic"
    return "visible_generic"


def _allowed_model_sidecar_visible_classes(visible_texture_mode: str) -> Tuple[str, ...]:
    normalized_mode = _normalize_model_visible_texture_mode(visible_texture_mode)
    if normalized_mode == "mesh_base_first":
        return ("primary_visible",)
    if normalized_mode == "layer_aware_visible":
        return ("primary_visible", "visible_generic", "layer_visible")
    return ("primary_visible", "visible_generic", "layer_visible")


def _model_sidecar_visible_class_priority(binding_class: str) -> int:
    if binding_class == "primary_visible":
        return 3
    if binding_class == "layer_visible":
        return 2
    if binding_class == "visible_generic":
        return 1
    return 0


def _model_texture_slot_hint_priority(preview_slot: str, semantic_hint: str) -> Optional[Tuple[int, int]]:
    normalized_slot = str(preview_slot or "").strip().lower()
    normalized_hint = str(semantic_hint or "").strip().lower().replace("_", "")
    if not normalized_slot or not normalized_hint:
        return None

    if normalized_slot == "base":
        if "basecolor" in normalized_hint:
            return (9, 4)
        if any(token in normalized_hint for token in ("grimediffuse", "detaildiffuse", "detailalbedo", "detailcolor")):
            return (5, 1)
        if any(
            token in normalized_hint
            for token in (
                "overlaycolor",
                "colortexture",
                "diffuse",
                "albedo",
                "emissive",
            )
        ):
            return (8, 3)
        if "tintcolor" in normalized_hint:
            return (6, 1)
        if "color" in normalized_hint or "overlay" in normalized_hint or "tint" in normalized_hint:
            return (5, 0)
        return None

    if normalized_slot == "normal":
        if normalized_hint in {"normaltexture", "basenormaltexture"}:
            return (9, 4)
        if "detailnormal" in normalized_hint or "grimenormal" in normalized_hint:
            return (5, 1)
        if normalized_hint.startswith("normal") or normalized_hint.endswith("normaltexture"):
            return (8, 3)
        if "normal" in normalized_hint:
            return (6, 0)
        return None

    if normalized_slot == "material":
        if normalized_hint in {"materialtexture", "basematerialtexture"}:
            return (9, 4)
        if "detailmaterial" in normalized_hint or "grimematerial" in normalized_hint:
            return (5, 1)
        if normalized_hint.startswith("material") or normalized_hint.endswith("materialtexture"):
            return (8, 3)
        if any(token in normalized_hint for token in ("masktexture", "detailmask", "material", "roughness", "metallic", "occlusion")):
            return (6, 0)
        return None

    if normalized_slot == "height":
        if normalized_hint in {"heighttexture", "displacementtexture"}:
            return (9, 4)
        if "detailheight" in normalized_hint or "detaildisplacement" in normalized_hint:
            return (5, 1)
        if normalized_hint.startswith("height") or normalized_hint.endswith("heighttexture"):
            return (8, 3)
        if normalized_hint.startswith("displacement") or normalized_hint.endswith("displacementtexture"):
            return (8, 2)
        if any(token in normalized_hint for token in ("height", "displacement", "parallax", "pom", "ssdm", "bump")):
            return (6, 0)
        return None

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
    candidate_extension = str(candidate.extension or "").strip().lower()
    candidate_basename = PurePosixPath(candidate.path.replace("\\", "/")).name.lower()
    if source_extension in {".pam", ".pamlod"} and normalized_candidate.endswith(".pami"):
        extension_priority = 2
    elif _is_material_sidecar_extension(candidate_extension, candidate_basename):
        extension_priority = 2
    elif normalized_candidate.endswith(".xml") or candidate_extension in _ARCHIVE_METADATA_XML_EXTENSIONS:
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
        elif candidate_extension in {".pami", ".pam_xml"}:
            extension_priority = 5
        elif candidate_extension in {".xml", ".pamlod_xml"}:
            extension_priority = 4
        elif candidate_extension == ".meshinfo":
            extension_priority = 3
        elif candidate_extension == ".hkx":
            extension_priority = 2
    elif source_extension == ".pamlod":
        if candidate_extension == ".pam":
            extension_priority = 6
        elif candidate_extension in {".pami", ".pamlod_xml", ".pam_xml"}:
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
        elif candidate_extension == ".pac_xml":
            extension_priority = 6
        elif candidate_extension in {".xml", ".prefabdata_xml"}:
            extension_priority = 5
        elif candidate_extension == ".meshinfo":
            extension_priority = 4
        elif candidate_extension == ".hkx":
            extension_priority = 3
    elif source_extension == ".meshinfo":
        if candidate_extension in {".pam", ".pamlod", ".pac"}:
            extension_priority = 7
        elif candidate_extension == ".hkx":
            extension_priority = 6
        elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
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
        elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            extension_priority = 4
    elif source_extension in {".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
        if candidate_extension in {".hkx", ".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
            extension_priority = 6
        elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            extension_priority = 5
    elif source_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
        source_stem_lower = PurePosixPath(source_entry.path.replace("\\", "/")).stem.lower()
        if source_stem_lower.endswith(".pac"):
            if candidate_extension == ".pac":
                extension_priority = 7
            elif candidate_extension == ".pab":
                extension_priority = 6
            elif candidate_extension == ".meshinfo":
                extension_priority = 5
            elif candidate_extension == ".hkx":
                extension_priority = 4
        elif source_stem_lower.endswith(".pam"):
            if candidate_extension == ".pam":
                extension_priority = 7
            elif candidate_extension == ".pamlod":
                extension_priority = 6
            elif candidate_extension == ".pami":
                extension_priority = 5
            elif candidate_extension == ".meshinfo":
                extension_priority = 4
            elif candidate_extension == ".hkx":
                extension_priority = 3
        elif source_stem_lower.endswith(".pamlod"):
            if candidate_extension == ".pamlod":
                extension_priority = 7
            elif candidate_extension == ".pam":
                extension_priority = 6
            elif candidate_extension == ".pami":
                extension_priority = 5
            elif candidate_extension == ".meshinfo":
                extension_priority = 4
            elif candidate_extension == ".hkx":
                extension_priority = 3
        elif source_stem_lower.endswith(".pab"):
            if candidate_extension == ".pab":
                extension_priority = 7
            elif candidate_extension == ".pac":
                extension_priority = 6
            elif candidate_extension == ".hkx":
                extension_priority = 5
            elif candidate_extension == ".meshinfo":
                extension_priority = 4
        elif candidate_extension in {".pam", ".pamlod", ".pac", ".pab", ".pami", ".meshinfo", ".hkx"}:
            extension_priority = 3
    elif source_extension == ".pami":
        if candidate_extension in {".pam", ".pamlod"}:
            extension_priority = 7
        elif candidate_extension == ".meshinfo":
            extension_priority = 6
        elif candidate_extension == ".hkx":
            extension_priority = 5
        elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            extension_priority = 4
    elif source_extension == ".hkx":
        if candidate_extension in {".pam", ".pamlod", ".pac"}:
            extension_priority = 7
        elif candidate_extension == ".pab":
            extension_priority = 6
        elif candidate_extension == ".meshinfo":
            extension_priority = 5
        elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            extension_priority = 4
    elif candidate_extension in _ARCHIVE_XML_LIKE_EXTENSIONS | {".meshinfo", ".hkx"}:
        extension_priority = 2
    return score_value, -len(candidate.path), extension_priority


def _extend_archive_related_target_basenames(
    add_target: Callable[[str], None],
    *,
    stem: str,
    source_extension: str,
) -> None:
    if not stem:
        return
    add_target(f"{stem}.xml")
    add_target(f"{stem}.hkx")
    add_target(f"{stem}.meshinfo")
    if source_extension in {".pam", ".pamlod"}:
        add_target(f"{stem}.pami")
        add_target(f"{stem}.pam_xml")
        add_target(f"{stem}.pamlod_xml")
    if source_extension == ".pam":
        add_target(f"{stem}.pamlod")
        if stem.endswith("_breakable"):
            add_target(f"{stem[:-10]}.pamlod")
    elif source_extension == ".pamlod":
        add_target(f"{stem}.pam")
    elif source_extension == ".pac":
        add_target(f"{stem}.pab")
        add_target(f"{stem}.prefabdata.xml")
        add_target(f"{stem}.pac_xml")
        add_target(f"{stem}.prefabdata_xml")
    elif source_extension == ".meshinfo":
        add_target(f"{stem}.pam")
        add_target(f"{stem}.pamlod")
        add_target(f"{stem}.pac")
        add_target(f"{stem}.pami")
    elif source_extension == ".pab":
        add_target(f"{stem}.pac")
    elif source_extension == ".pami":
        add_target(f"{stem}.pam")
        add_target(f"{stem}.pamlod")
    elif source_extension in {".pac_xml", ".pam_xml", ".pamlod_xml", ".prefabdata_xml"}:
        if source_extension == ".pac_xml":
            add_target(f"{stem}.pac")
            add_target(f"{stem}.pab")
            add_target(f"{stem}.hkx")
            add_target(f"{stem}.meshinfo")
        elif source_extension == ".pam_xml":
            add_target(f"{stem}.pam")
            add_target(f"{stem}.pamlod")
            add_target(f"{stem}.pami")
            add_target(f"{stem}.meshinfo")
            add_target(f"{stem}.hkx")
        elif source_extension == ".pamlod_xml":
            add_target(f"{stem}.pamlod")
            add_target(f"{stem}.pam")
            add_target(f"{stem}.pami")
            add_target(f"{stem}.meshinfo")
            add_target(f"{stem}.hkx")
    elif source_extension in {".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
        for related_extension in (".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"):
            add_target(f"{stem}{related_extension}")
    elif source_extension == ".hkx":
        add_target(f"{stem}.pam")
        add_target(f"{stem}.pamlod")
        add_target(f"{stem}.pac")
        add_target(f"{stem}.pab")
        add_target(f"{stem}.pami")


def _collect_same_stem_related_target_basenames(source_entry: ArchiveEntry) -> set[str]:
    normalized_path = source_entry.path.replace("\\", "/").strip()
    basename = PurePosixPath(normalized_path).name.strip().lower()
    stem = PurePosixPath(normalized_path).stem.strip()
    source_extension = str(source_entry.extension or "").strip().lower()
    targets: set[str] = set()

    def add_target(raw_value: str) -> None:
        candidate = str(raw_value or "").strip().lower()
        if candidate:
            targets.add(candidate)

    if basename:
        add_target(f"{basename}.xml")
        add_target(f"{basename}.hkx")
        add_target(f"{basename}.meshinfo")
    if stem:
        _extend_archive_related_target_basenames(
            add_target,
            stem=stem,
            source_extension=source_extension,
        )
        if source_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            nested_basename = stem.strip().lower()
            nested_extension = PurePosixPath(nested_basename).suffix.strip().lower()
            nested_stem = PurePosixPath(nested_basename).stem.strip()
            if nested_extension:
                add_target(nested_basename)
                _extend_archive_related_target_basenames(
                    add_target,
                    stem=nested_stem,
                    source_extension=nested_extension,
                )
    return targets


def _collect_family_heuristic_target_basenames(source_entry: ArchiveEntry) -> set[str]:
    normalized_path = source_entry.path.replace("\\", "/").strip().lower()
    source_extension = str(source_entry.extension or "").strip().lower()
    if source_extension not in {".pac", ".pab", ".hkx", ".meshinfo", ".xml", ".pac_xml", ".prefabdata_xml"}:
        return set()
    targets: set[str] = set()
    for pab_basename in iter_pab_candidate_basenames(normalized_path):
        normalized_pab = str(pab_basename or "").strip().lower()
        if not normalized_pab:
            continue
        targets.add(normalized_pab)
        family_stem = PurePosixPath(normalized_pab).stem
        if not family_stem:
            continue
        for extension in (".pac", ".pab", ".hkx", ".meshinfo", ".prefabdata.xml", ".pac_xml", ".prefabdata_xml"):
            targets.add(f"{family_stem}{extension}")
    return targets


def _relation_group_for_kind(relation_kind: str) -> str:
    normalized_kind = str(relation_kind or "").strip().lower()
    if normalized_kind == RelationKind.TEXTURE.value:
        return "Textures"
    if normalized_kind == RelationKind.MATERIAL_SIDECAR.value:
        return "Material Sidecars"
    if normalized_kind in {RelationKind.MESH.value, RelationKind.LOD.value}:
        return "Mesh / Model"
    if normalized_kind == RelationKind.SKELETON.value:
        return "Skeleton / Rig"
    if normalized_kind == RelationKind.ANIMATION.value:
        return "Animation / Motion"
    return "Metadata / Other"


def _relation_kind_for_entry(candidate_entry: Optional[ArchiveEntry], reference_name: str = "") -> str:
    reference_path = str(getattr(candidate_entry, "path", "") or reference_name).replace("\\", "/")
    reference_basename = PurePosixPath(reference_path).name.lower()
    extension = str(getattr(candidate_entry, "extension", "") or PurePosixPath(reference_path).suffix).strip().lower()
    if extension == ".dds":
        return RelationKind.TEXTURE.value
    if _is_material_sidecar_extension(extension, reference_basename):
        return RelationKind.MATERIAL_SIDECAR.value
    if extension == ".xml":
        return RelationKind.METADATA.value
    if extension == ".prefabdata_xml":
        return RelationKind.METADATA.value
    if extension in {".pab", ".pabc"}:
        return RelationKind.SKELETON.value
    if extension in {".pac", ".pam"}:
        return RelationKind.MESH.value
    if extension == ".pamlod":
        return RelationKind.LOD.value
    if extension in {".hkx", ".motionblending", ".papr", ".paa", ".pae", ".paem", ".paseq", ".paschedule", ".paschedulepath", ".pastage"}:
        return RelationKind.ANIMATION.value
    return RelationKind.METADATA.value


def _build_archive_relation_metadata(
    source_entry: ArchiveEntry,
    *,
    reference_name: str = "",
    resolved_entry: Optional[ArchiveEntry] = None,
    authoritative: bool = False,
    authoritative_reason: str = "",
) -> Tuple[str, str, str, str]:
    relation_kind = _relation_kind_for_entry(resolved_entry, reference_name=reference_name)
    normalized_reference = _normalize_model_texture_reference(reference_name)
    normalized_source = _normalize_model_texture_reference(source_entry.path)
    normalized_resolved = _normalize_model_texture_reference(str(getattr(resolved_entry, "path", "") or ""))
    normalized_basename = PurePosixPath(
        str(getattr(resolved_entry, "path", "") or reference_name).replace("\\", "/")
    ).name.strip().lower()
    same_stem_targets = _collect_same_stem_related_target_basenames(source_entry)
    family_targets = _collect_family_heuristic_target_basenames(source_entry)
    if authoritative:
        confidence = RelationConfidence.AUTHORITATIVE.value
        reason = authoritative_reason or "Explicit path or sidecar binding"
    elif normalized_reference and normalized_resolved and normalized_reference == normalized_resolved:
        confidence = RelationConfidence.EXACT_PATH.value
        reason = "Exact archive path"
    elif (
        normalized_reference
        and normalized_resolved
        and normalized_reference.lstrip("/") == normalized_resolved.lstrip("/")
    ):
        confidence = RelationConfidence.PATH_NORMALIZED.value
        reason = "Path-normalized reference"
    elif (
        normalized_source
        and normalized_resolved
        and normalized_source.replace("/modelproperty/", "/model/") == normalized_resolved
    ):
        confidence = RelationConfidence.PATH_NORMALIZED.value
        reason = "Linked mesh via modelproperty -> model"
    elif (
        normalized_source
        and normalized_resolved
        and normalized_source.replace("/model/", "/modelproperty/") == normalized_resolved
    ):
        confidence = RelationConfidence.PATH_NORMALIZED.value
        reason = "Linked material sidecar via model -> modelproperty"
    elif (
        isinstance(resolved_entry, ArchiveEntry)
        and source_entry.pamt_path != resolved_entry.pamt_path
        and source_entry.pamt_path.parent != resolved_entry.pamt_path.parent
    ):
        confidence = RelationConfidence.CROSS_PACKAGE.value
        reason = "Cross-package reference"
    elif normalized_basename and normalized_basename in family_targets and normalized_basename not in same_stem_targets:
        confidence = RelationConfidence.DERIVED_FAMILY_HEURISTIC.value
        reason = "Family-token heuristic"
    else:
        confidence = RelationConfidence.DERIVED_SAME_STEM.value
        reason = "Same-stem heuristic"
    return relation_kind, _relation_group_for_kind(relation_kind), confidence, reason


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
    must_keep_basenames: set[str] = set()

    def add_target(raw_value: str, *, must_keep: bool = False) -> None:
        candidate = str(raw_value or "").strip().lower()
        if candidate:
            target_basenames.add(candidate)
            if must_keep:
                must_keep_basenames.add(candidate)

    add_target(f"{basename}.xml", must_keep=True)
    if source_stem:
        _extend_archive_related_target_basenames(
            add_target,
            stem=source_stem,
            source_extension=source_extension,
        )
        if source_extension == ".pac":
            add_target(f"{source_stem}.pab", must_keep=True)
            add_target(f"{source_stem}.prefabdata.xml", must_keep=True)
            add_target(f"{source_stem}.pac_xml", must_keep=True)
            add_target(f"{source_stem}.prefabdata_xml", must_keep=True)
        elif source_extension == ".pam":
            add_target(f"{source_stem}.pami", must_keep=True)
            add_target(f"{source_stem}.pam_xml", must_keep=True)
            add_target(f"{source_stem}.pamlod", must_keep=True)
        elif source_extension == ".pamlod":
            add_target(f"{source_stem}.pami", must_keep=True)
            add_target(f"{source_stem}.pamlod_xml", must_keep=True)
            add_target(f"{source_stem}.pam_xml", must_keep=True)
            add_target(f"{source_stem}.pam", must_keep=True)
        if source_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            nested_basename = source_stem.strip()
            nested_extension = PurePosixPath(nested_basename).suffix.strip().lower()
            nested_stem = PurePosixPath(nested_basename).stem.strip()
            if nested_extension:
                add_target(nested_basename, must_keep=True)
                _extend_archive_related_target_basenames(
                    add_target,
                    stem=nested_stem,
                    source_extension=nested_extension,
                )
    for family_target in _collect_family_heuristic_target_basenames(source_entry):
        add_target(family_target)
    add_target(f"{basename}.hkx", must_keep=True)
    add_target(f"{basename}.meshinfo", must_keep=True)

    candidates: List[ArchiveEntry] = []
    must_keep_candidates: List[ArchiveEntry] = []
    for target_basename in target_basenames:
        for candidate in archive_entries_by_basename.get(target_basename, ()):
            if candidate.path == source_entry.path:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
            if target_basename in must_keep_basenames and candidate not in must_keep_candidates:
                must_keep_candidates.append(candidate)
    if not candidates:
        return ()
    candidates.sort(key=lambda candidate: _score_model_related_entry_candidate(source_entry, candidate), reverse=True)
    ordered: List[ArchiveEntry] = []
    for candidate in must_keep_candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered[:64])


def _find_archive_model_sidecar_entries(
    source_entry: ArchiveEntry,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]],
) -> Tuple[ArchiveEntry, ...]:
    if archive_entries_by_basename is None:
        return ()

    normalized_path = source_entry.path.replace("\\", "/").strip()
    basename = PurePosixPath(normalized_path).name.strip()
    source_stem = PurePosixPath(normalized_path).stem.strip()
    source_extension = str(source_entry.extension or "").strip().lower()
    target_basenames: set[str] = set()

    def add_target(raw_value: str) -> None:
        candidate = str(raw_value or "").strip().lower()
        if candidate:
            target_basenames.add(candidate)

    if basename:
        add_target(f"{basename}.xml")
    if source_stem:
        add_target(f"{source_stem}.xml")
        if source_extension == ".pac":
            add_target(f"{source_stem}.pac_xml")
        elif source_extension == ".pam":
            add_target(f"{source_stem}.pam_xml")
        elif source_extension == ".pamlod":
            add_target(f"{source_stem}.pamlod_xml")
        if source_extension in {".pam", ".pamlod"}:
            add_target(f"{source_stem}.pami")
        elif source_extension in _ARCHIVE_XML_LIKE_EXTENSIONS:
            nested_basename = source_stem.strip()
            nested_extension = PurePosixPath(nested_basename).suffix.strip().lower()
            nested_stem = PurePosixPath(nested_basename).stem.strip()
            if nested_extension:
                add_target(f"{nested_basename}.xml")
                add_target(f"{nested_stem}.xml")
                if nested_extension == ".pac":
                    add_target(f"{nested_stem}.pac_xml")
                elif nested_extension == ".pam":
                    add_target(f"{nested_stem}.pam_xml")
                elif nested_extension == ".pamlod":
                    add_target(f"{nested_stem}.pamlod_xml")
                if nested_extension in {".pam", ".pamlod"}:
                    add_target(f"{nested_stem}.pami")

    candidates: List[ArchiveEntry] = []
    for target_basename in target_basenames:
        for candidate in archive_entries_by_basename.get(target_basename, ()):
            if candidate.path == source_entry.path:
                continue
            candidate_basename = PurePosixPath(candidate.path.replace("\\", "/")).name.lower()
            if not _is_material_sidecar_extension(candidate.extension, candidate_basename):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        candidates = [
            candidate
            for candidate in _find_archive_model_related_entries(source_entry, archive_entries_by_basename)
            if _is_material_sidecar_extension(
                str(candidate.extension or "").strip().lower(),
                PurePosixPath(candidate.path.replace("\\", "/")).name.lower(),
            )
        ]
    if not candidates:
        return ()
    candidates.sort(key=lambda candidate: _score_model_sidecar_entry_candidate(source_entry, candidate), reverse=True)
    return tuple(candidates[:8])


def _parse_archive_model_sidecar_texture_bindings(
    sidecar_text: str,
    *,
    sidecar_path: str,
) -> Tuple[_ArchiveModelSidecarTextureBinding, ...]:
    parsed_bindings = parse_texture_sidecar_bindings(sidecar_text, sidecar_path=sidecar_path)
    archive_bindings: List[_ArchiveModelSidecarTextureBinding] = []
    try:
        from cdmw.modding.asset_replacement import classify_texture_binding
    except Exception:
        classify_texture_binding = None  # type: ignore[assignment]
    for binding in parsed_bindings:
        texture_role = binding.texture_role
        visualization_state = binding.visualization_state
        if classify_texture_binding is not None:
            try:
                classification = classify_texture_binding(binding.parameter_name, binding.texture_path)
                texture_role = classification.slot_label or classification.slot_kind
                visualization_state = classification.visual_state
            except Exception:
                pass
        archive_bindings.append(
            _ArchiveModelSidecarTextureBinding(
                texture_path=binding.texture_path,
                parameter_name=binding.parameter_name,
                submesh_name=binding.submesh_name,
                sidecar_path=binding.sidecar_path,
                sidecar_kind=binding.sidecar_kind,
                linked_mesh_path=binding.linked_mesh_path,
                part_name=binding.part_name,
                material_name=binding.material_name,
                shader_family=binding.shader_family,
                texture_role=texture_role,
                visualization_state=visualization_state,
                resolved_texture_exists=binding.resolved_texture_exists,
                represent_color=tuple(binding.represent_color or ()),
                tint_color=tuple(binding.tint_color or ()),
                brightness=float(binding.brightness or 1.0),
                uv_scale=float(binding.uv_scale or 1.0),
                tile_type=binding.tile_type,
            )
        )
    return tuple(archive_bindings)


def _archive_entry_identity_signature(entry: ArchiveEntry) -> Tuple[object, ...]:
    try:
        paz_stat = Path(getattr(entry, "paz_file", "")).stat()
        paz_stamp = (
            int(paz_stat.st_size),
            int(getattr(paz_stat, "st_mtime_ns", int(paz_stat.st_mtime * 1_000_000_000))),
        )
    except OSError:
        paz_stamp = (0, 0)
    return (
        str(getattr(entry, "path", "") or "").replace("\\", "/"),
        str(getattr(entry, "pamt_path", "") or ""),
        str(getattr(entry, "paz_file", "") or ""),
        paz_stamp,
        int(getattr(entry, "offset", 0)),
        int(getattr(entry, "comp_size", 0)),
        int(getattr(entry, "orig_size", 0)),
        int(getattr(entry, "flags", 0)),
        int(getattr(entry, "paz_index", 0)),
    )


def _extract_model_sidecar_entry_bindings_cached(
    sidecar_entry: ArchiveEntry,
    *,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[
    Tuple[_ArchiveModelSidecarTextureBinding, ...],
    Tuple[str, ...],
    Dict[str, Tuple[str, ...]],
    Dict[str, Tuple[str, ...]],
]:
    cache_key = _archive_entry_identity_signature(sidecar_entry)
    with _MODEL_SIDECAR_PARSE_CACHE_LOCK:
        cached = _MODEL_SIDECAR_PARSE_CACHE.get(cache_key)
        if cached is not None:
            _MODEL_SIDECAR_PARSE_CACHE.move_to_end(cache_key)
            return cached

    sidecar_data, _decompressed, _note = read_archive_entry_data(sidecar_entry, stop_event=stop_event)
    text = try_decode_text_like_archive_data(sidecar_data)
    if text is None:
        parsed_result = ((), (), {}, {})
    else:
        parsed_bindings = _parse_archive_model_sidecar_texture_bindings(text, sidecar_path=sidecar_entry.path)
        sidecar_texts_by_normalized_path: Dict[str, List[str]] = defaultdict(list)
        sidecar_texts_by_basename: Dict[str, List[str]] = defaultdict(list)
        for binding in parsed_bindings:
            normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
            if not normalized_texture_path:
                continue
            sidecar_texts_by_normalized_path[normalized_texture_path].append(text)
            texture_basename = PurePosixPath(normalized_texture_path).name
            if texture_basename:
                sidecar_texts_by_basename[texture_basename].append(text)
        parsed_result = (
            tuple(parsed_bindings),
            (sidecar_entry.path,) if parsed_bindings else (),
            {key: tuple(values) for key, values in sidecar_texts_by_normalized_path.items()},
            {key: tuple(values) for key, values in sidecar_texts_by_basename.items()},
        )

    with _MODEL_SIDECAR_PARSE_CACHE_LOCK:
        _MODEL_SIDECAR_PARSE_CACHE[cache_key] = parsed_result
        _MODEL_SIDECAR_PARSE_CACHE.move_to_end(cache_key)
        while len(_MODEL_SIDECAR_PARSE_CACHE) > _MODEL_SIDECAR_PARSE_CACHE_LIMIT:
            _MODEL_SIDECAR_PARSE_CACHE.popitem(last=False)
    return parsed_result


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
            parsed_bindings, parsed_paths, parsed_texts_by_path, parsed_texts_by_basename = (
                _extract_model_sidecar_entry_bindings_cached(sidecar_entry, stop_event=stop_event)
            )
        except Exception:
            continue
        if not parsed_bindings:
            continue
        for parsed_path in parsed_paths:
            if parsed_path not in sidecar_paths:
                sidecar_paths.append(parsed_path)
        for key, values in parsed_texts_by_path.items():
            sidecar_texts_by_normalized_path[key].extend(values)
        for key, values in parsed_texts_by_basename.items():
            sidecar_texts_by_basename[key].extend(values)
        for binding in parsed_bindings:
            normalized_texture_path = normalize_texture_reference_for_sidecar_lookup(binding.texture_path)
            key = (
                normalized_texture_path,
                str(binding.submesh_name or "").strip().lower(),
                str(binding.parameter_name or "").strip().lower(),
            )
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


def _iter_model_sidecar_binding_submesh_keys(binding: _ArchiveModelSidecarTextureBinding) -> Tuple[str, ...]:
    values: List[str] = [
        str(getattr(binding, "submesh_name", "") or ""),
        str(getattr(binding, "part_name", "") or ""),
        str(getattr(binding, "material_name", "") or ""),
    ]
    linked_mesh_path = str(getattr(binding, "linked_mesh_path", "") or "").replace("\\", "/").strip()
    if linked_mesh_path:
        linked_mesh = PurePosixPath(linked_mesh_path)
        values.extend([linked_mesh_path, linked_mesh.name, linked_mesh.stem])
    return _iter_model_submesh_reference_candidates(*values)


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


def _iter_model_texture_slot_family_reference_candidates(
    group_key: str,
    preview_slot: str,
) -> Tuple[str, ...]:
    normalized_slot = str(preview_slot or "").strip().lower()
    if not normalized_slot or normalized_slot == "base":
        return _iter_model_texture_family_reference_candidates(group_key)

    suffixes = _MODEL_TEXTURE_SUPPORT_FAMILY_SUFFIXES.get(normalized_slot, ())
    if not suffixes:
        return ()

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

    for suffix in suffixes:
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


def _match_model_texture_slot_family_suffix(
    texture_path: str,
    preview_slot: str,
) -> int:
    normalized_slot = str(preview_slot or "").strip().lower()
    suffixes = _MODEL_TEXTURE_SUPPORT_FAMILY_SUFFIXES.get(normalized_slot, ())
    if not suffixes:
        return -1
    basename = PurePosixPath(_normalize_model_texture_reference(texture_path)).name
    if not basename.endswith(".dds"):
        return -1
    stem = basename[:-4]
    for index, suffix in enumerate(suffixes):
        if stem.endswith(suffix):
            return index
    return -1


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


def _resolve_model_texture_semantic_details(
    texture_path: str,
    *,
    family_members: Sequence[str] = (),
    sidecar_texts: Sequence[str] = (),
) -> Tuple[str, str, int, Tuple[str, ...]]:
    semantic = infer_texture_semantics(
        texture_path,
        family_members=family_members,
        sidecar_texts=sidecar_texts,
    )
    texture_type = str(getattr(semantic, "texture_type", "") or "").strip().lower() or "unknown"
    semantic_subtype = str(getattr(semantic, "semantic_subtype", "") or "").strip().lower() or texture_type
    confidence = int(getattr(semantic, "confidence", 0) or 0)
    packed_channels = tuple(
        str(item or "").strip().lower()
        for item in getattr(semantic, "packed_channels", ())
        if str(item or "").strip()
    )
    if texture_type == "unknown":
        normalized = _normalize_model_texture_reference(texture_path)
        if normalized.endswith(".dds") and not _looks_like_technical_model_texture(normalized):
            return "color", "albedo", max(confidence, 64), ()
    return texture_type, semantic_subtype, confidence, packed_channels


def _refine_model_texture_semantic_from_hint(
    texture_type: str,
    semantic_subtype: str,
    semantic_hint: str,
) -> Tuple[str, str]:
    normalized_hint = re.sub(r"[^a-z0-9]+", "", str(semantic_hint or "").strip().lower())
    normalized_type = str(texture_type or "").strip().lower()
    normalized_subtype = str(semantic_subtype or "").strip().lower()
    if not normalized_hint:
        return normalized_type, normalized_subtype

    if any(token in normalized_hint for token in ("orm", "occlusionroughnessmetallic")):
        return "mask", "orm"
    if any(token in normalized_hint for token in ("rma", "roughnessmetallicao")):
        return "mask", "rma"
    if any(token in normalized_hint for token in ("mra", "metallicroughnessao")):
        return "mask", "mra"
    if any(token in normalized_hint for token in ("arm", "aoroughnessmetallic")):
        return "mask", "arm"
    if "roughness" in normalized_hint:
        return "roughness", "roughness"
    if any(token in normalized_hint for token in ("specular", "gloss", "smoothness")):
        return "mask", "specular"
    if any(token in normalized_hint for token in ("metallic", "metalness")):
        return "mask", "metallic"
    if any(token in normalized_hint for token in ("ao", "occlusion")):
        return "mask", "ao"
    if "opacity" in normalized_hint or "alpha" in normalized_hint:
        return "mask", "opacity_mask"
    if "material" in normalized_hint and normalized_subtype in {"unknown", "mask"}:
        return "mask", "material_mask"
    return normalized_type, normalized_subtype


def _infer_model_preview_texture_slot(
    texture_path: str,
    *,
    semantic_hint: str = "",
    sidecar_texts: Sequence[str] = (),
) -> str:
    normalized_hint = re.sub(r"[^a-z0-9]+", "", str(semantic_hint or "").strip().lower())
    if normalized_hint:
        if "normal" in normalized_hint:
            return "normal"
        if any(token in normalized_hint for token in ("height", "displacement", "parallax", "pom", "ssdm", "bump")):
            return "height"
        if any(token in normalized_hint for token in ("material", "roughness", "metallic", "metalness", "specular", "ao", "occlusion", "mask")):
            return "material"
        if any(token in normalized_hint for token in ("basecolor", "overlaycolor", "diffuse", "albedo", "colortexture", "emissive")):
            return "base"
    texture_type, semantic_subtype, _confidence = _resolve_model_texture_semantics(
        texture_path,
        sidecar_texts=sidecar_texts,
    )
    normalized_type = str(texture_type or "").strip().lower()
    normalized_subtype = str(semantic_subtype or "").strip().lower()
    if normalized_type == "normal":
        return "normal"
    if normalized_type == "height" or normalized_subtype in {"displacement", "parallax_height", "height", "bump"}:
        return "height"
    if normalized_type in {"mask", "roughness", "vector"}:
        return "material"
    return "base"


def _model_texture_candidate_slot_priority(
    preview_slot: str,
    texture_path: str,
    *,
    sidecar_texts: Sequence[str] = (),
) -> Optional[Tuple[int, int]]:
    normalized_slot = str(preview_slot or "").strip().lower()
    if normalized_slot not in {"normal", "material", "height"}:
        return None

    texture_type, semantic_subtype, _confidence = _resolve_model_texture_semantics(
        texture_path,
        sidecar_texts=sidecar_texts,
    )
    normalized_type = str(texture_type or "").strip().lower()
    normalized_subtype = str(semantic_subtype or "").strip().lower()
    suffix_index = _match_model_texture_slot_family_suffix(texture_path, normalized_slot)
    suffix_priority = (
        len(_MODEL_TEXTURE_SUPPORT_FAMILY_SUFFIXES.get(normalized_slot, ())) - suffix_index
        if suffix_index >= 0
        else 0
    )

    if normalized_slot == "normal":
        if normalized_type == "normal":
            return (12, 3)
        if suffix_index >= 0:
            return (10, suffix_priority)
        return None

    if normalized_slot == "height":
        if normalized_type == "height" or normalized_subtype in {"displacement", "parallax_height", "height", "bump"}:
            return (12, 3)
        if suffix_index >= 0:
            return (10, suffix_priority)
        return None

    if normalized_slot == "material":
        if normalized_type in {"mask", "roughness", "vector"}:
            return (12, 3)
        if normalized_subtype in {"packed_mask", "specular", "metallic", "ao", "mask", "opacity_mask"}:
            return (11, 2)
        if suffix_index >= 0:
            return (10, suffix_priority)
        return None

    return None


def _infer_model_preview_normal_strength(
    *,
    base_texture_path: str = "",
    normal_texture_path: str = "",
    material_name: str = "",
    semantic_hint: str = "",
    prefer_stronger: bool = False,
) -> float:
    normalized_hint = str(semantic_hint or "").strip().lower().replace("_", "")
    combined = " ".join(
        part
        for part in (
            _normalize_model_texture_reference(base_texture_path),
            _normalize_model_texture_reference(normal_texture_path),
            str(material_name or "").strip().lower(),
            normalized_hint,
        )
        if part
    )

    strength = 0.36
    if prefer_stronger:
        strength += 0.08
    if normalized_hint in {"normaltexture", "basenormaltexture"}:
        strength += 0.06
    elif "detailnormal" in normalized_hint or "grimenormal" in normalized_hint:
        strength -= 0.05

    soft_tokens = (
        "wood",
        "plank",
        "timber",
        "fabric",
        "cloth",
        "rope",
        "leather",
        "skin",
        "paper",
        "parchment",
        "banner",
        "canvas",
        "fur",
        "hair",
    )
    hard_tokens = (
        "stone",
        "rock",
        "brick",
        "concrete",
        "cliff",
        "marble",
        "granite",
        "dungeon",
        "ancient",
        "wall",
        "masonry",
        "ruin",
    )
    medium_tokens = (
        "metal",
        "rust",
        "iron",
        "steel",
        "armor",
        "shield",
        "weapon",
    )

    if any(token in combined for token in soft_tokens):
        strength -= 0.04
    if any(token in combined for token in hard_tokens):
        strength += 0.14
    if any(token in combined for token in medium_tokens):
        strength += 0.08

    return max(0.22, min(0.72, strength))


def _set_model_preview_texture_slot(
    mesh: ModelPreviewMesh,
    *,
    slot: str,
    preview_path: str,
    texture_path: str,
    normal_strength: Optional[float] = None,
    semantic_type: str = "",
    semantic_subtype: str = "",
    packed_channels: Sequence[str] = (),
    flip_vertical: Optional[bool] = None,
) -> bool:
    normalized_slot = str(slot or "").strip().lower()
    preview_path_text = str(preview_path or "").strip()
    texture_path_text = str(texture_path or "").strip()
    if not preview_path_text:
        return False

    if normalized_slot == "normal":
        if not str(getattr(mesh, "preview_normal_texture_path", "") or "").strip():
            mesh.preview_normal_texture_path = preview_path_text
            mesh.preview_normal_texture_image = None
            mesh.preview_normal_texture_name = texture_path_text
            if normal_strength is not None:
                mesh.preview_normal_texture_strength = float(normal_strength)
            if texture_path_text and not str(getattr(mesh, "texture_name", "") or "").strip():
                mesh.texture_name = texture_path_text
            return True
        return False
    if normalized_slot == "material":
        if not str(getattr(mesh, "preview_material_texture_path", "") or "").strip():
            mesh.preview_material_texture_path = preview_path_text
            mesh.preview_material_texture_image = None
            mesh.preview_material_texture_name = texture_path_text
            mesh.preview_material_texture_type = str(semantic_type or "").strip().lower()
            mesh.preview_material_texture_subtype = str(semantic_subtype or "").strip().lower()
            mesh.preview_material_texture_packed_channels = tuple(
                str(channel or "").strip().lower()
                for channel in packed_channels
                if str(channel or "").strip()
            )
            return True
        return False
    if normalized_slot == "height":
        if not str(getattr(mesh, "preview_height_texture_path", "") or "").strip():
            mesh.preview_height_texture_path = preview_path_text
            mesh.preview_height_texture_image = None
            mesh.preview_height_texture_name = texture_path_text
            return True
        return False

    changed = False
    if not str(getattr(mesh, "preview_texture_path", "") or "").strip():
        mesh.preview_texture_path = preview_path_text
        mesh.preview_texture_image = None
        changed = True
    if texture_path_text:
        current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        if not current_texture_name or not current_texture_name.lower().endswith(".dds"):
            mesh.texture_name = texture_path_text
            changed = True
    if flip_vertical is not None:
        mesh.preview_texture_flip_vertical = bool(flip_vertical)
        changed = True
    return changed


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
    preferred_slot: str = "",
) -> List[Tuple[ArchiveEntry, Tuple[int, int]]]:
    reference_candidates = _iter_model_texture_reference_candidates(texture_name, material_name)
    if not reference_candidates:
        return []

    expanded_reference_candidates: List[str] = list(reference_candidates)
    if expand_family_candidates:
        seen_expanded = set(expanded_reference_candidates)
        for normalized_reference in reference_candidates:
            group_key = derive_texture_group_key(normalized_reference)
            for family_reference in _iter_model_texture_slot_family_reference_candidates(group_key, preferred_slot):
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
    preferred_slot: str = "",
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> Tuple[Optional[ArchiveEntry], str]:
    normalized_preferred_slot = str(preferred_slot or "").strip().lower()
    if expand_family_candidates is None:
        if normalized_preferred_slot in {"normal", "material", "height"}:
            expand_family_candidates = True
        else:
            expand_family_candidates = not _has_explicit_model_texture_reference(texture_name, material_name)
    scored_candidates = _collect_model_texture_archive_entry_candidates(
        source_entry,
        texture_name,
        material_name,
        texture_entries_by_normalized_path,
        texture_entries_by_basename,
        expand_family_candidates=expand_family_candidates,
        preferred_slot=normalized_preferred_slot,
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
    slot_filtered_out = False
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
        if normalized_preferred_slot in {"normal", "material", "height"}:
            semantic_priority = _model_texture_candidate_slot_priority(
                normalized_preferred_slot,
                candidate.path,
                sidecar_texts=sidecar_texts,
            )
            if semantic_priority is None:
                slot_filtered_out = True
                continue
        else:
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
        if normalized_preferred_slot in {"normal", "material", "height"} and slot_filtered_out:
            return None, "technical_only"
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


def _model_preview_sidecar_tint(binding: _ArchiveModelSidecarTextureBinding) -> Tuple[float, float, float]:
    tint = tuple(getattr(binding, "tint_color", ()) or ())
    if len(tint) < 3:
        tint = tuple(getattr(binding, "represent_color", ()) or ())
    if len(tint) >= 3:
        return (
            max(0.0, min(2.0, float(tint[0]))),
            max(0.0, min(2.0, float(tint[1]))),
            max(0.0, min(2.0, float(tint[2]))),
        )
    return ()


def _model_preview_sidecar_uv_scale(binding: _ArchiveModelSidecarTextureBinding) -> Tuple[float, float]:
    try:
        uv_scale = float(getattr(binding, "uv_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        uv_scale = 1.0
    uv_scale = max(0.05, min(64.0, uv_scale))
    if abs(uv_scale - 1.0) <= 1e-6:
        return ()
    return (uv_scale, uv_scale)


def _mesh_existing_base_is_sidecar_identity(
    mesh: ModelPreviewMesh,
    parsed_submesh: Optional[object],
    binding: _ArchiveModelSidecarTextureBinding,
) -> bool:
    sidecar_candidates = _iter_model_submesh_reference_candidates(
        str(getattr(binding, "submesh_name", "") or ""),
        str(getattr(binding, "part_name", "") or ""),
        str(getattr(binding, "material_name", "") or ""),
    )
    if not sidecar_candidates:
        return False
    sidecar_candidate_set = set(sidecar_candidates)
    mesh_candidates = _iter_model_submesh_reference_candidates(
        str(getattr(parsed_submesh, "name", "") or ""),
        str(getattr(parsed_submesh, "material", "") or ""),
        str(getattr(parsed_submesh, "texture", "") or ""),
        str(getattr(mesh, "material_name", "") or ""),
        str(getattr(mesh, "texture_name", "") or ""),
    )
    return any(candidate in sidecar_candidate_set for candidate in mesh_candidates)


def _apply_model_sidecar_base_preview(
    mesh: ModelPreviewMesh,
    *,
    texture_entry: ArchiveEntry,
    preview_path_text: str,
    binding: _ArchiveModelSidecarTextureBinding,
    force_unflipped_preview: bool,
    set_texture_name: bool,
) -> None:
    if str(getattr(mesh, "preview_texture_path", "") or "").strip() != preview_path_text:
        mesh.preview_texture_path = preview_path_text
        mesh.preview_texture_image = None
    if force_unflipped_preview:
        mesh.preview_texture_flip_vertical = False
    current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
    if set_texture_name or not current_texture_name or not current_texture_name.lower().endswith(".dds"):
        mesh.texture_name = texture_entry.path
    current_material_name = str(getattr(mesh, "material_name", "") or "").strip()
    sidecar_material_name = str(getattr(binding, "submesh_name", "") or "").strip()
    if sidecar_material_name and not current_material_name:
        mesh.material_name = sidecar_material_name
    mesh.preview_base_texture_source = str(getattr(binding, "sidecar_kind", "") or "sidecar").strip() or "sidecar"
    mesh.preview_sidecar_material_primitive = (
        str(getattr(binding, "material_name", "") or "").strip()
        or str(getattr(binding, "part_name", "") or "").strip()
        or sidecar_material_name
    )
    mesh.preview_sidecar_shader_family = str(getattr(binding, "shader_family", "") or "").strip()
    try:
        mesh.preview_texture_brightness = max(0.1, min(3.0, float(getattr(binding, "brightness", 1.0) or 1.0)))
    except (TypeError, ValueError):
        mesh.preview_texture_brightness = 1.0
    mesh.preview_texture_tint = _model_preview_sidecar_tint(binding)
    mesh.preview_texture_uv_scale = _model_preview_sidecar_uv_scale(binding)
    if (
        mesh.preview_texture_tint
        or mesh.preview_texture_uv_scale
        or abs(float(mesh.preview_texture_brightness or 1.0) - 1.0) > 1e-6
    ):
        mesh.preview_texture_approximation_note = "Sidecar tint, brightness, and UV scale are preview approximations."


def _attach_model_sidecar_texture_preview_paths(
    texconv_path: Optional[Path],
    source_entry: ArchiveEntry,
    model_preview: Optional[ModelPreviewData],
    *,
    parsed_mesh: Optional[object],
    sidecar_texture_bindings: Sequence[_ArchiveModelSidecarTextureBinding],
    visible_texture_mode: str = "mesh_base_first",
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
    fallback_only: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    if texconv_path is None or model_preview is None or not model_preview.meshes or not sidecar_texture_bindings:
        return []

    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    resolved_texconv_path = texconv_path.expanduser().resolve()
    normalized_visible_texture_mode = _normalize_model_visible_texture_mode(visible_texture_mode)
    allowed_visible_classes = set(_allowed_model_sidecar_visible_classes(normalized_visible_texture_mode))
    resolved_by_submesh: Dict[str, Tuple[Tuple[int, int, int, int, int], ArchiveEntry, str, str, _ArchiveModelSidecarTextureBinding]] = {}
    global_visible_bindings: List[Tuple[ArchiveEntry, str, str, _ArchiveModelSidecarTextureBinding]] = []
    fallback_visible_bindings: List[
        Tuple[Tuple[int, int, int, int, int], ArchiveEntry, str, str, _ArchiveModelSidecarTextureBinding]
    ] = []
    seen_fallback_binding_keys: set[Tuple[str, str, str]] = set()
    seen_global_binding_keys: set[Tuple[str, str]] = set()
    sidecar_paths: List[str] = []
    promoted_anonymous_fallback = False
    force_unflipped_preview = str(getattr(source_entry, "extension", "") or "").lower() == ".pac"

    for binding in sidecar_texture_bindings:
        raise_if_cancelled(stop_event)
        submesh_keys = _iter_model_sidecar_binding_submesh_keys(binding)
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
        binding_class = _classify_model_sidecar_visible_binding(binding.parameter_name, texture_entry.path)
        if binding_class not in allowed_visible_classes:
            continue
        priority = _model_texture_hint_priority(binding.parameter_name) or _model_texture_semantic_priority(
            texture_type,
            semantic_subtype,
        )
        candidate_key = (
            _model_sidecar_visible_class_priority(binding_class),
            priority[0],
            priority[1],
            confidence,
            -len(texture_entry.path),
        )
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
                    binding,
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
                        binding,
                    )
        else:
            global_key = (
                _normalize_model_texture_reference(texture_entry.path),
                str(binding.parameter_name or "").strip().lower(),
            )
            if global_key not in seen_global_binding_keys:
                seen_global_binding_keys.add(global_key)
                global_visible_bindings.append((texture_entry, binding.parameter_name, binding.submesh_name, binding))
        if binding.sidecar_path and binding.sidecar_path not in sidecar_paths:
            sidecar_paths.append(binding.sidecar_path)

    assigned_count = 0
    identity_override_count = 0
    unresolved_meshes: List[ModelPreviewMesh] = []
    for mesh_index, mesh in enumerate(model_preview.meshes):
        raise_if_cancelled(stop_event)
        existing_preview_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
        parsed_submesh = parsed_submeshes[mesh_index] if mesh_index < len(parsed_submeshes) else None
        candidate_keys = _iter_model_submesh_reference_candidates(
            str(getattr(parsed_submesh, "name", "") or ""),
            str(getattr(parsed_submesh, "material", "") or ""),
            str(getattr(parsed_submesh, "texture", "") or ""),
            str(getattr(mesh, "material_name", "") or ""),
            str(getattr(mesh, "texture_name", "") or ""),
        )
        best_match: Optional[Tuple[Tuple[int, int, int, int, int], ArchiveEntry, str, str, _ArchiveModelSidecarTextureBinding]] = None
        for candidate_key_text in candidate_keys:
            resolved = resolved_by_submesh.get(candidate_key_text)
            if resolved is None:
                continue
            if best_match is None or resolved[0] > best_match[0]:
                best_match = resolved
        if best_match is None:
            if not existing_preview_path:
                unresolved_meshes.append(mesh)
            continue
        _candidate_key, texture_entry, _parameter_name, submesh_name, binding = best_match
        if existing_preview_path and not _mesh_existing_base_is_sidecar_identity(mesh, parsed_submesh, binding):
            continue
        try:
            preview_path_text = _ensure_archive_model_texture_preview_path(
                resolved_texconv_path,
                texture_entry,
                stop_event=stop_event,
            )
            _apply_model_sidecar_base_preview(
                mesh,
                texture_entry=texture_entry,
                preview_path_text=preview_path_text,
                binding=binding,
                force_unflipped_preview=force_unflipped_preview,
                set_texture_name=bool(existing_preview_path),
            )
            if existing_preview_path and _normalize_model_texture_reference(existing_preview_path) != _normalize_model_texture_reference(preview_path_text):
                identity_override_count += 1
            assigned_count += 1
        except RunCancelled:
            raise
        except Exception:
            continue

    if not global_visible_bindings and unresolved_meshes and fallback_visible_bindings:
        unique_named_sidecar_submeshes = {
            _normalize_model_submesh_reference(submesh_name)
            for _candidate_key, _texture_entry, _parameter_name, submesh_name, _binding in fallback_visible_bindings
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
            _candidate_key, texture_entry, parameter_name, submesh_name, binding = fallback_visible_bindings[0]
            global_visible_bindings.append((texture_entry, parameter_name, submesh_name, binding))
            promoted_anonymous_fallback = True

    if global_visible_bindings and unresolved_meshes:
        if len(global_visible_bindings) == 1:
            texture_entry, _parameter_name, submesh_name, binding = global_visible_bindings[0]
            for mesh in unresolved_meshes:
                raise_if_cancelled(stop_event)
                if str(getattr(mesh, "preview_texture_path", "") or "").strip():
                    continue
                try:
                    preview_path_text = _ensure_archive_model_texture_preview_path(
                        resolved_texconv_path,
                        texture_entry,
                        stop_event=stop_event,
                    )
                    _apply_model_sidecar_base_preview(
                        mesh,
                        texture_entry=texture_entry,
                        preview_path_text=preview_path_text,
                        binding=binding,
                        force_unflipped_preview=force_unflipped_preview,
                        set_texture_name=False,
                    )
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
                texture_entry, _parameter_name, submesh_name, binding = global_visible_bindings[binding_index]
                binding_index += 1
                try:
                    preview_path_text = _ensure_archive_model_texture_preview_path(
                        resolved_texconv_path,
                        texture_entry,
                        stop_event=stop_event,
                    )
                    _apply_model_sidecar_base_preview(
                        mesh,
                        texture_entry=texture_entry,
                        preview_path_text=preview_path_text,
                        binding=binding,
                        force_unflipped_preview=force_unflipped_preview,
                        set_texture_name=False,
                    )
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
        (
            f"Applied {assigned_count:,} textured preview fallback binding(s) from companion material sidecar data{sidecar_suffix}."
            if fallback_only
            else f"Applied {assigned_count:,} textured preview binding(s) from companion material sidecar data{sidecar_suffix}."
        )
    ]
    if promoted_anonymous_fallback:
        info_lines.append(
            "Used a sidecar texture fallback because the recovered mesh preview did not preserve a reliable submesh/material name match."
        )
    if identity_override_count > 0:
        info_lines.append(
            f"Selected {identity_override_count:,} sidecar base texture preview(s) over embedded material primitive/identity name(s)."
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
    override_existing_base: bool = False,
    prefer_material_name_for_base: bool = False,
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
    override_count = 0
    unresolved_lookup_names: List[str] = []
    technical_skip_names: List[str] = []
    preview_failure_names: List[str] = []
    force_unflipped_preview = str(getattr(source_entry, "extension", "") or "").lower() == ".pac"

    for mesh in model_preview.meshes:
        raise_if_cancelled(stop_event)
        existing_preview_path = str(getattr(mesh, "preview_texture_path", "") or "").strip()
        if override_existing_base and str(getattr(mesh, "preview_base_texture_source", "") or "").strip().lower() in {
            "pami",
            "pac_xml",
            "sidecar",
            "pamlod_xml",
            "pam_xml",
        }:
            continue
        if existing_preview_path and not override_existing_base:
            resolved_count += 1
            sidecar_bound_count += 1
            continue
        texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        material_name = str(getattr(mesh, "material_name", "") or "").strip()
        lookup_texture_name = texture_name
        lookup_material_name = material_name
        if override_existing_base and prefer_material_name_for_base and material_name and not material_name.lower().endswith(".dds"):
            lookup_texture_name = ""
            lookup_material_name = material_name
        texture_label = lookup_texture_name or lookup_material_name
        if not texture_label:
            continue

        texture_entry, resolution_status = _resolve_model_texture_archive_entry(
            source_entry,
            lookup_texture_name,
            lookup_material_name,
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

        if str(getattr(mesh, "preview_texture_path", "") or "").strip() != preview_path_text:
            mesh.preview_texture_path = preview_path_text
            mesh.preview_texture_image = None
        if force_unflipped_preview:
            mesh.preview_texture_flip_vertical = False
        current_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        if override_existing_base or not current_texture_name or not current_texture_name.lower().endswith(".dds"):
            mesh.texture_name = texture_entry.path
        if not str(getattr(mesh, "preview_base_texture_source", "") or "").strip():
            mesh.preview_base_texture_source = "embedded mesh"
        if (
            existing_preview_path
            and override_existing_base
            and _normalize_model_texture_reference(existing_preview_path)
            != _normalize_model_texture_reference(preview_path_text)
        ):
            override_count += 1
        resolved_count += 1

    info_lines: List[str] = []
    if resolved_count > 0:
        if override_count > 0:
            info_lines.append(
                f"Corrected {override_count:,} mesh base texture preview(s) so embedded material names override sidecar overlay/detail fallback."
            )
        elif override_existing_base:
            pass
        elif sidecar_bound_count > 0 and sidecar_bound_count >= resolved_count:
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
    if unresolved_lookup_count > 0 and not override_existing_base:
        lookup_suffix = f" Examples: {', '.join(unresolved_lookup_names)}." if unresolved_lookup_names else ""
        info_lines.append(
            f"{unresolved_lookup_count:,} embedded material base name(s) had no direct visible DDS match; "
            f"sidecar layer bindings may still provide a preview fallback.{lookup_suffix}"
        )
    if technical_skip_count > 0 and not override_existing_base:
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


def _attach_model_support_texture_preview_paths(
    texconv_path: Optional[Path],
    source_entry: ArchiveEntry,
    model_preview: Optional[ModelPreviewData],
    *,
    parsed_mesh: Optional[object] = None,
    sidecar_texture_bindings: Sequence[_ArchiveModelSidecarTextureBinding] = (),
    texture_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    texture_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_texts_by_normalized_path: Optional[Dict[str, Tuple[str, ...]]] = None,
    sidecar_texts_by_basename: Optional[Dict[str, Tuple[str, ...]]] = None,
    stop_event: Optional[threading.Event] = None,
) -> List[str]:
    if texconv_path is None or model_preview is None or not model_preview.meshes:
        return []

    parsed_submeshes = _iter_parsed_model_submeshes(parsed_mesh)
    resolved_texconv_path = texconv_path.expanduser().resolve()
    preview_cache: Dict[str, str] = {}
    support_slots = ("normal", "material", "height")
    slot_labels = {
        "normal": "normal-map",
        "material": "material-mask",
        "height": "height/displacement",
    }
    exact_assigned_by_slot: Dict[str, int] = {slot: 0 for slot in support_slots}
    fallback_assigned_by_slot: Dict[str, int] = {slot: 0 for slot in support_slots}
    exact_examples: Dict[str, List[str]] = {slot: [] for slot in support_slots}
    fallback_examples: Dict[str, List[str]] = {slot: [] for slot in support_slots}
    exact_sidecar_paths: List[str] = []
    force_unflipped_preview = str(getattr(source_entry, "extension", "") or "").lower() == ".pac"
    slot_hints = (
        ("normal", "normal"),
        ("material", "material"),
        ("height", "height"),
    )

    def _lookup_sidecar_texts(texture_path: str) -> Tuple[str, ...]:
        normalized_path = normalize_texture_reference_for_sidecar_lookup(texture_path)
        if sidecar_texts_by_normalized_path is not None and normalized_path:
            sidecar_texts = tuple(sidecar_texts_by_normalized_path.get(normalized_path, ()))
            if sidecar_texts:
                return sidecar_texts
        if sidecar_texts_by_basename is not None:
            basename = PurePosixPath(texture_path.replace("\\", "/")).name.lower()
            if basename:
                return tuple(sidecar_texts_by_basename.get(basename, ()))
        return ()

    def _preview_path_for_entry(texture_entry: ArchiveEntry) -> str:
        cache_key = _normalize_model_texture_reference(texture_entry.path)
        preview_path_text = preview_cache.get(cache_key, "")
        if preview_path_text:
            return preview_path_text
        preview_path_text = _ensure_archive_model_texture_preview_path(
            resolved_texconv_path,
            texture_entry,
            stop_event=stop_event,
        )
        preview_cache[cache_key] = preview_path_text
        return preview_path_text

    def _record_slot_example(target: Dict[str, List[str]], slot_name: str, texture_path: str) -> None:
        examples = target[slot_name]
        basename = PurePosixPath(texture_path.replace("\\", "/")).name
        if basename and basename not in examples and len(examples) < 3:
            examples.append(basename)

    def _assign_support_slot(
        mesh: ModelPreviewMesh,
        slot_name: str,
        texture_entry: ArchiveEntry,
        *,
        semantic_hint: str,
    ) -> bool:
        preview_path_text = _preview_path_for_entry(texture_entry)
        semantic_type = ""
        semantic_subtype = ""
        packed_channels: Tuple[str, ...] = ()
        if slot_name == "material":
            sidecar_texts = _lookup_sidecar_texts(texture_entry.path)
            semantic_type, semantic_subtype, _confidence, packed_channels = _resolve_model_texture_semantic_details(
                texture_entry.path,
                sidecar_texts=sidecar_texts,
            )
            semantic_type, semantic_subtype = _refine_model_texture_semantic_from_hint(
                semantic_type,
                semantic_subtype,
                semantic_hint,
            )
        changed = _set_model_preview_texture_slot(
            mesh,
            slot=slot_name,
            preview_path=preview_path_text,
            texture_path=texture_entry.path,
            normal_strength=(
                _infer_model_preview_normal_strength(
                    base_texture_path=str(getattr(mesh, "texture_name", "") or "").strip(),
                    normal_texture_path=texture_entry.path,
                    material_name=str(getattr(mesh, "material_name", "") or "").strip(),
                    semantic_hint=semantic_hint,
                    prefer_stronger=False,
                )
                if slot_name == "normal"
                else None
            ),
            semantic_type=semantic_type,
            semantic_subtype=semantic_subtype,
            packed_channels=packed_channels,
        )
        if changed and force_unflipped_preview:
            mesh.preview_texture_flip_vertical = False
        return changed

    exact_resolved_by_submesh: Dict[Tuple[str, str], Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]] = {}
    exact_global_bindings: Dict[str, List[Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]]] = defaultdict(list)
    seen_exact_global_keys: set[Tuple[str, str, str]] = set()

    for binding in sidecar_texture_bindings:
        raise_if_cancelled(stop_event)
        parameter_name = str(binding.parameter_name or "").strip()
        slot_name = _infer_model_preview_texture_slot("", semantic_hint=parameter_name)
        if slot_name not in support_slots:
            continue
        submesh_keys = _iter_model_sidecar_binding_submesh_keys(binding)
        texture_entry, resolution_status = _resolve_model_texture_archive_entry(
            source_entry,
            binding.texture_path,
            binding.submesh_name,
            texture_entries_by_normalized_path,
            texture_entries_by_basename,
            semantic_hint=parameter_name,
            expand_family_candidates=False,
            allow_technical_match=True,
            preferred_slot=slot_name,
            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
            sidecar_texts_by_basename=sidecar_texts_by_basename,
        )
        if texture_entry is None or resolution_status != "resolved":
            continue
        sidecar_texts = _lookup_sidecar_texts(texture_entry.path)
        texture_type, semantic_subtype, confidence = _resolve_model_texture_semantics(
            texture_entry.path,
            sidecar_texts=sidecar_texts,
        )
        slot_priority = (
            _model_texture_slot_hint_priority(slot_name, parameter_name)
            or _model_texture_candidate_slot_priority(slot_name, texture_entry.path, sidecar_texts=sidecar_texts)
        )
        if slot_priority is None:
            continue
        candidate_key = (
            slot_priority[0],
            slot_priority[1],
            confidence,
            -len(texture_entry.path),
        )
        if submesh_keys:
            for submesh_key in submesh_keys:
                resolved_key = (slot_name, submesh_key)
                existing = exact_resolved_by_submesh.get(resolved_key)
                if existing is None or candidate_key > existing[0]:
                    exact_resolved_by_submesh[resolved_key] = (
                        candidate_key,
                        texture_entry,
                        parameter_name,
                        binding.submesh_name,
                    )
        else:
            global_key = (
                slot_name,
                _normalize_model_texture_reference(texture_entry.path),
                parameter_name.lower(),
            )
            if global_key not in seen_exact_global_keys:
                seen_exact_global_keys.add(global_key)
                exact_global_bindings[slot_name].append(
                    (
                        candidate_key,
                        texture_entry,
                        parameter_name,
                        binding.submesh_name,
                    )
                )
        if binding.sidecar_path and binding.sidecar_path not in exact_sidecar_paths:
            exact_sidecar_paths.append(binding.sidecar_path)

    for mesh_index, mesh in enumerate(model_preview.meshes):
        raise_if_cancelled(stop_event)
        parsed_submesh = parsed_submeshes[mesh_index] if mesh_index < len(parsed_submeshes) else None
        candidate_keys = _iter_model_submesh_reference_candidates(
            str(getattr(parsed_submesh, "name", "") or ""),
            str(getattr(parsed_submesh, "material", "") or ""),
            str(getattr(parsed_submesh, "texture", "") or ""),
            str(getattr(mesh, "material_name", "") or ""),
            str(getattr(mesh, "texture_name", "") or ""),
        )
        for slot_name in support_slots:
            existing_preview_path = str(getattr(mesh, f"preview_{slot_name}_texture_path", "") or "").strip()
            if existing_preview_path:
                continue
            best_match: Optional[Tuple[Tuple[int, int, int, int], ArchiveEntry, str, str]] = None
            for candidate_key_text in candidate_keys:
                resolved = exact_resolved_by_submesh.get((slot_name, candidate_key_text))
                if resolved is None:
                    continue
                if best_match is None or resolved[0] > best_match[0]:
                    best_match = resolved
            if best_match is None:
                continue
            _candidate_key, texture_entry, parameter_name, _submesh_name = best_match
            try:
                if _assign_support_slot(mesh, slot_name, texture_entry, semantic_hint=parameter_name):
                    exact_assigned_by_slot[slot_name] += 1
                    _record_slot_example(exact_examples, slot_name, texture_entry.path)
            except RunCancelled:
                raise
            except Exception:
                continue

    for slot_name in support_slots:
        global_bindings = exact_global_bindings.get(slot_name, [])
        if not global_bindings:
            continue
        global_bindings.sort(key=lambda item: item[0], reverse=True)
        unresolved_meshes = [
            mesh
            for mesh in model_preview.meshes
            if not str(getattr(mesh, f"preview_{slot_name}_texture_path", "") or "").strip()
        ]
        if not unresolved_meshes:
            continue
        if len(global_bindings) == 1:
            _candidate_key, texture_entry, parameter_name, _submesh_name = global_bindings[0]
            for mesh in unresolved_meshes:
                raise_if_cancelled(stop_event)
                try:
                    if _assign_support_slot(mesh, slot_name, texture_entry, semantic_hint=parameter_name):
                        exact_assigned_by_slot[slot_name] += 1
                        _record_slot_example(exact_examples, slot_name, texture_entry.path)
                except RunCancelled:
                    raise
                except Exception:
                    continue
        else:
            binding_index = 0
            for mesh in unresolved_meshes:
                raise_if_cancelled(stop_event)
                if binding_index >= len(global_bindings):
                    break
                _candidate_key, texture_entry, parameter_name, _submesh_name = global_bindings[binding_index]
                binding_index += 1
                try:
                    if _assign_support_slot(mesh, slot_name, texture_entry, semantic_hint=parameter_name):
                        exact_assigned_by_slot[slot_name] += 1
                        _record_slot_example(exact_examples, slot_name, texture_entry.path)
                except RunCancelled:
                    raise
                except Exception:
                    continue

    for mesh in model_preview.meshes:
        raise_if_cancelled(stop_event)
        reference_texture_name = str(getattr(mesh, "texture_name", "") or "").strip()
        reference_material_name = str(getattr(mesh, "material_name", "") or "").strip()
        if not reference_texture_name and not reference_material_name:
            continue
        for slot_name, semantic_hint in slot_hints:
            existing_preview_path = str(getattr(mesh, f"preview_{slot_name}_texture_path", "") or "").strip()
            if existing_preview_path:
                continue
            texture_entry, resolution_status = _resolve_model_texture_archive_entry(
                source_entry,
                reference_texture_name,
                reference_material_name,
                texture_entries_by_normalized_path,
                texture_entries_by_basename,
                semantic_hint=semantic_hint,
                allow_technical_match=True,
                preferred_slot=slot_name,
                sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                sidecar_texts_by_basename=sidecar_texts_by_basename,
            )
            if texture_entry is None or resolution_status != "resolved":
                continue
            try:
                if _assign_support_slot(mesh, slot_name, texture_entry, semantic_hint=semantic_hint):
                    fallback_assigned_by_slot[slot_name] += 1
                    _record_slot_example(fallback_examples, slot_name, texture_entry.path)
            except RunCancelled:
                raise
            except Exception:
                continue

    info_lines: List[str] = []
    exact_total = sum(exact_assigned_by_slot.values())
    fallback_total = sum(fallback_assigned_by_slot.values())
    if exact_total > 0:
        sidecar_suffix = f" from {', '.join(exact_sidecar_paths[:2])}" if exact_sidecar_paths else ""
        if len(exact_sidecar_paths) > 2:
            sidecar_suffix += " ..."
        info_lines.append(
            f"Applied {exact_total:,} exact high-quality support-map binding(s) from companion material sidecar data{sidecar_suffix}."
        )
        for slot_name in support_slots:
            count = exact_assigned_by_slot[slot_name]
            if count <= 0:
                continue
            suffix = f" Examples: {', '.join(exact_examples[slot_name])}." if exact_examples[slot_name] else ""
            info_lines.append(
                f"Exact sidecar {slot_labels[slot_name]} bindings: {count:,}.{suffix}"
            )
    if fallback_total > 0:
        info_lines.append(
            f"Applied {fallback_total:,} semantic sibling high-quality support-map binding(s) using slot-correct family fallback."
        )
        for slot_name in support_slots:
            count = fallback_assigned_by_slot[slot_name]
            if count <= 0:
                continue
            suffix = f" Examples: {', '.join(fallback_examples[slot_name])}." if fallback_examples[slot_name] else ""
            info_lines.append(
                f"Semantic sibling {slot_labels[slot_name]} bindings: {count:,}.{suffix}"
            )
    if exact_total <= 0 and fallback_total <= 0:
        has_textured_mesh = any(
            str(getattr(mesh, "texture_name", "") or "").strip()
            or str(getattr(mesh, "preview_texture_path", "") or "").strip()
            for mesh in model_preview.meshes
        )
        if has_textured_mesh:
            info_lines.append(
                "No usable high-quality support maps were resolved from exact sidecar bindings or semantic sibling fallback. The preview remains base-texture only."
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
    basename = PurePosixPath(entry.path.replace("\\", "/")).name.lower()
    if extension == ".pam":
        return "Companion PAM"
    if extension == ".pamlod":
        return "Companion PAMLOD"
    if extension == ".pac":
        return "Companion PAC"
    if extension == ".pab":
        return "Companion PAB"
    if extension == ".pabc":
        return "Skeleton Variation"
    if extension == ".papr":
        return "Animation Constraint"
    if "prefabdata" in basename or extension == ".prefabdata_xml":
        return "Prefab Metadata"
    if extension == ".pami":
        return "Material Variant Sidecar"
    if _is_material_sidecar_extension(extension, basename):
        return "Material Sidecar"
    if extension == ".xml":
        return "Companion XML"
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


def _texture_reference_relation_metadata(
    source_entry: ArchiveEntry,
    reference_name: str,
    resolved_entry: Optional[ArchiveEntry],
    *,
    semantic_hint: str = "",
) -> Tuple[str, str]:
    if not isinstance(resolved_entry, ArchiveEntry):
        return (
            RelationConfidence.AUTHORITATIVE.value if semantic_hint else RelationConfidence.DERIVED_SAME_STEM.value,
            "Sidecar texture binding" if semantic_hint else "Resolved texture family",
        )
    normalized_reference = normalize_texture_reference_for_sidecar_lookup(reference_name)
    normalized_resolved = normalize_texture_reference_for_sidecar_lookup(resolved_entry.path)
    if normalized_reference and normalized_reference == normalized_resolved:
        return RelationConfidence.EXACT_PATH.value, "Exact archive path"
    if (
        normalized_reference
        and normalized_resolved
        and PurePosixPath(normalized_reference).name == PurePosixPath(normalized_resolved).name
        and source_entry.pamt_path.parent != resolved_entry.pamt_path.parent
    ):
        return RelationConfidence.CROSS_PACKAGE.value, "Cross-package texture reference"
    if normalized_reference and normalized_resolved and normalized_reference.lstrip("/") == normalized_resolved.lstrip("/"):
        return RelationConfidence.PATH_NORMALIZED.value, "Path-normalized texture reference"
    if semantic_hint:
        return RelationConfidence.AUTHORITATIVE.value, "Sidecar texture binding"
    return RelationConfidence.DERIVED_SAME_STEM.value, "Resolved texture family"


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

    references: Dict[Tuple[str, ...], ArchiveModelTextureReference] = {}
    ordered_keys: List[Tuple[str, ...]] = []

    for related_entry in related_companion_entries:
        related_key = ("sidecar", _normalize_model_texture_reference(related_entry.path))
        if related_key in references:
            continue
        relation_kind, relation_group, relation_confidence, relation_reason = _build_archive_relation_metadata(
            source_entry,
            resolved_entry=related_entry,
        )
        references[related_key] = ArchiveModelTextureReference(
            reference_name=PurePosixPath(related_entry.path.replace("\\", "/")).name,
            semantic_label=_describe_model_related_file_label(related_entry),
            resolution_status="resolved",
            resolved_archive_path=related_entry.path,
            resolved_package_label=related_entry.package_label,
            resolved_entry=related_entry,
            usage_count=1,
            reference_kind=relation_kind,
            relation_group=relation_group,
            relation_reason=relation_reason,
            relation_confidence=relation_confidence,
        )
        ordered_keys.append(related_key)

    candidates: List[Tuple[str, str, str, str, Optional[object]]] = []
    seen_candidate_keys: set[Tuple[str, str, str]] = set()
    for binding in sidecar_texture_references:
        texture_name = str(binding.texture_path or "").strip()
        material_name = str(
            getattr(binding, "part_name", "")
            or getattr(binding, "material_name", "")
            or binding.submesh_name
            or binding.parameter_name
            or ""
        ).strip()
        semantic_hint = str(binding.parameter_name or "").strip()
        key = (
            _normalize_model_texture_reference(texture_name),
            _normalize_model_texture_reference(material_name),
            str(semantic_hint or "").strip().lower(),
        )
        if not texture_name or key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidates.append((texture_name, material_name, "", semantic_hint, binding))
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
                None,
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
        candidates.append((texture_name, material_name, "", "", None))
    for raw_reference in binary_texture_references:
        texture_name = str(raw_reference or "").strip()
        if not texture_name:
            continue
        key = (_normalize_model_texture_reference(texture_name), "", "")
        if key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        candidates.append((texture_name, "", "", "", None))

    for texture_name, material_name, preview_texture_path, semantic_hint, sidecar_binding in candidates:
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
        if sidecar_binding is not None:
            key = (
                "texture",
                reference_key_value,
                _normalize_model_texture_reference(material_name),
                str(semantic_hint or "").strip().lower(),
                str(getattr(sidecar_binding, "sidecar_kind", "") or "").strip().lower(),
            )
        else:
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
        sidecar_kind = str(getattr(sidecar_binding, "sidecar_kind", "") or "").strip()
        linked_mesh_path = str(getattr(sidecar_binding, "linked_mesh_path", "") or "").strip()
        part_name = str(getattr(sidecar_binding, "part_name", "") or "").strip()
        shader_family = str(getattr(sidecar_binding, "shader_family", "") or "").strip()
        texture_role = str(getattr(sidecar_binding, "texture_role", "") or "").strip()
        visualization_state = str(getattr(sidecar_binding, "visualization_state", "") or "").strip()
        resolved_package_label = texture_entry.package_label if texture_entry is not None else ""
        relation_confidence, relation_reason = _texture_reference_relation_metadata(
            source_entry,
            reference_name,
            texture_entry,
            semantic_hint=semantic_hint,
        )
        existing = references.get(key)
        if existing is None:
            references[key] = ArchiveModelTextureReference(
                reference_name=reference_name,
                material_name=material_name,
                semantic_label=semantic_label,
                semantic_hint=semantic_hint,
                sidecar_parameter_name=semantic_hint,
                sidecar_kind=sidecar_kind,
                linked_mesh_path=linked_mesh_path,
                part_name=part_name,
                shader_family=shader_family,
                texture_role=texture_role,
                visualization_state=visualization_state,
                sidecar_texts=sidecar_texts,
                resolution_status=resolution_status,
                resolved_archive_path=resolved_archive_path,
                resolved_package_label=resolved_package_label,
                resolved_entry=texture_entry,
                preview_texture_path=preview_texture_path,
                usage_count=1,
                reference_kind="texture",
                relation_group="Textures",
                relation_reason=relation_reason,
                relation_confidence=relation_confidence,
            )
            ordered_keys.append(key)
            continue

        existing.usage_count += 1
        if material_name and not existing.material_name:
            existing.material_name = material_name
        if preview_texture_path and not existing.preview_texture_path:
            existing.preview_texture_path = preview_texture_path
        if sidecar_kind and not existing.sidecar_kind:
            existing.sidecar_kind = sidecar_kind
        if linked_mesh_path and not existing.linked_mesh_path:
            existing.linked_mesh_path = linked_mesh_path
        if part_name and not existing.part_name:
            existing.part_name = part_name
        if shader_family and not existing.shader_family:
            existing.shader_family = shader_family
        if texture_role and not existing.texture_role:
            existing.texture_role = texture_role
        if visualization_state and not existing.visualization_state:
            existing.visualization_state = visualization_state
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
            if not existing.sidecar_parameter_name:
                existing.sidecar_parameter_name = semantic_hint
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


def _looks_like_structured_asset_reference(value: str) -> bool:
    raw_text = str(value or "").strip().strip("\x00")
    if len(raw_text) < 3 or len(raw_text) > 255:
        return False
    normalized_text = raw_text.replace("\\", "/")
    if normalized_text.startswith("/") or normalized_text.endswith("/"):
        return False
    if "//" in normalized_text:
        return False
    suffix = PurePosixPath(normalized_text).suffix.lower()
    if suffix not in _STRUCTURED_BINARY_ASSET_REFERENCE_EXTENSIONS:
        return False
    segments = normalized_text.split("/")
    if not segments:
        return False
    for segment in segments:
        if not segment or not _STRUCTURED_BINARY_ASSET_SEGMENT_RE.fullmatch(segment):
            return False
    return any(character.isalpha() for character in normalized_text)


def _extract_binary_asset_references(
    data: bytes,
    *,
    sample_limit: int = 262_144,
    max_references: int = 64,
) -> List[str]:
    references: List[str] = []
    seen: set[str] = set()
    for text in extract_binary_strings(data, sample_limit=sample_limit, max_strings=max(max_references * 6, 96)):
        for match in _STRUCTURED_BINARY_ASSET_TOKEN_RE.finditer(text):
            raw_text = str(match.group(0) or "").strip().strip("\x00")
            if not _looks_like_structured_asset_reference(raw_text):
                continue
            raw_text = raw_text.replace("\\", "/")
            normalized = _normalize_model_texture_reference(raw_text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            references.append(raw_text)
            if len(references) >= max_references:
                return references
    return references


def _extract_text_asset_references(
    text: str,
    *,
    sidecar_path: str = "",
    max_references: int = 96,
) -> List[str]:
    references: List[str] = []
    seen: set[str] = set()

    def add_reference(raw_value: str) -> None:
        raw_text = str(raw_value or "").strip().strip("\x00").replace("\\", "/")
        if not _looks_like_structured_asset_reference(raw_text):
            return
        normalized = _normalize_model_texture_reference(raw_text)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        references.append(raw_text)

    for binding in parse_texture_sidecar_bindings(text, sidecar_path=sidecar_path):
        add_reference(binding.texture_path)
        if len(references) >= max_references:
            return references

    for match in _STRUCTURED_BINARY_ASSET_TOKEN_RE.finditer(text):
        add_reference(str(match.group(0) or ""))
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
    reference_basename = PurePosixPath(
        str(getattr(resolved_entry, "path", "") or reference_name).replace("\\", "/")
    ).name.lower()
    extension = str(getattr(resolved_entry, "extension", "") or PurePosixPath(reference_name.replace("\\", "/")).suffix).strip().lower()
    if extension == ".dds":
        semantic_label = _describe_model_texture_semantic_label(reference_name)
        return semantic_label or "Texture / DDS"
    if "prefabdata" in reference_basename or extension == ".prefabdata_xml":
        return "Prefab Metadata"
    if extension == ".pami":
        return "Material Variant Sidecar"
    if _is_material_sidecar_extension(extension, reference_basename):
        return "Material Sidecar"
    if extension == ".xml":
        return "Related XML"
    if extension == ".meshinfo":
        return "Related MeshInfo"
    if extension == ".hkx":
        return "Related HKX"
    if extension == ".pab":
        return "Related PAB"
    if extension == ".pabc":
        return "Skeleton Variation"
    if extension == ".papr":
        return "Animation Constraint"
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
        relation_kind, relation_group, relation_confidence, relation_reason = _build_archive_relation_metadata(
            source_entry,
            resolved_entry=companion_entry,
            authoritative=(
                str(source_entry.extension or "").strip().lower() == ".dds"
                and _is_material_sidecar_extension(
                    str(companion_entry.extension or "").strip().lower(),
                    PurePosixPath(companion_entry.path.replace("\\", "/")).name.lower(),
                )
            ),
            authoritative_reason="Sidecar binding reference",
        )
        references[key] = ArchiveModelTextureReference(
            reference_name=PurePosixPath(companion_entry.path.replace("\\", "/")).name,
            semantic_label=_describe_model_related_file_label(companion_entry),
            resolution_status="resolved",
            resolved_archive_path=companion_entry.path,
            resolved_package_label=companion_entry.package_label,
            resolved_entry=companion_entry,
            usage_count=1,
            reference_kind=relation_kind,
            relation_group=relation_group,
            relation_reason=relation_reason,
            relation_confidence=relation_confidence,
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
            authoritative = bool(isinstance(resolved_entry, ArchiveEntry) or "/" in reference_name or "." in PurePosixPath(reference_name).name)
            relation_kind, relation_group, relation_confidence, relation_reason = _build_archive_relation_metadata(
                source_entry,
                reference_name=reference_name,
                resolved_entry=resolved_entry if isinstance(resolved_entry, ArchiveEntry) else None,
                authoritative=authoritative,
                authoritative_reason="Explicit path reference",
            )
            references[key] = ArchiveModelTextureReference(
                reference_name=reference_name,
                semantic_label=_describe_generic_related_reference_label(reference_name, resolved_entry),
                resolution_status="resolved" if isinstance(resolved_entry, ArchiveEntry) else "missing",
                resolved_archive_path=resolved_entry.path if isinstance(resolved_entry, ArchiveEntry) else "",
                resolved_package_label=resolved_entry.package_label if isinstance(resolved_entry, ArchiveEntry) else "",
                resolved_entry=resolved_entry if isinstance(resolved_entry, ArchiveEntry) else None,
                usage_count=1,
                reference_kind=relation_kind,
                relation_group=relation_group,
                relation_reason=relation_reason,
                relation_confidence=relation_confidence,
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


def build_archive_asset_family_graph(
    source_entry: ArchiveEntry,
    references: Sequence[ArchiveModelTextureReference],
) -> AssetFamilyGraph:
    grouped_paths: Dict[str, List[str]] = defaultdict(list)
    relations: List[AssetRelation] = []
    member_paths: List[str] = []
    seen_members: set[str] = set()

    def add_member(raw_value: str) -> None:
        normalized = str(raw_value or "").strip().replace("\\", "/")
        if not normalized or normalized in seen_members:
            return
        seen_members.add(normalized)
        member_paths.append(normalized)

    add_member(source_entry.path)
    for reference in references:
        relation_group = str(getattr(reference, "relation_group", "") or "").strip() or "Metadata / Other"
        target_path = str(getattr(reference, "resolved_archive_path", "") or "").strip()
        if not target_path:
            target_path = str(getattr(reference, "reference_name", "") or "").strip().replace("\\", "/")
        if not target_path:
            continue
        add_member(target_path)
        if target_path not in grouped_paths[relation_group]:
            grouped_paths[relation_group].append(target_path)
        relations.append(
            AssetRelation(
                source_path=source_entry.path,
                target_path=target_path,
                relation_kind=str(getattr(reference, "reference_kind", "") or _relation_kind_for_entry(getattr(reference, "resolved_entry", None))),
                confidence=str(getattr(reference, "relation_confidence", "") or RelationConfidence.DERIVED_SAME_STEM.value),
                role_label=str(getattr(reference, "semantic_label", "") or "").strip(),
                reason=str(getattr(reference, "relation_reason", "") or "").strip(),
                source_entry=source_entry,
                target_entry=getattr(reference, "resolved_entry", None),
                semantic_label=str(getattr(reference, "semantic_label", "") or "").strip(),
                semantic_hint=str(getattr(reference, "semantic_hint", "") or "").strip(),
                sidecar_parameter_name=str(getattr(reference, "sidecar_parameter_name", "") or "").strip(),
                material_name=str(getattr(reference, "material_name", "") or "").strip(),
                package_label=str(getattr(reference, "resolved_package_label", "") or "").strip(),
            )
        )
    return AssetFamilyGraph(
        root_path=source_entry.path,
        family_key=PurePosixPath(source_entry.path.replace("\\", "/")).stem,
        members=tuple(member_paths),
        relations=tuple(relations),
        grouped_paths={key: tuple(value) for key, value in grouped_paths.items()},
    )


def _find_archive_texture_family_entries(
    source_entry: ArchiveEntry,
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]],
) -> Tuple[ArchiveEntry, ...]:
    if archive_entries_by_normalized_path is None:
        return ()
    extension = str(source_entry.extension or "").strip().lower()
    normalized_path = normalize_texture_reference_for_sidecar_lookup(source_entry.path)
    if extension != ".dds" or not normalized_path:
        return ()

    group_key = derive_texture_group_key(normalized_path)
    if not group_key:
        return ()
    if "/" in group_key:
        folder, family = group_key.rsplit("/", 1)
    else:
        folder, family = "", group_key
    if not family:
        return ()

    candidates: List[ArchiveEntry] = []
    seen_paths: set[str] = set()
    source_normalized = _normalize_model_texture_reference(source_entry.path)
    for suffix in _ARCHIVE_TEXTURE_FAMILY_SUFFIXES:
        candidate_path = f"{folder}/{family}{suffix}.dds" if folder else f"{family}{suffix}.dds"
        normalized_candidate_path = _normalize_model_texture_reference(candidate_path)
        for candidate in archive_entries_by_normalized_path.get(normalized_candidate_path, ()):
            normalized_candidate = _normalize_model_texture_reference(candidate.path)
            if normalized_candidate in seen_paths or normalized_candidate == source_normalized:
                continue
            seen_paths.add(normalized_candidate)
            candidates.append(candidate)

    if not candidates:
        return ()
    candidates.sort(key=lambda candidate: _score_model_related_entry_candidate(source_entry, candidate), reverse=True)
    return tuple(candidates[:16])


def _find_archive_texture_referencing_sidecar_entries(
    source_entry: ArchiveEntry,
    *,
    sidecar_entries_by_texture_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_entries_by_texture_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveEntry, ...]:
    normalized_path = normalize_texture_reference_for_sidecar_lookup(source_entry.path)
    if not normalized_path:
        return ()
    basename = PurePosixPath(normalized_path).name
    candidates: List[ArchiveEntry] = []
    seen_paths: set[str] = set()

    def add_candidate(entry: ArchiveEntry) -> None:
        normalized_candidate = _normalize_model_texture_reference(entry.path)
        if not normalized_candidate or normalized_candidate == _normalize_model_texture_reference(source_entry.path):
            return
        if normalized_candidate in seen_paths:
            return
        seen_paths.add(normalized_candidate)
        candidates.append(entry)

    if sidecar_entries_by_texture_path is not None:
        for candidate in sidecar_entries_by_texture_path.get(normalized_path, ()):
            add_candidate(candidate)
    if sidecar_entries_by_texture_basename is not None and basename:
        for candidate in sidecar_entries_by_texture_basename.get(basename, ()):
            add_candidate(candidate)
    return tuple(candidates)


def _collect_archive_texture_sidecar_texts_from_entries(
    sidecar_entries: Sequence[ArchiveEntry],
    *,
    limit: int = 6,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[str, ...]:
    texts: List[str] = []
    seen_texts: set[str] = set()
    for sidecar_entry in sidecar_entries:
        raise_if_cancelled(stop_event)
        try:
            raw_data, _decompressed, _note = read_archive_entry_data(sidecar_entry, stop_event=stop_event)
        except Exception:
            continue
        text = str(try_decode_text_like_archive_data(raw_data) or "").strip()
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        texts.append(text)
        if len(texts) >= limit:
            break
    return tuple(texts)


def build_archive_entry_related_references(
    source_entry: ArchiveEntry,
    *,
    text: str = "",
    binary_data: bytes = b"",
    explicit_reference_names: Sequence[str] = (),
    companion_entries: Sequence[ArchiveEntry] = (),
    archive_entries_by_normalized_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    archive_entries_by_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_entries_by_texture_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_entries_by_texture_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveModelTextureReference, ...]:
    combined_reference_names: List[str] = []
    seen_reference_names: set[str] = set()

    def add_reference_name(raw_value: str) -> None:
        normalized = _normalize_model_texture_reference(raw_value)
        if not normalized or normalized in seen_reference_names:
            return
        seen_reference_names.add(normalized)
        combined_reference_names.append(str(raw_value or "").strip().replace("\\", "/"))

    for reference_name in explicit_reference_names:
        add_reference_name(reference_name)
    if text:
        for reference_name in _extract_text_asset_references(text, sidecar_path=source_entry.path):
            add_reference_name(reference_name)
    elif binary_data:
        for reference_name in _extract_binary_asset_references(binary_data, sample_limit=262_144, max_references=64):
            add_reference_name(reference_name)

    combined_companion_entries: List[ArchiveEntry] = []
    seen_companion_paths: set[str] = set()

    def add_companion_entry(candidate: ArchiveEntry) -> None:
        normalized_candidate = _normalize_model_texture_reference(candidate.path)
        if not normalized_candidate or normalized_candidate == _normalize_model_texture_reference(source_entry.path):
            return
        if normalized_candidate in seen_companion_paths:
            return
        seen_companion_paths.add(normalized_candidate)
        combined_companion_entries.append(candidate)

    for candidate in companion_entries:
        add_companion_entry(candidate)
    for candidate in _find_archive_model_related_entries(source_entry, archive_entries_by_basename):
        add_companion_entry(candidate)
    for candidate in _find_archive_texture_family_entries(source_entry, archive_entries_by_normalized_path):
        add_companion_entry(candidate)
    if str(source_entry.extension or "").strip().lower() == ".dds":
        for candidate in _find_archive_texture_referencing_sidecar_entries(
            source_entry,
            sidecar_entries_by_texture_path=sidecar_entries_by_texture_path,
            sidecar_entries_by_texture_basename=sidecar_entries_by_texture_basename,
        ):
            add_companion_entry(candidate)
            for related_candidate in _find_archive_model_related_entries(candidate, archive_entries_by_basename):
                add_companion_entry(related_candidate)

    return build_archive_related_file_references(
        source_entry,
        explicit_reference_names=combined_reference_names,
        companion_entries=combined_companion_entries,
        archive_entries_by_normalized_path=archive_entries_by_normalized_path,
        archive_entries_by_basename=archive_entries_by_basename,
    )


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
    strings_preview = build_binary_strings_preview(data, sample_limit=65_536, max_strings=32)
    header_preview = format_binary_header_preview(data)
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
    elif normalized_extension == ".motionblending":
        title = "Motion blending inspector"
        metadata_label = "Motion Blending"
    else:
        title = f"{normalized_extension.lstrip('.').upper()} structured inspector"
        metadata_label = "Structured Binary"

    lines = [f"{title} for {virtual_path}", "", "Summary:"]
    lines.append(f"- Field-like entries: {len(field_names):,}")
    lines.append(f"- Readable strings: {len(strings):,}")
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
    if strings_preview:
        lines.extend(["", strings_preview])
    else:
        lines.extend(["", "Readable strings:", "  None detected in the preview sample."])
    lines.extend(["", "Binary header preview:", header_preview])

    detail_lines = [
        f"Detected {len(field_names):,} field-like identifier(s) from the preview sample.",
    ]
    if markers:
        detail_lines.append(f"Detected structured marker(s): {', '.join(markers)}.")
    if not field_names and not markers and not strings:
        detail_lines.append("No readable strings or structured markers were detected, so the preview falls back to raw header bytes.")
    if asset_references:
        detail_lines.append(f"Detected {len(asset_references):,} related asset reference(s).")
    if normalized_extension == ".paa":
        detail_lines.append("This inspector summarizes animation-side metadata and readable markers. Real animation playback is not implemented yet.")
    elif normalized_extension in {".pae", ".paem"}:
        detail_lines.append("This inspector summarizes effect/emitter-side metadata and readable markers. Real particle or timeline playback is not implemented yet.")
    elif normalized_extension == ".motionblending":
        detail_lines.append("This inspector summarizes motion/blend references and readable markers. Playback or editing is not implemented yet.")

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
    sidecar_entries_by_texture_path: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    sidecar_entries_by_texture_basename: Optional[Dict[str, Sequence[ArchiveEntry]]] = None,
    include_loose_preview_assets: bool = True,
    semantic_sidecar_texts: Sequence[str] = (),
    visible_texture_mode: str = "mesh_base_first",
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
    normalized_visible_texture_mode = _normalize_model_visible_texture_mode(visible_texture_mode)
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
            referencing_sidecar_entries = _find_archive_texture_referencing_sidecar_entries(
                entry,
                sidecar_entries_by_texture_path=sidecar_entries_by_texture_path,
                sidecar_entries_by_texture_basename=sidecar_entries_by_texture_basename,
            )
            combined_semantic_sidecar_texts: List[str] = [
                str(text or "").strip()
                for text in semantic_sidecar_texts
                if str(text or "").strip()
            ]
            for sidecar_text in _collect_archive_texture_sidecar_texts_from_entries(
                referencing_sidecar_entries,
                stop_event=stop_event,
            ):
                if sidecar_text not in combined_semantic_sidecar_texts:
                    combined_semantic_sidecar_texts.append(sidecar_text)
            related_references = build_archive_entry_related_references(
                entry,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
                sidecar_entries_by_texture_path=sidecar_entries_by_texture_path,
                sidecar_entries_by_texture_basename=sidecar_entries_by_texture_basename,
                companion_entries=referencing_sidecar_entries,
            )
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
                        sidecar_texts=tuple(combined_semantic_sidecar_texts),
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
            pathc_lookup_detail = build_archive_pathc_lookup_detail_for_entry(entry)
            if pathc_lookup_detail:
                extra_detail_parts.append(pathc_lookup_detail)
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
                                "Set texconv.exe under Settings > Paths to enable DDS image previews.",
                                *extra_detail_parts,
                            ]
                            if part
                        ),
                    ),
                    preferred_view="info",
                    warning_badge=warning_badge,
                    warning_text=warning_text,
                    model_texture_references=related_references,
                    asset_family_graph=build_archive_asset_family_graph(entry, related_references),
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
                model_texture_references=related_references,
                asset_family_graph=build_archive_asset_family_graph(entry, related_references),
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
            related_references = build_archive_entry_related_references(
                entry,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
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
                model_texture_references=related_references,
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

        if extension == ".pathc":
            pathc_preview = build_archive_pathc_preview(data, entry.path)
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(pathc_preview.detail_lines),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | {pathc_preview.metadata_label}",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=pathc_preview.preview_text,
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
                asset_family_graph=build_archive_asset_family_graph(entry, related_references),
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
                asset_family_graph=build_archive_asset_family_graph(entry, meshinfo_preview.related_references),
                preferred_view="text",
                loose_file_path=loose_file_path,
                loose_preview_image_path=loose_preview_image_path,
                loose_preview_media_path=loose_preview_media_path,
                loose_preview_media_kind=loose_preview_media_kind,
                loose_preview_title=loose_preview_title,
                loose_preview_metadata_summary=loose_preview_metadata_summary,
                loose_preview_detail_text=loose_preview_detail_text,
            )

        if extension in {".paa", ".pae", ".paem", ".motionblending"}:
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
                asset_family_graph=build_archive_asset_family_graph(entry, structured_preview.related_references),
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
            related_references = build_archive_entry_related_references(
                entry,
                binary_data=data,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
            detail_extra = "\n\n".join(
                part
                for part in [
                    ("Archive entry uses non-DDS Partial storage; preview is based on raw stored bytes." if "PartialRaw" in note_flags else ""),
                    ("Decrypted via deterministic ChaCha20 filename derivation." if "ChaCha20" in note_flags else ""),
                    "\n".join(hkx_preview.detail_lines),
                    ("Companion and related files are listed below." if related_references else ""),
                ]
                if part
            )
            return ArchivePreviewResult(
                status="ok",
                title=entry.basename,
                metadata_summary=f"{metadata_summary} | Havok",
                detail_text=build_archive_entry_detail_text(entry, detail_extra),
                preview_text=hkx_preview.preview_text,
                model_texture_references=related_references,
                asset_family_graph=build_archive_asset_family_graph(entry, related_references),
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
            related_references = build_archive_entry_related_references(
                entry,
                text=text,
                archive_entries_by_normalized_path=texture_entries_by_normalized_path,
                archive_entries_by_basename=texture_entries_by_basename,
            )
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
            if related_references:
                extra_note = "\n\n".join(
                    part for part in [extra_note.strip(), "Companion and related files are listed below."] if part
                )
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
                model_texture_references=related_references,
                asset_family_graph=build_archive_asset_family_graph(entry, related_references),
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
                from cdmw.modding.mesh_parser import parse_mesh

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
                        "Set texconv.exe under Settings > Paths to enable textured model shading and PNG-backed model export."
                    )
            else:
                if normalized_visible_texture_mode == "mesh_base_first":
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
                if sidecar_texture_references:
                    info_extra_parts.extend(
                        _attach_model_sidecar_texture_preview_paths(
                            texconv_path,
                            entry,
                            model_preview,
                            parsed_mesh=parsed_mesh_for_references,
                            sidecar_texture_bindings=sidecar_texture_references,
                            visible_texture_mode=normalized_visible_texture_mode,
                            texture_entries_by_normalized_path=texture_entries_by_normalized_path,
                            texture_entries_by_basename=texture_entries_by_basename,
                            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                            sidecar_texts_by_basename=sidecar_texts_by_basename,
                            stop_event=stop_event,
                        )
                    )
                if normalized_visible_texture_mode != "mesh_base_first":
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
                if sidecar_texture_references and normalized_visible_texture_mode == "mesh_base_first":
                    info_extra_parts.extend(
                        _attach_model_sidecar_texture_preview_paths(
                            texconv_path,
                            entry,
                            model_preview,
                            parsed_mesh=parsed_mesh_for_references,
                            sidecar_texture_bindings=sidecar_texture_references,
                            visible_texture_mode="layer_aware_visible",
                            texture_entries_by_normalized_path=texture_entries_by_normalized_path,
                            texture_entries_by_basename=texture_entries_by_basename,
                            sidecar_texts_by_normalized_path=sidecar_texts_by_normalized_path,
                            sidecar_texts_by_basename=sidecar_texts_by_basename,
                            fallback_only=True,
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
                            override_existing_base=True,
                            prefer_material_name_for_base=True,
                            stop_event=stop_event,
                        )
                    )
                info_extra_parts.extend(
                    _attach_model_support_texture_preview_paths(
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
        if extension in ARCHIVE_MODEL_EXTENSIONS and parsed_mesh_for_references is None:
            try:
                from cdmw.modding.mesh_parser import parse_mesh

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
            asset_family_graph=build_archive_asset_family_graph(entry, model_texture_references),
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

