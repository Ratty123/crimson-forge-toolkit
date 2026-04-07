from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import math
import re
import shutil
import struct
import sys
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from crimson_texture_forge.constants import *
from crimson_texture_forge.models import *
from crimson_texture_forge.core.common import *
from crimson_texture_forge.core.chainner import *

def parse_dds(dds_path: Path) -> DdsInfo:
    with dds_path.open("rb") as handle:
        blob = handle.read(148)

    if len(blob) < 128:
        raise ValueError("File is too small to be a valid DDS.")

    if blob[:4] != DDS_MAGIC:
        raise ValueError("Missing DDS magic.")

    header = blob[4:128]
    header_size = read_u32_le(header, 0)
    if header_size != 124:
        raise ValueError(f"Unexpected DDS header size: {header_size}")

    height = read_u32_le(header, 8)
    width = read_u32_le(header, 12)
    mip_count = read_u32_le(header, 24) or 1

    pf_size = read_u32_le(header, 72)
    if pf_size != 32:
        raise ValueError(f"Unexpected DDS pixel format size: {pf_size}")

    pf_flags = read_u32_le(header, 76)
    fourcc = header[80:84]
    rgb_bit_count = read_u32_le(header, 84)
    r_mask = read_u32_le(header, 88)
    g_mask = read_u32_le(header, 92)
    b_mask = read_u32_le(header, 96)
    a_mask = read_u32_le(header, 100)

    texconv_format: Optional[str] = None

    if pf_flags & DDPF_FOURCC:
        if fourcc == b"DX10":
            if len(blob) < 148:
                raise ValueError("DDS declares DX10 header, but file is too small.")
            dx10 = blob[128:148]
            dxgi_format = read_u32_le(dx10, 0)
            texconv_format = DXGI_TO_TEXCONV.get(dxgi_format)
            if not texconv_format:
                raise ValueError(f"Unsupported DXGI format: {dxgi_format}")
        else:
            texconv_format = LEGACY_FOURCC_TO_TEXCONV.get(fourcc)
            if not texconv_format:
                pretty_fourcc = fourcc.decode("ascii", errors="replace")
                raise ValueError(f"Unsupported legacy FOURCC format: {pretty_fourcc!r}")
    elif pf_flags & DDPF_RGB:
        if rgb_bit_count == 32:
            if (r_mask, g_mask, b_mask, a_mask) == (
                0x000000FF,
                0x0000FF00,
                0x00FF0000,
                0xFF000000,
            ):
                texconv_format = "R8G8B8A8_UNORM"
            elif (r_mask, g_mask, b_mask, a_mask) == (
                0x00FF0000,
                0x0000FF00,
                0x000000FF,
                0xFF000000,
            ):
                texconv_format = "B8G8R8A8_UNORM"
            elif (r_mask, g_mask, b_mask, a_mask) == (
                0x00FF0000,
                0x0000FF00,
                0x000000FF,
                0x00000000,
            ):
                texconv_format = "B8G8R8X8_UNORM"
            else:
                raise ValueError(
                    "Unsupported 32-bit RGB mask combination: "
                    f"R={r_mask:#010x} G={g_mask:#010x} B={b_mask:#010x} A={a_mask:#010x}"
                )
        else:
            raise ValueError(f"Unsupported uncompressed RGB bit depth: {rgb_bit_count}")
    else:
        raise ValueError(f"Unsupported DDS pixel format flags: {pf_flags:#x}")

    return DdsInfo(
        width=width,
        height=height,
        mip_count=max(1, mip_count),
        texconv_format=texconv_format,
        source_path=dds_path,
    )


def read_png_dimensions(png_path: Path) -> Tuple[int, int]:
    with png_path.open("rb") as handle:
        signature = handle.read(8)
        if signature != PNG_MAGIC:
            raise ValueError("Not a PNG file or PNG signature is invalid.")
        ihdr_len = struct.unpack(">I", handle.read(4))[0]
        chunk_type = handle.read(4)
        if chunk_type != b"IHDR" or ihdr_len != 13:
            raise ValueError("PNG IHDR chunk is missing or invalid.")
        width, height = struct.unpack(">II", handle.read(8))
        return width, height


def max_mips_for_size(width: int, height: int) -> int:
    return int(math.floor(math.log2(max(width, height)))) + 1


def normalize_required_path(value: str, label: str) -> Path:
    raw = value.strip()
    if not raw:
        raise ValueError(f"{label} is required.")
    return Path(raw).expanduser().resolve()


def normalize_optional_path(value: str) -> Optional[Path]:
    raw = value.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def ensure_existing_dir(path: Path, label: str) -> Path:
    if not path.exists() or not path.is_dir():
        raise ValueError(f"{label} does not exist or is not a folder: {path}")
    return path


def ensure_existing_file(path: Path, label: str) -> Path:
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} does not exist or is not a file: {path}")
    return path


def parse_filter_patterns(raw_text: str) -> Tuple[str, ...]:
    tokens: List[str] = []
    for line in raw_text.replace("\r", "\n").split("\n"):
        for piece in line.split(";"):
            token = piece.strip()
            if token:
                tokens.append(token)
    return tuple(tokens)


