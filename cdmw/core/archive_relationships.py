from __future__ import annotations

from dataclasses import dataclass, field
import html
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cdmw.core.archive import (
    _extract_archive_sidecar_texture_lookup_paths,
    _find_archive_model_sidecar_entries,
    read_archive_entry_data,
    try_decode_text_like_archive_data,
)
from cdmw.core.archive_modding import ARCHIVE_MESH_EXTENSIONS
from cdmw.core.upscale_profiles import normalize_texture_reference_for_sidecar_lookup, parse_texture_sidecar_bindings
from cdmw.models import ArchiveEntry


ARCHIVE_REL_INCLUDE_REQUIRED = "required"
ARCHIVE_REL_INCLUDE_RECOMMENDED = "recommended"
ARCHIVE_REL_INCLUDE_MANUAL = "manual"
ARCHIVE_REL_INCLUDE_RISKY = "risky"
ARCHIVE_REL_INCLUDE_UNRESOLVED = "unresolved"

SWAP_SCOPE_BODY_ONLY = "body_only"
SWAP_SCOPE_BODY_HEAD = "body_head"
SWAP_SCOPE_FULL_APPEARANCE_REDIRECT = "full_appearance_redirect"

_XML_DESCRIPTOR_EXTENSIONS = {".xml", ".app_xml", ".prefabdata_xml", ".paccd", ".pac_xml", ".pami"}
_MATERIAL_SIDECAR_EXTENSIONS = {".pac_xml", ".pam_xml", ".pamlod_xml", ".pami", ".xml"}
_SKELETON_EXTENSIONS = {".pab", ".pabc", ".pabv", ".papr"}
_PHYSICS_EXTENSIONS = {".hkx", ".hkt"}
_ANIMATION_EXTENSIONS = {".pam", ".paa", ".pacb"}
_UNRESOLVED_DESCRIPTOR_SUFFIXES = (".pabc", ".pabv", ".papr", ".hkt")
_PATH_INDEX_CACHE: Dict[Tuple[int, int, str, str], Dict[str, List[ArchiveEntry]]] = {}
_BASENAME_INDEX_CACHE: Dict[Tuple[int, int, str, str], Dict[str, List[ArchiveEntry]]] = {}
_INDEX_CACHE_LIMIT = 4


@dataclass(frozen=True, slots=True)
class ArchiveRelationEdge:
    source_path: str
    related_path: str = ""
    related_entry: Optional[ArchiveEntry] = None
    relation_kind: str = ""
    role: str = ""
    confidence: str = "heuristic"
    reason: str = ""
    include_policy: str = ARCHIVE_REL_INCLUDE_MANUAL
    risk: bool = False
    suggested_target_path: str = ""
    unresolved: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveRelationshipPlan:
    source_path: str
    mode: str = "inspect"
    edges: Tuple[ArchiveRelationEdge, ...] = ()
    warnings: Tuple[str, ...] = ()
    swap_scope: str = ""
    patched_target_app_xml: bytes = b""
    patched_target_app_path: str = ""


@dataclass(frozen=True, slots=True)
class _AppPrefabReference:
    tag: str
    name: str
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _AppDescriptor:
    prefabs: Tuple[_AppPrefabReference, ...] = ()
    descriptor_values: Tuple[Tuple[str, str], ...] = ()


def _normalized_archive_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().strip("/").lower()


def _archive_entries_cache_key(archive_entries: Sequence[ArchiveEntry]) -> Tuple[int, int, str, str]:
    count = len(archive_entries)
    first = _normalized_archive_path(archive_entries[0].path) if count else ""
    last = _normalized_archive_path(archive_entries[-1].path) if count else ""
    return (id(archive_entries), count, first, last)


def _trim_index_cache(cache: Dict[Tuple[int, int, str, str], object]) -> None:
    while len(cache) > _INDEX_CACHE_LIMIT:
        try:
            cache.pop(next(iter(cache)))
        except StopIteration:
            break


def _entry_key(entry: ArchiveEntry) -> str:
    return f"{entry.pamt_path.resolve()}::{_normalized_archive_path(entry.path)}"


