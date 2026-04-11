from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from crimson_texture_forge.constants import ARCHIVE_IMAGE_EXTENSIONS
from crimson_texture_forge.core.archive import ArchiveEntry, archive_entry_role, read_archive_entry_data
from crimson_texture_forge.core.common import raise_if_cancelled
from crimson_texture_forge.core.pipeline import (
    collect_compare_relative_paths,
    collect_dds_files,
    ensure_dds_preview_png,
    max_mips_for_size,
    parse_dds,
)
from crimson_texture_forge.core.upscale_profiles import (
    derive_texture_group_key as derive_semantic_texture_group_key,
    infer_texture_semantics,
    is_png_intermediate_high_risk,
)

try:
    from PySide6.QtGui import QColor, QImage
except Exception:  # pragma: no cover - GUI/runtime fallback
    QImage = None  # type: ignore[assignment]
    QColor = None  # type: ignore[assignment]


TEXTURE_IMAGE_EXTENSIONS = {
    ".bmp",
    ".dds",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".png",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
}
TEXTURE_SIDECAR_EXTENSIONS = {
    ".material",
    ".shader",
    ".xml",
    ".json",
}
REFERENCE_SOURCE_EXTENSIONS = {
    ".cfg",
    ".ini",
    ".json",
    ".lua",
    ".material",
    ".shader",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXTURE_REFERENCE_PATTERN = re.compile(
    r"(?i)([A-Za-z0-9_./\\\\-]+\.(?:dds|png|tga|jpg|jpeg|bmp|gif|tiff?|webp|hdr))"
)
NORMAL_FRIENDLY_FORMATS = {
    "BC5_UNORM",
    "BC5_SNORM",
    "BC7_UNORM",
    "R8G8B8A8_UNORM",
    "B8G8R8A8_UNORM",
}
NORMAL_SUSPICIOUS_FORMATS = {
    "BC1_UNORM",
    "BC1_UNORM_SRGB",
    "BC2_UNORM",
    "BC2_UNORM_SRGB",
    "BC3_UNORM_SRGB",
    "BC7_UNORM_SRGB",
}
REGEX_PRESET_DEFAULT_EXTENSIONS = ".xml;.json;.cfg;.ini;.lua;.material;.shader"

_SYSTEM_AREA_RULES: Tuple[Tuple[str, str], ...] = (
    ("ui", "ui"),
    ("ui", "icon"),
    ("ui", "hud"),
    ("ui", "menu"),
    ("ui", "widget"),
    ("sound", "sound"),
    ("sound", "voice"),
    ("sound", "dialog"),
    ("gameplay", "gameplay"),
    ("gameplay", "quest"),
    ("gameplay", "skill"),
    ("gameplay", "actor"),
    ("gameplay", "npc"),
    ("gameplay", "battle"),
    ("materials", "material"),
    ("materials", "renderpass"),
    ("materials", "shader"),
    ("materials", "effect"),
    ("textures", "texture"),
    ("textures", "impostor"),
    ("textures", "decal"),
    ("textures", "atlas"),
    ("world", "object"),
    ("world", "interior"),
    ("world", "gimmick"),
    ("world", "nature"),
    ("character", "character"),
    ("character", "head"),
    ("character", "body"),
    ("animation", "anim"),
    ("animation", "motion"),
    ("animation", "hkx"),
)


@dataclass(slots=True)
class DependencyEdge:
    left: str
    right: str
    package_count: int
    example_packages: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TextureClassificationRow:
    path: str
    package_label: str
    texture_type: str
    confidence: int
    reason: str
    group_key: str


@dataclass(slots=True)
class TextureSetMember:
    path: str
    package_label: str
    member_kind: str
    extension: str


@dataclass(slots=True)
class TextureSetGroup:
    group_key: str
    display_name: str
    member_count: int
    package_labels: List[str]
    member_kinds: List[str]
    members: List[TextureSetMember] = field(default_factory=list)


@dataclass(slots=True)
class MipAnalysisRow:
    relative_path: str
    original_format: str
    rebuilt_format: str
    original_size: str
    rebuilt_size: str
    original_mips: int
    rebuilt_mips: int
    warning_count: int
    warnings: List[str] = field(default_factory=list)


@dataclass(slots=True)
class NormalValidationRow:
    path: str
    root_label: str
    texconv_format: str
    size_text: str
    issue_count: int
    issues: List[str] = field(default_factory=list)


@dataclass(slots=True)
class AtlasDetectionRow:
    path: str
    root_label: str
    size_text: str
    score: int
    signals: List[str] = field(default_factory=list)


@dataclass(slots=True)
class TexturePreviewStats:
    path: str
    width: int
    height: int
    sample_count: int
    has_alpha: bool
    mean_r: float
    mean_g: float
    mean_b: float
    mean_a: float
    min_r: int
    min_g: int
    min_b: int
    min_a: int
    max_r: int
    max_g: int
    max_b: int
    max_a: int
    luma_mean: float
    luma_min: float
    luma_max: float
    opaque_fraction: float
    transparent_fraction: float


@dataclass(slots=True)
class RegexPreset:
    category: str
    name: str
    pattern: str
    description: str
    extensions: str = REGEX_PRESET_DEFAULT_EXTENSIONS
    path_hint: str = ""


@dataclass(slots=True)
class SearchCluster:
    mode: str
    label: str
    file_count: int
    total_matches: int
    sample_paths: List[str] = field(default_factory=list)


@dataclass(slots=True)
class MaterialTextureReferenceRow:
    source_path: str
    source_package_label: str
    related_path: str
    related_package_label: str
    relation_kind: str
    match_count: int
    snippet: str


@dataclass(slots=True)
class SidecarDiscoveryRow:
    anchor_path: str
    related_path: str
    package_label: str
    relation_kind: str
    confidence: int
    reason: str


@dataclass(slots=True)
class TextureUsageHeatRow:
    scope: str
    label: str
    texture_count: int
    set_count: int
    normal_count: int
    ui_count: int
    material_count: int
    impostor_count: int
    heat_score: int
    sample_paths: List[str] = field(default_factory=list)


@dataclass(slots=True)
class ResearchNote:
    target_key: str
    source_kind: str
    tags: List[str]
    note: str
    updated_at: str


def _normalized_parts(path_value: str) -> Tuple[str, ...]:
    return tuple(part for part in PurePosixPath(path_value.replace("\\", "/")).parts if part)


def system_area_from_path(path_value: str) -> str:
    lowered = path_value.replace("\\", "/").lower()
    for area, token in _SYSTEM_AREA_RULES:
        if f"/{token}/" in lowered or lowered.startswith(f"{token}/") or token in lowered.split("/")[0]:
            return area
    parts = _normalized_parts(path_value)
    if not parts:
        return "other"
    head = parts[0].lower()
    return {
        "object": "world",
        "character": "character",
        "sound": "sound",
        "material": "materials",
        "ui": "ui",
    }.get(head, head if len(head) <= 16 else "other")


def _package_bucket_for_path(path_value: str) -> str:
    parts = _normalized_parts(path_value)
    if not parts:
        return "other"
    prefix = "/".join(parts[:2]) if len(parts) >= 2 else "/".join(parts)
    return f"{system_area_from_path(path_value)} :: {prefix}"


def build_archive_dependency_graph(entries: Sequence[ArchiveEntry], *, top_n: int = 120) -> List[DependencyEdge]:
    packages: Dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        packages[entry.package_label].add(_package_bucket_for_path(entry.path))

    pair_counts: Counter[Tuple[str, str]] = Counter()
    pair_examples: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for package_label, bucket_set in packages.items():
        if len(bucket_set) < 2:
            continue
        for left, right in combinations(sorted(bucket_set), 2):
            pair = (left, right)
            pair_counts[pair] += 1
            if len(pair_examples[pair]) < 3:
                pair_examples[pair].append(package_label)

    edges = [
        DependencyEdge(
            left=left,
            right=right,
            package_count=count,
            example_packages=pair_examples[(left, right)],
        )
        for (left, right), count in pair_counts.most_common(top_n)
    ]
    return edges


def classify_texture_path(
    path_value: str,
    *,
    role_hint: str = "",
    family_members: Sequence[str] = (),
) -> Tuple[str, int, str]:
    if role_hint == "ui":
        return "ui", 92, "archive role marked as UI"
    if role_hint == "impostor":
        return "impostor", 96, "archive role marked as impostor"
    semantic = infer_texture_semantics(path_value, family_members=family_members)
    if semantic.texture_type != "unknown":
        reason = semantic.evidence[0] if semantic.evidence else "semantic inference"
        return semantic.texture_type, semantic.confidence, reason
    if role_hint == "normal":
        return "normal", 72, "archive role marked as normal-like companion map"
    if role_hint == "material":
        return "mask", 58, "archive role marked as technical/material companion map"
    lowered = path_value.replace("\\", "/").lower()
    if "/texture/" in lowered or Path(lowered).suffix.lower() in TEXTURE_IMAGE_EXTENSIONS:
        return "unknown", 45, "image/texture path without a stronger semantic hint"
    return "unknown", 25, "no strong texture-type hint"


def derive_texture_group_key(path_value: str) -> str:
    return derive_semantic_texture_group_key(path_value)


def build_archive_research_snapshot(
    entries: Sequence[ArchiveEntry],
    *,
    classification_limit: int = 3000,
    group_limit: int = 2000,
    heatmap_limit_per_scope: int = 24,
) -> Dict[str, object]:
    family_members_by_group: Dict[str, List[str]] = defaultdict(list)
    grouped_entries: Dict[str, List[ArchiveEntry]] = defaultdict(list)
    entry_metadata: List[Tuple[ArchiveEntry, str, bool, bool, str, str]] = []

    for entry in entries:
        normalized_path = entry.path.replace("\\", "/")
        lowered = normalized_path.lower()
        is_texture = entry.extension in TEXTURE_IMAGE_EXTENSIONS or "/texture/" in lowered
        is_sidecar = entry.extension in TEXTURE_SIDECAR_EXTENSIONS
        group_key = derive_texture_group_key(normalized_path)
        if is_texture:
            family_members_by_group[group_key].append(normalized_path)
        if is_texture or is_sidecar:
            grouped_entries[group_key].append(entry)
        entry_metadata.append((entry, normalized_path, is_texture, is_sidecar, lowered, group_key))

    classification_rows: List[TextureClassificationRow] = []
    classified_kinds_by_path: Dict[str, str] = {}
    heatmap_scopes: Dict[Tuple[str, str], Dict[str, object]] = {}

    for entry, normalized_path, is_texture, is_sidecar, lowered, group_key in entry_metadata:
        if not is_texture and not is_sidecar:
            continue

        family_members = tuple(family_members_by_group.get(group_key, ()))
        role_hint = archive_entry_role(entry)
        texture_type, confidence, reason = classify_texture_path(
            normalized_path,
            role_hint=role_hint,
            family_members=family_members,
        )
        classified_kinds_by_path[lowered] = texture_type

        if is_texture:
            classification_rows.append(
                TextureClassificationRow(
                    path=entry.path,
                    package_label=entry.package_label,
                    texture_type=texture_type,
                    confidence=confidence,
                    reason=reason,
                    group_key=group_key,
                )
            )

        parts = _normalized_parts(normalized_path)
        folder_label = "/".join(parts[:3]) if len(parts) >= 3 else ("/".join(parts) or entry.package_label)
        scope_labels = (
            ("System Area", system_area_from_path(normalized_path)),
            ("Folder", folder_label),
            ("Package", entry.package_label),
        )

        for scope_name, label in scope_labels:
            bucket = heatmap_scopes.setdefault(
                (scope_name, label),
                {
                    "texture_count": 0,
                    "set_keys": set(),
                    "normal_count": 0,
                    "ui_count": 0,
                    "material_count": 0,
                    "impostor_count": 0,
                    "sample_paths": [],
                },
            )
            if is_texture:
                bucket["texture_count"] += 1
                bucket["set_keys"].add(group_key)
                if texture_type == "normal":
                    bucket["normal_count"] += 1
                if texture_type == "ui":
                    bucket["ui_count"] += 1
                if texture_type == "impostor":
                    bucket["impostor_count"] += 1
            if is_sidecar:
                bucket["material_count"] += 1
            sample_paths = bucket["sample_paths"]
            if len(sample_paths) < 3:
                sample_paths.append(entry.path)

    classification_rows.sort(key=lambda row: (-row.confidence, row.texture_type, row.path))

    texture_groups: List[TextureSetGroup] = []
    for group_key, entry_members in grouped_entries.items():
        if len(entry_members) < 2:
            continue
        members: List[TextureSetMember] = []
        for entry in sorted(entry_members, key=lambda member: member.path):
            lowered = entry.path.replace("\\", "/").lower()
            member_kind = classified_kinds_by_path.get(lowered, "unknown")
            if entry.extension in TEXTURE_SIDECAR_EXTENSIONS and member_kind == "unknown":
                member_kind = "sidecar"
            members.append(
                TextureSetMember(
                    path=entry.path,
                    package_label=entry.package_label,
                    member_kind=member_kind,
                    extension=entry.extension,
                )
            )
        package_labels = sorted({member.package_label for member in members})
        member_kinds = sorted({member.member_kind for member in members})
        texture_groups.append(
            TextureSetGroup(
                group_key=group_key,
                display_name=PurePosixPath(group_key).name or group_key,
                member_count=len(members),
                package_labels=package_labels,
                member_kinds=member_kinds,
                members=members,
            )
        )
    texture_groups.sort(key=lambda group: (-group.member_count, group.display_name))

    heatmap_rows: List[TextureUsageHeatRow] = []
    for (scope_name, label), bucket in heatmap_scopes.items():
        set_count = len(bucket["set_keys"])
        heat_score = (
            int(bucket["texture_count"])
            + (set_count * 2)
            + (int(bucket["normal_count"]) * 2)
            + (int(bucket["ui_count"]) * 2)
            + int(bucket["material_count"])
            + (int(bucket["impostor_count"]) * 2)
        )
        heatmap_rows.append(
            TextureUsageHeatRow(
                scope=scope_name,
                label=label,
                texture_count=int(bucket["texture_count"]),
                set_count=set_count,
                normal_count=int(bucket["normal_count"]),
                ui_count=int(bucket["ui_count"]),
                material_count=int(bucket["material_count"]),
                impostor_count=int(bucket["impostor_count"]),
                heat_score=heat_score,
                sample_paths=list(bucket["sample_paths"]),
            )
        )

    grouped_heatmap_rows: Dict[str, List[TextureUsageHeatRow]] = defaultdict(list)
    for row in heatmap_rows:
        grouped_heatmap_rows[row.scope].append(row)

    flattened_heatmap_rows: List[TextureUsageHeatRow] = []
    for scope_name in ("System Area", "Folder", "Package"):
        scope_rows = sorted(
            grouped_heatmap_rows.get(scope_name, []),
            key=lambda row: (-row.heat_score, -row.texture_count, row.label.lower()),
        )
        flattened_heatmap_rows.extend(scope_rows[:heatmap_limit_per_scope])

    return {
        "classification_rows": classification_rows[:classification_limit],
        "texture_groups": texture_groups[:group_limit],
        "heatmap_rows": flattened_heatmap_rows,
    }


def classify_texture_entries(entries: Sequence[ArchiveEntry], *, limit: int = 3000) -> List[TextureClassificationRow]:
    snapshot = build_archive_research_snapshot(entries, classification_limit=limit, group_limit=0)
    rows = snapshot.get("classification_rows", [])
    return rows if isinstance(rows, list) else []


def _build_family_members_by_relative_path(paths: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    for path_value in paths:
        grouped[derive_texture_group_key(path_value)].append(path_value)

    family_members: Dict[str, Tuple[str, ...]] = {}
    for members in grouped.values():
        ordered = tuple(sorted(dict.fromkeys(members), key=str.lower))
        for member in ordered:
            family_members[member] = ordered
    return family_members


def bundle_texture_sets(entries: Sequence[ArchiveEntry], *, limit: int = 2000) -> List[TextureSetGroup]:
    snapshot = build_archive_research_snapshot(entries, classification_limit=0, group_limit=limit)
    groups = snapshot.get("texture_groups", [])
    return groups if isinstance(groups, list) else []


def _decode_reference_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16-le", "cp1252"):
        try:
            return data.decode(encoding, errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        except Exception:
            continue
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _normalize_reference_token(token: str) -> str:
    normalized = token.strip().strip("'\"").replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _extract_texture_reference_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    seen: set[str] = set()
    for match in TEXTURE_REFERENCE_PATTERN.finditer(text):
        normalized = _normalize_reference_token(match.group(1))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        tokens.append(normalized)
    return tokens


def _tail_path_key(path_value: str, depth: int) -> str:
    parts = _normalized_parts(path_value)
    if len(parts) < depth:
        return ""
    return "/".join(parts[-depth:]).lower()


def _build_texture_reference_indexes(
    entries: Sequence[ArchiveEntry],
) -> Tuple[
    Dict[str, ArchiveEntry],
    Dict[str, List[ArchiveEntry]],
    Dict[str, List[ArchiveEntry]],
    Dict[str, List[ArchiveEntry]],
]:
    by_path: Dict[str, ArchiveEntry] = {}
    by_basename: Dict[str, List[ArchiveEntry]] = defaultdict(list)
    by_tail2: Dict[str, List[ArchiveEntry]] = defaultdict(list)
    by_tail3: Dict[str, List[ArchiveEntry]] = defaultdict(list)
    for entry in entries:
        lowered = entry.path.replace("\\", "/").lower()
        if entry.extension not in TEXTURE_IMAGE_EXTENSIONS and "/texture/" not in lowered:
            continue
        by_path[lowered] = entry
        by_basename[PurePosixPath(lowered).name].append(entry)
        tail2 = _tail_path_key(lowered, 2)
        tail3 = _tail_path_key(lowered, 3)
        if tail2:
            by_tail2[tail2].append(entry)
        if tail3:
            by_tail3[tail3].append(entry)
    return by_path, by_basename, by_tail2, by_tail3


def _resolve_texture_reference_token(
    token: str,
    *,
    by_path: Dict[str, ArchiveEntry],
    by_basename: Dict[str, List[ArchiveEntry]],
    by_tail2: Dict[str, List[ArchiveEntry]],
    by_tail3: Dict[str, List[ArchiveEntry]],
) -> Tuple[List[ArchiveEntry], str]:
    normalized = _normalize_reference_token(token)
    if not normalized:
        return [], "unresolved"
    exact = by_path.get(normalized)
    if exact is not None:
        return [exact], "exact path"
    tail3 = _tail_path_key(normalized, 3)
    if tail3 and len(by_tail3.get(tail3, ())) == 1:
        return list(by_tail3[tail3]), "tail path"
    tail2 = _tail_path_key(normalized, 2)
    if tail2 and len(by_tail2.get(tail2, ())) == 1:
        return list(by_tail2[tail2]), "tail path"
    basename = PurePosixPath(normalized).name
    basename_matches = by_basename.get(basename, [])
    if len(basename_matches) == 1:
        return list(basename_matches), "unique basename"
    return [], "unresolved"


def _build_reference_snippet(text: str, token: str, *, radius: int = 80) -> str:
    lowered_text = text.lower()
    lowered_token = token.lower()
    index = lowered_text.find(lowered_token)
    if index < 0:
        compact = re.sub(r"\s+", " ", text.strip())
        return compact[: (radius * 2)] if compact else ""
    start = max(0, index - radius)
    end = min(len(text), index + len(token) + radius)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def resolve_material_texture_references(
    entries: Sequence[ArchiveEntry],
    target_path: str,
    *,
    limit: int = 240,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    stop_event: Optional[object] = None,
) -> Tuple[List[MaterialTextureReferenceRow], Dict[str, object]]:
    normalized_target = target_path.strip().replace("\\", "/").strip("/")
    if not normalized_target:
        return [], {"mode": "none", "searched_count": 0, "candidate_count": 0, "unreadable_count": 0}

    lowered_target = normalized_target.lower()
    all_entries_by_path = {entry.path.replace("\\", "/").lower(): entry for entry in entries}
    text_entries = [
        entry
        for entry in entries
        if entry.extension in REFERENCE_SOURCE_EXTENSIONS
    ]
    by_path, by_basename, by_tail2, by_tail3 = _build_texture_reference_indexes(entries)
    target_entry = all_entries_by_path.get(lowered_target)

    if target_entry is not None and target_entry.extension in REFERENCE_SOURCE_EXTENSIONS:
        rows: List[MaterialTextureReferenceRow] = []
        unreadable_count = 0
        seen_related: set[str] = set()
        if on_progress:
            on_progress(0, 1, f"Resolving outbound texture references from {target_entry.path}")
        try:
            data, _decompressed, _note = read_archive_entry_data(target_entry)
            text = _decode_reference_text(data)
        except Exception:
            unreadable_count = 1
            return [], {
                "mode": "outbound",
                "searched_count": 0,
                "candidate_count": 1,
                "unreadable_count": unreadable_count,
            }
        for token in _extract_texture_reference_tokens(text):
            resolved_entries, resolution_kind = _resolve_texture_reference_token(
                token,
                by_path=by_path,
                by_basename=by_basename,
                by_tail2=by_tail2,
                by_tail3=by_tail3,
            )
            for related_entry in resolved_entries:
                lowered_related = related_entry.path.replace("\\", "/").lower()
                if lowered_related in seen_related:
                    continue
                seen_related.add(lowered_related)
                rows.append(
                    MaterialTextureReferenceRow(
                        source_path=target_entry.path,
                        source_package_label=target_entry.package_label,
                        related_path=related_entry.path,
                        related_package_label=related_entry.package_label,
                        relation_kind=f"references texture ({resolution_kind})",
                        match_count=max(1, text.lower().count(token.lower())),
                        snippet=_build_reference_snippet(text, token),
                    )
                )
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        rows.sort(key=lambda row: (row.related_path.lower(), row.relation_kind))
        return rows, {
            "mode": "outbound",
            "searched_count": 1,
            "candidate_count": 1,
            "unreadable_count": unreadable_count,
        }

    rows = []
    unreadable_count = 0
    target_basename = PurePosixPath(lowered_target).name
    total = len(text_entries)
    for index, entry in enumerate(text_entries, start=1):
        raise_if_cancelled(stop_event)
        if on_progress:
            on_progress(index - 1, total, f"Searching material/sidecar references in {entry.path}")
        try:
            data, _decompressed, _note = read_archive_entry_data(entry)
            text = _decode_reference_text(data)
        except Exception:
            unreadable_count += 1
            continue
        lowered_text = text.lower()
        if lowered_target not in lowered_text and target_basename and target_basename not in lowered_text:
            continue
        match_count = 0
        match_kind = ""
        snippet = ""
        for token in _extract_texture_reference_tokens(text):
            resolved_entries, resolution_kind = _resolve_texture_reference_token(
                token,
                by_path=by_path,
                by_basename=by_basename,
                by_tail2=by_tail2,
                by_tail3=by_tail3,
            )
            resolved_paths = {resolved.path.replace("\\", "/").lower() for resolved in resolved_entries}
            if lowered_target in resolved_paths:
                match_count += 1
                match_kind = resolution_kind
                if not snippet:
                    snippet = _build_reference_snippet(text, token)
            elif target_entry is None and PurePosixPath(token).name.lower() == target_basename:
                match_count += 1
                match_kind = "basename match"
                if not snippet:
                    snippet = _build_reference_snippet(text, token)
        if match_count <= 0:
            continue
        rows.append(
            MaterialTextureReferenceRow(
                source_path=entry.path,
                source_package_label=entry.package_label,
                related_path=normalized_target,
                related_package_label=target_entry.package_label if target_entry is not None else "",
                relation_kind=f"references selected texture ({match_kind or 'text match'})",
                match_count=match_count,
                snippet=snippet,
            )
        )
        if len(rows) >= limit:
            break

    if on_progress:
        on_progress(total, total, f"Reference resolution complete. Found {len(rows):,} match(es).")
    rows.sort(key=lambda row: (-row.match_count, row.source_path.lower()))
    return rows, {
        "mode": "inbound",
        "searched_count": total - unreadable_count,
        "candidate_count": total,
        "unreadable_count": unreadable_count,
    }


def discover_archive_sidecars(
    entries: Sequence[ArchiveEntry],
    target_path: str,
    *,
    limit: int = 120,
    stop_event: Optional[object] = None,
) -> List[SidecarDiscoveryRow]:
    normalized_target = target_path.strip().replace("\\", "/").strip("/")
    if not normalized_target:
        return []
    lowered_target = normalized_target.lower()
    target_parts = _normalized_parts(lowered_target)
    target_parent = "/".join(target_parts[:-1])
    target_stem = PurePosixPath(lowered_target).stem.lower()
    target_group_key = derive_texture_group_key(lowered_target).lower()

    candidates: Dict[str, SidecarDiscoveryRow] = {}
    for entry in entries:
        raise_if_cancelled(stop_event)
        lowered_path = entry.path.replace("\\", "/").lower()
        if lowered_path == lowered_target:
            continue
        if entry.extension not in TEXTURE_IMAGE_EXTENSIONS and entry.extension not in TEXTURE_SIDECAR_EXTENSIONS:
            continue
        confidence = 0
        relation_kind = ""
        reason = ""
        entry_group_key = derive_texture_group_key(entry.path).lower()
        if entry_group_key == target_group_key:
            confidence = 96
            relation_kind = "same grouped set"
            reason = "Matches the same derived texture-set key."
        else:
            entry_parent = "/".join(_normalized_parts(lowered_path)[:-1])
            entry_stem = PurePosixPath(lowered_path).stem.lower()
            if entry_parent == target_parent and entry.extension in TEXTURE_SIDECAR_EXTENSIONS:
                if target_stem in entry_stem or entry_stem in target_stem:
                    confidence = 84
                    relation_kind = "same-folder sidecar"
                    reason = "Same folder with a matching or overlapping base stem."
            if confidence == 0 and entry_parent == target_parent and entry.extension in TEXTURE_IMAGE_EXTENSIONS:
                if target_stem in entry_stem or entry_stem in target_stem:
                    confidence = 74
                    relation_kind = "same-folder texture"
                    reason = "Nearby texture in the same folder with a similar base stem."
        if confidence <= 0:
            continue
        existing = candidates.get(lowered_path)
        if existing is not None and existing.confidence >= confidence:
            continue
        candidates[lowered_path] = SidecarDiscoveryRow(
            anchor_path=normalized_target,
            related_path=entry.path,
            package_label=entry.package_label,
            relation_kind=relation_kind,
            confidence=confidence,
            reason=reason,
        )

    rows = sorted(candidates.values(), key=lambda row: (-row.confidence, row.related_path.lower()))
    return rows[:limit]


def build_texture_usage_heatmap(
    entries: Sequence[ArchiveEntry],
    *,
    limit_per_scope: int = 24,
) -> List[TextureUsageHeatRow]:
    snapshot = build_archive_research_snapshot(
        entries,
        classification_limit=0,
        group_limit=0,
        heatmap_limit_per_scope=limit_per_scope,
    )
    rows = snapshot.get("heatmap_rows", [])
    return rows if isinstance(rows, list) else []


def export_texture_analysis_report(
    report_path: Path,
    mip_rows: Sequence[MipAnalysisRow],
    normal_rows: Sequence[NormalValidationRow],
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = report_path.suffix.lower()
    if suffix == ".json":
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mip_rows": [asdict(row) for row in mip_rows],
            "normal_rows": [asdict(row) for row in normal_rows],
        }
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return report_path

    with report_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "report_type",
            "path",
            "root",
            "original_format",
            "rebuilt_format",
            "original_size",
            "rebuilt_size",
            "original_mips",
            "rebuilt_mips",
            "format",
            "size",
            "issue_count",
            "summary",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in mip_rows:
            writer.writerow(
                {
                    "report_type": "mip",
                    "path": row.relative_path,
                    "original_format": row.original_format,
                    "rebuilt_format": row.rebuilt_format,
                    "original_size": row.original_size,
                    "rebuilt_size": row.rebuilt_size,
                    "original_mips": row.original_mips,
                    "rebuilt_mips": row.rebuilt_mips,
                    "issue_count": row.warning_count,
                    "summary": " | ".join(row.warnings),
                }
            )
        for row in normal_rows:
            writer.writerow(
                {
                    "report_type": "normal",
                    "path": row.path,
                    "root": row.root_label,
                    "format": row.texconv_format,
                    "size": row.size_text,
                    "issue_count": row.issue_count,
                    "summary": " | ".join(row.issues),
                }
            )
    return report_path


def _format_bytes(value: int) -> str:
    if value <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _format_percent(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def _collect_preview_stats(image_path: Path) -> Optional[TexturePreviewStats]:
    if QImage is None:
        return None
    image = QImage(str(image_path))
    if image.isNull():
        return None
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return None

    step_x = max(1, width // 64)
    step_y = max(1, height // 64)
    sample_count = 0
    sum_r = sum_g = sum_b = sum_a = 0.0
    sum_luma = 0.0
    min_r = min_g = min_b = min_a = 255
    max_r = max_g = max_b = max_a = 0
    min_luma = 255.0
    max_luma = 0.0
    opaque_count = 0
    transparent_count = 0
    has_alpha = bool(image.hasAlphaChannel())

    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            color = QColor(image.pixel(x, y))
            r = color.red()
            g = color.green()
            b = color.blue()
            a = color.alpha()
            luma = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
            sample_count += 1
            sum_r += r
            sum_g += g
            sum_b += b
            sum_a += a
            sum_luma += luma
            min_r = min(min_r, r)
            min_g = min(min_g, g)
            min_b = min(min_b, b)
            min_a = min(min_a, a)
            max_r = max(max_r, r)
            max_g = max(max_g, g)
            max_b = max(max_b, b)
            max_a = max(max_a, a)
            min_luma = min(min_luma, luma)
            max_luma = max(max_luma, luma)
            if a >= 250:
                opaque_count += 1
            if a <= 5:
                transparent_count += 1

    if sample_count <= 0:
        return None

    return TexturePreviewStats(
        path=str(image_path),
        width=width,
        height=height,
        sample_count=sample_count,
        has_alpha=has_alpha,
        mean_r=sum_r / sample_count,
        mean_g=sum_g / sample_count,
        mean_b=sum_b / sample_count,
        mean_a=sum_a / sample_count,
        min_r=min_r,
        min_g=min_g,
        min_b=min_b,
        min_a=min_a,
        max_r=max_r,
        max_g=max_g,
        max_b=max_b,
        max_a=max_a,
        luma_mean=sum_luma / sample_count,
        luma_min=min_luma,
        luma_max=max_luma,
        opaque_fraction=opaque_count / sample_count,
        transparent_fraction=transparent_count / sample_count,
    )


def _sample_image_channel_stats(image_path: Path) -> Optional[Dict[str, float]]:
    stats = _collect_preview_stats(image_path)
    if stats is None:
        return None
    return {
        "r": stats.mean_r,
        "g": stats.mean_g,
        "b": stats.mean_b,
        "a": stats.mean_a,
    }


def _preview_stats_summary(stats: Optional[TexturePreviewStats]) -> str:
    if stats is None:
        return "Preview statistics: unavailable."
    return (
        f"Preview {stats.width}x{stats.height}, sampled {stats.sample_count} px; "
        f"mean RGBA {stats.mean_r:.1f}/{stats.mean_g:.1f}/{stats.mean_b:.1f}/{stats.mean_a:.1f}; "
        f"range R {stats.min_r}-{stats.max_r}, G {stats.min_g}-{stats.max_g}, B {stats.min_b}-{stats.max_b}, A {stats.min_a}-{stats.max_a}; "
        f"luma {stats.luma_mean:.1f} (range {stats.luma_min:.1f}-{stats.luma_max:.1f}); "
        f"alpha opaque {_format_percent(stats.opaque_fraction)} / transparent {_format_percent(stats.transparent_fraction)}."
    )


def _compare_preview_stats(original: Optional[TexturePreviewStats], rebuilt: Optional[TexturePreviewStats]) -> List[str]:
    warnings: List[str] = []
    if original is None or rebuilt is None:
        if original is None and rebuilt is None:
            warnings.append("Preview statistics are unavailable for both files.")
        elif original is None:
            warnings.append("Original DDS preview could not be decoded for statistics.")
        else:
            warnings.append("Rebuilt DDS preview could not be decoded for statistics.")
        return warnings

    if original.has_alpha != rebuilt.has_alpha:
        warnings.append("Alpha-channel presence changed between original and rebuilt DDS.")

    alpha_delta = abs(original.opaque_fraction - rebuilt.opaque_fraction)
    if alpha_delta > 0.10:
        warnings.append(
            f"Alpha coverage changed by {_format_percent(alpha_delta)} between preview renders."
        )

    luma_delta = abs(original.luma_mean - rebuilt.luma_mean)
    if luma_delta > 12.0:
        warnings.append(f"Average brightness shifted by {luma_delta:.1f} luma points.")

    range_delta = abs((original.luma_max - original.luma_min) - (rebuilt.luma_max - rebuilt.luma_min))
    if range_delta > 18.0:
        warnings.append("Brightness range changed noticeably between original and rebuilt preview renders.")

    channel_deltas = {
        "R": abs(original.mean_r - rebuilt.mean_r),
        "G": abs(original.mean_g - rebuilt.mean_g),
        "B": abs(original.mean_b - rebuilt.mean_b),
        "A": abs(original.mean_a - rebuilt.mean_a),
    }
    if max(channel_deltas.values()) > 18.0:
        warnings.append(
            "Per-channel averages drifted: "
            + ", ".join(f"{name} {delta:.1f}" for name, delta in channel_deltas.items())
        )

    original_spans = {
        "R": original.max_r - original.min_r,
        "G": original.max_g - original.min_g,
        "B": original.max_b - original.min_b,
        "A": original.max_a - original.min_a,
    }
    rebuilt_spans = {
        "R": rebuilt.max_r - rebuilt.min_r,
        "G": rebuilt.max_g - rebuilt.min_g,
        "B": rebuilt.max_b - rebuilt.min_b,
        "A": rebuilt.max_a - rebuilt.min_a,
    }
    for channel in ("R", "G", "B", "A"):
        original_span = original_spans[channel]
        rebuilt_span = rebuilt_spans[channel]
        if original_span >= 16 and rebuilt_span <= max(4, original_span * 0.5):
            warnings.append(f"{channel} channel range collapsed in the rebuilt preview.")
            break

    if original.has_alpha and original.mean_a > 8 and rebuilt.mean_a <= 8:
        warnings.append("Original appears to use alpha, but the rebuilt preview is effectively opaque.")

    return warnings


def _dedupe_preserve_order(messages: Sequence[str]) -> List[str]:
    seen: set[str] = set()
    deduped: List[str] = []
    for message in messages:
        normalized = str(message).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _texture_specific_preview_warnings(
    relative_path: str,
    original: Optional[TexturePreviewStats],
    rebuilt: Optional[TexturePreviewStats],
    *,
    family_members: Sequence[str] = (),
) -> List[str]:
    if original is None or rebuilt is None:
        return []

    lowered = relative_path.lower()
    texture_type, _confidence, _reason = classify_texture_path(relative_path, family_members=family_members)
    warnings: List[str] = []

    if texture_type == "normal":
        if rebuilt.mean_b < original.mean_b - 12.0:
            warnings.append("Normal-map blue channel darkened noticeably in the rebuilt preview.")
        if abs(rebuilt.mean_r - original.mean_r) > 18.0 or abs(rebuilt.mean_g - original.mean_g) > 18.0:
            warnings.append("Normal-map red/green midpoint drifted noticeably in the rebuilt preview.")

    if texture_type == "mask" or any(token in lowered for token in ("_orm", "_rma", "_mra", "_mask", "_sp", "_ao", "_m.", "_ma", "_mg", "_o.", "_subsurface", "_emi")):
        original_channel_spread = max(original.mean_r, original.mean_g, original.mean_b) - min(original.mean_r, original.mean_g, original.mean_b)
        rebuilt_channel_spread = max(rebuilt.mean_r, rebuilt.mean_g, rebuilt.mean_b) - min(rebuilt.mean_r, rebuilt.mean_g, rebuilt.mean_b)
        if original_channel_spread >= 12.0 and rebuilt_channel_spread <= 4.0:
            warnings.append("Packed/mask channels appear flatter or more identical after rebuild.")
        if (
            abs(rebuilt.mean_r - rebuilt.mean_g) <= 2.0
            and abs(rebuilt.mean_g - rebuilt.mean_b) <= 2.0
            and original_channel_spread >= 10.0
        ):
            warnings.append("Packed/mask channels now look nearly identical; verify channel packing.")

    if any(token in lowered for token in ("_disp", "displacement", "_height", "_bump", "parallax", "_dmap", "_d.", "_d_", "_o.")):
        original_gray_spread = max(
            abs(original.mean_r - original.mean_g),
            abs(original.mean_g - original.mean_b),
            abs(original.mean_r - original.mean_b),
        )
        rebuilt_gray_spread = max(
            abs(rebuilt.mean_r - rebuilt.mean_g),
            abs(rebuilt.mean_g - rebuilt.mean_b),
            abs(rebuilt.mean_r - rebuilt.mean_b),
        )
        if original_gray_spread <= 6.0 and rebuilt_gray_spread >= 12.0:
            warnings.append("Grayscale technical map gained noticeable color drift in the rebuilt preview.")
        original_luma_range = original.luma_max - original.luma_min
        rebuilt_luma_range = rebuilt.luma_max - rebuilt.luma_min
        if original_luma_range >= 22.0 and rebuilt_luma_range <= original_luma_range * 0.60:
            warnings.append("Grayscale technical-map range compressed noticeably after rebuild.")

    if texture_type == "vector" or any(token in lowered for token in ("_dr", "_op", "_flow", "_velocity")):
        original_channel_spread = max(original.mean_r, original.mean_g, original.mean_b) - min(original.mean_r, original.mean_g, original.mean_b)
        rebuilt_channel_spread = max(rebuilt.mean_r, rebuilt.mean_g, rebuilt.mean_b) - min(rebuilt.mean_r, rebuilt.mean_g, rebuilt.mean_b)
        if original_channel_spread >= 12.0 and rebuilt_channel_spread <= 4.0:
            warnings.append("Vector/effect-map channels appear flatter after rebuild; verify directional data.")

    return warnings


def _compare_file_sizes(original_path: Path, rebuilt_path: Path) -> Tuple[str, List[str]]:
    original_size = original_path.stat().st_size if original_path.exists() else 0
    rebuilt_size = rebuilt_path.stat().st_size if rebuilt_path.exists() else 0
    if original_size <= 0 or rebuilt_size <= 0:
        return f"File sizes: { _format_bytes(original_size) } -> { _format_bytes(rebuilt_size) }", []
    ratio = rebuilt_size / max(1, original_size)
    summary = f"File sizes: {_format_bytes(original_size)} -> {_format_bytes(rebuilt_size)} ({ratio * 100.0:.1f}%)"
    warnings: List[str] = []
    if ratio < 0.70:
        warnings.append("Rebuilt DDS is substantially smaller than the original, which can indicate format or mip loss.")
    elif ratio > 1.50:
        warnings.append("Rebuilt DDS is substantially larger than the original, which can indicate format or mip growth.")
    return summary, warnings


def _format_preview_pair_section(label: str, stats: Optional[TexturePreviewStats]) -> List[str]:
    if stats is None:
        return [f"{label}: preview statistics unavailable."]
    return [
        f"{label}:",
        f"- { _preview_stats_summary(stats) }",
    ]


def build_mip_analysis_detail(
    original_root: Path,
    rebuilt_root: Path,
    row: MipAnalysisRow,
    *,
    texconv_path: Optional[Path] = None,
) -> str:
    relative = Path(row.relative_path)
    original_path = original_root / relative
    rebuilt_path = rebuilt_root / relative
    family_members_by_path = _build_family_members_by_relative_path(
        [path.as_posix() for path in collect_compare_relative_paths(original_root, rebuilt_root)]
    )
    family_members = family_members_by_path.get(row.relative_path, ())
    texture_type, confidence, reason = classify_texture_path(row.relative_path, family_members=family_members)
    detail_lines: List[str] = [
        f"Relative path: {row.relative_path}",
        "",
        "What this result means:",
        "- This row compares one DDS file found in both Original DDS root and Output root.",
        "- It checks header-level DDS settings first, then uses texconv previews when available for a safer visual check.",
        "",
        f"Texture semantic hint: {texture_type} ({confidence}% confidence, {reason})",
        f"Original DDS: {original_path}",
        f"Rebuilt DDS: {rebuilt_path}",
        f"Original header: {row.original_size} | {row.original_format} | mips={row.original_mips}",
        f"Rebuilt header: {row.rebuilt_size} | {row.rebuilt_format} | mips={row.rebuilt_mips}",
    ]
    size_summary, size_warnings = _compare_file_sizes(original_path, rebuilt_path)
    compare_warnings: List[str] = []
    detail_lines.extend(["", size_summary])
    if texconv_path is not None and texconv_path.exists():
        try:
            original_preview = _collect_preview_stats(ensure_dds_preview_png(texconv_path, original_path))
        except Exception as exc:
            original_preview = None
            detail_lines.append(f"Original preview: unavailable ({exc})")
        else:
            detail_lines.extend(["", *_format_preview_pair_section("Original preview", original_preview)])
        try:
            rebuilt_preview = _collect_preview_stats(ensure_dds_preview_png(texconv_path, rebuilt_path))
        except Exception as exc:
            rebuilt_preview = None
            detail_lines.append(f"Rebuilt preview: unavailable ({exc})")
        else:
            detail_lines.extend(["", *_format_preview_pair_section("Rebuilt preview", rebuilt_preview)])
        detail_lines.append("")
        detail_lines.append("Preview comparison:")
        compare_warnings = _compare_preview_stats(original_preview, rebuilt_preview)
        if compare_warnings:
            detail_lines.extend(f"- {warning}" for warning in compare_warnings)
        else:
            detail_lines.append("- No obvious preview drift detected.")
    else:
        detail_lines.extend(
            [
                "",
                "Preview comparison:",
                "- texconv.exe is not available, so preview-based brightness/alpha/channel checks are disabled.",
            ]
        )

    if original_path.exists() and rebuilt_path.exists():
        try:
            original_info = parse_dds(original_path)
            rebuilt_info = parse_dds(rebuilt_path)
        except Exception:
            original_info = None
            rebuilt_info = None
        if original_info is not None and rebuilt_info is not None:
            if original_info.texconv_format.endswith("_SRGB") != rebuilt_info.texconv_format.endswith("_SRGB"):
                detail_lines.append("")
                detail_lines.append("Color-space check:")
                detail_lines.append("- sRGB/linear usage changed between original and rebuilt DDS.")
            elif original_info.texconv_format != rebuilt_info.texconv_format:
                detail_lines.append("")
                detail_lines.append("Color-space check:")
                detail_lines.append("- DDS format changed; verify that color handling still matches the source.")

    if size_warnings:
        detail_lines.append("")
        detail_lines.append("Size warnings:")
        detail_lines.extend(f"- {warning}" for warning in size_warnings)

    already_reported_warnings = set(size_warnings)
    already_reported_warnings.update(compare_warnings)
    analysis_warnings = [warning for warning in row.warnings if warning not in already_reported_warnings]
    if analysis_warnings:
        detail_lines.append("")
        detail_lines.append("Additional analysis warnings:")
        detail_lines.extend(f"- {warning}" for warning in analysis_warnings)
    else:
        detail_lines.append("")
        detail_lines.append("Additional analysis warnings: none.")

    return "\n".join(detail_lines)


def build_normal_validation_detail(
    root: Path,
    row: NormalValidationRow,
    *,
    texconv_path: Optional[Path] = None,
) -> str:
    source_path = root / row.path
    detail_lines: List[str] = [
        f"Relative path: {row.path}",
        "",
        "What this result means:",
        "- This row comes from scanning normal-like DDS files in one root independently.",
        "- It checks format, dimensions, preview stability, and normal-map integrity signals.",
        "",
        f"Source root: {root}",
        f"Source path: {source_path}",
        f"Format: {row.texconv_format}",
        f"Size: {row.size_text}",
    ]

    if texconv_path is not None and texconv_path.exists() and source_path.exists():
        try:
            preview_stats = _collect_preview_stats(ensure_dds_preview_png(texconv_path, source_path))
        except Exception as exc:
            preview_stats = None
            detail_lines.extend(["", f"Preview statistics: unavailable ({exc})"])
        else:
            detail_lines.extend(["", *_format_preview_pair_section("Preview", preview_stats)])
            if preview_stats is not None:
                normal_signals: List[str] = []
                if preview_stats.mean_b < max(preview_stats.mean_r, preview_stats.mean_g):
                    normal_signals.append("Blue channel is not dominant; possible swizzle or non-standard normal encoding.")
                if preview_stats.mean_b < 110:
                    normal_signals.append("Blue channel average is low; possible channel issue or flattened normal detail.")
                if abs(preview_stats.mean_r - 128.0) > 26 or abs(preview_stats.mean_g - 128.0) > 26:
                    normal_signals.append("Red/green averages drift far from the usual 128 midpoint.")
                if (preview_stats.max_r - preview_stats.min_r) < 14 and (preview_stats.max_g - preview_stats.min_g) < 14:
                    normal_signals.append("Red/green ranges are narrow; precision may have been reduced.")
                if preview_stats.has_alpha and preview_stats.opaque_fraction < 0.95:
                    normal_signals.append("Alpha channel has visible variation; verify that it is meant to store data.")
                if normal_signals:
                    detail_lines.append("")
                    detail_lines.append("Preview-based normal-map signals:")
                    detail_lines.extend(f"- {signal}" for signal in normal_signals)
    else:
        detail_lines.extend(
            [
                "",
                "Preview statistics: unavailable.",
                "- texconv.exe is not available or the source file is missing, so image-based normal checks are disabled.",
            ]
        )

    if row.issues:
        detail_lines.append("")
        detail_lines.append("Validation issues:")
        detail_lines.extend(f"- {issue}" for issue in row.issues)
    else:
        detail_lines.append("")
        detail_lines.append("Validation issues: none detected.")

    if "FLOAT" in row.texconv_format.upper() or "SNORM" in row.texconv_format.upper():
        detail_lines.append("")
        detail_lines.append("Precision note:")
        detail_lines.append("- This texture type or source format is sensitive to PNG intermediates; compare carefully after rebuild.")

    return "\n".join(detail_lines)


def analyze_mip_behavior(
    original_root: Path,
    rebuilt_root: Path,
    *,
    texconv_path: Optional[Path] = None,
    limit: int = 3000,
    stop_event: Optional[object] = None,
) -> List[MipAnalysisRow]:
    rows: List[MipAnalysisRow] = []
    compare_relative_paths = [path.as_posix() for path in collect_compare_relative_paths(original_root, rebuilt_root)]
    family_members_by_path = _build_family_members_by_relative_path(compare_relative_paths)
    for relative_path_text in compare_relative_paths:
        raise_if_cancelled(stop_event)
        relative_path = Path(relative_path_text)
        original_path = original_root / relative_path
        rebuilt_path = rebuilt_root / relative_path
        family_members = family_members_by_path.get(relative_path_text, ())
        try:
            original_dds = parse_dds(original_path)
            rebuilt_dds = parse_dds(rebuilt_path)
        except Exception as exc:
            rows.append(
                MipAnalysisRow(
                    relative_path=relative_path.as_posix(),
                    original_format="-",
                    rebuilt_format="-",
                    original_size="-",
                    rebuilt_size="-",
                    original_mips=0,
                    rebuilt_mips=0,
                    warning_count=1,
                    warnings=[f"Could not parse DDS headers: {exc}"],
                )
            )
            continue

        warnings: List[str] = []
        original_size_bytes = original_path.stat().st_size if original_path.exists() else 0
        rebuilt_size_bytes = rebuilt_path.stat().st_size if rebuilt_path.exists() else 0
        if original_size_bytes > 0 and rebuilt_size_bytes > 0:
            size_ratio = rebuilt_size_bytes / max(1, original_size_bytes)
            if size_ratio < 0.70:
                warnings.append("Rebuilt DDS is substantially smaller than the original, which can indicate format or mip loss.")
            elif size_ratio > 1.50:
                warnings.append("Rebuilt DDS is substantially larger than the original, which can indicate format or mip growth.")
        if original_dds.texconv_format.endswith("_SRGB") != rebuilt_dds.texconv_format.endswith("_SRGB"):
            warnings.append("sRGB/linear usage changed between original and rebuilt DDS.")
        rebuilt_max = max_mips_for_size(rebuilt_dds.width, rebuilt_dds.height)
        if rebuilt_dds.mip_count < original_dds.mip_count:
            warnings.append(
                f"Rebuilt file has {original_dds.mip_count - rebuilt_dds.mip_count} fewer mip level(s) than the original."
            )
        if (rebuilt_dds.width > original_dds.width or rebuilt_dds.height > original_dds.height) and rebuilt_dds.mip_count <= original_dds.mip_count:
            warnings.append("Upscaled texture kept the same or fewer mips, which can waste added resolution.")
        if rebuilt_dds.mip_count < rebuilt_max:
            warnings.append(f"Rebuilt size supports up to {rebuilt_max} mips, but only {rebuilt_dds.mip_count} are present.")
        if rebuilt_dds.width < original_dds.width or rebuilt_dds.height < original_dds.height:
            warnings.append("Rebuilt dimensions are smaller than the original DDS.")
        if original_dds.texconv_format != rebuilt_dds.texconv_format:
            warnings.append("DDS format changed between original and rebuilt output.")
        if original_dds.has_alpha != rebuilt_dds.has_alpha:
            warnings.append("Alpha capability changed between original and rebuilt DDS.")
        texture_type = classify_texture_path(relative_path_text, family_members=family_members)[0]
        if is_png_intermediate_high_risk(texture_type, original_dds.texconv_format):
            warnings.append("Source format is precision-sensitive; PNG intermediates can hide detail loss.")
        if texconv_path is not None and texconv_path.exists():
            original_preview: Optional[TexturePreviewStats]
            rebuilt_preview: Optional[TexturePreviewStats]
            try:
                original_preview = _collect_preview_stats(ensure_dds_preview_png(texconv_path, original_path))
            except Exception:
                original_preview = None
            try:
                rebuilt_preview = _collect_preview_stats(ensure_dds_preview_png(texconv_path, rebuilt_path))
            except Exception:
                rebuilt_preview = None
            warnings.extend(
                warning
                for warning in _compare_preview_stats(original_preview, rebuilt_preview)
                if "preview could not be decoded for statistics" not in warning.lower()
                and "preview statistics are unavailable for both files" not in warning.lower()
            )
            warnings.extend(
                _texture_specific_preview_warnings(
                    relative_path_text,
                    original_preview,
                    rebuilt_preview,
                    family_members=family_members,
                )
            )
        warnings = _dedupe_preserve_order(warnings)

        rows.append(
            MipAnalysisRow(
                relative_path=relative_path.as_posix(),
                original_format=original_dds.texconv_format,
                rebuilt_format=rebuilt_dds.texconv_format,
                original_size=f"{original_dds.width}x{original_dds.height}",
                rebuilt_size=f"{rebuilt_dds.width}x{rebuilt_dds.height}",
                original_mips=original_dds.mip_count,
                rebuilt_mips=rebuilt_dds.mip_count,
                warning_count=len(warnings),
                warnings=warnings,
            )
        )
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: (-row.warning_count, row.relative_path))
    return rows


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _sample_image_channel_stats(image_path: Path) -> Optional[Dict[str, float]]:
    if QImage is None:
        return None
    image = QImage(str(image_path))
    if image.isNull():
        return None
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return None
    step_x = max(1, width // 64)
    step_y = max(1, height // 64)
    r_total = g_total = b_total = a_total = 0.0
    count = 0
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            color = QColor(image.pixel(x, y))
            r_total += color.red()
            g_total += color.green()
            b_total += color.blue()
            a_total += color.alpha()
            count += 1
    if count <= 0:
        return None
    return {
        "r": r_total / count,
        "g": g_total / count,
        "b": b_total / count,
        "a": a_total / count,
    }


def validate_normal_maps(
    root: Path,
    *,
    texconv_path: Optional[Path] = None,
    limit: int = 1500,
    stop_event: Optional[object] = None,
) -> List[NormalValidationRow]:
    dds_files = collect_dds_files(root, ())
    grouped_by_key: Dict[str, List[Path]] = defaultdict(list)
    for dds_path in dds_files:
        grouped_by_key[derive_texture_group_key(dds_path.relative_to(root).as_posix())].append(dds_path)
    normal_candidate_count = sum(
        1
        for dds_path in dds_files
        if classify_texture_path(
            dds_path.relative_to(root).as_posix(),
            family_members=tuple(member.relative_to(root).as_posix() for member in grouped_by_key[derive_texture_group_key(dds_path.relative_to(root).as_posix())]),
        )[0]
        == "normal"
    )
    preview_stats_budget = 200 if normal_candidate_count > 200 else normal_candidate_count
    preview_stats_used = 0

    rows: List[NormalValidationRow] = []
    for dds_path in dds_files:
        raise_if_cancelled(stop_event)
        relative_path = dds_path.relative_to(root).as_posix()
        group_members = grouped_by_key.get(derive_texture_group_key(relative_path), [])
        family_member_paths = tuple(member.relative_to(root).as_posix() for member in group_members)
        texture_type, _confidence, _reason = classify_texture_path(relative_path, family_members=family_member_paths)
        if texture_type != "normal":
            continue
        issues: List[str] = []
        try:
            info = parse_dds(dds_path)
        except Exception as exc:
            rows.append(
                NormalValidationRow(
                    path=relative_path,
                    root_label=root.name or str(root),
                    texconv_format="-",
                    size_text="-",
                    issue_count=1,
                    issues=[f"DDS header parse failed: {exc}"],
                )
            )
            continue

        if info.texconv_format in NORMAL_SUSPICIOUS_FORMATS:
            issues.append(f"Format {info.texconv_format} is unusual for a normal map.")
        elif info.texconv_format not in NORMAL_FRIENDLY_FORMATS:
            issues.append(f"Format {info.texconv_format} may be valid, but is not a common normal-map choice.")
        if "SRGB" in info.texconv_format:
            issues.append("sRGB normal maps are usually suspicious.")
        if not _is_power_of_two(info.width) or not _is_power_of_two(info.height):
            issues.append("Dimensions are not power-of-two.")
        if ("BC" in info.texconv_format or info.texconv_format.startswith("R")) and (info.width % 4 != 0 or info.height % 4 != 0):
            issues.append("Compressed DDS dimensions are not aligned to a 4x4 block size.")

        color_partner = next(
            (
                candidate
                for candidate in group_members
                if candidate != dds_path
                and classify_texture_path(
                    candidate.relative_to(root).as_posix(),
                    family_members=family_member_paths,
                )[0]
                == "color"
            ),
            None,
        )
        if color_partner is not None:
            try:
                color_info = parse_dds(color_partner)
                if (color_info.width, color_info.height) != (info.width, info.height):
                    issues.append("Normal map size differs from its color/albedo partner.")
            except Exception:
                pass

        if texconv_path is not None and texconv_path.exists() and preview_stats_used < preview_stats_budget:
            try:
                preview_path = ensure_dds_preview_png(texconv_path, dds_path)
                stats = _collect_preview_stats(preview_path)
                if stats is not None:
                    preview_stats_used += 1
                    if stats.mean_b < 110:
                        issues.append("Blue channel average is low; possible swizzle or non-standard normal encoding.")
                    if abs(stats.mean_r - 128.0) < 8 and abs(stats.mean_g - 128.0) < 8 and stats.mean_b > 220:
                        pass
                    elif stats.mean_b < max(stats.mean_r, stats.mean_g):
                        issues.append("Blue channel is not dominant; possible channel issue.")
                    if stats.has_alpha and stats.opaque_fraction < 0.90:
                        issues.append("Normal preview shows alpha variation; verify whether alpha stores packed data or should be preserved.")
                    if (stats.max_r - stats.min_r) < 12 and (stats.max_g - stats.min_g) < 12:
                        issues.append("Red/green channel range is narrow; precision may have been reduced.")
                    if "FLOAT" in info.texconv_format.upper() or "SNORM" in info.texconv_format.upper():
                        issues.append("Precision-sensitive normal format detected; PNG intermediates can hide detail loss.")
            except Exception:
                pass

        rows.append(
            NormalValidationRow(
                path=relative_path,
                root_label=root.name or str(root),
                texconv_format=info.texconv_format,
                size_text=f"{info.width}x{info.height}",
                issue_count=len(issues),
                issues=issues or ["No obvious issues detected."],
            )
        )
        if len(rows) >= limit:
            break
    rows.sort(key=lambda row: (-row.issue_count, row.path))
    return rows


def _estimate_grid_signal(image_path: Path) -> int:
    if QImage is None:
        return 0
    image = QImage(str(image_path))
    if image.isNull() or image.width() < 64 or image.height() < 64:
        return 0
    width = image.width()
    height = image.height()
    vertical_hits = 0
    horizontal_hits = 0

    sample_rows = [height // 4, height // 2, (height * 3) // 4]
    sample_cols = [width // 4, width // 2, (width * 3) // 4]

    for x in range(1, width - 1):
        score = 0.0
        for y in sample_rows:
            left = QColor(image.pixel(x - 1, y))
            right = QColor(image.pixel(x, y))
            score += abs(left.red() - right.red()) + abs(left.green() - right.green()) + abs(left.blue() - right.blue())
        if score / max(1, len(sample_rows)) > 160:
            vertical_hits += 1
    for y in range(1, height - 1):
        score = 0.0
        for x in sample_cols:
            top = QColor(image.pixel(x, y - 1))
            bottom = QColor(image.pixel(x, y))
            score += abs(top.red() - bottom.red()) + abs(top.green() - bottom.green()) + abs(top.blue() - bottom.blue())
        if score / max(1, len(sample_cols)) > 160:
            horizontal_hits += 1
    return int((vertical_hits / max(1, width)) * 100) + int((horizontal_hits / max(1, height)) * 100)


def detect_texture_atlases(
    root: Path,
    *,
    texconv_path: Optional[Path] = None,
    limit: int = 500,
) -> List[AtlasDetectionRow]:
    dds_files = collect_dds_files(root, ())
    preview_grid_budget = 200
    preview_grid_used = 0
    candidates: List[AtlasDetectionRow] = []
    for dds_path in dds_files:
        relative_path = dds_path.relative_to(root).as_posix()
        lowered = relative_path.lower()
        score = 0
        signals: List[str] = []
        try:
            info = parse_dds(dds_path)
        except Exception:
            continue

        if any(token in lowered for token in ("atlas", "sheet", "sprite", "icons", "decal", "/ui/", "impostor")):
            score += 3
            signals.append("Name/path suggests atlas or sheet usage.")
        if max(info.width, info.height) >= 2048:
            score += 1
            signals.append("Large texture dimensions.")
        ratio = max(info.width / max(1, info.height), info.height / max(1, info.width))
        if ratio >= 2.0:
            score += 1
            signals.append("Wide or tall aspect ratio.")
        if info.width % 256 == 0 and info.height % 256 == 0 and min(info.width, info.height) >= 512:
            score += 1
            signals.append("Dimensions align well to repeated tile cells.")

        if texconv_path is not None and texconv_path.exists() and preview_grid_used < preview_grid_budget:
            try:
                preview_path = ensure_dds_preview_png(texconv_path, dds_path)
                grid_signal = _estimate_grid_signal(preview_path)
                preview_grid_used += 1
                if grid_signal >= 8:
                    score += 2
                    signals.append("Preview image has repeated straight-line separators.")
            except Exception:
                pass

        if score <= 0:
            continue
        candidates.append(
            AtlasDetectionRow(
                path=relative_path,
                root_label=root.name or str(root),
                size_text=f"{info.width}x{info.height}",
                score=score,
                signals=signals,
            )
        )
    candidates.sort(key=lambda row: (-row.score, row.path))
    return candidates[:limit]


def get_regex_presets() -> List[RegexPreset]:
    return [
        RegexPreset("Materials", "Material names", r"(?i)material(name|id)?\s*=\s*\"([^\"]+)\"", "Find material-name assignments in XML or material-like files."),
        RegexPreset("Materials", "Texture references", r"(?i)(texture|albedo|normal|roughness|mask)[^\\n=]*=\s*\"([^\"]+)\"", "Find texture-path assignments and texture parameters."),
        RegexPreset("Actors", "Actor IDs", r"(?i)(actor|npc|pawn)[^\\n=]*id\s*=\s*\"?([A-Za-z0-9_./:-]+)\"?", "Find actor or NPC identifiers.", path_hint="character"),
        RegexPreset("Actors", "Gameplay tags", r"(?i)(gameplaytag|tag)[^\\n=]*=\s*\"([^\"]+)\"", "Find gameplay-tag style assignments."),
        RegexPreset("Paths", "File paths", r"(?i)([A-Za-z0-9_./-]+\.(dds|png|xml|material|json|lua))", "Find referenced asset paths."),
        RegexPreset("Paths", "Package-like IDs", r"(?i)\b\d{4}/[A-Za-z0-9_./-]+\b", "Find archive-style package/path references."),
        RegexPreset("Sound", "Event names", r"(?i)(Wwise|Sound(Event|Bank)|RTPC|SwitchGroup|State)", "Find sound-system references.", extensions=".xml;.json"),
        RegexPreset("UI", "UI widget refs", r"(?i)(widget|hud|icon|layout|panel|button)[A-Za-z0-9_./:-]*", "Find likely UI/layout terms.", extensions=".xml;.json;.cfg", path_hint="ui"),
        RegexPreset("Gameplay", "Quest or objective refs", r"(?i)(quest|objective|mission|scenario)[A-Za-z0-9_./:-]*", "Find quest/objective-style names."),
        RegexPreset("Scripts", "Class or function refs", r"(?i)\b(class|function|script|handler)\b", "Find script/class-like declarations.", extensions=".lua;.json;.xml"),
    ]


def cluster_text_search_results(results: Sequence[object], mode: str) -> List[SearchCluster]:
    bucket_counts: Dict[str, int] = defaultdict(int)
    bucket_matches: Dict[str, int] = defaultdict(int)
    bucket_samples: Dict[str, List[str]] = defaultdict(list)

    for result in results:
        relative_path = str(getattr(result, "relative_path", "") or "")
        if not relative_path:
            continue
        if mode == "package":
            label = str(getattr(result, "package_label", "") or "Loose file")
        elif mode == "system":
            label = system_area_from_path(relative_path)
        else:
            label = PurePosixPath(relative_path).parent.as_posix() or "(root)"
        bucket_counts[label] += 1
        bucket_matches[label] += int(getattr(result, "match_count", 0) or 0)
        samples = bucket_samples[label]
        if len(samples) < 3:
            samples.append(relative_path)

    clusters = [
        SearchCluster(
            mode=mode,
            label=label,
            file_count=file_count,
            total_matches=bucket_matches[label],
            sample_paths=bucket_samples[label],
        )
        for label, file_count in bucket_counts.items()
    ]
    clusters.sort(key=lambda cluster: (-cluster.file_count, -cluster.total_matches, cluster.label))
    return clusters


def load_research_notes(notes_path: Path) -> Dict[str, ResearchNote]:
    if not notes_path.exists():
        return {}
    try:
        payload = json.loads(notes_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    notes: Dict[str, ResearchNote] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        note_text = str(value.get("note", "")).strip()
        tags = value.get("tags", [])
        if not note_text and not tags:
            continue
        notes[key] = ResearchNote(
            target_key=key,
            source_kind=str(value.get("source_kind", "unknown")),
            tags=[str(tag).strip() for tag in tags if str(tag).strip()],
            note=note_text,
            updated_at=str(value.get("updated_at", "")),
        )
    return notes


def save_research_notes(notes_path: Path, notes: Dict[str, ResearchNote]) -> None:
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: {
            "source_kind": note.source_kind,
            "tags": list(note.tags),
            "note": note.note,
            "updated_at": note.updated_at,
        }
        for key, note in sorted(notes.items(), key=lambda item: item[0].lower())
    }
    notes_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def upsert_research_note(
    notes: Dict[str, ResearchNote],
    *,
    target_key: str,
    source_kind: str,
    tags_text: str,
    note_text: str,
) -> Dict[str, ResearchNote]:
    normalized_key = target_key.strip().replace("\\", "/")
    if not normalized_key:
        raise ValueError("Choose a file/path before saving a note.")
    tags = [token.strip() for token in re.split(r"[,\s;|]+", tags_text) if token.strip()]
    normalized_note = note_text.strip()
    if not tags and not normalized_note:
        notes.pop(normalized_key, None)
        return notes
    notes[normalized_key] = ResearchNote(
        target_key=normalized_key,
        source_kind=source_kind.strip() or "unknown",
        tags=tags,
        note=normalized_note,
        updated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    return notes


def delete_research_note(notes: Dict[str, ResearchNote], target_key: str) -> Dict[str, ResearchNote]:
    normalized_key = target_key.strip().replace("\\", "/")
    if normalized_key:
        notes.pop(normalized_key, None)
    return notes