def filter_matches(relative_path: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True

    rel_posix = relative_path.as_posix().lower()
    basename = relative_path.name.lower()
    parent = "" if relative_path.parent == Path(".") else relative_path.parent.as_posix().lower()

    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").strip().lower()
        if not pattern:
            continue
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if fnmatch.fnmatch(basename, pattern):
            return True
        if parent and fnmatch.fnmatch(parent, pattern):
            return True

        if not any(char in pattern for char in "*?[]"):
            clean = pattern.strip("/")
            if not clean:
                continue
            if rel_posix == clean or basename == clean or parent == clean:
                return True
            if rel_posix.startswith(f"{clean}/"):
                return True

    return False


def collect_dds_files(
    original_root: Path,
    include_filter_patterns: Sequence[str],
    stop_event: Optional[threading.Event] = None,
) -> List[Path]:
    files: List[Path] = []

    for path in original_root.rglob("*"):
        raise_if_cancelled(stop_event, "Scan cancelled by user.")
        if not path.is_file() or path.suffix.lower() != ".dds":
            continue

        relative_path = path.relative_to(original_root)
        if filter_matches(relative_path, include_filter_patterns):
            files.append(path)

    files.sort()
    return files


def find_png_matches(
    png_root: Path,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[Dict[str, Path], Dict[str, List[Path]], int]:
    relative_index: Dict[str, Path] = {}
    basename_index: Dict[str, List[Path]] = defaultdict(list)
    count = 0

    for path in png_root.rglob("*"):
        raise_if_cancelled(stop_event)
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        rel_key = path.relative_to(png_root).as_posix().lower()
        relative_index[rel_key] = path
        basename_index[path.name.lower()].append(path)
        count += 1

    return relative_index, basename_index, count


def resolve_png(
    rel_path_from_original_root: Path,
    relative_index: Dict[str, Path],
    basename_index: Dict[str, List[Path]],
    allow_unique_basename_fallback: bool,
) -> Tuple[Optional[Path], str]:
    rel_png = rel_path_from_original_root.with_suffix(".png").as_posix().lower()
    exact = relative_index.get(rel_png)
    if exact:
        return exact, "exact relative match"

    if not allow_unique_basename_fallback:
        return None, "no exact relative PNG match found"

    same_name = basename_index.get(rel_path_from_original_root.with_suffix(".png").name.lower(), [])
    if len(same_name) == 1:
        return same_name[0], "unique basename fallback"
    if len(same_name) > 1:
        return None, f"ambiguous basename fallback, {len(same_name)} matches found"

    return None, "no matching PNG found"


def build_texconv_command(
    texconv_path: Path,
    png_path: Path,
    output_dir: Path,
    fmt: str,
    mips: int,
    resize_width: Optional[int],
    resize_height: Optional[int],
    overwrite_existing_dds: bool,
) -> List[str]:
    cmd = [str(texconv_path), "-nologo"]

    if overwrite_existing_dds:
        cmd.append("-y")

    cmd.extend(
        [
            "-ft",
            "dds",
            "-f",
            fmt,
            "-m",
            str(mips),
            "-o",
            str(output_dir),
        ]
    )

    if resize_width is not None and resize_height is not None:
        cmd.extend(["-w", str(resize_width), "-h", str(resize_height)])

    cmd.append(str(png_path))
    return cmd


def _validate_choice(value: str, allowed: Sequence[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"Unsupported {label}: {value}")
    return value


def resolve_dds_output_settings(
    config: NormalizedConfig,
    dds_info: DdsInfo,
    png_width: int,
    png_height: int,
) -> DdsOutputSettings:
    notes: List[str] = []

    if config.dds_format_mode == DDS_FORMAT_MODE_MATCH_ORIGINAL:
        texconv_format = dds_info.texconv_format
    else:
        texconv_format = config.dds_custom_format
        notes.append(f"custom format {texconv_format}")

    if config.dds_size_mode == DDS_SIZE_MODE_ORIGINAL:
        output_width = dds_info.width
        output_height = dds_info.height
        resize_to_dimensions = True
        notes.append(f"original size {output_width}x{output_height}")
    elif config.dds_size_mode == DDS_SIZE_MODE_CUSTOM:
        output_width = config.dds_custom_width
        output_height = config.dds_custom_height
        resize_to_dimensions = True
        notes.append(f"custom size {output_width}x{output_height}")
    else:
        output_width = png_width
        output_height = png_height
        resize_to_dimensions = False

    max_possible_mips = max_mips_for_size(output_width, output_height)
    if config.dds_mip_mode == DDS_MIP_MODE_MATCH_ORIGINAL:
        mip_count = min(dds_info.mip_count, max_possible_mips)
        if mip_count != dds_info.mip_count:
            notes.append(
                f"original mip count {dds_info.mip_count} exceeds output max {max_possible_mips}, clamped to {mip_count}"
            )
    elif config.dds_mip_mode == DDS_MIP_MODE_FULL_CHAIN:
        mip_count = max_possible_mips
        notes.append(f"full mip chain {mip_count}")
    elif config.dds_mip_mode == DDS_MIP_MODE_SINGLE:
        mip_count = 1
        notes.append("single mip")
    else:
        mip_count = min(config.dds_custom_mip_count, max_possible_mips)
        if mip_count != config.dds_custom_mip_count:
            notes.append(
                f"custom mip count {config.dds_custom_mip_count} exceeds output max {max_possible_mips}, clamped to {mip_count}"
            )
        else:
            notes.append(f"custom mip count {mip_count}")

    return DdsOutputSettings(
        texconv_format=texconv_format,
        mip_count=mip_count,
        width=output_width,
        height=output_height,
        resize_to_dimensions=resize_to_dimensions,
        notes=notes,
    )


def apply_texture_rule_to_output_settings(
    settings: DdsOutputSettings,
    rule: TextureRule,
) -> Tuple[Optional[DdsOutputSettings], str]:
    if rule.action == "skip":
        return None, f"texture rule matched: {rule.pattern} -> skip"

    next_settings = DdsOutputSettings(
        texconv_format=settings.texconv_format,
        mip_count=settings.mip_count,
        width=settings.width,
        height=settings.height,
        resize_to_dimensions=settings.resize_to_dimensions,
        notes=list(settings.notes),
    )

    if rule.format_value and rule.format_value != DDS_FORMAT_MODE_MATCH_ORIGINAL:
        next_settings.texconv_format = rule.format_value
    if rule.size_value:
        if rule.size_value == DDS_SIZE_MODE_PNG:
            next_settings.resize_to_dimensions = False
        elif rule.size_value == DDS_SIZE_MODE_ORIGINAL:
            next_settings.resize_to_dimensions = True
        else:
            width_text, height_text = rule.size_value.lower().split("x", 1)
            next_settings.width = int(width_text)
            next_settings.height = int(height_text)
            next_settings.resize_to_dimensions = True
    if rule.mip_value:
        if rule.mip_value == DDS_MIP_MODE_FULL_CHAIN:
            next_settings.mip_count = max_mips_for_size(next_settings.width, next_settings.height)
        elif rule.mip_value == DDS_MIP_MODE_SINGLE:
            next_settings.mip_count = 1
        elif rule.mip_value not in {DDS_MIP_MODE_MATCH_ORIGINAL, DDS_MIP_MODE_FULL_CHAIN, DDS_MIP_MODE_SINGLE}:
            next_settings.mip_count = int(rule.mip_value)

    next_settings.notes.append(f"texture rule matched: {rule.pattern}")
    return next_settings, f"texture rule matched: {rule.pattern}"


def _rule_matches_path(pattern: str, relative_path: Path) -> bool:
    rel_posix = relative_path.as_posix().lower()
    basename = relative_path.name.lower()
    normalized_pattern = pattern.replace("\\", "/").strip().lower()
    if not normalized_pattern:
        return False
    return fnmatch.fnmatch(rel_posix, normalized_pattern) or fnmatch.fnmatch(basename, normalized_pattern)


def find_matching_texture_rule(relative_path: Path, rules: Sequence[TextureRule]) -> Optional[TextureRule]:
    for rule in rules:
        if _rule_matches_path(rule.pattern, relative_path):
            return rule
    return None


def parse_texture_rules(raw_text: str) -> Tuple[TextureRule, ...]:
    rules: List[TextureRule] = []
    for line_number, raw_line in enumerate(raw_text.replace("\r", "\n").split("\n"), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split(";") if part.strip()]
        if not parts:
            continue

        pattern = parts[0]
        if "=" in pattern:
            raise ValueError(f"Texture rule line {line_number} is missing the leading file pattern.")

        rule = TextureRule(pattern=pattern, source_line=line)
        for part in parts[1:]:
            if "=" not in part:
                raise ValueError(f"Texture rule line {line_number} has an invalid token: {part}")
            key, value = [piece.strip() for piece in part.split("=", 1)]
            lowered_key = key.lower()
            lowered_value = value.lower()
            if lowered_key == "action":
                if lowered_value not in {"process", "skip"}:
                    raise ValueError(f"Texture rule line {line_number} has an invalid action: {value}")
                rule.action = lowered_value
            elif lowered_key == "format":
                if lowered_value in {"match_original", "original"}:
                    rule.format_value = DDS_FORMAT_MODE_MATCH_ORIGINAL
                elif value in SUPPORTED_TEXCONV_FORMAT_CHOICES:
                    rule.format_value = value
                else:
                    raise ValueError(f"Texture rule line {line_number} has an unsupported format: {value}")
            elif lowered_key == "size":
                if lowered_value in {DDS_SIZE_MODE_PNG, DDS_SIZE_MODE_ORIGINAL}:
                    rule.size_value = lowered_value
                elif re.match(r"^\d+x\d+$", lowered_value):
                    rule.size_value = lowered_value
                else:
                    raise ValueError(f"Texture rule line {line_number} has an invalid size: {value}")
            elif lowered_key in {"mips", "mipmaps", "mip"}:
                if lowered_value in {DDS_MIP_MODE_MATCH_ORIGINAL, DDS_MIP_MODE_FULL_CHAIN, DDS_MIP_MODE_SINGLE}:
                    rule.mip_value = lowered_value
                elif lowered_value.isdigit() and int(lowered_value) >= 1:
                    rule.mip_value = lowered_value
                else:
                    raise ValueError(f"Texture rule line {line_number} has an invalid mip setting: {value}")
            else:
                raise ValueError(f"Texture rule line {line_number} has an unknown key: {key}")

        rules.append(rule)

    return tuple(rules)


def resolve_default_staging_png_root(png_root: Path, enable_chainner: bool) -> Path:
    if not enable_chainner:
        return png_root
    return png_root.parent / f"{png_root.name}_staged_input"


def build_manifest_path(output_root: Path) -> Path:
    return output_root / ".crimson_texture_forge_manifest.json"


def load_incremental_manifest(manifest_path: Path) -> Dict[str, Dict[str, object]]:
    source_path = manifest_path
    if not source_path.exists():
        legacy_path = manifest_path.with_name(".dds_rebuild_manifest.json")
        if legacy_path.exists():
            source_path = legacy_path
        else:
            return {}
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    return entries if isinstance(entries, dict) else {}


def save_incremental_manifest(manifest_path: Path, entries: Dict[str, Dict[str, object]]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    payload = {
        "version": 1,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    }
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(manifest_path)


def build_incremental_manifest_entry(
    original_dds: Path,
    png_path: Path,
    output_file: Path,
    output_settings: DdsOutputSettings,
) -> Dict[str, object]:
    original_stat = original_dds.stat()
    png_stat = png_path.stat()
    output_stat = output_file.stat()
    return {
        "original_mtime_ns": original_stat.st_mtime_ns,
        "original_size": original_stat.st_size,
        "png_mtime_ns": png_stat.st_mtime_ns,
        "png_size": png_stat.st_size,
        "output_mtime_ns": output_stat.st_mtime_ns,
        "output_size": output_stat.st_size,
        "format": output_settings.texconv_format,
        "mips": output_settings.mip_count,
        "resize": output_settings.resize_to_dimensions,
        "width": output_settings.width,
        "height": output_settings.height,
    }


def manifest_entry_matches(
    entry: Dict[str, object],
    original_dds: Path,
    png_path: Path,
    output_file: Path,
    output_settings: DdsOutputSettings,
) -> bool:
    if not output_file.exists():
        return False
    try:
        expected = build_incremental_manifest_entry(original_dds, png_path, output_file, output_settings)
    except OSError:
        return False
    for key, value in expected.items():
        if entry.get(key) != value:
            return False
    return True


def build_preflight_report_lines(
    normalized: NormalizedConfig,
    dds_files: Sequence[Path],
    *,
    chain_analysis: Optional[ChainnerChainAnalysis] = None,
    texture_rules: Sequence[TextureRule] = (),
) -> List[str]:
    total_dds_bytes = 0
    for path in dds_files:
        try:
            total_dds_bytes += path.stat().st_size
        except OSError:
            continue

    lines = [
        "Preflight report:",
        f"- DDS files matching filter: {len(dds_files)}",
        f"- Original DDS root: {normalized.original_dds_root}",
        f"- PNG root: {normalized.png_root}",
        f"- Output root: {normalized.output_root}",
        f"- DDS staging: {'enabled' if normalized.enable_dds_staging else 'disabled'}",
    ]

    if normalized.enable_dds_staging and normalized.dds_staging_root is not None:
        lines.append(f"- DDS staging root: {normalized.dds_staging_root}")
        if normalized.enable_chainner:
            lines.append(
                "Warning: DDS-to-PNG conversion is enabled before chaiNNer. "
                "Your chain must read PNG files from the staging root or another matching PNG folder, "
                "and it must not keep reading DDS from Original DDS root unless that is intentional."
            )
        if normalized.enable_chainner and "${staging_png_root}" not in normalized.chainner_override_json:
            lines.append("- Warning: staging is enabled, but your chaiNNer overrides do not reference ${staging_png_root}.")

    lines.extend(
        [
            f"- Incremental resume: {'enabled' if normalized.enable_incremental_resume else 'disabled'}",
            f"- Texture rules loaded: {len(texture_rules)}",
            f"- Estimated source DDS data: {total_dds_bytes / (1024 * 1024):.1f} MiB",
        ]
    )

    try:
        usage = shutil.disk_usage(normalized.output_root if normalized.output_root.exists() else normalized.output_root.parent)
        lines.append(f"- Free disk space near output root: {usage.free / (1024 * 1024 * 1024):.1f} GiB")
    except OSError:
        lines.append("- Free disk space near output root: unavailable")

    if chain_analysis and chain_analysis.warnings:
        lines.append("- chaiNNer preflight warnings:")
        for warning in chain_analysis.warnings[:5]:
            lines.append(f"  {warning}")

    return lines


def collect_relative_dds_paths(root: Path) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    files = [
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".dds"
    ]
    files.sort()
    return files


def collect_compare_relative_paths(original_root: Path, output_root: Path) -> List[Path]:
    combined = set(collect_relative_dds_paths(original_root))
    combined.update(collect_relative_dds_paths(output_root))
    return sorted(combined)


def build_preview_png_command(texconv_path: Path, dds_path: Path, output_dir: Path) -> List[str]:
    return [
        str(texconv_path),
        "-nologo",
        "-y",
        "-f",
        "R8G8B8A8_UNORM",
        "-ft",
        "png",
        "-o",
        str(output_dir),
        str(dds_path),
    ]


def ensure_dds_preview_png(texconv_path: Path, dds_path: Path) -> Path:
    stat = dds_path.stat()
    cache_key = hashlib.sha256(
        f"{dds_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / APP_NAME / "preview_cache" / cache_key
    preview_path = cache_dir / f"{dds_path.stem}.png"

    if preview_path.exists():
        return preview_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_preview_png_command(texconv_path, dds_path, cache_dir)
    return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=None)
    if return_code != 0:
        detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
        raise ValueError(f"Could not generate preview for {dds_path.name}: {detail}")

    if preview_path.exists():
        return preview_path

    candidates = sorted(cache_dir.glob("*.png"))
    if candidates:
        return candidates[0]

    raise ValueError(f"texconv did not produce a PNG preview for {dds_path.name}.")


def stage_dds_to_pngs(
    config: NormalizedConfig,
    dds_files: Sequence[Path],
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    if not config.enable_dds_staging or config.dds_staging_root is None:
        return

    stage_root = config.dds_staging_root
    stage_root.mkdir(parents=True, exist_ok=True)

    total = len(dds_files)
    if on_phase:
        on_phase("DDS Staging", "Extracting DDS files to PNG...", False)
    if on_log:
        on_log(f"Phase 0/2: staging DDS files to PNG in {stage_root}")
    if on_phase_progress:
        on_phase_progress(0, total, f"0 / {total} DDS staging files")

    for index, dds_path in enumerate(dds_files, start=1):
        raise_if_cancelled(stop_event)
        relative_path = dds_path.relative_to(config.original_dds_root)
        if on_current_file:
            on_current_file(f"Stage: {relative_path.as_posix()}")

        target_dir = stage_root / relative_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        target_png = stage_root / relative_path.with_suffix(".png")

        should_skip = False
        if target_png.exists():
            try:
                should_skip = target_png.stat().st_mtime_ns >= dds_path.stat().st_mtime_ns and target_png.stat().st_size > 0
            except OSError:
                should_skip = False

        if should_skip:
            if on_log:
                on_log(f"[{index}/{total}] STAGE SKIP {relative_path.as_posix()} -> PNG is newer than source DDS")
            if on_phase_progress:
                on_phase_progress(index, total, f"{index} / {total} DDS staging files")
            continue

        cmd = build_preview_png_command(config.texconv_path, dds_path, target_dir)
        if config.dry_run:
            if on_log:
                on_log(f"[{index}/{total}] STAGE DRYRUN {relative_path.as_posix()}")
        else:
            return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
            if return_code != 0:
                detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
                raise ValueError(f"Could not stage {relative_path.as_posix()} to PNG: {detail}")
        if on_phase_progress:
            on_phase_progress(index, total, f"{index} / {total} DDS staging files")


def build_compare_preview_pane_result(
    texconv_path: Optional[Path],
    dds_path: Optional[Path],
    missing_message: str,
) -> ComparePreviewPaneResult:
    if texconv_path is None:
        return ComparePreviewPaneResult(status="missing", message="Set texconv.exe to enable DDS previews.")

    if dds_path is None or not dds_path.exists():
        return ComparePreviewPaneResult(status="missing", message=missing_message)

    try:
        metadata_summary = ""
        try:
            dds_info = parse_dds(dds_path.resolve())
            metadata_summary = f"Format: {dds_info.texconv_format} | Size: {dds_info.width}x{dds_info.height} | Mips: {dds_info.mip_count}"
        except Exception:
            metadata_summary = "DDS metadata unavailable."
        preview_png = ensure_dds_preview_png(texconv_path.resolve(), dds_path.resolve())
        return ComparePreviewPaneResult(
            status="ok",
            title=dds_path.name,
            preview_png_path=str(preview_png),
            metadata_summary=metadata_summary,
        )
    except Exception as exc:
        return ComparePreviewPaneResult(status="error", message=str(exc))


def normalize_config(config: AppConfig) -> NormalizedConfig:
    original_dds_root = ensure_existing_dir(
        normalize_required_path(config.original_dds_root, "Original DDS root"),
        "Original DDS root",
    )
    png_root = normalize_required_path(config.png_root, "PNG root")
    if not config.enable_chainner and not config.enable_dds_staging:
        ensure_existing_dir(png_root, "PNG root")
    output_root = normalize_required_path(config.output_root, "Output root")
    texconv_path = ensure_existing_file(
        normalize_required_path(config.texconv_path, "texconv.exe path"),
        "texconv.exe path",
    )

    csv_log_path: Optional[Path] = None
    if config.csv_log_enabled:
        csv_log_path = normalize_optional_path(config.csv_log_path)
        if csv_log_path is None:
            raise ValueError("CSV log is enabled, but the CSV log path is empty.")

    chainner_exe_path: Optional[Path] = None
    chainner_chain_path: Optional[Path] = None
    if config.enable_chainner:
        chainner_exe_path = ensure_existing_file(
            normalize_required_path(config.chainner_exe_path, "chaiNNer executable path"),
            "chaiNNer executable path",
        )
        chainner_chain_path = ensure_existing_file(
            normalize_required_path(config.chainner_chain_path, "chaiNNer chain path"),
            "chaiNNer chain path",
        )

    dds_staging_root: Optional[Path] = None
    if config.enable_dds_staging:
        if config.dds_staging_root.strip():
            dds_staging_root = normalize_required_path(config.dds_staging_root, "DDS staging root")
        else:
            dds_staging_root = resolve_default_staging_png_root(png_root, config.enable_chainner).resolve()
        if config.enable_chainner and dds_staging_root.resolve() == png_root.resolve():
            raise ValueError("DDS staging root must be different from the final PNG root when chaiNNer is enabled.")

    dds_format_mode = _validate_choice(
        config.dds_format_mode,
        (DDS_FORMAT_MODE_MATCH_ORIGINAL, DDS_FORMAT_MODE_CUSTOM),
        "DDS format mode",
    )
    dds_size_mode = _validate_choice(
        config.dds_size_mode,
        (DDS_SIZE_MODE_PNG, DDS_SIZE_MODE_ORIGINAL, DDS_SIZE_MODE_CUSTOM),
        "DDS size mode",
    )
    dds_mip_mode = _validate_choice(
        config.dds_mip_mode,
        (DDS_MIP_MODE_MATCH_ORIGINAL, DDS_MIP_MODE_FULL_CHAIN, DDS_MIP_MODE_SINGLE, DDS_MIP_MODE_CUSTOM),
        "DDS mip mode",
    )

    dds_custom_format = config.dds_custom_format.strip() or DEFAULT_DDS_CUSTOM_FORMAT
    if dds_format_mode == DDS_FORMAT_MODE_CUSTOM and dds_custom_format not in SUPPORTED_TEXCONV_FORMAT_CHOICES:
        raise ValueError(f"Unsupported custom DDS format: {dds_custom_format}")

    dds_custom_width = int(config.dds_custom_width)
    dds_custom_height = int(config.dds_custom_height)
    dds_custom_mip_count = int(config.dds_custom_mip_count)
    if dds_size_mode == DDS_SIZE_MODE_CUSTOM:
        if dds_custom_width < 1 or dds_custom_height < 1:
            raise ValueError("Custom DDS size must be at least 1x1.")
    if dds_mip_mode == DDS_MIP_MODE_CUSTOM and dds_custom_mip_count < 1:
        raise ValueError("Custom DDS mip count must be at least 1.")

    parsed_texture_rules = parse_texture_rules(config.texture_rules_text)

    return NormalizedConfig(
        original_dds_root=original_dds_root,
        png_root=png_root,
        output_root=output_root,
        dds_staging_root=dds_staging_root,
        texconv_path=texconv_path,
        dds_format_mode=dds_format_mode,
        dds_custom_format=dds_custom_format,
        dds_size_mode=dds_size_mode,
        dds_custom_width=dds_custom_width,
        dds_custom_height=dds_custom_height,
        dds_mip_mode=dds_mip_mode,
        dds_custom_mip_count=dds_custom_mip_count,
        enable_dds_staging=config.enable_dds_staging,
        enable_incremental_resume=config.enable_incremental_resume,
        texture_rules_text=config.texture_rules_text,
        dry_run=config.dry_run,
        csv_log_path=csv_log_path,
        allow_unique_basename_fallback=config.allow_unique_basename_fallback,
        overwrite_existing_dds=config.overwrite_existing_dds,
        include_filter_patterns=parse_filter_patterns(config.include_filters),
        enable_chainner=config.enable_chainner,
        chainner_exe_path=chainner_exe_path,
        chainner_chain_path=chainner_chain_path,
        chainner_override_json=config.chainner_override_json,
        texture_rules=parsed_texture_rules,  # type: ignore[call-arg]
    )


def scan_dds_files(config: AppConfig, stop_event: Optional[threading.Event] = None) -> ScanResult:
    original_root = ensure_existing_dir(
        normalize_required_path(config.original_dds_root, "Original DDS root"),
        "Original DDS root",
    )
    include_filters = parse_filter_patterns(config.include_filters)
    files = collect_dds_files(original_root, include_filters, stop_event=stop_event)
    return ScanResult(total_files=len(files), files=files)


def convert_dds_to_pngs(
    config: AppConfig,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_total: Optional[Callable[[int], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, int, int, int], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> RunSummary:
    original_dds_root = ensure_existing_dir(
        normalize_required_path(config.original_dds_root, "Original DDS root"),
        "Original DDS root",
    )
    png_root = normalize_required_path(config.png_root, "PNG root")
    texconv_path = ensure_existing_file(
        normalize_required_path(config.texconv_path, "texconv.exe path"),
        "texconv.exe path",
    )
    include_filters = parse_filter_patterns(config.include_filters)
    csv_log_path = normalize_optional_path(config.csv_log_path) if config.csv_log_enabled else None
    if config.csv_log_enabled and csv_log_path is None:
        raise ValueError("CSV log is enabled, but the CSV log path is empty.")

    png_root.mkdir(parents=True, exist_ok=True)

    def emit_log(message: str) -> None:
        if on_log:
            on_log(message)

    def emit_progress(processed: int, total: int, converted: int, skipped: int, failed: int) -> None:
        if on_progress:
            on_progress(processed, total, converted, skipped, failed)

    def emit_phase(name: str, detail: str, indeterminate: bool) -> None:
        if on_phase:
            on_phase(name, detail, indeterminate)

    def emit_phase_progress(current: int, total: int, detail: str) -> None:
        if on_phase_progress:
            on_phase_progress(current, total, detail)

    emit_log(
        "DDS -> PNG configuration: "
        f"dry_run={'on' if config.dry_run else 'off'}, "
        f"png_root={png_root}."
    )
    emit_log("Scanning DDS files...")
    dds_files = collect_dds_files(
        original_dds_root,
        include_filters,
        stop_event=stop_event,
    )
    total = len(dds_files)
    if total == 0:
        raise ValueError("No DDS files were found under the original root with the current filter.")

    emit_log(f"Found {total} DDS files to convert.")
    if on_total:
        on_total(total)
    emit_phase("DDS to PNG", f"Converting DDS files to PNG in {png_root}...", False)
    emit_phase_progress(0, total, f"0 / {total} DDS files")
    emit_progress(0, total, 0, 0, 0)

    results: List[JobResult] = []
    converted = 0
    skipped = 0
    failed = 0
    cancelled = False

    try:
        for index, dds_path in enumerate(dds_files, start=1):
            raise_if_cancelled(stop_event)
            rel_path = dds_path.relative_to(original_dds_root)
            rel_display = rel_path.as_posix()
            target_dir = png_root / rel_path.parent
            target_png = png_root / rel_path.with_suffix(".png")

            if on_current_file:
                on_current_file(rel_display)
            emit_progress(index - 1, total, converted, skipped, failed)
            emit_phase_progress(index - 1, total, f"{index - 1} / {total} DDS files")

            target_dir.mkdir(parents=True, exist_ok=True)

            should_skip = False
            if target_png.exists():
                try:
                    should_skip = target_png.stat().st_mtime_ns >= dds_path.stat().st_mtime_ns and target_png.stat().st_size > 0
                except OSError:
                    should_skip = False

            try:
                dds_info = parse_dds(dds_path)
            except RunCancelled:
                raise
            except Exception:
                dds_info = None

            if should_skip:
                skipped += 1
                note = "PNG is newer than source DDS"
                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png=str(target_png),
                        output_dir=str(target_dir),
                        width=dds_info.width if dds_info is not None else 0,
                        height=dds_info.height if dds_info is not None else 0,
                        original_mips=dds_info.mip_count if dds_info is not None else 0,
                        used_mips=dds_info.mip_count if dds_info is not None else 0,
                        texconv_format=dds_info.texconv_format if dds_info is not None else "",
                        status="skipped",
                        note=note,
                    )
                )
                emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                emit_progress(index, total, converted, skipped, failed)
                emit_phase_progress(index, total, f"{index} / {total} DDS files")
                continue

            cmd = build_preview_png_command(texconv_path, dds_path, target_dir)
            action = "DRYRUN" if config.dry_run else "CONVERT"
            emit_log(f"[{index}/{total}] {action} {rel_display} -> {target_png.relative_to(png_root).as_posix()}")

            try:
                if config.dry_run:
                    converted += 1
                    status = "dry-run"
                    note = "planned DDS to PNG conversion"
                else:
                    return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
                    if return_code != 0:
                        failed += 1
                        status = "failed"
                        note = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
                    else:
                        converted += 1
                        status = "converted"
                        note = "DDS converted to PNG"

                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png=str(target_png),
                        output_dir=str(target_dir),
                        width=dds_info.width if dds_info is not None else 0,
                        height=dds_info.height if dds_info is not None else 0,
                        original_mips=dds_info.mip_count if dds_info is not None else 0,
                        used_mips=dds_info.mip_count if dds_info is not None else 0,
                        texconv_format=dds_info.texconv_format if dds_info is not None else "",
                        status=status,
                        note=note,
                    )
                )
                if status == "failed":
                    emit_log(f"[{index}/{total}] FAIL {rel_display} -> {note}")
            except RunCancelled:
                raise
            except Exception as exc:
                failed += 1
                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png=str(target_png),
                        output_dir=str(target_dir),
                        width=dds_info.width if dds_info is not None else 0,
                        height=dds_info.height if dds_info is not None else 0,
                        original_mips=dds_info.mip_count if dds_info is not None else 0,
                        used_mips=dds_info.mip_count if dds_info is not None else 0,
                        texconv_format=dds_info.texconv_format if dds_info is not None else "",
                        status="failed",
                        note=str(exc),
                    )
                )
                emit_log(f"[{index}/{total}] FAIL {rel_display} -> {exc}")

            emit_progress(index, total, converted, skipped, failed)
            emit_phase_progress(index, total, f"{index} / {total} DDS files")
    except RunCancelled as exc:
        cancelled = True
        emit_log(str(exc))

    if csv_log_path:
        write_csv_log(csv_log_path, results)
        emit_log(f"CSV log written to: {csv_log_path}")

    return RunSummary(
        total_files=total,
        converted=converted,
        skipped=skipped,
        failed=failed,
        cancelled=cancelled,
        log_csv_path=csv_log_path,
        results=results,
    )


def write_csv_log(log_path: Path, results: Sequence[JobResult]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "original_dds",
                "png",
                "output_dir",
                "width",
                "height",
                "original_mips",
                "used_mips",
                "texconv_format",
                "status",
                "note",
            ],
        )
        writer.writeheader()
        for row in results:
            writer.writerow(row.__dict__)


