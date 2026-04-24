"""Texture and material-sidecar planning for static mesh replacement."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence

from .mesh_parser import ParsedMesh
from .static_mesh_replacer import StaticSubmeshMapping, _semantic_tokens


@dataclass(slots=True)
class ReplacementTextureSlot:
    material_name: str
    slot_kind: str
    source_path: Path
    normal_space: str = ""


@dataclass(slots=True)
class ReplacementTextureSet:
    material_name: str
    slots: dict[str, ReplacementTextureSlot] = field(default_factory=dict)
    source_face_count: int = 0


@dataclass(slots=True)
class TextureSlotMapping:
    target_material_name: str
    target_texture_path: str
    slot_kind: str
    source_material_name: str
    source_path: Path
    output_texture_path: str
    normal_space: str = ""


@dataclass(slots=True)
class SidecarTextureParameterInjection:
    target_material_name: str
    parameter_name: str
    texture_path: str


@dataclass(slots=True)
class SidecarPatchPlan:
    sidecar_path: str
    texture_path_replacements: dict[str, str] = field(default_factory=dict)
    texture_parameter_injections: list[SidecarTextureParameterInjection] = field(default_factory=list)


@dataclass(slots=True)
class SidecarPatchReport:
    sidecar_path: str = ""
    replaced_count: int = 0
    unchanged_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TextureReplacementPayload:
    target_path: str
    payload_data: bytes
    kind: str
    source_path: Path
    note: str = ""


@dataclass(slots=True)
class TextureReplacementReport:
    texture_sets: list[ReplacementTextureSet] = field(default_factory=list)
    slot_mappings: list[TextureSlotMapping] = field(default_factory=list)
    sidecar_reports: list[SidecarPatchReport] = field(default_factory=list)
    generated_payloads: list[TextureReplacementPayload] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_TEXTURE_SUFFIXES: tuple[tuple[str, str, str], ...] = (
    ("base", "Base_Color", "base_color"),
    ("base", "BaseColor", "basecolor"),
    ("base", "Albedo", "albedo"),
    ("base", "Diffuse", "diffuse"),
    ("base", "Dif", "diffuse"),
    ("base", "Di", "diffuse"),
    ("base", "Color", "color"),
    ("normal", "Normal_OpenGL", "opengl"),
    ("normal", "Normal_DirectX", "directx"),
    ("normal", "Normal_DX", "directx"),
    ("normal", "Normal", ""),
    ("normal", "Nor", ""),
    ("normal", "No", ""),
    ("metallic", "Metallic", "metallic"),
    ("metallic", "Metalness", "metallic"),
    ("roughness", "Roughness", "roughness"),
    ("roughness", "Roughne", "roughness"),
    ("roughness", "Roughnes", "roughness"),
    ("roughness", "Rough", "roughness"),
    ("roughness", "Rou", "roughness"),
    ("roughness", "Ro", "roughness"),
    ("ao", "Mixed_AO", "ao"),
    ("ao", "AO", "ao"),
    ("height", "Displacement", "height"),
    ("height", "Height", "height"),
    ("height", "Hei", "height"),
    ("height", "He", "height"),
    ("height", "Disp", "height"),
    ("material", "Reflection", "material"),
    ("material", "Reflecti", "material"),
    ("material", "Reflect", "material"),
    ("material", "Ref", "material"),
    ("material", "Re", "material"),
    ("material", "Material", "material"),
    ("material", "Mask", "material"),
)


def analyze_replacement_textures(
    obj_mesh: ParsedMesh,
    texture_files: Sequence[Path],
    original_sidecar_texts: Sequence[str] = (),
    original_texture_refs: Sequence[object] = (),
) -> TextureReplacementReport:
    """Group replacement texture files and report likely material slots."""
    del original_sidecar_texts, original_texture_refs
    texture_sets = group_replacement_texture_sets(texture_files, obj_mesh=obj_mesh)
    report = TextureReplacementReport(texture_sets=list(texture_sets.values()))
    if not texture_sets and texture_files:
        report.warnings.append("No replacement texture files matched known material suffix patterns.")
    return report


def build_texture_replacement_payloads(
    *,
    obj_mesh: ParsedMesh,
    rebuilt_mesh: Optional[ParsedMesh] = None,
    texture_files: Sequence[Path],
    original_texture_refs: Sequence[object],
    original_sidecars: Sequence[tuple[object, str]],
    submesh_mappings: Sequence[StaticSubmeshMapping],
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    on_log: Optional[Callable[[str], None]] = None,
    enable_missing_base_color_parameters: bool = False,
    texture_slot_overrides: Sequence[object] = (),
    pac_driven_sidecar: bool = False,
) -> tuple[list[TextureReplacementPayload], TextureReplacementReport]:
    """Build generated DDS and patched sidecar payloads for a static replacement."""
    report = analyze_replacement_textures(obj_mesh, texture_files)
    texture_sets = {texture_set.material_name.lower(): texture_set for texture_set in report.texture_sets}
    if not texture_sets:
        return [], report

    _attach_source_face_counts(texture_sets, obj_mesh)
    target_to_source_material = _choose_source_materials_for_targets(obj_mesh, texture_sets, submesh_mappings, report)

    if pac_driven_sidecar and rebuilt_mesh is not None:
        generated_payloads = _build_rebuilt_pac_driven_payloads(
            obj_mesh=obj_mesh,
            rebuilt_mesh=rebuilt_mesh,
            texture_sets=texture_sets,
            original_texture_refs=original_texture_refs,
            original_sidecars=original_sidecars,
            submesh_mappings=submesh_mappings,
            target_to_source_material=target_to_source_material,
            texconv_path=texconv_path,
            read_original_texture_bytes=read_original_texture_bytes,
            original_texture_source_path=original_texture_source_path,
            report=report,
            on_log=on_log,
            enable_missing_base_color_parameters=enable_missing_base_color_parameters,
            texture_slot_overrides=texture_slot_overrides,
        )
        report.generated_payloads = generated_payloads
        _append_unused_texture_warnings(texture_sets, report)
        return list(report.generated_payloads), report

    texture_payloads: list[TextureReplacementPayload] = []
    sidecar_replacements_by_path: dict[str, str] = {}
    sidecar_parameter_injections: list[SidecarTextureParameterInjection] = []
    reference_by_target_path = _references_by_target_path(original_texture_refs)
    emitted_target_paths: set[str] = set()
    if texture_slot_overrides:
        override_payloads, override_replacements = _build_manual_texture_slot_override_payloads(
            texture_slot_overrides=texture_slot_overrides,
            reference_by_target_path=reference_by_target_path,
            texture_sets=texture_sets,
            texconv_path=texconv_path,
            read_original_texture_bytes=read_original_texture_bytes,
            original_texture_source_path=original_texture_source_path,
            report=report,
            on_log=on_log,
        )
        texture_payloads.extend(override_payloads)
        sidecar_replacements_by_path.update(override_replacements)
        emitted_target_paths.update(_normalize_texture_path(payload.target_path) for payload in override_payloads)

    skipped_inactive_target_count = 0
    for reference in original_texture_refs:
        target_path = _reference_target_path(reference)
        if not target_path:
            continue
        if _normalize_texture_path(target_path) in emitted_target_paths:
            continue
        if not _should_replace_original_texture_reference(reference, target_path):
            continue
        if not _reference_belongs_to_active_static_target(reference, target_path, target_to_source_material):
            skipped_inactive_target_count += 1
            continue
        target_material = str(getattr(reference, "material_name", "") or "").strip()
        source_material = _best_source_material_for_target(target_material, target_to_source_material)
        if not source_material:
            source_material = _best_source_material_for_target(
                PurePosixPath(str(target_path or "").replace("\\", "/")).stem,
                target_to_source_material,
            )
        texture_set = texture_sets.get(source_material.lower()) if source_material else None
        if texture_set is None:
            continue

        slot_kind = _infer_slot_kind(
            str(getattr(reference, "sidecar_parameter_name", "") or ""),
            target_path,
        )
        source_slot = _slot_for_target(texture_set, slot_kind)
        if source_slot is None:
            continue
        if slot_kind == "material" and source_slot.slot_kind != "material":
            report.warnings.append(
                f"{target_path} expects a packed material/mask texture; using {source_slot.slot_kind} source "
                f"{source_slot.source_path.name}. Bake or pack metallic/roughness/AO into the game's expected mask layout for best results."
            )

        target_entry = getattr(reference, "resolved_entry", None)
        if target_entry is None:
            report.warnings.append(f"Texture target could not be resolved in archive: {target_path}")
            continue
        output_texture_path = _replacement_output_texture_path(source_slot, target_path)

        try:
            payload = _build_texture_payload(
                source_slot,
                target_entry=target_entry,
                texconv_path=texconv_path,
                read_original_texture_bytes=read_original_texture_bytes,
                original_texture_source_path=original_texture_source_path,
                report=report,
                on_log=on_log,
            )
        except Exception as exc:
            report.errors.append(f"Failed to build replacement texture for {target_path}: {exc}")
            continue

        texture_payloads.append(
            TextureReplacementPayload(
                target_path=output_texture_path,
                payload_data=payload,
                kind="texture_generated",
                source_path=source_slot.source_path,
                note=f"{source_slot.material_name} {source_slot.slot_kind} -> {output_texture_path}",
            )
        )
        report.slot_mappings.append(
            TextureSlotMapping(
                target_material_name=target_material,
                target_texture_path=target_path,
                slot_kind=slot_kind,
                source_material_name=source_slot.material_name,
                source_path=source_slot.source_path,
                output_texture_path=output_texture_path,
                normal_space=source_slot.normal_space,
            )
        )
        original_reference_name = str(getattr(reference, "reference_name", "") or "").strip()
        if original_reference_name and original_reference_name != output_texture_path:
            sidecar_replacements_by_path[original_reference_name] = output_texture_path
        if target_path != output_texture_path:
            sidecar_replacements_by_path[target_path] = output_texture_path

    if skipped_inactive_target_count:
        report.warnings.append(
            f"Skipped {skipped_inactive_target_count:,} original texture binding(s) for draw/material slots with no replacement geometry."
        )

    if enable_missing_base_color_parameters:
        injected_payloads, injected_parameters = _build_missing_base_color_parameter_payloads(
            obj_mesh=obj_mesh,
            texture_sets=texture_sets,
            original_texture_refs=original_texture_refs,
            target_to_source_material=target_to_source_material,
            existing_slot_mappings=report.slot_mappings,
            texconv_path=texconv_path,
            read_original_texture_bytes=read_original_texture_bytes,
            original_texture_source_path=original_texture_source_path,
            report=report,
            on_log=on_log,
        )
        texture_payloads.extend(injected_payloads)
        sidecar_parameter_injections.extend(injected_parameters)
    elif _needs_missing_base_color_parameter_payloads(
        texture_sets=texture_sets,
        target_to_source_material=target_to_source_material,
        existing_slot_mappings=report.slot_mappings,
        original_sidecars=original_sidecars,
    ):
        report.warnings.append(
            "A replacement base-color texture has no safe existing material slot. "
            "The app did not inject a new .pac_xml material parameter because this can make some shaders render untextured."
        )

    sidecar_payloads: list[TextureReplacementPayload] = []
    if texture_payloads and (sidecar_replacements_by_path or sidecar_parameter_injections):
        for sidecar_entry, sidecar_text in original_sidecars:
            sidecar_path = str(getattr(sidecar_entry, "path", "") or "").strip()
            patched_text, sidecar_report = patch_material_sidecar_text(
                sidecar_text,
                SidecarPatchPlan(
                    sidecar_path=sidecar_path,
                    texture_path_replacements=sidecar_replacements_by_path,
                    texture_parameter_injections=sidecar_parameter_injections,
                ),
            )
            report.sidecar_reports.append(sidecar_report)
            if sidecar_report.replaced_count <= 0 and (sidecar_replacements_by_path or sidecar_parameter_injections):
                report.warnings.append(
                    f"Patched sidecar {PurePosixPath(sidecar_path).name} did not apply any texture path or parameter changes."
                )
                continue
            sidecar_payloads.append(
                TextureReplacementPayload(
                    target_path=sidecar_path,
                    payload_data=patched_text.encode("utf-8"),
                    kind="sidecar_generated",
                    source_path=Path(PurePosixPath(sidecar_path).name),
                    note="Patched material sidecar cloned from original archive entry.",
                )
            )

    report.generated_payloads = texture_payloads + sidecar_payloads
    _append_unused_texture_warnings(texture_sets, report)
    return list(report.generated_payloads), report


def patch_material_sidecar_text(
    original_text: str,
    sidecar_patch_plan: SidecarPatchPlan,
) -> tuple[str, SidecarPatchReport]:
    """Clone-patch sidecar text by replacing paths and optional compatible texture parameters."""
    patched = str(original_text or "")
    report = SidecarPatchReport(sidecar_path=sidecar_patch_plan.sidecar_path)
    for old_path, new_path in sidecar_patch_plan.texture_path_replacements.items():
        old_value = str(old_path or "").strip()
        new_value = str(new_path or "").strip()
        if not old_value or not new_value:
            continue
        if old_value == new_value:
            if old_value in patched:
                report.unchanged_count += 1
            continue
        occurrences = patched.count(old_value)
        if occurrences <= 0:
            report.warnings.append(f"Sidecar did not contain texture path: {old_value}")
            continue
        patched = patched.replace(old_value, new_value)
        report.replaced_count += occurrences
    for injection in sidecar_patch_plan.texture_parameter_injections:
        patched, injected = _inject_sidecar_texture_parameter(patched, injection, report)
        if injected:
            report.replaced_count += 1
    return patched, report


def _build_rebuilt_pac_driven_payloads(
    *,
    obj_mesh: ParsedMesh,
    rebuilt_mesh: ParsedMesh,
    texture_sets: Mapping[str, ReplacementTextureSet],
    original_texture_refs: Sequence[object],
    original_sidecars: Sequence[tuple[object, str]],
    submesh_mappings: Sequence[StaticSubmeshMapping],
    target_to_source_material: Mapping[str, str],
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    report: TextureReplacementReport,
    on_log: Optional[Callable[[str], None]],
    enable_missing_base_color_parameters: bool,
    texture_slot_overrides: Sequence[object],
) -> list[TextureReplacementPayload]:
    """Build texture and sidecar payloads from final rebuilt PAC/PAM draw sections.

    This path intentionally ignores unrelated original sidecar bindings. Only
    rebuilt submeshes with geometry are considered active texture targets.
    """
    del obj_mesh
    references_by_material = _references_by_material(original_texture_refs)
    references_by_target_path = _references_by_target_path(original_texture_refs)
    active_target_names = _active_rebuilt_material_names(rebuilt_mesh, submesh_mappings)
    if not active_target_names:
        report.warnings.append("PAC-driven material sidecar had no rebuilt draw sections with geometry to bind.")
        return []

    payloads: list[TextureReplacementPayload] = []
    sidecar_replacements_by_path: dict[str, str] = {}
    sidecar_parameter_injections: list[SidecarTextureParameterInjection] = []
    emitted_texture_paths: set[str] = set()
    manual_targets: set[str] = set()
    material_source_overrides: dict[str, str] = {}

    if texture_slot_overrides:
        override_payloads, override_replacements = _build_manual_texture_slot_override_payloads(
            texture_slot_overrides=texture_slot_overrides,
            reference_by_target_path=references_by_target_path,
            texture_sets=texture_sets,
            texconv_path=texconv_path,
            read_original_texture_bytes=read_original_texture_bytes,
            original_texture_source_path=original_texture_source_path,
            report=report,
            on_log=on_log,
        )
        payloads.extend(override_payloads)
        sidecar_replacements_by_path.update(override_replacements)
        for mapping in report.slot_mappings:
            normalized_target = _normalize_texture_path(mapping.output_texture_path or mapping.target_texture_path)
            if normalized_target:
                manual_targets.add(normalized_target)
            if mapping.target_material_name and mapping.source_material_name:
                material_source_overrides.setdefault(
                    _normalize_sidecar_material_name(mapping.target_material_name),
                    mapping.source_material_name,
                )
        emitted_texture_paths.update(_normalize_texture_path(payload.target_path) for payload in override_payloads)

    for target_name in active_target_names:
        target_key = _normalize_sidecar_material_name(target_name)
        source_material = material_source_overrides.get(target_key) or _best_source_material_for_target(
            target_name,
            target_to_source_material,
        )
        texture_set = texture_sets.get(str(source_material or "").strip().lower()) if source_material else None
        if texture_set is None:
            report.warnings.append(f"No replacement texture set was selected for rebuilt draw section {target_name}.")
            continue

        material_refs = _references_for_active_material(target_name, references_by_material)
        direct_refs = [
            reference
            for reference in material_refs
            if _is_direct_pac_driven_parameter(reference, _reference_target_path(reference))
        ]
        if not direct_refs:
            report.warnings.append(
                f"Rebuilt draw section {target_name} has no direct texture parameters in the original sidecar; "
                "base/normal/material slots may need manual sidecar authoring."
            )

        mapped_kinds: set[str] = set()
        for reference in direct_refs:
            target_path = _reference_target_path(reference)
            normalized_target = _normalize_texture_path(target_path)
            if not target_path or normalized_target in emitted_texture_paths:
                continue
            if normalized_target in manual_targets:
                continue
            target_entry = getattr(reference, "resolved_entry", None)
            if target_entry is None:
                report.warnings.append(f"Texture target could not be resolved in archive: {target_path}")
                continue
            parameter_name = str(getattr(reference, "sidecar_parameter_name", "") or "")
            slot_kind = _infer_slot_kind(parameter_name, target_path)
            source_slot = _slot_for_target(texture_set, slot_kind)
            if source_slot is None:
                continue
            if slot_kind == "material" and source_slot.slot_kind != "material":
                report.warnings.append(
                    f"{target_path} expects a packed material/mask texture; using {source_slot.slot_kind} source "
                    f"{source_slot.source_path.name}. Bake or pack metallic/roughness/AO into the game's expected mask layout for best results."
                )
            try:
                payload_data = _build_texture_payload(
                    source_slot,
                    target_entry=target_entry,
                    texconv_path=texconv_path,
                    read_original_texture_bytes=read_original_texture_bytes,
                    original_texture_source_path=original_texture_source_path,
                    report=report,
                    on_log=on_log,
                )
            except Exception as exc:
                report.errors.append(f"Failed to build replacement texture for {target_path}: {exc}")
                continue
            output_texture_path = _replacement_output_texture_path(source_slot, target_path)
            payloads.append(
                TextureReplacementPayload(
                    target_path=output_texture_path,
                    payload_data=payload_data,
                    kind="texture_generated",
                    source_path=source_slot.source_path,
                    note=f"PAC-driven {target_name} {slot_kind}: {source_slot.source_path.name}",
                )
            )
            report.slot_mappings.append(
                TextureSlotMapping(
                    target_material_name=target_name,
                    target_texture_path=target_path,
                    slot_kind=slot_kind,
                    source_material_name=source_slot.material_name,
                    source_path=source_slot.source_path,
                    output_texture_path=output_texture_path,
                    normal_space=source_slot.normal_space,
                )
            )
            original_reference_name = str(getattr(reference, "reference_name", "") or "").strip()
            if original_reference_name and original_reference_name != output_texture_path:
                sidecar_replacements_by_path[original_reference_name] = output_texture_path
            if target_path != output_texture_path:
                sidecar_replacements_by_path[target_path] = output_texture_path
            emitted_texture_paths.add(normalized_target)
            mapped_kinds.add(slot_kind)

        if "base" not in mapped_kinds and texture_set.slots.get("base") is not None:
            if enable_missing_base_color_parameters:
                injected_payloads, injected_parameters = _build_base_color_injection_for_target(
                    target_name=target_name,
                    texture_set=texture_set,
                    original_texture_refs=original_texture_refs,
                    material_refs=material_refs,
                    texconv_path=texconv_path,
                    read_original_texture_bytes=read_original_texture_bytes,
                    original_texture_source_path=original_texture_source_path,
                    report=report,
                    on_log=on_log,
                )
                payloads.extend(injected_payloads)
                sidecar_parameter_injections.extend(injected_parameters)
            else:
                report.warnings.append(
                    f"{target_name}: base color source {texture_set.slots['base'].source_path.name} is available, "
                    "but the original wrapper has no direct base/overlay slot. Enable PAC XML rebuild/injection to add one."
                )

    sidecar_payloads = _build_patched_sidecar_payloads(
        original_sidecars=original_sidecars,
        sidecar_replacements_by_path=sidecar_replacements_by_path,
        sidecar_parameter_injections=sidecar_parameter_injections,
        report=report,
        include_unchanged_clone=bool(payloads),
    )
    if payloads and not sidecar_payloads and original_sidecars:
        report.warnings.append(
            "PAC-driven texture payloads were built, but no .pac_xml sidecar changes were applied. "
            "This is expected only when texture paths are overwritten in-place."
        )
    elif sidecar_payloads:
        report.warnings.append(
            f"PAC-driven material sidecar rebuild scoped texture bindings to {len(active_target_names):,} rebuilt draw section(s)."
        )
    return payloads + sidecar_payloads


def _active_rebuilt_material_names(
    rebuilt_mesh: ParsedMesh,
    submesh_mappings: Sequence[StaticSubmeshMapping],
) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    mapping_names_by_index = {
        int(mapping.target_submesh_index): str(mapping.target_submesh_name or "").strip()
        for mapping in submesh_mappings
    }
    for index, submesh in enumerate(rebuilt_mesh.submeshes):
        if not getattr(submesh, "vertices", None) or not getattr(submesh, "faces", None):
            continue
        name = (
            str(getattr(submesh, "material", "") or "").strip()
            or str(getattr(submesh, "name", "") or "").strip()
            or mapping_names_by_index.get(index, "")
            or f"target {index}"
        )
        key = _normalize_sidecar_material_name(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def _references_by_material(original_texture_refs: Sequence[object]) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {}
    for reference in original_texture_refs:
        if str(getattr(reference, "reference_kind", "texture") or "texture").strip().lower() != "texture":
            continue
        material_name = str(getattr(reference, "material_name", "") or "").strip()
        if not material_name:
            continue
        result.setdefault(_normalize_sidecar_material_name(material_name), []).append(reference)
    return result


def _references_for_active_material(
    target_name: str,
    references_by_material: Mapping[str, Sequence[object]],
) -> list[object]:
    target_key = _normalize_sidecar_material_name(target_name)
    if target_key in references_by_material:
        return list(references_by_material[target_key])
    scored: list[tuple[float, object]] = []
    for material_key, references in references_by_material.items():
        if not material_key:
            continue
        representative = str(getattr(references[0], "material_name", "") or material_key)
        score = _sidecar_material_match_score(target_name, representative)
        if _sidecar_material_names_match(target_name, representative):
            score += 8.0
        for reference in references:
            path_text = _reference_target_path(reference)
            if _active_target_tokens_match_path(target_name, path_text):
                score += 4.0
        if score > 0:
            for reference in references:
                scored.append((score, reference))
    best_score = max((score for score, _reference in scored), default=0.0)
    if best_score < 6.0:
        return []
    return [reference for score, reference in scored if score == best_score]


def _is_direct_pac_driven_parameter(reference: object, target_path: str) -> bool:
    if not target_path.lower().endswith(".dds"):
        return False
    if _is_shared_material_layer_texture(target_path):
        return False
    parameter = str(getattr(reference, "sidecar_parameter_name", "") or "").strip().lower()
    return parameter in {
        "_overlaycolortexture",
        "_basecolortexture",
        "_diffusetexture",
        "_albedotexture",
        "_normaltexture",
        "_heighttexture",
        "_colorblendingmasktexture",
        "_detailmasktexture",
    }


def _build_base_color_injection_for_target(
    *,
    target_name: str,
    texture_set: ReplacementTextureSet,
    original_texture_refs: Sequence[object],
    material_refs: Sequence[object],
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    report: TextureReplacementReport,
    on_log: Optional[Callable[[str], None]],
) -> tuple[list[TextureReplacementPayload], list[SidecarTextureParameterInjection]]:
    base_slot = texture_set.slots.get("base")
    if base_slot is None:
        return [], []
    template_reference = _base_color_template_reference(material_refs) or _base_color_template_reference(original_texture_refs)
    if template_reference is None or getattr(template_reference, "resolved_entry", None) is None:
        report.warnings.append(
            f"{target_name}: cannot inject _overlayColorTexture because no compatible base texture template was found."
        )
        return [], []
    output_texture_path = _infer_base_color_path_for_material(
        original_texture_refs,
        target_name,
        fallback_parent=_reference_target_parent(template_reference),
    )
    if not output_texture_path:
        report.warnings.append(f"{target_name}: could not infer output path for injected base color texture.")
        return [], []
    try:
        payload_data = _build_texture_payload(
            base_slot,
            target_entry=getattr(template_reference, "resolved_entry", None),
            texconv_path=texconv_path,
            read_original_texture_bytes=read_original_texture_bytes,
            original_texture_source_path=original_texture_source_path,
            report=report,
            on_log=on_log,
        )
    except Exception as exc:
        report.errors.append(f"Failed to build injected base-color texture for {target_name}: {exc}")
        return [], []
    payload = TextureReplacementPayload(
        target_path=output_texture_path,
        payload_data=payload_data,
        kind="texture_generated",
        source_path=base_slot.source_path,
        note=f"PAC-driven injected _overlayColorTexture for {target_name}",
    )
    report.slot_mappings.append(
        TextureSlotMapping(
            target_material_name=target_name,
            target_texture_path="(injected _overlayColorTexture)",
            slot_kind="base",
            source_material_name=base_slot.material_name,
            source_path=base_slot.source_path,
            output_texture_path=output_texture_path,
            normal_space=base_slot.normal_space,
        )
    )
    report.warnings.append(
        f"PAC XML rebuild: added _overlayColorTexture for {target_name} using {base_slot.source_path.name}."
    )
    return [payload], [
        SidecarTextureParameterInjection(
            target_material_name=target_name,
            parameter_name="_overlayColorTexture",
            texture_path=output_texture_path,
        )
    ]


def _build_patched_sidecar_payloads(
    *,
    original_sidecars: Sequence[tuple[object, str]],
    sidecar_replacements_by_path: Mapping[str, str],
    sidecar_parameter_injections: Sequence[SidecarTextureParameterInjection],
    report: TextureReplacementReport,
    include_unchanged_clone: bool = False,
) -> list[TextureReplacementPayload]:
    if not original_sidecars or not (include_unchanged_clone or sidecar_replacements_by_path or sidecar_parameter_injections):
        return []
    sidecar_payloads: list[TextureReplacementPayload] = []
    for sidecar_entry, sidecar_text in original_sidecars:
        sidecar_path = str(getattr(sidecar_entry, "path", "") or "").strip()
        patched_text, sidecar_report = patch_material_sidecar_text(
            sidecar_text,
            SidecarPatchPlan(
                sidecar_path=sidecar_path,
                texture_path_replacements=dict(sidecar_replacements_by_path),
                texture_parameter_injections=list(sidecar_parameter_injections),
            ),
        )
        report.sidecar_reports.append(sidecar_report)
        if sidecar_report.replaced_count <= 0 and not include_unchanged_clone:
            report.warnings.append(
                f"Patched sidecar {PurePosixPath(sidecar_path).name} did not apply any texture path or parameter changes."
            )
            continue
        payload_note = (
            "PAC-driven material sidecar cloned from original archive entry."
            if sidecar_report.replaced_count <= 0
            else "PAC-driven material sidecar patched from original archive entry."
        )
        sidecar_payloads.append(
            TextureReplacementPayload(
                target_path=sidecar_path,
                payload_data=patched_text.encode("utf-8"),
                kind="sidecar_generated",
                source_path=Path(PurePosixPath(sidecar_path).name),
                note=payload_note,
            )
        )
    return sidecar_payloads


def _references_by_target_path(original_texture_refs: Sequence[object]) -> dict[str, object]:
    references: dict[str, object] = {}
    for reference in original_texture_refs:
        target_path = _reference_target_path(reference)
        if not target_path:
            continue
        references.setdefault(_normalize_texture_path(target_path), reference)
        reference_name = str(getattr(reference, "reference_name", "") or "").strip()
        if reference_name:
            references.setdefault(_normalize_texture_path(reference_name), reference)
    return references


def _normalize_texture_path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip().lower()


def _build_manual_texture_slot_override_payloads(
    *,
    texture_slot_overrides: Sequence[object],
    reference_by_target_path: Mapping[str, object],
    texture_sets: Mapping[str, ReplacementTextureSet],
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    report: TextureReplacementReport,
    on_log: Optional[Callable[[str], None]],
) -> tuple[list[TextureReplacementPayload], dict[str, str]]:
    payloads: list[TextureReplacementPayload] = []
    sidecar_replacements: dict[str, str] = {}
    emitted_targets: set[str] = set()
    for override in texture_slot_overrides:
        if not bool(getattr(override, "enabled", True)):
            continue
        target_path = str(getattr(override, "target_texture_path", "") or "").replace("\\", "/").strip()
        source_path_text = str(getattr(override, "source_path", "") or "").strip()
        if not target_path or not source_path_text:
            continue
        normalized_target = _normalize_texture_path(target_path)
        if normalized_target in emitted_targets:
            continue
        reference = reference_by_target_path.get(normalized_target)
        if reference is None:
            report.warnings.append(f"Manual texture slot target was not found in original bindings: {target_path}")
            continue
        target_entry = getattr(reference, "resolved_entry", None)
        if target_entry is None:
            report.warnings.append(f"Manual texture slot target could not be resolved in archive: {target_path}")
            continue
        source_path = Path(source_path_text).expanduser().resolve()
        if not source_path.is_file():
            report.warnings.append(f"Manual texture source file is missing: {source_path_text}")
            continue
        slot_kind = str(getattr(override, "slot_kind", "") or "").strip().lower() or _infer_slot_kind(
            str(getattr(reference, "sidecar_parameter_name", "") or ""),
            target_path,
        )
        source_slot = _source_slot_from_manual_path(source_path, slot_kind, texture_sets)
        try:
            payload = _build_texture_payload(
                source_slot,
                target_entry=target_entry,
                texconv_path=texconv_path,
                read_original_texture_bytes=read_original_texture_bytes,
                original_texture_source_path=original_texture_source_path,
                report=report,
                on_log=on_log,
            )
        except Exception as exc:
            report.errors.append(f"Failed to build manual replacement texture for {target_path}: {exc}")
            continue
        output_texture_path = _replacement_output_texture_path(source_slot, target_path)
        payloads.append(
            TextureReplacementPayload(
                target_path=output_texture_path,
                payload_data=payload,
                kind="texture_generated",
                source_path=source_slot.source_path,
                note=f"Manual texture slot: {source_slot.source_path.name} -> {output_texture_path}",
            )
        )
        report.slot_mappings.append(
            TextureSlotMapping(
                target_material_name=str(getattr(override, "target_material_name", "") or getattr(reference, "material_name", "") or ""),
                target_texture_path=target_path,
                slot_kind=slot_kind,
                source_material_name=source_slot.material_name,
                source_path=source_slot.source_path,
                output_texture_path=output_texture_path,
                normal_space=source_slot.normal_space,
            )
        )
        original_reference_name = str(getattr(reference, "reference_name", "") or "").strip()
        if original_reference_name and original_reference_name != output_texture_path:
            sidecar_replacements[original_reference_name] = output_texture_path
        if target_path != output_texture_path:
            sidecar_replacements[target_path] = output_texture_path
        emitted_targets.add(normalized_target)
    if payloads:
        report.warnings.append(f"Applied {len(payloads):,} manual texture slot override(s).")
    return payloads, sidecar_replacements


def _source_slot_from_manual_path(
    source_path: Path,
    slot_kind: str,
    texture_sets: Mapping[str, ReplacementTextureSet],
) -> ReplacementTextureSlot:
    resolved_source = source_path.expanduser().resolve()
    for texture_set in texture_sets.values():
        for slot in texture_set.slots.values():
            if slot.source_path.expanduser().resolve() == resolved_source:
                return ReplacementTextureSlot(
                    material_name=slot.material_name,
                    slot_kind=slot_kind or slot.slot_kind,
                    source_path=resolved_source,
                    normal_space=slot.normal_space,
                )
    material_name = _manual_source_material_name(resolved_source)
    normal_space = "opengl" if "opengl" in resolved_source.stem.lower() else ("directx" if "directx" in resolved_source.stem.lower() or "_dx" in resolved_source.stem.lower() else "")
    return ReplacementTextureSlot(
        material_name=material_name,
        slot_kind=slot_kind or "material",
        source_path=resolved_source,
        normal_space=normal_space,
    )


def _manual_source_material_name(source_path: Path) -> str:
    parsed = _parse_replacement_texture_filename(source_path, set())
    if parsed is not None:
        return parsed[0]
    stem = source_path.stem
    return re.sub(r"_(base|base_color|diffuse|albedo|normal|normal_opengl|normal_directx|normal_dx|height|disp|displacement|metallic|roughness|mixed_ao|ao|reflection|reflect|ref)$", "", stem, flags=re.IGNORECASE) or stem


def group_replacement_texture_sets(
    texture_files: Sequence[Path],
    *,
    obj_mesh: Optional[ParsedMesh] = None,
) -> dict[str, ReplacementTextureSet]:
    known_materials = {
        str(sm.material or sm.name or "").strip()
        for sm in (obj_mesh.submeshes if obj_mesh is not None else [])
        if str(sm.material or sm.name or "").strip()
    }
    grouped: dict[str, ReplacementTextureSet] = {}
    for raw_path in texture_files:
        path = raw_path.expanduser().resolve()
        if path.suffix.lower() not in {".png", ".dds", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}:
            continue
        parsed = _parse_replacement_texture_filename(path, known_materials)
        if parsed is None:
            continue
        material_name, slot_kind, normal_space = parsed
        texture_set = grouped.setdefault(material_name.lower(), ReplacementTextureSet(material_name=material_name))
        existing = texture_set.slots.get(slot_kind)
        if existing is None or _texture_slot_priority(path, slot_kind) > _texture_slot_priority(existing.source_path, existing.slot_kind):
            texture_set.slots[slot_kind] = ReplacementTextureSlot(
                material_name=material_name,
                slot_kind=slot_kind,
                source_path=path,
                normal_space=normal_space,
            )
    return grouped


def _parse_replacement_texture_filename(
    path: Path,
    known_materials: set[str],
) -> Optional[tuple[str, str, str]]:
    stem = path.stem
    lowered = stem.lower()
    matched: Optional[tuple[str, str, str, int]] = None
    for slot_kind, suffix, hint in _TEXTURE_SUFFIXES:
        suffix_lower = suffix.lower()
        for candidate_suffix in (f"_{suffix_lower}", suffix_lower):
            if not lowered.endswith(candidate_suffix):
                continue
            prefix = stem[: len(stem) - len(candidate_suffix)].rstrip("_-. ")
            if not prefix:
                continue
            prefix = _match_known_material_prefix(prefix, known_materials) or prefix
            score = len(candidate_suffix)
            if prefix in known_materials:
                score += 100
            if matched is None or score > matched[3]:
                normal_space = hint if slot_kind == "normal" and hint in {"opengl", "directx"} else ""
                matched = (prefix, slot_kind, normal_space, score)
    if matched is None:
        return None
    return matched[0], matched[1], matched[2]


def _match_known_material_prefix(prefix: str, known_materials: set[str]) -> str:
    raw_prefix = str(prefix or "").strip()
    if not raw_prefix or not known_materials:
        return ""
    prefix_lower = raw_prefix.lower()
    prefix_compact = re.sub(r"[^a-z0-9]+", "", prefix_lower)
    best_material = ""
    best_score = 0.0
    prefix_tokens = _semantic_tokens(raw_prefix)
    for material in known_materials:
        material_text = str(material or "").strip()
        if not material_text:
            continue
        material_lower = material_text.lower()
        material_compact = re.sub(r"[^a-z0-9]+", "", material_lower)
        score = 0.0
        if prefix_lower == material_lower:
            score += 100.0
        elif material_lower in prefix_lower:
            score += 85.0 + min(20.0, len(material_lower) * 0.25)
        elif material_compact and material_compact in prefix_compact:
            score += 75.0 + min(20.0, len(material_compact) * 0.25)
        material_tokens = _semantic_tokens(material_text)
        overlap = prefix_tokens & material_tokens
        if overlap:
            score += len(overlap) * 8.0 + min(10.0, sum(len(token) for token in overlap) * 0.4)
        if score > best_score:
            best_score = score
            best_material = material_text
    return best_material if best_score >= 12.0 else ""


def _texture_slot_priority(path: Path, slot_kind: str) -> tuple[int, int]:
    suffix = path.suffix.lower()
    return (2 if suffix == ".dds" else 1, len(slot_kind))


def _attach_source_face_counts(texture_sets: Mapping[str, ReplacementTextureSet], obj_mesh: ParsedMesh) -> None:
    for submesh in obj_mesh.submeshes:
        material_key = str(submesh.material or submesh.name or "").strip().lower()
        texture_set = texture_sets.get(material_key)
        if texture_set is not None:
            texture_set.source_face_count += len(submesh.faces)


def _choose_source_materials_for_targets(
    obj_mesh: ParsedMesh,
    texture_sets: Mapping[str, ReplacementTextureSet],
    submesh_mappings: Sequence[StaticSubmeshMapping],
    report: TextureReplacementReport,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for mapping in submesh_mappings:
        candidates: list[ReplacementTextureSet] = []
        for source_index in mapping.source_submesh_indices:
            if source_index < 0 or source_index >= len(obj_mesh.submeshes):
                continue
            source_submesh = obj_mesh.submeshes[source_index]
            material_key = str(source_submesh.material or source_submesh.name or "").strip().lower()
            texture_set = texture_sets.get(material_key)
            if texture_set is not None:
                candidates.append(texture_set)
            else:
                inferred_texture_set = _best_texture_set_for_source_mapping(
                    source_submesh,
                    mapping.target_submesh_name,
                    texture_sets,
                )
                if inferred_texture_set is not None:
                    candidates.append(inferred_texture_set)
                    report.warnings.append(
                        f"Texture set {inferred_texture_set.material_name} was matched to renamed source "
                        f"{source_submesh.material or source_submesh.name or source_index} for {mapping.target_submesh_name}."
                    )
        if not candidates:
            continue
        candidates.sort(
            key=lambda item: (
                _texture_source_candidate_score(mapping.target_submesh_name, item),
                item.source_face_count,
                len(item.slots),
            ),
            reverse=True,
        )
        chosen = candidates[0]
        result[mapping.target_submesh_name.lower()] = chosen.material_name
        if len({candidate.material_name.lower() for candidate in candidates}) > 1:
            report.warnings.append(
                f"Multiple replacement texture sets map to {mapping.target_submesh_name}; "
                f"using {chosen.material_name}. Bake/atlas textures to preserve separate source materials."
            )
    return result


def _best_texture_set_for_source_mapping(
    source_submesh: object,
    target_material_name: str,
    texture_sets: Mapping[str, ReplacementTextureSet],
) -> Optional[ReplacementTextureSet]:
    best: Optional[ReplacementTextureSet] = None
    best_score = 0.0
    source_text = f"{getattr(source_submesh, 'name', '')} {getattr(source_submesh, 'material', '')} {target_material_name}"
    source_tokens = _semantic_tokens(source_text)
    for texture_set in texture_sets.values():
        texture_tokens = _semantic_tokens(texture_set.material_name)
        if not texture_tokens:
            continue
        overlap = source_tokens & texture_tokens
        score = len(overlap) * 8.0
        if overlap:
            score += min(12.0, sum(len(token) for token in overlap) * 0.5)
        score += _texture_source_candidate_score(target_material_name, texture_set)
        if "blade" in source_tokens and "cuchilla" in texture_tokens:
            score += 12.0
        if "handle" in source_tokens and "mango" in texture_tokens:
            score += 10.0
        if "guard" in source_tokens and "soporte" in texture_tokens:
            score += 10.0
        if score > best_score:
            best_score = score
            best = texture_set
    return best if best_score >= 10.0 else None


def _texture_source_candidate_score(target_material_name: str, texture_set: ReplacementTextureSet) -> float:
    target_tokens = _semantic_tokens(target_material_name)
    source_tokens = _semantic_tokens(texture_set.material_name)
    if not target_tokens or not source_tokens:
        return 0.0
    overlap = target_tokens & source_tokens
    score = len(overlap) * 8.0
    if overlap:
        score += min(10.0, sum(len(token) for token in overlap) * 0.5)
    if "handle" in target_tokens and "mango" in source_tokens:
        score += 5.0
    if "blade" in target_tokens and "cuchilla" in source_tokens:
        score += 5.0
    if "guard" in target_tokens and "soporte" in source_tokens:
        score += 5.0
    if "acc" in target_tokens and ("circular" in source_tokens or "circulares" in source_tokens):
        score += 4.0
    if "handle" in target_tokens and ("tip" in source_tokens or "edge" in source_tokens):
        score -= 4.0
    return score


def _best_source_material_for_target(target_material: str, target_to_source_material: Mapping[str, str]) -> str:
    target_key = str(target_material or "").strip().lower()
    if target_key in target_to_source_material:
        return target_to_source_material[target_key]
    best_value = ""
    best_score = 0.0
    target_tokens = _material_tokens(target_key)
    for target_name, source_material in target_to_source_material.items():
        source_tokens = _material_tokens(f"{target_name} {source_material}")
        overlap = target_tokens & source_tokens
        score = float(len(overlap) * 8)
        for token in overlap:
            score += min(6.0, len(token) * 0.75)
        if target_name and (target_name in target_key or target_key in target_name):
            score += min(20.0, len(target_name) * 0.5)
        target_name_tokens = _material_tokens(target_name)
        if "sword" in target_tokens and "blade" in target_name_tokens:
            score += 14.0
        if "blade" in target_tokens and "blade" in target_name_tokens:
            score += 14.0
        if "handle" in target_tokens and "handle" in target_name_tokens:
            score += 14.0
        if "guard" in target_tokens and "guard" in target_name_tokens:
            score += 14.0
        if "acc" in target_tokens and "acc" in target_name_tokens:
            score += 14.0
        if score > best_score:
            best_score = score
            best_value = source_material
    return best_value if best_score >= 11.5 else ""


def _material_tokens(value: str) -> set[str]:
    stop_words = {"cd", "phm", "pc", "texture", "material", "mesh", "obj", "dds", "png"}
    tokens: set[str] = set()
    for raw_token in re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split():
        token = re.sub(r"\d+$", "", raw_token.strip())
        if len(token) > 1 and token not in stop_words and not token.isdigit():
            tokens.add(token)
    return tokens


def _reference_target_path(reference: object) -> str:
    return str(
        getattr(reference, "resolved_archive_path", "")
        or getattr(reference, "reference_name", "")
        or ""
    ).replace("\\", "/").strip()


def _replacement_output_texture_path(source_slot: ReplacementTextureSlot, target_path: str) -> str:
    del source_slot
    normalized_target = str(target_path or "").replace("\\", "/").strip()
    if normalized_target:
        return normalized_target
    return "character/texture/static_replacement.dds"


def _is_shared_material_layer_texture(target_path: str) -> bool:
    basename = PurePosixPath(str(target_path or "").replace("\\", "/")).name.lower()
    return basename.startswith("cd_texturelayer_") or basename.startswith("cd_temp")


def _should_replace_original_texture_reference(reference: object, target_path: str) -> bool:
    if str(getattr(reference, "reference_kind", "texture") or "texture").strip().lower() != "texture":
        return False
    if not str(target_path or "").lower().endswith(".dds"):
        return False
    parameter = str(getattr(reference, "sidecar_parameter_name", "") or "").strip().lower()
    basename = PurePosixPath(str(target_path or "").replace("\\", "/")).name.lower()

    # These are shared dye/grime/detail layers used by many materials. Replacing
    # them for one imported OBJ causes broad side effects and also tricks missing
    # base-color detection into thinking a material already has a direct diffuse.
    if _is_shared_material_layer_texture(target_path):
        return False

    if parameter in {
        "_normaltexture",
        "_heighttexture",
        "_overlaycolortexture",
        "_basecolortexture",
        "_diffusetexture",
        "_albedotexture",
        "_colorblendingmasktexture",
        "_detailmasktexture",
    }:
        return True
    if parameter.startswith("_grime") or parameter.startswith("_detail"):
        return False
    if not parameter:
        return any(token in basename for token in ("_o.dds", "_n.dds", "_disp.dds"))
    return False


def _reference_belongs_to_active_static_target(
    reference: object,
    target_path: str,
    target_to_source_material: Mapping[str, str],
) -> bool:
    """Keep texture generation scoped to original slots that receive replacement geometry.

    Static replacement mappings may intentionally leave original draw sections empty.
    Sidecar discovery can still expose those sections, and some recovered preview
    metadata can assign the replacement material name to unrelated texture paths.
    The texture path itself is therefore used as a second guard so a blade-only
    replacement does not generate acc/guard/handle DDS payloads.
    """
    if not target_to_source_material:
        return False
    material_name = str(getattr(reference, "material_name", "") or "").strip()
    path_text = PurePosixPath(str(target_path or "").replace("\\", "/")).stem
    for active_target in target_to_source_material.keys():
        active_name = str(active_target or "").strip()
        if not active_name:
            continue
        path_matches_active = _sidecar_material_names_match(path_text, active_name) or _active_target_tokens_match_path(active_name, path_text)
        path_conflicts_active = _active_target_tokens_conflict_path(active_name, path_text)
        if material_name and _sidecar_material_names_match(material_name, active_name) and not path_conflicts_active:
            return True
        if path_matches_active:
            return True
    return False


def _important_material_tokens(value: str) -> set[str]:
    return _semantic_tokens(value) & {
        "acc",
        "accessory",
        "blade",
        "body",
        "cape",
        "cloth",
        "edge",
        "guard",
        "handle",
        "helmet",
        "hilt",
        "plate",
        "trim",
    }


def _active_target_tokens_conflict_path(active_target: str, path_text: str) -> bool:
    path_tokens = _important_material_tokens(path_text)
    active_tokens = _important_material_tokens(active_target)
    return bool(path_tokens and active_tokens and not (path_tokens & active_tokens))


def _active_target_tokens_match_path(active_target: str, path_text: str) -> bool:
    active_tokens = _semantic_tokens(active_target)
    path_tokens = _semantic_tokens(path_text)
    if not active_tokens or not path_tokens:
        return False
    important_path_tokens = _important_material_tokens(path_text)
    important_active_tokens = _important_material_tokens(active_target)
    if important_path_tokens and important_active_tokens:
        return bool(important_path_tokens & important_active_tokens)
    return bool(path_tokens & active_tokens)


def _is_direct_base_color_mapping(mapping: TextureSlotMapping) -> bool:
    if str(mapping.slot_kind or "").strip().lower() != "base":
        return False
    target_path = str(mapping.target_texture_path or "").replace("\\", "/").strip()
    if not target_path:
        return False
    if target_path.startswith("("):
        return True
    if _is_shared_material_layer_texture(target_path):
        return False
    basename = PurePosixPath(target_path).name.lower()
    return (
        basename.endswith("_o.dds")
        or "base" in basename
        or "diffuse" in basename
        or "albedo" in basename
        or "color" in basename
    )


def _needs_missing_base_color_parameter_payloads(
    *,
    texture_sets: Mapping[str, ReplacementTextureSet],
    target_to_source_material: Mapping[str, str],
    existing_slot_mappings: Sequence[TextureSlotMapping],
    original_sidecars: Sequence[tuple[object, str]],
) -> bool:
    if not original_sidecars:
        return False
    base_mapped_targets = {
        str(mapping.target_material_name or "").strip().lower()
        for mapping in existing_slot_mappings
        if _is_direct_base_color_mapping(mapping)
    }
    for target_material_name, source_material_name in target_to_source_material.items():
        target_key = str(target_material_name or "").strip().lower()
        if not target_key or target_key in base_mapped_targets:
            continue
        texture_set = texture_sets.get(str(source_material_name or "").strip().lower())
        if texture_set is not None and texture_set.slots.get("base") is not None:
            return True
    return False


def _infer_slot_kind(parameter_name: str, texture_path: str) -> str:
    text = f"{parameter_name} {texture_path}".lower()
    normalized = re.sub(r"[^a-z0-9]+", "", text)
    if any(token in normalized for token in ("basecolor", "diffuse", "albedo", "colortexture")):
        return "base"
    if "normal" in normalized:
        return "normal"
    if any(token in normalized for token in ("height", "displacement", "disp", "bump", "parallax")):
        return "height"
    if any(token in normalized for token in ("material", "mask", "metallic", "roughness", "occlusion", "specular")):
        return "material"
    name = PurePosixPath(texture_path.replace("\\", "/")).name.lower()
    if name.endswith("_o.dds") or "_base" in name:
        return "base"
    if name.endswith("_n.dds"):
        return "normal"
    if name.endswith("_disp.dds") or name.endswith("_d.dds"):
        return "height"
    return "material"


def _slot_for_target(texture_set: ReplacementTextureSet, slot_kind: str) -> Optional[ReplacementTextureSlot]:
    if slot_kind in texture_set.slots:
        return texture_set.slots[slot_kind]
    if slot_kind == "material":
        for fallback in ("material", "metallic", "roughness", "ao"):
            if fallback in texture_set.slots:
                return texture_set.slots[fallback]
    if slot_kind == "base":
        return texture_set.slots.get("base")
    return None


def _build_missing_base_color_parameter_payloads(
    *,
    obj_mesh: ParsedMesh,
    texture_sets: Mapping[str, ReplacementTextureSet],
    original_texture_refs: Sequence[object],
    target_to_source_material: Mapping[str, str],
    existing_slot_mappings: Sequence[TextureSlotMapping],
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    report: TextureReplacementReport,
    on_log: Optional[Callable[[str], None]],
) -> tuple[list[TextureReplacementPayload], list[SidecarTextureParameterInjection]]:
    del obj_mesh
    base_mapped_targets = {
        str(mapping.target_material_name or "").strip().lower()
        for mapping in existing_slot_mappings
        if _is_direct_base_color_mapping(mapping)
    }
    template_reference = _base_color_template_reference(original_texture_refs)
    if template_reference is None or getattr(template_reference, "resolved_entry", None) is None:
        report.warnings.append(
            "Missing base-color parameter injection was requested, but no existing base/overlay texture parameter was available to clone."
        )
        return [], []

    generated_payloads: list[TextureReplacementPayload] = []
    injections: list[SidecarTextureParameterInjection] = []
    emitted_targets: set[str] = set()
    for target_material_name, source_material_name in target_to_source_material.items():
        target_key = str(target_material_name or "").strip().lower()
        if not target_key or target_key in base_mapped_targets or target_key in emitted_targets:
            continue
        texture_set = texture_sets.get(str(source_material_name or "").strip().lower())
        base_slot = texture_set.slots.get("base") if texture_set is not None else None
        if base_slot is None:
            continue
        output_texture_path = _infer_base_color_path_for_material(
            original_texture_refs,
            target_material_name,
            fallback_parent=_reference_target_parent(template_reference),
        )
        if not output_texture_path:
            report.warnings.append(
                f"Could not infer an original-style base color path for {target_material_name}; skipping injected _overlayColorTexture."
            )
            continue
        try:
            payload = _build_texture_payload(
                base_slot,
                target_entry=getattr(template_reference, "resolved_entry", None),
                texconv_path=texconv_path,
                read_original_texture_bytes=read_original_texture_bytes,
                original_texture_source_path=original_texture_source_path,
                report=report,
                on_log=on_log,
            )
        except Exception as exc:
            report.errors.append(
                f"Failed to build injected base-color texture for {target_material_name}: {exc}"
            )
            continue
        generated_payloads.append(
            TextureReplacementPayload(
                target_path=output_texture_path,
                payload_data=payload,
                kind="texture_generated",
                source_path=base_slot.source_path,
                note=f"Injected _overlayColorTexture for {target_material_name}: {base_slot.source_path.name}",
            )
        )
        report.slot_mappings.append(
            TextureSlotMapping(
                target_material_name=target_material_name,
                target_texture_path="(injected _overlayColorTexture)",
                slot_kind="base",
                source_material_name=base_slot.material_name,
                source_path=base_slot.source_path,
                output_texture_path=output_texture_path,
                normal_space=base_slot.normal_space,
            )
        )
        injections.append(
            SidecarTextureParameterInjection(
                target_material_name=target_material_name,
                parameter_name="_overlayColorTexture",
                texture_path=output_texture_path,
            )
        )
        emitted_targets.add(target_key)
        report.warnings.append(
            f"Sidecar patch: added _overlayColorTexture for {target_material_name} using {base_slot.source_path.name}."
        )
    return generated_payloads, injections


def _base_color_template_reference(original_texture_refs: Sequence[object]) -> Optional[object]:
    best: Optional[object] = None
    best_score = -1
    for reference in original_texture_refs:
        target_path = _reference_target_path(reference)
        if not target_path or getattr(reference, "resolved_entry", None) is None:
            continue
        if _is_shared_material_layer_texture(target_path):
            continue
        slot_kind = _infer_slot_kind(str(getattr(reference, "sidecar_parameter_name", "") or ""), target_path)
        if slot_kind != "base":
            continue
        parameter = str(getattr(reference, "sidecar_parameter_name", "") or "").strip().lower()
        score = 10
        if parameter == "_overlaycolortexture":
            score += 20
        elif parameter in {"_basecolortexture", "_diffusetexture", "_albedotexture"}:
            score += 15
        if score > best_score:
            best = reference
            best_score = score
    return best


def _reference_target_parent(reference: object) -> str:
    target_path = _reference_target_path(reference)
    parent = PurePosixPath(target_path.replace("\\", "/")).parent
    return "" if str(parent) in {"", "."} else parent.as_posix()


def _infer_base_color_path_for_material(
    original_texture_refs: Sequence[object],
    target_material_name: str,
    *,
    fallback_parent: str = "character/texture",
) -> str:
    target_key = _normalize_sidecar_material_name(target_material_name)
    support_candidates: list[str] = []
    fuzzy_support_candidates: list[str] = []
    base_candidates: list[str] = []
    fuzzy_base_candidates: list[str] = []
    for reference in original_texture_refs:
        material_name = str(getattr(reference, "material_name", "") or "")
        material_key = _normalize_sidecar_material_name(material_name)
        exact_material_match = bool(target_key and material_key and target_key == material_key)
        fuzzy_material_match = bool(
            target_key
            and material_name
            and not exact_material_match
            and _sidecar_material_names_match(target_material_name, material_name)
        )
        if target_key and material_name and not exact_material_match and not fuzzy_material_match:
            continue
        target_path = _reference_target_path(reference)
        if not target_path.lower().endswith(".dds"):
            continue
        slot_kind = _infer_slot_kind(str(getattr(reference, "sidecar_parameter_name", "") or ""), target_path)
        if slot_kind == "base" and not _is_shared_material_layer_texture(target_path):
            if exact_material_match:
                base_candidates.append(target_path)
            else:
                fuzzy_base_candidates.append(target_path)
        elif exact_material_match:
            support_candidates.append(target_path)
        else:
            fuzzy_support_candidates.append(target_path)
    if base_candidates:
        return base_candidates[0].replace("\\", "/")
    for candidate in support_candidates:
        inferred = _infer_base_color_path_from_support_texture(candidate)
        if inferred:
            return inferred
    if fuzzy_base_candidates:
        return fuzzy_base_candidates[0].replace("\\", "/")
    for candidate in fuzzy_support_candidates:
        inferred = _infer_base_color_path_from_support_texture(candidate)
        if inferred:
            return inferred
    material_token = re.sub(r"[^a-z0-9]+", "_", str(target_material_name or "").lower()).strip("_")
    if not material_token:
        return ""
    parent = str(fallback_parent or "character/texture").replace("\\", "/").strip("/")
    return f"{parent}/{material_token}.dds" if parent else f"{material_token}.dds"


def _infer_base_color_path_from_support_texture(texture_path: str) -> str:
    normalized = str(texture_path or "").replace("\\", "/").strip()
    if not normalized.lower().endswith(".dds"):
        return ""
    parent = PurePosixPath(normalized).parent
    stem = Path(PurePosixPath(normalized).name).stem
    lowered_stem = stem.lower()
    suffixes = (
        "_normal",
        "_n",
        "_disp",
        "_height",
        "_d",
        "_ma",
        "_mg",
        "_sp",
        "_m",
        "_mask",
        "_roughness",
        "_metallic",
    )
    for suffix in suffixes:
        if lowered_stem.endswith(suffix) and len(stem) > len(suffix):
            base_name = stem[: -len(suffix)] + ".dds"
            return f"{parent.as_posix()}/{base_name}" if str(parent) not in {"", "."} else base_name
    return ""


def _inject_sidecar_texture_parameter(
    sidecar_text: str,
    injection: SidecarTextureParameterInjection,
    report: SidecarPatchReport,
) -> tuple[str, bool]:
    target_name = str(injection.target_material_name or "").strip()
    texture_path = str(injection.texture_path or "").strip()
    parameter_name = str(injection.parameter_name or "_overlayColorTexture").strip() or "_overlayColorTexture"
    if not target_name or not texture_path:
        return sidecar_text, False
    wrapper_match = _find_sidecar_material_wrapper(sidecar_text, target_name)
    if wrapper_match is None:
        report.warnings.append(f"Could not find sidecar material wrapper for injected texture target: {target_name}")
        return sidecar_text, False
    wrapper_text = wrapper_match.group(0)
    if re.search(rf'_name="{re.escape(parameter_name)}"', wrapper_text, flags=re.IGNORECASE):
        report.unchanged_count += 1
        return sidecar_text, False
    template = _sidecar_texture_parameter_template(sidecar_text, parameter_name)
    next_index = _next_material_parameter_index(wrapper_text)
    parameter_text = _retarget_texture_parameter_template(template, parameter_name, texture_path, next_index)
    parameter_vector_match = re.search(
        r'(<Vector\s+Name="_parameters"\s*>)(.*?)(\s*</Vector>)',
        wrapper_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if parameter_vector_match is None:
        report.warnings.append(f"Could not find _parameters vector for injected texture target: {target_name}")
        return sidecar_text, False
    new_wrapper_text = (
        wrapper_text[: parameter_vector_match.start(3)]
        + "\n\t\t\t\t\t\t\t"
        + parameter_text
        + wrapper_text[parameter_vector_match.start(3) :]
    )
    return (
        sidecar_text[: wrapper_match.start()]
        + new_wrapper_text
        + sidecar_text[wrapper_match.end() :],
        True,
    )


def _find_sidecar_material_wrapper(sidecar_text: str, target_name: str) -> Optional[re.Match[str]]:
    normalized_target = _normalize_sidecar_material_name(target_name)
    fallback: Optional[tuple[float, re.Match[str]]] = None
    wrapper_pattern = re.compile(
        r"<SkinnedMeshMaterialWrapper\b[^>]*>.*?</SkinnedMeshMaterialWrapper>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in wrapper_pattern.finditer(sidecar_text):
        name_match = re.search(r'_subMeshName="([^"]+)"', match.group(0), flags=re.IGNORECASE)
        if name_match and _normalize_sidecar_material_name(name_match.group(1)) == normalized_target:
            return match
        if name_match:
            score = _sidecar_material_match_score(target_name, name_match.group(1))
            if score > 0 and (fallback is None or score > fallback[0]):
                fallback = (score, match)
    if fallback is not None and fallback[0] >= 6.0:
        return fallback[1]
    return None


def _sidecar_material_names_match(left: str, right: str) -> bool:
    left_normalized = _normalize_sidecar_material_name(left)
    right_normalized = _normalize_sidecar_material_name(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    if len(left_normalized) >= 8 and left_normalized in right_normalized:
        return True
    if len(right_normalized) >= 8 and right_normalized in left_normalized:
        return True
    return _sidecar_material_match_score(left, right) >= 6.0


def _sidecar_material_match_score(left: str, right: str) -> float:
    left_tokens = _material_tokens(left)
    right_tokens = _material_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    score = float(len(overlap) * 4)
    for token in overlap:
        score += min(4.0, len(token) * 0.5)
    return score


def _normalize_sidecar_material_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _sidecar_texture_parameter_template(sidecar_text: str, parameter_name: str) -> str:
    parameter_match = re.search(
        rf"<MaterialParameterTexture\b[^>]*(?:StringItemID|_name)=\"{re.escape(parameter_name)}\"[^>]*>.*?</MaterialParameterTexture>",
        sidecar_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if parameter_match is not None:
        return parameter_match.group(0).strip()
    item_id = "1" if parameter_name == "_overlayColorTexture" else "0"
    return (
        f'<MaterialParameterTexture StringItemID="{parameter_name}" ItemID="{item_id}" _name="{parameter_name}" Index="0">\n'
        f'\t\t\t\t\t\t\t\t<ResourceReferencePath_ITexture Name="_value" _path=""/>\n'
        f"\t\t\t\t\t\t\t</MaterialParameterTexture>"
    )


def _next_material_parameter_index(wrapper_text: str) -> int:
    indexes = []
    for raw_index in re.findall(r'\bIndex="(\d+)"', wrapper_text):
        try:
            indexes.append(int(raw_index))
        except ValueError:
            continue
    return max(indexes, default=-1) + 1


def _retarget_texture_parameter_template(
    template: str,
    parameter_name: str,
    texture_path: str,
    index: int,
) -> str:
    patched = template.strip()
    patched = re.sub(r'StringItemID="[^"]*"', f'StringItemID="{parameter_name}"', patched, count=1)
    patched = re.sub(r'_name="[^"]*"', f'_name="{parameter_name}"', patched, count=1)
    patched = re.sub(r'Index="\d+"', f'Index="{int(index)}"', patched, count=1)
    if '_path="' in patched:
        patched = re.sub(r'_path="[^"]*"', f'_path="{_escape_xml_attr(texture_path)}"', patched, count=1)
    else:
        patched = patched.replace(
            "</MaterialParameterTexture>",
            f'\n\t\t\t\t\t\t\t\t<ResourceReferencePath_ITexture Name="_value" _path="{_escape_xml_attr(texture_path)}"/>\n\t\t\t\t\t\t\t</MaterialParameterTexture>',
        )
    return patched


def _escape_xml_attr(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _append_unused_texture_warnings(
    texture_sets: Mapping[str, ReplacementTextureSet],
    report: TextureReplacementReport,
) -> None:
    used = {
        (
            str(mapping.source_material_name or "").strip().lower(),
            str(mapping.source_path.name or "").strip().lower(),
        )
        for mapping in report.slot_mappings
    }
    for texture_set in texture_sets.values():
        unused_slots = [
            slot
            for slot in texture_set.slots.values()
            if (
                str(slot.material_name or "").strip().lower(),
                str(slot.source_path.name or "").strip().lower(),
            )
            not in used
        ]
        if unused_slots:
            report.warnings.append(
                f"{texture_set.material_name}: {len(unused_slots)} source texture(s) were not mapped to existing material parameters: "
                + ", ".join(slot.source_path.name for slot in unused_slots[:6])
                + (" ..." if len(unused_slots) > 6 else "")
            )


def _build_texture_payload(
    source_slot: ReplacementTextureSlot,
    *,
    target_entry: object,
    texconv_path: Optional[Path],
    read_original_texture_bytes: Callable[[object], bytes],
    original_texture_source_path: Callable[[object], Path],
    report: TextureReplacementReport,
    on_log: Optional[Callable[[str], None]],
) -> bytes:
    from crimson_forge_toolkit.core.pipeline import build_texconv_command, max_mips_for_size, parse_dds
    from crimson_forge_toolkit.core.common import run_process_with_cancellation

    if source_slot.source_path.suffix.lower() == ".dds":
        source_info = parse_dds(source_slot.source_path)
        original_info = parse_dds(original_texture_source_path(target_entry))
        mismatch_parts: list[str] = []
        if (source_info.width, source_info.height) != (original_info.width, original_info.height):
            mismatch_parts.append(
                f"size {source_info.width}x{source_info.height} != original {original_info.width}x{original_info.height}"
            )
        if source_info.texconv_format != original_info.texconv_format:
            mismatch_parts.append(f"format {source_info.texconv_format} != original {original_info.texconv_format}")
        if int(source_info.mip_count or 1) != int(original_info.mip_count or 1):
            mismatch_parts.append(f"mips {source_info.mip_count or 1} != original {original_info.mip_count or 1}")
        if mismatch_parts:
            report.warnings.append(
                f"DDS replacement {source_slot.source_path.name} differs from target template: {', '.join(mismatch_parts)}."
            )
        return source_slot.source_path.read_bytes()
    if texconv_path is None or not texconv_path.expanduser().is_file():
        raise FileNotFoundError("texconv.exe is required to convert image replacement textures to DDS.")

    original_source = original_texture_source_path(target_entry)
    original_info = parse_dds(original_source)
    resolved_texconv = texconv_path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="cft_static_texture_") as temp_text:
        temp_dir = Path(temp_text)
        source_png = source_slot.source_path
        prepared_png = temp_dir / source_png.name
        if source_slot.slot_kind == "normal" and source_slot.normal_space == "opengl":
            _copy_png_with_inverted_green(source_png, prepared_png)
            report.warnings.append(f"Inverted green channel for OpenGL normal map: {source_png.name}")
        else:
            shutil.copy2(source_png, prepared_png)
        out_dir = temp_dir / "dds"
        out_dir.mkdir(parents=True, exist_ok=True)
        mip_count = max(1, min(max_mips_for_size(original_info.width, original_info.height), int(original_info.mip_count or 1)))
        cmd = build_texconv_command(
            resolved_texconv,
            prepared_png,
            out_dir,
            original_info.texconv_format,
            mip_count,
            original_info.width,
            original_info.height,
            overwrite_existing_dds=True,
        )
        if on_log:
            on_log(f"Converting {source_png.name} -> {getattr(target_entry, 'path', 'texture')} ({original_info.texconv_format})")
        return_code, stdout, stderr = run_process_with_cancellation(cmd)
        if return_code != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"texconv exited with code {return_code}")
        produced = out_dir / f"{prepared_png.stem}.dds"
        if not produced.is_file():
            raise FileNotFoundError(f"texconv did not produce {produced.name}")
        return produced.read_bytes()


def _copy_png_with_inverted_green(source_path: Path, target_path: Path) -> None:
    from PIL import Image

    with Image.open(source_path) as image:
        rgba = image.convert("RGBA")
        r, g, b, a = rgba.split()
        g = g.point(lambda value: 255 - int(value))
        Image.merge("RGBA", (r, g, b, a)).save(target_path)
