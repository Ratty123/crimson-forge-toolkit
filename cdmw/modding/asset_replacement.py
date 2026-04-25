"""Asset-family compatibility analysis for mesh replacement workflows.

This layer does not build replacement payloads.  It describes what the
selected archive asset appears to be, which companion files were found, and
whether the existing replacement pipeline can safely continue.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Mapping, Sequence

from cdmw.models import ArchiveEntry
from cdmw.core.upscale_profiles import infer_texture_semantics
from cdmw.modding.mesh_parser import ParsedMesh


SUPPORTED_MESH_FORMATS = {".pam", ".pamlod", ".pac"}
MATERIAL_SIDECAR_EXTENSIONS = {".pami", ".pac_xml", ".pam_xml", ".pamlod_xml"}
RIG_EXTENSIONS = {".pab", ".pabc", ".hkx"}
METADATA_EXTENSIONS = {".meshinfo", ".xml", ".prefabdata_xml"}


@dataclass(slots=True, frozen=True)
class ReplacementRelatedFile:
    path: str
    extension: str
    role: str
    confidence: str = "same-stem"


@dataclass(slots=True, frozen=True)
class ReplacementTextureBinding:
    sidecar_kind: str = ""
    linked_mesh_path: str = ""
    part_name: str = ""
    material_name: str = ""
    shader_family: str = ""
    parameter_name: str = ""
    texture_path: str = ""
    slot_kind: str = ""
    slot_label: str = ""
    semantic_type: str = ""
    semantic_subtype: str = ""
    visualized: bool = False
    visual_state: str = ""
    resolved_texture_exists: bool = False
    reason: str = ""
    source: str = ""


@dataclass(slots=True, frozen=True)
class TextureSlotClassification:
    slot_kind: str
    slot_label: str
    semantic_type: str
    semantic_subtype: str
    visualized: bool
    visual_state: str
    reason: str
    packed_channels: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ReplacementAssetProfile:
    source_path: str
    mesh_format: str
    category_hint: str
    asset_family: str
    support_level: str
    replacement_support: str
    export_supported: bool
    geometry_mode: str
    lod_mode: str
    sidecar_mode: str
    required_companions: tuple[str, ...] = ()
    related_files: tuple[ReplacementRelatedFile, ...] = ()
    texture_bindings: tuple[ReplacementTextureBinding, ...] = ()
    texture_summary: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    facts: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True, frozen=True)
class ReplacementBuildPlan:
    geometry_mode: str = "static_replacement"
    lod_mode: str = "selected_mesh_only"
    sidecar_mode: str = "patch_when_available"
    risk_flags: tuple[str, ...] = ()
    texture_bindings: tuple[ReplacementTextureBinding, ...] = ()


def analyze_replacement_asset(
    entry: ArchiveEntry,
    *,
    archive_entries_by_basename: Mapping[str, Sequence[ArchiveEntry]] | None = None,
    parsed_mesh: ParsedMesh | None = None,
    sidecar_texture_bindings: Sequence[object] = (),
    sidecar_texts: Sequence[str] = (),
) -> ReplacementAssetProfile:
    """Return a conservative compatibility profile for a replacement target."""

    extension = str(getattr(entry, "extension", "") or "").strip().lower()
    source_path = str(getattr(entry, "path", "") or "").replace("\\", "/")
    related_files = _find_related_files(entry, archive_entries_by_basename or {})
    texture_bindings = _classify_sidecar_texture_bindings(sidecar_texture_bindings)
    texture_summary = summarize_texture_bindings(texture_bindings)
    sidecar_feature_facts, sidecar_feature_warnings = _summarize_sidecar_features(sidecar_texts)
    asset_family = _infer_asset_family(source_path)
    category_hint = _infer_category_hint(source_path, parsed_mesh=parsed_mesh, related_files=related_files)
    warnings: list[str] = []
    errors: list[str] = []

    submesh_count = len(getattr(parsed_mesh, "submeshes", ()) or ()) if parsed_mesh is not None else 0
    vertex_count = int(getattr(parsed_mesh, "total_vertices", 0) or 0) if parsed_mesh is not None else 0
    face_count = int(getattr(parsed_mesh, "total_faces", 0) or 0) if parsed_mesh is not None else 0
    has_uvs = bool(getattr(parsed_mesh, "has_uvs", False)) if parsed_mesh is not None else False
    has_bones = bool(getattr(parsed_mesh, "has_bones", False)) if parsed_mesh is not None else False

    if extension in MATERIAL_SIDECAR_EXTENSIONS:
        linked_mesh_paths = tuple(
            _dedupe(
                [
                    str(getattr(binding, "linked_mesh_path", "") or "").replace("\\", "/")
                    for binding in sidecar_texture_bindings
                    if str(getattr(binding, "linked_mesh_path", "") or "").strip()
                ]
            )
        )
        support_level = "Preview only"
        export_supported = False
        geometry_mode = "sidecar_analysis_only"
        lod_mode = "sidecar_linked_mesh" if linked_mesh_paths else "sidecar_only"
        if asset_family == "leveldata" or "proxylod" in source_path.lower():
            errors.append("Level/proxylod sidecars are parsed for texture review, but geometry replacement is preview-only in this version.")
        else:
            errors.append("Select the linked .pam/.pac mesh to perform geometry replacement; this sidecar is used for material and texture mapping.")
    elif extension not in SUPPORTED_MESH_FORMATS:
        errors.append(f"{extension or 'unknown'} is not a supported replacement mesh format.")
        support_level = "Blocked"
        export_supported = False
        geometry_mode = "unsupported"
        lod_mode = "unsupported"
    elif parsed_mesh is None or submesh_count <= 0 or face_count <= 0:
        errors.append("The target mesh could not be parsed into replaceable draw sections.")
        support_level = "Blocked"
        export_supported = False
        geometry_mode = "unparsed"
        lod_mode = "unknown"
    elif extension == ".pam":
        support_level = "Supported"
        export_supported = True
        geometry_mode = "flexible_static_replacement"
        lod_mode = "paired_pamlod" if _has_related_extension(related_files, ".pamlod") else "selected_mesh_only"
        if not _has_related_extension(related_files, ".pamlod"):
            warnings.append("No paired .pamlod was found; export will replace only the selected .pam payload.")
    elif extension == ".pamlod":
        support_level = "Preview only"
        export_supported = False
        geometry_mode = "lod_family_target"
        lod_mode = "replace_paired_pam_first" if _has_related_extension(related_files, ".pam") else "selected_lod_preview_only"
        errors.append(
            "Direct arbitrary .pamlod replacement is not enabled yet. Replace the paired .pam when available so LOD handling can stay consistent."
        )
    else:
        missing_metadata = _pac_missing_rebuild_metadata(parsed_mesh)
        if missing_metadata:
            support_level = "Blocked"
            export_supported = False
            geometry_mode = "pac_replacement_blocked"
            lod_mode = "pac_lods_preserved"
            errors.extend(missing_metadata)
        elif has_bones or category_hint in {"armor", "helmet", "cape/cloth", "character/skinned"}:
            support_level = "Experimental"
            export_supported = True
            geometry_mode = "pac_donor_skinning_replacement"
            lod_mode = "pac_lods_rebuilt_from_replacement"
            warnings.append(
                "PAC replacement will inherit skinning from nearest original donor vertices; test armor, helmets, and capes in game."
            )
        else:
            support_level = "Supported"
            export_supported = True
            geometry_mode = "pac_static_style_replacement"
            lod_mode = "pac_lods_rebuilt_from_replacement"

    if parsed_mesh is not None and not has_uvs:
        warnings.append("The parsed target has no UVs; replacement textures may not preview or render correctly.")

    if extension in MATERIAL_SIDECAR_EXTENSIONS:
        sidecar_mode = "selected_sidecar"
    else:
        sidecar_mode = "patch_when_available" if any(file.role == "Material sidecar" for file in related_files) else "no_sidecar_found"
    if sidecar_mode == "no_sidecar_found":
        warnings.append("No companion material sidecar was found; texture-slot export may be limited to existing mesh bindings.")
    warnings.extend(sidecar_feature_warnings)

    facts = (
        ("Format", extension.lstrip(".").upper() if extension else "Unknown"),
        ("Family", asset_family),
        ("Category", category_hint),
        ("Support", support_level),
        ("Submeshes", f"{submesh_count:,}"),
        ("Vertices", f"{vertex_count:,}"),
        ("Faces", f"{face_count:,}"),
        ("UVs", "Yes" if has_uvs else "No"),
        ("Skinning", "Yes" if has_bones else "No"),
        ("LOD", _describe_lod_mode(lod_mode)),
        ("Sidecar", _describe_sidecar_mode(sidecar_mode, related_files)),
        ("Texture slots", str(len(texture_bindings)) if texture_bindings else "Not inspected"),
    ) + tuple(sidecar_feature_facts)

    return ReplacementAssetProfile(
        source_path=source_path,
        mesh_format=extension.lstrip("."),
        category_hint=category_hint,
        asset_family=asset_family,
        support_level=support_level,
        replacement_support=support_level,
        export_supported=export_supported,
        geometry_mode=geometry_mode,
        lod_mode=lod_mode,
        sidecar_mode=sidecar_mode,
        required_companions=tuple(_required_companions(related_files, texture_bindings)),
        related_files=tuple(related_files),
        texture_bindings=tuple(texture_bindings),
        texture_summary=tuple(texture_summary),
        warnings=tuple(_dedupe(warnings)),
        errors=tuple(_dedupe(errors)),
        facts=facts,
    )


def classify_texture_binding(
    parameter_name: str,
    texture_path: str,
    *,
    sidecar_texts: Sequence[str] = (),
) -> TextureSlotClassification:
    """Classify a sidecar DDS slot for assignment, preview, and user-facing labels."""

    parameter_text = str(parameter_name or "").strip()
    path_text = str(texture_path or "").replace("\\", "/").strip()
    combined = f"{parameter_text} {PurePosixPath(path_text).name}".strip()
    normalized = re.sub(r"[^a-z0-9]+", "", combined.lower())

    def result(
        slot_kind: str,
        slot_label: str,
        semantic_type: str,
        semantic_subtype: str,
        visualized: bool,
        reason: str,
        packed_channels: Sequence[str] = (),
    ) -> TextureSlotClassification:
        return TextureSlotClassification(
            slot_kind=slot_kind,
            slot_label=slot_label,
            semantic_type=semantic_type,
            semantic_subtype=semantic_subtype,
            visualized=visualized,
            visual_state="Visualized" if visualized else "Not visualized",
            reason=reason,
            packed_channels=tuple(
                str(channel or "").strip().lower()
                for channel in packed_channels
                if str(channel or "").strip()
            ),
        )

    if "nonetexture" in normalized or "none_texture" in normalized:
        return result("material", "Placeholder", "unknown", "placeholder", False, "Placeholder texture reference; no DDS replacement is required unless the user explicitly assigns one.")
    if any(token in normalized for token in ("wrinklecolortexture", "wrinklediffuse", "wrinklealbedo")):
        return result("base", "Wrinkle color", "color", "wrinkle_color", True, "Wrinkle color is shown as a visible color layer when selected.")
    if any(token in normalized for token in ("wrinklenormal", "wrinklenormaltexture")):
        return result("normal", "Wrinkle normal", "normal", "wrinkle_normal", True, "Wrinkle normal is routed into the preview normal-map input.")
    if any(token in normalized for token in ("wrinkledisplacement", "wrinkleheight")):
        return result("height", "Wrinkle displacement", "height", "wrinkle_displacement", True, "Wrinkle displacement is routed into the preview height input.")
    if any(token in normalized for token in ("skindetailmask", "skinmask")):
        return result("material", "Skin detail mask", "mask", "skin_detail_mask", False, "Skin detail masks affect the character shader, but the alignment preview cannot reproduce that shader exactly.", ("skin", "detail"))
    if any(token in normalized for token in ("damageblendingdiffuse", "damagediffuse")):
        return result("base", "Damage diffuse", "color", "damage_diffuse", True, "Damage diffuse is shown as a visible color layer when selected.")
    if any(token in normalized for token in ("damageblendingnormal", "damagenormal")):
        return result("normal", "Damage normal", "normal", "damage_normal", True, "Damage normal is routed into the preview normal-map input.")
    if any(token in normalized for token in ("damageblendingmaterial", "damagematerial", "damagetexture")):
        return result("material", "Damage material", "mask", "damage_material", True, "Damage material is routed into the preview material-mask input.", ("damage",))
    if any(token in normalized for token in ("waterfoam", "foamtexture")):
        return result("base", "Water foam", "color", "water_foam", False, "Water foam is preserved for export; the alignment preview does not reproduce the water shader.")
    if any(token in normalized for token in ("rgbtexture", "colortextureg", "colortextureb", "normaltextureg", "normaltextureb", "materialtextureg", "materialtextureb", "heighttextureg", "heighttextureb")):
        return result("material", "RGB channel layer", "mask", "rgb_layer", False, "RGB channel texture sets drive specialized multi-layer shaders and are preserved for export.")
    if "grimediffuse" in normalized:
        return result("base", "Grime diffuse", "color", "diffuse", True, "Grime diffuse is shown as a visible base/color layer.")
    if "grimenormal" in normalized:
        return result("normal", "Grime normal", "normal", "normal", True, "Grime normal is routed into the preview normal-map input.")
    if "grimematerial" in normalized:
        return result("material", "Grime material", "mask", "material_mask", True, "Grime material is routed into the preview material-mask input.")
    if "colorblendingmask" in normalized:
        return result(
            "material",
            "Color blend mask",
            "mask",
            "color_blending_mask",
            False,
            "Color blending masks affect export, but the current preview shader cannot reproduce the blend layer exactly.",
            ("blend",),
        )
    if "detailmask" in normalized:
        return result(
            "material",
            "Detail mask",
            "mask",
            "detail_mask",
            False,
            "Detail masks select shader detail layers that are not fully reproduced in the alignment preview.",
            ("detail",),
        )
    if any(token in normalized for token in ("detaildiffuse", "detailalbedo", "detailcolor")):
        return result("base", "Detail diffuse", "color", "detail_diffuse", True, "Detail diffuse is shown as a visible base/color layer.")
    if "detailnormal" in normalized:
        return result("normal", "Detail normal", "normal", "detail_normal", True, "Detail normal is routed into the preview normal-map input.")
    if "detailmaterial" in normalized:
        return result("material", "Detail material", "mask", "detail_material_mask", True, "Detail material is routed into the preview material-mask input.", ("detail",))
    if any(token in normalized for token in ("flowtexture", "directiontexture", "ssdmdirection", "ssdm", "vectortexture", "pivottexture", "positiontexture")):
        return result(
            "material",
            "Vector / flow",
            "vector",
            "flow_vector" if "flow" in normalized else "direction_vector",
            False,
            "Vector, flow, SSDM, pivot, and position maps are preserved for export but are not represented by the alignment preview shader.",
            ("vector",),
        )
    if "emissive" in normalized or "glow" in normalized or "illum" in normalized:
        return result("base", "Emissive", "emissive", "emissive", True, "Emissive-like shader parameter is shown as a visible texture layer.")
    if any(token in normalized for token in ("basecolor", "basecolour", "overlaycolor", "diffuse", "albedo", "colortexture", "basetexture", "tintcolor", "decalbasecolor")):
        return result("base", "Base / diffuse", "color", "albedo", True, "Color-like shader parameter is shown as the visible base texture.")
    if "normal" in normalized:
        return result("normal", "Normal", "normal", "normal", True, "Normal parameter is routed into the preview normal-map input.")
    if any(token in normalized for token in ("parallaxmaterial", "materialparallax")):
        return result("material", "Material / mask", "mask", "material_mask", True, "Parallax material parameter is routed into the preview material-mask input.")
    if any(token in normalized for token in ("height", "displacement", "depth", "parallax", "pom", "ssdm", "bump")):
        return result("height", "Height", "height", "height", True, "Height/displacement parameter is routed into the preview height input.")
    if any(token in normalized for token in ("opacity", "alpha", "alphatexture", "transparency")):
        return result(
            "material",
            "Opacity mask",
            "mask",
            "opacity_mask",
            False,
            "Opacity masks are preserved for export; alpha cutout is not represented in the current alignment preview.",
            ("alpha",),
        )
    if "subsurface" in normalized:
        return result("material", "Subsurface mask", "mask", "subsurface", True, "Subsurface masks are routed into the preview material-mask input as technical shader support data.", ("subsurface",))
    if any(token in normalized for token in ("material", "roughness", "gloss", "smoothness", "metallic", "metalness", "specular", "ao", "occlusion", "mask")):
        return result("material", "Material / mask", "mask", "material_mask", True, "Material-like parameter is routed into the preview material-mask input.")

    semantic = infer_texture_semantics(path_text, sidecar_texts=tuple(sidecar_texts) + ((parameter_text,) if parameter_text else ()))
    semantic_type = str(getattr(semantic, "texture_type", "") or "").strip().lower() or "unknown"
    semantic_subtype = str(getattr(semantic, "semantic_subtype", "") or "").strip().lower() or semantic_type
    packed_channels = tuple(
        str(channel or "").strip().lower()
        for channel in getattr(semantic, "packed_channels", ())
        if str(channel or "").strip()
    )
    if semantic_type in {"color", "ui", "emissive", "impostor"}:
        return result("base", semantic_subtype.replace("_", " ").title(), semantic_type, semantic_subtype, True, "DDS semantics identify this as visible color data.")
    if semantic_type == "normal":
        return result("normal", "Normal", semantic_type, semantic_subtype, True, "DDS semantics identify this as a normal map.")
    if semantic_type == "height" or semantic_subtype in {"displacement", "parallax_height", "height", "bump"}:
        return result("height", semantic_subtype.replace("_", " ").title(), semantic_type, semantic_subtype, True, "DDS semantics identify this as height/displacement data.")
    if semantic_type in {"mask", "roughness"}:
        return result("material", semantic_subtype.replace("_", " ").title(), semantic_type, semantic_subtype, True, "DDS semantics identify this as material-mask data.", packed_channels)
    if semantic_type == "vector":
        return result("material", semantic_subtype.replace("_", " ").title(), semantic_type, semantic_subtype, False, "Vector data is preserved for export but is not represented by the alignment preview shader.", packed_channels)
    if path_text.lower().endswith(".dds"):
        return result("base", "Base / diffuse", "color", "albedo", True, "DDS path has no technical semantic signal, so it is treated as visible color.")
    return result("material", "Unknown", semantic_type, semantic_subtype, False, "Slot is exported if assigned, but no reliable preview route was identified.")


def summarize_texture_bindings(
    texture_bindings: Sequence[ReplacementTextureBinding],
) -> tuple[tuple[str, str], ...]:
    if not texture_bindings:
        return ()
    by_label = Counter(binding.slot_label or binding.slot_kind or "Unknown" for binding in texture_bindings)
    visualized = sum(1 for binding in texture_bindings if binding.visualized)
    not_visualized = len(texture_bindings) - visualized
    summary: list[tuple[str, str]] = [
        ("Total", f"{len(texture_bindings):,}"),
        ("Visualized", f"{visualized:,}"),
        ("Export-only", f"{not_visualized:,}"),
    ]
    summary.extend((label, f"{count:,}") for label, count in by_label.most_common(8))
    return tuple(summary)


def _classify_sidecar_texture_bindings(
    sidecar_texture_bindings: Sequence[object],
) -> tuple[ReplacementTextureBinding, ...]:
    result: list[ReplacementTextureBinding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for binding in sidecar_texture_bindings:
        texture_path = str(getattr(binding, "texture_path", "") or "").replace("\\", "/").strip()
        parameter_name = str(getattr(binding, "parameter_name", "") or "").strip()
        part_name = str(getattr(binding, "part_name", "") or getattr(binding, "submesh_name", "") or "").strip()
        material_name = str(getattr(binding, "material_name", "") or part_name).strip()
        shader_family = str(getattr(binding, "shader_family", "") or "").strip()
        if not texture_path:
            continue
        key = (part_name.lower(), shader_family.lower(), parameter_name.lower(), texture_path.lower())
        if key in seen:
            continue
        seen.add(key)
        classification = classify_texture_binding(parameter_name, texture_path)
        result.append(
            ReplacementTextureBinding(
                sidecar_kind=str(getattr(binding, "sidecar_kind", "") or "").strip(),
                linked_mesh_path=str(getattr(binding, "linked_mesh_path", "") or "").strip(),
                part_name=part_name,
                material_name=material_name,
                shader_family=shader_family,
                parameter_name=parameter_name,
                texture_path=texture_path,
                slot_kind=classification.slot_kind,
                slot_label=classification.slot_label,
                semantic_type=classification.semantic_type,
                semantic_subtype=classification.semantic_subtype,
                visualized=classification.visualized,
                visual_state=classification.visual_state,
                resolved_texture_exists=bool(getattr(binding, "resolved_texture_exists", False)),
                reason=classification.reason,
                source=str(getattr(binding, "sidecar_path", "") or "").strip(),
            )
        )
    return tuple(result)


def _find_related_files(
    entry: ArchiveEntry,
    archive_entries_by_basename: Mapping[str, Sequence[ArchiveEntry]],
) -> tuple[ReplacementRelatedFile, ...]:
    source_path = str(getattr(entry, "path", "") or "").replace("\\", "/")
    source_extension = str(getattr(entry, "extension", "") or PurePosixPath(source_path).suffix).lower()
    source_stem = PurePosixPath(source_path).stem
    candidates: list[tuple[str, str]] = []

    def add(name: str, confidence: str = "same-stem") -> None:
        normalized = str(name or "").strip().lower()
        if normalized:
            candidates.append((normalized, confidence))

    if source_stem:
        for suffix in (".xml", ".meshinfo", ".hkx"):
            add(f"{source_stem}{suffix}")
        if source_extension == ".pam":
            for suffix in (".pamlod", ".pami", ".pam_xml", ".pamlod_xml"):
                add(f"{source_stem}{suffix}")
        elif source_extension == ".pamlod":
            for suffix in (".pam", ".pami", ".pamlod_xml", ".pam_xml"):
                add(f"{source_stem}{suffix}")
        elif source_extension == ".pac":
            for suffix in (".pab", ".pabc", ".pac_xml", ".prefabdata.xml", ".prefabdata_xml"):
                add(f"{source_stem}{suffix}")
            for skeleton_stem in _probable_pab_family_stems(source_path):
                add(f"{skeleton_stem}.pab", "family-skeleton")
                add(f"{skeleton_stem}.pabc", "family-skeleton")
        elif source_extension == ".pami":
            for suffix in (".pam", ".pamlod", ".pam_xml", ".pamlod_xml"):
                add(f"{source_stem}{suffix}", "sidecar-linked")
        elif source_extension == ".pac_xml":
            add(f"{source_stem}.pac", "sidecar-linked")
            for suffix in (".pab", ".pabc", ".meshinfo", ".hkx"):
                add(f"{source_stem}{suffix}", "sidecar-linked")
            for skeleton_stem in _probable_pab_family_stems(source_path):
                add(f"{skeleton_stem}.pab", "family-skeleton")
                add(f"{skeleton_stem}.pabc", "family-skeleton")

    for token in _family_tokens(source_path):
        for suffix in (".pac", ".pam", ".pamlod", ".pab", ".pami", ".pac_xml", ".pam_xml", ".pamlod_xml", ".meshinfo", ".hkx"):
            add(f"{token}{suffix}", "family")

    related: list[ReplacementRelatedFile] = []
    seen_paths: set[str] = set()
    for basename, confidence in candidates:
        for candidate in archive_entries_by_basename.get(basename, ()):
            candidate_path = str(getattr(candidate, "path", "") or "").replace("\\", "/")
            if not candidate_path or candidate_path == source_path or candidate_path.lower() in seen_paths:
                continue
            seen_paths.add(candidate_path.lower())
            extension = str(getattr(candidate, "extension", "") or PurePosixPath(candidate_path).suffix).lower()
            related.append(
                ReplacementRelatedFile(
                    path=candidate_path,
                    extension=extension,
                    role=_related_role(extension),
                    confidence=confidence,
                )
            )
    return tuple(sorted(related, key=lambda item: (_role_sort_key(item.role), item.path.lower())))


def _related_role(extension: str) -> str:
    normalized = str(extension or "").strip().lower()
    if normalized in MATERIAL_SIDECAR_EXTENSIONS:
        return "Material sidecar"
    if normalized == ".pamlod":
        return "LOD mesh"
    if normalized in {".pam", ".pac"}:
        return "Paired mesh"
    if normalized in {".pab", ".pabc"}:
        return "Skeleton"
    if normalized == ".hkx":
        return "Animation/physics"
    if normalized in METADATA_EXTENSIONS:
        return "Metadata"
    return "Related"


def _role_sort_key(role: str) -> int:
    order = {
        "Paired mesh": 0,
        "LOD mesh": 1,
        "Material sidecar": 2,
        "Skeleton": 3,
        "Animation/physics": 4,
        "Metadata": 5,
    }
    return order.get(role, 99)


def _has_related_extension(related_files: Sequence[ReplacementRelatedFile], extension: str) -> bool:
    normalized = str(extension or "").strip().lower()
    return any(file.extension == normalized for file in related_files)


def _infer_category_hint(
    source_path: str,
    *,
    parsed_mesh: ParsedMesh | None,
    related_files: Sequence[ReplacementRelatedFile],
) -> str:
    text_parts = [source_path]
    if parsed_mesh is not None:
        for submesh in getattr(parsed_mesh, "submeshes", ()) or ():
            text_parts.append(str(getattr(submesh, "name", "") or ""))
            text_parts.append(str(getattr(submesh, "material", "") or ""))
            text_parts.append(str(getattr(submesh, "texture", "") or ""))
    text_parts.extend(file.path for file in related_files[:16])
    text = " ".join(text_parts).lower()
    normalized_path = str(source_path or "").replace("\\", "/").lower()
    if any(token in normalized_path for token in ("/13_hel/", "_hel_", "helmet", "helm")):
        return "helmet"
    if any(token in normalized_path for token in ("/19_cloak/", "cloak", "cape", "cloth", "mantle", "skirt")):
        return "cape/cloth"
    if any(token in normalized_path for token in ("/weapon/", "weapon", "sword", "dagger", "blade", "bow", "axe", "spear")):
        return "weapon"
    if "/2_mon/" in normalized_path or "/1_pc/" in normalized_path:
        if any(token in normalized_path for token in ("/head/", "/nude/", "/hair/")):
            return "character/skinned"
    category_tokens = (
        ("cape/cloth", ("cape", "cloak", "cloth", "mantle", "skirt", "fabric")),
        ("helmet", ("helmet", "helm", "mask", "faceguard")),
        ("armor", ("armor", "armour", "chest", "torso", "plate", "mail", "gauntlet", "boots")),
        ("weapon", ("weapon", "sword", "dagger", "blade", "bow", "axe", "spear", "handle", "guard")),
        ("character/skinned", ("character", "body", "hair", "skin", "pc_", "npc_")),
        ("static prop", ("prop", "static", "object", "furniture", "building")),
    )
    text_tokens = set(re.findall(r"[a-z0-9]+", text))
    for label, tokens in category_tokens:
        if any(token in text_tokens for token in tokens):
            return label
    if parsed_mesh is not None and bool(getattr(parsed_mesh, "has_bones", False)):
        return "character/skinned"
    return "unknown"


def _infer_asset_family(source_path: str) -> str:
    normalized = str(source_path or "").replace("\\", "/").lower()
    if "/leveldata/" in normalized or normalized.startswith("leveldata/"):
        return "leveldata"
    if "/effect/" in normalized or normalized.startswith("effect/"):
        return "effect"
    if "/character/modelproperty/1_pc/" in normalized or "/character/model/1_pc/" in normalized:
        return "character"
    if "/character/modelproperty/2_mon/" in normalized or "/character/model/2_mon/" in normalized:
        return "monster"
    if "/character/modelproperty/3_npc/" in normalized or "/character/model/3_npc/" in normalized:
        return "npc"
    if "/character/modelproperty/4_riding/" in normalized or "/character/model/4_riding/" in normalized:
        return "riding"
    if "/character/modelproperty/6_object/" in normalized or "/character/model/6_object/" in normalized:
        return "object"
    if "/object/" in normalized or normalized.startswith("object/"):
        return "object"
    if "/character/" in normalized or normalized.startswith("character/"):
        return "character"
    return "unknown"


def _required_companions(
    related_files: Sequence[ReplacementRelatedFile],
    texture_bindings: Sequence[ReplacementTextureBinding],
) -> tuple[str, ...]:
    values: list[str] = []
    for related in related_files:
        if related.role in {"Skeleton", "Material sidecar", "Metadata", "Animation/physics", "Paired mesh", "LOD mesh"}:
            values.append(related.path)
    for binding in texture_bindings:
        if binding.linked_mesh_path:
            values.append(binding.linked_mesh_path)
        if binding.texture_path:
            values.append(binding.texture_path)
    return tuple(_dedupe(values))


def _pac_missing_rebuild_metadata(parsed_mesh: ParsedMesh | None) -> list[str]:
    if parsed_mesh is None:
        return ["PAC metadata could not be inspected."]
    missing: list[str] = []
    for index, submesh in enumerate(getattr(parsed_mesh, "submeshes", ()) or ()):
        if int(getattr(submesh, "source_vertex_stride", 0) or 0) < 12:
            missing.append(f"PAC submesh {index} is missing source vertex stride metadata.")
        if not getattr(submesh, "source_vertex_offsets", None):
            missing.append(f"PAC submesh {index} is missing source vertex record offsets.")
        if int(getattr(submesh, "source_descriptor_offset", -1) or -1) < 0:
            missing.append(f"PAC submesh {index} is missing descriptor offset metadata.")
    return _dedupe(missing)


def _describe_lod_mode(lod_mode: str) -> str:
    labels = {
        "paired_pamlod": "Paired PAMLOD found",
        "selected_mesh_only": "Selected mesh only",
        "replace_paired_pam_first": "Use paired PAM",
        "selected_lod_preview_only": "Selected LOD preview only",
        "pac_lods_rebuilt_from_replacement": "PAC LOD sections rebuilt",
        "pac_lods_preserved": "PAC LOD metadata preserved",
        "unsupported": "Unsupported",
        "unknown": "Unknown",
    }
    return labels.get(lod_mode, lod_mode.replace("_", " ").title())


def _describe_sidecar_mode(sidecar_mode: str, related_files: Sequence[ReplacementRelatedFile]) -> str:
    if sidecar_mode == "selected_sidecar":
        return "Selected file"
    if sidecar_mode != "patch_when_available":
        return "Not found"
    count = sum(1 for file in related_files if file.role == "Material sidecar")
    return f"{count} found"


def _summarize_sidecar_features(sidecar_texts: Sequence[str]) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    combined_text = "\n".join(str(text or "") for text in sidecar_texts if str(text or "").strip())
    if not combined_text:
        return (), ()

    def _values(attr_name: str) -> tuple[str, ...]:
        pattern = re.compile(rf'{re.escape(attr_name)}="([^"]*)"', re.IGNORECASE)
        return tuple(_dedupe([match.group(1) for match in pattern.finditer(combined_text)]))

    pbd_names = _values("_pbdSimulationMaterialName")
    cloth_categories = _values("_clothCategory")
    if not cloth_categories:
        cloth_categories = tuple(
            _dedupe(
                re.findall(
                    r'MaterialParameterClothCategory\b[^>]*\b_value="([^"]*)"',
                    combined_text,
                    flags=re.IGNORECASE,
                )
            )
        )
    equip_types = _values("_equipType")
    physics_files = _values("_physicsFileName")
    wrinkle_files = _values("_wrinkleFileName")
    texture_ref_count = len(re.findall(r"<\s*TextureRef\b", combined_text, flags=re.IGNORECASE))
    override_pbd_count = len(re.findall(r"<\s*OverridedPbdMaterialProperty\b", combined_text, flags=re.IGNORECASE))

    facts: list[tuple[str, str]] = []
    warnings: list[str] = []

    if pbd_names:
        preview = ", ".join(pbd_names[:3]) + (" ..." if len(pbd_names) > 3 else "")
        facts.append(("PBD/cloth", preview))
        warnings.append("Material sidecar contains cloth/PBD simulation settings; preserve the patched sidecar and test motion-sensitive parts in game.")
    if cloth_categories:
        preview = ", ".join(cloth_categories[:4]) + (" ..." if len(cloth_categories) > 4 else "")
        facts.append(("Cloth material", preview))
    if equip_types:
        preview = ", ".join(equip_types[:4]) + (" ..." if len(equip_types) > 4 else "")
        facts.append(("Equip anchors", preview))
        warnings.append("Sidecar contains stack/equip attachment offsets; replacement geometry should preserve the intended anchor placement.")
    if physics_files:
        facts.append(("Physics refs", str(len(physics_files))))
        warnings.append("Sidecar references physics files; model replacement should preserve compatible collision/physics companion data when available.")
    if wrinkle_files:
        facts.append(("Wrinkle refs", str(len(wrinkle_files))))
        warnings.append("Sidecar references wrinkle descriptors; facial/head replacements may need matching wrinkle data or original descriptor preservation.")
    if texture_ref_count > 0:
        facts.append(("TextureRef slots", f"{texture_ref_count:,} raw"))
    if override_pbd_count > 0:
        facts.append(("PBD overrides", f"{override_pbd_count:,}"))

    return tuple(facts), tuple(_dedupe(warnings))


def _family_tokens(path: str) -> tuple[str, ...]:
    stem = PurePosixPath(str(path or "").replace("\\", "/")).stem.lower()
    if not stem:
        return ()
    tokens = [stem]
    for suffix in ("_lod", "_lodd", "_low", "_high", "_mesh", "_model"):
        if stem.endswith(suffix):
            tokens.append(stem[: -len(suffix)])
    for marker in ("_lod", "_low", "_high"):
        if marker in stem:
            tokens.append(stem.split(marker, 1)[0])
    return tuple(_dedupe(tokens))


def _probable_pab_family_stems(path: str) -> tuple[str, ...]:
    """Return shared skeleton stems used by character/monster PAC families.

    Real extracted assets rarely use same-basename PAB files. Player gear tends
    to share class skeletons such as phm_01.pab or ptm_01.pab, while monster
    variants often reference a parent family skeleton from an ancestor folder.
    """

    normalized = str(path or "").replace("\\", "/").strip().lower()
    parts = [part for part in normalized.split("/") if part]
    stems: list[str] = []

    for part in parts:
        match = re.match(r"^\d+_([a-z]{2,5})$", part)
        if match:
            stems.append(f"{match.group(1)}_01")

    for part in parts:
        if re.match(r"^cd_m\d{4}_", part):
            stems.append(part)

    if "character/model/1_pc/" in normalized:
        stems.append("identityskeleton")
    return tuple(_dedupe(stems))


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
