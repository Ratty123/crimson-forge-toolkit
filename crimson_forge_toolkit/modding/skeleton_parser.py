"""PAB skeleton parser for Crimson Desert.

Parses .pab files to extract bone hierarchies with names, parent indices,
and transform matrices. Used to add armature data to PAC mesh exports.

PAB format (PAR v5.1):
  Header: 22 bytes (magic + version + hash + uint16 bone_count)
  [0x14] uint16: bone_count
  Per bone:
    [4B] bone_hash
    [1B] bone_name_length
    [Nb] bone_name (length-prefixed ASCII)
    [4B] parent_index (int32, -1 = root)
    [64B] bind_matrix (4x4 float32)
    [64B] inverse_bind_matrix (4x4 float32)
    [64B] bind_matrix_copy
    [64B] inverse_bind_copy
    [12B] scale (3 float32)
    [16B] rotation_quaternion (4 float32: x, y, z, w)
    [12B] position (3 float32)
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from .logging import get_logger

logger = get_logger("core.skeleton_parser")

PAR_MAGIC = b"PAR "
PAB_HEADER_SIZE = 0x16
PAB_BONE_FIXED_SIZE = 305  # hash + name_len + parent + 4 matrices + scale + rotation + position
_RIG_FAMILY_RE = re.compile(r"^(?:cd_)?([a-z0-9]+)_(\d{2})(?:_|$)")


@dataclass
class Bone:
    """A single bone in the skeleton hierarchy."""
    index: int = 0
    name: str = ""
    name_hash: int = 0
    parent_index: int = -1
    bind_matrix: tuple = ()       # 16 floats (4x4 row-major)
    inv_bind_matrix: tuple = ()   # 16 floats
    scale: tuple = (1.0, 1.0, 1.0)
    rotation: tuple = (0.0, 0.0, 0.0, 1.0)  # quaternion xyzw
    position: tuple = (0.0, 0.0, 0.0)
    file_offset: int = 0
    file_end: int = 0


@dataclass
class Skeleton:
    """Parsed skeleton with bone hierarchy."""
    path: str = ""
    bones: list[Bone] = field(default_factory=list)
    bone_count: int = 0
    tail_offset: int = 0
    tail_data: bytes = b""
    parser_mode: str = "fixed"
    parse_warning: str = ""

    def get_bone_by_name(self, name: str) -> Optional[Bone]:
        for b in self.bones:
            if b.name == name:
                return b
        return None

    def get_children(self, bone_index: int) -> list[Bone]:
        return [b for b in self.bones if b.parent_index == bone_index]

    def get_root_bones(self) -> list[Bone]:
        return [b for b in self.bones if b.parent_index == -1]


def _read_fixed_pab_bone(data: bytes, off: int, index: int) -> tuple[Bone, int]:
    start = off
    if off + PAB_BONE_FIXED_SIZE > len(data):
        raise ValueError(f"Bone {index} fixed record is truncated at 0x{off:X}.")

    bone = Bone(index=index, file_offset=start)
    bone.name_hash = struct.unpack_from("<I", data, off)[0]
    off += 4

    name_len = data[off]
    off += 1
    if off + name_len + 4 > len(data):
        raise ValueError(f"Bone {index} name is truncated at 0x{off:X}.")
    bone.name = data[off:off + name_len].decode("ascii", "replace")
    off += name_len

    bone.parent_index = struct.unpack_from("<i", data, off)[0]
    off += 4
    bone.bind_matrix = struct.unpack_from("<16f", data, off)
    off += 64
    bone.inv_bind_matrix = struct.unpack_from("<16f", data, off)
    off += 64
    # The format stores two additional matrix copies that are not used by the
    # current preview/export APIs, but they are part of the fixed record stride.
    off += 128
    bone.scale = struct.unpack_from("<fff", data, off)
    off += 12
    bone.rotation = struct.unpack_from("<ffff", data, off)
    off += 16
    bone.position = struct.unpack_from("<fff", data, off)
    off += 12
    bone.file_end = off
    return bone, off


def _parse_pab_fixed(data: bytes, filename: str = "") -> Skeleton:
    if len(data) < PAB_HEADER_SIZE or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAB file: {data[:4]!r}")

    skeleton = Skeleton(path=filename, parser_mode="fixed")
    bone_count = struct.unpack_from("<H", data, 0x14)[0]
    skeleton.bone_count = bone_count
    if bone_count == 0:
        skeleton.tail_offset = PAB_HEADER_SIZE
        skeleton.tail_data = data[PAB_HEADER_SIZE:]
        return skeleton

    off = PAB_HEADER_SIZE
    for i in range(bone_count):
        bone, off = _read_fixed_pab_bone(data, off, i)
        skeleton.bones.append(bone)

    skeleton.tail_offset = off
    skeleton.tail_data = data[off:]
    return skeleton


def _parse_pab_legacy_scan(data: bytes, filename: str = "", warning: str = "") -> Skeleton:
    """Best-effort legacy parser retained for unknown/truncated variants."""
    if len(data) < 0x16 or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAB file: {data[:4]!r}")

    skeleton = Skeleton(path=filename, parser_mode="legacy_scan", parse_warning=warning)

    bone_count = data[0x14]
    skeleton.bone_count = bone_count

    if bone_count == 0:
        skeleton.tail_offset = 0x17
        skeleton.tail_data = data[0x17:]
        return skeleton

    off = 0x15
    off += 2

    for i in range(bone_count):
        if off + 8 >= len(data):
            break

        bone = Bone(index=i, file_offset=off)
        if off + 4 <= len(data):
            bone.name_hash = struct.unpack_from("<I", data, off)[0]

        off += 4
        name_start = off
        name_end = off
        while name_end < min(off + 128, len(data)):
            byte = data[name_end]
            if byte < 0x20 or byte > 0x7E:
                break
            name_end += 1
        bone.name = data[name_start:name_end].decode('ascii', 'replace')
        off = name_end

        # Parent index: find FFFFFFFF (root = -1) or a valid small int
        # The parent field immediately follows the name (possibly after null bytes)
        parent_found = False
        scan_end = min(off + 16, len(data) - 4)
        for scan in range(off, scan_end):
            val = struct.unpack_from('<i', data, scan)[0]
            if val == -1 or (0 <= val < bone_count):
                bone.parent_index = val
                off = scan + 4
                parent_found = True
                break
        if not parent_found:
            off = name_end + 4  # skip 4 bytes and hope

        # Transform data: 4 matrices (4x4 float each = 64 bytes) + scale + rotation + position
        # Total: 256 + 40 = 296 bytes minimum
        if off + 64 <= len(data):
            bone.bind_matrix = struct.unpack_from('<16f', data, off)
            off += 64

        if off + 64 <= len(data):
            bone.inv_bind_matrix = struct.unpack_from('<16f', data, off)
            off += 64

        # Skip 2 more matrices (copies)
        if off + 128 <= len(data):
            off += 128

        # Scale (3 floats)
        if off + 12 <= len(data):
            bone.scale = struct.unpack_from('<fff', data, off)
            off += 12

        # Rotation quaternion (4 floats: x, y, z, w)
        if off + 16 <= len(data):
            bone.rotation = struct.unpack_from('<ffff', data, off)
            off += 16

        # Position (3 floats)
        if off + 12 <= len(data):
            bone.position = struct.unpack_from('<fff', data, off)
            off += 12

        # Skip any remaining padding/data to align with next bone hash
        # Next bone starts with a 4-byte hash before its name
        # Scan forward for next uppercase letter (bone name start)
        if i < bone_count - 1:
            while off < len(data) - 4:
                # Check if next bone name starts here (uppercase letter)
                if off + 5 < len(data) and 65 <= data[off + 4] <= 90:
                    break
                off += 1

        bone.file_end = off
        skeleton.bones.append(bone)

    skeleton.tail_offset = off
    skeleton.tail_data = data[off:]
    return skeleton


def parse_pab(data: bytes, filename: str = "") -> Skeleton:
    """Parse a .pab skeleton file.

    Returns a Skeleton with bone names, parent indices, and transforms.
    """
    if len(data) < PAB_HEADER_SIZE or data[:4] != PAR_MAGIC:
        raise ValueError(f"Not a valid PAB file: {data[:4]!r}")
    try:
        skeleton = _parse_pab_fixed(data, filename)
        if skeleton.bone_count > 0 and not skeleton.bones:
            raise ValueError("Fixed PAB parser recovered no bones.")
        logger.info(
            "Parsed PAB %s with fixed layout: %d/%d bones",
            filename,
            len(skeleton.bones),
            skeleton.bone_count,
        )
        return skeleton
    except Exception as exc:
        warning = f"Fixed PAB parser failed; used legacy scan fallback: {exc}"
        logger.warning("PAB %s: %s", filename, warning)
        skeleton = _parse_pab_legacy_scan(data, filename, warning=warning)
        logger.info(
            "Parsed PAB %s with legacy scan fallback: %d/%d bones",
            filename,
            len(skeleton.bones),
            skeleton.bone_count,
        )
        return skeleton


def find_matching_pab(pac_path: str, pamt_entries) -> Optional[str]:
    """Find a .pab file matching a .pac file path."""
    normalized_pac_path = str(pac_path or "").replace("\\", "/").strip().lower()
    exact_match_path = f"{PurePosixPath(normalized_pac_path).with_suffix('.pab').as_posix()}"
    candidate_basenames = iter_pab_candidate_basenames(normalized_pac_path)

    best_path: Optional[str] = None
    best_score = -1
    for entry in pamt_entries:
        entry_path = str(getattr(entry, "path", "") or "").replace("\\", "/").strip().lower()
        if not entry_path.endswith(".pab"):
            continue
        score = 0
        if entry_path == exact_match_path:
            score += 100
        entry_basename = PurePosixPath(entry_path).name
        if entry_basename in candidate_basenames:
            score += 25
        if score > best_score:
            best_score = score
            best_path = getattr(entry, "path", None)
    if best_score >= 0:
        return best_path
    return None


def is_skeleton_file(path: str) -> bool:
    """Check if a file is a skeleton file."""
    return os.path.splitext(path.lower())[1] == ".pab"


def iter_pab_candidate_basenames(pac_path: str) -> tuple[str, ...]:
    normalized_path = str(pac_path or "").replace("\\", "/").strip().lower()
    stem = PurePosixPath(normalized_path).stem
    if not stem:
        return ()

    ordered: list[str] = []
    seen: set[str] = set()

    def _append(raw_value: str) -> None:
        candidate = str(raw_value or "").strip().lower()
        if not candidate:
            return
        if not candidate.endswith(".pab"):
            candidate = f"{candidate}.pab"
        if candidate in seen:
            return
        seen.add(candidate)
        ordered.append(candidate)

    _append(stem)

    tokens = [token for token in stem.split("_") if token]
    if len(tokens) >= 3 and tokens[0] == "cd":
        _append("_".join(tokens[:3]))
        _append("_".join(tokens[1:3]))

    rig_match = _RIG_FAMILY_RE.match(stem)
    if rig_match is not None:
        family_name = f"{rig_match.group(1)}_{rig_match.group(2)}"
        _append(family_name)
        _append(f"cd_{family_name}")

    return tuple(ordered)