def rebuild_dds_files(
    config: AppConfig,
    *,
    on_log: Optional[Callable[[str], None]] = None,
    on_total: Optional[Callable[[int], None]] = None,
    on_current_file: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, int, int, int], None]] = None,
    on_phase: Optional[Callable[[str, str, bool], None]] = None,
    on_phase_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> RunSummary:
    normalized = normalize_config(config)
    normalized.output_root.mkdir(parents=True, exist_ok=True)
    active_png_root = normalized.png_root

    def emit_log(message: str) -> None:
        if on_log:
            on_log(message)

    def emit_progress(processed: int, total: int, converted: int, skipped: int, failed: int) -> None:
        if on_progress:
            on_progress(processed, total, converted, skipped, failed)

    def emit_phase(name: str, detail: str, indeterminate: bool) -> None:
        if on_phase:
            on_phase(name, detail, indeterminate)

    def emit_phase_progress(current: int, total: int, detail: str) -> None:
        if on_phase_progress:
            on_phase_progress(current, total, detail)

    emit_log(
        "Build configuration: "
        f"chaiNNer={'enabled' if normalized.enable_chainner else 'disabled'}, "
        f"dds_staging={'enabled' if normalized.enable_dds_staging else 'disabled'}, "
        f"incremental_resume={'enabled' if normalized.enable_incremental_resume else 'disabled'}, "
        f"dry_run={'on' if normalized.dry_run else 'off'}, "
        f"dds_format_mode={normalized.dds_format_mode}, "
        f"dds_size_mode={normalized.dds_size_mode}, "
        f"dds_mip_mode={normalized.dds_mip_mode}, "
        f"overwrite_existing_dds={'on' if normalized.overwrite_existing_dds else 'off'}."
    )
    if normalized.enable_chainner:
        emit_log(f"chaiNNer executable: {normalized.chainner_exe_path}")
        emit_log(f"chaiNNer chain: {normalized.chainner_chain_path}")
    else:
        emit_log("chaiNNer stage is disabled, so the app will rebuild DDS from the existing PNG root.")

    emit_log("Scanning DDS files...")
    dds_files = collect_dds_files(
        normalized.original_dds_root,
        normalized.include_filter_patterns,
        stop_event=stop_event,
    )
    total = len(dds_files)
    if total == 0:
        raise ValueError("No DDS files were found under the original root with the current filter.")

    emit_log(f"Found {total} DDS files matching the current filter.")
    if on_total:
        on_total(total)
    emit_progress(0, total, 0, 0, 0)

    chain_analysis = analyze_chainner_chain(normalized.chainner_chain_path, normalized) if normalized.enable_chainner and normalized.chainner_chain_path else None
    for line in build_preflight_report_lines(
        normalized,
        dds_files,
        chain_analysis=chain_analysis,
        texture_rules=normalized.texture_rules,
    ):
        emit_log(line)

    if normalized.enable_dds_staging:
        stage_dds_to_pngs(
            normalized,
            dds_files,
            on_log=on_log,
            on_phase=on_phase,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )
        if not normalized.enable_chainner and normalized.dds_staging_root is not None:
            active_png_root = normalized.dds_staging_root

    if normalized.enable_chainner:
        run_chainner_stage(
            normalized,
            expected_output_total=total,
            on_log=on_log,
            on_phase=on_phase,
            on_phase_progress=on_phase_progress,
            on_current_file=on_current_file,
            stop_event=stop_event,
        )

    emit_phase("DDS Rebuild", "Indexing PNG files...", False)
    emit_phase_progress(0, 0, "Indexing PNG files...")
    emit_log("Indexing PNG files...")
    relative_png_index, basename_png_index, png_count = find_png_matches(
        active_png_root,
        stop_event=stop_event,
    )
    emit_log(f"Indexed {png_count} PNG files.")
    if normalized.enable_chainner and png_count == 0:
        chain_analysis = chain_analysis or ChainnerChainAnalysis()
        detail = ""
        if chain_analysis.warnings:
            detail = " " + " | ".join(chain_analysis.warnings[:3])
        raise ValueError(
            "chaiNNer finished, but no PNG files were found in the configured PNG root. "
            "The chain likely still points at old folders or writes somewhere else."
            + detail
        )
    emit_phase_progress(0, total, f"0 / {total} DDS files")
    emit_log(f"Found {total} DDS files to process.")
    emit_phase("DDS Rebuild", "Converting PNG files to DDS...", False)

    results: List[JobResult] = []
    converted = 0
    skipped = 0
    failed = 0
    cancelled = False
    manifest_entries: Dict[str, Dict[str, object]] = {}
    manifest_path: Optional[Path] = None
    if normalized.enable_incremental_resume:
        manifest_path = build_manifest_path(normalized.output_root)
        manifest_entries = load_incremental_manifest(manifest_path)
        emit_log(f"Incremental manifest: {manifest_path}")

    try:
        for index, dds_path in enumerate(dds_files, start=1):
            raise_if_cancelled(stop_event)

            rel_path = dds_path.relative_to(normalized.original_dds_root)
            rel_display = rel_path.as_posix()
            target_dir = normalized.output_root / rel_path.parent
            target_file = normalized.output_root / rel_path

            if on_current_file:
                on_current_file(rel_display)
            emit_progress(index - 1, total, converted, skipped, failed)
            emit_phase_progress(index - 1, total, f"{index - 1} / {total} DDS files")

            png_path, match_note = resolve_png(
                rel_path,
                relative_png_index,
                basename_png_index,
                normalized.allow_unique_basename_fallback,
            )

            if png_path is None:
                skipped += 1
                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png="",
                        output_dir=str(target_dir),
                        width=0,
                        height=0,
                        original_mips=0,
                        used_mips=0,
                        texconv_format="",
                        status="skipped",
                        note=match_note,
                    )
                )
                emit_log(f"[{index}/{total}] SKIP {rel_display} -> {match_note}")
                emit_progress(index, total, converted, skipped, failed)
                emit_phase_progress(index, total, f"{index} / {total} DDS files")
                continue

            try:
                dds_info = parse_dds(dds_path)
                png_width, png_height = read_png_dimensions(png_path)
                notes = [match_note]
                output_settings = resolve_dds_output_settings(normalized, dds_info, png_width, png_height)
                rule = find_matching_texture_rule(rel_path, normalized.texture_rules)
                if rule is not None:
                    updated_settings, rule_message = apply_texture_rule_to_output_settings(output_settings, rule)
                    if updated_settings is None:
                        skipped += 1
                        note = "; ".join(notes + [rule_message])
                        results.append(
                            JobResult(
                                original_dds=str(dds_path),
                                png=str(png_path),
                                output_dir=str(target_dir),
                                width=0,
                                height=0,
                                original_mips=dds_info.mip_count,
                                used_mips=0,
                                texconv_format="",
                                status="skipped",
                                note=note,
                            )
                        )
                        emit_log(f"[{index}/{total}] SKIP {rel_display} -> {rule_message}")
                        emit_progress(index, total, converted, skipped, failed)
                        emit_phase_progress(index, total, f"{index} / {total} DDS files")
                        continue
                    output_settings = updated_settings
                notes.extend(output_settings.notes)

                if manifest_path is not None and manifest_entry_matches(
                    manifest_entries.get(rel_path.as_posix(), {}),
                    dds_path,
                    png_path,
                    target_file,
                    output_settings,
                ):
                    skipped += 1
                    note = "; ".join(notes + ["unchanged output detected by incremental manifest"])
                    results.append(
                        JobResult(
                            original_dds=str(dds_path),
                            png=str(png_path),
                            output_dir=str(target_dir),
                            width=output_settings.width,
                            height=output_settings.height,
                            original_mips=dds_info.mip_count,
                            used_mips=output_settings.mip_count,
                            texconv_format=output_settings.texconv_format,
                            status="skipped",
                            note=note,
                        )
                    )
                    emit_log(f"[{index}/{total}] SKIP {rel_display} -> unchanged output detected by incremental manifest")
                    emit_progress(index, total, converted, skipped, failed)
                    emit_phase_progress(index, total, f"{index} / {total} DDS files")
                    continue

                if target_file.exists() and not normalized.overwrite_existing_dds:
                    note = "output DDS already exists and overwrite is disabled"
                    skipped += 1
                    results.append(
                        JobResult(
                            original_dds=str(dds_path),
                            png=str(png_path),
                            output_dir=str(target_dir),
                            width=output_settings.width,
                            height=output_settings.height,
                            original_mips=dds_info.mip_count,
                            used_mips=output_settings.mip_count,
                            texconv_format=output_settings.texconv_format,
                            status="skipped",
                            note=note,
                        )
                    )
                    emit_log(f"[{index}/{total}] SKIP {rel_display} -> {note}")
                    emit_progress(index, total, converted, skipped, failed)
                    emit_phase_progress(index, total, f"{index} / {total} DDS files")
                    continue

                target_dir.mkdir(parents=True, exist_ok=True)
                cmd = build_texconv_command(
                    texconv_path=normalized.texconv_path,
                    png_path=png_path,
                    output_dir=target_dir,
                    fmt=output_settings.texconv_format,
                    mips=output_settings.mip_count,
                    resize_width=output_settings.width if output_settings.resize_to_dimensions else None,
                    resize_height=output_settings.height if output_settings.resize_to_dimensions else None,
                    overwrite_existing_dds=normalized.overwrite_existing_dds,
                )

                action = "DRYRUN" if normalized.dry_run else "BUILD"
                emit_log(
                    f"[{index}/{total}] {action} {rel_display} "
                    f"-> format={output_settings.texconv_format} mips={output_settings.mip_count} "
                    f"output={output_settings.width}x{output_settings.height} png={png_width}x{png_height}"
                )

                if normalized.dry_run:
                    converted += 1
                    status = "dry-run"
                    note = "; ".join(notes)
                else:
                    return_code, stdout, stderr = run_process_with_cancellation(cmd, stop_event=stop_event)
                    if return_code != 0:
                        failed += 1
                        status = "failed"
                        detail = stderr.strip() or stdout.strip() or f"texconv failed with exit code {return_code}"
                        notes.append(detail)
                        note = "; ".join(notes)
                    else:
                        converted += 1
                        status = "converted"
                        note = "; ".join(notes)
                        if manifest_path is not None and target_file.exists():
                            manifest_entries[rel_path.as_posix()] = build_incremental_manifest_entry(
                                dds_path,
                                png_path,
                                target_file,
                                output_settings,
                            )
                            save_incremental_manifest(manifest_path, manifest_entries)

                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png=str(png_path),
                        output_dir=str(target_dir),
                        width=output_settings.width,
                        height=output_settings.height,
                        original_mips=dds_info.mip_count,
                        used_mips=output_settings.mip_count,
                        texconv_format=output_settings.texconv_format,
                        status=status,
                        note=note,
                    )
                )
            except RunCancelled:
                raise
            except Exception as exc:
                failed += 1
                results.append(
                    JobResult(
                        original_dds=str(dds_path),
                        png=str(png_path),
                        output_dir=str(target_dir),
                        width=0,
                        height=0,
                        original_mips=0,
                        used_mips=0,
                        texconv_format="",
                        status="failed",
                        note=str(exc),
                    )
                )
                emit_log(f"[{index}/{total}] FAIL {rel_display} -> {exc}")

            emit_progress(index, total, converted, skipped, failed)
            emit_phase_progress(index, total, f"{index} / {total} DDS files")
    except RunCancelled as exc:
        cancelled = True
        emit_log(str(exc))

    if normalized.csv_log_path:
        write_csv_log(normalized.csv_log_path, results)
        emit_log(f"CSV log written to: {normalized.csv_log_path}")

    return RunSummary(
        total_files=total,
        converted=converted,
        skipped=skipped,
        failed=failed,
        cancelled=cancelled,
        log_csv_path=normalized.csv_log_path,
        results=results,
    )


def run_cli(config: Optional[AppConfig] = None) -> int:
    active_config = config or default_config()

    def on_log(message: str) -> None:
        print(message)

    def on_total(total: int) -> None:
        print(f"Total DDS files found: {total}")

    try:
        summary = rebuild_dds_files(
            active_config,
            on_log=on_log,
            on_total=on_total,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("")
    print("Done.")
    print(f"Total DDS files: {summary.total_files}")
    print(f"Converted / planned: {summary.converted}")
    print(f"Skipped: {summary.skipped}")
    print(f"Failed: {summary.failed}")
    if summary.log_csv_path:
        print(f"CSV log: {summary.log_csv_path}")

    if summary.cancelled:
        return 1
    return 0 if summary.failed == 0 else 2