def _build_path_index(archive_entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
    cache_key = _archive_entries_cache_key(archive_entries)
    cached = _PATH_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result: Dict[str, List[ArchiveEntry]] = {}
    for entry in archive_entries:
        key = _normalized_archive_path(entry.path)
        if key:
            result.setdefault(key, []).append(entry)
    _PATH_INDEX_CACHE[cache_key] = result
    _trim_index_cache(_PATH_INDEX_CACHE)
    return result


def _build_basename_index(archive_entries: Sequence[ArchiveEntry]) -> Dict[str, List[ArchiveEntry]]:
    cache_key = _archive_entries_cache_key(archive_entries)
    cached = _BASENAME_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result: Dict[str, List[ArchiveEntry]] = {}
    for entry in archive_entries:
        basename = PurePosixPath(entry.path.replace("\\", "/")).name.lower()
        if basename:
            result.setdefault(basename, []).append(entry)
    _BASENAME_INDEX_CACHE[cache_key] = result
    _trim_index_cache(_BASENAME_INDEX_CACHE)
    return result


def _read_entry_text(entry: ArchiveEntry) -> str:
    data, _decompressed, _note = read_archive_entry_data(entry)
    return try_decode_text_like_archive_data(data) or ""


def _parse_xml(text: str) -> Optional[ET.Element]:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        try:
            return ET.fromstring(f"<Root>{raw}</Root>")
        except ET.ParseError:
            return None


def _local_name(tag: str) -> str:
    return str(tag or "").split("}", 1)[-1].strip()


def _looks_like_reference(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "/" in text or "\\" in text or "." in PurePosixPath(text).name:
        return True
    return bool(re.search(r"_(?:\d{4,}|[a-z]{2,})", text, re.IGNORECASE))


def parse_app_xml(text: str) -> _AppDescriptor:
    root = _parse_xml(text)
    if root is None:
        return _AppDescriptor()
    prefabs: List[_AppPrefabReference] = []
    descriptors: List[Tuple[str, str]] = []
    for element in root.iter():
        tag = _local_name(element.tag)
        attrs = {str(key): html.unescape(str(value or "")) for key, value in element.attrib.items()}
        name = attrs.get("Name") or attrs.get("name") or ""
        if name:
            prefabs.append(_AppPrefabReference(tag=tag, name=name, attributes=attrs))
        for key, value in attrs.items():
            key_lower = key.lower()
            if key_lower in {"customizationfile", "meshparamfile", "decorationparamfile"} or (
                key_lower.endswith("file") and _looks_like_reference(value)
            ):
                descriptors.append((key, value))
    return _AppDescriptor(prefabs=tuple(prefabs), descriptor_values=tuple(dict.fromkeys(descriptors)))


def parse_prefabdata_xml(text: str) -> Tuple[Tuple[str, str], ...]:
    root = _parse_xml(text)
    if root is None:
        return ()
    refs: List[Tuple[str, str]] = []
    for element in root.iter():
        for key, raw_value in element.attrib.items():
            value = html.unescape(str(raw_value or "")).strip()
            if not value:
                continue
            key_lower = str(key or "").lower()
            if (
                key_lower in {"filename", "skeletonname", "skeletonvariationname", "morphtargetsetname", "ragdollname"}
                or key_lower.endswith("name")
                or key_lower.endswith("file")
                or key_lower.endswith("path")
            ):
                if _looks_like_reference(value) or key_lower in {"filename", "skeletonname", "skeletonvariationname"}:
                    refs.append((str(key), value))
    return tuple(dict.fromkeys(refs))


def _candidate_basenames_for_xml_reference(raw_value: str, attr_name: str) -> Tuple[str, ...]:
    value = html.unescape(str(raw_value or "")).replace("\\", "/").strip()
    if not value:
        return ()
    basename = PurePosixPath(value).name.strip()
    if not basename:
        return ()
    stem = PurePosixPath(basename).stem
    suffix = PurePosixPath(basename).suffix.lower()
    attr = str(attr_name or "").strip().lower()
    candidates: List[str] = [basename]
    if attr == "name":
        candidates.extend(
            (
                f"{basename}.prefab",
                f"{basename}.prefabdata_xml",
                f"{basename}.prefabdata.xml",
                f"{basename}.pac",
                f"{basename}.pac_xml",
                f"{basename}.pami",
            )
        )
    elif attr == "customizationfile":
        if not suffix:
            candidates.extend((f"{basename}.paccd", f"{basename}.xml"))
    elif attr in {"meshparamfile", "decorationparamfile"} and not suffix:
        candidates.append(f"{basename}.xml")
    elif attr == "filename":
        if not suffix:
            candidates.extend((f"{basename}.xml", f"{basename}.pab", f"{basename}.pabc", f"{basename}.pabv", f"{basename}.papr", f"{basename}.hkx", f"{basename}.hkt"))
        if suffix == ".prefabdata":
            candidates.append(f"{stem}.prefabdata_xml")
    elif not suffix:
        candidates.extend((f"{basename}.xml", f"{basename}.prefabdata_xml", f"{basename}.pab", f"{basename}.hkx", f"{basename}.hkt"))
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _score_xml_reference_candidate(source_path: str, entry: ArchiveEntry) -> Tuple[int, int, int]:
    source_parts = [part for part in PurePosixPath(_normalized_archive_path(source_path)).parts if part]
    entry_parts = [part for part in PurePosixPath(_normalized_archive_path(entry.path)).parts if part]
    shared_prefix = 0
    for source_part, entry_part in zip(source_parts, entry_parts):
        if source_part != entry_part:
            break
        shared_prefix += 1
    same_package_depth = 1 if source_parts[:1] and source_parts[:1] == entry_parts[:1] else 0
    return shared_prefix, same_package_depth, -len(entry.path)


def _relation_kind_for_entry(entry: ArchiveEntry) -> str:
    extension = str(entry.extension or "").lower()
    path = _normalized_archive_path(entry.path)
    if extension == ".dds":
        return "texture"
    if extension == ".app_xml":
        return "appearance"
    if extension == ".prefabdata_xml":
        return "prefab_data"
    if extension == ".prefab":
        return "prefab"
    if extension in ARCHIVE_MESH_EXTENSIONS:
        return "model"
    if extension in _SKELETON_EXTENSIONS:
        return "skeleton"
    if extension in _PHYSICS_EXTENSIONS:
        return "physics"
    if extension in _ANIMATION_EXTENSIONS or "/animation/" in path:
        return "animation"
    if extension in _MATERIAL_SIDECAR_EXTENSIONS and (
        "modelproperty/" in path or extension in {".pac_xml", ".pam_xml", ".pamlod_xml", ".pami"}
    ):
        return "material_sidecar"
    if extension in _XML_DESCRIPTOR_EXTENSIONS:
        return "descriptor"
    return "file"


def _policy_for_kind(kind: str) -> Tuple[str, bool]:
    if kind in {"skeleton", "physics", "animation"}:
        return ARCHIVE_REL_INCLUDE_MANUAL, True
    if kind in {"appearance_patch"}:
        return ARCHIVE_REL_INCLUDE_REQUIRED, False
    if kind in {"texture", "model", "material_sidecar", "prefab_data", "prefab", "descriptor"}:
        return ARCHIVE_REL_INCLUDE_RECOMMENDED, False
    return ARCHIVE_REL_INCLUDE_MANUAL, False


def _resolve_basenames(
    raw_value: str,
    attr_name: str,
    basename_index: Mapping[str, Sequence[ArchiveEntry]],
    *,
    source_path: str = "",
    path_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveEntry, ...]:
    result: List[ArchiveEntry] = []
    seen: set[str] = set()

    def add_entries(candidates: Sequence[ArchiveEntry]) -> None:
        ordered_candidates = list(candidates)
        if source_path:
            ordered_candidates.sort(key=lambda entry: _score_xml_reference_candidate(source_path, entry), reverse=True)
            if len(ordered_candidates) > 1:
                best_prefix = _score_xml_reference_candidate(source_path, ordered_candidates[0])[0]
                if best_prefix > 0:
                    ordered_candidates = [
                        entry
                        for entry in ordered_candidates
                        if _score_xml_reference_candidate(source_path, entry)[0] == best_prefix
                    ]
        for entry in ordered_candidates:
            key = _entry_key(entry)
            if key and key not in seen:
                result.append(entry)
                seen.add(key)

    value = html.unescape(str(raw_value or "")).replace("\\", "/").strip()
    if value and "/" in value and path_index is not None:
        path_candidates: List[ArchiveEntry] = []
        for basename in _candidate_basenames_for_xml_reference(raw_value, attr_name):
            candidate_path = value
            if PurePosixPath(value).name != basename:
                parent = str(PurePosixPath(value).parent).strip(".")
                candidate_path = f"{parent}/{basename}" if parent else basename
            path_candidates.extend(tuple(path_index.get(_normalized_archive_path(candidate_path), ()) or ()))
        add_entries(path_candidates)

    for basename in _candidate_basenames_for_xml_reference(raw_value, attr_name):
        add_entries(tuple(basename_index.get(str(basename).lower(), ()) or ()))
    return tuple(result)


def _edge_for_entry(
    source_path: str,
    entry: ArchiveEntry,
    *,
    role: str,
    confidence: str,
    reason: str,
    suggested_target_path: str = "",
) -> ArchiveRelationEdge:
    kind = _relation_kind_for_entry(entry)
    policy, risk = _policy_for_kind(kind)
    return ArchiveRelationEdge(
        source_path=source_path,
        related_path=entry.path.replace("\\", "/"),
        related_entry=entry,
        relation_kind=kind,
        role=role,
        confidence=confidence,
        reason=reason,
        include_policy=policy,
        risk=risk,
        suggested_target_path=suggested_target_path,
    )


def _unresolved_edge(source_path: str, raw_value: str, attr_name: str, *, role: str, reason: str) -> ArchiveRelationEdge:
    return ArchiveRelationEdge(
        source_path=source_path,
        related_path=str(raw_value or "").replace("\\", "/").strip(),
        relation_kind="unresolved",
        role=role,
        confidence="unresolved",
        reason=reason,
        include_policy=ARCHIVE_REL_INCLUDE_UNRESOLVED,
        risk=True,
        unresolved=True,
    )


def _dedupe_edges(edges: Iterable[ArchiveRelationEdge]) -> Tuple[ArchiveRelationEdge, ...]:
    result: List[ArchiveRelationEdge] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for edge in edges:
        entry_key = _entry_key(edge.related_entry) if edge.related_entry is not None else ""
        key = (entry_key, _normalized_archive_path(edge.related_path), edge.relation_kind, edge.role)
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return tuple(result)


def _resolve_sidecar_texture_edges(
    sidecar_entry: ArchiveEntry,
    *,
    source_path: str,
    archive_entries: Sequence[ArchiveEntry] = (),
    path_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    basename_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveRelationEdge, ...]:
    if path_index is None:
        path_index = _build_path_index(archive_entries)
    if basename_index is None:
        basename_index = _build_basename_index(archive_entries)
    try:
        text = _read_entry_text(sidecar_entry)
    except Exception:
        return ()
    edges: List[ArchiveRelationEdge] = []
    structured_bindings = tuple(parse_texture_sidecar_bindings(text, sidecar_path=sidecar_entry.path))
    structured_paths: List[Tuple[str, str]] = []
    seen_structured: set[Tuple[str, str]] = set()
    for binding in structured_bindings:
        raw_texture_path = str(getattr(binding, "texture_path", "") or "").strip()
        if not raw_texture_path:
            continue
        parameter_name = str(getattr(binding, "parameter_name", "") or "").strip()
        key = (normalize_texture_reference_for_sidecar_lookup(raw_texture_path), parameter_name.lower())
        if key in seen_structured:
            continue
        seen_structured.add(key)
        structured_paths.append((raw_texture_path, parameter_name))
    if not structured_paths:
        structured_paths = [(raw_texture_path, "") for raw_texture_path in _extract_archive_sidecar_texture_lookup_paths(text)]

    for raw_texture_path, parameter_name in structured_paths:
        normalized = normalize_texture_reference_for_sidecar_lookup(raw_texture_path)
        exact_candidates = tuple(path_index.get(normalized, ()) or ())
        if exact_candidates:
            for candidate in exact_candidates:
                if str(candidate.extension or "").lower() == ".dds":
                    edges.append(
                        _edge_for_entry(
                            source_path,
                            candidate,
                            role=parameter_name or "texture",
                            confidence="exact_path",
                            reason=f"Texture path referenced by {sidecar_entry.basename}",
                        )
                    )
            continue
        basename = PurePosixPath(str(raw_texture_path or "").replace("\\", "/")).name.lower()
        for candidate in tuple(basename_index.get(basename, ()) or ()):
            if str(candidate.extension or "").lower() == ".dds":
                edges.append(
                    ArchiveRelationEdge(
                        source_path=source_path,
                        related_path=candidate.path.replace("\\", "/"),
                        related_entry=candidate,
                        relation_kind="texture",
                        role=parameter_name or "texture",
                        confidence="basename_fallback",
                        reason=f"Texture basename referenced by {sidecar_entry.basename}",
                        include_policy=ARCHIVE_REL_INCLUDE_MANUAL,
                    )
                )
    return _dedupe_edges(edges)


def _sidecar_submesh_names(sidecar_text: str) -> Tuple[str, ...]:
    names: List[str] = []
    for match in re.finditer(r'_subMeshName"\s*value="([^"]+)"', sidecar_text or "", re.IGNORECASE):
        value = html.unescape(match.group(1)).strip().lower()
        if value and value not in names:
            names.append(value)
    return tuple(names)


def _read_sidecar_submesh_names(entry: ArchiveEntry) -> Tuple[str, ...]:
    try:
        return _sidecar_submesh_names(_read_entry_text(entry))
    except Exception:
        return ()


def resolve_material_texture_graph(
    model_or_sidecar_entry: ArchiveEntry,
    archive_entries: Sequence[ArchiveEntry] = (),
    *,
    path_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    basename_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> ArchiveRelationshipPlan:
    if basename_index is None:
        basename_index = _build_basename_index(archive_entries)
    if path_index is None:
        path_index = _build_path_index(archive_entries)
    source_path = model_or_sidecar_entry.path.replace("\\", "/")
    edges: List[ArchiveRelationEdge] = []
    sidecar_entries: Tuple[ArchiveEntry, ...]
    if _relation_kind_for_entry(model_or_sidecar_entry) == "material_sidecar":
        sidecar_entries = (model_or_sidecar_entry,)
    else:
        sidecar_entries = _find_archive_model_sidecar_entries(model_or_sidecar_entry, basename_index)

    for sidecar_entry in sidecar_entries:
        edges.append(
            _edge_for_entry(
                source_path,
                sidecar_entry,
                role="material_sidecar",
                confidence="sidecar_match",
                reason="Material sidecar matched by model basename/path",
            )
        )
        edges.extend(
            _resolve_sidecar_texture_edges(
                sidecar_entry,
                source_path=source_path,
                archive_entries=archive_entries,
                path_index=path_index,
                basename_index=basename_index,
            )
        )
    return ArchiveRelationshipPlan(source_path=source_path, mode="material_texture_graph", edges=_dedupe_edges(edges))


def _expand_prefabdata_graph(
    source_path: str,
    prefab_entry: ArchiveEntry,
    archive_entries: Sequence[ArchiveEntry] = (),
    *,
    basename_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    path_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> Tuple[ArchiveRelationEdge, ...]:
    if basename_index is None:
        basename_index = _build_basename_index(archive_entries)
    if path_index is None:
        path_index = _build_path_index(archive_entries)
    try:
        text = _read_entry_text(prefab_entry)
    except Exception:
        return ()
    edges: List[ArchiveRelationEdge] = []
    for attr_name, raw_value in parse_prefabdata_xml(text):
        resolved = _resolve_basenames(
            raw_value,
            attr_name,
            basename_index,
            source_path=prefab_entry.path,
            path_index=path_index,
        )
        if resolved:
            for entry in resolved:
                edges.append(
                    _edge_for_entry(
                        source_path,
                        entry,
                        role=str(attr_name).lower(),
                        confidence="xml_reference",
                        reason=f"Referenced by {prefab_entry.basename} attribute {attr_name}",
                    )
                )
        elif PurePosixPath(str(raw_value or "").replace("\\", "/")).suffix.lower() in _UNRESOLVED_DESCRIPTOR_SUFFIXES:
            edges.append(
                _unresolved_edge(
                    source_path,
                    raw_value,
                    attr_name,
                    role=str(attr_name).lower(),
                    reason=f"{prefab_entry.basename} references a descriptor not present in the loaded archive set",
                )
            )
    return _dedupe_edges(edges)


def build_archive_relationship_plan(
    entry: ArchiveEntry,
    archive_entries: Sequence[ArchiveEntry] = (),
    mode: str = "inspect",
    *,
    path_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
    basename_index: Optional[Mapping[str, Sequence[ArchiveEntry]]] = None,
) -> ArchiveRelationshipPlan:
    source_path = entry.path.replace("\\", "/")
    relation_kind = _relation_kind_for_entry(entry)
    edges: List[ArchiveRelationEdge] = []
    warnings: List[str] = []
    if basename_index is None:
        basename_index = _build_basename_index(archive_entries)
    if path_index is None:
        path_index = _build_path_index(archive_entries)

    if relation_kind in {"model", "material_sidecar"}:
        material_plan = resolve_material_texture_graph(
            entry,
            archive_entries,
            path_index=path_index,
            basename_index=basename_index,
        )
        edges.extend(material_plan.edges)

    if relation_kind == "appearance":
        try:
            descriptor = parse_app_xml(_read_entry_text(entry))
        except Exception:
            descriptor = _AppDescriptor()
        for attr_name, raw_value in descriptor.descriptor_values:
            resolved = _resolve_basenames(
                raw_value,
                attr_name,
                basename_index,
                source_path=entry.path,
                path_index=path_index,
            )
            if not resolved:
                edges.append(
                    _unresolved_edge(
                        source_path,
                        raw_value,
                        attr_name,
                        role=str(attr_name).lower(),
                        reason=f"Appearance descriptor references {raw_value}",
                    )
                )
            for related in resolved:
                edges.append(
                    _edge_for_entry(
                        source_path,
                        related,
                        role=str(attr_name).lower(),
                        confidence="app_xml_reference",
                        reason=f"Referenced by appearance attribute {attr_name}",
                    )
                )
        for prefab in descriptor.prefabs:
            resolved = _resolve_basenames(
                prefab.name,
                "Name",
                basename_index,
                source_path=entry.path,
                path_index=path_index,
            )
            if not resolved:
                edges.append(
                    _unresolved_edge(
                        source_path,
                        prefab.name,
                        "Name",
                        role=prefab.tag.lower(),
                        reason=f"Appearance prefab {prefab.tag} was not resolved",
                    )
                )
                continue
            for related in resolved:
                role = prefab.tag.lower()
                edges.append(
                    _edge_for_entry(
                        source_path,
                        related,
                        role=role,
                        confidence="app_xml_prefab",
                        reason=f"Appearance {prefab.tag} prefab reference",
                    )
                )
                if _relation_kind_for_entry(related) == "prefab_data":
                    edges.extend(
                        _expand_prefabdata_graph(
                            source_path,
                            related,
                            archive_entries,
                            basename_index=basename_index,
                            path_index=path_index,
                        )
                    )
                if _relation_kind_for_entry(related) in {"model", "material_sidecar"}:
                    edges.extend(
                        resolve_material_texture_graph(
                            related,
                            archive_entries,
                            path_index=path_index,
                            basename_index=basename_index,
                        ).edges
                    )

    if relation_kind == "prefab_data":
        edges.extend(
            _expand_prefabdata_graph(
                source_path,
                entry,
                archive_entries,
                basename_index=basename_index,
                path_index=path_index,
            )
        )

    # Follow direct models/sidecars discovered from XML once so app graphs include textures.
    expanded: List[ArchiveRelationEdge] = []
    for edge in edges:
        expanded.append(edge)
        if edge.related_entry is None:
            continue
        if _relation_kind_for_entry(edge.related_entry) in {"model", "material_sidecar"}:
            expanded.extend(
                resolve_material_texture_graph(
                    edge.related_entry,
                    archive_entries,
                    path_index=path_index,
                    basename_index=basename_index,
                ).edges
            )
    return ArchiveRelationshipPlan(source_path=source_path, mode=mode, edges=_dedupe_edges(expanded), warnings=tuple(warnings))


def _find_related_app_entries(entry: ArchiveEntry, archive_entries: Sequence[ArchiveEntry]) -> Tuple[ArchiveEntry, ...]:
    if str(entry.extension or "").lower() == ".app_xml":
        return (entry,)
    source_stem = PurePosixPath(entry.path.replace("\\", "/")).stem.lower()
    if not source_stem:
        return ()
    tokens = tuple(token for token in re.split(r"[^a-z0-9]+", source_stem) if token and len(token) > 1)
    app_candidates = tuple(candidate for candidate in archive_entries if str(candidate.extension or "").lower() == ".app_xml")
    scored: List[Tuple[int, ArchiveEntry]] = []
    for candidate in archive_entries:
        if str(candidate.extension or "").lower() != ".app_xml":
            continue
        candidate_path = candidate.path.replace("\\", "/").lower()
        score = 0
        if source_stem in candidate_path:
            score += 80
        for token in tokens:
            if token in candidate_path:
                score += 8
        if score > 0:
            scored.append((score, candidate))
    # Reading thousands of app_xml payloads on the GUI thread is expensive.
    # Path matches are enough for named characters like Damian/Macduff; only
    # fall back to payload scanning when no plausible path match exists.
    if scored:
        scored.sort(key=lambda item: (item[0], -len(item[1].path)), reverse=True)
        return tuple(candidate for _score, candidate in scored[:8])
    for candidate in app_candidates:
        candidate_path = candidate.path.replace("\\", "/").lower()
        score = 0
        try:
            text = _read_entry_text(candidate).lower()
        except Exception:
            text = ""
        if source_stem in text:
            score += 120
        for token in tokens:
            if token in text:
                score += 4
        for token in tokens:
            if token in candidate_path:
                score += 2
        if score > 0:
            scored.append((score, candidate))
    scored.sort(key=lambda item: (item[0], -len(item[1].path)), reverse=True)
    result: List[ArchiveEntry] = []
    seen: set[str] = set()
    for _score, candidate in scored[:8]:
        key = _entry_key(candidate)
        if key not in seen:
            result.append(candidate)
            seen.add(key)
    return tuple(result)


def _find_primary_sidecar(entry: ArchiveEntry, archive_entries: Sequence[ArchiveEntry]) -> Optional[ArchiveEntry]:
    basename_index = _build_basename_index(archive_entries)
    sidecars = _find_archive_model_sidecar_entries(entry, dict(basename_index))
    return sidecars[0] if sidecars else None


def _patch_target_app_with_source(
    target_app: ArchiveEntry,
    source_app: ArchiveEntry,
    *,
    swap_scope: str,
) -> Tuple[bytes, str]:
    try:
        target_text = _read_entry_text(target_app)
        source_text = _read_entry_text(source_app)
    except Exception:
        return b"", ""
    target_root = _parse_xml(target_text)
    source_root = _parse_xml(source_text)
    if target_root is None or source_root is None:
        return b"", ""
    tags_to_patch = {"Nude"}
    if swap_scope == SWAP_SCOPE_BODY_HEAD:
        tags_to_patch.add("Head")
    elif swap_scope == SWAP_SCOPE_FULL_APPEARANCE_REDIRECT:
        tags_to_patch.update({"Head", "Hair", "Armor", "Accessory", "Face", "Body"})

    source_by_tag: Dict[str, ET.Element] = {}
    for element in source_root.iter():
        local = _local_name(element.tag)
        if local in tags_to_patch and ("Name" in element.attrib or "name" in element.attrib):
            source_by_tag.setdefault(local, element)

    changed = False
    for element in target_root.iter():
        local = _local_name(element.tag)
        source_element = source_by_tag.get(local)
        if source_element is None:
            continue
        for attr_name, source_value in source_element.attrib.items():
            if attr_name.lower() in {"name", "characterscale", "scale", "preview"} or "scale" in attr_name.lower():
                target_key = attr_name if attr_name in element.attrib else next(
                    (key for key in element.attrib if key.lower() == attr_name.lower()),
                    attr_name,
                )
                if element.attrib.get(target_key) != source_value:
                    element.attrib[target_key] = source_value
                    changed = True
    if not changed:
        return b"", ""
    return ET.tostring(target_root, encoding="utf-8", xml_declaration=True), target_app.path.replace("\\", "/")


def build_character_swap_plan(
    target_entry: ArchiveEntry,
    source_entry: ArchiveEntry,
    archive_entries: Sequence[ArchiveEntry],
    swap_scope: str = SWAP_SCOPE_BODY_HEAD,
) -> ArchiveRelationshipPlan:
    if swap_scope not in {SWAP_SCOPE_BODY_ONLY, SWAP_SCOPE_BODY_HEAD, SWAP_SCOPE_FULL_APPEARANCE_REDIRECT}:
        swap_scope = SWAP_SCOPE_BODY_HEAD
    source_path = source_entry.path.replace("\\", "/")
    target_apps = _find_related_app_entries(target_entry, archive_entries)
    source_apps = _find_related_app_entries(source_entry, archive_entries)
    edges: List[ArchiveRelationEdge] = []
    warnings: List[str] = []
    patched_payload = b""
    patched_target_path = ""

    if target_apps and source_apps:
        patched_payload, patched_target_path = _patch_target_app_with_source(
            target_apps[0],
            source_apps[0],
            swap_scope=swap_scope,
        )
        if patched_payload:
            edges.append(
                ArchiveRelationEdge(
                    source_path=source_path,
                    related_path=patched_target_path,
                    related_entry=target_apps[0],
                    relation_kind="appearance_patch",
                    role=swap_scope,
                    confidence="app_xml_patch",
                    reason="Patch target appearance body/head prefab names while preserving other target appearance sections",
                    include_policy=ARCHIVE_REL_INCLUDE_REQUIRED,
                    suggested_target_path=patched_target_path,
                )
            )
        else:
            warnings.append("No target appearance XML patch was produced for the selected source/target pair.")
        source_app_plan = build_archive_relationship_plan(source_apps[0], archive_entries, mode="character_swap_source_graph")
        edges.extend(source_app_plan.edges)
        warnings.extend(source_app_plan.warnings)
    else:
        warnings.append("Character appearance XML could not be resolved for the selected source/target pair.")

    source_sidecar = _find_primary_sidecar(source_entry, archive_entries)
    target_sidecar = _find_primary_sidecar(target_entry, archive_entries)
    if source_sidecar is not None and target_sidecar is not None:
        source_names = set(_read_sidecar_submesh_names(source_sidecar))
        target_names = set(_read_sidecar_submesh_names(target_sidecar))
        if source_names and target_names and source_names != target_names:
            warnings.append(
                "Source and target material sidecar submesh wrappers differ; generated/retargeted sidecar patching is preferred over copying source sidecar bytes."
            )
            edges.append(
                ArchiveRelationEdge(
                    source_path=source_path,
                    related_path=source_sidecar.path.replace("\\", "/"),
                    related_entry=source_sidecar,
                    relation_kind="material_sidecar",
                    role="topology_reference",
                    confidence="topology_diff",
                    reason="Source material wrapper topology differs from target; use as patch input, not direct source-sidecar copy.",
                    include_policy=ARCHIVE_REL_INCLUDE_MANUAL,
                    risk=True,
                )
            )
    material_plan = resolve_material_texture_graph(source_entry, archive_entries)
    edges.extend(material_plan.edges)
    return ArchiveRelationshipPlan(
        source_path=source_path,
        mode="character_swap",
        edges=_dedupe_edges(edges),
        warnings=tuple(dict.fromkeys(warnings)),
        swap_scope=swap_scope,
        patched_target_app_xml=patched_payload,
        patched_target_app_path=patched_target_path,
    )
